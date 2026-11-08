/**
 * 后端 API 封装
 */

import type {
  AgentEvent,
  AgentPayload,
  AgentPromptResponse,
  AgentsResponse,
  AgentWriteResponse,
  ApprovalResponse,
  AssistantCheckinResponse,
  AttachmentMeta,
  BaseAgent,
  ChannelsResponse,
  ComputerControlStatus,
  ComputerControlPreferences,
  ComputerControlPreview,
  ComputerControlVisionTest,
  ConversationDetailResponse,
  ConversationFileContent,
  ConversationFilesResponse,
  ConversationListResponse,
  ConversationSearchResponse,
  ConversationSummary,
  Employee,
  EmployeeListResponse,
  EmployeeWorksResponse,
  FileUploadResponse,
  Group,
  GroupListResponse,
  GroupMessagesResponse,
  HookInfo,
  HookRunsResponse,
  HookStats,
  HookTestResponse,
  HooksResponse,
  HookWritePayload,
  IndexDocument,
  IndexStatus,
  KnowledgeScopeResponse,
  LlmModelsResponse,
  LlmProbeResult,
  LlmRemoteModelsResponse,
  MarketCreateBrief,
  MarketEntry,
  MarketKind,
  MarketLocalInstallResponse,
  MarketMcpImportResponse,
  MarketSearchResponse,
  MarketSource,
  MarketWriteResponse,
  McpApplyResult,
  McpServer,
  McpServerPayload,
  McpStatusResponse,
  MemoryCreateResponse,
  MemoryItem,
  MemoryListResponse,
  MemoryPromptBlockResponse,
  ModelsResponse,
  PersonaFragmentResponse,
  PersonaProfile,
  PersonaResponse,
  PersonaUpdateResponse,
  PermissionModeResponse,
  PermissionModeValue,
  PermissionRulesResponse,
  QuestionAnswerResponse,
  QuestionAnswers,
  QuickCommand,
  QuickCommandListResponse,
  QuickCommandWritePayload,
  RagModelsResponse,
  RagProbeResult,
  ResumeResponse,
  RuleStats,
  RuleWritePayload,
  RuleWriteResponse,
  ScanPlan,
  ScanReport,
  ScheduleDetailResponse,
  ScheduleRunNowResponse,
  ScheduledRunListResponse,
  ScheduledTaskDTO,
  ScheduledTaskListResponse,
  ScheduleWritePayload,
  SkillDetailResponse,
  SkillInfo,
  SubtaskListResponse,
  SubtaskDTO,
  ToolListResponse,
  UsageOverviewResponse,
  UsageRecordsResponse,
  WorkdirBinding,
  WorkdirBrowseResult,
  WorkdirEntry,
  WorkdirListResponse,
  WorkdirRoot,
  WorkdirWriteResponse,
  AutoWorkdirListResponse,
  VoiceProbeResult,
  VoiceSettingsResponse,
  WechatLoginState,
  WorkflowCreatePayload,
  WorkflowDeleteResponse,
  WorkflowDetailResponse,
  WorkflowDTO,
  WorkflowListResponse,
  WorkflowMetaResponse,
  WorkflowRunDetailResponse,
  WorkflowRunStartResponse,
  WorkflowWritePayload,
  Workspace,
  WorkspaceListResponse,
  WorkspaceToolsResponse,
} from '@/types';
import { parseSSE } from '@/lib/sse';

export const API_BASE = import.meta.env.VITE_API_BASE_URL ?? '/api';

/**
 * 当前会话的 thread_id（后端用它做 Checkpointer 的会话键 + 会话历史的归档键）。
 *
 * ★ 存在 localStorage 而不是 sessionStorage
 *
 * sessionStorage 的生命周期是「一个标签页」，于是：刷新能留住、**新开标签页就没了**、
 * 关掉浏览器再打开更是全新会话 —— 而用户理解的是「我刚才聊的那些去哪了」。
 * 换成 localStorage 后，重新打开页面会接着上一次的会话继续
 * （`bootstrap()` 会把它的历史取回来渲染）。
 * 读写都做了 try/catch：隐私模式下 localStorage 可能直接抛异常，
 * 那时退化成「每次都是新会话」，而不是整个应用起不来。
 *
 * ★ 两个命名空间，但只有一个是「会变的那条线」
 *
 * 普通对话（/chat）的当前会话存在 localStorage 里，每次开的都是新 thread。
 * 助理（/assistant）**有且只有一条会话**：id 固定（`assistant_main`），
 * 不存在"新建助理会话"这回事 —— 所以它不需要 localStorage（服务端历史
 * 永远挂在这一个 id 上），也永远不会出现在侧栏的普通对话列表里
 * （后端按 `assistant_` 前缀把它从 main 列表过滤掉）。
 */
const THREAD_KEY = 'xjms.thread_id';

/** 会话命名空间：main=普通对话，assistant=助理 */
export type ThreadScope = 'main' | 'assistant';

/** 助理唯一会话的固定 id（前缀与后端 `ASSISTANT_THREAD_PREFIX` 对齐） */
export const ASSISTANT_THREAD_ID = 'assistant_main';

export function isAssistantThreadId(id: string): boolean {
  return id === ASSISTANT_THREAD_ID;
}

/** 生成（或读回已存的）某个命名空间的 thread_id */
export function createThreadId(scope: ThreadScope = 'main'): string {
  if (scope === 'assistant') return ASSISTANT_THREAD_ID;
  const existing = readThreadId(scope);
  if (existing) return existing;
  const uuid = crypto.randomUUID ? crypto.randomUUID() : `t-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  writeThreadId(uuid);
  return uuid;
}

/** 读取某个命名空间「上次聊到哪」（助理固定那一根，无需存储） */
export function readThreadId(scope: ThreadScope = 'main'): string | null {
  if (scope === 'assistant') return ASSISTANT_THREAD_ID;
  try {
    // 兼容早期把 id 放在 sessionStorage 的版本：能读到就沿用，避免多出一个空会话
    return localStorage.getItem(THREAD_KEY) ?? sessionStorage.getItem(THREAD_KEY);
  } catch {
    return null;
  }
}

export function writeThreadId(id: string): void {
  // 助理那一根不占 localStorage：它没有"上次是哪条"的问题
  if (isAssistantThreadId(id)) return;
  try {
    localStorage.setItem(THREAD_KEY, id);
    sessionStorage.removeItem(THREAD_KEY);
  } catch {
    /* 隐私模式等场景：退化成本次进程内有效 */
  }
}

/** 在普通对话里开启新会话（助理没有"新会话"：它的 thread 永远是那一根） */
export function resetThreadId(scope: ThreadScope = 'main'): string {
  if (scope === 'assistant') return ASSISTANT_THREAD_ID;
  try {
    localStorage.removeItem(THREAD_KEY);
    sessionStorage.removeItem(THREAD_KEY);
  } catch {
    /* ignore */
  }
  return createThreadId(scope);
}

export interface StreamChatOptions {
  message: string;
  threadId: string;
  topK?: number;
  /** 对话附件的 id 列表（`attachFile` 返回的），按顺序注入本轮上下文 */
  attachmentIds?: string[];
  /**
   * 是否允许联网搜索。不传 = 跟随后端默认（开）。
   * 关掉时后端**不给 `browser_agent` 绑定 `web_search` 工具**，
   * 而不是在 prompt 里劝模型别搜 —— 后者会被无视，前者想调也调不到。
   */
  web?: boolean;
  /** 是否走知识图谱召回。不传 = 跟随后端默认（开）。UI 不暴露，留给调用方 */
  graph?: boolean;
  /**
   * 深度思考开关。不传 = 跟随后端全局配置（`.env` 的 `LLM_ENABLE_THINKING`）。
   * 开启时思考内容经 `reasoning` 事件实时流入折叠块。
   */
  thinking?: boolean;
  /**
   * 本轮临时收紧的权限档位。**只能比设置里的默认档更紧**。
   *
   * 后端 `policy.effective_mode` 会取两者中紧的那一档，所以这里传 `full`
   * 也换不来全盘访问 —— 放宽只有设置页那一次点击。
   * 不传 = 跟随默认档。
   */
  permissionMode?: PermissionModeValue;
  signal?: AbortSignal;
}

/** 流式聊天：返回 AgentEvent 异步生成器 */
export async function* streamChat(options: StreamChatOptions): AsyncGenerator<AgentEvent, void, void> {
  const { message, threadId, topK = 5, attachmentIds, web, graph, thinking, permissionMode, signal } = options;

  const response = await fetch(`${API_BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      thread_id: threadId,
      top_k: topK,
      ...(attachmentIds?.length ? { attachments: attachmentIds } : {}),
      // 选项只在显式给出时才传：让「默认值」只有后端一处定义，
      // 前端不重复一遍（重复就会漂移）
      ...(web === undefined ? {} : { web }),
      ...(graph === undefined ? {} : { graph }),
      ...(thinking === undefined ? {} : { thinking }),
      ...(permissionMode ? { permission_mode: permissionMode } : {}),
    }),
    signal,
  });

  if (!response.ok) {
    const detail = await response.text().catch(() => '');
    throw new Error(`请求失败 (${response.status})${detail ? `: ${detail.slice(0, 200)}` : ''}`);
  }

  yield* parseSSE(response);
}

/** Best-effort local diagnostics; never include prompt text or block generation. */
export function reportChatLifecycle(threadId: string, action: 'stream_started' | 'stream_completed' | 'stream_incomplete' | 'stop_requested' | 'new_session' | 'history_restore_ignored' | 'scope_restore_deferred', targetThreadId = ''): void {
  void fetch(`${API_BASE}/chat/lifecycle`, { method: 'POST', keepalive: true,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thread_id: threadId, action, target_thread_id: targetThreadId }),
  }).catch(() => {});
}

/**
 * 提交 HITL 审批结果。
 *
 * ★ 后两个参数是护栏 6 的定位维度，必须原样回传 `interrupt` 事件里给的值。
 *
 * 后端按 `(thread_id, call_id, run_id, employee_id)` 四维**精确**匹配等待者，
 * 少给一维就返回未匹配。群组里 `thread_id` 是共用的、`call_id` 是模型给的短 id，
 * 所以「不传」不是省事，是**甲的批准可能放行乙的危险操作**。
 * 主对话没有这两维 → 空串，与后端缺省一致（附加式改动，老路径行为不变）。
 */
export async function submitApproval(
  threadId: string,
  callId: string,
  approved: boolean,
  runId = '',
  employeeId = '',
  scope: 'once' | 'same_operation' = 'once',
  requestId = '',
): Promise<ApprovalResponse> {
  const response = await fetch(`${API_BASE}/approval`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      thread_id: threadId,
      call_id: callId,
      approved,
      run_id: runId,
      employee_id: employeeId,
      scope,
      request_id: requestId,
    }),
  });

  if (!response.ok) {
    throw new Error(`审批提交失败 (${response.status})`);
  }
  const data = (await response.json()) as ApprovalResponse;
  // 后端把「没找到对应的等待」做成了 200 + success=false（它不是 HTTP 层的错）。
  // 不读这个标志，用户点完批准什么也没发生，弹窗自己关掉了 —— 那是最坏的失效。
  if (!data.success) {
    throw new Error(data.message || '该审批已过期或不属于当前请求方');
  }
  return data;
}

/**
 * 提交用户对模型提问的回答（`POST /questions/answer`）。
 *
 * ★ 与审批同构的定位键 + 一个不能少的 `question_id`
 * 后端按 `(thread_id, call_id, run_id, employee_id)` 匹配等待者，
 * `question_id` 再校验这张卡片没有过期 —— 旧标签页上点一下，
 * 不能把恢复之后重新发起的那次提问答掉。
 *
 * `success=false` 是 200 回执（「已超时 / 已被另一标签页处理」），
 * 不是 HTTP 错误；这里抛成 Error 是为了让调用方必须处理它（与 `submitApproval` 一致）。
 */
/**
 * 「这张卡已经失效」——超时、已被另一个标签页处理、或恢复后重新发起了新提问。
 *
 * ★ 与网络错误必须分开
 * 网络错误该把卡片留在原地、让用户重试；卡失效则要把它收成只读，
 * 否则用户会对着一个永远点不动的按钮反复点。
 */
export class QuestionExpiredError extends Error {}

export async function answerQuestion(payload: {
  threadId: string;
  callId: string;
  questionId: string;
  answers: QuestionAnswers;
  skipped?: boolean;
  runId?: string;
  employeeId?: string;
}): Promise<QuestionAnswerResponse> {
  const response = await fetch(`${API_BASE}/questions/answer`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      thread_id: payload.threadId,
      call_id: payload.callId,
      question_id: payload.questionId,
      answers: payload.answers,
      skipped: Boolean(payload.skipped),
      run_id: payload.runId ?? '',
      employee_id: payload.employeeId ?? '',
    }),
  });
  if (!response.ok) {
    throw new Error(`提交回答失败 (${response.status})`);
  }
  const data = (await response.json()) as QuestionAnswerResponse;
  if (!data.success) {
    throw new QuestionExpiredError(
      data.message || '这个提问已经失效了（可能已超时，或在另一个窗口里答过）',
    );
  }
  return data;
}

// ---------- 后台子任务（Supervisor 架构，见 ADR 0019）----------

/**
 * 拉后台子任务列表 —— 进度的**第二条通道**。
 *
 * ★ 为什么是轮询而不是又开一条 SSE
 * ADR 0003 选了 SSE-over-HTTP，`lib/sse.ts` 又明确不用 `EventSource`
 * （要 POST + AbortController）。为一条 99% 时间空闲的通道手写第二个
 * SSE 解析器，而且它要承受与 `chat.py` 完全相同的 Nginx 60s 空闲超时 ——
 * 这是最差的连接形态。
 *
 * ★ 什么时候轮询
 * 有非终态任务 && 没在流式接收 && 页面可见。三个条件缺一不可：
 * - 不流式时轮询：正在发消息时 SSE 已经推了，再轮询只是重复
 * - 页面不可见时停：后台标签页轮询既费电，又会在切回来时堆积一批过期响应
 */
export async function fetchTasks(threadId: string): Promise<SubtaskListResponse> {
  const response = await fetch(
    `${API_BASE}/tasks?thread_id=${encodeURIComponent(threadId)}&limit=50`,
  );
  if (!response.ok) {
    // ★ 抛出去而不是返回空列表 ——
    // 「拉不到」与「没有」是两件事，返回空列表会把前者变成后者。
    throw new Error(`任务列表拉取失败 (${response.status})`);
  }
  return (await response.json()) as SubtaskListResponse;
}

/**
 * 取消一个后台任务。
 *
 * 返回 `{ ok: false }` 表示「它已经结束了，没得取消」——
 * **不是**错误（那会显示成「找不到」），任务确实在，只是结束了。
 */
export async function cancelTask(taskId: string): Promise<{ ok: boolean; reason?: string }> {
  const response = await fetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: 'POST',
  });
  if (!response.ok) {
    throw new Error(`取消任务失败 (${response.status})`);
  }
  return (await response.json()) as { ok: boolean; reason?: string };
}

/**
 * 让一个**中断**的任务从断点继续（`POST /api/tasks/{id}/resume`）。
 *
 * ★ `needs_confirm=true` 不是错误
 * 最后一步可能已经生效（文件写了、消息发了）。后端不替用户决定，而是把风险
 * 原样交回（`reason` 里写着最后一步是什么）。界面弹一次确认，用户看过之后
 * 带 `acknowledge=true` 再调一次。
 */
export async function resumeTask(
  taskId: string,
  acknowledge = false,
): Promise<{ ok: boolean; needs_confirm?: boolean; reason?: string }> {
  const qs = acknowledge ? '?acknowledge=true' : '';
  const response = await fetch(
    `${API_BASE}/tasks/${encodeURIComponent(taskId)}/resume${qs}`,
    { method: 'POST' },
  );
  if (!response.ok) {
    throw new Error(`继续任务失败 (${response.status})`);
  }
  return (await response.json()) as { ok: boolean; needs_confirm?: boolean; reason?: string };
}

/**
 * 删除一个**已结束**的后台任务（`DELETE /api/tasks/{id}`）。
 *
 * 与取消同一条约定：`ok=false` 表示「这次没删」（比如还在进行中），
 * 不是「任务不存在」—— 404 才走 throw。
 */
export async function deleteTask(taskId: string): Promise<{ ok: boolean; reason?: string }> {
  const response = await fetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}`, {
    method: 'DELETE',
  });
  if (!response.ok) {
    throw new Error(`删除任务失败 (${response.status})`);
  }
  return (await response.json()) as { ok: boolean; reason?: string };
}

/**
 * 后台任务**进度流**的 URL（`GET /api/tasks/stream`）。
 *
 * ★ 为什么给 URL 而不是在这里直接 fetch
 * 订阅要持有 `AbortController` 并在组件卸载时中断，属于调用方（组件）的生命周期 ——
 * 封装成一个 `fetch` 会让「谁来中断」变糊。这里只统一 URL 构造（含 `API_BASE` 与转义）。
 *
 * 调用方用 `parseSSE<TaskStreamEvent>(resp)` 解析（与主对话流同一个解析器）。
 */
export function tasksStreamUrl(threadId: string): string {
  return `${API_BASE}/tasks/stream?thread_id=${encodeURIComponent(threadId)}`;
}

/**
 * 单个后台任务的完整进度（`GET /api/tasks/{id}`）。
 *
 * 列表接口只带最近若干步，定时任务详情页的「看执行过程」按 task_id 现查 ——
 * 进度只有一份真相（子任务自己的 jsonl），详情页不复制第二份。
 */
export async function fetchTaskDetail(taskId: string, limit = 100): Promise<SubtaskDTO> {
  return getJson<SubtaskDTO>(`/tasks/${encodeURIComponent(taskId)}?limit=${limit}`);
}

// ---------- 定时任务（REST：/api/schedules）----------

/**
 * 定时任务列表。
 *
 * ★ `storage === 'unavailable'` 时必须渲染成「查不了」而不是「没有任务」 ——
 * 与 fetchTasks 同一铁律：错过一次提醒的后果，比列表空白大得多。
 */
export async function fetchSchedules(
  options: { enabled?: boolean; q?: string } = {},
): Promise<ScheduledTaskListResponse> {
  const params = new URLSearchParams();
  if (options.enabled !== undefined) params.set('enabled', String(options.enabled));
  if (options.q) params.set('q', options.q);
  const qs = params.toString();
  return getJson<ScheduledTaskListResponse>(`/schedules${qs ? `?${qs}` : ''}`);
}

/** 新建（freq 非法 / 日期窗已过会在 400 里给一句人话，不静默建一个永不触发的任务） */
export async function createSchedule(payload: ScheduleWritePayload): Promise<ScheduledTaskDTO> {
  return sendJson<ScheduledTaskDTO>('/schedules', 'POST', payload);
}

/**
 * 编辑。
 *
 * ⚠️ 整张表单提交（含空串），不要只 diff 改动项 ——
 * 后端「不带这个键 = 保持原样」，`start_date: ''` 才是「清掉它」。
 */
export async function updateSchedule(
  scheduleId: string,
  payload: Partial<ScheduleWritePayload>,
): Promise<ScheduledTaskDTO> {
  return sendJson<ScheduledTaskDTO>(`/schedules/${encodeURIComponent(scheduleId)}`, 'PUT', payload);
}

export async function deleteSchedule(scheduleId: string): Promise<{ ok: boolean }> {
  return sendJson<{ ok: boolean }>(`/schedules/${encodeURIComponent(scheduleId)}`, 'DELETE');
}

export async function setScheduleEnabled(
  scheduleId: string,
  enabled: boolean,
): Promise<ScheduledTaskDTO> {
  return sendJson<ScheduledTaskDTO>(
    `/schedules/${encodeURIComponent(scheduleId)}/enabled`,
    'PUT',
    { enabled },
  );
}

/** 立即运行一次。`ok=false` 是业务态（上一次还没跑完），不是异常 */
export async function runScheduleNow(scheduleId: string): Promise<ScheduleRunNowResponse> {
  return sendJson<ScheduleRunNowResponse>(
    `/schedules/${encodeURIComponent(scheduleId)}/run-now`,
    'POST',
  );
}

/** 跨任务「运行记录」页签（按开始时间倒序，每条带任务名） */
export async function fetchScheduleRuns(limit = 50): Promise<ScheduledRunListResponse> {
  return getJson<ScheduledRunListResponse>(`/schedules/runs?limit=${limit}`);
}

/** 单个任务的详情 + 它自己的运行时间线 */
export async function fetchScheduleDetail(
  scheduleId: string,
  runsLimit = 50,
): Promise<ScheduleDetailResponse> {
  return getJson<ScheduleDetailResponse>(
    `/schedules/${encodeURIComponent(scheduleId)}?runs_limit=${runsLimit}`,
  );
}

export async function cancelScheduleRun(
  scheduleId: string,
  runId: string,
): Promise<ScheduleRunNowResponse> {
  return sendJson<ScheduleRunNowResponse>(
    `/schedules/${encodeURIComponent(scheduleId)}/runs/${encodeURIComponent(runId)}/cancel`,
    'POST',
  );
}

// ---------- 快捷指令（问法库，REST：/api/quick-commands） ----------

/** 全部指令（内置在前）。输入框 `/`、命令面板、设置页共读这一份。 */
export async function fetchQuickCommands(category = ''): Promise<QuickCommandListResponse> {
  const qs = category ? `?category=${encodeURIComponent(category)}` : '';
  return getJson<QuickCommandListResponse>(`/quick-commands${qs}`);
}

export async function createQuickCommand(payload: QuickCommandWritePayload): Promise<QuickCommand> {
  const res = await sendJson<{ ok: boolean; command: QuickCommand }>('/quick-commands', 'POST', payload);
  return res.command;
}

export async function updateQuickCommand(
  commandId: string,
  payload: QuickCommandWritePayload,
): Promise<QuickCommand> {
  const res = await sendJson<{ ok: boolean; command: QuickCommand }>(
    `/quick-commands/${encodeURIComponent(commandId)}`,
    'PUT',
    payload,
  );
  return res.command;
}

export async function deleteQuickCommand(
  commandId: string,
): Promise<{ ok: boolean; deleted: string }> {
  return sendJson<{ ok: boolean; deleted: string }>(
    `/quick-commands/${encodeURIComponent(commandId)}`,
    'DELETE',
  );
}

// ---------- 可视化工作流（REST：/api/workflows） ----------

/** 编辑器静态清单：节点类型、条件运算符、可选工具（含 danger 标记） */
export async function fetchWorkflowMeta(): Promise<WorkflowMetaResponse> {
  return getJson<WorkflowMetaResponse>('/workflows/meta');
}

export async function fetchWorkflows(query = ''): Promise<WorkflowListResponse> {
  const qs = query ? `?q=${encodeURIComponent(query)}` : '';
  return getJson<WorkflowListResponse>(`/workflows${qs}`);
}

export async function createWorkflow(payload: WorkflowCreatePayload): Promise<WorkflowDTO> {
  return sendJson<WorkflowDTO>('/workflows', 'POST', payload);
}

/** 定义 + 最近若干次运行（编辑页首屏一次读全） */
export async function fetchWorkflowDetail(
  workflowId: string,
  runsLimit = 20,
): Promise<WorkflowDetailResponse> {
  return getJson<WorkflowDetailResponse>(
    `/workflows/${encodeURIComponent(workflowId)}?runs_limit=${runsLimit}`,
  );
}

export async function updateWorkflow(
  workflowId: string,
  payload: WorkflowWritePayload,
): Promise<WorkflowDTO> {
  return sendJson<WorkflowDTO>(`/workflows/${encodeURIComponent(workflowId)}`, 'PUT', payload);
}

/** `ok=false` = 还有一次运行没结束，先取消或等它跑完 */
export async function deleteWorkflow(workflowId: string): Promise<WorkflowDeleteResponse> {
  return sendJson<WorkflowDeleteResponse>(`/workflows/${encodeURIComponent(workflowId)}`, 'DELETE');
}

/** 手动运行。`ok=false` 是业务态（上一次没跑完 / 功能降级），不是异常 */
export async function runWorkflow(
  workflowId: string,
  input: string,
): Promise<WorkflowRunStartResponse> {
  return sendJson<WorkflowRunStartResponse>(
    `/workflows/${encodeURIComponent(workflowId)}/run`,
    'POST',
    { input },
  );
}

export async function fetchWorkflowRun(
  workflowId: string,
  runId: string,
): Promise<WorkflowRunDetailResponse> {
  return getJson<WorkflowRunDetailResponse>(
    `/workflows/${encodeURIComponent(workflowId)}/runs/${encodeURIComponent(runId)}`,
  );
}

export async function cancelWorkflowRun(
  workflowId: string,
  runId: string,
): Promise<WorkflowRunStartResponse> {
  return sendJson<WorkflowRunStartResponse>(
    `/workflows/${encodeURIComponent(workflowId)}/runs/${encodeURIComponent(runId)}/cancel`,
    'POST',
  );
}

/** 上传文件 → 解析 → 入库 */
export async function uploadFile(file: File): Promise<FileUploadResponse> {
  const form = new FormData();
  form.append('file', file);

  const response = await fetch(`${API_BASE}/files/upload`, {
    method: 'POST',
    body: form,
  });

  if (!response.ok) {
    throw new Error(`上传失败 (${response.status})`);
  }
  return (await response.json()) as FileUploadResponse;
}

/**
 * 上传一个**对话附件**（临时上下文，不入知识库，24h 过期）。
 *
 * 与 `uploadFile`（设置 → 知识库）是两条路径：附件回答「拿这份文件问一句话」。
 * 失败时后端 detail 是给人看的一句话（类型不支持 / 超限 / 解析失败），
 * 原样透传 —— 它会直接显示在附件 chip 上。
 */
export async function attachFile(file: File): Promise<AttachmentMeta> {
  const form = new FormData();
  form.append('file', file);

  const response = await fetch(`${API_BASE}/files/attach`, {
    method: 'POST',
    body: form,
  });

  if (!response.ok) {
    let detail = '';
    try {
      const parsed = (await response.json()) as { detail?: unknown };
      if (parsed?.detail) detail = String(parsed.detail);
    } catch {
      detail = await response.text().catch(() => '');
    }
    throw new Error(detail || `附件上传失败 (${response.status})`);
  }
  return (await response.json()) as AttachmentMeta;
}

// ────────────────────────────────────────────────
// 技能（Skills）—— 一个技能 = 磁盘上的一个文件夹
// ────────────────────────────────────────────────

/** 显式重扫（用户拷完技能点一下；清单本身是自动刷新的，这个按钮是"确认给我看"） */
export async function rescanSkills(): Promise<{ ok: boolean; skills: SkillInfo[]; total: number }> {
  return sendJson<{ ok: boolean; skills: SkillInfo[]; total: number }>('/skills/rescan', 'POST');
}

/** 一份技能的元数据 + 正文（`instructions` 已按 L2/L3 拼好，直接展示即可） */
export async function fetchSkillDetail(name: string): Promise<SkillDetailResponse> {
  return getJson<SkillDetailResponse>(`/skills/${encodeURIComponent(name)}`);
}

/**
 * MCP 全景（配置 + 每台 server 的连接状态）。
 *
 * 这一页**只读不缓存**：连接状态是运行时的，两次点击之间就可能变（重连、超时），
 * 而界面显示旧状态比多打一次请求危险得多 —— 用户会照着旧状态判断"它是通的"。
 */
export async function fetchMcp(): Promise<McpStatusResponse> {
  return getJson<McpStatusResponse>('/mcp');
}

/** 新增一个 server（保存即对账：能连就连上并注册工具） */
export async function createMcpServer(
  payload: McpServerPayload,
): Promise<{ ok: boolean; server: McpServer; apply: McpApplyResult }> {
  return sendJson('/mcp', 'POST', payload);
}

/** 修改一个 server（只提交改过的字段；凭据填 `********` = 保持原值） */
export async function updateMcpServer(
  name: string,
  payload: McpServerPayload,
): Promise<{ ok: boolean; server: McpServer; apply: McpApplyResult }> {
  return sendJson(`/mcp/${encodeURIComponent(name)}`, 'PUT', payload);
}

/** 断开重连（无视重试窗口）—— 远端刚重启时点这个 */
export async function reconnectMcpServer(
  name: string,
): Promise<{ ok: boolean; server: string; status: string; connection: McpServer | null }> {
  return sendJson(`/mcp/${encodeURIComponent(name)}/reconnect`, 'POST');
}

// ────────────────────────────────────────────────
// 插件市场（发现 / 已安装 / 连接器 三个面共用）
//
// ★ 安装只认一次性凭证
// 凭证由目录接口下发、十分钟过期、用一次就废。界面上不存在
// 「拿 canonical 直接装」的调用 —— 和权限放宽只做进设置页是同一个思路：
// 能做这件事的入口越少，越不需要靠约定保证安全。
// ────────────────────────────────────────────────

export interface FetchMarketOptions {
  view?: 'discover' | 'installed';
  q?: string;
  kinds?: MarketKind[];
  category?: string;
  featured?: boolean;
  sort?: 'popular' | 'newest';
  page?: number;
  pageSize?: number;
}

/** 目录 / 已安装清单（不缓存：安装状态两次点击之间就会变） */
export async function fetchMarket(options: FetchMarketOptions = {}): Promise<MarketSearchResponse> {
  const params = new URLSearchParams();
  params.set('view', options.view ?? 'discover');
  if (options.q) params.set('q', options.q);
  if (options.kinds?.length) params.set('kinds', options.kinds.join(','));
  if (options.category) params.set('category', options.category);
  if (options.featured) params.set('featured', 'true');
  params.set('sort', options.sort ?? 'popular');
  params.set('page', String(options.page ?? 1));
  if (options.pageSize) params.set('page_size', String(options.pageSize));
  return getJson<MarketSearchResponse>(`/market?${params.toString()}`);
}

/** 重读目录源（远程源重新拉一次） */
export async function refreshMarket(): Promise<{
  ok: boolean;
  sources: MarketSource[];
  failures: MarketSource[];
}> {
  return sendJson('/market/refresh', 'POST');
}

/** 单条详情（详情抽屉） */
export async function fetchMarketEntry(canonical: string): Promise<MarketEntry> {
  return getJson<MarketEntry>(`/market/entry/${encodeURIComponent(canonical)}`);
}

/** 凭安装凭证安装（组合包连带成员） */
export async function installMarketEntry(installRef: string): Promise<MarketWriteResponse> {
  return sendJson('/market/install', 'POST', { install_ref: installRef });
}

/** 卸载（组合包成员不单独卸；内置技能不卸） */
export async function uninstallMarketEntry(canonical: string): Promise<MarketWriteResponse> {
  return sendJson('/market/uninstall', 'POST', { canonical });
}

/** 启用 / 停用（组合包 = 连带全部成员） */
export async function setMarketEnabled(
  canonical: string,
  enabled: boolean,
): Promise<MarketWriteResponse> {
  return sendJson(`/market/entry/${encodeURIComponent(canonical)}/enabled`, 'POST', { enabled });
}

/** 目录外的东西怎么进来：本机路径（目录 / zip）或 zip 地址 */
export async function installMarketLocal(payload: {
  kind: 'skill' | 'plugin';
  source: string;
}): Promise<MarketLocalInstallResponse> {
  return sendJson('/market/install-local', 'POST', payload);
}

/** 界面选本地文件装：技能 = zip / SKILL.md，插件 = 带 plugin.json 的 zip */
export async function uploadMarketBundle(
  file: File,
  kind: 'skill' | 'plugin' | 'auto',
): Promise<MarketLocalInstallResponse> {
  const form = new FormData();
  form.append('file', file);
  form.append('kind', kind);
  const response = await fetch(`${API_BASE}/market/upload`, { method: 'POST', body: form });
  if (!response.ok) throw await toApiError('上传失败', response);
  return (await response.json()) as MarketLocalInstallResponse;
}

/** 粘贴一段 MCP 配置（`mcpServers` 映射 / 数组 / 单个 server 对象） */
export async function importMarketMcpConfig(
  config: string,
): Promise<MarketMcpImportResponse> {
  return sendJson('/market/mcp-import', 'POST', { config });
}

/** 「通过 Syncore 创建」预填的那段话（技能目录、清单格式这些约束由后端给） */
export async function fetchMarketBrief(kind: 'skill' | 'plugin'): Promise<MarketCreateBrief> {
  return getJson<MarketCreateBrief>(`/market/create-brief?kind=${kind}`);
}

// ────────────────────────────────────────────────
// Phase 5：权限档位与例外规则
//
// 用户可在设置中改变全局模式；会话级 permission_mode 仍只能收紧。
// ────────────────────────────────────────────────

/** 当前档位 + 四档说明 + 实际文件边界 */
export async function fetchPermissionMode(): Promise<PermissionModeResponse> {
  return getJson<PermissionModeResponse>('/permissions/mode');
}

export function savePermissionMode(mode: PermissionModeValue, acknowledgeFullAccess = false) {
  return sendJson<PermissionModeResponse>('/permissions/mode', 'PUT', { mode, acknowledge_full_access: acknowledgeFullAccess });
}

export function pauseAIOperations() {
  return sendJson<PermissionModeResponse>('/permissions/pause', 'POST');
}

export interface ApprovalGrantInfo {
  grant_id: string; thread_id: string; employee_id: string; tool_names: string[];
  remaining: number; max_uses: number; expires_at: string; note: string; scope: string;
}
export interface ApprovalRecordInfo {
  record_id: string; tool_name: string; thread_id: string; employee_id: string;
  args: Record<string, unknown>; reason: string; requested_at: string;
  decision_label: string; decided_by_label: string; executed: boolean | null; success: boolean | null;
}
export function fetchApprovalGrants() {
  return getJson<{ grants: ApprovalGrantInfo[] }>('/approval/grants');
}
export function revokeApprovalGrant(grantId?: string) {
  return sendJson<{ ok: boolean }>(`/approval/grants${grantId ? `/${encodeURIComponent(grantId)}` : ''}`, 'DELETE');
}
export function fetchApprovalRecords() {
  return getJson<{ records: ApprovalRecordInfo[]; stats: { enabled: boolean } }>('/approval/records?limit=50');
}

/** 全部例外规则（新建的在前）+ 三个行为的界面文案 */
export async function fetchPermissionRules(): Promise<PermissionRulesResponse> {
  return getJson<PermissionRulesResponse>('/permissions/rules');
}

export async function createPermissionRule(
  payload: RuleWritePayload,
): Promise<RuleWriteResponse> {
  return sendJson<RuleWriteResponse>('/permissions/rules', 'POST', payload);
}

/** 部分更新：没传的字段保持原样（整张表单提交也不会清空没填的框） */
export async function updatePermissionRule(
  ruleId: string,
  payload: RuleWritePayload,
): Promise<RuleWriteResponse> {
  return sendJson<RuleWriteResponse>(
    `/permissions/rules/${encodeURIComponent(ruleId)}`,
    'PUT',
    payload,
  );
}

export async function setPermissionRuleEnabled(
  ruleId: string,
  enabled: boolean,
): Promise<RuleWriteResponse> {
  return sendJson<RuleWriteResponse>(
    `/permissions/rules/${encodeURIComponent(ruleId)}/enabled`,
    'PUT',
    { enabled },
  );
}

export async function deletePermissionRule(
  ruleId: string,
): Promise<{ ok: boolean; stats: RuleStats }> {
  return sendJson<{ ok: boolean; stats: RuleStats }>(
    `/permissions/rules/${encodeURIComponent(ruleId)}`,
    'DELETE',
  );
}

// ────────────────────────────────────────────────
// 电脑操控：总开关只对助理生效
//
// 打开后助理可使用桌面工具，执行仍受统一权限、逐次询问规则与钩子约束；
// 普通对话与数字员工根本拿不到这些工具 —— 作用域判定在后端。
// ────────────────────────────────────────────────

/** 总开关 + 连接器状态 + 已注册的桌面工具数 */
export async function fetchComputerControl(): Promise<ComputerControlStatus> {
  return getJson<ComputerControlStatus>('/computer-control');
}

/** 打开 / 关闭电脑操控（返回完整状态，界面不必自己猜失败后的值） */
export async function setComputerControl(
  enabled: boolean,
): Promise<ComputerControlStatus & { ok: boolean }> {
  return sendJson<ComputerControlStatus & { ok: boolean }>('/computer-control', 'PUT', {
    enabled,
  });
}

export function stopComputerControl(): Promise<ComputerControlStatus> {
  return sendJson('/computer-control/stop', 'POST');
}

export function reconnectComputerControl(): Promise<ComputerControlStatus> {
  return sendJson('/computer-control/reconnect', 'POST');
}

export function updateComputerControlPreferences(patch: Partial<ComputerControlPreferences>): Promise<ComputerControlStatus> {
  return sendJson('/computer-control/preferences', 'PATCH', patch);
}

export function previewComputerControl(): Promise<ComputerControlPreview> {
  return sendJson('/computer-control/preview', 'POST');
}

export function testComputerControlVision(): Promise<ComputerControlVisionTest> {
  return sendJson('/computer-control/vision-test', 'POST');
}

// ────────────────────────────────────────────────
// Phase 5：Hooks
// ────────────────────────────────────────────────

/** 全部钩子 + 事件目录 + 运行环境（一次拿全） */
export async function fetchHooks(): Promise<HooksResponse> {
  return getJson<HooksResponse>('/hooks');
}

/** 总开关。关掉时后端一个进程都不起，不是"只改界面提示" */
export async function setHooksEnabled(
  enabled: boolean,
): Promise<HooksResponse & { ok: boolean }> {
  return sendJson<HooksResponse & { ok: boolean }>('/hooks/enabled', 'PUT', { enabled });
}

export async function createHook(
  payload: HookWritePayload,
): Promise<{ ok: boolean; action: 'created' | 'updated'; hook: HookInfo; stats: HookStats }> {
  return sendJson('/hooks', 'POST', payload);
}

export async function updateHook(
  hookId: string,
  payload: HookWritePayload,
): Promise<{ ok: boolean; hook: HookInfo; stats: HookStats }> {
  return sendJson(`/hooks/${encodeURIComponent(hookId)}`, 'PUT', payload);
}

export async function setHookEnabled(
  hookId: string,
  enabled: boolean,
): Promise<{ ok: boolean; hook: HookInfo; stats: HookStats }> {
  return sendJson(`/hooks/${encodeURIComponent(hookId)}/enabled`, 'PUT', { enabled });
}

export async function deleteHook(hookId: string): Promise<{ ok: boolean; stats: HookStats }> {
  return sendJson(`/hooks/${encodeURIComponent(hookId)}`, 'DELETE');
}

/**
 * 试跑：真的执行那条命令（读写文件、发请求都会发生），只是结果不参与判定。
 *
 * 响应里的 `payload` 就是脚本这次收到的 stdin JSON —— 界面把它展示出来，
 * 用户照着写脚本，不用猜键名。总开关关着时也能试跑（`ran_while_disabled`）。
 */
export async function testHook(
  hookId: string,
  payload: { tool_name?: string; args?: Record<string, unknown>; prompt?: string; answer?: string; message?: string } = {},
): Promise<HookTestResponse> {
  return sendJson<HookTestResponse>(`/hooks/${encodeURIComponent(hookId)}/test`, 'POST', payload);
}

/** 执行留痕（新的在前）：退出码 / 耗时 / 超时标记 / 表态 */
export async function fetchHookRuns(limit = 50): Promise<HookRunsResponse> {
  return getJson<HookRunsResponse>(`/hooks/runs?limit=${limit}`);
}

/** 查询工具注册中心（含健康状态） */
export async function fetchTools(checkHealth = false): Promise<ToolListResponse> {
  const response = await fetch(`${API_BASE}/tools?check_health=${checkHealth}`);
  if (!response.ok) throw new Error(`获取工具列表失败 (${response.status})`);
  return (await response.json()) as ToolListResponse;
}

/** 健康检查 */
export async function checkHealth(): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/health`);
    return response.ok;
  } catch {
    return false;
  }
}

/** 服务自述：版本号 + 当前模型名（侧栏用来替代内部 thread id） */
export interface ServiceInfo {
  version: string;
  model: string;
}

/**
 * 模型名去掉日期后缀，例如 `qwen3.7-flash-2026-07-15` → `qwen3.7-flash`。
 *
 * 对用户来说「哪家的哪个档位」是有用信息，「哪一天的快照」是维护信息 ——
 * 前者放进侧栏，后者在开发者模式与健康检查里都看得到。
 */
export function shortModelName(model: string): string {
  return model.replace(/-\d{4}-\d{2}-\d{2}$/, '') || model;
}

/**
 * 模块级缓存：侧栏每次挂载都会问一次，但服务版本与模型在一次会话里不会变。
 * 缓存住之后，切会话、重渲染都只是读一个已 resolve 的 Promise，不再打网络。
 *
 * ⚠️ 「一次会话里不会变」这个前提**已经被语言模型的运行时切换打破**：
 * 界面上换掉默认模型之后，这里缓存的就是旧答案。所以必须有 `invalidateServiceInfo()`
 * —— 换模型的调用方负责把缓存清掉（见 `agentStore.loadServiceInfo(true)`）。
 */
let serviceInfoPromise: Promise<ServiceInfo> | null = null;

/** 丢掉服务信息缓存，让下一次 `fetchServiceInfo()` 真的去问后端 */
export function invalidateServiceInfo(): void {
  serviceInfoPromise = null;
}

export async function fetchServiceInfo(): Promise<ServiceInfo> {
  if (!serviceInfoPromise) {
    serviceInfoPromise = (async () => {
      // 走 /info 而不是 /health：后者要逐个探活依赖，依赖挂掉时要等十几秒，
      // 而侧栏要的只是「我是什么」。见后端 `app/api/health.py::service_info`。
      const response = await fetch(`${API_BASE}/info`);
      if (!response.ok) throw new Error(`获取服务信息失败 (${response.status})`);
      const data = (await response.json()) as {
        version?: string;
        model?: string;
      };
      return {
        version: data.version ?? '',
        model: data.model ?? '',
      };
    })().catch((e) => {
      // 失败不能把 Promise 缓存成「永远失败」—— 后端稍后起来了还得能拿到
      serviceInfoPromise = null;
      throw e;
    });
  }
  return serviceInfoPromise;
}

/**
 * 模型清单（顶栏「模型」下拉的候选）。
 *
 * 后端**未配置 `LLM_AVAILABLE_MODELS` 时返回空清单** —— 前端据此不显示选择器，
 * 而不是显示一个只有一项、点了也没变化的控件（「能自动的不要问」）。
 */
export async function fetchModels(): Promise<ModelsResponse> {
  return getJson<ModelsResponse>('/models');
}

// ────────────────────────────────────────────────
// 语音（Phase 4b）
// ────────────────────────────────────────────────

export interface TranscribeResponse {
  text: string;
  empty: boolean;
}

/** 语音转文字：上传 16kHz 单声道 WAV，返回识别文本 */
export async function transcribeAudio(blob: Blob, signal?: AbortSignal): Promise<TranscribeResponse> {
  const form = new FormData();
  form.append('file', blob, 'recording.wav');

  const request = new AbortController();
  let timedOut = false;
  const abort = () => request.abort();
  signal?.addEventListener('abort', abort, { once: true });
  if (signal?.aborted) request.abort();
  const timeout = window.setTimeout(() => { timedOut = true; request.abort(); }, 30000);
  try {
    const response = await fetch(`${API_BASE}/voice/transcribe`, {
      method: 'POST', body: form, signal: request.signal,
    });
    if (!response.ok) {
      const detail = await response.text().catch(() => '');
      throw new Error(`语音识别失败 (${response.status})${detail ? `: ${detail.slice(0, 200)}` : ''}`);
    }
    return (await response.json()) as TranscribeResponse;
  } catch (error) {
    if (timedOut) throw new Error('语音识别超时，请再试一次。');
    throw error;
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener('abort', abort);
  }
}

export interface WakeStatusResponse {
  enabled: boolean;
  available: boolean;
  model: string;
  path: string;
  reason: string | null;
  default_word: string;
  word: string;
  loaded: boolean;
}

export interface WakeCheckResponse {
  ok: boolean;
  word: string;
  tokens: string[];
  reason: string | null;
  keyword?: string;
}

/**
 * 唤醒 WebSocket 地址。
 *
 * 开发态 `API_BASE=/api` → 走当前页面的 host（Vite 代理需要 `ws:true`）；
 * 桌面生产态 `API_BASE=http://127.0.0.1:8000/api` → 直接换协议，
 * 不从 `file://` 去拼相对地址。
 */
export function wakeSocketUrl(): string {
  const base = API_BASE.replace(/\/+$/, '');
  if (/^https?:/i.test(base)) return `${base.replace(/^http/i, 'ws')}/voice/wake`;
  const protocol = typeof window !== 'undefined' && window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = typeof window !== 'undefined' ? window.location.host : '127.0.0.1:8000';
  return `${protocol}//${host}${base}/voice/wake`;
}

export async function fetchWakeStatus(): Promise<WakeStatusResponse> {
  const request = new AbortController();
  const timeout = window.setTimeout(() => request.abort(), 5000);
  try {
    const response = await fetch(`${API_BASE}/voice/wake/status`, { cache: 'no-store', signal: request.signal });
    if (!response.ok) throw new Error(`唤醒状态读取失败 (${response.status})`);
    return (await response.json()) as WakeStatusResponse;
  } finally {
    window.clearTimeout(timeout);
  }
}

/** 校验/归一化唤醒词；非法词也是 200，看 `ok` 与 `reason` */
export async function checkWakeWord(word: string): Promise<WakeCheckResponse> {
  const response = await fetch(`${API_BASE}/voice/wake/check`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ word }),
  });
  if (!response.ok) throw new Error(`唤醒词校验失败 (${response.status})`);
  return (await response.json()) as WakeCheckResponse;
}

/** 文字转语音：返回音频 Blob（wav/mp3），供 new Audio(URL.createObjectURL) 播放 */
export async function synthesizeSpeech(text: string): Promise<Blob> {
  const response = await fetch(`${API_BASE}/voice/tts`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => '');
    throw new Error(`语音合成失败 (${response.status})${detail ? `: ${detail.slice(0, 200)}` : ''}`);
  }
  return await response.blob();
}

// ────────────────────────────────────────────────
// 可观测与评估（Phase 4）
// ────────────────────────────────────────────────

/**
 * 带后端 detail 的错误。
 *
 * ★ 为什么要留 `detail` 这个结构化字段而不是只拼进 message
 * 有些非 2xx 是**业务结果**不是故障：409「这个群已有一次协作在跑」的 body 里
 * 带着正在跑的 `active_run_id`，界面要拿它去显示「上一次还在跑（run …）」并
 * 接得上那张 run 卡片。只给一句字符串的话，调用方只能靠正则从人话里抠 id。
 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(prefix: string, status: number, detail: unknown) {
    const human = describeDetail(detail);
    super(`${prefix} (${status})${human ? `: ${human.slice(0, 200)}` : ''}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

/**
 * 把 FastAPI 的 `detail` 收成一句人话。
 *
 * ★ 这里过去是 `String(detail)`
 * `detail` 有三种形状：字符串（`HTTPException('资源不存在')`）、对象
 * （409 要带结构化结果）、pydantic 的校验错误数组。后两种经 `String()` 出来是
 * `[object Object]` —— 用户看到「请求失败 (409): [object Object]」比看到状态码
 * 更少信息，而这恰好发生在最需要看得懂的时刻。
 */
function describeDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) =>
        item && typeof item === 'object' ? String((item as { msg?: unknown }).msg ?? '') : '',
      )
      .filter(Boolean)
      .join('；');
  }
  if (detail && typeof detail === 'object') {
    const d = detail as Record<string, unknown>;
    return String(d.reason ?? d.message ?? d.error ?? '');
  }
  return '';
}

async function toApiError(prefix: string, response: Response): Promise<ApiError> {
  let detail: unknown;
  try {
    const body = (await response.json()) as { detail?: unknown };
    detail = body && typeof body === 'object' && 'detail' in body ? body.detail : body;
  } catch {
    detail = await response.text().catch(() => '');
  }
  return new ApiError(prefix, response.status, detail);
}

/**
 * 统一的 JSON 请求封装：失败时带上后端 detail，避免只看到「500」。
 *
 * `signal` 只有搜索在用 —— 输入还在变时上一个请求的结果已经没有意义，
 * 取消它比「靠一个序号丢弃结果」更直接，也顺便省掉一次多余的渲染。
 */
async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  if (signal?.aborted) throw signal.reason;
  const controller = new AbortController();
  const onAbort = () => controller.abort(signal?.reason);
  signal?.addEventListener('abort', onAbort, { once: true });
  let timedOut = false;
  // 列表和设置不能因为服务未响应而一直转圈；流式对话与写操作不走这里。
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, 30_000);
  try {
    const response = await fetch(`${API_BASE}${path}`, { signal: controller.signal });
    if (!response.ok) throw await toApiError('请求失败', response);
    return (await response.json()) as T;
  } catch (error) {
    if (timedOut) throw new Error('请求超时，请检查本地服务后重试');
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onAbort);
  }
}

// ================================================================
// 工作区（用户授权的本地目录）—— 输入框下面那颗「选择工作区」的全部后端
// ================================================================

/**
 * 绑定表里的特殊值：交给系统给这个对话窗口分配一个产物文件夹（整个对话共用一个）。
 * 与后端 `app/workdir/store.py::AUTO_BINDING` 是同一个字符串。
 */
export const AUTO_WORKDIR_ID = 'auto';

/** 全部工作区（按最近使用排序，每条带 `exists`；默认只含手动登记的目录） */
export async function fetchWorkdirs(): Promise<WorkdirListResponse> {
  return getJson<WorkdirListResponse>('/workdirs');
}

/** 对话自动分配的产物文件夹（「设置 → 数据」页用；带会话标题与占用） */
export async function fetchAutoWorkdirs(): Promise<AutoWorkdirListResponse> {
  return getJson<AutoWorkdirListResponse>('/workdirs/auto');
}

/**
 * 登记一个工作区 = 授权 Agent 读写这个目录。
 * 失败原因（目录不存在 / 已登记过 / 不是绝对路径）由后端写成人话，原样抛给界面。
 */
export async function createWorkdir(payload: {
  path: string;
  name?: string;
  icon?: string;
  color?: string;
}): Promise<WorkdirEntry> {
  const res = await sendJson<WorkdirWriteResponse>('/workdirs', 'POST', payload);
  if (!res.workdir) throw new Error('工作区创建失败');
  return res.workdir;
}

/**
 * 撤销授权（默认只删登记，磁盘上的目录与文件不动）。
 * `purge` 只对自动分配的对话文件夹有效（「设置 → 数据」那个删除按钮）——
 * 手动工作区是你自己的目录，后端永远不会删它的内容。
 */
export async function deleteWorkdir(id: string, options: { purge?: boolean } = {}): Promise<void> {
  const query = options.purge ? '?purge=true' : '';
  await sendJson<WorkdirWriteResponse>(`/workdirs/${encodeURIComponent(id)}${query}`, 'DELETE');
}

/** 目录选择器的快捷入口：主目录 / 桌面 / 文档 / 下载 / 各盘根 */
export async function fetchWorkdirRoots(): Promise<WorkdirRoot[]> {
  const res = await getJson<{ roots: WorkdirRoot[] }>('/workdirs/roots');
  return res.roots;
}

/** 列出某个目录下的子目录（只给目录名，不读任何文件内容） */
export async function browseWorkdir(path: string, query = ''): Promise<WorkdirBrowseResult> {
  const params = new URLSearchParams({ path });
  if (query) params.set('query', query);
  return getJson<WorkdirBrowseResult>(`/workdirs/browse?${params.toString()}`);
}

/** 这个会话当前绑在哪个工作区上（没绑 = 走沙箱，是正常状态不是错误） */
export async function fetchWorkdirBinding(threadId: string): Promise<WorkdirBinding> {
  return getJson<WorkdirBinding>(`/workdirs/binding/${encodeURIComponent(threadId)}`);
}

/** 把会话绑到某个工作区；`workdirId` 传空串 = 不指定工作区 */
export async function setWorkdirBinding(
  threadId: string,
  workdirId: string,
): Promise<WorkdirBinding> {
  return sendJson<WorkdirBinding>(`/workdirs/binding/${encodeURIComponent(threadId)}`, 'PUT', {
    workdir_id: workdirId,
  });
}

export interface FetchUsageOverviewOptions {
  days?: number;
  model?: string;
  threadId?: string;
}

/**
 * 用量总览：总量 + 按天曲线 + 按模型 + 按对话窗口。
 *
 * 四个视图由后端**一次扫描**同时算出（口径必须一致），所以这里只有一个接口，
 * 而不是四个各查一次。账本只读，没有删除接口 —— 前端也不许出现删除入口。
 */
export async function fetchUsageOverview(
  options: FetchUsageOverviewOptions = {},
): Promise<UsageOverviewResponse> {
  const { days = 30, model, threadId } = options;
  const params = new URLSearchParams({ days: String(days) });
  if (model) params.set('model', model);
  if (threadId) params.set('thread_id', threadId);
  return getJson<UsageOverviewResponse>(`/usage/overview?${params.toString()}`);
}

export interface FetchUsageRecordsOptions {
  limit?: number;
  offset?: number;
  days?: number;
  model?: string;
  threadId?: string;
  windowKind?: string;
}

/** 用量明细（按时间倒序翻页）。`days` 留空 = 全部历史 */
export async function fetchUsageRecords(
  options: FetchUsageRecordsOptions = {},
): Promise<UsageRecordsResponse> {
  const { limit = 50, offset = 0, days, model, threadId, windowKind } = options;
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (days) params.set('days', String(days));
  if (model) params.set('model', model);
  if (threadId) params.set('thread_id', threadId);
  if (windowKind) params.set('window_kind', windowKind);
  return getJson<UsageRecordsResponse>(`/usage/records?${params.toString()}`);
}

// ────────────────────────────────────────────────
// 长期记忆与增量索引（Phase 5）
// ────────────────────────────────────────────────

/**
 * 带请求体的 JSON 调用。
 *
 * 单独抽出来是因为**错误信息必须带后端 detail**：
 * 记忆接口会用 400/404 表达「value 为空」「key 不存在」这类业务失败，
 * 只抛「请求失败 (404)」会让控制台完全没法排查。
 */
async function sendJson<T>(path: string, method: string, body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw await toApiError('请求失败', response);
  return (await response.json()) as T;
}

/** 全部长期记忆 + 统计 */
export async function fetchMemory(): Promise<MemoryListResponse> {
  return getJson<MemoryListResponse>('/memory');
}

/** 手动添加 / 覆盖一条偏好 */
export async function createMemory(payload: {
  value: string;
  key?: string;
  category?: string;
  confidence?: number;
  pinned?: boolean;
}): Promise<MemoryCreateResponse> {
  return sendJson<MemoryCreateResponse>('/memory', 'POST', payload);
}

/** 忘记一条（key 即 id） */
export async function deleteMemory(key: string): Promise<{ ok: boolean; deleted: string }> {
  return sendJson<{ ok: boolean; deleted: string }>(`/memory/${encodeURIComponent(key)}`, 'DELETE');
}

/**
 * 清空「她记得你」（只删关系/生活类事实，保留工作偏好）。
 *
 * 后端把这条路由声明在 `DELETE /memory/{key}` 之前，否则 `owner` 会被当成
 * 一条记忆的 key。这里用固定路径，不做任何 key 编码。
 */
export async function clearOwnerMemory(): Promise<{ ok: boolean; cleared: number }> {
  return sendJson<{ ok: boolean; cleared: number }>('/memory/owner', 'DELETE');
}

/**
 * 查看当前会注入 System Prompt 的那段文本。
 *
 * 这是记忆功能里最该被验证的一环 —— 用户关心的不是「有没有存」，
 * 而是「模型到底看到了什么、有没有被截断」。
 */
export async function fetchMemoryPromptBlock(): Promise<MemoryPromptBlockResponse> {
  return getJson<MemoryPromptBlockResponse>('/memory/prompt-block');
}

/** 编辑一条已有偏好（key 与来源不变，旧值自动进历史留痕） */
export async function updateMemory(
  key: string,
  payload: { value: string; category?: string; confidence?: number; pinned?: boolean },
): Promise<MemoryCreateResponse> {
  return sendJson<MemoryCreateResponse>(`/memory/${encodeURIComponent(key)}`, 'PUT', payload);
}

/** 恢复一条已失效（被否定）的记忆 */
export async function restoreMemory(key: string): Promise<{ ok: boolean; restored: string }> {
  return sendJson<{ ok: boolean; restored: string }>(
    `/memory/${encodeURIComponent(key)}/restore`,
    'POST',
  );
}

/** 用户再次确认旧记忆，恢复强度并延长保持时间。 */
export async function reinforceMemory(key: string): Promise<{ ok: boolean; reinforced: boolean; item: MemoryItem }> {
  return sendJson(`/memory/${encodeURIComponent(key)}/reinforce`, 'POST');
}

// ────────────────────────────────────────────────
// 助手人设 —— 与后端 app/api/persona.py 对齐
// ────────────────────────────────────────────────

/** 当前人设 + 可用语气预设 */
export async function fetchPersona(): Promise<PersonaResponse> {
  return getJson<PersonaResponse>('/persona');
}

/** 部分更新人设（下一轮回答即生效 —— system prompt 每轮重新构造） */
export async function updatePersona(
  changes: Partial<
    Pick<
      PersonaProfile,
      | 'name'
      | 'tagline'
      | 'tone'
      | 'tone_custom'
      | 'instructions'
      | 'avatar'
      | 'traits'
      | 'greeting'
      | 'examples'
      | 'enabled'
      | 'apply_globally'
      | 'preset'
      | 'address_me'
      | 'quirks'
      | 'time_awareness'
      | 'proactive'
      | 'quiet_start'
      | 'quiet_end'
      | 'proactive_min_gap_hours'
      | 'proactive_daily_max'
    >
  >,
): Promise<PersonaUpdateResponse> {
  return sendJson<PersonaUpdateResponse>('/persona', 'PUT', changes);
}

/** 恢复默认人设 */
export async function resetPersona(): Promise<PersonaUpdateResponse> {
  return sendJson<PersonaUpdateResponse>('/persona/reset', 'POST');
}

/** 查看当前会注入 System Prompt 的人设块原文（可验证性出口） */
export async function fetchPersonaFragment(): Promise<PersonaFragmentResponse> {
  return getJson<PersonaFragmentResponse>('/persona/prompt-fragment');
}

/**
 * 让助理尝试主动开口（打开助理会话时调一次）。
 *
 * `ok=false` 是正常回执（静默时段 / 刚聊过 / 今天已经说过），
 * 调用方不应把它当错误展示 —— 绝大多数情况下本来就该返回 false。
 */
export async function checkinAssistant(): Promise<AssistantCheckinResponse> {
  return sendJson<AssistantCheckinResponse>('/assistant/checkin', 'POST');
}

// ────────────────────────────────────────────────
// 外部渠道 —— 与后端 app/api/channels.py 对齐
// ────────────────────────────────────────────────

/**
 * 渠道绑定状态。
 *
 * 后端只读本地凭据，不去敲微信网关探活 —— 探一次活就会和通道 worker 抢
 * 长轮询游标，用户的那条消息被这个「想知道红点颜色」的请求吃掉。
 * 所以这里拿到的是「有没有绑定」，不是「通道在不在跑」。
 */
export async function fetchChannels(): Promise<ChannelsResponse> {
  return getJson<ChannelsResponse>('/channels');
}

// ────────────────────────────────────────────────
// 微信扫码登录（一次会话：起码 → 读状态 → 交验证码 → 作废）
//
// 轮询在后端做：网关那条 `get_qrcode_status` 本身是 35s 长轮询，前端要是每 1.5s
// 直接问网关，会叠出二十几条并发长轮询问同一张码。所以下面的 GET 读的是后端
// 内存里的快照 —— 刷多快都不花钱。
// ────────────────────────────────────────────────

/** 开一次扫码登录；已经在等扫时后端把当前那张码原样还回来（换码=让用户重扫） */
export async function startWechatLogin(): Promise<WechatLoginState> {
  const res = await sendJson<{ state: WechatLoginState }>('/channels/wechat/login', 'POST');
  return res.state;
}

export async function fetchWechatLogin(): Promise<WechatLoginState> {
  const res = await getJson<{ state: WechatLoginState }>('/channels/wechat/login');
  return res.state;
}

/** 手机上弹出数字验证码时把它交给后端那条轮询 */
export async function submitWechatVerifyCode(code: string): Promise<WechatLoginState> {
  const res = await sendJson<{ state: WechatLoginState }>('/channels/wechat/login/verify', 'POST', {
    code,
  });
  return res.state;
}

/** 关窗即作废。不抛错 —— 用户只是关了个弹窗，不该被后端的抖动挡一下 */
export async function cancelWechatLogin(): Promise<void> {
  await sendJson<{ state: WechatLoginState }>('/channels/wechat/login', 'DELETE').catch(
    () => undefined,
  );
}

// ────────────────────────────────────────────────
// 智能体注册表 —— 与后端 app/api/agents.py 对齐
// ────────────────────────────────────────────────

/**
 * 全部专员 + 运行时表现。
 *
 * 统计是后端算好随响应回的（口径在这里一处定死）：前端拉任务自己聚合会
 * 把「哪个 scope、多少条」的口径散到两端，哪天任务加状态就算出个看着没错的错数字。
 */
export async function fetchAgents(): Promise<AgentsResponse> {
  return getJson<AgentsResponse>('/agents');
}

/** 新建自定义专员 */
export async function createAgent(payload: AgentPayload): Promise<AgentWriteResponse> {
  return sendJson<AgentWriteResponse>('/agents', 'POST', payload);
}

/** 部分更新（未提及的字段保持原样）。下一轮对话即生效 —— system prompt 每轮重建 */
export async function updateAgent(name: string, changes: AgentPayload): Promise<AgentWriteResponse> {
  return sendJson<AgentWriteResponse>(`/agents/${encodeURIComponent(name)}`, 'PUT', changes);
}

/** 删除自定义专员（内置的会被后端拒，错误文案直接拿来显示） */
export async function deleteAgent(name: string): Promise<{ ok: boolean }> {
  return sendJson<{ ok: boolean }>(`/agents/${encodeURIComponent(name)}`, 'DELETE');
}

/** 恢复默认：内置的退回代码种子，自定义的等价于删除 */
export async function resetAgent(name: string): Promise<AgentWriteResponse> {
  return sendJson<AgentWriteResponse>(`/agents/${encodeURIComponent(name)}/reset`, 'POST');
}

/** 这个专员实际会被拼进 System Prompt 的身份段原文（可验证性出口） */
export async function fetchAgentPrompt(name: string): Promise<AgentPromptResponse> {
  return getJson<AgentPromptResponse>(`/agents/${encodeURIComponent(name)}/prompt`);
}

/** 索引状态（清单 + 监听 + 两个存储的实际块数） */
export async function fetchIndexStatus(): Promise<IndexStatus> {
  return getJson<IndexStatus>('/index/status');
}

/** 触发一次增量扫描。dryRun=true 时只返回变更计划，不写库、不调 Embedding */
export async function scanIndex(dryRun = false): Promise<ScanPlan | ScanReport> {
  return sendJson<ScanPlan | ScanReport>(`/index/scan?dry_run=${dryRun ? 'true' : 'false'}`, 'POST');
}

/** 知识库已入库文档清单（指纹清单逐条） */
export async function fetchIndexDocuments(): Promise<{ documents: IndexDocument[] }> {
  return getJson<{ documents: IndexDocument[] }>('/index/documents');
}

/** 从向量库、全文索引和清单里删掉一个来源 */
export async function deleteIndexDocument(
  source: string,
): Promise<{ ok: boolean; source: string; known: boolean; milvus: number; es: number }> {
  return sendJson(`/index/documents/${encodeURIComponent(source)}`, 'DELETE');
}

// ────────────────────────────────────────────────
// 会话历史（Phase 7）—— 与后端 app/api/conversations.py 对齐
// ────────────────────────────────────────────────

/** 会话列表（最近聊的在最前）。恒为普通对话那一侧 —— 助理是固定单例，不进列表 */
export async function fetchConversations(
  options: { limit?: number; offset?: number } = {},
): Promise<ConversationListResponse> {
  const { limit = 50, offset = 0 } = options;
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset), scope: 'main' });
  return getJson<ConversationListResponse>(`/conversations?${params.toString()}`);
}

/** 一个会话的全部轮次（前端据此重放出当时的界面） */
export async function fetchConversation(threadId: string): Promise<ConversationDetailResponse> {
  return getJson<ConversationDetailResponse>(`/conversations/${encodeURIComponent(threadId)}`);
}

/**
 * 确保该会话的**模型上下文**可用（必要时后端会从历史播种）。
 *
 * 打开历史会话时顺手调一次：正常情况它什么都不做并回答「上下文仍在线」；
 * 只有在 Checkpointer 真的丢了状态时才会播种 —— 于是「这次恢复到底做没做」
 * 在界面上是可见的，而不是让模型默默失忆。
 */
export async function resumeConversation(threadId: string): Promise<ResumeResponse> {
  return sendJson<ResumeResponse>(`/conversations/${encodeURIComponent(threadId)}/resume`, 'POST');
}

/**
 * 这个会话**产出过**的文件 + 现算的磁盘状态（大小 / 是否还在）。
 *
 * 只在用户真的要下载时调（展开「本次产出」块）—— 每轮回答之后都调一次
 * 是纯浪费：绝大多数回答并不产出文件。
 */
export async function fetchConversationFiles(threadId: string): Promise<ConversationFilesResponse> {
  return getJson<ConversationFilesResponse>(`/conversations/${encodeURIComponent(threadId)}/files`);
}

/** 单个产出文件的下载地址（直接给 `<a download>` 用，不走 fetch） */
export function conversationFileUrl(threadId: string, path: string): string {
  const params = new URLSearchParams({ path });
  return `${API_BASE}/conversations/${encodeURIComponent(threadId)}/files/download?${params.toString()}`;
}

export function conversationFileInlineUrl(threadId: string, path: string): string {
  const params = new URLSearchParams({ path });
  return `${API_BASE}/conversations/${encodeURIComponent(threadId)}/files/inline?${params.toString()}`;
}

/**
 * 读取一个产出文件的**文本内容**，供画布（Artifacts）直接渲染。
 *
 * 与下载接口共用同一道路径校验（只认本会话产出过的文件），
 * 区别只在于它返回文本而不是文件流 —— 前端要的是能渲染的内容，不是一份下载。
 */
export async function fetchConversationFileContent(
  threadId: string,
  path: string,
): Promise<ConversationFileContent> {
  const params = new URLSearchParams({ path });
  return getJson<ConversationFileContent>(
    `/conversations/${encodeURIComponent(threadId)}/files/content?${params.toString()}`,
  );
}

/** 打包下载地址（浏览器直接触发下载，不需要前端持有 zip 内容） */
export function conversationFilesZipUrl(threadId: string): string {
  return `${API_BASE}/conversations/${encodeURIComponent(threadId)}/files/zip`;
}

/** 某个历史版本的下载地址（只读，不改变磁盘上的当前文件） */
export function conversationFileRevisionUrl(
  threadId: string,
  path: string,
  revisionId: string,
): string {
  const params = new URLSearchParams({ path, revision_id: revisionId });
  return `${API_BASE}/conversations/${encodeURIComponent(threadId)}/files/revisions/download?${params.toString()}`;
}

/** 把历史版本写回当前文件（版本库会给它记一条 restored_from） */
export async function restoreConversationFileRevision(
  threadId: string,
  path: string,
  revisionId: string,
): Promise<void> {
  await sendJson(
    `/conversations/${encodeURIComponent(threadId)}/files/revisions/restore`,
    'POST',
    { path, revision_id: revisionId },
  );
}

/** 附件原件下载地址（附件按 24h 过期） */
export function attachmentFileUrl(attachmentId: string): string {
  return `${API_BASE}/files/attach/${encodeURIComponent(attachmentId)}`;
}

/**
 * 下载附件原件。
 *
 * 走 fetch 而不是 `<a href>`：附件是 24h 临时物，过期后接口返回 404，
 * 直接点链接只会看到一屏裸 JSON。这里把它变成一句能读的提示。
 */
export async function downloadAttachmentFile(
  attachmentId: string,
  filename: string,
): Promise<void> {
  const response = await fetch(attachmentFileUrl(attachmentId));
  if (!response.ok) throw new Error('附件原件已过期或不存在');
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename || 'attachment';
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/** 改会话标题 */
export async function renameConversation(
  threadId: string,
  title: string,
): Promise<{ ok: boolean; conversation: ConversationSummary }> {
  return sendJson(`/conversations/${encodeURIComponent(threadId)}`, 'PATCH', { title });
}

/**
 * 从一个会话的前 N 轮分支出新会话（「从这里分支」）。
 *
 * 复制发生在服务端（轮次文件原样拷贝）—— 前端不重发消息，
 * 否则等于让模型把整段对话重跑一遍。
 */
export async function forkConversation(
  threadId: string,
  turns: number,
): Promise<{ ok: boolean; conversation: ConversationSummary }> {
  return sendJson(`/conversations/${encodeURIComponent(threadId)}/fork`, 'POST', { turns });
}

/**
 * 删除一轮（0 基，一问一答算一轮）。
 *
 * `conversation` 为 null 表示轮次已删光、会话本身也被删掉了 ——
 * 调用方据此切到新会话，不能继续停在一个已不存在的 thread 上。
 */
export async function deleteConversationTurn(
  threadId: string,
  turnIndex: number,
): Promise<{
  ok: boolean;
  conversation: ConversationSummary | null;
  /** 模型上下文这边做了什么（摘除条数 / 是否按剩余轮次重建） */
  context?: { removed?: number; reseeded?: boolean; reason?: string };
  /** 最后一轮删掉时会话下线，连带清理的后台任务数 */
  tasks_deleted?: number | null;
}> {
  return sendJson(
    `/conversations/${encodeURIComponent(threadId)}/turns/${turnIndex}`,
    'DELETE',
  );
}

/** 删除一个会话（默认连服务端上下文一起删） */
export async function deleteConversation(
  threadId: string,
  options: { dropContext?: boolean } = {},
): Promise<{ ok: boolean; deleted: string; context_deleted: boolean | null }> {
  const { dropContext = true } = options;
  const params = new URLSearchParams({ drop_context: dropContext ? 'true' : 'false' });
  return sendJson(`/conversations/${encodeURIComponent(threadId)}?${params.toString()}`, 'DELETE');
}

/** 清空会话历史（不动长期记忆与审批留痕）。侧栏只清普通对话，助理单例另走 deleteConversation */
export async function clearConversations(
  scope: 'all' | 'main' = 'main',
): Promise<{ ok: boolean; cleared: number }> {
  return sendJson(`/conversations?scope=${scope}`, 'DELETE');
}

/**
 * 跨会话搜「对话正文」（用户输入 + 助手回答）。
 *
 * ★ 搜正文而不是标题
 *
 * 标题是派生的（多半还是自动生成的），用户记不住它写了什么；
 * 用户记得住的是**自己说过的那句话**。「那条问报销的对话」——
 * 「报销」在正文里，而不是在标题里。
 *
 * 空关键词后端直接返回空结果（不会返回全部会话），前端因此也不必拦这一下。
 */
export async function searchConversations(
  query: string,
  options: { limit?: number; signal?: AbortSignal } = {},
): Promise<ConversationSearchResponse> {
  const { limit = 30, signal } = options;
  const params = new URLSearchParams({ q: query, limit: String(limit), scope: 'main' });
  return getJson<ConversationSearchResponse>(`/conversations/search?${params.toString()}`, signal);
}

// ────────────────────────────────────────────────
// 检索模型（向量召回 / 精排）—— 与后端 app/api/rag.py 对齐
// ────────────────────────────────────────────────

/** 当前检索链路用的两个模型 + 本机可选的本地模型 + 向量库一致性 */
export async function fetchRagModels(): Promise<RagModelsResponse> {
  return getJson<RagModelsResponse>('/rag/models');
}

/**
 * 切换检索/精排模型（部分更新 —— 只传要改的字段）。
 *
 * 参数类型刻意写成开放的 `Record`，而不是再列一遍键名：
 * **可改哪些键由后端白名单决定**（`app/config.py` 的 `RUNTIME_OVERRIDE_SPEC`），
 * 前端再抄一份就会漂移 —— 而后端的白名单是安全边界（覆写文件不能改任意配置），
 * 不该有两处定义。非法键会被 400 拒绝，消息里带着可改清单。
 *
 * 返回的是**切换之后**的完整状态，外加 `applied`（实际写入了哪些键）
 * 与 `warnings`（可疑但合法的配置）。用后端返回的状态直接替换本地状态，
 * 而不是自己拼一遍 —— 面板上显示的必须就是服务端正在用的。
 */
export async function updateRagModels(
  patch: Record<string, string | number | boolean>,
): Promise<RagModelsResponse> {
  return sendJson<RagModelsResponse>('/rag/models', 'PUT', patch);
}

/**
 * 用当前配置真打一次向量化与精排。
 *
 * 这条接口**永远返回 200**：失败本身就是答案（在 `ok=false` 与 `error` 里），
 * 用 500 表达会让界面只能显示「请求失败」，把真正有用的原因丢掉。
 * 本地模型的首问会连带加载权重，所以超时给得比较宽。
 */
export async function probeRagModels(): Promise<RagProbeResult> {
  return sendJson<RagProbeResult>('/rag/probe', 'POST');
}

// ────────────────────────────────────────────────
// 语音模型设置 —— 与后端 app/api/voice.py 的 /settings 对齐
// ────────────────────────────────────────────────

/** 当前生效的语音配置 + 覆写现状 */
export async function fetchVoiceSettings(): Promise<VoiceSettingsResponse> {
  return getJson<VoiceSettingsResponse>('/voice/settings');
}

/** 部分更新语音配置；落盘即生效（引擎/识别器单例在后端已重置） */
export async function updateVoiceSettings(
  patch: Partial<Pick<VoiceSettingsResponse['config'], 'VOICE_STT_BACKEND' | 'VOICE_STT_MODEL' | 'VOICE_TTS_BACKEND' | 'VOICE_TTS_MODEL' | 'VOICE_TTS_VOICE' | 'VOICE_TTS_VOICE_EDGE' | 'VOICE_TTS_RATE'>>,
): Promise<VoiceSettingsResponse> {
  return sendJson<VoiceSettingsResponse>('/voice/settings', 'PUT', patch);
}

/** 清空语音域的运行时覆写（不动对话/检索模型） */
export async function resetVoiceSettings(): Promise<VoiceSettingsResponse> {
  return sendJson<VoiceSettingsResponse>('/voice/settings/reset', 'POST');
}

/** 用当前配置真合成一小段语音（永远 200：失败本身就是答案） */
export async function probeVoiceSettings(): Promise<VoiceProbeResult> {
  return sendJson<VoiceProbeResult>('/voice/settings/probe', 'POST');
}

// ────────────────────────────────────────────────
// 语言模型（默认回答模型）—— 与后端 app/api/llm.py 对齐
// ────────────────────────────────────────────────

/**
 * 当前默认回答模型 + 候选清单 + 覆写现状 + 只读运行参数。
 *
 * 只读、不发模型请求 —— 网关不通时面板照样能打开（换模型这件事在最需要它的时候
 * 不能因为依赖挂了就用不了，与 `/rag/models` 同一取舍）。
 */
export async function fetchLlmModel(): Promise<LlmModelsResponse> {
  return getJson<LlmModelsResponse>('/llm/models');
}

/**
 * 切换默认回答模型（部分更新，写运行时覆写，不碰 `.env`）。
 *
 * 参数刻意写成开放的 `Record`：**可改哪些键由后端白名单决定**
 * （`app/config.py` 的 `RUNTIME_OVERRIDE_SPEC`），前端再抄一份就会漂移。
 * 非法键会被 400 拒绝，消息里带着可改清单。
 */
export async function updateLlmModel(
  patch: Record<string, string | number | boolean>,
): Promise<LlmModelsResponse> {
  return sendJson<LlmModelsResponse>('/llm/models', 'PUT', patch);
}

/**
 * 只恢复**语言模型**这一项到 `.env` 的值（不动检索模型的覆写）。
 *
 * 与「检索模型」页的同名操作互不干扰：覆写文件两个面板共用，
 * 后端按域收窄（`config.remove_runtime_overrides`）。
 */
export async function resetLlmModel(): Promise<LlmModelsResponse> {
  return sendJson<LlmModelsResponse>('/llm/models/reset', 'POST');
}

/**
 * 用当前配置真打一次模型，回报实测事实（通不通 / 多快 / 有没有正文）。
 *
 * 这条接口**永远返回 200**：失败本身就是答案（在 `ok=false` 与 `error` 里）。
 * 会真实产生一次极小的 token 消耗 —— 它证明的是「这个模型名网关认不认」，
 * 不点它就只能靠下一次提问撞出来。
 */
export async function probeLlmModel(): Promise<LlmProbeResult> {
  return sendJson<LlmProbeResult>('/llm/probe', 'POST');
}

/**
 * 用**草稿参数**测一次连通性（`POST /llm/test`）—— 「测试连接」按钮的后端。
 *
 * 与 probeLlmModel 的分工：probe 测「已保存」的配置；这里测「表单里还没保存」
 * 的参数（缺省字段由后端回退到当前生效值）。**绝不落盘** —— 这正是
 * 「Key 测试通过才允许保存」这条规则能够成立的前提。
 *
 * 永远返回 200：失败原因（401 / 模型名不认识 / 超时）在 `ok=false` 与 `error` 里。
 */
export async function testLlmModel(patch: {
  LLM_BASE_URL?: string;
  LLM_API_KEY?: string;
  LLM_MODEL?: string;
}): Promise<LlmProbeResult> {
  return sendJson<LlmProbeResult>('/llm/test', 'POST', patch);
}

/**
 * 用**草稿参数**问供应商「你家有哪些模型」（`POST /llm/models/list`）。
 *
 * 设置页「默认模型」下拉的数据源。与 testLlmModel 同一约定：永远 200，
 * 拉不到时 `ok=false` 且 `models` 为空 —— 界面回退到手填模型名，不报错。
 */
export async function fetchLlmRemoteModels(patch: {
  LLM_BASE_URL?: string;
  LLM_API_KEY?: string;
}): Promise<LlmRemoteModelsResponse> {
  return sendJson<LlmRemoteModelsResponse>('/llm/models/list', 'POST', patch);
}

// ────────────────────────────────────────────────
// 工作空间（群组 + 数字员工）—— 与后端 app/api/workspace.py 对齐
// ────────────────────────────────────────────────

/** 工作空间列表 */
export async function fetchWorkspaces(): Promise<WorkspaceListResponse> {
  return getJson<WorkspaceListResponse>('/workspace/workspaces?limit=100');
}

/** 创建工作空间（后端会自动建一个同名群组） */
export async function createWorkspace(payload: {
  name: string;
  description?: string;
}): Promise<{ ok: boolean; workspace: Workspace }> {
  return sendJson('/workspace/workspaces', 'POST', payload);
}

/** 删除工作空间（级联删群组/成员/员工/消息） */
export async function deleteWorkspace(wsId: string): Promise<{ ok: boolean; deleted: boolean }> {
  return sendJson(`/workspace/workspaces/${encodeURIComponent(wsId)}`, 'DELETE');
}

/** 群组列表（可按工作空间过滤） */
export async function fetchGroups(workspaceId = ''): Promise<GroupListResponse> {
  const q = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : '';
  return getJson<GroupListResponse>(`/workspace/groups${q}`);
}

/** 单个群组（含成员） */
export async function fetchGroup(groupId: string): Promise<{ group: Group }> {
  return getJson<{ group: Group }>(`/workspace/groups/${encodeURIComponent(groupId)}`);
}

/** 群组消息（时间正序） */
export async function fetchGroupMessages(
  groupId: string,
  options: { limit?: number; offset?: number; afterSeq?: number } = {},
): Promise<GroupMessagesResponse> {
  const { limit = 200, offset = 0, afterSeq = 0 } = options;
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  // 断线补拉游标：`messages.rowid` 是本项目唯一可比较的顺序键（`id` 是随机 hex）。
  // 带它就只回「比这个 rowid 更新的消息」，重连不必重放全量。
  if (afterSeq > 0) params.set('after_seq', String(afterSeq));
  return getJson<GroupMessagesResponse>(
    `/workspace/groups/${encodeURIComponent(groupId)}/messages?${params.toString()}`,
  );
}

/**
 * 群组消息 SSE 的 URL（`GET /workspace/groups/{id}/stream`）。
 *
 * 与 `tasksStreamUrl` 同一理由：订阅要持有 `AbortController` 并在组件卸载时中断，
 * URL 构造统一到这里，解析用 `parseSSE<GroupStreamEvent>`。
 */
export function groupStreamUrl(groupId: string): string {
  return `${API_BASE}/workspace/groups/${encodeURIComponent(groupId)}/stream`;
}

/** 触发一次编排协作（主持人拆解 → 派发成员并行 → 自动汇报 → 汇总结论） */
export async function orchestrateGroup(
  groupId: string,
  instruction: string,
): Promise<{ ok: boolean; group_id: string; run_id: string }> {
  return sendJson(`/workspace/groups/${encodeURIComponent(groupId)}/orchestrate`, 'POST', {
    instruction,
  });
}

export interface GroupMessageOptions {
  recipient_employee_id?: string;
  reply_to?: string;
  intent?: 'auto' | 'message' | 'task';
  client_id?: string;
}

export async function sendGroupMessage(groupId: string, content: string, options: GroupMessageOptions = {}): Promise<{ok: boolean; run_id: string; message_id: string}> {
  return sendJson(`/workspace/groups/${encodeURIComponent(groupId)}/messages`, 'POST', { content, ...options });
}

export async function reviewWorkspaceCard(cardId: string, accepted: boolean, reason = ''): Promise<{ok: boolean; card: import('@/types').BoardCard}> {
  return sendJson(`/workspace/cards/${encodeURIComponent(cardId)}/review`, 'POST', { accepted, reason });
}

export async function resumeWorkspaceRun(runId: string): Promise<{ok: boolean; resumed: number}> {
  return sendJson(`/workspace/runs/${encodeURIComponent(runId)}/resume`, 'POST');
}

export interface WorkspaceArtifact {
  id: string; card_id: string; name: string; carrier: string; path: string; body: string;
  summary: string; version: number; sha256: string;
}

export async function fetchWorkspaceArtifacts(runId: string): Promise<{artifacts: WorkspaceArtifact[]}> {
  return getJson(`/workspace/runs/${encodeURIComponent(runId)}/artifacts`);
}

export function workspaceArtifactUrl(id: string): string {
  return `${API_BASE}/workspace/artifacts/${encodeURIComponent(id)}/download`;
}

export interface WorkspaceMemory {scope: 'project' | 'employee'; key: string; value: string; evidence: string; updated_at: string}
export async function fetchWorkspaceMemories(employeeId: string): Promise<{memories: WorkspaceMemory[]}> {
  return getJson(`/workspace/employees/${encodeURIComponent(employeeId)}/memories`);
}
export async function saveWorkspaceMemory(employeeId: string, memory: Pick<WorkspaceMemory, 'scope' | 'key' | 'value' | 'evidence'>): Promise<{ok: boolean}> {
  return sendJson(`/workspace/employees/${encodeURIComponent(employeeId)}/memories`, 'PUT', memory);
}

export async function deleteWorkspaceMemory(employeeId: string, key: string, scope: 'project' | 'employee'): Promise<{ok: boolean}> {
  return sendJson(`/workspace/employees/${encodeURIComponent(employeeId)}/memories?key=${encodeURIComponent(key)}&scope=${scope}`, 'DELETE');
}

/**
 * 取消一次协作。
 *
 * `ok: false` 不是错误，是「这次协作已经结束了」（completed / failed / expired）——
 * 后端带着人话的 reason 回来，界面照着说，不该弹一个看起来像失败的失败。
 */
export async function cancelRun(
  runId: string,
): Promise<{ ok: boolean; run_id?: string; reason?: string; run?: unknown }> {
  return sendJson(`/workspace/runs/${encodeURIComponent(runId)}/cancel`, 'POST');
}

/** 把一名员工拉进群组 */
export async function addGroupMember(
  groupId: string,
  employeeId: string,
  role = 'member',
): Promise<{ ok: boolean }> {
  return sendJson(`/workspace/groups/${encodeURIComponent(groupId)}/members`, 'POST', {
    employee_id: employeeId,
    role,
  });
}

/** 把一名员工移出群组 */
export async function removeGroupMember(
  groupId: string,
  employeeId: string,
): Promise<{ ok: boolean }> {
  return sendJson(
    `/workspace/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(employeeId)}`,
    'DELETE',
  );
}

/** 员工列表（可按工作空间过滤） */
export async function fetchEmployees(workspaceId = ''): Promise<EmployeeListResponse> {
  const q = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : '';
  return getJson<EmployeeListResponse>(`/workspace/employees${q}`);
}

/** 单个员工 */
export async function fetchEmployee(employeeId: string): Promise<{ employee: Employee }> {
  return getJson<{ employee: Employee }>(`/workspace/employees/${encodeURIComponent(employeeId)}`);
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

/** 创建数字员工 */
export async function createEmployee(
  payload: EmployeePayload,
): Promise<{ ok: boolean; employee: Employee }> {
  return sendJson('/workspace/employees', 'POST', payload);
}

/** 局部更新数字员工（未提及的字段保持原样） */
export async function updateEmployee(
  employeeId: string,
  payload: Partial<Omit<EmployeePayload, 'workspace_id'>>,
): Promise<{ ok: boolean; employee: Employee }> {
  return sendJson(`/workspace/employees/${encodeURIComponent(employeeId)}`, 'PATCH', payload);
}

/** 删除数字员工 */
export async function deleteEmployee(employeeId: string): Promise<{ ok: boolean; deleted: boolean }> {
  return sendJson(`/workspace/employees/${encodeURIComponent(employeeId)}`, 'DELETE');
}

/** 员工私聊的 thread_id（并确保映射已登记） */
export async function fetchEmployeeThread(
  employeeId: string,
): Promise<{ thread_id: string }> {
  return getJson<{ thread_id: string }>(`/workspace/employees/${encodeURIComponent(employeeId)}/thread`);
}

export interface EmployeeWorksOptions {
  limit?: number;
  offset?: number;
  progressLimit?: number;
}

/**
 * 这名员工**为团队干过的活**（进度、工具调用、结果全文）。
 *
 * ★ 数据只在私聊这一屏出现：群聊时间线里只有他自己打模型产出的短句摘要。
 */
export async function fetchEmployeeWorks(
  employeeId: string,
  options: EmployeeWorksOptions = {},
): Promise<EmployeeWorksResponse> {
  const { limit = 20, offset = 0, progressLimit = 50 } = options;
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
    progress_limit: String(progressLimit),
  });
  return getJson<EmployeeWorksResponse>(
    `/workspace/employees/${encodeURIComponent(employeeId)}/works?${params.toString()}`,
  );
}

export interface EmployeeChatOptions {
  message: string;
  threadId: string;
  web?: boolean;
  graph?: boolean;
  model?: string;
  thinking?: boolean;
  signal?: AbortSignal;
}

/**
 * 员工私聊 SSE —— 与主对话流同构（token / tool_call / interrupt / done…），
 * 复用 `AgentEvent` 类型与 `parseSSE`，前端渲染逻辑也能共用同一套组件。
 *
 * ★ 这条通道**只对用户可见**：后端把原始流落 `emp_<id>` 线程，绝不写群组消息。
 */
export async function* employeeChatStream(
  employeeId: string,
  options: EmployeeChatOptions,
): AsyncGenerator<AgentEvent, void, void> {
  const { message, threadId, web, graph, model, thinking, signal } = options;
  const response = await fetch(`${API_BASE}/workspace/employees/${encodeURIComponent(employeeId)}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      thread_id: threadId,
      ...(web === undefined ? {} : { web }),
      ...(graph === undefined ? {} : { graph }),
      ...(model ? { model } : {}),
      ...(thinking === undefined ? {} : { thinking }),
    }),
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => '');
    throw new Error(`员工请求失败 (${response.status})${detail ? `: ${detail.slice(0, 200)}` : ''}`);
  }
  yield* parseSSE(response);
}

/** 工具白名单勾选用的清单（`GET /workspace/tools`） */
export async function fetchWorkspaceTools(): Promise<WorkspaceToolsResponse> {
  return getJson<WorkspaceToolsResponse>('/workspace/tools');
}

/**
 * 项目知识边界（ADR 0022）。
 *
 * ⚠️ `prefixes` 与索引侧的 `source` 命名同源：`<监听目录名>/<相对路径>`。
 * 界面上要照这个形状给提示，否则用户会填一个绝对路径进来 —— 那永远不会命中。
 */
export async function fetchKnowledgeScope(): Promise<KnowledgeScopeResponse> {
  return getJson<KnowledgeScopeResponse>('/workspace/knowledge-scope');
}

/** 整体替换一个项目的边界；后端校验失败（前缀过长/条数超上限）会抛带文案的 Error */
export async function putKnowledgeScope(
  workspaceId: string,
  prefixes: string[],
): Promise<{ ok: boolean; prefixes: string[] }> {
  return sendJson(`/workspace/workspaces/${encodeURIComponent(workspaceId)}/knowledge-scope`, 'PUT', {
    prefixes,
  });
}

/** 撤掉一个项目的全部声明（= 该项目的员工回到「看得见全部」） */
export async function deleteKnowledgeScope(workspaceId: string): Promise<{ ok: boolean }> {
  return sendJson(`/workspace/workspaces/${encodeURIComponent(workspaceId)}/knowledge-scope`, 'DELETE');
}

export async function saveCanvasFile(threadId: string, path: string, content: string, original: string): Promise<void> {
  await sendJson(`/conversations/${encodeURIComponent(threadId)}/files/save`, 'POST', { path, content, original });
}
export async function convertCanvasFile(threadId: string, path: string, format: string): Promise<void> {
  const response = await fetch(`${API_BASE}/conversations/${encodeURIComponent(threadId)}/files/convert?${new URLSearchParams({ path, format })}`);
  if (!response.ok) { const error = await response.json(); throw new Error(error.detail || '转换失败'); }
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = path.split(/[\\/]/).pop()!.replace(/\.[^.]+$/, '') + '.' + format;
  anchor.click(); window.setTimeout(() => URL.revokeObjectURL(url), 60000);
}
