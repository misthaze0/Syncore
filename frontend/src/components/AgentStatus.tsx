/**
 * Agent 状态流转指示器
 *
 * 用步骤条形式呈现「主控 → 领域专员 → ...」的调用链，
 * 最后一个节点视为「当前执行中」，其余为「已完成」。
 */

import { agentLabel } from '@/types';

interface AgentStatusProps {
  /** Agent 调用链，如 ['main_agent', 'file_agent'] */
  chain: string[];
  /** 是否正在执行（决定是否高亮当前节点） */
  running?: boolean;
  /** 是否为紧凑模式（用于消息内联展示） */
  compact?: boolean;
}

export function AgentStatus({ chain, running = false, compact = false }: AgentStatusProps) {
  if (!chain.length) return null;

  return (
    <span className="agent-status">
      {chain.map((agent, index) => {
        const isLast = index === chain.length - 1;
        const state = isLast && running ? 'active' : 'done';
        return (
          <span key={`${agent}-${index}`} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <span className={`agent-step agent-step--${state}`}>
              <span className="agent-step__dot" />
              {agentLabel(agent)}
            </span>
            {!isLast && <span className="agent-arrow">→</span>}
          </span>
        );
      })}
      {!compact && running && <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>执行中…</span>}
    </span>
  );
}
