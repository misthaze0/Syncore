/**
 * 工作空间状态（Zustand）—— 工作空间 / 群组 / 数字员工 / 群组消息
 *
 * ★ 与 agentStore 的分工
 * agentStore 管「主对话 + 后台子任务」，这里管「工作空间」这一块新领域。
 * 两者各管各的，不互相塞状态 —— 群组消息的生命周期跟主对话无关，
 * 员工私聊更是**只对用户可见**、不进任何群组状态（需求 3、4 的界面侧隔离）。
 *
 * ★ 员工私聊不放这里
 * 私聊是「点开某个员工 → 单独一段对话」，属于页面级、易逝的状态；
 * 而这里是跨页面共享的「工作空间结构 + 群组时间线」。私聊的流式状态
 * 由 EmployeeChatPage 自己持有（`employeeChatStream`），不污染全局。
 */

import { create } from 'zustand';

import {
  addGroupMember as addGroupMemberApi,
  answerQuestion as answerQuestionApi,
  ApiError,
  cancelRun as cancelRunApi,
  createEmployee as createEmployeeApi,
  createWorkspace as createWorkspaceApi,
  deleteEmployee as deleteEmployeeApi,
  deleteWorkspace as deleteWorkspaceApi,
  fetchEmployees,
  fetchGroup as fetchGroupApi,
  fetchGroupMessages,
  fetchGroups,
  fetchWorkspaceTools,
  fetchWorkspaces,
  sendGroupMessage,
  QuestionExpiredError,
  removeGroupMember as removeGroupMemberApi,
  submitApproval,
  updateEmployee as updateEmployeeApi,
} from '@/lib/api';
import type { GroupMessageOptions } from '@/lib/api';
import { askedFromPayload } from '@/lib/questions';
import type {
  AskedQuestion,
  BoardCard,
  BaseAgent,
  Employee,
  Group,
  GroupMessage,
  GroupStreamEvent,
  GuardrailNotice,
  GroupApprovalRequest,
  QuestionAnswers,
  RunSummary,
  EmployeeCapabilityOption,
  Workspace,
} from '@/types';

let groupViewVersion = 0;
const messageRequestIds = new Map<string, string>();

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * 卡片事件的落地方式：有则整份覆盖，无则追加。
 *
 * ★ 为什么是「覆盖」而不是「挑几个字段改」
 * 后端载荷就是这张卡的完整快照（`events.card_summary`）。前端若只改 `status`，
 * 那么 `owner_employee_id` / `task_id` / `blocked_reason` 这几列会停在旧值 ——
 * 而「显示着有人在做、实际上 owner 已经被释放」正是任务板上最骗人的一格。
 */
function upsertCard(cards: BoardCard[], card: BoardCard): BoardCard[] {
  const idx = cards.findIndex((c) => c.card_id === card.card_id);
  if (idx < 0) return [...cards, card];
  const next = cards.slice();
  next[idx] = card;
  return next;
}

export interface EmployeePayload {
  workspace_id: string;
  name: string;
  model?: string;
  model_profile_id?: string;
  tagline?: string;
  tone?: string;
  tone_custom?: string;
  instructions?: string;
  scope?: string;
  base_agent?: BaseAgent;
  tools_allowed?: string[];
  enabled?: boolean;
}

interface WorkspaceStore {
  workspaces: Workspace[];
  workspacesLoading: boolean;
  selectedWorkspaceId: string | null;
  groups: Group[];
  employees: Employee[];
  tools: EmployeeCapabilityOption[];
  toolsLoading: boolean;
  toolsError: string | null;
  error: string | null;

  // ---------- 群组时间线 ----------
  currentGroup: Group | null;
  messages: GroupMessage[];
  messagesTotal: number;
  orchestrateBusy: boolean;
  /**
   * 最近一次发起拿到的 run_id（阶段 1 起 run 是数据库里的行，它有地址了）。
   *
   * ★ 为什么值得占一个字段
   * 改之前 `orchestrate` 把返回值整个丢掉，于是「我刚才那一次」在界面上不存在：
   * 时间线要等第一条 SSE 消息才有动静，那段等待期没有任何东西可对账，
   * 而 409 报回来的「正在跑的 run」也无处安放。阶段 2 的 run 卡片跟着它走。
   */
  activeRunId: string | null;

  // ---------- 阶段 2：run / 任务板 / 待审批 / 护栏 ----------
  /**
   * 当前该显示的 run（在跑的，或最近一次已结束的）。
   *
   * ★ 为什么连带「已结束的那次」也留着
   * 只存活跃 run 的话，协作跑完的下一帧看板就凭空消失了 —— 用户看到的是
   * 「数据没了」，而实际上是「跑完了」。
   */
  run: RunSummary | null;
  cards: BoardCard[];
  /** 群组频道上等待用户决策的危险工具（护栏 6：一次批准只放行它自己的那个人） */
  pendingApprovals: GroupApprovalRequest[];
  /**
   * 群组频道上等待用户回答的模型提问（`ask_user`）。
   *
   * 与审批**并列**、不合并：审批有风险、回答没有；审批是二元，
   * 回答是多选 + 自由输入。混成一个列表迟早会把安全语义搅进闲聊语义里。
   */
  pendingQuestions: AskedQuestion[];
  /** 护栏触发过哪几条 —— 「为什么这件事停在这儿」的界面答案 */
  guardrails: GuardrailNotice[];
  /** 已消费到的 `messages.rowid`：断线重连时按它补拉，不重放全量 */
  latestSeq: number;

  // ---------- 工作空间 ----------
  loadWorkspaces: () => Promise<void>;
  createWorkspace: (name: string, description: string) => Promise<Workspace | null>;
  deleteWorkspace: (wsId: string) => Promise<void>;
  selectWorkspace: (wsId: string) => void;
  loadWorkspaceDetail: () => Promise<void>;
  loadTools: () => Promise<void>;

  // ---------- 员工 ----------
  createEmployee: (payload: EmployeePayload) => Promise<Employee | null>;
  updateEmployee: (employeeId: string, payload: Partial<Omit<EmployeePayload, 'workspace_id'>>) => Promise<boolean>;
  deleteEmployee: (employeeId: string) => Promise<void>;

  // ---------- 群组 ----------
  /**
   * 换群前**同步**清掉上一屏（时间线 / run / 看板 / 待审批 / 游标）。
   *
   * ★ 为什么单独一个动作而不是塞进 `openGroup`
   * `openGroup` 里有一次 await，清状态放在它后面就会盖掉先到的那一发快照。
   * 放在调用方的 effect 开头，顺序才是确定的。
   */
  resetGroupView: () => void;
  openGroup: (groupId: string) => Promise<void>;
  loadMessages: (groupId: string) => Promise<void>;
  /**
   * 按 `latestSeq` 游标补拉断线窗口里漏掉的消息（追加而非整屏重放）。
   *
   * 游标是 `messages.rowid`：`messages.id` 是随机 hex、不可比较，所以做不了
   * Last-Event-ID 式补拉 —— 这是本项目唯一能用的顺序键。
   */
  backfillMessages: (groupId: string) => Promise<void>;
  applyGroupEvent: (event: GroupStreamEvent) => void;
  /** 关掉一条护栏通知（只关界面提示，不改任何状态 —— 原因仍在时间线与审计里） */
  dismissGuardrail: (index: number) => void;
  orchestrate: (groupId: string, instruction: string, options?: GroupMessageOptions) => Promise<void>;
  cancelRun: () => Promise<void>;
  /**
   * 对一条待审批下决策。四元组里的 `run_id` / `employee_id` 从事件载荷里原样回传，
   * 少一维后端就匹配不上（宁可报「该审批已过期」，也不让甲的批准放行乙的危险操作）。
   */
  resolveApproval: (callId: string, approved: boolean) => Promise<void>;
  /**
   * 回答群组里的一张提问卡。
   *
   * 四元组 + question_id 原样回传（与主对话同一条 API）；
   * 成功后不本地摘卡，等后端的 `question_resolved` —— 与审批同一纪律。
   */
  resolveQuestion: (questionId: string, answers: QuestionAnswers, skipped?: boolean) => Promise<void>;
  addMember: (groupId: string, employeeId: string) => Promise<void>;
  removeMember: (groupId: string, employeeId: string) => Promise<void>;

  clearError: () => void;
}

export const useWorkspaceStore = create<WorkspaceStore>((set, get) => ({
  workspaces: [],
  workspacesLoading: false,
  selectedWorkspaceId: null,
  groups: [],
  employees: [],
  tools: [],
  toolsLoading: false,
  toolsError: null,
  error: null,

  currentGroup: null,
  messages: [],
  messagesTotal: 0,
  orchestrateBusy: false,
  activeRunId: null,
  run: null,
  cards: [],
  pendingApprovals: [],
  pendingQuestions: [],
  guardrails: [],
  latestSeq: 0,

  loadWorkspaces: async () => {
    set({ workspacesLoading: true, error: null });
    try {
      const resp = await fetchWorkspaces();
      const selected = get().selectedWorkspaceId;
      set({
        workspaces: resp.workspaces,
        workspacesLoading: false,
      });
      // 首次进入时，若还没选就选第一个
      if (!selected && resp.workspaces.length > 0) {
        set({ selectedWorkspaceId: resp.workspaces[0].id });
        void get().loadWorkspaceDetail();
      }
    } catch (e) {
      set({ workspacesLoading: false, error: errorText(e) });
    }
  },

  createWorkspace: async (name, description) => {
    try {
      const resp = await createWorkspaceApi({ name, description });
      set((s) => ({
        workspaces: [resp.workspace, ...s.workspaces],
        selectedWorkspaceId: resp.workspace.id,
        groups: [],
        employees: [],
        error: null,
      }));
      void get().loadWorkspaceDetail();
      return resp.workspace;
    } catch (e) {
      set({ error: errorText(e) });
      return null;
    }
  },

  deleteWorkspace: async (wsId) => {
    try {
      await deleteWorkspaceApi(wsId);
      set((s) => ({
        workspaces: s.workspaces.filter((w) => w.id !== wsId),
        selectedWorkspaceId: s.selectedWorkspaceId === wsId ? null : s.selectedWorkspaceId,
        groups: s.selectedWorkspaceId === wsId ? [] : s.groups,
        employees: s.selectedWorkspaceId === wsId ? [] : s.employees,
        error: null,
      }));
    } catch (e) {
      set({ error: errorText(e) });
    }
  },

  selectWorkspace: (wsId) => {
    if (get().selectedWorkspaceId === wsId) return;
    set({ selectedWorkspaceId: wsId, groups: [], employees: [], error: null });
    void get().loadWorkspaceDetail();
  },

  loadWorkspaceDetail: async () => {
    const wsId = get().selectedWorkspaceId;
    if (!wsId) return;
    try {
      const [groupsResp, employeesResp] = await Promise.all([
        fetchGroups(wsId),
        fetchEmployees(wsId),
      ]);
      if (get().selectedWorkspaceId !== wsId) return;
      set({ groups: groupsResp.groups, employees: employeesResp.employees });
    } catch (e) {
      set({ error: errorText(e) });
    }
  },

  loadTools: async () => {
    set({ toolsLoading: true, toolsError: null });
    try {
      const resp = await fetchWorkspaceTools();
      set({ tools: resp.tools, toolsLoading: false });
    } catch (error) {
      set({ toolsLoading: false, toolsError: errorText(error) });
    }
  },

  createEmployee: async (payload) => {
    try {
      const resp = await createEmployeeApi(payload);
      set((s) => ({ employees: [...s.employees, resp.employee], error: null }));
      return resp.employee;
    } catch (e) {
      set({ error: errorText(e) });
      return null;
    }
  },

  updateEmployee: async (employeeId, payload) => {
    try {
      const resp = await updateEmployeeApi(employeeId, payload);
      set((s) => ({
        employees: s.employees.map((e) => (e.id === employeeId ? resp.employee : e)),
        error: null,
      }));
      return true;
    } catch (e) {
      set({ error: errorText(e) });
      return false;
    }
  },

  deleteEmployee: async (employeeId) => {
    try {
      await deleteEmployeeApi(employeeId);
      set((s) => ({ employees: s.employees.filter((e) => e.id !== employeeId), error: null }));
    } catch (e) {
      set({ error: errorText(e) });
    }
  },

  openGroup: async (groupId) => {
    const version = groupViewVersion;
    try {
      const resp = await fetchGroupApi(groupId);
      if (version !== groupViewVersion) return;
      // ★ 这里**不清空**这一屏的状态 —— 清空是 `resetGroupView()` 的活。
      // 放在这里的话它是一次「异步完成后才落地」的覆盖：SSE 的快照通常比这个 REST
      // 先到，于是快照刚写进来就被一次换群清空抹掉。实测症状是「时间线只剩最后一条、
      // 看板只有刚被认领的那两张」，看上去像事件没发，其实是被抹了。
      set({ currentGroup: resp.group, error: null });
    } catch (e) {
      if (version === groupViewVersion) set({ error: errorText(e) });
    }
  },

  resetGroupView: () => {
    groupViewVersion += 1;
    set({
      currentGroup: null,
      messages: [],
      messagesTotal: 0,
      activeRunId: null,
      run: null,
      cards: [],
      pendingApprovals: [],
      pendingQuestions: [],
      guardrails: [],
      latestSeq: 0,
      error: null,
    });
  },

  loadMessages: async (groupId) => {
    const version = groupViewVersion;
    try {
      const resp = await fetchGroupMessages(groupId);
      if (version !== groupViewVersion) return;
      set({
        messages: resp.messages,
        messagesTotal: resp.total,
        latestSeq: resp.messages.reduce((max, m) => Math.max(max, m.seq ?? 0), 0),
      });
    } catch (e) {
      set({ error: errorText(e) });
    }
  },

  backfillMessages: async (groupId) => {
    const version = groupViewVersion;
    const since = get().latestSeq;
    if (since <= 0) {
      void get().loadMessages(groupId);
      return;
    }
    try {
      const resp = await fetchGroupMessages(groupId, { afterSeq: since });
      if (version !== groupViewVersion) return;
      const fresh = resp.messages.filter((m) => !get().messages.some((o) => o.id === m.id));
      if (fresh.length === 0) return;
      set((s) => ({
        messages: [...s.messages, ...fresh],
        messagesTotal: s.messagesTotal + fresh.length,
        latestSeq: fresh.reduce((max, m) => Math.max(max, m.seq ?? 0), s.latestSeq),
      }));
    } catch (e) {
      set({ error: errorText(e) });
    }
  },

  /**
   * 群组 SSE 事件 → 界面状态。
   *
   * ★ 这里的 `case` 清单与后端 `workspace/events.py::GROUP_EVENT_TYPES` 是同一份约定
   * 由 `test_group_event_whitelist_matches_frontend_reducer` 钉住。两边各写各的时，
   * 少的那一侧**不报错**：后端发了前端不认 = 看板停在上一状态，而 SSE 通道本身看起来是通的
   * （消息事件还在动），于是这个 bug 会被一路当成「网络抖」查下去。
   */
  applyGroupEvent: (event) => {
    switch (event.type) {
      case 'snapshot': {
        // 快照是**整屏替换**，不是合并：库里没有的东西必须从界面上消失，
        // 否则一次「服务重启 + run 被判 expired」之后，看板还挂着上一轮的进行中卡片。
        set({
          messages: event.messages,
          messagesTotal: event.total,
          run: event.run ?? null,
          activeRunId: event.run && !event.run.terminal ? event.run.run_id : null,
          pendingApprovals: event.pending_approvals ?? [],
          pendingQuestions: (event.pending_questions ?? []).map((q) => askedFromPayload(q)),
          cards: event.cards ?? [],
          latestSeq: event.latest_seq ?? 0,
        });
        return;
      }
      case 'message': {
        const msg = event as GroupMessage;
        set((s) => {
          if (s.messages.some((m) => m.id === msg.id)) return { messages: s.messages.map(m => m.id === msg.id ? { ...m, ...msg } : m) };
          return {
            messages: [...s.messages, msg],
            messagesTotal: s.messagesTotal + 1,
            latestSeq: Math.max(s.latestSeq, msg.seq ?? 0),
          };
        });
        return;
      }
      case 'run_status': {
        // run 卡片是「这一次协作到哪了」的唯一入口：spinner / 取消按钮 / 终态文案全看它。
        // `activeRunId` 一并跟随终态清空，否则「取消」按钮会挂在一个已经结束的 run 上。
        set((s) => ({
          run: event.run,
          activeRunId: event.run.terminal ? null : event.run.run_id || s.activeRunId,
        }));
        return;
      }
      case 'card_created': {
        set((s) => ({ cards: upsertCard(s.cards, event.card) }));
        return;
      }
      case 'card_claimed': {
        // 抢卡成功那一刻 owner 就有了，`task_id` 可能还是空的（派发是下一步）。
        // 整份覆盖 —— 载荷本身就是完整卡片，前端不再自己拼字段。
        set((s) => ({ cards: upsertCard(s.cards, event.card) }));
        return;
      }
      case 'card_status': {
        // `previous_status === card.status` 表示「没迁移，只是链接变了」（挂 task）。
        set((s) => ({ cards: upsertCard(s.cards, event.card) }));
        return;
      }
      case 'approval_required': {
        set((s) =>
          s.pendingApprovals.some((p) => p.call_id === event.call_id && p.employee_id === event.employee_id)
            ? s
            : {
                pendingApprovals: [
                  ...s.pendingApprovals,
                  {
                    thread_id: event.thread_id,
                    call_id: event.call_id,
                    tool_name: event.tool_name,
                    tool_label: event.tool_label,
                    args: event.args,
                    reason: event.reason,
                    run_id: event.run_id,
                    employee_id: event.employee_id,
                    requestor: event.requestor,
                  },
                ],
              },
        );
        return;
      }
      case 'approval_resolved': {
        // 三种结局（批准 / 拒绝 / 超时）都会到这里 —— 少收超时的话那条审批卡
        // 会永远挂着，用户点下去只得到「未找到等待中的审批」，看着像系统坏了。
        set((s) => ({
          pendingApprovals: s.pendingApprovals.filter(
            (p) => !(p.call_id === event.call_id && p.employee_id === event.employee_id),
          ),
        }));
        return;
      }
      case 'question_required': {
        // 与审批同理：同一张卡重复到达（重连 / 重放）只留一份。
        // 去重键用 questionId —— 同一次 call_id 恢复后可能被**重新发起**，
        // 按 callId 去重会把新卡误判成旧卡，用户答了却唤醒不了任何人。
        const question = askedFromPayload({
          question_id: event.question_id,
          call_id: event.call_id,
          tool_name: event.tool_name,
          tool_label: event.tool_label,
          questions: event.questions,
          run_id: event.run_id,
          employee_id: event.employee_id,
          requestor: event.requestor,
          timeout_seconds: event.timeout_seconds,
          asked_at: event.asked_at,
          thread_id: event.thread_id,
        });
        set((s) =>
          s.pendingQuestions.some((q) => q.questionId === question.questionId)
            ? s
            : { pendingQuestions: [...s.pendingQuestions, question] },
        );
        return;
      }
      case 'question_resolved': {
        // 三种结局（回答 / 跳过 / 超时）都要收卡，否则超时的卡会永远挂着
        set((s) => ({
          pendingQuestions: s.pendingQuestions.filter(
            (q) => q.questionId !== event.question_id,
          ),
        }));
        return;
      }
      case 'guardrail_triggered': {
        set((s) => ({
          guardrails: [
            ...s.guardrails,
            {
              guardrail: event.guardrail,
              code: event.code,
              message: event.message,
              run_id: event.run_id,
              card_id: event.card_id,
            },
          ].slice(-5), // 只留最近 5 条：这是通知不是历史，历史归审计页
        }));
        return;
      }
      default:
        // 白名单外的名字（后端加了、前端还没跟上）直接忽略 —— 时间线与看板照旧工作，
        // 由那条三端一致性测试去红，而不是让界面在这里崩。
        return;
    }
  },

  resolveApproval: async (callId, approved) => {
    const target = get().pendingApprovals.find((p) => p.call_id === callId);
    if (!target) return;
    set({ error: null });
    try {
      await submitApproval(
        target.thread_id,
        callId,
        approved,
        target.run_id,
        target.employee_id,
      );
      // 成功时**不**本地摘除：后端的 `approval_resolved` 会推过来。
      // 自己删的话，一次网络抖动会让「已批准」和「还在等」两种状态各显示一遍。
    } catch (e) {
      // 后端把「没匹配上」做成了 200 + success=false，`submitApproval` 已转成异常。
      // 这种情况审批确实已经没了（多半是超时），所以本地那条要收掉，不能留一张死卡。
      set((s) => ({
        error: errorText(e),
        pendingApprovals: s.pendingApprovals.filter((p) => p.call_id !== callId),
      }));
    }
  },

  resolveQuestion: async (questionId, answers, skipped = false) => {
    const target = get().pendingQuestions.find((q) => q.questionId === questionId);
    if (!target) return;
    set({ error: null });
    try {
      await answerQuestionApi({
        threadId: target.threadId ?? '',
        callId: target.callId,
        questionId: target.questionId,
        answers,
        skipped,
        runId: target.runId,
        employeeId: target.employeeId,
      });
      // 成功时**不**本地摘卡：后端的 `question_resolved` 会推过来（与审批同一纪律）
    } catch (e) {
      if (e instanceof QuestionExpiredError) {
        // 卡已失效（超时 / 另一个窗口答过）：摘掉这条死卡并说明原因
        set((s) => ({
          error: errorText(e),
          pendingQuestions: s.pendingQuestions.filter((q) => q.questionId !== questionId),
        }));
        return;
      }
      // 网络错误：卡留在原地，用户可重试
      set({ error: errorText(e) });
      throw e;
    }
  },

  dismissGuardrail: (index) =>
    set((s) => ({ guardrails: s.guardrails.filter((_, i) => i !== index) })),

  orchestrate: async (groupId, instruction, options = {}) => {
    const trimmed = instruction.trim();
    if (!trimmed || get().orchestrateBusy) return;
    set({ orchestrateBusy: true, error: null });
    try {
      const requestKey = JSON.stringify([groupId, trimmed, options.recipient_employee_id, options.reply_to, options.intent]);
      const clientId = messageRequestIds.get(requestKey) || crypto.randomUUID();
      messageRequestIds.set(requestKey, clientId);
      const resp = await sendGroupMessage(groupId, trimmed, { ...options, client_id: clientId });
      messageRequestIds.delete(requestKey);
      // 成功**不占 error 通道**（那是错误横幅的样式）。run_id 进 activeRunId，
      // 界面上是一个能对账的标识 + 一个「取消」入口。
      set(s => ({ activeRunId: s.run?.run_id === resp.run_id && s.run.terminal ? null : resp.run_id }));
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // 双跑冲突是**业务结果**不是故障。后端带着正在跑的 run_id 回来，
        // 界面就说得出「在跑的是哪一次」，而不是让用户对着 409 猜。
        const detail = e.detail as { active_run_id?: string } | null;
        const running = detail?.active_run_id ?? '';
        set(running ? { error: e.message, activeRunId: running } : { error: e.message });
        return;
      }
      set({ error: errorText(e) });
    } finally {
      set({ orchestrateBusy: false });
    }
  },

  cancelRun: async () => {
    const runId = get().activeRunId;
    if (!runId) return;
    set({ error: null });
    try {
      const resp = await cancelRunApi(runId);
      if (!resp.ok) set({ error: resp.reason ?? '这次协作已经结束', activeRunId: null });
      else set({ activeRunId: null });
    } catch (e) {
      set({ error: errorText(e) });
    }
  },

  addMember: async (groupId, employeeId) => {
    try {
      await addGroupMemberApi(groupId, employeeId);
      // 成员数变了，重拉一次群组（成员列表跟着更新）
      const resp = await fetchGroupApi(groupId);
      set({ currentGroup: resp.group, error: null });
    } catch (e) {
      set({ error: errorText(e) });
    }
  },

  removeMember: async (groupId, employeeId) => {
    try {
      await removeGroupMemberApi(groupId, employeeId);
      const resp = await fetchGroupApi(groupId);
      set({ currentGroup: resp.group, error: null });
    } catch (e) {
      set({ error: errorText(e) });
    }
  },

  clearError: () => set({ error: null }),
}));
