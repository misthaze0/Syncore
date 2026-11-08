/**
 * 内部术语 → 用户语言。
 *
 * ★ 为什么需要单独一个文件
 *
 * 「意图 rag」「工具 bash」「参数 {"command": "python -V"}」这些东西在系统内部
 * 必须存在（路由、审批、留痕都要靠它们），但**用户不该看见**。
 * 翻译散落在各个组件里就一定会漂移 —— 审批弹窗说「将删除 demo.txt」、
 * 消息里的工具卡片说「bash」是同一个动作的两种说法，用户会以为它们不是一回事。
 *
 * 所以：**所有面向用户的措辞只在这里定义一次**。
 * 组件负责决定「什么时候显示」，这里只负责「显示成什么话」。
 *
 * ★ 两条措辞规则
 *
 * 1. **说动作，不说函数名。** `delete` → 「删除」，不是「调用 delete 工具」。
 * 2. **进行时与将来时分开。** 消息里已经发生的是「正在删除…」（或已完成），
 *    审批弹窗里还没发生的是「将删除…」。同一个动作在两种时态下给人的感受完全不同：
 *    前者是通知，后者是要你负责。
 */

import type { LucideIcon } from '@/components/icons';
import type { SettingsTab } from '@/pages/SettingsModal';
import type { HookEvent, SubtaskStatus, TurnUsage } from '@/types';
import {
  BarChart3,
  Bell,
  BookOpen,
  Bot,
  Database,
  Globe,
  Info,
  Keyboard,
  Monitor,
  Package,
  Palette,
  Puzzle,
  Server,
  Wand2,
  Wrench,
  Zap,
} from '@/components/icons';

/** 取一个字符串参数，去空白；不是字符串就当没有 */
function str(args: Record<string, unknown>, key: string): string {
  const value = args?.[key];
  return typeof value === 'string' ? value.trim() : '';
}

/** 取第一个命中的字符串参数（不同工具的字段名不完全统一） */
function firstStr(args: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = str(args, key);
    if (value) return value;
  }
  return '';
}

/** 过长的参数（例如一整行 shell 命令）截断，免得把一行撑爆 */
function clip(text: string, max = 48): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

interface Phrase {
  /** 进行时 / 已完成：「正在生成 周报.docx」 */
  doing: string;
  /** 将来时：「将生成 周报.docx」 */
  will: string;
}

/**
 * 工具调用的一句话描述。
 *
 * 未登记的工具（MCP 接进来的第三方工具、未来新增的内置工具）走兜底分支 ——
 * **不能因为不认识就什么都不显示**，那会让「工具正在跑」这件事凭空消失。
 */
function phraseOf(toolName: string, args: Record<string, unknown>, label?: string): Phrase {
  const path = firstStr(args, ['path', 'file_path', 'filename', 'dir', 'directory']);
  const target = path || '工作目录';

  switch (toolName) {
    case 'read':
      return { doing: `正在读取 ${clip(path || '文件')}`, will: `将读取 ${clip(path || '文件')}` };
    case 'write':
      return { doing: `正在写入 ${clip(path || '文件')}`, will: `将写入 ${clip(path || '文件')}` };
    case 'edit':
      return { doing: `正在修改 ${clip(path || '文件')}`, will: `将修改 ${clip(path || '文件')}` };
    case 'delete':
      return { doing: `正在删除 ${clip(path || '文件')}`, will: `将删除 ${clip(path || '文件')}` };
    case 'ls':
      return { doing: `正在查看 ${clip(target)}`, will: `将查看 ${clip(target)}` };
    case 'glob': {
      const pattern = firstStr(args, ['pattern', 'query']);
      return { doing: `正在找文件 ${clip(pattern, 30)}`, will: `将找文件 ${clip(pattern, 30)}` };
    }
    case 'grep': {
      const pattern = firstStr(args, ['pattern', 'query']);
      return { doing: `正在搜索 ${clip(pattern, 30)}`, will: `将搜索 ${clip(pattern, 30)}` };
    }
    case 'generate_document':
      return { doing: `正在生成 ${clip(path || '文件')}`, will: `将生成 ${clip(path || '文件')}` };
    case 'read_document':
      return { doing: `正在读取 ${clip(path || '办公文档')}`, will: `将读取 ${clip(path || '办公文档')}` };
    case 'edit_document':
      return { doing: `正在编辑 ${clip(path || '办公文档')}`, will: `将编辑并保存 ${clip(path || '办公文档')}` };
    case 'merge_document':
      return { doing: `正在填充 ${clip(path || '文档模板')}`, will: `将填充并保存 ${clip(path || '文档模板')}` };
    case 'batch_merge_documents':
      return { doing: '正在批量生成文档', will: '将批量生成文档并打包' };
    case 'convert_document':
      return { doing: `正在转换 ${clip(path || '办公文档')}`, will: `将转换并保存 ${clip(path || '办公文档')}` };
    case 'office_capabilities':
      return { doing: '正在检查办公能力', will: '将检查办公能力' };

    // 提问不是一个「动作」，但它会出现在工具卡片/进度里 ——
    // 不给一句话的话，那一行的文案会退回到工具的内部名。
    case 'ask_user': {
      const list = Array.isArray(args.questions) ? (args.questions as Record<string, unknown>[]) : [];
      const first = list[0] ?? {};
      const head = typeof first.header === 'string' && first.header
        ? first.header
        : typeof first.question === 'string' ? first.question : '';
      return {
        doing: head ? `正在等你回答「${clip(head, 20)}」` : '正在等你回答',
        will: head ? `将问你「${clip(head, 20)}」` : '将向你提问',
      };
    }

    case 'bash': {
      const command = firstStr(args, ['command', 'cmd', 'script']);
      const shown = command ? clip(command, 40) : '命令';
      return { doing: `正在执行 ${shown}`, will: `将执行 ${shown}` };
    }
    case 'bash_output': {
      const job = firstStr(args, ['job_id', 'id']);
      return { doing: `正在收取任务 ${clip(job, 16)}`, will: `将收取任务 ${clip(job, 16)}` };
    }
    case 'bash_kill': {
      const job = firstStr(args, ['job_id', 'id']);
      return { doing: `正在终止任务 ${clip(job, 16)}`, will: `将终止任务 ${clip(job, 16)}` };
    }

    case 'web_search': {
      const query = firstStr(args, ['query', 'q', 'keyword']);
      const shown = query ? `「${clip(query, 30)}」` : '';
      return { doing: `正在联网搜索${shown}`, will: `将联网搜索${shown}` };
    }
    case 'web_fetch': {
      const url = firstStr(args, ['url', 'link']);
      const shown = url ? ` ${clip(url, 40)}` : '';
      return { doing: `正在打开网页${shown}`, will: `将打开网页${shown}` };
    }

    case 'kb_search': {
      const query = firstStr(args, ['query', 'q', 'keyword']);
      const shown = query ? `「${clip(query, 30)}」` : '';
      return { doing: `正在查知识库${shown}`, will: `将查知识库${shown}` };
    }

    case 'memory_list':
      return { doing: '正在翻看你的偏好', will: '将翻看你的偏好' };
    case 'memory_remember': {
      const value = firstStr(args, ['value']);
      return { doing: `正在记住 ${clip(value, 24)}`.trim(), will: `将记住 ${clip(value, 24)}`.trim() };
    }
    case 'memory_forget': {
      const key = firstStr(args, ['key']);
      return { doing: `正在忘掉 ${clip(key, 20)}`.trim(), will: `将忘掉 ${clip(key, 20)}`.trim() };
    }

    case 'use_skill': {
      const skill = firstStr(args, ['name', 'skill']);
      const shown = skill ? clip(skill, 24) : '一份技能说明';
      return { doing: `正在翻 ${shown} 的做法`, will: `将翻 ${shown} 的做法` };
    }
    case 'skills_list':
      return { doing: '正在查看装了哪些技能', will: '将查看装了哪些技能' };

    case 'tasks_list':
      return { doing: '正在查看后台任务', will: '将查看后台任务' };
    case 'task_status':
      return { doing: '正在查看任务进展', will: '将查看任务进展' };
    case 'task_dispatch': {
      const title = firstStr(args, ['title', 'instruction']);
      return { doing: `正在派发任务 ${clip(title, 24)}`.trim(), will: `将派发任务 ${clip(title, 24)}`.trim() };
    }
    case 'task_cancel': {
      const id = firstStr(args, ['task_id', 'id']);
      return { doing: `正在取消任务 ${clip(id, 12)}`.trim(), will: `将取消任务 ${clip(id, 12)}`.trim() };
    }

    default:
      // 第三方 / 新增工具：说「在使用 X」而不是假装认识它。
      // `label` 是后端下发的中文名（MCP 工具带连接器名）；拿不到才退回内部名。
      return { doing: `正在使用 ${label || toolName}`, will: `将使用 ${label || toolName}` };
  }
}

/** 消息里的工具卡片用（进行时 / 已完成） */
export function describeToolCall(
  toolName: string,
  args: Record<string, unknown> = {},
  label?: string,
): string {
  return phraseOf(toolName, args, label).doing;
}

/** 审批弹窗用（还没发生，要用户签字） */
export function describeApprovalRequest(
  toolName: string,
  args: Record<string, unknown> = {},
  label?: string,
): string {
  return phraseOf(toolName, args, label).will;
}

/**
 * 这一轮在做什么 —— 顶栏的进行中提示。
 *
 * 刻意**不用 `intentLabel()`**：那个返回的是「知识库问答」这种分类名，
 * 是系统的内部分类，用户看到只会想「哦？还分类了？」。
 * 这里要回答的是「它现在在忙什么」，所以是动词。
 */
export function intentProgress(intent: string): string {
  switch (intent) {
    case 'pc':
      return '正在巡检电脑…';
    case 'file':
      return '正在处理文件与资料…';
    case 'software':
      return '正在打理软件…';
    case 'browser':
      return '正在联网查看…';
    default:
      return '正在思考…';
  }
}

// ────────────────────────────────────────────────
// 后台子任务（Supervisor 架构，见 ADR 0019）
//
// ★ 基础页签禁术语 —— 这条在这里体现为「状态值是人话」
// 后端的状态是 pending / running / waiting_approval / orphaned，
// 直接显示它们等于把内部实现露给用户（用户会想「orphaned 是什么」）。
// ────────────────────────────────────────────────

/** 子任务状态 → 人话（与后端 `subtasks.py::_STATUS_TEXT` 同一套口径） */
export function subtaskStatus(status: SubtaskStatus): string {
  switch (status) {
    case 'pending':
      return '排队中';
    case 'running':
      return '进行中';
    case 'waiting_approval':
      return '等你确认';
    // ★ 与「等你确认」并列：都停在人身上，但一个等的是「批不批」，一个等的是「选哪个」
    case 'waiting_input':
      return '等你回答';
    // ★ 与「等你确认」并列：都是任务停着、等人点一下，但点的是不同按钮
    case 'interrupted':
      return '等你继续';
    case 'succeeded':
      return '已完成';
    case 'failed':
      return '失败了';
    case 'cancelled':
      return '已取消';
    case 'orphaned':
      return '服务重启时中断了';
    default:
      return '';
  }
}

/** 一批任务的整体概览（面板收起时那一行） */
export function subtaskSummary(activeCount: number, totalCount: number): string {
  if (activeCount <= 0) {
    return totalCount > 0 ? `${totalCount} 个后台任务已完成` : '';
  }
  return activeCount === 1 ? '后台有 1 个任务在进行' : `后台有 ${activeCount} 个任务在进行`;
}

/** 进度步骤的图标语义（真正的图标由组件决定，这里只给「该不该转圈」） */
export function isSubtaskStepRunning(status: string): boolean {
  return status === 'running' || status === 'awaiting';
}

// ────────────────────────────────────────────────
// 命令面板的静态命令表 —— 也是「能力入口」的唯一清单
// ────────────────────────────────────────────────

export interface PaletteEntry {
  id: string;
  group: string;
  label: string;
  hint: string;
  icon: LucideIcon;
  /** 额外可搜的关键词（拼音/别名/术语），空格分隔 */
  keywords: string;
  /** 打开设置弹窗的哪个页签；没有就是纯动作 */
  settingsTab?: SettingsTab;
  /** 跳转侧栏一级页面（`/market`、`/knowledge`）；和 settingsTab 二选一 */
  route?: string;
}

/**
 * 设置页签在命令面板里的入口。
 *
 * **与 `SettingsModal` 的 ITEMS 必须一一对应** —— 这里多一条会打开一个不存在的页签，
 * 少一条则某个设置永远搜不到。所以宁可让它看起来有点重复，也好过两边各自长。
 *
 * 例外：`page.market` / `page.knowledge` 带的是 `route` —— 这两个已经搬到侧栏的一级页面，
 * 不在设置弹窗里，硬挂成页签会把人带进一个空 tab。
 *
 * `settingsTab` 的类型是 `SettingsTab` 而不是 `string`：2026-10 那次设置页重构
 * 把页签改了名（`knowledge`→`indexdocs`、`llm`→`models`…），这边 15 条里 9 条没跟上，
 * 而字符串对不上不会报错，只会让 SettingsModal 的 `ALL_KEYS.has()` 校验静默退回首屏。
 * 收紧成联合类型后，同一个漂移变成 `tsc --noEmit` 直接编译失败。
 */
export const SETTINGS_PALETTE: PaletteEntry[] = [
  // ── 通用 ──
  {
    id: 'set.appearance',
    group: '通用',
    label: '外观',
    hint: '主题、字号等视觉偏好',
    icon: Palette,
    keywords: '外观 主题 暗色 深色 浅色 字号 字体 appearance theme',
    settingsTab: 'appearance',
  },
  {
    id: 'set.shortcuts',
    group: '通用',
    label: '快捷键',
    hint: '全部键盘操作一览',
    icon: Keyboard,
    keywords: '快捷键 键盘 热键 shortcut keyboard',
    settingsTab: 'shortcuts',
  },
  {
    id: 'set.about',
    group: '通用',
    label: '关于',
    hint: '版本与运行状态',
    icon: Info,
    keywords: '关于 版本 状态 about version',
    settingsTab: 'about',
  },
  {
    id: 'set.usage',
    group: '通用',
    label: '用量管理',
    hint: 'Token 用量与调用统计',
    icon: BarChart3,
    keywords: '用量 token 统计 花费 成本 usage cost',
    settingsTab: 'usage',
  },
  // ── AI ──
  {
    id: 'set.models',
    group: 'AI',
    label: '模型',
    hint: '回答与检索用的模型',
    icon: Server,
    keywords: '模型 llm 语言模型 检索 向量 精排 embedding rerank qwen gpt',
    settingsTab: 'models',
  },
  {
    id: 'set.agents',
    group: 'AI',
    label: '智能体',
    hint: '专员的定义、工具与模型',
    icon: Bot,
    keywords: '智能体 agent 专员 子任务 自定义 supervisor',
    settingsTab: 'agents',
  },
  {
    id: 'set.skills',
    group: 'AI',
    label: '技能',
    hint: '在插件市场管理技能',
    icon: Wand2,
    keywords: '技能 正文 诊断 skill 文件夹',
    route: '/market?view=installed',
  },
  {
    id: 'set.quickcmd',
    group: 'AI',
    label: '快捷指令',
    hint: '输入 / 插入的常用问法',
    icon: Zap,
    keywords: '快捷指令 问法 提示词 斜杠 slash prompt',
    settingsTab: 'quickcmd',
  },
  {
    id: 'set.rulesmemory',
    group: 'AI',
    label: '规则与记忆',
    hint: '人设与长期偏好',
    icon: BookOpen,
    keywords: '规则 记忆 人设 偏好 persona memory 性格 语气',
    settingsTab: 'rulesmemory',
  },
  // ── 工作区 ──
  {
    id: 'page.knowledge',
    group: '工作区',
    label: '知识库',
    hint: '上传文档、查看索引状态',
    icon: Database,
    keywords: '知识库 索引 文档 上传 knowledge index document',
    route: '/knowledge',
  },
  {
    id: 'page.market',
    group: '工作区',
    label: '插件市场',
    hint: '扩展助手的能力',
    icon: Package,
    keywords: '插件 市场 扩展 plugin 技能 连接器 装',
    route: '/market',
  },
  {
    id: 'set.mcp',
    group: '工作区',
    label: '连接器',
    hint: '在插件市场管理连接',
    icon: Wrench,
    keywords: 'mcp 工具 服务器 接入 fieldwork',
    route: '/market?view=installed',
  },
  {
    id: 'set.browser',
    group: '工作区',
    label: '浏览器',
    hint: '联网查证时的浏览方式',
    icon: Globe,
    keywords: '浏览器 链接 联网 网页 browser',
    settingsTab: 'browser',
  },
  {
    id: 'set.device',
    group: '工作区',
    label: '电脑控制',
    hint: '本机文件操作边界',
    icon: Monitor,
    keywords: '电脑控制 本机 文件 沙箱 权限 device shell',
    settingsTab: 'device',
  },
  // ── 高级 ──
  {
    id: 'set.hooks',
    group: '高级',
    label: 'Hooks',
    hint: '会话节点的自动动作',
    icon: Bell,
    keywords: 'hooks 钩子 自动 触发',
    settingsTab: 'hooks',
  },
  {
    id: 'set.toolsgov',
    group: '高级',
    label: '工具治理',
    hint: '工具危险等级与授权',
    icon: Puzzle,
    keywords: '工具 治理 危险 等级 授权 tools governance',
    route: '/market?view=installed&panel=tools',
  },
];

// ==========================================================================
// 本轮用量（P1-10）—— 回答下方那一行小字
// ==========================================================================

/** 毫秒 →「840ms」/「1.2s」/「1分12秒」；无效值返回空串（调用方据此跳过） */
export function formatDuration(ms: number | undefined): string {
  if (!ms || ms < 0) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}分${Math.round(seconds % 60)}秒`;
}

/** 大数压成 k：12345 → 12.3k（一行小字里不该出现五位数） */
export function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(1)}k`;
}

/**
 * 回答下方那行小字的正文：「1.2s · 843 tokens」。
 *
 * ★ 只放用户能直接感知的两个数：等了多久、花了多少 token。
 * 输入/输出拆分、思考占比这类明细收进 tooltip（`usageDetail`）——
 * 第一屏只需要「这次划不划算」，不需要一张账单（渐进式披露）。
 * 两个数都拿不到时返回空串，调用方据此**整行不渲染**。
 */
export function formatUsageLine(
  usage: TurnUsage | undefined,
  durationMs: number | undefined,
): string {
  const parts: string[] = [];
  const duration = formatDuration(durationMs);
  if (duration) parts.push(duration);
  const total = usage?.total_tokens;
  if (typeof total === 'number' && total > 0) parts.push(`${formatTokens(total)} tokens`);
  return parts.join(' · ');
}

/**
 * 悬停明细：这些 token 花在哪了（多行）。
 *
 * 包含思考型模型那部分**看不见的推理** —— 它是「首字延迟十几秒」最直接的
 * 解释，也是本机网关唯一能拿到的思考信息（不透出思考正文，只给计数）。
 */
export function usageDetail(usage: TurnUsage | undefined): string {
  if (!usage) return '';
  const lines: string[] = [];
  if (typeof usage.prompt_tokens === 'number') lines.push(`输入 ${formatTokens(usage.prompt_tokens)}`);
  if (typeof usage.completion_tokens === 'number') lines.push(`输出 ${formatTokens(usage.completion_tokens)}`);
  if (typeof usage.reasoning_tokens === 'number' && usage.reasoning_tokens > 0) {
    lines.push(`其中内部推理 ${formatTokens(usage.reasoning_tokens)}`);
  }
  if (typeof usage.total_tokens === 'number') lines.push(`合计 ${formatTokens(usage.total_tokens)}`);
  return lines.join('\n');
}

/**
 * 钩子事件 → 给用户看的短语（消息尾部那行提示的前缀）。
 *
 * 后端在 `hook_notice` 上只给机器名 `event`，文案在这里补一份最小映射：
 * 同一句话在不同事件里读起来必须分得出来源 ——「已备份到 D:\bak」
 * 是「调用前检查」说的还是「本轮结束时」说的，是两回事。
 * 措辞刻意不用「PreToolUse」这类内部名（基础页签禁术语）。
 */
export const HOOK_EVENT_LABEL: Record<HookEvent, string> = {
  PreToolUse: '调用前检查',
  PostToolUse: '调用后检查',
  UserPromptSubmit: '收到消息时',
  Notification: '提醒',
  Stop: '本轮结束时',
};
