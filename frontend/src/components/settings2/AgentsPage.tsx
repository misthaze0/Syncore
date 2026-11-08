/**
 * 智能体页 —— 专员墙：有谁、能干什么、点进去改。
 *
 * 这一屏只管智能体，不管数字员工（后者在「工作空间」里，按专员模板雇佣）。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';

import {
  Bot,
  FileText,
  Globe,
  Monitor,
  Package,
  Plus,
  RefreshCw,
  Users,
} from '@/components/icons';
import { fetchAgents, fetchModels, fetchTools } from '@/lib/api';
import type {
  AgentDef,
  AgentsResponse,
  ModelOption,
  ToolInfo,
} from '@/types';
import { AgentEditor } from './AgentEditor';
import { PageHead, Section } from './kit';

/** 内置专员的图标按名字给（自定义的一律 Bot） */
const AGENT_ICON: Record<string, typeof Bot> = {
  supervisor: Users,
  pc_agent: Monitor,
  file_agent: FileText,
  software_agent: Package,
  browser_agent: Globe,
};

/** 「新建」走同一个编辑器：`editing` 存专员名，这个名字当 key 用 */
const NEW = '__new__';

export function AgentsPage() {
  const [data, setData] = useState<AgentsResponse | null>(null);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /** null = 列表；'__new__' = 新建；其余 = 编辑那个专员 */
  const [editing, setEditing] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // 工具表与模型候选是编辑器的附属数据：它们读不到不该让整页空掉
      const [agents, toolList, modelList] = await Promise.all([
        fetchAgents(),
        fetchTools()
          .then((r) => r.tools)
          .catch(() => [] as ToolInfo[]),
        fetchModels().catch(() => null),
      ]);
      setData(agents);
      setTools(toolList);
      if (modelList) setModels(modelList.models);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const upsertAgent = useCallback((saved: AgentDef) => {
    setData((prev) => {
      if (!prev) return prev;
      const agents = prev.agents.some((a) => a.name === saved.name)
        ? prev.agents.map((a) => (a.name === saved.name ? saved : a))
        : [...prev.agents, saved];
      agents.sort(
        (a, b) =>
          Number(b.builtin) - Number(a.builtin) ||
          Number(b.resident) - Number(a.resident) ||
          a.name.localeCompare(b.name),
      );
      return { ...prev, agents, custom_count: agents.filter((a) => !a.builtin).length };
    });
  }, []);

  const dropAgent = useCallback((name: string) => {
    setData((prev) =>
      prev
        ? {
            ...prev,
            agents: prev.agents.filter((a) => a.name !== name),
            custom_count: Math.max(0, prev.custom_count - 1),
          }
        : prev,
    );
  }, []);

  const editingAgent =
    editing === null || editing === NEW
      ? null
      : (data?.agents.find((a) => a.name === editing) ?? null);

  const agents = data?.agents ?? [];
  const atMax = data ? data.custom_count >= data.max_custom : false;

  return (
    <div className="tset tset-page tset-page--wide">
      {error && <div className="alert alert--error">{error}</div>}

      {editing !== null ? (
        <AgentEditor
          key={editing}
          agent={editingAgent}
          tools={tools}
          models={models}
          globalModel={data?.global_model ?? ''}
          globalMaxSteps={data?.global_max_steps ?? 8}
          onSaved={upsertAgent}
          onDeleted={dropAgent}
          onBack={() => setEditing(null)}
        />
      ) : (
        <>
          <PageHead title="智能体" />

          <div className="set-filters">
            <span className="set-filters__meta">
              自定义 {data?.custom_count ?? 0}/{data?.max_custom ?? 0}
            </span>
            <div className="set-filters__right">
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => void load()}
                disabled={loading}
                aria-label="刷新"
              >
                {loading ? <span className="spinner" /> : <RefreshCw size={13} />}
              </button>
              <button
                type="button"
                className="btn btn--sm btn--primary"
                onClick={() => setEditing(NEW)}
                disabled={atMax}
              >
                <Plus size={13} /> 新建专员
              </button>
            </div>
          </div>

          <Section title="专员列表">
            {loading && agents.length === 0 ? (
              <div className="empty-hint">
                <span className="spinner" />
              </div>
            ) : (
              <div className="ag-grid">
                {agents.map((agent) => (
                  <AgentCard
                    key={agent.name}
                    agent={agent}
                    fallback={data?.fallback ?? ''}
                    onOpen={() => setEditing(agent.name)}
                  />
                ))}
                <button
                  type="button"
                  className="ag-card ag-card--new"
                  onClick={() => setEditing(NEW)}
                  disabled={atMax}
                >
                  <Plus size={18} />
                  <span>新建专员</span>
                </button>
              </div>
            )}
          </Section>
        </>
      )}
    </div>
  );
}

/** 专员卡片：身份一眼可见，配置差异收在底部一行 */
function AgentCard({
  agent,
  fallback,
  onOpen,
}: {
  agent: AgentDef;
  fallback: string;
  onOpen: () => void;
}) {
  const Icon = AGENT_ICON[agent.name] ?? Bot;
  const meta = useMemo(() => {
    const parts: string[] = [];
    parts.push(agent.model || '跟随全局');
    parts.push(`${agent.effective_tools?.length ?? 0} 工具`);
    if (agent.max_steps) parts.push(`${agent.max_steps} 步`);
    if (agent.retrieval) parts.push('检索');
    return parts.join(' · ');
  }, [agent]);

  return (
    <button
      type="button"
      className={`ag-card${agent.enabled ? '' : ' ag-card--off'}`}
      onClick={onOpen}
    >
      <span className="ag-card__head">
        <span className="ag-card__icon">
          <Icon size={17} />
        </span>
        <span className="ag-card__main">
          <span className="ag-card__name clamp1">{agent.display_name || agent.name}</span>
          <code className="ag-card__id">{agent.name}</code>
        </span>
        {agent.resident && <span className="ag-card__pin">常驻</span>}
        {!agent.builtin && !agent.resident && <span className="ag-card__pin">自定义</span>}
        {agent.name === fallback && !agent.resident && !agent.builtin && (
          <span className="ag-card__pin">兜底</span>
        )}
      </span>

      <span className="ag-card__desc clamp1">{agent.responsibility || agent.domain || '—'}</span>

      {agent.capabilities.length > 0 && (
        <span className="ag-card__chips">
          {agent.capabilities.map((c) => (
            <span key={c} className="ag-chip">
              {c}
            </span>
          ))}
        </span>
      )}

      <span className="ag-card__meta">{meta}</span>
    </button>
  );
}

export default AgentsPage;
