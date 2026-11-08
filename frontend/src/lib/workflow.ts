/**
 * 画布与后端契约之间的转换层。
 *
 * 节点位置、标题、配置都存进后端定义的 `nodes`；前端唯一的加工是
 * 「DTO ↔ React Flow 节点」。可以丢的东西（选中态、运行态）全部派生，
 * 这样保存出去的图永远是画布上真实的那一张。
 */

import type {
  WorkflowConditionOperator,
  WorkflowEdgeDTO,
  WorkflowNodeConfig,
  WorkflowNodeDTO,
  WorkflowNodeType,
  WorkflowRunStatus,
  WorkflowTrigger,
} from '@/types';

export const NODE_TYPE_META: Record<WorkflowNodeType, { label: string; hint: string }> = {
  start: { label: '开始', hint: '接收输入：手动运行时填写的文本，或定时任务里配置的内容' },
  llm: { label: '大模型', hint: '按提示词生成一段内容，可以引用上游节点' },
  knowledge: { label: '知识库', hint: '检索本地知识库，把命中的片段作为输出' },
  tool: { label: '工具', hint: '调用一个已注册的工具，需要人工批准的工具在运行中会被拒绝' },
  condition: { label: '条件', hint: '对左值做判断，按真假分别走向两个下游' },
  answer: { label: '回答', hint: '组装最终输出，工作流在这里结束' },
};

export const NODE_TYPES: WorkflowNodeType[] = ['start', 'llm', 'knowledge', 'tool', 'condition', 'answer'];

export const OPERATOR_LABELS: Record<WorkflowConditionOperator, string> = {
  contains: '包含',
  not_contains: '不包含',
  equals: '等于',
  not_equals: '不等于',
  starts_with: '开头是',
  ends_with: '结尾是',
  is_empty: '为空',
  not_empty: '不为空',
  gt: '大于',
  gte: '大于等于',
  lt: '小于',
  lte: '小于等于',
};

export const OPERATORS = Object.keys(OPERATOR_LABELS) as WorkflowConditionOperator[];

export function nodeLabel(type: WorkflowNodeType): string {
  return NODE_TYPE_META[type]?.label ?? type;
}

export function defaultNodeConfig(type: WorkflowNodeType): WorkflowNodeConfig {
  switch (type) {
    case 'llm':
      return { system: '', prompt: '{{input}}', model: '' };
    case 'knowledge':
      return { query: '{{input}}', top_k: 5 };
    case 'tool':
      return { tool: '', args: '{}' };
    case 'condition':
      return { left: '{{input}}', operator: 'not_empty', right: '' };
    case 'answer':
      return { template: '{{input}}' };
    default:
      return {};
  }
}

function randomSuffix(): string {
  return Math.random().toString(36).slice(2, 6);
}

export function makeNodeId(type: WorkflowNodeType): string {
  return `${type}_${randomSuffix()}${randomSuffix()}`;
}

export function newNode(
  type: WorkflowNodeType,
  position: { x: number; y: number },
  title?: string,
): WorkflowNodeDTO {
  return {
    id: makeNodeId(type),
    type,
    title: title ?? nodeLabel(type),
    x: Math.round(position.x),
    y: Math.round(position.y),
    config: defaultNodeConfig(type),
  };
}

/** 新建工作流时的默认骨架：开始 → 大模型 → 回答 */
export function defaultGraph(): { nodes: WorkflowNodeDTO[]; edges: WorkflowEdgeDTO[] } {
  const start: WorkflowNodeDTO = {
    id: 'start_main',
    type: 'start',
    title: '开始',
    x: 80,
    y: 200,
    config: {},
  };
  const llm: WorkflowNodeDTO = {
    id: 'llm_main',
    type: 'llm',
    title: '大模型',
    x: 380,
    y: 200,
    config: { system: '', prompt: '{{input}}', model: '' },
  };
  const answer: WorkflowNodeDTO = {
    id: 'answer_main',
    type: 'answer',
    title: '回答',
    x: 680,
    y: 200,
    config: { template: '{{llm_main.output}}' },
  };
  return {
    nodes: [start, llm, answer],
    edges: [
      { id: 'edge_start_llm', source: start.id, target: llm.id },
      { id: 'edge_llm_answer', source: llm.id, target: answer.id },
    ],
  };
}

/** 新建节点时的一句话摘要：画布上看得出它在干什么，不用点开检查器 */
export function nodeSummary(node: WorkflowNodeDTO): string {
  const config = node.config ?? {};
  const oneLine = (value: unknown) => String(value ?? '').replace(/\s+/g, ' ').trim();
  switch (node.type) {
    case 'llm':
      return oneLine(config.prompt) || '还没写提示词';
    case 'knowledge': {
      const query = oneLine(config.query) || '还没写检索内容';
      const topK = Number(config.top_k) || 5;
      return `${query} · 取 ${topK} 条`;
    }
    case 'tool':
      return oneLine(config.tool) || '还没选工具';
    case 'condition': {
      const op = config.operator as WorkflowConditionOperator | undefined;
      const label = op ? OPERATOR_LABELS[op] : '条件';
      const left = oneLine(config.left) || '…';
      const right = oneLine(config.right);
      return op === 'is_empty' || op === 'not_empty' ? `${left} ${label}` : `${left} ${label} ${right}`;
    }
    case 'answer':
      return oneLine(config.template) || '还没写回答内容';
    default:
      return '用户输入';
  }
}

export function edgeId(source: string, target: string, handle: string): string {
  return `e_${source}_${handle || 'out'}_${target}_${randomSuffix()}`;
}

export function runStatusText(status: WorkflowRunStatus | string): string {
  switch (status) {
    case 'running':
      return '运行中';
    case 'succeeded':
      return '已完成';
    case 'failed':
      return '失败了';
    case 'cancelled':
      return '已取消';
    case 'orphaned':
      return '已中断';
    default:
      return status || '';
  }
}

export function triggerText(trigger: WorkflowTrigger | string): string {
  switch (trigger) {
    case 'manual':
      return '手动';
    case 'scheduled':
      return '定时';
    case 'assistant':
      return '助理';
    default:
      return trigger || '';
  }
}

/**
 * 画布上的连线规则（与后端 validate_graph 同一套约束，提前在交互里挡住）：
 * - 不能连向开始节点；
 * - 非条件节点最多一条出边，新连线替换旧的；
 * - 条件节点的「真」「假」各自最多一条；
 * - 回答节点没有出边。
 */
export function canConnect(
  nodes: WorkflowNodeDTO[],
  edges: WorkflowEdgeDTO[],
  connection: { source?: string | null; target?: string | null; sourceHandle?: string | null },
): { ok: boolean; reason?: string } {
  const { source, target } = connection;
  if (!source || !target) return { ok: false, reason: '连线不完整' };
  if (source === target) return { ok: false, reason: '不能连到自己' };
  const sourceNode = nodes.find((n) => n.id === source);
  const targetNode = nodes.find((n) => n.id === target);
  if (!sourceNode || !targetNode) return { ok: false, reason: '节点不存在' };
  if (targetNode.type === 'start') return { ok: false, reason: '开始节点没有上游' };
  if (sourceNode.type === 'answer') return { ok: false, reason: '回答节点不能再往下连' };
  const handle = connection.sourceHandle || '';
  if (sourceNode.type === 'condition') {
    if (handle !== 'true' && handle !== 'false') {
      return { ok: false, reason: '条件节点要从「真」或「假」拉线' };
    }
    if (edges.some((e) => e.source === source && (e.source_handle || '') === handle)) {
      return { ok: false, reason: '这条分支已经连过了，先删掉旧连线' };
    }
    return { ok: true };
  }
  if (edges.some((e) => e.source === source && (e.source_handle || '') === handle)) {
    return { ok: false, reason: '每个节点只能有一个下一步' };
  }
  return { ok: true };
}

const VAR_RE = /\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}/g;

/** 从提示词里抽出引用了哪些变量（检查器用来提示「引用了还不存在的节点」） */
export function referencedVariables(text: string): string[] {
  const found = new Set<string>();
  let match: RegExpExecArray | null;
  const re = new RegExp(VAR_RE.source, 'g');
  while ((match = re.exec(text)) !== null) {
    found.add(match[1]);
  }
  return [...found];
}
