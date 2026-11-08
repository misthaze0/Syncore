/**
 * 全量 Agent 事件类型定义
 *
 * 与后端 `app/agents/graph.py` 的 `get_agent_event_stream` 产出的事件一一对应，
 * 是前后端 SSE 协议的唯一契约来源。
 */

// ────────────────────────────────────────────────
// 基础类型
// ────────────────────────────────────────────────

/** 工具危险等级：danger 会触发 HITL 人工审批 */
export type DangerLevel = 'safe' | 'moderate' | 'danger';

/**
 * 该条来源是靠哪一路召回命中的
 * - hybrid: 多路都命中（语义 / 关键词 / 图 同时认为相关，信号最强）
 * - vector: 只被语义检索命中
 * - bm25:   只被关键词检索命中（多见于编号 / 配置项名这类字面匹配）
 * - graph:  只被图路命中（多跳证据：查询与它零字面重合，只能沿关系链走到）
 */
export type RetrievalLane = 'hybrid' | 'vector' | 'bm25' | 'graph' | 'unknown';

/** 检索来源文档（Phase 3 起携带混合检索与精排的可观测字段） */
export interface SourceDoc {
  source: string;
  page: number;
  /** 最终排序分：有精排时是精排分，否则是 RRF 分或该路原始分 */
  score: number;
  /** 召回方式 */
  retrieval?: RetrievalLane;
  /** 命中的路名列表，如 ['vector', 'bm25'] */
  recalled_by?: string[];
  /** RRF 融合分（仅混合检索模式有） */
  rrf_score?: number | null;
  /** Cross-Encoder 精排分（未启用精排时为 null） */
  rerank_score?: number | null;
  /** 在各路中的排名（1 起） */
  vector_rank?: number | null;
  bm25_rank?: number | null;
  /**
   * 图路排名（1 起）。`null` = 这条不是靠图路召回的
   */
  graph_rank?: number | null;
  /**
   * 从查询命中的实体走到本块所需的**最小跳数**。
   * - `0` = 本块直接提到了查询里的实体
   * - `>= 1` = 只能顺着关系链走到（这就是「多跳证据」）
   * - `null` = 该条不是图路召回
   */
  graph_hops?: number | null;
  /** 走过来的实体链，如 ['星图模块', 'MOD-8801', '四十五秒'] */
  graph_path?: string[];
  /** 经过的关系名（与 graph_path 对齐：第 i 条关系连接 path[i] 与 path[i+1]） */
  graph_relations?: string[];
  /**
   * 该条是「多跳证据保底槽」补进来的。
   *
   * 为 `true` 意味着它在 RRF 里拿不到共识票（只被图路一路命中），
   * 是被 `GRAPH_MIN_MULTIHOP` 主动补到末尾的 —— 展示时要说明这一点，
   * 否则用户会以为它的排序分本该更低。
   */
  backfilled?: boolean;
  /** 片段预览 */
  preview?: string;
}

/** 工具调用状态 */
export type ToolCallStatus = 'pending' | 'awaiting_approval' | 'success' | 'failed' | 'rejected';

/** 工具调用记录（前端可视化用） */
export interface ToolCallInfo {
  callId: string;
  toolName: string;
  /** 面向用户的中文名（后端下发）；缺省时前端查工具台账 */
  toolLabel?: string;
  args: Record<string, unknown>;
  status: ToolCallStatus;
  dangerLevel: DangerLevel;
  result?: string;
  durationMs?: number;
  reason?: string;
}

/** 待审批操作 */
export interface PendingApproval {
  requestId?: string;
  grantAllowed?: boolean;
  grantMaxUses?: number;
  grantTtlSeconds?: number;
  expiresAt?: number;
  callId: string;
  toolName: string;
  /** 面向用户的中文名（后端下发） */
  toolLabel?: string;
  args: Record<string, unknown>;
  reason: string;
  /**
   * ★ 定位这次请求的四元组里，除 (threadId, callId) 之外的两维（护栏 6）。
   *
   * 提交时必须原样回传：后端只在四维全等时才唤醒等待者，
   * 少一维就返回「未匹配」（宁可弹窗报已过期，也不让甲的批准放行乙的危险操作）。
   * 主对话里它们是空串 —— 那个场景没有「哪个员工」这一说。
   */
  runId: string;
  employeeId: string;
  /** 给人看的那句「是谁在请求」（弹窗标题的数据来源） */
  requestor: string;
}

// ────────────────────────────────────────────────
// 模型主动提问（`ask_user`）—— 与审批是两条路
//
// ★ 为什么单独一套类型而不是复用 `PendingApproval`
// 审批是「允不允许做这件危险操作」，提问是「缺一条信息，你告诉我」。
// 两者的等待、留痕、恢复载荷都不同（后端 `questions/` 与 `approval_flow`
// 是两份实现），混成一种形状会让审计答不出「这次到底是授权还是澄清」。
//
// 一套数据结构三处复用：主对话消息、群组时间线、后台任务面板。
// ────────────────────────────────────────────────

/** 一个可点击的选项 */
export interface QuestionOption {
  label: string;
  /** 这个选项意味着什么（一句话，可省略） */
  description?: string;
  /** 推荐项（每题最多一个，后端保证） */
  recommended?: boolean;
}

/** 一个问题（已由后端归一化，`id` 形如 `q1`） */
export interface QuestionSpec {
  id: string;
  /** 很短的分组标签 */
  header?: string;
  question: string;
  options: QuestionOption[];
  multi_select?: boolean;
}

/** 用户对一个问题的回答 */
export interface QuestionAnswer {
  selected: string[];
  other: string;
}

/** 整张卡片的草稿：问题 id → 回答 */
export type QuestionAnswers = Record<string, QuestionAnswer>;

/**
 * 一次提问的生命周期状态。
 *
 * `expired` 是**前端本地**的判断（重新打开历史会话时，挂着的卡片不能点）；
 * 后端只会给 pending / answered / skipped / timeout。
 */
export type QuestionState = 'pending' | 'answered' | 'skipped' | 'timeout' | 'expired';

/**
 * 一条已发出/已结束的提问（挂在消息、群组、任务面板上）。
 *
 * 与 `QuestionEvent` 的区别：事件是**通道载荷**（snake_case），
 * 这是**界面形状**（camelCase）。转换只发生在 reducer 里。
 */
export interface AskedQuestion {
  questionId: string;
  callId: string;
  questions: QuestionSpec[];
  status: QuestionState;
  answers: QuestionAnswers;
  /** 内部原因（user / skipped / timeout / cancelled），界面上只在超时时用 */
  reason?: string;
  /** 「是谁在问」—— 群组里多个员工并发提问时唯一的区分依据 */
  requestor?: string;
  /** 回传后端定位等待者用（与审批同一套四元组） */
  runId: string;
  employeeId: string;
  /**
   * 归属会话。群组路径下是群组 id（回答接口靠它定位等待者）；
   * 主对话 / 员工私聊路径为空 —— 那边的 threadId 由页面自己持有。
   */
  threadId?: string;
  /** 等待时限（秒）；没有就不显示倒计时 */
  timeoutSeconds?: number;
  /** 卡片到达前端的时刻（毫秒），倒计时用 */
  askedAt: number;
}

/** 当前正等着用户回答的那一张卡（同一时刻最多一张） */
export type PendingQuestion = AskedQuestion;

/** `POST /questions/answer` 的回执。`success=false` 是「卡已失效」，不是 HTTP 错误。 */
export interface QuestionAnswerResponse {
  success: boolean;
  call_id: string;
  question_id: string;
  status: string;
  message: string;
  record_id: string;
}

/** 聊天消息 */
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  /** 助理这一轮连续发送的独立消息；content 仍保留完整纯文本供复制、朗读。 */
  replyMessages?: string[];
  /** 该轮回答引用的知识库来源 */
  sources?: SourceDoc[];
  /** 该轮的 Agent 调用链 */
  agentChain?: string[];
  /** 该轮的工具调用记录 */
  toolCalls?: ToolCallInfo[];
  /**
   * 该轮的**推理正文**（后端切出来的，见 `DoneEvent.reasoning`）。
   *
   * 有它就把正文里的那一段挪进折叠块 —— 否则用户会读到一大段模型的英文内心独白，
   * 末尾还挂着 `</think>`。折叠（而不是丢掉）的理由与「数据链路一行都不删」一致：
   * 想核对「它为什么这么答」的人应该看得到。
   */
  reasoning?: string;
  /** 该轮识别到的意图 */
  intent?: string;
  intentConfidence?: number;
  /** 该轮写入的长期记忆回执（「已记住：…」），多条则多行 */
  memoryNotes?: string[];
  /**
   * 该轮被预授权**自动放行**的危险操作（Phase 6），多条则多行。
   *
   * 与 `toolCalls` 分开存：`toolCalls` 是"调用了什么"，
   * 这里是"其中哪几次没有经过你确认"—— 后者是安全相关的提示，
   * 需要在消息里单独、显眼地说明，不能混在工具卡片流里被划过。
   */
  autoApprovals?: AutoApprovalNote[];
  /**
   * 该轮里钩子对用户说的话（Phase 5），多条则多行。
   *
   * 与 `autoApprovals` 是同一类东西 —— 非阻塞、必须看得见、不进正文 ——
   * 但分开存：那条是「哪个危险操作没经过你点」，这条是「她的脚本想告诉你一件事」。
   * 混在一起会让用户以为钩子在报告审批结果。
   */
  hookNotices?: HookNoticeNote[];
  /**
   * 这一轮里助手向你提过的问题（`ask_user`），多条则多张卡片。
   *
   * 回答结束后卡片不消失：它要留在原地变成「你当时选了什么」——
   * 删掉的话，回看历史时那句「按你说的选了 A」就失去了依据。
   */
  askedQuestions?: AskedQuestion[];
  /** 是否正在流式输出 */
  streaming?: boolean;
  /** 错误信息 */
  error?: string;
  /**
   * 该轮**不是正常收尾**时的标记（从会话历史恢复时才可能带上）。
   *
   * 恢复历史时前端才知道「当时这一轮是怎么停的」（中断 / 出错 / 停在等你确认）。
   * 必须显示出来：否则用户看到一个半截回答，会以为是恢复过程把它截断了，
   * 而事实上当时就停在这里。
   */
  interrupted?: { status: string; label: string };
  /**
   * 这一轮产出的文件（交付物）。
   *
   * 两个来源，同一种渲染：
   * - 实时：`done` 事件带的 `files`
   * - 重放：`ConversationTurn.files`
   *
   * 只有路径与工具名（后端派生的事实）；大小/是否还在磁盘由
   * `TurnFiles` 组件在**展开时**拉一次 `/files` 补上 —— 不展开不打请求。
   */
  files?: TurnFileRef[];
  /**
   * 这条用户消息带的**对话附件**（拖拽/粘贴/回形针进来的临时文件）。
   *
   * 只有文件名与元信息 —— 附件正文是 24h 临时物，重放时已不可能取回，
   * 显示文件名是「当时所见」的下限保证（与后端轮次记录对齐）。
   */
  attachments?: AttachmentMeta[];
  /**
   * 这一轮的真实 token 用量（实时来自 `done.usage`，重放来自 `ConversationTurn.usage`）。
   * 渲染成回答下方一行小字；拿不到就整行不出现（见 `TurnUsage`）。
   */
  usage?: TurnUsage;
  /**
   * 这一轮的总耗时（毫秒）。只在实时路径上由前端自己计时 ——
   * 后端不提供「从发起到结束」的墙钟时间（它落在 trace 里，属于开发者视角）。
   * 重放的历史消息没有它，因此同样按「有没有」决定是否显示。
   */
  durationMs?: number;
  /**
   * 这一轮在会话里的序号（0 基）。
   *
   * 删除单轮要用它，而不是 `Math.floor(index / 2)`：助理主动问候会产生
   * **只有回答、没有提问**的轮次，按「用户一条 + 助理一条」的固定配对去数，
   * 一次主动问候就能让后面所有轮次的序号错位，删错记录。
   */
  turnIndex?: number;
  /** 谁先开口：assistant = 助理主动问候（该气泡没有对应的用户消息） */
  initiatedBy?: 'user' | 'assistant';
  /**
   * 这条是**后台任务完成汇报**，且收尾了 N 个任务（>0 才渲染标记）。
   *
   * 实时来自 `assistant_turn_token` / `assistant_turn` 的 `task_count`，
   * 重放来自落盘的 `task_report` 事件 —— 两条路落到同一个字段上。
   * 标记渲染在气泡顶部：顺序上「完成」在前、「总结」在后。
   */
  taskCount?: number;
  taskStatus?: 'succeeded' | 'partial' | 'failed';
}

/** 对话附件的元数据（`POST /api/files/attach`；不含正文） */
export interface AttachmentMeta {
  attachment_id: string;
  filename: string;
  file_size: number;
  /** 解析出的文本字符数（0 表示没能解析出内容） */
  chars: number;
  truncated: boolean;
  /** 原件是否还在（24h 内可下载）；历史重放没有这个字段，按「可能已过期」处理 */
  original_available?: boolean;
  message?: string;
}

/**
 * 一个已安装的技能（`GET /api/skills`）。
 *
 * 字段名保持后端的 snake_case —— 这份出参是 `app/skills.py` 的 `Skill.to_dict()`
 * 逐字产出的，前端再做一次驼峰映射只会让两边漂移。
 */
export interface SkillInfo {
  name: string;
  description: string;
  /** bundled = 随应用内置（只读）；user = 用户放在 data/skills 里的 */
  origin: 'bundled' | 'user';
  enabled: boolean;
  /** SKILL.md 能读出一份可用指令 */
  usable: boolean;
  /** 不可用的原因（空字符串 = 没问题）。有问题的技能也必须显示 */
  error: string;
  /** 不影响使用的写法提示（name 与目录名不一致、未知 frontmatter 键等） */
  warnings: string[];
  /** 被 data/skills 里的同名技能遮住，生效的是那一个 */
  shadowed: boolean;
  dir: string;
  license: string;
  compatibility: string;
  metadata: Record<string, unknown>;
  allowed_tools: string[];
  body_chars: number;
  oversized: boolean;
  /** L3 材料（相对技能目录的路径，如 `references/checklist.md`） */
  resources: string[];
  resources_truncated: boolean;
}

/** 技能详情：多出 `instructions`（后端把正文与材料清单拼成的一段文本） */
export interface SkillDetailResponse extends SkillInfo {
  instructions: string;
}

/**
 * 一个 MCP Server 的「配置 + 运行状态」（`GET /api/mcp` 的 servers 项）。
 *
 * ⚠️ 凭据是**只写的**：`env` / `headers` 的真值永远不会出现在这里，
 * 只有键名（`env_keys` / `header_keys`）。编辑时把某个值原样填成 `********`
 * 表示"这个键别动"，填新值即替换。
 */
export interface McpServer {
  name: string;
  /** 界面显示名（中文优先）；为空时显示 name */
  display_name: string;
  transport: 'stdio' | 'streamable_http' | 'sse';
  enabled: boolean;
  command: string;
  args: string[];
  cwd: string;
  env_keys: string[];
  url: string;
  header_keys: string[];
  tool_prefix: string;
  /** 实际生效的前缀（留空时后端用 `<name>_`） */
  effective_prefix: string;
  danger_level: 'safe' | 'moderate' | 'danger';
  allowed_agents: string[];
  note: string;

  // ---------- 运行状态 ----------
  connected: boolean;
  server_info: { name?: string; version?: string; protocol?: string };
  /** 远端工具名（未加前缀的原名） */
  tools: string[];
  tools_total: number;
  /** 远端工具数超过上限，清单被截断 */
  truncated: boolean;
  /** 注册进工具治理中心的本地名 */
  registered: string[];
  error: string | null;
  /** 还要等多少秒才允许自动重连（0 = 现在就能试） */
  retry_in: number;
  connected_at: number | null;
}

/** 新建 / 修改的请求体（全部可选：改哪填哪） */
export interface McpServerPayload {
  name?: string;
  /** 界面显示名（中文优先）；留空则显示 name */
  display_name?: string;
  transport?: McpServer['transport'];
  enabled?: boolean;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
  url?: string;
  headers?: Record<string, string>;
  tool_prefix?: string;
  danger_level?: McpServer['danger_level'];
  allowed_agents?: string[];
  note?: string;
}

/** 一次对账的结果：写配置之后，运行时追上了没有 */
export interface McpApplyResult {
  servers: Record<string, string>;
  tools: number;
  connected: string[];
  failed: Record<string, string>;
}

export interface McpStatusResponse {
  enabled: boolean;
  store: string;
  timeout_seconds: number;
  connect_timeout_seconds: number;
  retry_seconds: number;
  max_tools_per_server: number;
  /** 明说没做授权流：别在界面上装一个"已授权" */
  oauth: string;
  transports: string[];
  danger_levels: string[];
  servers: McpServer[];
  tools: Array<{ name: string; server: string }>;
  connected: number;
  failed: Array<{ server: string; error: string | null }>;
}

/** 一条「预授权自动放行」的回执（由 SSE `approval_auto` 事件产生） */
export interface AutoApprovalNote {
  callId: string;
  toolName: string;
  /** 面向用户的中文名（后端下发） */
  toolLabel?: string;
  grantId: string;
  /** 该预授权还可自动放行几次（0 = 这是最后一次，之后会重新弹窗） */
  remaining: number;
  reason: string;
  /** 给用户看的一句话，直接来自后端 */
  content: string;
}

/**
 * 钩子在某一轮里对用户说的一句话（Phase 5）。
 *
 * 来源是脚本 stdout 的 `message` / `systemMessage` / 纯文本首行，
 * 以及「钩子想放行但被忽略」这条系统提示。文字**全部来自后端**，
 * 前端只补一个「哪件事说的」标签（`event`），不改写内容。
 */
export interface HookNoticeNote {
  content: string;
  event: HookEvent;
  toolName?: string;
  toolLabel?: string;
}

// ────────────────────────────────────────────────
// SSE 事件联合类型
// ────────────────────────────────────────────────

export interface TokenEvent {
  type: 'token';
  content: string;
  /** 仅助理会话提供：这段文字属于第几条短消息。 */
  message_index?: number;
}

export interface AssistantMessagesEvent {
  type: 'assistant_messages';
  /** 已生成消息的快照；流式期间末项可为空，表示准备发送下一条。 */
  content: string[];
}

export interface SourcesEvent {
  type: 'sources';
  content: SourceDoc[];
}

export interface IntentEvent {
  type: 'intent';
  content: string;
  confidence: number;
}

export interface AgentSwitchEvent {
  type: 'agent_switch';
  content: string;
  /** 后端字段名 */
  from_agent?: string;
  /** 兼容简写 */
  from?: string;
}

export interface ToolCallEvent {
  type: 'tool_call';
  tool_name: string;
  tool_label?: string;
  args: Record<string, unknown>;
  call_id: string;
  danger_level?: DangerLevel;
}

export interface ToolResultEvent {
  type: 'tool_result';
  call_id: string;
  tool_name: string;
  tool_label?: string;
  result: string;
  success: boolean;
  /** 权限/审批拒绝（后端 `_refuse` 给）；普通工具失败没有这个标记 */
  denied?: boolean;
  duration_ms: number;
}

export interface InterruptEvent {
  request_id?: string;
  grant_allowed?: boolean;
  grant_max_uses?: number;
  grant_ttl_seconds?: number;
  expires_at?: number;
  type: 'interrupt';
  call_id: string;
  tool_name: string;
  tool_label?: string;
  args: Record<string, unknown>;
  reason: string;
  /**
   * ★ 护栏 6「审批不可委托」的三件（后端 `approval_flow` 一定给，主对话给空串）。
   *
   * 群组里 `thread_id` 就是群组的 id、`call_id` 是模型给的短 id（并发下会撞），
   * 所以「三条弹窗分别是谁的」只能靠这三项表达 ——
   * 少读一项，界面上就是三条一模一样的弹窗，而回传少一维会直接 unmatched。
   */
  run_id: string;
  employee_id: string;
  requestor: string;
}

/**
 * 模型主动向你提问（`ask_user` 挂起）。
 *
 * ★ 它是**独立事件**，不是给 `interrupt` 加一个 kind
 * `interrupt` 在前端的语义是「打开审批弹窗」；提问是内联的选择卡片。
 * 复用同一个事件名，前端就必须从 payload 猜「该弹哪个」，而少写一次判断
 * 的后果是把一张提问卡渲染成审批弹窗 —— 用户会以为自己要授权一次危险操作。
 *
 * ★ 回传时这四个字段一个都不能少
 * 后端按 `(thread_id, call_id, run_id, employee_id)` 精确匹配等待者，
 * 外加 `question_id` 校验卡片没有过期（同一 call_id 恢复后可能被重新发起）。
 */
export interface QuestionPayload {
  /** 这一次提问的稳定标识（回答时原样带回，旧卡片不会唤醒新提问） */
  question_id: string;
  call_id: string;
  tool_name: string;
  tool_label?: string;
  /** 已归一化：1-4 个问题，每个 2-4 个选项 */
  questions: QuestionSpec[];
  run_id: string;
  employee_id: string;
  requestor: string;
  /** 等待时限（秒）—— 超时后按跳过继续 */
  timeout_seconds?: number;
  /** 提问登记时刻（epoch 毫秒）。群组快照重画卡片时用它还原倒计时 */
  asked_at?: number;
  /** 群组路径下 = 群组 id（回答接口的定位维度）；其它路径不带 */
  thread_id?: string;
}

export interface QuestionEvent extends QuestionPayload {
  type: 'question';
}

/** 提问有了结局（回答 / 跳过 / 超时）—— 前端据此把卡片收成只读 */
export interface QuestionResolvedEvent {
  type: 'question_resolved';
  question_id: string;
  call_id: string;
  status: 'answered' | 'skipped' | 'timeout';
  answers: QuestionAnswers;
  reason?: string;
  run_id: string;
  employee_id: string;
  /** 给用户看的一句话（「已回答」/「已跳过」/「等待超时，已按跳过继续」） */
  content?: string;
}

/**
 * 预授权自动放行事件（Phase 6）
 *
 * ★ 它为什么是**独立的事件类型**，而不是给 `interrupt` 加一个 `auto_approved` 标志
 *
 * `interrupt` 在前端的语义是「有东西在等你点」—— 它会打开阻塞式弹窗。
 * 而自动放行时**没有任何东西在等**，复用 `interrupt` 就等于要求前端
 * 从标志位去猜"该不该弹窗"。少写一次判断，用户就会看到一个点了也没用的弹窗。
 * 独立类型让"不可能被误渲染成弹窗"成为结构上的保证。
 *
 * 但它必须被展示 —— 静默执行危险操作是不可接受的，
 * 所以前端渲染成**非阻塞**的提示（`ApprovalAutoNotice`）。
 */
export interface ApprovalAutoEvent {
  type: 'approval_auto';
  call_id: string;
  tool_name: string;
  tool_label?: string;
  args: Record<string, unknown>;
  reason: string;
  /** 是哪一条预授权放行的（可据此跳转到预授权列表） */
  grant_id: string;
  /** 该预授权还可自动放行几次（0 表示这是最后一次） */
  remaining: number;
  content: string;
  /** 「自动放行的是谁的操作」—— 与 `interrupt` 同口径（护栏 6） */
  run_id: string;
  employee_id: string;
  requestor: string;
}

export interface StateUpdateEvent {
  type: 'state_update';
  content: string;
  /**
   * ★ 收窄为字面量：只保留审批决策回执。
   *
   * 原本还有 `phase` / `intent` / `active_agent` 三种装饰性取值，
   * 而前端**从未消费**（agentStore 的 case 是空分支）——
   * 文档却写它驱动「前端状态条」。收窄之后，
   * 「又有人发了个没人看的 state_update」变成**编译错误**。
   *
   * 为什么不「接上它」：基础页签禁术语（产品口径），
   * 接上意味着界面显示「意图识别: rag」「当前执行: file_agent」——
   * 那是内部编排信息。该显示的是顶栏那句根据 intent 推断的人话。
   */
  state_key: 'approval';
  value: boolean;
  /** 审批决策的来源：user（人点的）/ grant（预授权放行）/ timeout（等待超时） */
  decided_by?: 'user' | 'grant' | 'timeout';
}

// ────────────────────────────────────────────────
// 后台子任务（Supervisor 架构，见 ADR 0019）
//
// ★ 进度有两条通道 + 一条兜底，**职责不同**
// - **主对话流**（`/api/chat`）：只在「用户正在发消息」时活着，推一条 `subtask`
//   （派发瞬间），让用户当场看到「我派了任务」。
// - **进度流**（`GET /api/tasks/stream`，见下面 `TaskStreamEvent`）：按会话常驻，
//   先发快照（真相源）、再发 `subtask` / `subtask_progress` / `subtask_done` 增量。
//   这是子任务「跨轮次存活」对应的**实时**通道 —— 主对话流做不到（它一轮就结束）。
// - **轮询**（`GET /api/tasks`，见 lib/api.ts::fetchTasks）：**兜底**。SSE 断线、
//   页面刷新后靠它对齐，也是 `seq` 断档时的补拉手段（SSE ≠ 真相源）。
// ────────────────────────────────────────────────

export interface SubtaskEvent {
  type: 'subtask';
  task_id: string;
  /** 内部专员名（file_agent 等）—— **开发者模式才显示**，基础界面不出现 */
  agent: string;
  /** ★ 人话任务名（8-20 字）。界面显示的始终是它，不是 agent */
  title: string;
  /** 本轮派发总数；没数据不带 */
  total?: number;
  depends_on?: string[];
}

/**
 * 一步进度的形状（REST 轮询 `GET /api/tasks` 返回的 progress 元素）。
 *
 * ★ 它**不是** SSE 事件（没有 `type` 字段）—— 它是轮询与快照里 progress 数组的元素。
 */
export interface SubtaskProgressStep {
  seq: number;
  /** ISO 时间戳（后端落盘时加） */
  at?: string;
  kind:
    | 'plan'
    | 'sources'
    | 'tool_call'
    | 'tool_result'
    | 'approval'
    | 'answer'
    | 'error'
    /** 子任务停下来向你提问（`waiting_input` 那条进度） */
    | 'question'
    /** ★ 子 Agent 在两次工具调用之间说的那段话 —— 展开后当正文段落渲染 */
    | 'narrative';
  /** ★ 人话描述，后端给的（前端不自己拼——「可数事实只有一处」） */
  label: string;
  status: 'running' | 'ok' | 'failed' | 'awaiting';
  /** 工具名 / 「命中 3 个来源」这类补充；没有时字段不存在（不是空串） */
  detail?: string;
  /**
   * ★ 仅 `narrative` 有：整段正文（可能多行）。
   * 与 `label` 分工：`label` 是收起的列表里那一行，`text` 是展开后才读的段落。
   */
  text?: string;
  /** 仅 tool_call / tool_result / approval 三类有 */
  call_id?: string;
}

// ────────────────────────────────────────────────
// 进度流（`GET /api/tasks/stream`）的事件
//
// ★ 这是**另一条流**，与主对话流（`/api/chat`）分开。
// 理由：子任务跨轮次存活、主对话 SSE 一轮就结束，两者生命周期不匹配 ——
// 进度单开一条长连接，由前端按会话持有（见后端 `app/tasks/bus.py`）。
//
// 契约：连接建立先收一条 `snapshot`（真相源 = 任务表），之后是增量；
// 断档靠 `GET /api/tasks/{id}` 补拉，**不做服务端补发**。
// ────────────────────────────────────────────────

/** 进度流：连接建立时的快照（真相源 = 任务表） */
export interface SubtaskSnapshotEvent {
  type: 'snapshot';
  thread_id: string;
  tasks: SubtaskDTO[];
}

/** 进度流：子任务推进一步（= 一步进度的形状 + 事件头） */
export interface SubtaskProgressEvent extends SubtaskProgressStep {
  type: 'subtask_progress';
  task_id: string;
}

/** 进度流：子任务到达终态 */
export interface SubtaskDoneEvent {
  type: 'subtask_done';
  task_id: string;
  status: 'succeeded' | 'failed' | 'cancelled' | 'orphaned';
  title?: string;
  result?: string;
  usage?: Record<string, number>;
  files?: Array<{ path: string; tool: string }>;
  error?: string;
}

/**
 * 进度流：后台任务失败后，后端在**主对话**里追加的一条助理消息
 * （落盘成功后才广播 —— 与「主动问候」同一条契约）。
 *
 * `task_count` 只在「完成汇报」上带：它让消息顶部挂上
 * 「N 项后台任务已完成」的完成标记，把汇报和它上面的分发对话
 * 连成一句完整的因果 —— 顺序上标记在前、总结在后。
 * 主动问候 / 失败通知不带它（保持无标记）。
 */
export interface AssistantTurnEvent {
  type: 'assistant_turn';
  turn_id: string;
  text: string;
  /** 这一轮是完成汇报，且当时有 N 个后台任务收尾（>0 才显示标记） */
  task_count?: number;
  task_status?: 'succeeded' | 'partial' | 'failed';
  /**
   * 这次收尾的任务们产出的文件（沙箱相对路径，与 `DoneEvent.files` 同形）。
   * 实时路径没有重放那一步 —— 文件卡片的渲染信息必须随终版广播走。
   */
  files?: Array<{ path: string; tool: string }>;
}

/**
 * 进度流：任务完成汇报的**流式增量**（总 Agent 正在打字）。
 *
 * ★ 与 `assistant_turn` 的分工
 * token 是渲染效果（用户看到「她正在汇报」），`assistant_turn` 是**终版**
 * （落盘成功后才广播）：前端收到它时把流式中的那条消息收尾成完整正文，
 * 断线没收到增量时它就是整条消息本身 —— 两条路都幂等（按 turn_id）。
 */
export interface AssistantTurnTokenEvent {
  type: 'assistant_turn_token';
  turn_id: string;
  content: string;
  /** 流式期间就带上：完成标记在打字开始时出现，而不是等终版才补上 */
  task_count?: number;
  task_status?: 'succeeded' | 'partial' | 'failed';
}

/**
 * 重放事件（落盘在轮次 events 里的第一条）：这一轮是**完成汇报**，
 * 顶部要挂「N 项后台任务已完成」标记。
 *
 * ★ 为什么实时路径不靠它
 * 实时时 `assistant_turn_token` / `assistant_turn` 直接带 `task_count`；
 * 但重放走的是 `turn.events`，那条广播不会重来一遍 —— 标记的信息
 * 只能从这条落盘事件里拿回来。
 */
export interface TaskReportEvent {
  type: 'task_report';
  count: number;
  status?: 'succeeded' | 'partial' | 'failed';
}

/**
 * 进度流的联合事件类型（`GET /api/tasks/stream`）。
 *
 * ★ 为什么不塞进 `AgentEvent`
 * 那是**主对话流**的形状。进度流里的 `snapshot` 与消息毫无关系，
 * 塞进去会让对话 reducer（`applyAgentEvent`）被迫认识它 —— 职责会糊。
 */
export type TaskStreamEvent =
  | SubtaskSnapshotEvent
  | SubtaskEvent
  | SubtaskProgressEvent
  | SubtaskDoneEvent
  | AssistantTurnEvent
  | { type: 'assistant_turn_cancelled'; turn_id: string }
  | AssistantTurnTokenEvent;

// ────────────────────────────────────────────────
// REST 接口：后台子任务
// ────────────────────────────────────────────────

/**
 * ★ 「查不到」与「没有」必须能被前端区分。
 *
 * `storage: "unavailable"` 时**不能**渲染成「没有任务」——
 * 那是谎报，而后端返回 unavailable 恰恰是因为它**读不到**。
 * （不变量：空列表会被当成「没有任务」，而真相是「查不了」）
 */
export interface SubtaskDTO {
  task_id: string;
  thread_id: string;
  parent_turn_id?: string;
  depends_on?: string[];
  agent: string;
  title: string;
  status: SubtaskStatus;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
  error?: string;
  result?: string;
  usage?: Record<string, number>;
  files?: Array<{ path: string; tool: string }>;
  /** ★ 超了上限才带（响应里带上它，界面才能说「只显示了最近 N 步」） */
  total_steps?: number;
  /**
   * ★ 只有 `interrupted` 才带：续跑前要做什么。
   * `checkpoint` = 直接继续；`approval` = 继续后会重新问一次；
   * `reconcile` = 最后一步可能已经生效，先核对再继续（界面要弹确认）。
   */
  resume_mode?: 'checkpoint' | 'approval' | 'reconcile';
  /** 给用户看的一句话（为什么中断 / 续跑要注意什么） */
  resume_note?: string;
  /**
   * ★ 只有 `waiting_input` 才带：模型正在等回答的那张卡片（形状 = SSE `question` 载荷）。
   * 刷新 / 换设备后靠它重画问卷 —— 只给状态不给内容，界面只能显示「等你回答」却说不出在等什么。
   */
  pending_question?: QuestionPayload;
  progress: SubtaskProgressStep[];
}

export type SubtaskStatus =
  | 'pending'
  | 'running'
  | 'waiting_approval'
  /** 停在模型提问上（等的是答案，不是审批 —— 与 waiting_approval 是两条路） */
  | 'waiting_input'
  /** 进程没了但还能接着跑 —— 不是终态，也不是「进行中」（见后端 models.py） */
  | 'interrupted'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'orphaned';

export interface SubtaskListResponse {
  storage: 'ok' | 'unavailable';
  total: number;
  limit: number;
  offset: number;
  tasks: SubtaskDTO[];
  /** storage=unavailable 时的原因；★ ok 时字段不存在 */
  storage_error?: string;
}

/**
 * 一轮对话的**真实** token 用量。
 *
 * 来源是后端 `runner` 从聚合后的 AIMessage 上取到的 `usage_metadata`
 * （不是按流式文本估的），多 Agent / 工具循环里的多次调用会累加。
 * 字段全可选：网关不返回 usage、客户端中途断连时整块缺失 ——
 * 界面据「有没有这块」决定那行小字要不要出现，而不是显示 0。
 */
export interface TurnUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  /**
   * 其中花在**内部推理**上的 token（思考型模型才有，如 qwen3.x）。
   * 已包含在 `completion_tokens` 里 —— 单独拎出来是为了解释首字延迟：
   * 「这些 token 里有一大半花在了你看不见的思考上」。
   */
  reasoning_tokens?: number;
}

export interface DoneEvent {
  type: 'done';
  content?: string;
  /**
   * 本轮**成功写入工作目录**的文件（交付物）。
   *
   * ★ 这是 `done` 事件上的一个可选字段，不是新事件类型 —— 事件流仍是唯一契约。
   * 后端由事件流里的 `tool_call` / `tool_result` 配对派生出这份清单，
   * 于是前端不必去猜「刚才那轮到底产出了什么」。
   *
   * 它只带路径与来源工具（不含大小/是否还在）：那两个是**磁盘现状**，
   * 由 `GET /api/conversations/{id}/files` 现算 —— 展开文件列表时才拉，
   * 免得每一轮回答都多打一次请求。
   */
  files?: TurnFileRef[];
  /**
   * 本轮真实 token 用量（★ 同为 `done` 上的可选字段，不是新事件类型）。
   * 与 `files` 完全同一套约定：拿不到就不带，前端据有没有它决定是否显示。
   */
  usage?: TurnUsage;
  /**
   * 模型的**推理正文**（★ 同为 `done` 上的可选字段，不是新事件类型）。
   *
   * 为什么需要它：本项目网关在带工具的调用里会把模型自己的思考混进正文一起流出来，
   * 末尾还留一个字面量 `</think>` —— 用户会读到一大段英文独白。
   * 后端在收尾时把两者切开（`app/agents/think.py`，全项目唯一定义处），
   * 于是这里拿到的是「怎么想的」，`answer` 拿到的是「答了什么」。
   *
   * 拿不到就不带（绝大多数正常回答都是），前端据此决定折叠块要不要出现。
   */
  reasoning?: string;
  /**
   * 摘掉推理之后的**干净回答**（仅在存在推理时才带）。
   *
   * 为什么要把已经流过的正文再给一遍：流式那一份是**混着推理**的，
   * 而「从哪个字符起才算回答」是后端的判断 —— 让前端再实现一遍切分规则，
   * 就等于同一个规则有两份实现，实时那一屏与重放那一屏迟早会不一致。
   */
  answer?: string;
}

/** 一轮产出的文件（引用形态：只记它是怎么来的） */
export interface TurnFileRef {
  /** 相对工作目录的路径，如 `周报.docx` */
  path: string;
  /** 产出它的工具：write / edit / generate_document */
  tool: string;
}

/** 一个产物文件的某个历史版本（`GET /api/conversations/{id}/files` 的 revisions） */
export interface ArtifactRevision {
  revision_id: string;
  created_at: string;
  size: number;
  sha256: string;
  tool: string;
  turn_id: string;
  /** 由「恢复」产生的版本会指向被恢复的版本 id */
  restored_from?: string;
}

/** 产出文件的**磁盘现状**（`GET /api/conversations/{id}/files`） */
export interface ConversationFile {
  path: string;
  name: string;
  /** 扩展名（不含点），用于挑图标，如 docx / xlsx / pptx / md */
  suffix: string;
  tool: string;
  turn_id: string;
  created_at: string;
  /** 文件是否还在磁盘上。false = 显示成「已不在了」，不给可点的下载按钮 */
  exists: boolean;
  size: number;
  /** 内容变过的历史版本，最新的在最后；这个功能上线前生成的文件为空 */
  revisions?: ArtifactRevision[];
}

export interface ConversationFilesResponse {
  success: boolean;
  thread_id: string;
  total: number;
  /** 其中还存在于磁盘上的条数 */
  available: number;
  files: ConversationFile[];
}

/**
 * 产出文件的**文本内容**（`GET /api/conversations/{id}/files/content`）。
 *
 * ★ 为什么 `is_text` 由后端给，而不是前端按扩展名猜
 *
 * 只有读过字节的一方才说得准。前端猜会两头出错：把 `.docx` 当文本解码成乱码、
 * 又认不出没有扩展名的纯文本。所以后端如实回答「这能不能当文本读」，
 * 前端只决定「拿到文本之后怎么渲染」——呈现方式的映射（html→画布、md→Markdown、
 * csv→表格）是纯粹的展示决策，留在前端。
 */
export interface ConversationFileContent {
  success: boolean;
  path: string;
  name: string;
  suffix: string;
  /** 文件**真实**大小（不是读到的字节数 —— 截断时两者不同） */
  size: number;
  is_text: boolean;
  /** `is_text=false` 时为 null */
  content: string | null;
  /** 超过预览上限被截断 —— 界面必须说出来，否则用户以为文件就这么短 */
  truncated: boolean;
  preview?: SpreadsheetFilePreview | { kind: 'document'; text: string; blocks?: DocumentPreviewBlock[]; notice: string; truncated: boolean }
    | { kind: 'archive'; entries: { name: string; size: number }[]; total: number; truncated: boolean };
}

export interface PreviewCell { text: string; value: number | null; formula?: string }
export type DocumentPreviewBlock = { kind: 'heading' | 'paragraph'; text: string } | { kind: 'table'; rows: string[][] };
export interface PreviewSheet {
  name: string; rows: PreviewCell[][]; total_rows: number; total_columns: number;
  truncated: boolean; formula_count: number;
}
export interface SpreadsheetFilePreview {
  kind: 'spreadsheet'; sheets: PreviewSheet[]; truncated: boolean; notice?: string;
}

export interface ErrorEvent {
  type: 'error';
  content: string;
}

/**
 * 记忆事件（Phase 5）
 *
 * 只在**真的写入了**记忆时才推送 —— 没有变化就不推，
 * 否则每次对话都弹一句「已记住」会变成噪音。
 * `items` 是写进去的值本身，`content` 是给用户看的一句话回执。
 */
export interface MemoryEvent {
  type: 'memory';
  action: 'saved' | 'skipped' | 'error';
  content: string;
  items?: string[];
}

/**
 * 思考增量事件（深度思考模式，`enable_thinking` 时后端逐段推送）
 *
 * 与 `token` 同构（content 为增量文本），但渲染去向不同：
 * token 进回答正文，reasoning 进折叠的思考块 —— 实时流式期间就能看到。
 * 完整正文在 `done.reasoning` 上再给一份（回看历史/重放走它）。
 */
export interface ReasoningEvent {
  type: 'reasoning';
  content: string;
}

/**
 * 钩子给用户看的一句话（Phase 5）
 *
 * ★ 它是**通知**，不是判定
 *
 * 钩子的 deny 走的是 `interrupt` / 拒绝文案那条正路（有 call_id、有理由、会挡住执行）；
 * `hook_notice` 只负责「脚本说了句人话，得让用户看见」—— 脚本 stdout 里的
 * `message` / `systemMessage`、纯文本首行、以及被忽略的 allow 都从这里出来。
 * 渲染成非阻塞的一行，绝不弹窗：一条钩子不该有打断对话的能力。
 *
 * `event` 让前端知道是哪个节点说的（工具调用后 vs 本轮结束时），
 * `call_id` / `tool_name` 只工具类事件带，用于挂到对应的那次调用上。
 */
export interface HookNoticeEvent {
  type: 'hook_notice';
  content: string;
  event: HookEvent;
  call_id?: string;
  tool_name?: string;
  tool_label?: string;
}

export type AgentEvent =
  | TokenEvent
  | AssistantMessagesEvent
  | SourcesEvent
  | IntentEvent
  | AgentSwitchEvent
  | ToolCallEvent
  | ToolResultEvent
  | InterruptEvent
  | QuestionEvent
  | QuestionResolvedEvent
  | ApprovalAutoEvent
  | HookNoticeEvent
  | StateUpdateEvent
  | MemoryEvent
  | ReasoningEvent
  | SubtaskEvent
  | TaskReportEvent
  | DoneEvent
  | ErrorEvent;

// ────────────────────────────────────────────────
// REST 接口类型
// ────────────────────────────────────────────────

export interface FileUploadResponse {
  filename: string;
  file_size: number;
  chunks_indexed: number;
  success: boolean;
  message: string;
}

export interface ToolInfo {
  name: string;
  /** 面向用户的中文名（后端唯一的展示名来源） */
  display_name?: string;
  description: string;
  danger_level: DangerLevel;
  allowed_agents: string[];
  version: string;
  args_schema?: Record<string, unknown> | null;
  health: 'healthy' | 'degraded' | 'down' | 'unknown';
  /** 治理界面分组：文件 / 终端 / 网络 / 文档 / 编排 / 日程 / 邮件 */
  category?: string;
  /** builtin / mcp / skill —— 只用于展示与审计，三类工具在注册中心同权 */
  source?: string;
  /** MCP 工具所属 server 名；内置工具为空 */
  server?: string;
  /** 只读工具（权限模式可整体放行读取） */
  read_only?: boolean;
  /** 内部工具，不在治理界面展示 */
  hidden?: boolean;
}

export interface ToolListResponse {
  total: number;
  stats: Record<string, number>;
  /** 分类 → 工具数 */
  categories?: Record<string, number>;
  tools: ToolInfo[];
  /** 主对话可直接调用的工具名 */
  main_tools?: string[];
  /** 工具名 → 主对话不可用原因（danger / desktop / hidden / web_off / mcp_scope） */
  main_unavailable?: Record<string, string>;
  /** 作用域异常：allowed_agents 里写着不存在的专员 */
  scope_warnings?: { tool: string; display_name: string; unknown_agents: string[] }[];
}

export interface ApprovalResponse {
  success: boolean;
  call_id: string;
  approved: boolean;
  message: string;
  /**
   * 后端落库的留痕 id（Phase 6）。
   *
   * 拿不到时为 `""`（例如决策到达晚了、没有匹配到等待中的请求）。
   * 带上它是为了让「刚批的这一次」能被直接跳转到留痕面板里那一条。
   */
  record_id?: string;
}

// ────────────────────────────────────────────────
// 长期记忆（Phase 5）—— 与后端 app/api/memory.py 对齐
// ────────────────────────────────────────────────

/** 一条长期偏好 */
export interface MemoryItem {
  /** 去重键，同时也是删除时用的 id */
  id: string;
  key: string;
  /** 真正被拼进 Prompt 的那句话 */
  value: string;
  category: string;
  confidence: number;
  /** 用户原话 —— 界面必须展示，否则用户无法判断系统是否误解了他 */
  evidence: string;
  source: 'explicit' | 'inferred' | 'manual' | string;
  created_at: string;
  updated_at: string;
  thread_id: string;
  /** 被本条取代的历史值（最近的在前） */
  superseded: string[];
  /** 软删除标记：false 表示已被否定（注入跳过，可恢复） */
  active: boolean;
  inactive_reason: string;
  stability_days?: number;
  last_reinforced_at?: string;
  reinforcement_count?: number;
  pinned?: boolean;
  retention?: number;
  memory_state?: 'core' | 'fresh' | 'fading' | 'dormant' | 'inactive';
}

export interface MemoryStats {
  total: number;
  by_category: Record<string, number>;
  by_source: Record<string, number>;
  updated_at: string;
  enabled: boolean;
  extract_enabled: boolean;
  store_path: string;
  max_items: number;
  max_chars: number;
  /** 「她记得你」的条数与两条硬上限（后端未重启时缺省） */
  owner_items?: number;
  owner_max_items?: number;
  owner_max_chars?: number;
  decay_enabled?: boolean;
  recall_threshold?: number;
  by_state?: Record<string, number>;
}

export interface MemoryListResponse {
  items: MemoryItem[];
  stats: MemoryStats;
  categories: Record<string, string>;
  sources: Record<string, string>;
}

export interface MemoryCreateResponse {
  ok: boolean;
  /** created / updated / unchanged / protected */
  action: string;
  item: MemoryItem;
}

export interface MemoryPromptBlockResponse {
  block: string;
  chars: number;
  empty: boolean;
  /** 「她记得你」的注入块（与偏好块分开注入，后端未重启时缺省） */
  owner_block?: string;
  owner_chars?: number;
}

// ────────────────────────────────────────────────
// 助手人设 —— 与后端 app/api/persona.py 对齐
// ────────────────────────────────────────────────

/** 一份可编辑的助手人设（人设卡）。带 ? 的字段是人设卡扩展 —— 后端未重启时缺省 */
export interface PersonaProfile {
  name: string;
  tagline: string;
  /** 语气预设 key 或 'custom' */
  tone: string;
  tone_custom: string;
  instructions: string;
  /** 头像 data URL（纯 UI，不进 Prompt） */
  avatar?: string;
  /** 性格标签（进 Prompt） */
  traits?: string[];
  /** 开场白（新会话欢迎语，纯 UI） */
  greeting?: string;
  /** 说话示例（进 Prompt 的口吻样例） */
  examples?: string[];
  enabled: boolean;
  /** 作用范围：true = 普通对话与助理都注入；false/缺省 = 仅助理会话 */
  apply_globally?: boolean;
  // ---------- 说话层与关系感（后端未重启时缺省） ----------
  /** 预设 key：只用于回显「这套人设的底子是哪套」 */
  preset?: string;
  /** 她怎么称呼你（进 Prompt；写了就不再从关系记忆里取称呼） */
  address_me?: string;
  /** 口头禅/说话小习惯 */
  quirks?: string[];
  /** 是否把当前日期/时段注入动态块 */
  time_awareness?: boolean;
  /** 主动问候级别：off / light */
  proactive?: 'off' | 'light' | string;
  quiet_start?: string;
  quiet_end?: string;
  proactive_min_gap_hours?: number;
  proactive_daily_max?: number;
  updated_at: string;
}

/** 一套人设预设（后端 PERSONA_PRESETS 的镜像；只负责填充草稿） */
export interface PersonaPreset {
  label: string;
  description: string;
  tone: string;
  traits: string[];
  instructions: string;
  examples: string[];
  quirks: string[];
}

export interface PersonaResponse {
  profile: PersonaProfile;
  /** 语气预设 key → 说明（含 custom 项） */
  tone_presets: Record<string, string>;
  /** 人设预设方案（后端未重启时缺省） */
  presets?: Record<string, PersonaPreset>;
  proactive_levels?: string[];
  stats: {
    enabled: boolean;
    apply_globally?: boolean;
    preset?: string;
    proactive?: string;
    time_awareness?: boolean;
    store_path: string;
    tone_presets: Record<string, string>;
  };
}

export interface PersonaUpdateResponse {
  ok: boolean;
  profile: PersonaProfile;
}

export interface PersonaFragmentResponse {
  fragment: string;
  chars: number;
  empty: boolean;
  /** 实际用于渲染的会话（缺省按助理会话预览） */
  thread_id?: string;
  /** 稳定块：不随时间变化的人设（后端未重启时缺省） */
  stable?: string;
  /** 动态块：此刻 + 关于用户（后端未重启时缺省） */
  dynamic?: string;
  stable_chars?: number;
  dynamic_chars?: number;
}

/** `POST /api/assistant/checkin` 的回执 */
export interface AssistantCheckinResponse {
  ok: boolean;
  /** 不可用时说明原因（quiet_hours / too_soon / daily_limit / cooldown…） */
  reason?: string;
  text?: string;
  turn_id?: string;
  created_at?: string;
  initiated_by?: 'assistant' | string;
}

/** 自定义语气在预设字典里的 key（与后端 TONE_CUSTOM 对齐） */
export const PERSONA_TONE_CUSTOM = 'custom';

/** 类别 → 中文标签（后端 CATEGORIES 的镜像，仅用于兜底渲染） */
export const MEMORY_CATEGORY_LABEL: Record<string, string> = {
  output_format: '输出格式',
  language: '语言',
  tone: '语气风格',
  length: '详略程度',
  workflow: '工作方式',
  tooling: '工具偏好',
  schedule: '时间安排',
  identity: '身份信息',
  relationship: '关系与称呼',
  life: '生活信息',
  other: '其他',
};

/** 来源 → 中文标签 + 可信度提示 */
export const MEMORY_SOURCE_LABEL: Record<string, string> = {
  explicit: '你明确说过',
  inferred: '系统推断',
  manual: '手动添加',
};

// ────────────────────────────────────────────────
// 增量索引（Phase 5）—— 与后端 app/api/indexing.py 对齐
// ────────────────────────────────────────────────

export interface IndexWatchDir {
  dir: string;
  exists: boolean;
}

export interface IndexWatcherStatus {
  running: boolean;
  scans_done: number;
  /** 连续失败次数。>0 说明后台监听正在出问题 —— 必须让它可见 */
  consecutive_failures: number;
  last_error: string | null;
}

export interface IndexStatus {
  busy?: boolean;
  supported_extensions?: string[];
  max_file_size_mb?: number;
  es_enabled?: boolean;
  health?: { state: 'healthy' | 'warning'; issues: string[] };
  retrieval?: { mode: string; rerank_enabled: boolean; graph_enabled: boolean };
  watch_enabled: boolean;
  watch_dirs: string[];
  interval_seconds: number;
  manifest_version: number;
  manifest_updated_at: string;
  /** 清单里登记的文件数（我们认为应该有多少） */
  tracked_files: number;
  tracked_chunks: number;
  /** 上一轮扫描的完整报告（重启后仍保留） */
  last_scan: ScanReport | null;
  watcher: IndexWatcherStatus;
  watch_dirs_exist: IndexWatchDir[];
  /** 存储侧实际块数（与 tracked_* 互相印证） */
  milvus_chunks: number | null;
  es_docs: number | null;
  milvus_error?: string;
  es_error?: string;
}

/** 知识库里的一份文档（指纹清单逐条，最新入库在前） */
export interface IndexDocument {
  keyword_indexed?: boolean | null;
  warnings?: string[];
  error?: string;
  status?: 'indexed' | 'warning' | 'error' | 'empty';
  origin?: 'upload' | 'directory';
  extension?: string;
  source: string;
  path: string;
  size: number;
  chunks: number;
  indexed_at: string;
  /** 原始文件是否还在原处。不在也照样列出来——正因为扫描不会再管它，删除入口只在这里 */
  on_disk: boolean;
}

/** 扫描计划（dry_run=true 时的返回，无副作用） */
export interface ScanPlan {
  warnings?: string[];
  has_changes: boolean;
  scanned_files: number;
  unchanged: number;
  added: Array<{ source: string; path: string }>;
  modified: Array<{ source: string; path: string }>;
  deleted: Array<{ source: string }>;
  /** 同一来源名在不同目录下重复出现 —— 会被跳过而不是互相覆盖 */
  conflicts: Array<{ source: string; paths: string[] }>;
  /** 超过 INDEX_WATCH_MAX_FILES 被跳过的文件数 */
  skipped_overflow: number;
  ok: true;
  dry_run: true;
}

/** 执行报告（dry_run=false） */
export interface ScanReport {
  busy?: boolean;
  warnings?: string[];
  ok: boolean;
  reason?: string;
  has_changes?: boolean;
  duration_ms?: number;
  finished_at?: string;
  scanned_files?: number;
  reused_unchanged?: number;
  indexed_chunks?: number;
  added: Array<{ source: string; path?: string; chunks?: number }>;
  modified: Array<{ source: string; path?: string; chunks?: number }>;
  deleted: Array<{ source: string; milvus?: number; es?: number }>;
  failed: Array<{ source: string; error: string }>;
  conflicts?: Array<{ source: string; paths: string[] }>;
  skipped_overflow?: number;
}

// ────────────────────────────────────────────────
// 会话历史（Phase 7）—— 与后端 app/api/conversations.py 对齐
// ────────────────────────────────────────────────

/** 会话的最后活跃时间只用来排序与展示，展示成「3 分钟前」比 ISO 串好读 */
export type ConversationStatus = 'ok' | 'aborted' | 'error' | 'interrupted';

export const CONVERSATION_STATUS_LABEL: Record<string, string> = {
  ok: '完成',
  aborted: '已中断',
  error: '出错',
  interrupted: '等待确认',
};

/** 列表里的一条会话（不含轮次明细） */
export interface ConversationSummary {
  thread_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  /** 轮次数（一问一答算一轮） */
  turns: number;
  /** 最后一轮的摘要（列表里那一行小字） */
  preview: string;
  /**
   * 最后一轮的收尾状态。
   *
   * 列表里要能看出「这个会话上次是答完了，还是停在等你确认/被中断了」——
   * 否则用户点进去才发现是个半截回答，会以为是恢复过程把它截断了。
   */
  last_status: string;
  /**
   * 索引里有、但轮次文件已经不在了（手工删掉或磁盘异常）。
   *
   * 必须如实展示：否则用户点进去看到空会话，只会以为「历史功能坏了」。
   */
  exists: boolean;
}

export interface ConversationStats {
  enabled: boolean;
  dir: string;
  conversations: number;
  turns: number;
  max_items: number;
}

export interface ConversationListResponse {
  total: number;
  count: number;
  offset: number;
  limit: number;
  conversations: ConversationSummary[];
  stats: ConversationStats;
}

/**
 * 一轮对话。
 *
 * ★ 存的是 `events`（当时的 SSE 事件快照）而不是渲染好的消息
 *
 * 前端恢复历史时会把 `events` **重放**进和实时流相同的那套 reducer，
 * 于是工具卡片 / 引用来源 / 审批留痕 / 记忆回执的渲染逻辑只有一份 ——
 * 不会出现「历史会话看起来和当时不一样」这种只在恢复路径上冒出来的偏差。
 * `token` 与 `done` 已经由后端剔除，重放时用 `answer` 补一个 token 事件即可。
 */
export interface ConversationTurn {
  turn_id: string;
  thread_id: string;
  created_at: string;
  user_input: string;
  answer: string;
  events: AgentEvent[];
  status: ConversationStatus | string;
  status_label: string;
  error: string;
  /**
   * 这一轮产出的文件（由后端从事件流派生，见 `DoneEvent.files`）。
   *
   * 重放路径的清单来自这里；实时路径来自 `done` 事件 —— 两条路渲染同一块界面。
   */
  files?: TurnFileRef[];
  /** 这一轮用户带的对话附件（只存 id + 文件名，正文是 24h 临时物不落盘） */
  attachments?: AttachmentMeta[];
  /** 这一轮的真实 token 用量（落盘后重放也能看到，不必当场记住） */
  usage?: TurnUsage;
  /**
   * 模型的推理正文（见 `DoneEvent.reasoning`）。
   *
   * 落盘理由与 `usage` 相同：它是**这一轮对话的属性**，重新打开历史会话时
   * 也该能看到「当时它在想什么」—— 而不是只有当场那一屏才有。
   */
  reasoning?: string;
  /**
   * 谁先开口：user（正常一问一答）/ assistant（主动问候）。
   * 旧历史没有该字段，按 user 处理。
   */
  initiated_by?: 'user' | 'assistant' | string;
}

export interface ConversationDetailResponse {
  success: boolean;
  conversation: ConversationSummary;
  total: number;
  turns: ConversationTurn[];
}

/** 恢复模型上下文的结果（打开历史会话时顺手调一次，让「到底恢复了没有」可见） */
export interface ResumeResponse {
  success: boolean;
  enabled: boolean;
  thread_id: string;
  /** true = 这次真的从历史把上下文播回去了 */
  seeded: boolean;
  turns: number;
  messages: number;
  reason: string;
}

/**
 * 一个模型对「深度思考」开关的支持情况（后端按「模型名 × 网关域名」判定，
 * 见 `app/llm/thinking.py`）。
 *
 * 拿不到（老后端 / 降级清单）时前端按「支持」处理 —— 不因此禁用开关，
 * 与后端「不发字段 = 跟随网关默认」的基线一致。
 */
export interface ThinkingCapability {
  /** 能不能主动打开思考 */
  can_on: boolean;
  /** 能不能关闭思考（仅思考模型 = false，开关锁在开） */
  can_off: boolean;
  /** 不支持方向的人话解释 —— 禁用态悬停文案 */
  reason?: string;
}

/**
 * 一个可选的模型（`GET /api/models`）。
 *
 * ★ 清单的来源是「设置 → 模型」里那份候选，不是供应商预设目录
 * 只有用户在那边配好并保存过的模型会出现在这里 —— 输入框那颗模型钮
 * 因此永远常驻（改版前它只有一个候选就整个不显示）。
 */
export interface ModelOption {
  id: string;
  label: string;
  /** true = 全局默认模型（`.env` 的 `LLM_MODEL`） */
  default: boolean;
  /**
   * 这个模型属于哪家供应商 —— 同名模型可能出现在多家目录里，
   * 光看名字分不清切过去会连到哪台网关。
   */
  provider?: string;
  /**
   * 切到这个模型时要一起切过去的连接（profile）。
   * `null` = 目录里没有对应记录（手改 .env 的旧配置），只改模型名。
   */
  profile_id?: string | null;
  /** 「深度思考」在这个连接上能不能开/关（前端开关的禁用态数据源） */
  thinking?: ThinkingCapability;
}

export interface ModelsResponse {
  models: ModelOption[];
  /** 当前全局默认模型名 */
  default: string;
  /** 这些候选所属的供应商显示名（自定义端点回域名） */
  provider: string;
  /** 当前生效连接对应的 profile；不在目录里为 null */
  active_profile_id?: string | null;
}

/**
 * `GET /api/llm/models` —— 默认回答模型 + 候选 + 覆写现状 + 只读运行参数。
 *
 * ★ 与 `ModelsResponse` 的分工
 *
 * 那个是顶栏下拉用的，只回答「**能选什么**」（只读候选，永不失败）；
 * 这个是设置页用的，还回答「**默认是哪个、怎么改**」——
 * 所以多了 `overridden` / `store` / `runtime`，也多了 PUT 与 reset。
 */
export interface LlmModelsResponse {
  /** 当前生效的默认模型（`.env` 或运行时覆写） */
  model: string;
  /** `.env` 里的原始值 —— 「恢复默认」的目标 */
  default: string;
  /** 当前是否被运行时覆写顶掉 */
  overridden: boolean;
  /** `LLM_AVAILABLE_MODELS` 的候选；为空 = 没配候选，只能手填模型名 */
  models: ModelOption[];
  selectable: boolean;
  /** 当前模型有没有在候选清单里声明过（手填的新名字会是 false） */
  in_catalog: boolean;
  /** 当前 Base URL（供应商接入，运行时覆写或 .env） */
  base_url: string;
  base_url_overridden?: boolean;
  /** 是否已配置非 .env 默认值的 API Key（GET 不回传明文，只给掩码） */
  api_key_set?: boolean;
  /** 是否有可用的 API Key（含跟随 .env 的情形；状态卡看这个） */
  api_key_present?: boolean;
  /** API Key 掩码（如 ••••••••abcd） */
  api_key_masked?: string;
  /** 供应商目录：每家各自的 Base URL / 候选模型 / Key 掩码（chip 按它渲染；GET 不回明文） */
  profiles?: LlmProfileView[];
  /** 当前驱动生效连接的 profile；null = 生效连接不在目录里（如「恢复默认」后） */
  active_profile_id?: string | null;
  /** 候选清单原文（逗号分隔；.env 或覆写） */
  available_models_declared?: string;
  config: Record<string, string | number | boolean | null>;
  store: OverrideStore;
  /** 只读运行参数 —— 界面上要能回答「这句话最终是谁在听」 */
  runtime: {
    base_url: string;
    temperature: number;
    /** null = 不发送 `enable_thinking` 字段（跟随网关默认） */
    thinking: boolean | null;
    timeout_seconds: number;
  };
  /** PUT 时回填：本次实际写入的键 */
  applied?: string[];
  /** PUT 时回填：可疑但合法的配置（不影响保存） */
  warnings?: string[];
}

/**
 * LLM profile 目录里的一条（`GET /api/llm/models` 的 `profiles`）。
 *
 * 后端 `data/llm_profiles.json` 的只读视图：Key 只给掩码与「配没配」，
 * 明文永不离开后端（ADR 0021 保证 3）。
 */
export interface LlmProfileView {
  id: string;
  name: string;
  base_url: string;
  /** 供应商控制台取 Key 的链接；自定义端点为空 */
  key_url?: string;
  /** 这家连接**保存成功**的模型 —— 对话下拉与「已保存」chip 的唯一来源 */
  models: string[];
  /** 预设建议（不落盘）：供应商清单拉不到时，「默认模型」下拉的兜底 */
  preset_models?: string[];
  /** 预设目录里的八家 vs 用户自填的自定义端点 */
  builtin?: boolean;
  api_key_present?: boolean;
  api_key_masked?: string;
}

/** `POST /api/llm/probe` —— 一次真实调用得到的实测事实 */
export interface LlmProbeResult {
  ok: boolean;
  model: string;
  /** 草稿连通性测试（/llm/test）回显：实际使用的 Base URL */
  base_url?: string;
  /** 草稿连通性测试（/llm/test）回显：实际使用的 Key 掩码 */
  api_key_masked?: string;
  /** 模型回复（已切成正文，最多 120 字） */
  reply?: string;
  /** 被切出来的推理正文字数（> 0 说明这一轮走了思考模式） */
  reasoning_chars?: number;
  ms: number;
  error?: string;
}

/** `POST /api/llm/models/list` —— 供应商真实模型清单（设置页「默认模型」下拉的数据源） */
export interface LlmRemoteModelsResponse {
  ok: boolean;
  /** 模型 id 列表；拉不到时为空数组（原因在 error 里） */
  models: string[];
  base_url: string;
  ms: number;
  error?: string;
}

/**
 * 语音模型的运行时设置（`GET/PUT /api/voice/settings`）。
 *
 * `config` 的键与后端 `VOICE_OVERRIDE_KEYS` 一一对应：
 * STT 后端（local / dashscope）、STT 模型、TTS 后端（dashscope / edge）、
 * TTS 模型与音色。
 */
export interface VoiceSettingsResponse {
  config: {
    VOICE_STT_BACKEND: string;
    VOICE_STT_MODEL: string;
    VOICE_TTS_BACKEND: string;
    VOICE_TTS_MODEL: string;
    VOICE_TTS_VOICE: string;
    VOICE_TTS_VOICE_EDGE: string;
    VOICE_TTS_RATE: string;
  };
  store: OverrideStore;
  enabled: boolean;
  /** 是否已配置可用的 API Key（云端 STT 与 DashScope TTS 依赖它） */
  api_key_set: boolean;
  /** 本地 STT 模型状态（后端只查文件，不加载模型） */
  local_stt: {
    available: boolean;
    model: string;
    path: string;
    reason: string | null;
  };
  /** PUT 时回填：本次实际写入的键 */
  applied?: string[];
  /** PUT 时回填：可疑但合法的配置 */
  warnings?: string[];
}

/** `POST /api/voice/settings/probe` —— 真合成一小段语音的实测结果 */
export interface VoiceProbeResult {
  ok: boolean;
  backend: string;
  model: string;
  voice?: string;
  /** 合成出的音频字节数 */
  bytes?: number;
  first_audio_ms?: number;
  streaming?: boolean;
  ms: number;
  error?: string;
}

/**
 * 搜索命中片段的三段式。
 *
 * ★ 为什么是由后端切成三段，而不是前端再做一次 `indexOf`
 *
 * 「命中在哪」只有搜过的一方说得准（大小写、空白压平都在后端做过了）。
 * 前端只要把三段按顺序拼起来、给中间那段套一层高亮 —— 于是全项目
 * 只有一处规则在定义「什么算命中」，不会两边各维护一份然后慢慢漂移。
 */
export interface ConversationMatchSnippet {
  prefix: string;
  match: string;
  suffix: string;
}

export interface ConversationMatch {
  turn_id: string;
  created_at: string;
  /** 命中出现在哪一侧：用户说的话，还是助手的回答 */
  role: 'user' | 'assistant' | string;
  snippet: ConversationMatchSnippet;
}

/** 搜索命中一个会话（含它的前几条命中片段） */
export interface ConversationSearchHit {
  thread_id: string;
  title: string;
  updated_at: string;
  /** 该会话的总轮次数 */
  turns: number;
  /** 该会话里命中的**总处数**（可能多于 `matches` 的长度） */
  match_count: number;
  matches: ConversationMatch[];
}

export interface ConversationSearchResponse {
  success: boolean;
  /** 后端压平空白后的实际查询词（用来回显，也用来核对前端传对了没） */
  query: string;
  total: number;
  results: ConversationSearchHit[];
}

// ────────────────────────────────────────────────
// 展示辅助：Agent 中文名
// ────────────────────────────────────────────────

export const AGENT_LABEL: Record<string, string> = {
  main_agent: '主控',
  pc_agent: '电脑管家',
  file_agent: '文件管家',
  software_agent: '软件管家',
  browser_agent: '浏览器管家',
  tool_executor: '工具执行器',
};

export const INTENT_LABEL: Record<string, string> = {
  pc: '系统运维',
  file: '文件与知识',
  software: '软件与工具',
  browser: '网页交互',
  general: '通用对话',
};

export function agentLabel(name: string): string {
  return AGENT_LABEL[name] ?? name;
}

export function intentLabel(intent: string): string {
  return INTENT_LABEL[intent] ?? intent;
}

// ────────────────────────────────────────────────
// 审批留痕（Phase 6）—— 与后端 app/api/approval.py 对齐
// ────────────────────────────────────────────────

// ────────────────────────────────────────────────
// 检索模型（向量召回 / 精排的运行时切换）—— 与后端 app/api/rag.py 对齐
// ────────────────────────────────────────────────

export type EmbeddingBackend = 'api' | 'local';

/** 向量召回（embedding）侧的生效状态（`embedding_info()`） */
export interface RagEmbeddingState {
  backend: EmbeddingBackend | string;
  model: string;
  /** 解析出的向量维度；解析不出来时为 null */
  dimension: number | null;
  /** 后端配置有问题时的原因（维度对不上 / 目录缺失） */
  degraded_reason: string | null;
  /** 本地模型目录的零成本预检结果（backend=local 时才有值） */
  local_model_problem?: string | null;
  /** 仅 backend=local：模型目录 / 设备 / 权重是否已载入内存 */
  path?: string;
  device?: string;
  loaded?: boolean;
}

/** 精排（rerank）侧的生效状态（`reranker_status()`） */
export interface RagRerankState {
  enabled: boolean;
  /** 生效配置里指定的后端（`api` / `local` / `none`）；与 `active` 的区别在于
   *  `active` 是**实际构造出来的实例**名：没启用或还没构造时是 `noop` / `not_loaded` */
  configured: string;
  /** 实际在用的后端；`not_loaded` = 还没构造过 */
  active: string;
  model: string;
  candidates: number;
  top_n: number;
  /** 降级原因（目录缺失 / 未知后端 / 依赖没装） */
  degraded_reason: string | null;
  local_model_problem?: string | null;
  path?: string;
  device?: string;
  loaded?: boolean;
}

/**
 * 向量库与当前 embedding 配置的一致性。
 *
 * ★ 三态，不是一个 bool
 *
 * - `match`    同一个向量空间，可以直接用
 * - `mismatch` 库是别的模型建的 → 相似度已失去意义，需要重建
 * - `unknown`  读不到（旧库没登记产出模型 / Milvus 连不上）→ **不下结论**
 *
 * 把 `unknown` 也当成「需要重建」，会让人在 Milvus 没启动的时候去重建向量库。
 */
export interface RagVectorStoreState {
  collection: string;
  /** 当前配置对应的向量空间标识（`backend:model:dim`） */
  expected: string | null;
  /** 库里登记的产出模型（建表时写进 description 的那串） */
  registered: string | null;
  /** 库里实际的向量维度 */
  dimension: number | null;
  state: 'match' | 'mismatch' | 'unknown';
  /** 读不到 / 连不上 / 未登记时的说明 */
  error: string | null;
}

/** 本机扫到的本地模型（供直接挑选） */
export interface RagLocalModel {
  path: string;
  name: string;
  /** 从 config.json 的 architectures 判出来的用途 */
  kind: 'embedding' | 'rerank' | 'unknown';
  dimension: number | null;
  model_type: string | null;
  /** 权重文件大小（MB），没有 safetensors 时为 null */
  weights_mb: number | null;
  root: string;
}

/** 运行时覆写的现状（改的是这份文件，不是 .env） */
/**
 * 运行时覆写的现状 —— 两个模型面板（「AI → 语言模型」/「高级 → 检索模型」）共用。
 *
 * ★ 为什么是同一个结构
 *
 * 后端也共用同一个文件（`data/runtime_config.json`，见 `app/config.py::override_status`）。
 * 前端要是有两种形状，界面就得记住「这一页的 store 有 `updated_at`、那一页没有」——
 * 而两者的差异其实只有**键集**（各自收窄到自己的域），字段是同一套。
 */
export interface OverrideStore {
  path: string;
  /** 已生效的覆写项（只有本域收窄后的键） */
  active: Record<string, string | number | boolean>;
  keys: string[];
  /** `.env` 的原始值 —— 「恢复默认」的目标 */
  defaults: Record<string, string | number | boolean | null>;
  /** 本域允许在运行时修改的键 */
  updatable: string[];
  updated_at: string;
}

/** `GET /api/rag/models` 的全部内容 */
export interface RagModelsResponse {
  /**
   * 生效的配置项（两个后端各自的模型名都在，不管当前用哪个）。
   *
   * 表单要读的是它 —— `embedding` 只在 backend=local 时才带 `path`，
   * 想「预览切到 local 会用什么模型」就只能看这里。
   */
  config: Record<string, string | number | boolean | null>;
  embedding: RagEmbeddingState;
  rerank: RagRerankState;
  vector_store: RagVectorStoreState;
  local_models: RagLocalModel[];
  store: OverrideStore;
  /** PUT 时回填：本次实际写入的键 */
  applied?: string[];
  /** PUT 时回填：可疑但合法的配置（不影响保存，但会让下一次加载失败） */
  warnings?: string[];
}

/** `POST /api/rag/probe` —— 一次真实调用得到的实测事实 */
export interface RagProbeResult {
  embedding: {
    ok: boolean;
    backend: string;
    model: string;
    dimension?: number;
    declared_dimension?: number;
    dimension_matches?: boolean;
    error?: string;
    ms: number;
  };
  rerank: {
    ok: boolean;
    backend: string;
    model: string;
    /** 每条候选的精排分（降级时为 null） */
    scores?: Array<number | null>;
    /** 相关句与无关句的分数差 —— 接近 0 说明精排没在做事 */
    gap?: number | null;
    error?: string;
    ms: number;
  };
}

// ────────────────────────────────────────────────
// 智能体注册表 —— 与后端 app/agents/registry.py + app/api/agents.py 对齐
//
// ★ 只管「智能体」，不管「数字员工」
// 专员服务于**主对话**（Supervisor 拆解 → 子 Agent 执行），改一个影响所有会话；
// 数字员工服务于**工作空间**，是「拿某个专员当模板的一次雇佣」。
// 两套异构维度不同、爆炸半径不同，故意不共用类型（见下方 `Employee`）。
// ────────────────────────────────────────────────

/** 一个专员的完整定义（后端 `AgentSpec.to_json()` 的镜像） */
export interface AgentDef {
  name: string;
  display_name: string;
  domain: string;
  responsibility: string;
  /** 调度视角的能力说明：「Supervisor 该在什么时候想到我」 */
  planner_hint: string;
  /** 人话能力名，模型在计划里选的是这些词而不是内部标识 */
  capabilities: string[];
  /** 显式工具白名单；空数组 = 跟随工具表各自的 allowed_agents */
  tools_allowed: string[];
  /** 该专员的模型；空串 = 跟随全局 */
  model: string;
  /** null = 跟随现状（runner 的 temperature=0） */
  temperature: number | null;
  /** null = 跟随全局 AGENT_MAX_TOOL_STEPS */
  max_steps: number | null;
  /** 规划前是否跑知识库检索 */
  retrieval: boolean;
  /** 常驻 = 主对话的调度者，不进可派发清单 */
  resident: boolean;
  enabled: boolean;
  builtin: boolean;
  updated_at: string;

  // ---------- 后端派生（界面用） ----------
  /** 最终生效的工具名（跟随工具表时是全集，显式指定时是收窄后的那一批） */
  effective_tools: string[];
  tool_mode: 'inherit' | 'explicit';
}

/** `GET /api/agents` */
export interface AgentsResponse {
  agents: AgentDef[];
  /** 能力认不出时的兜底专员标识 */
  fallback: string;
  max_custom: number;
  custom_count: number;
  global_model: string;
  global_max_steps: number;
}

export interface AgentWriteResponse {
  ok: boolean;
  agent: AgentDef | null;
}

/** 新建/编辑入参：全部可选，更新走部分合并 */
export interface AgentPayload {
  name?: string;
  display_name?: string;
  domain?: string;
  responsibility?: string;
  planner_hint?: string;
  capabilities?: string[];
  tools_allowed?: string[];
  model?: string;
  temperature?: number | null;
  max_steps?: number | null;
  retrieval?: boolean;
  enabled?: boolean;
}

/**
 * `GET /api/agents/{name}/prompt` —— 身份段原文（可验证出口）。
 *
 * ⚠️ 它**不含**助手人设、长期记忆与员工角色卡：那些是运行时另外叠上去的。
 */
export interface AgentPromptResponse {
  identity: string;
  chars: number;
}

// ────────────────────────────────────────────────
// 工作空间（群组 + 数字员工）—— 与后端 app/workspace 对齐
// ────────────────────────────────────────────────

/** 数字员工的能力模板（与后端 `models.BASE_AGENTS` 对齐） */
export type BaseAgent = 'pc_agent' | 'file_agent' | 'software_agent' | 'browser_agent';

export interface Workspace {
  id: string;
  name: string;
  description: string;
  created_at: string;
  updated_at: string;
  group_count?: number;
  employee_count?: number;
  groups?: Group[];
}

export interface Group {
  id: string;
  workspace_id: string;
  name: string;
  created_at: string;
  member_count?: number;
  message_count?: number;
  members?: Employee[];
}

export interface Employee {
  id: string;
  workspace_id: string;
  name: string;
  tagline: string;
  tone: string;
  tone_custom: string;
  instructions: string;
  scope: string;
  base_agent: BaseAgent;
  tools_allowed: string[];
  enabled: boolean;
  model: string;
  model_profile_id: string;
  created_at: string;
  updated_at: string;
}

/**
 * 群组消息的九类（后端 `store.MESSAGE_KINDS` 的同一份词表）。
 *
 * ★ 前三类是**定向的**（`recipient_employee_id` 非空）：request / commitment / deliverable / blocker
 *   后五是**广播的**：goal / proposal / decision / summary / system。
 *   分得开，界面上才分得出「说给我听的」和「说给大家听的」（施工图 §5.7）。
 * `report` / `conclusion` 是 v1 的旧名，后端读取时已归一，前端不再需要别名。
 */
export type GroupMessageKind =
  | 'goal'
  | 'proposal'
  | 'request'
  | 'commitment'
  | 'deliverable'
  | 'blocker'
  | 'decision'
  | 'summary'
  | 'system';

export interface GroupMessage {
  id: string;
  group_id: string;
  employee_id: string | null;
  kind: GroupMessageKind;
  content: string;
  created_at: string;
  meta: Record<string, unknown>;
  /** 发送者人话名字（前端 enrich 出来的派生字段，非落库字段） */
  employee_name?: string;
  employee_tagline?: string;
  /** 说给谁听的（定向消息才有；空 = 广播给全群） */
  recipient_employee_id?: string;
  recipient_name?: string;
  /** 回答的是哪一条 request（只由 `answer` 写） */
  reply_to?: string;
  /** 收件箱的一次性状态：`''` 待读 / `seen` 读过 / `answered` 已回答 */
  status?: string;
  delivery_state?: 'pending' | 'read' | 'processing' | 'answered' | 'failed' | 'cancelled';
  delivery_error?: string;
  run_id?: string;
  task_id?: string;
  sender_kind?: string;
  visibility?: string;
  /**
   * SQLite 的 `rowid` —— 本项目唯一可比较的消息顺序键（`id` 是随机 hex，排不了序）。
   * 断线补拉的游标就是它：`GET .../messages?after_seq=<见过的最大值>`。
   */
  seq?: number;
}

export interface WorkspaceListResponse {
  total: number;
  count: number;
  workspaces: Workspace[];
}

export interface GroupListResponse {
  total: number;
  groups: Group[];
}

export interface GroupMessagesResponse {
  total: number;
  count: number;
  messages: GroupMessage[];
}

/**
 * 员工为团队干过的一件活（`GET /workspace/employees/{id}/works`，施工图 §4.3 执行层）。
 *
 * ★ 这一屏才有全文，群聊只有摘要短句
 * 进度条目、工具调用与返回、`result` 全文、当时接到的原始要求（`instruction`）都在这里。
 * 后端按 `employee_id` 反查（不是按 thread_id —— 他跑群任务时线程归属是群组），
 * 少一个字段就是「他什么都没干过」那种谎报。
 */
export interface EmployeeWorkDTO extends SubtaskDTO {
  /** 来源：`board`（任务板认领）/ `employee_private`（私聊派活）/ `supervisor` / 旧记录空串 */
  source?: string;
  group_id?: string;
  run_id?: string;
  board_card_id?: string;
  instruction?: string;
}

export interface EmployeeWorksResponse {
  approvals?: GroupApprovalRequest[];
  /** ★ 'unavailable' 时必须渲染成「查不了」而不是「他没干过活」（同 SubtaskListResponse） */
  storage: 'ok' | 'unavailable';
  total: number;
  limit: number;
  offset: number;
  works: EmployeeWorkDTO[];
  storage_error?: string;
}

export interface EmployeeListResponse {
  total: number;
  employees: Employee[];
}

export interface EmployeeCapabilityOption {
  name: string;
  display_name: string;
  description: string;
  kind: 'plugin' | 'skill' | 'mcp';
  legacy_tool_names?: string[];
}

/** `GET /workspace/tools` —— 插件市场已安装且可分配的扩展。 */
export interface WorkspaceToolsResponse {
  total: number;
  tools: EmployeeCapabilityOption[];
}

/**
 * 一个项目的知识边界声明（ADR 0022「按项目隔离知识库」）。
 *
 * ⚠️ 文案口径：这是**取用隔离** —— 越界内容检索不到，但向量仍在同一个库里。
 * 界面只能写「该员工仅可检索本项目资料」，不许写「数据已按项目隔离」。
 */
export interface KnowledgeBoundary {
  workspace_id: string;
  workspace_name: string;
  /** 项目已删除但声明还留着时是 false —— 那些前缀正在把资料判给一个不存在的项目 */
  workspace_exists: boolean;
  prefixes: string[];
}

/** `GET /workspace/knowledge-scope` */
export interface KnowledgeScopeResponse {
  /** 只要有任何一个项目声明过范围就是 true（全空 = 整个知识库对所有员工可见） */
  enabled: boolean;
  workspaces_declared: number;
  path: string;
  exists: boolean;
  boundaries: KnowledgeBoundary[];
}

/**
 * run 的状态机（与后端 `run_store` 的词表一一对应，终态之外都算「还在跑」）。
 *
 * `input_required` 是界面上唯一能把「在等人」和「干砸了」分开的状态：
 * 任务板派发的危险工具挂在审批上时，run 就停在这儿。
 */
export type RunStatus =
  | 'review'
  | 'partial'
  | 'planned'
  | 'running'
  | 'input_required'
  | 'stalling'
  | 'replan'
  | 'budget_limited'
  | 'concluding'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'expired';

/**
 * 一次协作（run）的界面形状 —— 与后端 `events.run_summary` 的键**逐一对齐**。
 *
 * ★ 这份形状是三处副本之一（后端 builder / 这里 / `test_group_events.py` 的清单）
 * 键名漂移的症状不是报错，是「那一行永远刷不出来」：后端发 `id`、这里读 `run_id`，
 * 于是看板静止不动，而用户以为在跑。所以字段一律写全、不给可选。
 */
export interface RunSummary {
  budget?: {calls: number; tokens: number; max_calls: number; max_tokens: number; error: string};
  resume_available?: boolean;
  run_id: string;
  group_id: string;
  status: RunStatus;
  /** 后端给的人话（"等你补充信息"），界面读这个而不是自己再翻译一遍机器码 */
  status_label: string;
  terminal: boolean;
  /** `direct`（单卡直派，跳过拆解）/ 分工模式 */
  mode: string;
  instruction: string;
  error: string;
  /** 还没到终态的 task_id —— 「在等什么」的可对账依据 */
  pending: string[];
  stall_count: number;
  claim_rounds: number;
  updated_at: string;
}

export type CardStatus =
  | 'todo'
  | 'in_progress'
  | 'blocked'
  | 'review'
  | 'done'
  | 'cancelled';

/** 任务板上的一张卡（`events.card_summary`） */
export interface BoardCard {
  artifact_ids?: string[];
  card_id: string;
  run_id: string;
  title: string;
  status: CardStatus;
  status_label: string;
  terminal: boolean;
  /** 批次：第 0 批先跑，后一批等前一批（只做批次，不做完整 DAG） */
  batch: number;
  priority: 'high' | 'normal' | 'low' | string;
  /** 空串 = 还没人认领。认领是员工自己抢的，不是主持人派的 */
  owner_employee_id: string;
  /** 认领后真正派出去的执行任务；空串表示「抢到了但还没派」 */
  task_id: string;
  depends_on: string[];
  blocked_reason: string;
  /** 这张卡是谁建的（协调者拆解 / 某员工转提 / 用户手加） */
  created_by: string;
  updated_at: string;
}

/**
 * 挂在群组时间线上的待审批（护栏 6 的界面半边）。
 *
 * ★ 为什么不复用主对话那个 `PendingApproval`（camelCase）
 * 那份是 UI 形状（`callId`），这份是**通道载荷形状**（`call_id`）—— 事件里每个键
 * 都要能对着后端 `events.approval_required()` 的关键字参数逐个核。在这里换成 camelCase
 * 就等于在中间加一层映射，而键名漂移恰恰是这一屏最容易静默失效的地方：
 * 后端发 `call_id`、这里读 `callId`，不报错，只是那张审批卡永远点不动。
 *
 * `run_id` / `employee_id` 必须回传给 `POST /api/approval` —— 后端按四元组精确
 * 匹配等待者，少一维就是「没匹配上」，而不是「替别人签了字」。
 */
export interface GroupApprovalRequest {
  request_id?: string;
  grant_allowed?: boolean;
  grant_max_uses?: number;
  grant_ttl_seconds?: number;
  expires_at?: number;
  thread_id: string;
  call_id: string;
  tool_name: string;
  tool_label?: string;
  args: Record<string, unknown>;
  reason: string;
  run_id: string;
  employee_id: string;
  /** 「是谁在请求」—— 三条并发弹窗唯一的区分依据 */
  requestor: string;
}

/**
 * 挂在群组时间线上的待回答（模型提问的界面半边）。
 *
 * 与 `GroupApprovalRequest` 同一口径：键名照抄后端 `events.question_required()`
 * 的关键字参数，回传时 `thread_id` + `call_id` + `run_id` + `employee_id`
 * + `question_id` 一个都不能少（少一维 = 没匹配上；旧卡片唤醒新提问 = 串答案）。
 */
export interface GroupQuestionRequest extends QuestionPayload {
  thread_id: string;
}

/** 护栏触发通知：哪条护栏、在哪个环节点的、接下来会怎样（人话） */
export interface GuardrailNotice {
  guardrail: number;
  code: string;
  message: string;
  run_id: string;
  card_id: string;
}

/**
 * 群组 SSE 事件：`snapshot` 是真相，其余只是加速。
 *
 * ★ 事件名与后端 `workspace/events.py::GROUP_EVENT_TYPES` 必须一致
 * 由 `test_group_events_whitelist_matches_frontend_reducer` 钉住（与 kind 白名单同一手法）。
 * 少一边都不报错：后端发了前端不认 = 看板不动；前端写了后端没有产生者 = 永远不触发的死分支。
 */
export type GroupStreamEvent =
  | {
      type: 'snapshot';
      group_id: string;
      pending_approvals?: GroupApprovalRequest[];
      /** 刷新 / 重连后还挂着的提问卡（后端从 broker 里捞出来） */
      pending_questions?: GroupQuestionRequest[];
      messages: GroupMessage[];
      total: number;
      /** 没有在跑的 run 时是最近一次已结束的（让看板刷新后不凭空消失）；从没跑过才是 null */
      run: RunSummary | null;
      cards: BoardCard[];
      /** 断线补拉游标（messages.rowid）；少了它重连只能重放全量 */
      latest_seq: number;
    }
  | ({ type: 'message' } & GroupMessage)
  | { type: 'run_status'; run: RunSummary }
  | { type: 'card_created'; card: BoardCard; previous_status?: string }
  | { type: 'card_claimed'; card: BoardCard; previous_status?: string }
  | { type: 'card_status'; card: BoardCard; previous_status: string }
  | ({ type: 'approval_required' } & GroupApprovalRequest)
  | {
      type: 'approval_resolved';
      thread_id: string;
      call_id: string;
      approved: boolean;
      decided_by: string;
      run_id: string;
      employee_id: string;
    }
  | ({ type: 'question_required' } & GroupQuestionRequest)
  | {
      type: 'question_resolved';
      thread_id: string;
      call_id: string;
      question_id: string;
      status: 'answered' | 'skipped' | 'timeout';
      answers: QuestionAnswers;
      run_id: string;
      employee_id: string;
    }
  | ({ type: 'guardrail_triggered' } & GuardrailNotice);



// ────────────────────────────────────────────────
// Phase 5：权限模式 / 例外规则 / Hooks
//
// ★ 命名口径：全部 snake_case，与后端返回一致（全站约定）
// 后端不维护 Pydantic 镜像，这些类型是**接口返回形状**的前端唯一记录处。
// ────────────────────────────────────────────────

/** 四档权限模式，由紧到松（顺序即语义，后端 `MODE_ORDER`） */
export type PermissionModeValue = 'read_only' | 'manual' | 'auto' | 'full';

/** 一条例外规则的行为。顺序是**优先级**：deny 永远赢 */
export type RuleBehavior = 'deny' | 'ask' | 'allow';

export interface PermissionModeInfo {
  value: PermissionModeValue;
  label: string;
  /** 一句话说明「这一档她会做什么」—— 界面上直接展示，不要另写文案 */
  hint: string;
  looseness: number;
}

export interface PermissionModeResponse {
  default_mode: PermissionModeValue;
  label: string;
  hint: string;
  /** 上一次由谁在什么时候改的（空 = 从没改过，走的是出厂默认） */
  changed_at: string;
  catalog: PermissionModeInfo[];
  /** 比当前档更紧（含当前）的档位：本轮临时收紧的选择器只能用这个列表 */
  tighten_options: PermissionModeValue[];
  file_boundary: {
    /** true = `完全访问`，目录限制已解除 */
    exempt: boolean;
    sandbox: string;
    allowed_dirs: string[];
  };
}

export interface PermissionRule {
  behavior: RuleBehavior;
  tool: string;
  /** 空 = 由后端自动挑主要参数 */
  arg: string;
  /** 空 = 该工具的整个调用 */
  pattern: string;
  note: string;
  enabled: boolean;
  rule_id: string;
  created_at: string;
  /** 后端拼好的人话（「禁止 calendar_delete」），列表直接用 */
  description: string;
  behavior_label: string;
  behavior_hint: string;
}

export interface RuleStats {
  total: number;
  enabled: number;
  by_behavior: Record<RuleBehavior, number>;
  max_rules: number;
}

export interface PermissionRulesResponse {
  rules: PermissionRule[];
  total: number;
  stats: RuleStats;
  behaviors: Array<{ value: RuleBehavior; label: string; hint: string }>;
  pattern_hint: string;
}

export interface RuleWritePayload {
  behavior?: RuleBehavior;
  tool?: string;
  arg?: string;
  pattern?: string;
  note?: string;
  enabled?: boolean;
}

export interface RuleWriteResponse {
  ok: boolean;
  action?: 'created' | 'updated';
  rule: PermissionRule;
  stats: RuleStats;
}

// ---------- 电脑操控 ----------

/** 桌面连接器（MCP server）的实时状态 */
export interface ComputerControlServer {
  name: string;
  connected: boolean;
  /** 该连接器注册进来的工具数 */
  tools: number;
  error: string;
}

/**
 * `GET /computer-control` 的全部状态。
 *
 * `assistant_only` 恒为 true：总开关只对助理会话生效，
 * 普通对话、数字员工、后台子任务都拿不到桌面工具。
 */
export interface ComputerControlStatus {
  enabled: boolean;
  assistant_only: boolean;
  changed_at: string;
  /** 注册中心里属于桌面连接器的工具数 */
  tools: number;
  tool_names: string[];
  server: ComputerControlServer;
  ready: boolean;
  capabilities: Array<{ id: string; label: string; available: boolean }>;
  preferences: ComputerControlPreferences;
  model: string;
  runtime: {
    active: boolean;
    active_tool: string;
    last_capture_at: string;
    events: Array<{ tool: string; success: boolean; at: string }>;
  };
}

export interface ComputerControlPreferences {
  capture_after_action: boolean;
  capture_display: 'all' | 'primary';
  vision_model: string;
  vision_profile_id: string;
}

export interface ComputerControlPreview {
  captured_at: string;
  duration_ms: number;
  images: Array<{ image_url: string }>;
}

export interface ComputerControlVisionTest {
  model: string;
  description: string;
  duration_ms: number;
}

// ---------- Hooks ----------

/** 五个钩子事件（后端 `HOOK_EVENTS`；只有 PreToolUse 参与判定） */
export type HookEvent = 'PreToolUse' | 'PostToolUse' | 'UserPromptSubmit' | 'Notification' | 'Stop';

export interface HookInfo {
  event: HookEvent;
  command: string;
  /** 工具名通配（`bash` / `mcp_notion_*`），空 = 全部 */
  tool: string;
  /** 0 = 用全局默认 */
  timeout: number;
  enabled: boolean;
  note: string;
  hook_id: string;
  created_at: string;
  event_label: string;
  description: string;
  effective_timeout: number;
}

export interface HookStats {
  global_enabled: boolean;
  total: number;
  enabled: number;
  by_event: Record<HookEvent, number>;
  default_timeout_seconds: number;
}

export interface HooksResponse {
  hooks: HookInfo[];
  total: number;
  enabled: number;
  global_enabled: boolean;
  stats: HookStats;
  events: Array<{ value: HookEvent; label: string; decides: boolean }>;
  paths: { store: string; run_log: string };
  runtime: {
    cwd: string;
    env: string;
    default_timeout_seconds: number;
    max_timeout_seconds: number;
    payload: string;
  };
}

export interface HookWritePayload {
  event?: HookEvent;
  command?: string;
  tool?: string;
  timeout?: number;
  enabled?: boolean;
  note?: string;
}

/** 钩子 stdout 收敛出来的表态（`allow` 不会出现：钩子只能收紧） */
export interface HookVerdict {
  decision: '' | 'deny' | 'ask';
  reason: string;
  notice?: string;
  context?: string;
  error?: string;
  ignored_allow?: boolean;
}

export interface HookTestResponse {
  ok: boolean;
  hook: HookInfo;
  /** 这次真的发给脚本的 JSON —— 照着它写脚本，不用猜键名 */
  payload: Record<string, unknown>;
  ran_while_disabled: boolean;
  exit_code: number;
  timed_out: boolean;
  duration_ms: number;
  stdout: string;
  stderr: string;
  verdict: HookVerdict;
}

export interface HookRunRow {
  logged_at: string;
  event: HookEvent;
  hook_id: string;
  command: string;
  tool_name: string;
  exit_code: number;
  timed_out: boolean;
  duration_ms: number;
  decision: string | null;
  reason: string;
  notice?: string;
  test?: boolean;
}

export interface HookRunsResponse {
  runs: HookRunRow[];
  limit: number;
  log_path: string;
}

// ────────────────────────────────────────────────
// 插件市场（/api/market）
//
// 四个状态位彼此独立，界面各画各的徽标：
// installed（装没装）/ enabled（开没开）/ configuration_required（缺配置）/
// update_available（目录版本比本机新）。合并成一个布尔就会回到「1/1」的老路。
// ────────────────────────────────────────────────

export type MarketKind = 'plugin' | 'skill' | 'mcp';

export interface MarketComponentRef {
  kind: MarketKind;
  ref: string;
  name: string;
}

export interface MarketEntry {
  /** 随应用提供的代码插件，由应用版本更新，不独立安装/卸载。 */
  builtin?: boolean;
  toggleable?: boolean;
  provided_tools?: ToolInfo[];
  kind: MarketKind;
  canonical: string;
  name: string;
  description: string;
  /** official = 内置目录；remote = 远程目录；local/custom = 本机自己来的 */
  source: 'official' | 'local' | 'remote' | 'custom';
  publisher: string;
  version: string;
  category: string;
  tags: string[];
  /** 组合包成员；非 plugin 为空 */
  components: MarketComponentRef[];
  install_kind: 'pack-dir' | 'zip-url' | 'mcp-server' | '';
  danger_level: 'safe' | 'moderate' | 'danger' | string;
  required_env: string[];
  popularity: number;
  updated_at: string;
  /** 目录页「精选」筛选用，纯展示位 */
  featured: boolean;

  installed: boolean;
  installed_version: string | null;
  update_available: boolean;
  enabled: boolean;
  configuration_required: boolean;
  /** 异常 / 遮蔽 / 连接失败等原因；空串 = 没什么要解释的 */
  status_note: string;
  /** 内置能力为 false（只能停用不能卸载） */
  uninstallable: boolean;
  /** 组合包成员不为空：要卸先卸包 */
  via_bundle: string | null;
  /**
   * 「需处理」= 缺配置 / 可升级 / 坏了（技能读不出、连不上、成员缺失）。
   * ★ 停用不算 —— 那是用户自己的选择，不是待办。
   * 后端算好给一个布尔，前端别再拿 `status_note` 非空当判据（内置技能的说明会让计数虚高）。
   */
  attention: boolean;
  /** 一次性安装凭证；已安装或不可装时为 null */
  install_ref: string | null;
}

export interface MarketSource {
  id: string;
  kind: 'bundled' | 'remote' | string;
  url: string;
  ok: boolean;
  error: string | null;
  count: number;
  fetched_at: string | null;
}

/** 分类轨道上的一个分类 + 点进去能看到几条（按当前 tab 与搜索词算） */
export interface MarketCategoryCount {
  name: string;
  count: number;
}

export interface MarketSearchResponse {
  items: MarketEntry[];
  categories: MarketCategoryCount[];
  /** 「精选」chip 的条数（不受已选分类影响，和其它 chip 同一口径） */
  featured_total: number;
  sources: MarketSource[];
  failures: MarketSource[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

export interface MarketWriteResponse {
  ok: boolean;
  entry?: MarketEntry;
  /** 这次实际落地的组件 key（`skill:xxx` / `mcp:xxx`）；组合包会有好几条 */
  installed?: string[];
  apply?: unknown;
}

/** 上传 / 本机路径装出来的东西（技能或插件包） */
export interface MarketLocalInstallResponse {
  ok: boolean;
  canonical: string;
  /** 插件包才有：清单里给人看的名字 */
  name?: string;
  installed?: string[];
  entry?: MarketEntry | null;
  apply?: unknown;
}

/** 粘贴一段 MCP 配置的返回：逐条报成/败，一条坏配置不拦其它条 */
export interface MarketMcpImportResponse {
  ok: boolean;
  created: string[];
  failed: Record<string, string>;
  /** 本机没有对应机制、被丢掉的键（不静默吞掉） */
  ignored: Record<string, string[]>;
  apply?: unknown;
}

/** 「通过 Syncore 创建」预填给她的那段话（路径与格式约束由后端拼，前端不抄第二份） */
export interface MarketCreateBrief {
  kind: 'skill' | 'plugin';
  prompt: string;
  hint: string;
}

// ================================================================
// 工作区（用户授权的本地目录）
// ================================================================
//
// ⚠️ 与「工作空间」（`Workspace` / 群组 + 数字员工）是两回事：
// 那是一套组织概念，这里就是**一个磁盘目录**。界面上都叫「工作区」，
// 代码里叫 workdir，就是为了不让两者在一个名字下糊成一团。

/** 一个登记过的工作区（= Agent 被授权读写的一个目录） */
export interface WorkdirEntry {
  id: string;
  name: string;
  path: string;
  /** 图标名，取值由后端 `ICON_NAMES` 定义；前端认不出来时回落到文件夹 */
  icon: string;
  /** `#rrggbb`，渲染成图标底色 */
  color: string;
  created_at: string;
  last_used_at: string;
  /** 目录还在不在（被用户在资源管理器里删掉时是 false） */
  exists: boolean;
  /** `manual` = 用户点选的目录；`auto` = 对话自动分配的产物文件夹（`2026102901`） */
  origin: 'manual' | 'auto';
  /** `auto` 专属：这个文件夹属于哪个会话 */
  thread_id: string;
}

/** 自动分配的对话文件夹（多带会话标题与占用；仅「设置 → 数据」用） */
export interface AutoWorkdirEntry extends WorkdirEntry {
  /** 所属对话的标题；会话被删了就为空串 */
  title: string;
  files: number;
  bytes: number;
}

export interface AutoWorkdirListResponse {
  total: number;
  /** 自动目录的父目录（就是沙箱根） */
  root: string;
  bytes: number;
  files: number;
  workdirs: AutoWorkdirEntry[];
}

export interface WorkdirListResponse {
  total: number;
  workdirs: WorkdirEntry[];
  /** 后端认可的图标名清单 —— 由它给出，前端不抄一份，否则两边迟早漂移 */
  icons: string[];
}

export interface WorkdirWriteResponse {
  ok: boolean;
  workdir?: WorkdirEntry;
  deleted?: boolean;
  /** 磁盘上的文件夹是否也一起删了（只对自动目录可能为真） */
  purged?: boolean;
}

/**
 * 会话 ↔ 工作区绑定。
 *
 * 三种 `mode`：
 * - `none`：不指定，落沙箱
 * - `manual`：绑了用户点选的目录
 * - `auto`：由系统给这个对话窗口分配一个产物文件夹（整个对话共用一个，第一次用到时才建）
 */
export interface WorkdirBinding {
  workdir_id: string;
  mode: 'none' | 'manual' | 'auto';
  workdir: WorkdirEntry | null;
}

/** 目录选择器的快捷入口（主目录 / 桌面 / 文档 / 各盘根） */
export interface WorkdirRoot {
  label: string;
  path: string;
  exists: boolean;
}

/** 浏览某个目录下的子目录（选择器只要目录，不读文件内容） */
export interface WorkdirBrowseResult {
  path: string;
  /** 上一级；已经在盘根时是空串（据此禁用「上一级」） */
  parent: string;
  /** false = 这个目录读不了（权限不足），界面要说「进不去」而不是「是空的」 */
  can_read: boolean;
  total: number;
  count: number;
  truncated: boolean;
  entries: Array<{ name: string; path: string }>;
}

// ────────────────────────────────────────────────
// 定时任务（REST：/api/schedules）
// ────────────────────────────────────────────────

/** 一次运行的状态。比 SubtaskStatus 少 pending/waiting_approval —— 那两维属于子任务自己 */
export type ScheduledRunStatus = 'running' | 'succeeded' | 'failed' | 'cancelled' | 'orphaned';

export type ScheduleFreq =
  | { kind: 'daily'; time: string }
  | { kind: 'weekly'; time: string; days: number[] }
  | { kind: 'monthly'; time: string; day: number }
  | { kind: 'interval'; time: string; every_days: number }
  | { kind: 'cron'; expr: string };

export interface ScheduledTaskDTO {
  schedule_id: string;
  title: string;
  /** `chat` = 到点派给专员；`workflow` = 到点运行一个画好的工作流 */
  kind?: 'chat' | 'workflow';
  /** kind=workflow 时运行哪个工作流 */
  workflow_id?: string;
  /** 到点喂给专员的完整指令；工作流类型下是 `{{input}}` 的文本（可空） */
  instruction: string;
  freq: ScheduleFreq;
  /** 人话调度描述（「每天 09:00」）—— 后端派生，前端不自己拼中文语序 */
  freq_text?: string;
  start_date?: string;
  end_date?: string;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
  agent?: string;
  model?: string;
  web_enabled?: boolean;
  graph_enabled?: boolean;
  permission_mode?: string;
  workdir_id?: string;
  /** 下一次执行时间。缺省 = 停用或日期窗已尽 → 界面显示「暂无后续执行」 */
  next_run_at?: string;
  last_run_at?: string;
  last_status?: ScheduledRunStatus;
  last_thread_id?: string;
  last_task_id?: string;
}

export interface ScheduledRunDTO {
  run_id: string;
  schedule_id: string;
  /** ★ 本次运行的会话：文件下载与「去会话看全文」都挂在这个 thread 上 */
  thread_id?: string;
  task_id?: string;
  /** kind=workflow：对应的那一次工作流运行（步骤/产出都在它身上） */
  workflow_run_id?: string;
  trigger: 'scheduled' | 'manual';
  status: ScheduledRunStatus;
  started_at?: string;
  finished_at?: string;
  result?: string;
  error?: string;
  files?: Array<{ path: string; tool: string }>;
  usage?: Record<string, number>;
  /** 全局「运行记录」页签才带（单任务详情不必） */
  schedule_title?: string;
}

/** 调度循环健康摘要 —— 「它坏了」必须在界面上看得见，不是只写进日志 */
export interface SchedulerStatus {
  enabled: boolean;
  running: boolean;
  dispatched: number;
  consecutive_failures: number;
  last_error?: string;
}

/** storage 语义同 SubtaskListResponse：'unavailable' 必须渲染成「查不了」而不是「没有任务」 */
export interface ScheduledTaskListResponse {
  storage: 'ok' | 'unavailable';
  total: number;
  schedules: ScheduledTaskDTO[];
  storage_error?: string;
  scheduler?: SchedulerStatus;
}

export interface ScheduledRunListResponse {
  storage: 'ok' | 'unavailable';
  total: number;
  runs: ScheduledRunDTO[];
  storage_error?: string;
}

export interface ScheduleDetailResponse {
  storage: 'ok' | 'unavailable';
  schedule?: ScheduledTaskDTO;
  runs: ScheduledRunDTO[];
  total_runs: number;
  storage_error?: string;
}

export interface ScheduleRunNowResponse {
  ok: boolean;
  reason?: string;
  schedule_id: string;
  run?: ScheduledRunDTO;
}

export interface ScheduleWritePayload {
  title: string;
  /** `chat` = 派给专员的对话任务；`workflow` = 运行一个画好的工作流 */
  kind?: 'chat' | 'workflow';
  workflow_id?: string;
  instruction: string;
  freq: ScheduleFreq;
  start_date?: string;
  end_date?: string;
  enabled?: boolean;
  agent?: string;
  model?: string;
  web_enabled?: boolean;
  graph_enabled?: boolean;
  permission_mode?: string;
  workdir_id?: string;
}

// ---------- 可视化工作流（与后端 app/schemas/workflows.py 对齐） ----------

export type WorkflowNodeType = 'start' | 'llm' | 'knowledge' | 'tool' | 'condition' | 'answer';

export type WorkflowConditionOperator =
  | 'contains'
  | 'not_contains'
  | 'equals'
  | 'not_equals'
  | 'starts_with'
  | 'ends_with'
  | 'is_empty'
  | 'not_empty'
  | 'gt'
  | 'gte'
  | 'lt'
  | 'lte';

export interface WorkflowNodeConfig {
  /** llm：系统提示词（可空） */
  system?: string;
  /** llm：用户提示词（必填） */
  prompt?: string;
  /** llm：模型名；空 = 跟随全局默认模型 */
  model?: string;
  /** knowledge：检索内容（必填） */
  query?: string;
  /** knowledge：召回条数 */
  top_k?: number;
  /** tool：工具名 */
  tool?: string;
  /** tool：参数，JSON 字符串（模板变量在运行时替换） */
  args?: string;
  /** condition：左值 / 运算符 / 右值 */
  left?: string;
  operator?: WorkflowConditionOperator;
  right?: string;
  /** answer：回答模板 */
  template?: string;
  [key: string]: unknown;
}

export interface WorkflowNodeDTO {
  id: string;
  type: WorkflowNodeType;
  title: string;
  x: number;
  y: number;
  config: WorkflowNodeConfig;
}

export interface WorkflowEdgeDTO {
  id: string;
  source: string;
  target: string;
  /** 条件节点的出边是 `true` / `false` */
  source_handle?: 'true' | 'false' | string;
}

export type WorkflowLastStatus =
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'orphaned';

export interface WorkflowDTO {
  workflow_id: string;
  name: string;
  description: string;
  nodes: WorkflowNodeDTO[];
  edges: WorkflowEdgeDTO[];
  created_at?: string;
  updated_at?: string;
  last_run_at?: string;
  last_status?: WorkflowLastStatus;
}

export interface WorkflowListResponse {
  storage: 'ok' | 'unavailable';
  total: number;
  workflows: WorkflowDTO[];
  storage_error?: string;
}

export type WorkflowRunStatus = 'running' | 'succeeded' | 'failed' | 'cancelled' | 'orphaned';

export type WorkflowTrigger = 'manual' | 'scheduled' | 'assistant';

export interface WorkflowRunStepDTO {
  node_id: string;
  title: string;
  type: WorkflowNodeType;
  status: 'succeeded' | 'failed';
  started_at?: string;
  finished_at?: string;
  output?: string;
  error?: string;
  /** 条件节点走了哪一支 */
  branch?: 'true' | 'false';
}

export interface WorkflowRunDTO {
  run_id: string;
  workflow_id: string;
  status: WorkflowRunStatus;
  trigger: WorkflowTrigger;
  input?: string;
  output?: string;
  error?: string;
  current_node?: string;
  started_at?: string;
  finished_at?: string;
  steps: WorkflowRunStepDTO[];
}

export interface WorkflowDetailResponse {
  storage: 'ok' | 'unavailable';
  workflow: WorkflowDTO;
  runs: WorkflowRunDTO[];
  total_runs: number;
  storage_error?: string;
}

export interface WorkflowRunDetailResponse {
  storage: 'ok' | 'unavailable';
  run: WorkflowRunDTO;
  storage_error?: string;
}

export interface WorkflowRunStartResponse {
  ok: boolean;
  workflow_id: string;
  run?: WorkflowRunDTO;
  reason?: string;
}

export interface WorkflowDeleteResponse {
  ok: boolean;
  reason?: string;
}

export interface WorkflowWritePayload {
  name?: string;
  description?: string;
  nodes?: WorkflowNodeDTO[];
  edges?: WorkflowEdgeDTO[];
}

export interface WorkflowCreatePayload {
  name: string;
  description?: string;
  nodes: WorkflowNodeDTO[];
  edges: WorkflowEdgeDTO[];
}

// ────────────────────────────────────────────────
// 快捷指令（问法库）—— 与后端 app/api/quick_commands.py 对齐
//
// ★ 与工作流是两回事
// 这里的 body 只是**一段可插入输入框的话**：用户选中后还能改、能带附件。
// 它不执行任何东西，也不是工作流的别名 —— 工作流的「/」入口不存在。
// ────────────────────────────────────────────────

export interface QuickCommand {
  id: string;
  title: string;
  /** 插入输入框的正文；`{{...}}` 是给用户自己替换的占位 */
  body: string;
  category: string;
  /** 内置指令只读（改 / 删会得到 403），复制一条才能改 */
  builtin: boolean;
  created_at?: string;
}

export interface QuickCommandListResponse {
  commands: QuickCommand[];
  categories: string[];
  total: number;
}

export interface QuickCommandWritePayload {
  title: string;
  body: string;
  category: string;
}

export interface WorkflowMetaResponse {
  node_types: Array<{ type: WorkflowNodeType; label: string }>;
  operators: Array<{ value: WorkflowConditionOperator; label: string }>;
  tools: Array<{
    name: string;
    display_name?: string;
    description: string;
    category: string;
    danger_level: 'safe' | 'moderate' | 'danger';
    requires_approval: boolean;
  }>;
  max_steps: number;
}

// ---------- 外部渠道（与后端 app/api/channels.py 对齐） ----------

/**
 * 一条 IM 渠道的状态。
 *
 * ⚠️ `bound` 与 `running` 是两件事：
 * - `bound` = 这台机器上有没有绑定凭据；
 * - `running` = 后端通道 worker 此刻在不在收消息。
 * 只有 `running` 为 true 时，微信那头才会有人回话。界面必须两个都看，
 * 否则「凭据还在、服务死了」会被显示成一切正常。
 */
export interface ChannelState {
  key: string;
  label: string;
  transport: string;
  bound: boolean;
  /** 通道 worker 是否在收消息（后端状态接口给的） */
  running?: boolean;
  /** 最近一次通道错误；空串 = 正常 */
  last_error?: string;
  /** 最近一次收到消息的时刻（epoch 秒），0 = 还没收到过 */
  last_message_at?: number;
  /** 一句话状态（未绑定 / 已连接 / 已绑定，通道未运行…），后端给的，直接显示 */
  detail: string;
  /** 绑定时刻（epoch 秒） */
  bound_at?: number | null;
}

export interface ChannelsResponse {
  channels: ChannelState[];
}

/**
 * 微信扫码登录的一次性会话（后端 /channels/wechat/login）
 *
 * `idle` = 没在扫；`bound` = 网关说这个微信号早就绑过了（不是失败，但也没拿到新凭据）。
 */
export type WechatLoginStatus =
  | 'idle'
  | 'waiting'
  | 'scanned'
  | 'verifycode'
  | 'confirmed'
  | 'expired'
  | 'bound'
  | 'failed';

export interface WechatLoginState {
  status: WechatLoginStatus;
  /** 后端原文（多半是错误信息），只在 failed / bound 这类需要说清原因时才显示 */
  detail: string;
  /** 二维码 PNG 的 data URL。过期后后端给空串 —— 一张扫不了的码不该继续挂着 */
  qr_png: string;
  /** 还剩几秒可扫；0 表示已经没有倒计时可言 */
  expires_in: number;
}

// ────────────────────────────────────────────────
// 用量账本 —— 与后端 app/api/usage.py 对齐
// 与 trace 是两本账：trace 有保留期会滚动删除，账本只增不删
// ────────────────────────────────────────────────

/** 对话窗口类型，由 thread_id 前缀派生（后端 window_kind_of） */
export type UsageWindowKind = 'chat' | 'employee' | 'subtask' | 'assistant';

/** 账本存储状态。`immutable` 是后端声明的不变量，界面据此显示「只增不删」 */
export interface UsageLedgerStats {
  enabled: boolean;
  directory: string;
  shards: number;
  total_bytes: number;
  today_records: number;
  appended_this_process: number;
  write_errors: number;
  immutable: boolean;
}

/** 曲线上的一天 */
export interface UsageDayPoint {
  date: string;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  calls: number;
}

/** 按模型汇总的一行 */
export interface UsageModelRow {
  model: string;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  calls: number;
  /** 该模型出现在多少个对话窗口里 */
  windows: number;
}

/** 一个对话窗口内，按模型拆开的 token */
export interface UsageWindowModel {
  model: string;
  total_tokens: number;
}

/** 按对话窗口汇总的一行（用量页的主表） */
export interface UsageWindowRow {
  thread_id: string;
  /** 展示名：当前会话标题 > 落账时的标题快照 > 「类型·短 id」 */
  label: string;
  title: string;
  window_kind: UsageWindowKind;
  first_ts_ms: number | null;
  last_ts_ms: number | null;
  /** 轮次 = 一次问答（同一 trace 里的多次模型调用算一轮） */
  turns: number;
  /** 模型调用笔数 */
  calls: number;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  models: UsageWindowModel[];
}

export interface UsageOverviewResponse {
  success: boolean;
  days: number;
  totals: {
    prompt_tokens: number;
    completion_tokens: number;
    reasoning_tokens: number;
    total_tokens: number;
    calls: number;
    windows: number;
    /** 网关没返回 usage 的笔数 —— 这些笔记 0，界面标「未上报」而不是显示成免费 */
    unreported: number;
  };
  /** 账本里出现过的模型（下拉数据源，不受当前过滤影响） */
  models: string[];
  per_day: UsageDayPoint[];
  per_model: UsageModelRow[];
  per_window: UsageWindowRow[];
  ledger: UsageLedgerStats;
}

/** 一笔模型调用的流水 */
export interface UsageRecord {
  record_id: string;
  ts_ms: number;
  at: string;
  thread_id: string;
  window_kind: UsageWindowKind;
  title: string;
  label: string;
  purpose: string;
  agent: string;
  model: string;
  step: number;
  duration_ms: number;
  status: string;
  reported: boolean;
  trace_id: string;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
}

export interface UsageRecordsResponse {
  success: boolean;
  count: number;
  /** 过滤后的总条数（不是账本全量） */
  total: number;
  limit: number;
  offset: number;
  records: UsageRecord[];
}
