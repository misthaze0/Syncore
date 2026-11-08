import { confirmAction } from '@/stores/confirmStore';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import {
  CircleCheck,
  CircleSlash,
  CircleX,
  Hourglass,
  Loader2,
  Network,
  Play,
  Plus,
  RefreshCw,
  Trash,
  TriangleAlert,
  X,
  type LucideIcon,
} from '@/components/icons';
import { ConversationSkeleton } from '@/components/ConversationSkeleton';
import { EmptyState } from '@/components/EmptyState';
import { PageShell } from '@/components/PageShell';
import { PageSearch } from '@/components/PageSearch';
import { createWorkflow, deleteWorkflow, fetchWorkflows } from '@/lib/api';
import { formatLocalDateTime } from '@/lib/schedule';
import { defaultGraph, runStatusText } from '@/lib/workflow';
import { isTopmostModal, useModalFocus } from '@/hooks/useModalFocus';
import { toast } from '@/stores/toastStore';
import type { WorkflowDTO, WorkflowLastStatus, WorkflowNodeType } from '@/types';

const NODE_COUNTS: WorkflowNodeType[] = ['llm', 'knowledge', 'tool', 'condition'];

const NODE_SHORT: Record<WorkflowNodeType, string> = {
  start: '开始',
  llm: '模型',
  knowledge: '知识库',
  tool: '工具',
  condition: '条件',
  answer: '回答',
};

function statusIcon(status: WorkflowLastStatus): LucideIcon {
  switch (status) {
    case 'running':
      return Loader2;
    case 'succeeded':
      return CircleCheck;
    case 'failed':
      return CircleX;
    case 'cancelled':
      return CircleSlash;
    case 'orphaned':
      return TriangleAlert;
    default:
      return Hourglass;
  }
}

function messageOf(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback;
}

export function WorkflowsPage() {
  const navigate = useNavigate();
  const [workflows, setWorkflows] = useState<WorkflowDTO[]>([]);
  const [storage, setStorage] = useState<'ok' | 'unavailable'>('ok');
  const [storageError, setStorageError] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [query, setQuery] = useState('');
  const [busyId, setBusyId] = useState('');
  const [creating, setCreating] = useState(false);

  const load = useCallback(async (silent: boolean) => {
    if (!silent) setRefreshing(true);
    try {
      const resp = await fetchWorkflows();
      if (resp.storage === 'unavailable') {
        setStorage('unavailable');
        setStorageError(resp.storage_error || '工作流暂时查不了');
      } else {
        setStorage('ok');
        setStorageError('');
        setWorkflows(resp.workflows);
      }
    } catch (e) {
      setStorage('unavailable');
      setStorageError(messageOf(e, '工作流暂时查不了'));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load(true);
  }, [load]);

  const hasRunning = workflows.some((item) => item.last_status === 'running');
  useEffect(() => {
    if (!hasRunning) return;
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    const timer = window.setInterval(() => void load(true), 4000);
    return () => window.clearInterval(timer);
  }, [hasRunning, load]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return workflows;
    return workflows.filter(
      (item) =>
        item.name.toLowerCase().includes(needle) ||
        (item.description ?? '').toLowerCase().includes(needle),
    );
  }, [workflows, query]);

  const remove = async (workflow: WorkflowDTO) => {
    if (!await confirmAction({ title: '删除这个工作流？', description: `「${workflow.name}」及其运行记录将被永久删除，此操作无法撤销。` })) return;
    setBusyId(workflow.workflow_id);
    try {
      const res = await deleteWorkflow(workflow.workflow_id);
      if (!res.ok) {
        toast(res.reason || '删除失败', 'error');
        return;
      }
      setWorkflows((prev) => prev.filter((item) => item.workflow_id !== workflow.workflow_id));
    } catch (e) {
      toast(messageOf(e, '删除失败'), 'error');
    } finally {
      setBusyId('');
    }
  };

  const actions = (
    <>
      <button
        type="button"
        className="btn btn--sm btn--ghost"
        onClick={() => void load(false)}
        disabled={refreshing}
        title="刷新"
      >
        <RefreshCw size={13} className={refreshing ? 'spin' : undefined} />
        刷新
      </button>
      <button type="button" className="btn btn--sm btn--primary" onClick={() => setCreating(true)}>
        <Plus size={13} />
        新建工作流
      </button>
    </>
  );

  return (
    <PageShell
      title="工作流"
      icon={Network}
      actions={actions}
      toolbar={
        <>
          <span className="page-toolbar__label">全部工作流 <span className="page-count">{workflows.length}</span></span>
          <PageSearch value={query} onChange={setQuery} placeholder="搜索工作流" />
        </>
      }
    >
      {storage === 'unavailable' && (
        <div className="subtask-panel subtask-panel--unavailable">
          <TriangleAlert size={14} aria-hidden />
          <span>{storageError}</span>
        </div>
      )}

      {loading && <ConversationSkeleton rows={3} label="正在加载工作流…" />}

      {!loading && storage === 'ok' && visible.length === 0 && (
        <EmptyState
          fill
          icon={Network}
          title={workflows.length === 0 ? '还没有工作流' : '没有匹配的工作流'}
          action={workflows.length === 0 && (
            <button type="button" className="btn btn--primary btn--sm" onClick={() => setCreating(true)}>
              <Plus size={13} />
              新建工作流
            </button>
          )}
        />
      )}

      {!loading && storage === 'ok' && visible.length > 0 && (
        <div className="wf-grid">
          {visible.map((workflow) => {
            const Icon = statusIcon(workflow.last_status ?? 'succeeded');
            const counts = NODE_COUNTS.map((type) => ({
              type,
              count: workflow.nodes.filter((node) => node.type === type).length,
            })).filter((item) => item.count > 0);
            return (
              <article key={workflow.workflow_id} className="wf-card">
                <button
                  type="button"
                  className="wf-card__main"
                  onClick={() => navigate(`/workflows/${workflow.workflow_id}`)}
                >
                  <div className="wf-card__top">
                    <span className="wf-card__icon">
                      <Network size={16} />
                    </span>
                    <span className="wf-card__name">{workflow.name}</span>
                  </div>
                  <p className="wf-card__desc">
                    {workflow.description || `${workflow.nodes.length} 个节点`}
                  </p>
                  <div className="wf-card__chips">
                    {counts.map((item) => (
                      <span key={item.type} className={`wf-chip wf-chip--${item.type}`}>
                        {NODE_SHORT[item.type]} {item.count}
                      </span>
                    ))}
                    {counts.length === 0 && <span className="wf-chip">空画布</span>}
                  </div>
                </button>

                <div className="wf-card__foot">
                  <span className="wf-card__status">
                    {workflow.last_status ? (
                      <>
                        <Icon
                          size={12}
                          className={workflow.last_status === 'running' ? 'spin' : undefined}
                          aria-hidden
                        />
                        {runStatusText(workflow.last_status)}
                      </>
                    ) : (
                      <span className="wf-card__never">从未运行</span>
                    )}
                    {workflow.last_run_at && (
                      <em>{formatLocalDateTime(workflow.last_run_at)}</em>
                    )}
                  </span>
                  <span className="wf-card__ctl">
                    <button
                      type="button"
                      className="icon-btn"
                      title="打开运行面板"
                      onClick={() => navigate(`/workflows/${workflow.workflow_id}`, { state: { run: true } })}
                    >
                      <Play size={13} />
                    </button>
                    <button
                      type="button"
                      className="icon-btn icon-btn--danger"
                      title="删除"
                      disabled={busyId === workflow.workflow_id}
                      onClick={() => void remove(workflow)}
                    >
                      {busyId === workflow.workflow_id ? (
                        <Loader2 size={13} className="spin" />
                      ) : (
                        <Trash size={13} />
                      )}
                    </button>
                  </span>
                </div>
              </article>
            );
          })}
        </div>
      )}

      {creating && (
        <CreateWorkflowDialog
          onClose={() => setCreating(false)}
          onCreated={(workflow) => {
            setCreating(false);
            navigate(`/workflows/${workflow.workflow_id}`);
          }}
        />
      )}
    </PageShell>
  );
}

function CreateWorkflowDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (workflow: WorkflowDTO) => void;
}) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalFocus(dialogRef);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting && isTopmostModal(dialogRef.current)) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, submitting]);

  const submit = async () => {
    if (submitting) return;
    if (!name.trim()) {
      setError('给工作流起个名字');
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      const graph = defaultGraph();
      const workflow = await createWorkflow({
        name: name.trim(),
        description: description.trim(),
        nodes: graph.nodes,
        edges: graph.edges,
      });
      onCreated(workflow);
    } catch (e) {
      setError(messageOf(e, '创建失败'));
      setSubmitting(false);
    }
  };

  return (
    <div
      className="modal-mask"
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !submitting) onClose();
      }}
    >
      <div ref={dialogRef} className="modal wf-create" role="dialog" aria-modal="true" aria-labelledby="wf-create-title" tabIndex={-1}>
        <div className="modal__head">
          <div className="modal__title" id="wf-create-title" style={{ flex: 1 }}>
            新建工作流
          </div>
          <button type="button" className="sch-editor__close" onClick={onClose} title="关闭">
            <X size={15} />
          </button>
        </div>
        <div className="modal__body wf-create__body">
          <input
            className="wf-input wf-input--lg"
            value={name}
            maxLength={80}
            placeholder="工作流名称"
            aria-label="工作流名称"
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void submit();
            }}
          />
          <textarea
            className="wf-textarea"
            rows={3}
            value={description}
            maxLength={500}
            placeholder="描述（可选）"
            aria-label="描述"
            onChange={(e) => setDescription(e.target.value)}
          />
          {error && <div className="wf-error">{error}</div>}
        </div>
        <div className="modal__foot">
          <button type="button" className="btn" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button type="button" className="btn btn--primary" onClick={() => void submit()} disabled={submitting}>
            {submitting ? <Loader2 size={13} className="spin" /> : <Plus size={13} />}
            创建
          </button>
        </div>
      </div>
    </div>
  );
}
