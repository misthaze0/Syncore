/**
 * Agent 运行时状态管理（Zustand）
 *
 * 集中管理跨组件共享的 Agent 状态：
 * - messages      消息列表（含每轮的 sources / agentChain / toolCalls 快照）
 * - agentChain    当前 Agent 调用链
 * - toolCalls     工具调用记录（含审批状态）
 * - pendingApproval 待审批操作（驱动 ApprovalDialog）
 * - intent        当前意图
 * - conversations 会话历史列表（Phase 7）
 *
 * SSE 事件在这里被分发到上述状态，组件只负责渲染。
 *
 * ★ 会话历史的恢复为什么是「重放事件」而不是「直接塞渲染好的消息」
 *
 * 后端在每轮结束时存下这一轮的**事件流快照**（`token` / `done` 除外）。
 * 恢复一个历史会话时，前端把那些事件重新喂给**同一个** `applyAgentEvent` ——
 * 也就是实时流用的那一个。这样工具卡片的状态机（pending → awaiting_approval →
 * success/rejected）、引用来源、审批留痕、记忆回执的渲染逻辑就只有一份，
 * 不需要在恢复路径上再实现一遍（那才会长出「历史会话和当时看起来不一样」的偏差）。
 */

import { approvalToolStates } from '@/lib/messagePresentation';
import { askedFromPayload } from '@/lib/questions';
import { useWorkdirStore } from '@/stores/workdirStore';
import { toast } from '@/stores/toastStore';
import { create } from 'zustand';
import type { PetVoiceTurn, PetVoiceUpdate } from '@/types/desktop';

import {
  AUTO_WORKDIR_ID,
  answerQuestion as answerQuestionApi,
  cancelTask as cancelTaskApi,
  checkinAssistant,
  clearConversations,
  createThreadId,
  deleteConversation,
  deleteConversationTurn,
  fetchConversation,
  fetchConversations,
  isAssistantThreadId,
  fetchModels,
  fetchPermissionMode,
  fetchServiceInfo,
  fetchTasks,
  forkConversation as forkConversationApi,
  invalidateServiceInfo,
  renameConversation as renameConversationApi,
  resetThreadId,
  QuestionExpiredError,
  resumeConversation,
  resumeTask as resumeTaskApi,
  streamChat,
  reportChatLifecycle,
  submitApproval,
  type ServiceInfo,
  type ThreadScope,
  updateLlmModel,
  uploadFile,
  writeThreadId,
} from '@/lib/api';
import type {
  AgentEvent,
  AskedQuestion,
  AttachmentMeta,
  AutoApprovalNote,
  ChatMessage,
  ConversationSummary,
  DangerLevel,
  FileUploadResponse,
  HookNoticeNote,
  ModelOption,
  PendingApproval,
  PendingQuestion,
  PermissionModeInfo,
  PermissionModeValue,
  QuestionAnswers,
  QuestionPayload,
  SourceDoc,
  SubtaskDTO,
  SubtaskProgressStep,
  SubtaskStatus,
  TaskStreamEvent,
  ToolCallInfo,
} from '@/types';

/** 已上传文件条目 */
export interface UploadedFile {
  name: string;
  size: number;
  chunks: number;
  success: boolean;
  message: string;
  uploadedAt: number;
}

/**
 * 一个后台子任务的前端视图（Supervisor 架构，见 ADR 0019）。
 *
 * ★ 它与后端的 `TaskRecord` **不是**同一个形状
 * 两层的生命周期不同：存储层跟着落盘格式变，视图层跟着渲染需求变。
 * 合成一个定义就等于把这两件事绑死 ——
 * 后端加一个落盘字段就会污染界面，反之亦然。
 */
export interface SubtaskView {
  taskId: string;
  /** 内部专员名 —— **开发者模式才显示** */
  agent: string;
  /** ★ 人话任务名。基础界面显示的始终是它 */
  title: string;
  status: SubtaskStatus;
  dependsOn?: string[];
  /** 进度步骤，按 seq 升序（来自 REST 轮询，后端已排序） */
  progress: SubtaskProgressStep[];
  /** ★ 超了上限才带 —— 界面据此说「只显示了最近 N 步」，不能悄悄截断 */
  totalSteps?: number;
  result?: string;
  usage?: Record<string, number>;
  files?: Array<{ path: string; tool: string }>;
  error?: string;
  startedAt?: string;
  finishedAt?: string;
  /** ★ 只有中断的任务才带（「怎么续」，见 `SubtaskDTO` 的同名字段） */
  resumeMode?: 'checkpoint' | 'approval' | 'reconcile';
  resumeNote?: string;
  /** ★ 只有 `waiting_input` 才带：原地重画那张提问卡（刷新后靠它，不靠消息历史） */
  pendingQuestion?: QuestionPayload;
}

// ---------- 本轮选项的本地持久化 ----------
// 「联网」是**用户的偏好**，不是某一次对话的数据：
// 关过一次联网之后，下次打开应该还是关的。存 localStorage，与 pinStore（置顶）
// 同一思路 —— 代价说清楚：换浏览器 / 清缓存就没了。
// 模型不在这张表里：它早已是**全局设置**（后端运行时覆写），不再是本轮偏好。
const WEB_KEY = 'xjms.chat.webEnabled';
const THINKING_KEY = 'xjms.chat.thinking';

function readStoredBool(key: string, fallback: boolean): boolean {
  try {
    const raw = window.localStorage.getItem(key);
    return raw === null ? fallback : raw === '1';
  } catch {
    // 隐私模式 / 存储被禁用：退回默认值，不影响功能
    return fallback;
  }
}

function writeStored(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* 存不了就算：偏好不持久化不影响这一次使用 */
  }
}

interface AgentStore {
  // ---------- 状态 ----------
  messages: ChatMessage[];
  threadId: string;
  /**
   * 当前会话命名空间：'main'=普通对话（/chat），'assistant'=助理（/assistant）。
   *
   * ★ 两侧只共享「侧栏那份普通对话列表」，其它都各是各的
   * 助理有且只有一条固定会话（`assistant_main`）：不能新建、不分支、
   * 也永远不出现在侧栏列表里（后端按 `assistant_` 前缀过滤）。
   * 切空间切的只是「聊天区此刻显示哪根 thread」—— 消息内容都躺在
   * 服务端各自的历史里，切回来就恢复回来，谁也不覆盖谁。
   */
  scope: ThreadScope;
  isStreaming: boolean;
  /**
   * 桌面助手正在用语音占用助理会话。
   *
   * 语音事件只投影到助理消息列表；请求仍由桌面助手持有，切页不停止语音。
   * 锁住助理输入框，避免两个入口同时往同一根 thread 发消息。
   */
  voiceBusy: boolean;
  setVoiceBusy: (busy: boolean) => void;
  receivePetVoice: (update: PetVoiceUpdate) => void;
  restorePetVoice: (turn: PetVoiceTurn) => void;
  intent: string | null;
  intentConfidence: number | null;
  agentChain: string[];
  toolCalls: ToolCallInfo[];
  pendingApproval: PendingApproval | null;
  /**
   * 正等着用户回答的那张提问卡（`ask_user`）。
   *
   * ★ 与 `pendingApproval` 并列而不是合并
   * 审批是「拦不拦」、提问是「选哪个」—— 两条路在结构上就是分开的
   * （后端两套 broker、两套留痕、两种恢复载荷）。合成一个 `pending` 字段，
   * 第二个消费方（群组 / 员工私聊）出现时必然要靠猜 `kind`。
   */
  pendingQuestion: PendingQuestion | null;
  /**
   * 提交当前提问的回答。
   *
   * `skipped=true` = 什么都不答，让模型按自己的判断继续（与「只答一部分」不同：
   * 后者仍会回答其中几题）。成功后卡片就地变成只读，不等 SSE 兜底。
   */
  answerQuestion: (answers: QuestionAnswers, opts?: { skipped?: boolean }) => Promise<void>;
  /** 跳过当前提问的简写（输入框旁那颗「跳过」按钮） */
  skipQuestion: () => Promise<void>;
  uploadedFiles: UploadedFile[];
  uploading: boolean;
  error: string | null;

  // ---------- 会话历史（Phase 7）----------
  conversations: ConversationSummary[];
  conversationsTotal: number;
  /** 列表请求与聊天记录恢复分别计时，避免刷新侧栏打断聊天区。 */
  conversationsListLoading: boolean;
  conversationsLoaded: boolean;
  conversationsError: string | null;
  conversationsLoading: boolean;
  historyDir: string;
  /**
   * 打开页面时是否已经尝试过恢复上次的会话。
   *
   * 需要它是因为「新会话 → 发第一条消息 → 后端落盘」之间有一个窗口：
   * 此时列表里还没有这条会话，`bootstrap` 不该把它当成「历史已恢复」。
   */
  bootstrapped: boolean;
  /** 「已从历史恢复 N 轮上下文」这类一次性提示 */
  restoreNote: string | null;

  // ---------- 后台子任务（Supervisor 架构，见 ADR 0019）----------
  /**
   * task_id → 视图。
   *
   * ★ 为什么放 store **顶层**而不是挂在某条消息上
   *
   * 子任务的生命周期**比产生它的那条消息长得多**：
   * 用户发完消息那一轮就结束了（SSE 流已关闭），任务还在后台跑；
   * 刷新页面、切换会话、关掉浏览器再回来，它都还在。
   * 按 message 存的话，`openConversation` 清消息时会把它们一起清掉 ——
   * 于是「切个会话再回来，任务凭空消失了」。
   *
   * 形状与 `ChatMessage` 分开，是为了让「会话历史」与「后台任务」
   * 两种生命周期各自独立演进，不必互相迁就。
   */
  tasks: Record<string, SubtaskView>;
  /**
   * ★ 「查不到」与「没有」的区别。
   *
   * `'ok'` 时 `tasks` 是完整的；`'unavailable'` 时**不能**渲染成
   * 「没有后台任务」—— 那是谎报，后端返回 unavailable 恰恰是因为它**读不到**。
   * 界面上要如实说「任务状态暂不可查询」。
   */
  taskStorage: 'ok' | 'unavailable' | 'unknown';
  /** 有任务在跑（未进终态）—— 顶栏那个小转圈据此显示 */
  activeTaskCount: number;
  /** 拉一次任务列表（轮询用；也用于页面刷新后补齐） */
  refreshTasks: (opts?: { silent?: boolean }) => Promise<void>;
  /** 取消一个任务 */
  cancelTask: (taskId: string) => Promise<void>;
  /** 取消当前会话所有还在跑的后台任务（顶栏「停止」入口） */
  cancelActiveTasks: () => Promise<void>;
  /**
   * 让**中断**的任务继续。
   *
   * 返回 `needsConfirm` 表示后端要求先核对最后一步的副作用（可能已经执行过）——
   * 界面弹一次确认、用户看过之后再带 `acknowledge=true` 调一次。
   */
  resumeTask: (
    taskId: string,
    acknowledge?: boolean,
  ) => Promise<{ ok: boolean; needsConfirm?: boolean; reason?: string }>;
  /** 应用一条进度流事件（`GET /api/tasks/stream` 的实时通道） */
  applyTaskEvent: (event: TaskStreamEvent) => void;

  // ---------- 本轮选项（用户对「怎么答」的干预）----------
  /**
   * 是否允许联网搜索。会话级偏好，存本地。
   *
   * 关掉时后端**不给 `browser_agent` 绑定 `web_search` 工具**，而不是在 prompt 里
   * 劝模型别搜 —— 后者会被无视，前者想调也调不到（见后端 `runner._tools_for_agent`）。
   */
  webEnabled: boolean;
  setWebEnabled: (value: boolean) => void;
  /**
   * 深度思考开关。会话级偏好，存本地（与联网开关同一套做法）。
   *
   * 开启时后端给本轮 LLM 调用带 `enable_thinking: true`，思考内容经
   * `reasoning` 事件实时流入折叠块；代价是首字延迟和 token 消耗明显上升，
   * 所以默认**关**（简单问答不需要推理）。
   */
  thinkingEnabled: boolean;
  setThinkingEnabled: (value: boolean) => void;
  /**
   * 本轮权限档位（空串 = 跟随默认档）。
   *
   * ★ 只能往紧里收
   *
   * 后端的 `policy.effective_mode` 把会话级的值当**收紧**用：传进来的档位如果比
   * 默认档松，会被丢掉。所以对话里给不出「本轮临时完全访问」—— 也不能由
   * 对话里的一句话、或读到的某封邮件里的内容触发。
   */
  turnMode: PermissionModeValue | '';
  setTurnMode: (mode: PermissionModeValue | '') => void;
  /** 可以往紧里收的档位（含人话名字）；空数组 = 后端没给，输入框就不渲染这个选择器 */
  turnModeOptions: PermissionModeInfo[];
  /** 默认档的人话名字，选择器收起时显示当前生效档 */
  defaultModeLabel: string;
  loadPermissionMode: () => Promise<void>;
  /** 候选清单 = 「设置 → 模型」里配好那些（含当前默认） */
  availableModels: ModelOption[];
  /** 这些候选所属的供应商显示名，模型弹层里那行小字 */
  modelProvider: string;
  loadModels: () => Promise<void>;
  /** 把某个模型切成**全局默认**（对话里选模型 = 直接改配置，新对话与后续都走它） */
  switchModel: (model: ModelOption) => Promise<void>;

  /**
   * 服务信息（版本 + **当前生效的回答模型**），侧栏与「关于」页共用。
   *
   * ★ 为什么放进 store 而不是各自 `fetchServiceInfo()`
   *
   * 它读的是**运行时的**默认模型 —— 设置里换一次模型，两个地方都得跟着变。
   * 各组件自己持有一份的话，常驻的侧栏会一直显示旧名字（它只在挂载时取一次），
   * 用户会以为「换了没生效」。放进 store 之后，换模型的调用方发一次
   * `loadServiceInfo(true)`，所有露出模型名的地方一起更新。
   */
  serviceInfo: ServiceInfo | null;
  /** `loading` = 还没问；`down` = 问了但后端没起来（关于页要如实显示） */
  serviceInfoStatus: 'loading' | 'ok' | 'down';
  /** `force=true` 时先丢掉模块级缓存再拉（换过模型之后必须这样） */
  loadServiceInfo: (force?: boolean) => Promise<void>;

  // ---------- 动作 ----------
  /** 发送消息。`attachments` 是已通过 `/files/attach` 上传好的附件元数据 */
  send: (text: string, attachments?: AttachmentMeta[]) => Promise<void>;
  approve: (scope?: 'once' | 'same_operation') => Promise<void>;
  reject: () => Promise<void>;
  abort: () => void;
  newSession: () => void;
  /**
   * 切换聊天区所在的命名空间（/chat ↔ /assistant）。
   * 侧栏那份普通对话列表**不受影响**；助理侧永远是那条固定会话。
   */
  setScope: (scope: ThreadScope) => Promise<void>;
  upload: (file: File) => Promise<FileUploadResponse>;
  removeUploadedFile: (name: string) => void;
  clearError: () => void;

  // 消息操作
  regenerate: (message: ChatMessage) => void;
  editMessage: (messageId: string, newText: string) => void;
  /** 删除某条消息所在的**一轮**（一问一答，后端存储的最小单元） */
  deleteTurn: (message: ChatMessage) => Promise<void>;
  /** 从某条消息所在的一轮分支出新会话（原会话不动） */
  forkFromMessage: (message: ChatMessage) => Promise<void>;

  // 会话历史
  bootstrap: () => Promise<void>;
  refreshConversations: (opts?: { silent?: boolean }) => Promise<void>;
  openConversation: (threadId: string, opts?: { tolerateMissing?: boolean; force?: boolean }) => Promise<void>;
  /**
   * 打开一根 thread，并把它所属的页面带上前台（侧栏列表与命令面板共用这一个入口）。
   *
   * ★ 为什么跳转要写在 store 里而不是每个入口各写一遍
   * 「哪根 thread 属于哪个页面」和「界面现在该显示谁」必须一次决定。
   * 两处各写各的话，漏一处就是红线那个样子：顶栏写着助理，内容却是普通对话。
   */
  openThread: (threadId: string) => Promise<void>;
  removeConversation: (threadId: string) => Promise<void>;
  /** 批量删除（侧栏多选）。后端没有批量接口，逐个删；返回结果给调用方提示。 */
  removeConversations: (threadIds: string[]) => Promise<{ removed: number; failed: number }>;
  clearAllConversations: () => Promise<void>;
  renameConversation: (threadId: string, title: string) => Promise<void>;
  dismissRestoreNote: () => void;
}

let abortController: AbortController | null = null;

/**
 * 主动问候的模块级节流。
 *
 * 切页、React 严格模式的双次挂载、刷新都会重新触发 setScope —— 每次都打一发
 * `/assistant/checkin` 只会让同一句问候被反复生成（服务端冷却在 5 分钟后才兜底）。
 * 正在请求时直接跳过，刚刚问过的一分钟内也不再问。
 */
let checkinInFlight = false;
let checkinAskedAt = 0;
const CHECKIN_LOCAL_COOLDOWN_MS = 60_000;

function uid(): string {
  return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** 不可变地更新指定消息 */
function patchMessage(
  messages: ChatMessage[],
  id: string,
  patch: (msg: ChatMessage) => ChatMessage,
): ChatMessage[] {
  return messages.map((m) => (m.id === id ? patch(m) : m));
}

/** 按 question_id 改一张已挂在该消息上的提问卡（没有就原样返回） */
function patchAskedQuestion(
  messages: ChatMessage[],
  questionId: string,
  patch: (asked: AskedQuestion) => AskedQuestion,
): ChatMessage[] {
  return messages.map((m) => {
    const list = m.askedQuestions;
    if (!list?.some((q) => q.questionId === questionId)) return m;
    return {
      ...m,
      askedQuestions: list.map((q) => (q.questionId === questionId ? patch(q) : q)),
    };
  });
}

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

type StoreSet = (partial: Partial<AgentStore> | ((state: AgentStore) => Partial<AgentStore>)) => void;

/**
 * 把一个 SSE 事件应用到指定的 assistant 消息上。
 *
 * 实时流与会话历史恢复**共用**这一个函数 —— 见文件头的说明。
 */
function applyAgentEvent(
  event: AgentEvent,
  assistantId: string,
  set: StoreSet,
  /** 历史重放：挂着的提问一律按「已失效」重建（见 `askedFromPayload`） */
  replay = false,
): void {
  switch (event.type) {
    case 'token':
      set((s) => ({
        messages: patchMessage(s.messages, assistantId, (m) => {
          if (event.message_index === undefined) return { ...m, content: m.content + event.content };
          const replyMessages = [...(m.replyMessages ?? [])];
          replyMessages[event.message_index] = (replyMessages[event.message_index] ?? '') + event.content;
          return { ...m, replyMessages, content: replyMessages.filter(Boolean).join('\n\n') };
        }),
      }));
      break;

    case 'assistant_messages':
      set((s) => ({
        messages: patchMessage(s.messages, assistantId, (m) => ({
          ...m, replyMessages: [...event.content], content: event.content.filter(Boolean).join('\n\n'),
        })),
      }));
      break;

    case 'reasoning':
      // 深度思考的实时增量：进折叠块（ChatMessage 渲染 m.reasoning）。
      // 与 token 分开走 —— 思考不该混进回答正文。
      set((s) => ({
        messages: patchMessage(s.messages, assistantId, (m) => ({
          ...m,
          reasoning: (m.reasoning ?? '') + event.content,
        })),
      }));
      break;

    case 'sources':
      set((s) => ({
        messages: patchMessage(s.messages, assistantId, (m) => ({
          ...m,
          sources: event.content as SourceDoc[],
        })),
      }));
      break;

    case 'intent':
      set((s) => ({
        intent: event.content,
        intentConfidence: event.confidence,
        messages: patchMessage(s.messages, assistantId, (m) => ({
          ...m,
          intent: event.content,
          intentConfidence: event.confidence,
        })),
      }));
      break;

    case 'agent_switch': {
      const agent = event.content;
      set((s) => {
        // 相邻去重，避免工具循环中重复追加同一 Agent
        const chain = s.agentChain[s.agentChain.length - 1] === agent
          ? s.agentChain
          : [...s.agentChain, agent];
        return {
          agentChain: chain,
          messages: patchMessage(s.messages, assistantId, (m) => ({ ...m, agentChain: chain })),
        };
      });
      break;
    }

    case 'tool_call': {
      const info: ToolCallInfo = {
        callId: event.call_id,
        toolName: event.tool_name,
        toolLabel: event.tool_label,
        args: event.args ?? {},
        status: 'pending',
        dangerLevel: (event.danger_level ?? 'safe') as DangerLevel,
      };
      set((s) => {
        // 同 call_id 去重（防御性：事件重发时不产生重复卡片）
        if (s.toolCalls.some((t) => t.callId === info.callId)) return s;
        const toolCalls = [...s.toolCalls, info];
        return {
          toolCalls,
          messages: patchMessage(s.messages, assistantId, (m) => ({ ...m, toolCalls })),
        };
      });
      break;
    }

    case 'interrupt': {
      const approval: PendingApproval = {
        requestId: event.request_id,
        grantAllowed: event.grant_allowed,
        grantMaxUses: event.grant_max_uses,
        grantTtlSeconds: event.grant_ttl_seconds,
        expiresAt: event.expires_at,
        callId: event.call_id,
        toolName: event.tool_name,
        toolLabel: event.tool_label,
        args: event.args ?? {},
        reason: event.reason,
        // ★ 这三项必须一路带回提交处（护栏 6）：它们是后端唯一的定位依据
        runId: event.run_id ?? '',
        employeeId: event.employee_id ?? '',
        requestor: event.requestor ?? '',
      };
      set((s) => {
        const toolCalls = s.toolCalls.map((t) =>
          t.callId === event.call_id
            ? { ...t, status: 'awaiting_approval' as const, reason: event.reason }
            : t,
        );
        return {
          pendingApproval: approval,
          toolCalls,
          messages: patchMessage(s.messages, assistantId, (m) => ({ ...m, toolCalls })),
        };
      });
      break;
    }

    // ---------- 模型主动提问（`ask_user`）----------
    // 与 `interrupt` 是并列的两条路：这里画内联卡片，不打开审批弹窗。
    case 'question': {
      const asked = askedFromPayload(event, replay ? 'expired' : 'pending');
      set((s) => {
        const existing = s.messages.find((m) => m.id === assistantId)?.askedQuestions ?? [];
        // 同一 question_id 只挂一次（实时流与重放都会经过这里）
        if (existing.some((q) => q.questionId === asked.questionId)) return s;
        return {
          messages: patchMessage(s.messages, assistantId, (m) => ({
            ...m,
            askedQuestions: [...(m.askedQuestions ?? []), asked],
          })),
          // 重放时绝不点亮「等你回答」—— 那张卡已经不可能再答了
          ...(replay ? {} : { pendingQuestion: asked }),
        };
      });
      break;
    }

    case 'question_resolved':
      set((s) => ({
        messages: patchAskedQuestion(s.messages, event.question_id, (asked) => ({
          ...asked,
          status: event.status,
          answers: event.answers ?? {},
          ...(event.reason ? { reason: event.reason } : {}),
        })),
        pendingQuestion:
          s.pendingQuestion?.questionId === event.question_id ? null : s.pendingQuestion,
      }));
      break;

    case 'tool_result':
      set((s) => {
        const toolCalls = s.toolCalls.map((t) =>
          t.callId === event.call_id
            ? {
                ...t,
                status: (event.success ? 'success' : ['awaiting_approval', 'rejected'].includes(t.status) ? 'rejected' : 'failed') as ToolCallInfo['status'],
                result: event.result,
                durationMs: event.duration_ms,
              }
            : t,
        );
        return {
          toolCalls,
          messages: patchMessage(s.messages, assistantId, (m) => ({ ...m, toolCalls })),
        };
      });
      break;

    case 'error':
      set((s) => ({
        error: event.content,
        messages: patchMessage(s.messages, assistantId, (m) => ({ ...m, error: event.content })),
      }));
      break;

    case 'memory':
      // 记忆回执：只在真的写入了才推。挂到本条消息上而不是弹全局 toast，
      // 是因为「记住了什么」必须和「你刚说的那句话」放在一起才读得懂。
      if (event.action === 'saved' && event.content) {
        set((s) => ({
          messages: patchMessage(s.messages, assistantId, (m) => ({
            ...m,
            memoryNotes: [...(m.memoryNotes ?? []), event.content],
          })),
        }));
      }
      break;

    case 'approval_auto': {
      // ★ 预授权自动放行：危险操作**没经过用户点击**就执行了。
      //
      // 这里刻意**不**碰 pendingApproval —— 自动放行意味着没有东西在等，
      // 开了弹窗用户点了也没用（后端根本没有等待中的请求）。
      // 但必须留痕给用户看见：静默执行危险操作是不可接受的。
      // 所以挂到消息上做一条非阻塞提示，而不是弹窗。
      const note: AutoApprovalNote = {
        callId: event.call_id,
        toolName: event.tool_name,
        toolLabel: event.tool_label,
        grantId: event.grant_id,
        remaining: event.remaining,
        reason: event.reason,
        content: event.content,
      };
      set((s) => {
        // 同 call_id 去重（事件重发时不产生重复行）
        const existing = s.messages.find((m) => m.id === assistantId)?.autoApprovals ?? [];
        if (existing.some((n) => n.callId === note.callId)) return s;
        const toolCalls = s.toolCalls.map((t) =>
          t.callId === event.call_id
            ? { ...t, status: 'pending' as const, reason: event.reason }
            : t,
        );
        return {
          toolCalls,
          messages: patchMessage(s.messages, assistantId, (m) => ({
            ...m,
            autoApprovals: [...(m.autoApprovals ?? []), note],
            toolCalls,
          })),
        };
      });
      break;
    }

    case 'hook_notice': {
      // 钩子要给用户看的一句话（Phase 5）。
      //
      // ★ 它**不碰** pendingApproval，也不改工具状态
      // 钩子的拦截走的是判定层（`interrupt` 或被拒绝的 `tool_result`），
      // 到这里的事件一律只是"说一句"。渲染成消息尾部一行非阻塞提示。
      if (!event.content) break;
      const note: HookNoticeNote = {
        content: event.content,
        event: event.event,
        toolName: event.tool_name,
        toolLabel: event.tool_label,
      };
      set((s) => {
        const existing = s.messages.find((m) => m.id === assistantId)?.hookNotices ?? [];
        // 同一事件同一句话只留一行（重放与实时流都会经过这里，去重必须做在写入侧）
        if (existing.some((n) => n.event === note.event && n.content === note.content)) return s;
        return {
          messages: patchMessage(s.messages, assistantId, (m) => ({
            ...m,
            hookNotices: [...(m.hookNotices ?? []), note],
          })),
        };
      });
      break;
    }

    case 'done':
      set((s) => ({
        messages: patchMessage(s.messages, assistantId, (m) => ({
          ...m,
          streaming: false,
          // 本轮产出（由后端从事件流派生，见 DoneEvent.files）。
          // 空数组也要写：`undefined` 表示「这不是本轮的信息」，
          // 而 `[]` 表示「本轮确实没产出文件」—— 两者渲染结果一样，
          // 但语义不同，重放时不该让上一轮的值残留。
          files: event.files ?? [],
          // 真实用量（后端从聚合后的 AIMessage 上取的，不是按文本估算）。
          // 拿不到就不带 —— 界面据有没有它决定那行小字要不要出现。
          ...(event.usage ? { usage: event.usage } : {}),
          // ---------- 推理正文 ----------
          // 网关会把模型的思考混进正文一起流出来（末尾带 `</think>`），
          // 所以后端在收尾时切开：`reasoning` 给折叠块，`answer` 是干净回答。
          // ★ 这里**不做任何切分判断** —— 「从哪个字符起才算回答」由后端决定
          //（`app/agents/think.py`），前端只负责把已经流过的那一段挪个位置。
          // 没有 reasoning 的正常回答走不到这里，正文一个字都不动。
          ...(event.reasoning
            ? { reasoning: event.reasoning, content: event.answer ?? m.content }
            : {}),
        })),
      }));
      break;

    // 重放：完成汇报的落盘标记（轮次 events 的第一条）。
    // 实时路径不产生这条事件 —— 广播直接带 task_count；
    // 落盘版只在重载历史时被 `openConversation` 读回来。
    case 'task_report':
      set((s) => ({
        messages: patchMessage(s.messages, assistantId, (m) => ({
          ...m,
          taskCount: event.count,
          taskStatus: event.status,
        })),
      }));
      break;

    // ---------- 审批决策回执（不变量 39：它不是 interrupt 的标志位）----------
    // ★ 原本这里是一个空分支（`case 'state_update': default: break`），
    // 而后端发了 6 处 —— 文档声称它驱动「前端状态条」，实际从未被消费。
    // 现在只处理**审批**这一种（`types/index.ts` 已把 `state_key` 收窄成
    // `'approval'`，将来有人再发个装饰性的 state_update 会是**编译错误**）。
    //
    // 它做的唯一一件事：把对应工具卡片从「等待审批」改成最终状态。
    // 不弹窗、不显示文字 —— 界面上的审批结果由 `tool_result` 呈现，
    // 这条只是让卡片别一直转圈。
    case 'state_update': {
      if (event.state_key === 'approval') {
        set((s) => {
          const toolCalls = approvalToolStates(s.toolCalls, Boolean(event.value));
          const decided = s.toolCalls.filter(tool => tool.status === 'awaiting_approval').map(tool => tool.callId);
          return { toolCalls, pendingApproval: s.pendingApproval && decided.includes(s.pendingApproval.callId) ? null : s.pendingApproval,
            messages: patchMessage(s.messages, assistantId, message => ({ ...message, toolCalls })) };
        });
      }
      break;
    }

    // ---------- 后台子任务（Supervisor 架构，见 ADR 0019）----------
    // 主对话流只会推 `subtask`（派发瞬间）；进度流的其余事件走
    // `applyTaskEvent` → 同一个 `reduceTaskEvent`（折叠只有一处）。
    case 'subtask':
      reduceTaskEvent(event, set);
      break;

    default:
      break;
  }
}

/**
 * 折叠一条**任务事件**成 store 状态（进度流与主对话流共用）。
 *
 * ★ 为什么单独一个 reducer 而不是塞进 `applyAgentEvent`
 * `applyAgentEvent` 是「某条**消息**的事件」的 reducer（它要 assistantId
 * 去 patch 消息）。而任务进度**不属于任何一条消息** —— 子任务活得比产生它
 * 的那一轮更久。硬塞进去会让它带一个用不上的 assistantId，职责也糊。
 *
 * ★ 两条流会推**同一条 `subtask`**（主对话流 + 进度流），所以这里的每个
 * 分支都必须**幂等** —— 否则 `activeTaskCount` 会被加两次。
 */
function reduceTaskEvent(event: TaskStreamEvent, set: StoreSet): void {
  switch (event.type) {
    // ---------- 快照：整份替换（真相源 = 任务表）----------
    // 断线重连先收这条 —— 所以前端不依赖「事件一条都不丢」。
    case 'snapshot': {
      const next: Record<string, SubtaskView> = {};
      let active = 0;
      for (const dto of event.tasks) {
        const view = toSubtaskView(dto);
        next[view.taskId] = view;
        if (isSubtaskActive(view.status)) active += 1;
      }
      set({ tasks: next, taskStorage: 'ok', activeTaskCount: active });
      break;
    }

    // ---------- 派发 ----------
    case 'subtask': {
      set((s) => {
        const known = Boolean(s.tasks[event.task_id]);
        return {
          tasks: {
            ...s.tasks,
            [event.task_id]: {
              taskId: event.task_id,
              agent: event.agent,
              title: event.title,
              status: known ? s.tasks[event.task_id].status : 'pending' as const,
              dependsOn: event.depends_on ?? s.tasks[event.task_id]?.dependsOn,
              // 已存在就保留进度（重复的 subtask 事件不该清空已有进度）
              progress: known ? s.tasks[event.task_id].progress : [],
            },
          },
          // ★ 幂等：两条流都推它，已存在时不再 +1
          activeTaskCount: known ? s.activeTaskCount : s.activeTaskCount + 1,
        };
      });
      break;
    }

    // ---------- 推进一步 ----------
    case 'subtask_progress': {
      set((s) => {
        const cur = s.tasks[event.task_id];
        if (!cur) return {};
        // ★ 按 seq 去重 —— 两条流 + 重连都可能重复推同一个 seq，
        // 不去重的话进度会在界面上重复出现（照抄 tool_call 按 call_id 去重）。
        if (cur.progress.some((p) => p.seq === event.seq)) return {};
        return {
          tasks: {
            ...s.tasks,
            [event.task_id]: {
              ...cur,
              // 插到正确位置而不是 append —— 事件到达顺序 ≠ 发生顺序
              progress: [...cur.progress, event].sort((a, b) => a.seq - b.seq),
            },
          },
        };
      });
      break;
    }

    // ---------- 终态 ----------
    case 'subtask_done': {
      set((s) => {
        const cur = s.tasks[event.task_id];
        if (!cur) return {};
        // ★ 已是终态就不覆盖（先到的赢）—— 乱序到达时不能让状态回退，
        // 那会让一个已完成的任务又转起来，用户以为它还在跑。
        if (isSubtaskTerminal(cur.status)) return {};
        return {
          tasks: {
            ...s.tasks,
            [event.task_id]: {
              ...cur,
              status: event.status,
              ...(event.title ? { title: event.title } : {}),
              ...(event.result ? { result: event.result } : {}),
              ...(event.usage ? { usage: event.usage } : {}),
              ...(event.files ? { files: event.files } : {}),
              ...(event.error ? { error: event.error } : {}),
              // 终态不可能还在等人回答 —— 留着那张卡会让面板显示一个点不动的输入区
              pendingQuestion: undefined,
              finishedAt: new Date().toISOString(),
            },
          },
          activeTaskCount: Math.max(0, s.activeTaskCount - 1),
        };
      });
      break;
    }

    // ---------- 助理发起的消息进主对话（失败通知 / 完成汇报） ----------
    // 后端先把它落成一轮（initiated_by=assistant）再广播 —— 与「主动问候」
    // 同一条契约。重载历史会整份替换消息列表，所以这里只需要防
    // 同一条事件被应用两次（按 turn_id 幂等）。

    // 流式增量：总 Agent 的完成汇报正在生成。
    // 第一次收到时创建一条「打字中」的消息，之后逐段追加 —— 与普通回答
    // 同一个渲染效果；终版 `assistant_turn` 到达时收尾。
    case 'assistant_turn_cancelled': {
      set(s => ({ messages: s.messages.filter(m => m.id !== `task-notice-${event.turn_id}` || !m.streaming) }));
      break;
    }
    case 'assistant_turn_token': {
      set((s) => {
        const id = `task-notice-${event.turn_id}`;
        if (s.messages.some((m) => m.id === id)) {
          if (!s.messages.find(m => m.id === id)?.streaming) return {};
          return {
            messages: patchMessage(s.messages, id, (m) => ({
              ...m,
              content: m.content + event.content,
            })),
          };
        }
        let max = -1;
        for (const m of s.messages) {
          if (typeof m.turnIndex === 'number' && m.turnIndex > max) max = m.turnIndex;
        }
        return {
          messages: [
            ...s.messages,
            {
              id,
              role: 'assistant' as const,
              content: event.content,
              sources: [],
              agentChain: [],
              toolCalls: [],
              streaming: true,
              initiatedBy: 'assistant' as const,
              turnIndex: max + 1,
              ...(typeof event.task_count === 'number' ? { taskCount: event.task_count } : {}),
              taskStatus: event.task_status,
            },
          ],
        };
      });
      break;
    }

    // 终版：把流式中的那条收尾成完整正文；没收到过增量时，它就是整条消息。
    case 'assistant_turn': {
      set((s) => {
        const id = `task-notice-${event.turn_id}`;
        if (s.messages.some((m) => m.id === id)) {
          return {
            messages: patchMessage(s.messages, id, (m) => ({
              ...m,
              content: event.text,
              streaming: false,
              ...(typeof event.task_count === 'number' ? { taskCount: event.task_count } : {}),
              taskStatus: event.task_status,
              ...(Array.isArray(event.files) ? { files: event.files } : {}),
            })),
          };
        }
        let max = -1;
        for (const m of s.messages) {
          if (typeof m.turnIndex === 'number' && m.turnIndex > max) max = m.turnIndex;
        }
        return {
          messages: [
            ...s.messages,
            {
              id,
              role: 'assistant' as const,
              content: event.text,
              sources: [],
              agentChain: [],
              toolCalls: [],
              streaming: false,
              initiatedBy: 'assistant' as const,
              turnIndex: max + 1,
              ...(typeof event.task_count === 'number' ? { taskCount: event.task_count } : {}),
              taskStatus: event.task_status,
              ...(Array.isArray(event.files) ? { files: event.files } : {}),
            },
          ],
        };
      });
      break;
    }
  }
}

/** 终态任务：不会再变化（「进行中」只看这个分类，与后端同一套口径） */
function isSubtaskTerminal(status: SubtaskStatus): boolean {
  return (
    status === 'succeeded' ||
    status === 'failed' ||
    status === 'cancelled' ||
    status === 'orphaned'
  );
}

/**
 * 活跃任务：会自己往前走的那些（与后端 `ACTIVE_STATUSES` 同一套口径）。
 *
 * ★ 与 `!isSubtaskTerminal` 的区别就是 `interrupted`
 * 中断的任务在等人，不是系统在忙 —— 算进「进行中」会让顶栏一直转圈、
 * 轮询永不停止（每 2s 拉一次，直到用户点「继续」或「放弃」）。
 */
function isSubtaskActive(status: SubtaskStatus): boolean {
  return (
    status === 'pending' ||
    status === 'running' ||
    status === 'waiting_approval' ||
    status === 'waiting_input'
  );
}

/** 后端 DTO → 前端视图（REST 轮询那条通道用） */
function toSubtaskView(dto: SubtaskDTO): SubtaskView {
  return {
    taskId: dto.task_id,
    agent: dto.agent,
    title: dto.title,
    status: dto.status,
    dependsOn: dto.depends_on,
    progress: dto.progress ?? [],
    ...(dto.total_steps !== undefined ? { totalSteps: dto.total_steps } : {}),
    ...(dto.result ? { result: dto.result } : {}),
    ...(dto.usage ? { usage: dto.usage } : {}),
    ...(dto.files ? { files: dto.files } : {}),
    ...(dto.error ? { error: dto.error } : {}),
    ...(dto.started_at ? { startedAt: dto.started_at } : {}),
    ...(dto.finished_at ? { finishedAt: dto.finished_at } : {}),
    ...(dto.resume_mode ? { resumeMode: dto.resume_mode } : {}),
    ...(dto.resume_note ? { resumeNote: dto.resume_note } : {}),
    ...(dto.pending_question ? { pendingQuestion: dto.pending_question } : {}),
  };
}

export const useAgentStore = create<AgentStore>((set, get) => {
  let conversationListRequest: Promise<void> | null = null;
  let bootstrapRequest: Promise<void> | null = null;
  let voiceTurn: PetVoiceTurn | null = null;
  let voiceAssistantId: string | null = null;
  let voiceUserId: string | null = null;
  let historyRequest = 0;

  const applyVoiceEvent = (event: AgentEvent, assistantId: string) => {
    applyAgentEvent(event, assistantId, set);
    if (event.type === 'done') {
      set((s) => ({ messages: patchMessage(s.messages, assistantId, (m) => ({
        ...m, streaming: false,
        ...(event.answer === undefined ? {} : { content: event.answer }),
        ...(event.files ? { files: event.files } : {}),
        ...(event.usage ? { usage: event.usage } : {}),
        ...(event.reasoning ? { reasoning: event.reasoning } : {}),
      })) }));
    }
  };

  const showVoiceTurn = () => {
    const state = get();
    if (!voiceTurn || voiceTurn.complete || state.scope !== 'assistant' || state.isStreaming || state.conversationsLoading) return;
    if (voiceAssistantId && state.messages.some((m) => m.id === voiceAssistantId)) return;
    voiceAssistantId = beginTurn(voiceTurn.message, false);
    voiceUserId = get().messages[get().messages.length - 2]?.id ?? null;
    const id = voiceAssistantId;
    set((s) => ({ messages: patchMessage(s.messages, id, (m) => ({ ...m, streaming: true })) }));
    for (const event of voiceTurn.events) applyVoiceEvent(event, id);
  };
  /**
   * 下一条消息所属的轮次下标。
   *
   * ★ 用它而不是 `Math.floor(index / 2)` 算轮次
   *
   * 助理会主动开口（`initiated_by=assistant`），那种轮次只有一条消息。
   * 再按「每轮固定两条」去数，删一轮就会删错行 —— 消息自己带着轮次下标，
   * 配对关系就不再依赖列表里消息的条数。
   */
  const nextTurnIndex = (messages: ChatMessage[]): number => {
    let max = -1;
    for (const m of messages) {
      if (typeof m.turnIndex === 'number' && m.turnIndex > max) max = m.turnIndex;
    }
    return max + 1;
  };

  /**
   * 开一轮：追加「用户消息 + 占位助手消息」并把本轮累积器清零。
   *
   * 实时发送与历史重放都从这里开始 —— 两者开头做的事必须完全一致，
   * 否则恢复出来的界面会在「哪条消息带工具卡片」这类细节上和当时不一样。
   */
  const beginTurn = (
    userContent: string,
    live: boolean,
    attachments?: AttachmentMeta[],
    turnIndex?: number,
  ): string => {
    const assistantId = uid();
    set((s) => {
      const index = turnIndex ?? nextTurnIndex(s.messages);
      return {
        messages: [
          ...s.messages,
          { id: uid(), role: 'user', content: userContent, attachments, turnIndex: index },
          {
            id: assistantId,
            role: 'assistant',
            content: '',
            sources: [],
            agentChain: [],
            toolCalls: [],
            streaming: live,
            turnIndex: index,
          },
        ],
        isStreaming: live,
        intent: null,
        intentConfidence: null,
        agentChain: [],
        toolCalls: [],
        pendingApproval: null,
        pendingQuestion: null,
        error: null,
      };
    });
    return assistantId;
  };

  /**
   * 开一轮「她先开口」：只有一条助手消息，没有对应的用户消息。
   *
   * 主动问候在服务端也是一轮（`initiated_by=assistant`），所以这里补上的
   * 消息必须带同一个 `initiatedBy` —— 删除、重放、重新生成都靠它区分
   * 「这一轮有没有用户提问」。
   */
  const beginAssistantTurn = (text: string, turnIndex?: number, turnId?: string): string => {
    const assistantId = turnId ? `task-notice-${turnId}` : uid();
    set((s) => ({
      messages: [
        ...s.messages,
        {
          id: assistantId,
          role: 'assistant',
          content: text,
          sources: [],
          agentChain: [],
          toolCalls: [],
          streaming: false,
          initiatedBy: 'assistant',
          turnIndex: turnIndex ?? nextTurnIndex(s.messages),
        },
      ],
    }));
    return assistantId;
  };

  /**
   * 进助理页时问一次「要不要她先开口」。
   *
   * 决定与生成都在服务端（人设、时间、关系记忆都在那边），这里只负责
   * 把结果补进聊天区。拿不到、不必开口、期间用户已经自己说话了 ——
   * 都安静停着：主动问候是可选项，失败不该在欢迎态上弹一条红条。
   */
  const runAssistantCheckin = async (): Promise<void> => {
    if (get().voiceBusy || checkinInFlight || Date.now() - checkinAskedAt < CHECKIN_LOCAL_COOLDOWN_MS) return;
    const threadId = get().threadId;
    const before = get().messages.length;
    checkinInFlight = true;
    checkinAskedAt = Date.now();
    try {
      const resp = await checkinAssistant();
      if (!resp.ok || !resp.text) return;
      if (get().scope !== 'assistant' || get().threadId !== threadId) return;
      if (get().voiceBusy || get().isStreaming || get().messages.length !== before) return;
      // 服务端已经把它落成一轮了，这里补同一条消息（不重新拉详情，那会闪一屏）
      beginAssistantTurn(resp.text);
    } catch {
      /* 主动问候失败不打扰用户 */
    } finally {
      checkinInFlight = false;
    }
  };

  const endTurn = (assistantId: string): void => {
    set((s) => ({
      isStreaming: false,
      messages: patchMessage(s.messages, assistantId, (m) => ({ ...m, streaming: false })),
    }));
  };

  return {
    messages: [],
    threadId: createThreadId('main'),
    scope: 'main',
    isStreaming: false,
    voiceBusy: false,
    intent: null,
    intentConfidence: null,
    agentChain: [],
    toolCalls: [],
    pendingApproval: null,
    pendingQuestion: null,
    uploadedFiles: [],
    uploading: false,
    error: null,

    // 本轮选项：联网默认开（「能自动的不要问」），深度思考默认关
    // （首字延迟与 token 代价大，复杂问题再手动开）
    webEnabled: readStoredBool(WEB_KEY, true),
    thinkingEnabled: readStoredBool(THINKING_KEY, false),
    // 本轮权限档位**不存本地**：见接口注释，它是一次「这一轮收紧」的决定，
    // 存下来会变成跨天的隐形状态，用户只会觉得「她今天什么都不肯干」
    turnMode: '',
    turnModeOptions: [],
    defaultModeLabel: '',
    availableModels: [],
    modelProvider: '',

    serviceInfo: null,
    serviceInfoStatus: 'loading',

    conversations: [],
    conversationsTotal: 0,
    conversationsListLoading: false,
    conversationsLoaded: false,
    conversationsError: null,
    conversationsLoading: false,
    historyDir: '',
    bootstrapped: false,
    restoreNote: null,

    // ---------- 后台子任务（Supervisor 架构）----------
    tasks: {},
    // 'unknown' = 还没拉过。界面**不据此显示任何东西** ——
    // 「还没查」与「查了没有」是两件事，混起来会让面板在首次轮询前
    // 闪一下「没有后台任务」。
    taskStorage: 'unknown',
    activeTaskCount: 0,

    /**
     * 拉一次后台任务列表（进度的**唯一**通道）。
     *
     * ★ 子任务的进度与完成只走这里（SubtaskPanel 里的 2s 轮询）；
     * SSE 只在派发瞬间推 `subtask` 事件。所以这里整份替换 `tasks`，
     * 不存在「与 SSE 抢 seq」的问题（见 types/index.ts 的注释）。
     */
    refreshTasks: async (opts) => {
      const threadId = get().threadId;
      if (!threadId) return;
      try {
        const resp = await fetchTasks(threadId);
        if (get().threadId !== threadId) return; // 期间切了会话，丢弃
        if (resp.storage === 'unavailable') {
          // ★ 如实标记「查不到」，不渲染成「没有任务」——
          // 空列表会被当成「没有任务」，而真相是「读不到」。
          set({ taskStorage: 'unavailable' });
          return;
        }
        const next: Record<string, SubtaskView> = {};
        let active = 0;
        for (const dto of resp.tasks) {
          const view = toSubtaskView(dto);
          next[view.taskId] = view;
          if (isSubtaskActive(view.status)) active += 1;
        }
        // ★ 整体替换而不是合并
        // 合并会让「后端已经删掉的任务」永远留在界面上 ——
        // 用户在设置里关掉任务功能后，界面上还有一批僵尸任务。
        set({ tasks: next, taskStorage: 'ok', activeTaskCount: active });
      } catch (e) {
        if (!opts?.silent) {
          set({ taskStorage: 'unavailable' });
        }
      }
    },

    cancelTask: async (taskId: string) => {
      try {
        const resp = await cancelTaskApi(taskId);
        if (!resp.ok) {
          // 任务已经结束了 —— 不是错误，只是没得取消
          await get().refreshTasks({ silent: true });
          return;
        }
        set((s) => ({
          tasks: {
            ...s.tasks,
            [taskId]: { ...s.tasks[taskId], status: 'cancelled' as const },
          },
          activeTaskCount: Math.max(0, s.activeTaskCount - 1),
        }));
      } catch {
        // 取消失败就刷新一次，让界面显示后端的真实状态
        await get().refreshTasks({ silent: true });
      }
    },

    cancelActiveTasks: async () => {
      const active = Object.values(get().tasks).filter((t) => isSubtaskActive(t.status));
      if (active.length === 0) return;
      // 逐个走 cancelTask：落盘与失败兜底（刷新真实状态）都在那一个入口里
      await Promise.all(active.map((t) => get().cancelTask(t.taskId)));
    },

    resumeTask: async (taskId, acknowledge = false) => {
      try {
        const resp = await resumeTaskApi(taskId, acknowledge);
        if (!resp.ok) {
          // 状态已经变了（别处续过 / 已放弃）—— 以后端为准，不是本地兜底
          await get().refreshTasks({ silent: true });
          return {
            ok: false,
            needsConfirm: Boolean(resp.needs_confirm),
            reason: resp.reason,
          };
        }
        set((s) => {
          const cur = s.tasks[taskId];
          if (!cur) return {};
          // ★ 乐观翻成进行中：顶栏转圈与 2s 轮询都靠 `activeTaskCount` 启动，
          // 不翻的话界面会停在「等你继续」，而任务其实已经在跑了。
          const next: SubtaskView = { ...cur, status: 'running' };
          delete next.resumeMode;
          delete next.resumeNote;
          return {
            tasks: { ...s.tasks, [taskId]: next },
            activeTaskCount: s.activeTaskCount + 1,
          };
        });
        void get().refreshTasks({ silent: true });
        return { ok: true };
      } catch {
        await get().refreshTasks({ silent: true });
        return { ok: false, reason: '继续的请求没有送达，请稍后再试' };
      }
    },

    // 进度流事件入口（`GET /api/tasks/stream`）。折叠逻辑与主对话流共用
    // `reduceTaskEvent` —— 两条流推同一份事实，判断只有一处。
    applyTaskEvent: (event) => {
      reduceTaskEvent(event, set);
    },

    // ================================================================
    // 本轮选项（联网 / 深度思考 / 权限档位 / 模型）
    // ================================================================
    setWebEnabled: (value: boolean) => {
      writeStored(WEB_KEY, value ? '1' : '0');
      set({ webEnabled: value });
    },

    setThinkingEnabled: (value: boolean) => {
      writeStored(THINKING_KEY, value ? '1' : '0');
      set({ thinkingEnabled: value });
    },

    setTurnMode: (mode) => set({ turnMode: mode }),

    setVoiceBusy: (busy) => set({ voiceBusy: busy }),

    restorePetVoice: (turn) => {
      if (voiceTurn && turn.sequence <= voiceTurn.sequence) return;
      if (!turn.complete && voiceAssistantId && get().scope === 'assistant') {
        set((s) => ({ messages: s.messages.filter((m) => m.id !== voiceAssistantId && m.id !== voiceUserId) }));
      }
      voiceTurn = turn;
      voiceAssistantId = null;
      showVoiceTurn();
      if (turn.complete && get().scope === 'assistant' && !get().isStreaming) {
        void get().openConversation(get().threadId, { force: true, tolerateMissing: true });
      }
    },

    receivePetVoice: (update) => {
      const sequence = update.sequence ?? 0;
      if (voiceTurn && sequence <= voiceTurn.sequence) return;
      if (update.type === 'start') {
        voiceTurn = { id: update.id, message: update.message, events: [], complete: false, sequence };
        voiceAssistantId = null;
        showVoiceTurn();
        return;
      }
      if (!voiceTurn || update.id !== voiceTurn.id || voiceTurn.complete) return;
      voiceTurn.sequence = sequence;
      if (update.type === 'event') {
        const event = update.event;
        if (event.type === 'assistant_messages') {
          voiceTurn.events = voiceTurn.events.filter((item) => !['token', 'assistant_messages'].includes(item.type));
        }
        const previous = voiceTurn.events[voiceTurn.events.length - 1];
        if (event.type === 'token' && previous?.type === 'token' && previous.message_index === event.message_index) {
          voiceTurn.events[voiceTurn.events.length - 1] = { ...previous, content: previous.content + event.content };
        } else if (event.type === 'reasoning' && previous?.type === 'reasoning') {
          voiceTurn.events[voiceTurn.events.length - 1] = { ...previous, content: previous.content + event.content };
        } else voiceTurn.events.push(event);
        const visible = voiceAssistantId && get().scope === 'assistant' &&
          !get().conversationsLoading && get().messages.some((m) => m.id === voiceAssistantId);
        if (visible) applyVoiceEvent(update.event, voiceAssistantId!);
        else showVoiceTurn();
      } else {
        voiceTurn.complete = true;
        if (get().scope !== 'assistant' || get().isStreaming) return;
        if (voiceAssistantId) endTurn(voiceAssistantId);
        set({ pendingApproval: null, pendingQuestion: null });
        if (!voiceTurn.events.some((event) => event.type === 'done')) {
          // 取消连接时服务端仍可能在收尾落盘，立即拉历史会把眼前这轮擦掉。
          // 保留实时消息并标明未完成；下次进入助理页再以服务端历史为准。
          if (voiceAssistantId) set((s) => ({ messages: patchMessage(s.messages, voiceAssistantId!, (m) => ({
            ...m, interrupted: { status: 'aborted', label: '未完成' },
            askedQuestions: m.askedQuestions?.map((q) => q.status === 'pending' ? { ...q, status: 'expired' as const } : q),
          })) }));
          return;
        }
        // SSE 已关闭，服务端已落盘。保留当前画面，收到历史后一次替换。
        void get().openConversation(get().threadId, { force: true, tolerateMissing: true });
      }
    },

    loadPermissionMode: async () => {
      try {
        const data = await fetchPermissionMode();
        // tighten_options 是后端算出来的（比默认档更紧的那些），前端不自己排
        const byValue = new Map(data.catalog.map((m) => [m.value, m]));
        set({
          turnModeOptions: data.tighten_options
            .map((v) => byValue.get(v))
            .filter((m): m is PermissionModeInfo => Boolean(m)),
          defaultModeLabel: data.label,
        });
        // 默认档变化后，本轮那个选择可能已经不合法（比如默认已经是只读，
        // 再选「人工确认」等于放宽）—— 后端会自己丢掉，但界面上留着它
        // 只会让人以为「我选了没用」，所以直接退回默认
        const current = get().turnMode;
        if (current && !data.tighten_options.includes(current)) set({ turnMode: '' });
      } catch {
        // 档位读不到就当没有这个开关：默认档照旧生效，本轮不传 permission_mode
        set({ turnModeOptions: [], defaultModeLabel: '', turnMode: '' });
      }
    },

    loadModels: async () => {
      try {
        const data = await fetchModels();
        set({ availableModels: data.models, modelProvider: data.provider });
      } catch {
        // 清单拿不到就当只有当前这一个模型 —— 那颗钮还是点得开，
        // 里面至少能跳到「模型管理」
        set({ availableModels: [], modelProvider: '' });
      }
    },

    switchModel: async (model: ModelOption) => {
      // 对话里选模型 = 直接改全局默认（与设置页同一个写入口，不另造第二把钥匙）。
      //
      // ★ 必须带上 `profile_id`
      //
      // 候选来自多家的连接，只发 `LLM_MODEL` 会让新模型继续连在旧供应商上
      // （qwen 的名字打 Moonshot 的门，必然 404）。后端按 profile 把
      // Base URL 与 Key 一起换过去；没有 profile 的旧配置才退回只改模型名。
      //
      // ★ 换过去的模型若没有思考能力，开关要当场收敛
      //
      // 开关状态是会话级的（localStorage），不收敛的话它会带着「开」走进一个
      // 不支持思考的模型 —— 后端会静默忽略（app/llm/thinking.py 的最后一道保险），
      // 但界面上一个永远点不动的亮开关比一次明确的「已自动关闭」更让人困惑。
      const cap = model.thinking;
      if (cap && !cap.can_on && get().thinkingEnabled) {
        set({ thinkingEnabled: false });
        toast('当前模型不支持深度思考，已自动关闭', 'info');
      }
      try {
        await updateLlmModel({
          LLM_MODEL: model.id,
          ...(model.profile_id ? { profile_id: model.profile_id } : {}),
        });
      } finally {
        await get().loadModels();
        void get().loadServiceInfo(true);
      }
    },

    loadServiceInfo: async (force = false) => {
      // `force` 由「换过模型」的调用方传：模块级缓存的前提是「一次会话里模型不变」，
      // 而语言模型的运行时切换正好打破它（见 api.ts 的 invalidateServiceInfo）。
      if (force) invalidateServiceInfo();
      try {
        set({ serviceInfo: await fetchServiceInfo(), serviceInfoStatus: 'ok' });
      } catch {
        // 后端没起来时侧栏照样要能用 —— 关于页据此显示「未连接」
        set({ serviceInfoStatus: 'down' });
      }
    },

    // ================================================================
    // 发送消息 → 消费全量 SSE 事件流
    // ================================================================
    send: async (text: string, attachments?: AttachmentMeta[]) => {
      const trimmed = text.trim();
      // 助理输入框已被桌面助手语音回合锁住时，send 也要早退 —— 界面禁用只是第一道
      if (!trimmed || get().isStreaming || (get().scope === 'assistant' && get().voiceBusy)) return;

      const threadId = get().threadId;
      const attachmentIds = (attachments ?? []).map((a) => a.attachment_id);
      // 本轮选项在**发送这一刻**取一次：流中途用户改了开关，不该影响正在跑的这一轮
      const { webEnabled, thinkingEnabled, turnMode } = get();
      // Any history request started before this send is now stale, even if it
      // returns after a short response has already finished.
      historyRequest += 1;
      const assistantId = beginTurn(trimmed, true, attachments?.length ? attachments : undefined);

      const controller = new AbortController();
      abortController = controller;
      reportChatLifecycle(threadId, 'stream_started');
      let receivedDone = false;
      // 本轮墙钟耗时：从发出的这一刻算到 `done`。后端不提供这个数
      // （它落在 trace 里，属于开发者视角），前端自己计时最省事。
      const startedAt = performance.now();

      try {
        for await (const event of streamChat({
          message: trimmed,
          threadId,
          attachmentIds,
          // 不传时由后端取默认值 —— 「默认」只有一处定义，前端不重复一遍
          web: webEnabled,
          thinking: thinkingEnabled,
          // 空串 = 不传，由后端用设置里的默认档（「默认只有一处定义」）
          permissionMode: turnMode || undefined,
          signal: controller.signal,
        })) {
          if (abortController !== controller || get().threadId !== threadId) continue;
          applyAgentEvent(event, assistantId, set);
          if (event.type === 'done') {
            receivedDone = true;
            const durationMs = Math.round(performance.now() - startedAt);
            set((s) => ({
              messages: patchMessage(s.messages, assistantId, (m) => ({ ...m, durationMs })),
            }));
          }
        }
      } catch (e) {
        const aborted = controller.signal.aborted || (e instanceof DOMException && e.name === 'AbortError');
        if (!aborted && abortController === controller && get().threadId === threadId) {
          const message = errorText(e);
          set((s) => ({
            error: message,
            messages: patchMessage(s.messages, assistantId, (m) => ({ ...m, error: message })),
          }));
        }
      } finally {
        // An aborted request may settle after a new send. It owns neither the
        // new controller nor the new conversation's streaming state.
        if (abortController !== controller || get().threadId !== threadId) return;
        abortController = null;
        if (!receivedDone) {
          reportChatLifecycle(threadId, 'stream_incomplete');
          set((s) => ({ messages: patchMessage(s.messages, assistantId, (m) => ({
            ...m, interrupted: m.interrupted ?? { status: 'aborted', label: controller.signal.aborted ? '已停止' : '连接中断，回答未完成，可重新生成' },
          })) }));
        } else reportChatLifecycle(threadId, 'stream_completed');
        endTurn(assistantId);
        // 「自动」要等后端在首轮里把目录建出来才变成 `2026102901`，
        // 胶囊得跟着换名字；`boundEntry` 已有值就不用每轮再问一次。
        const workdirState = useWorkdirStore.getState();
        if (workdirState.boundMode === 'auto' && !workdirState.boundEntry && workdirState.boundThreadId === threadId) {
          void workdirState.syncBinding(threadId);
        }
        // 这一轮刚被后端落盘 —— 刷新列表，让标题/时间/轮次数立刻反映出来。
        // 放在 finally 而不是 done 分支：中断与报错的轮次同样会落盘
        // （后端把「用户按了停止」也当作一轮留痕），列表必须一起更新。
        void get()
          .refreshConversations({ silent: true })
          .then(() => {
            // 首轮之后后端会**异步**用模型换一个更短的标题（把「在吗」变成
            // 「报销流程咨询」，见后端 CONVERSATION_AUTO_TITLE）。它是后台任务，
            // 落地时机取决于网关（本机实测 15~20 秒），所以这里**轮询**着等它出现，
            // 而不是猜一个固定延迟 —— 猜早了永远看不到，猜晚了白等。
            const conv = get().conversations.find((c) => c.thread_id === threadId);
            if (!conv || conv.turns !== 1) return; // 只有首轮才会被改标题
            const before = conv.title;
            let tries = 0;
            const timer = window.setInterval(() => {
              tries += 1;
              void get()
                .refreshConversations({ silent: true })
                .then(() => {
                  const now = get().conversations.find((c) => c.thread_id === threadId);
                  // 标题变了就收工（自动标题落地，或者用户自己改了名）；
                  // 试满 8 次（≈40 秒）也收工 —— 在没有网关的环境里不该无限轮询。
                  if (!now || now.title !== before || tries >= 8) window.clearInterval(timer);
                });
            }, 5000);
          });
      }
    },

    // ================================================================
    // HITL 审批
    // ================================================================
    approve: async (scope = 'once') => {
      const pending = get().pendingApproval;
      if (!pending) return;

      // 乐观关闭弹窗，避免用户重复点击
      set((s) => ({
        pendingApproval: null,
        toolCalls: s.toolCalls.map((t) =>
          t.callId === pending.callId ? { ...t, status: 'pending' as const } : t,
        ),
      }));

      try {
        await submitApproval(
          get().threadId,
          pending.callId,
          true,
          pending.runId,
          pending.employeeId,
          scope,
          pending.requestId,
        );
      } catch (e) {
        set({ error: errorText(e), pendingApproval: pending });
      }
    },

    reject: async () => {
      const pending = get().pendingApproval;
      if (!pending) return;

      set((s) => ({
        pendingApproval: null,
        toolCalls: s.toolCalls.map((t) =>
          t.callId === pending.callId ? { ...t, status: 'rejected' as const } : t,
        ),
      }));

      try {
        await submitApproval(
          get().threadId,
          pending.callId,
          false,
          pending.runId,
          pending.employeeId,
          'once',
          pending.requestId,
        );
      } catch (e) {
        // 与 `approve` 同样把弹窗放回去：后端现在对少一维是硬失败，
        // 吞掉它等于让用户以为「已经拒了」，而实际上那次危险操作还在等。
        set((s) => ({
          error: errorText(e),
          pendingApproval: pending,
          toolCalls: s.toolCalls.map((t) =>
            t.callId === pending.callId ? { ...t, status: 'awaiting_approval' as const } : t,
          ),
        }));
      }
    },

    // ================================================================
    // 模型提问（`ask_user`）—— 与审批并列，不是它的一个分支
    // ================================================================
    answerQuestion: async (answers, opts) => {
      const pending = get().pendingQuestion;
      if (!pending) return;
      const skipped = Boolean(opts?.skipped);

      try {
        await answerQuestionApi({
          // 载荷自带归属就信载荷：切换会话的竞态里 get().threadId 可能已经变了，
          // 而那张卡属于发出它的那根 thread
          threadId: pending.threadId ?? get().threadId,
          callId: pending.callId,
          questionId: pending.questionId,
          answers,
          skipped,
          runId: pending.runId,
          employeeId: pending.employeeId,
        });
      } catch (e) {
        if (e instanceof QuestionExpiredError) {
          // 卡已经失效（超时 / 另一个窗口答过 / 恢复后重新提问）：
          // 收成只读即可。这不是「提交失败」，不该让用户在错误提示下继续点。
          set((s) => ({
            pendingQuestion:
              s.pendingQuestion?.questionId === pending.questionId ? null : s.pendingQuestion,
            messages: patchAskedQuestion(s.messages, pending.questionId, (q) => ({
              ...q,
              status: 'expired',
            })),
          }));
          return;
        }
        // 网络错误：卡片留在原地并显示原因，用户可以重试
        throw e;
      }

      // 乐观收起 —— SSE 的 `question_resolved` 随后会再落一次（幂等）。
      // 不等它是为了点击后立刻有反馈：那一刻后端已经在继续跑了。
      const status = skipped ? ('skipped' as const) : ('answered' as const);
      set((s) => ({
        pendingQuestion:
          s.pendingQuestion?.questionId === pending.questionId ? null : s.pendingQuestion,
        messages: patchAskedQuestion(s.messages, pending.questionId, (q) => ({
          ...q,
          status,
          answers: skipped ? {} : answers,
          ...(skipped ? { reason: 'skipped' } : {}),
        })),
      }));
    },

    skipQuestion: async () => {
      await get().answerQuestion({}, { skipped: true });
    },

    // ================================================================
    // 其它
    // ================================================================
    abort: () => {
      if (abortController) reportChatLifecycle(get().threadId, 'stop_requested');
      abortController?.abort();
      // Keep ownership until this request settles; a replacement send may
      // replace it immediately, and its controller must then remain intact.
      set((s) => ({
        isStreaming: false,
        pendingApproval: null,
        pendingQuestion: null,
        // 中断之后这张卡再也答不了了（后端会把等待结算成 cancelled）——
        // 留着可点的样子只会让用户白点一次
        messages: s.messages.map((m) => {
          const stopped = m.role === 'assistant' && m.streaming ? {
            ...m, streaming: false, interrupted: { status: 'aborted', label: '已停止' },
          } : m;
          return stopped.askedQuestions?.some((q) => q.status === 'pending')
            ? {
                ...stopped,
                askedQuestions: stopped.askedQuestions.map((q) => q.status === 'pending'
                  ? { ...q, status: 'expired' as const, reason: 'cancelled' } : q),
              }
            : stopped;
        }),
      }));
    },

    newSession: () => {
      // ★ 助理没有「新建会话」这一说：她全场就那一条固定会话。
      // 按钮在助理页已经不出现，这里再守一道 —— 快捷键之类的旁路也进不来。
      if (get().scope === 'assistant') return;
      historyRequest += 1;
      if (abortController) reportChatLifecycle(get().threadId, 'new_session');
      abortController?.abort();
      abortController = null;
      const nextThread = resetThreadId('main');
      set({
        messages: [],
        threadId: nextThread,
        isStreaming: false,
        intent: null,
        intentConfidence: null,
        agentChain: [],
        toolCalls: [],
        pendingApproval: null,
        pendingQuestion: null,
        error: null,
        restoreNote: null,
        // ★ **不清** tasks —— 后台子任务的生命周期与消息不同：
        // 它活得比这一轮更久（关页面也在跑），切会话不该让它凭空消失。
        // 下一行的 refreshTasks 会按新 thread_id 重新拉一次，
        // 那一刻旧任务才被替换掉。
        taskStorage: 'unknown',
      });
      // ★ 新对话默认「自动分配」：这段对话的产出落在它自己的文件夹里。
      // 先把界面切过去再发请求 —— 否则胶囊会在首个往返里闪一下「选择工作区」。
      useWorkdirStore.setState({
        boundId: AUTO_WORKDIR_ID,
        boundMode: 'auto',
        boundEntry: null,
        boundThreadId: nextThread,
      });
      void useWorkdirStore
        .getState()
        .bind(nextThread, AUTO_WORKDIR_ID)
        .catch(() => {
          // 绑定没落上就退回「未指定」：宁可这一轮走沙箱，
          // 也不让界面显示一个后端并不知道的状态
          useWorkdirStore.setState({ boundId: '', boundMode: 'none', boundEntry: null });
        });
      void get().refreshTasks({ silent: true });
    },

    setScope: async (scope) => {
      if (get().scope === scope) return;
      // ChatPage retries the requested scope after this response completes.
      // Route restoration must never silently cancel a live generation.
      if (get().isStreaming) {
        reportChatLifecycle(get().threadId, 'scope_restore_deferred');
        return;
      }
      historyRequest += 1;
      // ★ 切空间**不动侧栏的会话列表** —— 列表永远是普通对话那批记录，
      // 助理页只是把「聊天区此刻显示哪根 thread」换掉。进助理再退出来，
      // 主界面的消息记录一条都不会少（它们一直各自躺在服务端历史里）。
      set({
        scope,
        conversationsLoading: false,
        messages: [],
        threadId: createThreadId(scope),
        isStreaming: false,
        intent: null,
        intentConfidence: null,
        agentChain: [],
        toolCalls: [],
        pendingApproval: null,
        pendingQuestion: null,
        error: null,
        restoreNote: null,
        taskStorage: 'unknown',
      });
      // 把新空间那根 thread 的历史恢复出来：
      // - 助理：她的历史挂在固定 id 上，服务端还没有这一条时（第一次找她）
      //   `tolerateMissing` 会把那次 404 咽掉 —— 停在欢迎态，不弹「没有找到该会话」；
      // - 普通对话：只有列表里确实有这条才去取（刚点的「新建会话」是根空 thread，
      //   为它打这一枪必然 404，而那不该报错）。
      const threadId = get().threadId;
      const worthRestoring =
        scope === 'assistant' ||
        get().conversations.some((c) => c.thread_id === threadId && c.exists);
      if (worthRestoring && get().messages.length === 0 && !get().isStreaming) {
        await get().openConversation(threadId, { tolerateMissing: scope === 'assistant' });
        if (get().scope !== scope || get().threadId !== threadId) return; // 期间又切走了
      }
      // 进助理页 = 她可能先开口。服务端自己判断时机（间隔/静音时段/当天次数），
      // 前端不做第二套规则；响应慢也不挡页面，任务列表先刷新。
      if (scope === 'assistant') void runAssistantCheckin();
      void get().refreshTasks({ silent: true });
    },

    upload: async (file: File) => {
      set({ uploading: true, error: null });
      try {
        const result = await uploadFile(file);
        const entry: UploadedFile = {
          name: result.filename,
          size: result.file_size,
          chunks: result.chunks_indexed,
          success: result.success,
          message: result.message,
          uploadedAt: Date.now(),
        };
        set((s) => ({
          uploadedFiles: [entry, ...s.uploadedFiles.filter((f) => f.name !== entry.name)],
          uploading: false,
        }));
        return result;
      } catch (e) {
        const message = errorText(e);
        set({ uploading: false, error: message });
        throw e;
      }
    },

    removeUploadedFile: (name: string) =>
      set((s) => ({ uploadedFiles: s.uploadedFiles.filter((f) => f.name !== name) })),

    clearError: () => set({ error: null }),

    // ================================================================
    // 会话历史（Phase 7）
    // ================================================================
    refreshConversations: (opts: { silent?: boolean } = {}) => {
      // 多个入口共享同一次请求，旧响应不会覆盖更新的列表。
      if (conversationListRequest) return conversationListRequest;
      const visible = !opts.silent || !get().conversationsLoaded || !!get().conversationsError;
      if (visible) set({ conversationsListLoading: true, conversationsError: null });
      conversationListRequest = (async () => {
        try {
          const data = await fetchConversations({ limit: 50 });
          set({
            conversations: data.conversations,
            conversationsTotal: data.total,
            historyDir: data.stats?.dir ?? '',
            conversationsLoaded: true,
            conversationsError: null,
          });
        } catch (e) {
          // 保留已有列表，错误只在侧栏展示，不占用聊天错误条。
          if (visible) set({ conversationsError: errorText(e) });
        } finally {
          set({ conversationsListLoading: false });
          conversationListRequest = null;
        }
      })();
      return conversationListRequest;
    },

    /**
     * 打开页面时调用：拉列表 + 把上次那个会话的历史恢复出来。
     *
     * 这就是「关掉浏览器再打开，接着上次聊」这条体验的落点。
     * 只在本地还没有消息时才恢复 —— 否则会把用户已经开始的新对话覆盖掉。
     */
    bootstrap: () => {
      if (bootstrapRequest) return bootstrapRequest;
      if (get().bootstrapped) return Promise.resolve();
      // 记录请求开始时的会话，重试期间新建/切换会话后不再恢复旧历史。
      const initialThread = get().threadId;
      bootstrapRequest = (async () => {
        // 模型清单与权限档位并行拉，不阻塞历史列表。
        void get().loadModels();
        void get().loadPermissionMode();

        await get().refreshConversations();
        // 失败不标记完成：侧栏重试和重新联网仍可恢复首屏历史。
        if (get().conversationsError) return;
        set({ bootstrapped: true });
        // 助理的固定会话由 setScope 恢复；普通列表不负责恢复助理。
        if (get().scope !== 'main') return;
        if (get().messages.length > 0 || get().isStreaming) return;
        if (get().threadId !== initialThread) return;

        const threadId = get().threadId;
        if (!get().conversations.some((c) => c.thread_id === threadId && c.exists)) return;
        await get().openConversation(threadId);
      })().finally(() => { bootstrapRequest = null; });
      return bootstrapRequest;
    },

    openThread: async (threadId: string) => {
      if (get().isStreaming && threadId !== get().threadId) {
        toast('当前回答仍在生成，请完成后再切换会话，或先点击停止。', 'info');
        return;
      }
      // 助理那条永远不从列表/面板进来（她不在列表里）；走到这里的一定是普通对话。
      const route = isAssistantThreadId(threadId) ? '/assistant' : '/chat';
      const hash = `#${route}`;
      if (get().scope !== (route === '/assistant' ? 'assistant' : 'main')) {
        // 换空间：先把「要看哪根」记下来，恢复交给目标页面挂载时的 setScope。
        // 在这里抢跑 openConversation 会把同一份历史拉两遍（还多闪一次「正在恢复会话」）。
        writeThreadId(threadId);
        if (window.location.hash !== hash) window.location.hash = hash;
        return;
      }
      await get().openConversation(threadId);
      if (window.location.pathname !== route) window.location.hash = hash;
    },

    /**
     * 切换到一个历史会话。
     *
     * 走的是「重放事件流」这条路（见文件头）；顺带通知后端准备模型上下文，
     * 后端只有在 Checkpointer 里已经没有这个会话时才会真的播种，
     * 于是「这次恢复到底做没做」在界面上是可解释的。
     */
    openConversation: async (threadId: string, opts?: { tolerateMissing?: boolean; force?: boolean }) => {
      if (!opts?.force && threadId === get().threadId && get().messages.length > 0) return;
      // Includes force-refresh callers from check-in / voice restoration. A
      // read of history is not an instruction to stop the current response.
      if (get().isStreaming) {
        reportChatLifecycle(get().threadId, 'history_restore_ignored', threadId);
        return;
      }

      const request = ++historyRequest;
      const previousThread = get().threadId;
      const scope = get().scope;
      const stillCurrent = () => request === historyRequest && get().scope === scope &&
        get().threadId === previousThread && !get().isStreaming;
      set({ conversationsLoading: true, error: null, restoreNote: null,
        ...(opts?.force ? {} : { messages: [] }) });
      try {
        const detail = await fetchConversation(threadId);
        if (!stillCurrent()) return;
        set({ messages: [] });
        voiceAssistantId = null;

        for (const [index, turn] of detail.turns.entries()) {
          // 她先开口的那一轮没有用户消息：只补一条助手消息，
          // 并把 `initiatedBy` 带上 —— 删除/重新生成据此知道「这一轮没有提问」。
          const assistantId =
            turn.initiated_by === 'assistant'
              ? beginAssistantTurn('', index, turn.turn_id)
              : beginTurn(
                  turn.user_input,
                  false,
                  turn.attachments?.length ? turn.attachments : undefined,
                  index,
                );
          // 重放：未决提问按「已失效」重建（见 `askedFromPayload`）
          for (const event of turn.events) applyAgentEvent(event, assistantId, set, true);
          // 回答文本由后端单独存着（token 事件不入库），这里补成一个 token 事件
          if (turn.answer && !turn.events.some(event => event.type === 'assistant_messages')) {
            applyAgentEvent({ type: 'token', content: turn.answer }, assistantId, set);
          }
          // 这一轮的推理正文：与实时路径同一个字段、同一块渲染。
          // ★ 落盘的 `answer` 已经是摘掉推理的那一半，所以这里**不需要**再切一次 ——
          // 重放与实时切在同一条线上的保证来自后端，不来自这里。
          if (turn.reasoning) {
            set((s) => ({
              messages: patchMessage(s.messages, assistantId, (m) => ({ ...m, reasoning: turn.reasoning })),
            }));
          }
          // 状态不是「完成」的轮次要显式标出来 —— 否则用户看到一个半截回答，
          // 会以为是恢复过程把它截断了，而其实是当时就停在这里
          if (turn.status && turn.status !== 'ok') {
            set((s) => ({
              messages: patchMessage(s.messages, assistantId, (m) => ({
                ...m,
                interrupted: { status: turn.status, label: turn.status_label || turn.status },
              })),
            }));
          }
          // 这一轮产出的文件：重放路径直接从轮次里拿（不必再请求 /files），
          // 与实时路径同一个渲染分支 —— 见 `ChatMessage` 的 `TurnFiles`。
          if (turn.files?.length) {
            set((s) => ({
              messages: patchMessage(s.messages, assistantId, (m) => ({ ...m, files: turn.files })),
            }));
          }
          // 这一轮的用量：`done` 不进重放（不变量 45），所以它落在轮次记录里。
          // 与 `files` 同一处理方式 —— 回看历史时也看得到用量，
          // 而不是只有当场那一屏才有。
          if (turn.usage && Object.keys(turn.usage).length) {
            set((s) => ({
              messages: patchMessage(s.messages, assistantId, (m) => ({ ...m, usage: turn.usage })),
            }));
          }
          endTurn(assistantId);
        }

        writeThreadId(threadId);
        set({
          threadId,
          conversationsLoading: false,
          isStreaming: false,
          // 历史里挂着未决审批是常态（用户当时关掉了页面）——
          // 绝不能因此在打开会话时弹一个「点了也没用」的审批窗。
          pendingApproval: null,
          // 提问同理：卡片在重放里被重建为「已失效」，但**绝不允许**进入等待态
          pendingQuestion: null,
          // ★ 后台任务的进度不经过事件重放 —— 子任务的进度只走 REST 轮询
          // （后端从不发 subtask_progress/subtask_done，见 types/index.ts）。
          // 所以这里不需要「重放不弹窗」的处理，下面紧接着重新拉一次，
          // 让进度回到**现在**的状态而不是当时的快照。
        });
        showVoiceTurn();

        // ★ 换会话后重新拉后台任务
        // 重放给的是「当时那一轮」的事件，而任务还在继续跑 ——
        // 不重新拉的话，用户会看到一个停在昨天的进度条。
        void get().refreshTasks({ silent: true });

        // 让后端准备模型上下文（必要时从历史播种）
        try {
          const resumed = await resumeConversation(threadId);
          if (resumed.seeded && request === historyRequest && get().threadId === threadId && get().scope === scope) set({ restoreNote: resumed.reason });
        } catch {
          // 恢复上下文失败不阻塞回看历史；下一轮问答时后端还会再试一次
        }
      } catch (e) {
        if (!stillCurrent()) return;
        // tolerateMissing：助理那条会话**可能还没被创建过**（第一次找她）。
        // 服务端答「没有这一条」不是故障，是她的常态 —— 安静停在欢迎态，
        // 别为一次必然的 404 弹一条「没有找到该会话」的红条。
        set({
          conversationsLoading: false,
          ...(opts?.tolerateMissing ? {} : { error: errorText(e) }),
        });
        showVoiceTurn();
      }
    },

    removeConversation: async (threadId: string) => {
      try {
        await deleteConversation(threadId);
      } catch (e) {
        toast(errorText(e), 'error');
        return;
      }

      set((s) => ({
        conversations: s.conversations.filter((c) => c.thread_id !== threadId),
        conversationsTotal: Math.max(0, s.conversationsTotal - 1),
      }));

      // 删掉的正是当前会话：界面必须跟着走，否则会出现
      // 「侧栏里已经没有它了，聊天区还显示着它的内容、还能继续发消息」这种半撕状态
      //
      // ★ 后端会联动删掉该会话的后台任务（`api/conversations.py`），
      // 而 `newSession()` 里会重新拉一次 `tasks` —— 所以这里不用额外清理。
      // 删的是**别的**会话时，当前 `tasks` 本来就不属于它，不该动。
      if (threadId === get().threadId) get().newSession();
    },

    removeConversations: async (threadIds: string[]) => {
      const removed = new Set<string>();
      let failed = 0;

      // 串行而不是并发：删除在后端要联动清理该会话的后台任务与产物，
      // 十几个请求同时打上去只会让失败原因变得没法解释。
      for (const threadId of threadIds) {
        try {
          await deleteConversation(threadId);
          removed.add(threadId);
        } catch {
          failed += 1;
        }
      }

      if (removed.size > 0) {
        set((s) => ({
          conversations: s.conversations.filter((c) => !removed.has(c.thread_id)),
          conversationsTotal: Math.max(0, s.conversationsTotal - removed.size),
        }));
        // 当前打开的会话正好在被删的那批里 —— 与单条删除同样收尾，
        // 否则聊天区会停在一段已经不存在的历史上
        if (get().threadId && removed.has(get().threadId)) get().newSession();
      }

      return { removed: removed.size, failed };
    },

    clearAllConversations: async () => {
      // 只清普通对话：助理那一条是她的全部家当，不该被侧栏这个
      // 对着主对话列表的按钮顺手带走（要清空她，去她的页面删那一轮一轮删）
      try {
        await clearConversations('main');
      } catch (e) {
        toast(errorText(e), 'error');
        return;
      }
      set({ conversations: [], conversationsTotal: 0 });
      get().newSession();
    },

    renameConversation: async (threadId: string, title: string) => {
      const clean = title.trim();
      if (!clean) return;
      try {
        const result = await renameConversationApi(threadId, clean);
        set((s) => ({
          conversations: s.conversations.map((c) =>
            c.thread_id === threadId ? { ...c, title: result.conversation.title } : c,
          ),
        }));
      } catch (e) {
        set({ error: errorText(e) });
      }
    },

    dismissRestoreNote: () => set({ restoreNote: null }),

    // ================================================================
    // 消息操作：重新生成 / 编辑消息
    // ================================================================
    regenerate: (message: ChatMessage) => {
      if (get().scope === 'assistant' && get().voiceBusy) return;
      const messages = get().messages;
      const targetIndex = messages.findIndex((m) => m.id === message.id);
      if (targetIndex < 0 || message.role !== 'assistant') return;
      // 她主动开口的那一轮没有用户提问 —— 没有可以重发的内容。
      // 不守这一条，往前扫描会捡到**上一轮**的用户消息，把历史重发一遍。
      if (message.initiatedBy === 'assistant') return;

      // 找到这条 assistant 消息对应的用户消息：往前扫描最近一条 user
      let userIndex = -1;
      for (let i = targetIndex - 1; i >= 0; i--) {
        if (messages[i].role === 'user') {
          userIndex = i;
          break;
        }
      }
      if (userIndex < 0) return;

      const userMessage = messages[userIndex];
      const userText = userMessage.content;
      // 截断到该用户消息（含），然后重新发送
      set((s) => ({
        messages: s.messages.slice(0, userIndex),
        intent: null,
        intentConfidence: null,
        agentChain: [],
        toolCalls: [],
        pendingApproval: null,
        pendingQuestion: null,
        error: null,
      }));

      void get().send(userText, userMessage.attachments);
    },

    editMessage: (messageId: string, newText: string) => {
      if (get().scope === 'assistant' && get().voiceBusy) return;
      const trimmed = newText.trim();
      if (!trimmed) return;

      const messages = get().messages;
      const targetIndex = messages.findIndex((m) => m.id === messageId);
      if (targetIndex < 0 || messages[targetIndex].role !== 'user') return;

      // 截断到该消息位置（不含），send 会自己追加新的 user + assistant 消息
      set((s) => ({
        messages: s.messages.slice(0, targetIndex),
        intent: null,
        intentConfidence: null,
        agentChain: [],
        toolCalls: [],
        pendingApproval: null,
        pendingQuestion: null,
        error: null,
      }));

      // 附件跟着消息走：用户改的是文字，带没带文件是另一件事
      void get().send(trimmed, messages[targetIndex].attachments);
    },

    // ================================================================
    // 消息操作：删除一轮 / 从某轮分支
    // ================================================================
    // 轮次下标来自消息自己的 `turnIndex`（与后端轮次文件的行序一一对应）。
    // 不能按「两条消息一轮」数：她主动开口的那一轮只有一条助手消息。
    deleteTurn: async (message: ChatMessage) => {
      if (get().scope === 'assistant' && get().voiceBusy) return;
      const messages = get().messages;
      const index = messages.findIndex((m) => m.id === message.id);
      if (index < 0) return;
      const turnIndex = message.turnIndex ?? Math.floor(index / 2);
      const threadId = get().threadId;

      try {
        const result = await deleteConversationTurn(threadId, turnIndex);
        if (!result.conversation) {
          // 轮次删光 → 会话本身也被后端删了：切新会话 + 刷新列表。
          // 助理是固定单例，newSession 会直接 no-op —— 这里显式清空她的聊天区，
          // 否则界面上还停着已经被删掉的最后一轮。
          if (get().scope === 'assistant') {
            get().abort();
            set({
              messages: [],
              intent: null,
              intentConfidence: null,
              agentChain: [],
              toolCalls: [],
              pendingApproval: null,
              pendingQuestion: null,
              error: null,
              restoreNote: null,
              taskStorage: 'unknown',
            });
            void get().refreshTasks({ silent: true });
          } else {
            get().newSession();
          }
          await get().refreshConversations({ silent: true });
          return;
        }
        // 本地摘掉这一轮的消息（不重新拉详情重放 —— 那会闪一屏）。
        // 后面的轮次要跟着减一：后端的轮次下标是连续行号，删掉中间一轮
        // 后面全部前移 —— 不重编号的话，下一次删除/分支会指到错误的轮。
        set((s) => {
          const kept: ChatMessage[] = [];
          s.messages.forEach((m, i) => {
            const idx = m.turnIndex ?? Math.floor(i / 2);
            if (idx === turnIndex) return;
            kept.push(idx > turnIndex ? { ...m, turnIndex: idx - 1 } : m);
          });
          // 删掉的如果正是带着「等你回答」的那一轮，等待指针必须一起清掉 ——
          // 否则顶栏一直写着「等待你回答」，而页面上已经没有那张卡可点
          const pending = s.pendingQuestion;
          const alive = pending
            ? kept.some((m) => m.askedQuestions?.some((q) => q.questionId === pending.questionId))
            : false;
          return { messages: kept, ...(alive ? {} : { pendingQuestion: null }) };
        });
        void get().refreshConversations({ silent: true });
      } catch (e) {
        toast(errorText(e), 'error');
      }
    },

    forkFromMessage: async (message: ChatMessage) => {
      // 「从这里分支」会造出一条新会话 —— 助理全场只有那一条，分支等于
      // 凭空多出一个她。入口（消息操作里的分支按钮）在助理页已经不出现，
      // 这里再守一道。
      if (get().scope === 'assistant') return;
      const messages = get().messages;
      const index = messages.findIndex((m) => m.id === message.id);
      if (index < 0) return;
      const turnIndex = message.turnIndex ?? Math.floor(index / 2);
      try {
        // 含这一轮：分支点是「聊到这里为止」
        const result = await forkConversationApi(get().threadId, turnIndex + 1);
        await get().refreshConversations({ silent: true });
        // 打开新会话：重放复制的轮次 + resume 播种模型上下文
        await get().openConversation(result.conversation.thread_id);
      } catch (e) {
        toast(errorText(e), 'error');
      }
    },
  };
});
