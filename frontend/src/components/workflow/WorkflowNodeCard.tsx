import { memo } from 'react';
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';

import {
  CircleCheck,
  CircleX,
  Database,
  GitFork,
  Loader2,
  MessageSquare,
  Play,
  Sparkles,
  Wrench,
  type LucideIcon,
} from '@/components/icons';
import { nodeSummary } from '@/lib/workflow';
import type { WorkflowNodeDTO, WorkflowNodeType } from '@/types';

export interface WorkflowNodeData extends Record<string, unknown> {
  node: WorkflowNodeDTO;
  /** 运行中的节点（画布上高亮） */
  running?: boolean;
  /** 本次运行里这个节点已经走过了 */
  stepStatus?: 'succeeded' | 'failed';
}

export type WorkflowFlowNode = Node<WorkflowNodeData, 'workflow'>;

const ICONS: Record<WorkflowNodeType, LucideIcon> = {
  start: Play,
  llm: Sparkles,
  knowledge: Database,
  tool: Wrench,
  condition: GitFork,
  answer: MessageSquare,
};

export const WORKFLOW_NODE_ICONS = ICONS;

function WorkflowNodeCard({ data, selected }: NodeProps<WorkflowFlowNode>) {
  const { node, running, stepStatus } = data;
  const Icon = ICONS[node.type] ?? Sparkles;
  const state = running ? 'running' : stepStatus;

  return (
    <div
      className={[
        'wf-node',
        `wf-node--${node.type}`,
        selected ? 'is-selected' : '',
        state ? `is-${state}` : '',
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {node.type !== 'start' && (
        <Handle type="target" position={Position.Left} className="wf-handle wf-handle--in" />
      )}

      <div className="wf-node__head">
        <span className="wf-node__icon">
          <Icon size={13} />
        </span>
        <span className="wf-node__title" title={node.title}>
          {node.title || '未命名节点'}
        </span>
        {running && <Loader2 size={12} className="spin wf-node__spinner" />}
        {!running && stepStatus === 'succeeded' && <CircleCheck size={12} className="wf-node__ok" />}
        {!running && stepStatus === 'failed' && <CircleX size={12} className="wf-node__fail" />}
      </div>

      <p className="wf-node__summary">{nodeSummary(node)}</p>

      {node.type === 'condition' ? (
        <>
          <span className="wf-node__branch wf-node__branch--true">真</span>
          <span className="wf-node__branch wf-node__branch--false">假</span>
          <Handle
            id="true"
            type="source"
            position={Position.Right}
            className="wf-handle wf-handle--true"
            style={{ top: '38%' }}
          />
          <Handle
            id="false"
            type="source"
            position={Position.Right}
            className="wf-handle wf-handle--false"
            style={{ top: '74%' }}
          />
        </>
      ) : node.type !== 'answer' ? (
        <Handle type="source" position={Position.Right} className="wf-handle wf-handle--out" />
      ) : null}
    </div>
  );
}

export const WorkflowNodeCardMemo = memo(WorkflowNodeCard);
