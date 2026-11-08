import { confirmAction } from '@/stores/confirmStore';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import {
  Background,
  Controls,
  MarkerType,
  ReactFlow,
  ReactFlowProvider,
  applyEdgeChanges,
  applyNodeChanges,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type NodeChange,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import {
  ArrowLeft,
  Check,
  Loader2,
  Play,
  Plus,
  Trash,
  TriangleAlert,
  X,
} from '@/components/icons';
import { ConversationSkeleton } from '@/components/ConversationSkeleton';
import { NodeInspector } from '@/components/workflow/NodeInspector';
import {
  WORKFLOW_NODE_ICONS,
  WorkflowNodeCardMemo,
  type WorkflowFlowNode,
} from '@/components/workflow/WorkflowNodeCard';
import { WorkflowRunPanel } from '@/components/workflow/WorkflowRunPanel';
import {
  ApiError,
  cancelWorkflowRun,
  deleteWorkflow,
  fetchWorkflowDetail,
  fetchWorkflowMeta,
  fetchWorkflowRun,
  runWorkflow,
  updateWorkflow,
} from '@/lib/api';
import {
  NODE_TYPES,
  NODE_TYPE_META,
  canConnect,
  edgeId,
  newNode,
} from '@/lib/workflow';
import { toast } from '@/stores/toastStore';
import type {
  WorkflowEdgeDTO,
  WorkflowMetaResponse,
  WorkflowNodeDTO,
  WorkflowNodeType,
  WorkflowRunDTO,
} from '@/types';

const nodeTypes = { workflow: WorkflowNodeCardMemo };

function messageOf(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback;
}

function toFlowNodes(list: WorkflowNodeDTO[]): WorkflowFlowNode[] {
  return list.map((node) => ({
    id: node.id,
    type: 'workflow',
    position: { x: node.x || 0, y: node.y || 0 },
    data: { node },
  }));
}

function toFlowEdges(list: WorkflowEdgeDTO[]): Edge[] {
  return list.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    sourceHandle: edge.source_handle || undefined,
    type: 'smoothstep',
    className: edge.source_handle ? `wf-edge wf-edge--${edge.source_handle}` : 'wf-edge',
  }));
}

function isNodeType(value: string): value is WorkflowNodeType {
  return (NODE_TYPES as string[]).includes(value);
}

export function WorkflowEditorPage() {
  const { workflowId = '' } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const wantRun = Boolean((location.state as { run?: boolean } | null)?.run);

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [nodes, setNodes] = useState<WorkflowFlowNode[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [runs, setRuns] = useState<WorkflowRunDTO[]>([]);
  const [meta, setMeta] = useState<WorkflowMetaResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [missing, setMissing] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [error, setError] = useState('');
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [selectedId, setSelectedId] = useState('');
  const [sideTab, setSideTab] = useState<'config' | 'run'>(wantRun ? 'run' : 'config');
  const [runInput, setRunInput] = useState('');
  const [runBusy, setRunBusy] = useState(false);
  const [refreshingRuns, setRefreshingRuns] = useState(false);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [detail, metaResult] = await Promise.allSettled([
        fetchWorkflowDetail(workflowId, 30),
        fetchWorkflowMeta(),
      ]);
      if (detail.status === 'rejected') {
        if (detail.reason instanceof ApiError && detail.reason.status === 404) {
          setMissing(true);
        } else {
          setLoadError(messageOf(detail.reason, '工作流加载失败'));
        }
        return;
      }
      const data = detail.value;
      setName(data.workflow.name);
      setDescription(data.workflow.description ?? '');
      setNodes(toFlowNodes(data.workflow.nodes));
      setEdges(toFlowEdges(data.workflow.edges));
      setRuns(data.runs);
      setRunInput((current) => current || data.runs[0]?.input || '');
      setDirty(false);
      if (metaResult.status === 'fulfilled') {
        setMeta(metaResult.value);
      } else {
        setError('工具清单没拉到，工具节点暂时只能手填工具名');
      }
    } finally {
      setLoading(false);
    }
  }, [workflowId]);

  useEffect(() => {
    void load();
  }, [load]);

  const dtoNodes = useCallback(
    (): WorkflowNodeDTO[] =>
      nodes.map((item) => ({
        ...item.data.node,
        x: Math.round(item.position.x),
        y: Math.round(item.position.y),
      })),
    [nodes],
  );

  const dtoEdges = useCallback(
    (): WorkflowEdgeDTO[] =>
      edges.map((edge) => ({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        source_handle: edge.sourceHandle ?? '',
      })),
    [edges],
  );

  const save = useCallback(async (): Promise<boolean> => {
    if (saving) return false;
    if (!name.trim()) {
      setError('工作流名称不能为空');
      return false;
    }
    setSaving(true);
    setError('');
    try {
      await updateWorkflow(workflowId, {
        name: name.trim(),
        description,
        nodes: dtoNodes(),
        edges: dtoEdges(),
      });
      setDirty(false);
      return true;
    } catch (e) {
      setError(messageOf(e, '保存失败'));
      return false;
    } finally {
      setSaving(false);
    }
  }, [description, dtoEdges, dtoNodes, name, saving, workflowId]);

  const saveRef = useRef(save);
  saveRef.current = save;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (document.querySelector('[aria-modal="true"]')) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        if (dirtyRef.current) {
          void saveRef.current().then((ok) => {
            if (ok) toast('已保存', 'success');
          });
        }
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [dirty]);

  const refreshRuns = useCallback(async () => {
    setRefreshingRuns(true);
    try {
      const detail = await fetchWorkflowDetail(workflowId, 30);
      setRuns(detail.runs);
    } catch (e) {
      toast(messageOf(e, '运行记录刷新失败'), 'error');
    } finally {
      setRefreshingRuns(false);
    }
  }, [workflowId]);

  const activeRunId = runs.find((run) => run.status === 'running')?.run_id ?? '';

  useEffect(() => {
    if (!activeRunId) return;
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    const timer = window.setInterval(async () => {
      try {
        const resp = await fetchWorkflowRun(workflowId, activeRunId);
        setRuns((current) =>
          current.map((run) => (run.run_id === resp.run.run_id ? resp.run : run)),
        );
        if (resp.run.status !== 'running') void refreshRuns();
      } catch {
        /* 下一次轮询再试 */
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [activeRunId, workflowId, refreshRuns]);

  const latestRun = runs[0];
  const displayNodes = useMemo(() => {
    const steps = new Map(latestRun?.steps.map((step) => [step.node_id, step]) ?? []);
    return nodes.map((item) => {
      const step = steps.get(item.id);
      return {
        ...item,
        data: {
          ...item.data,
          stepStatus:
            step?.status === 'succeeded' || step?.status === 'failed' ? step.status : undefined,
        },
      };
    });
  }, [latestRun, nodes]);

  const onNodesChange = useCallback((changes: NodeChange<WorkflowFlowNode>[]) => {
    setNodes((current) => applyNodeChanges(changes, current));
    const removed = changes
      .filter((change): change is NodeChange<WorkflowFlowNode> & { type: 'remove'; id: string } =>
        change.type === 'remove',
      )
      .map((change) => change.id);
    if (removed.length > 0) {
      setEdges((current) =>
        current.filter(
          (edge) => !removed.includes(edge.source) && !removed.includes(edge.target),
        ),
      );
      setSelectedId((current) => (removed.includes(current) ? '' : current));
    }
    if (
      changes.some(
        (change) =>
          change.type === 'position' || change.type === 'add' || change.type === 'remove' || change.type === 'replace',
      )
    ) {
      setDirty(true);
    }
  }, []);

  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    setEdges((current) => applyEdgeChanges(changes, current));
    if (
      changes.some(
        (change) => change.type === 'add' || change.type === 'remove' || change.type === 'replace',
      )
    ) {
      setDirty(true);
    }
  }, []);

  const onConnect = useCallback(
    (connection: Connection) => {
      const check = canConnect(dtoNodes(), dtoEdges(), connection);
      if (!check.ok) {
        toast(check.reason || '这条连线不合法', 'warning');
        return;
      }
      const source = connection.source ?? '';
      const target = connection.target ?? '';
      const handle = connection.sourceHandle ?? '';
      setEdges((current) => [
        ...current,
        {
          id: edgeId(source, target, handle),
          source,
          target,
          sourceHandle: handle || undefined,
          type: 'smoothstep',
          className: handle ? `wf-edge wf-edge--${handle}` : 'wf-edge',
        },
      ]);
      setDirty(true);
    },
    [dtoEdges, dtoNodes],
  );

  const addNode = useCallback(
    (type: WorkflowNodeType, position?: { x: number; y: number }) => {
      if (type === 'start' && nodes.some((item) => item.data.node.type === 'start')) {
        toast('只能有一个开始节点', 'warning');
        return;
      }
      const sameType = nodes.filter((item) => item.data.node.type === type).length;
      const title = sameType > 0 ? `${NODE_TYPE_META[type].label} ${sameType + 1}` : NODE_TYPE_META[type].label;
      const fallback = {
        x: 200 + (nodes.length % 6) * 42,
        y: 140 + (nodes.length % 5) * 74,
      };
      const node = newNode(type, position ?? fallback, title);
      setNodes((current) => [
        ...current,
        { id: node.id, type: 'workflow', position: { x: node.x, y: node.y }, data: { node } },
      ]);
      setSelectedId(node.id);
      setSideTab('config');
      setDirty(true);
    },
    [nodes],
  );

  const updateNode = useCallback((updated: WorkflowNodeDTO) => {
    setNodes((current) =>
      current.map((item) =>
        item.id === updated.id ? { ...item, data: { ...item.data, node: updated } } : item,
      ),
    );
    setDirty(true);
  }, []);

  const deleteSelected = useCallback(() => {
    if (!selectedId) return;
    setNodes((current) => current.filter((item) => item.id !== selectedId));
    setEdges((current) =>
      current.filter((edge) => edge.source !== selectedId && edge.target !== selectedId),
    );
    setSelectedId('');
    setDirty(true);
  }, [selectedId]);

  const duplicateSelected = useCallback(() => {
    const current = nodes.find((item) => item.id === selectedId);
    if (!current || current.data.node.type === 'start') return;
    const source = current.data.node;
    const copy = newNode(
      source.type,
      { x: current.position.x + 48, y: current.position.y + 48 },
      `${source.title || NODE_TYPE_META[source.type].label} 副本`,
    );
    copy.config = { ...source.config };
    setNodes((list) => [
      ...list,
      { id: copy.id, type: 'workflow', position: { x: copy.x, y: copy.y }, data: { node: copy } },
    ]);
    setSelectedId(copy.id);
    setDirty(true);
  }, [nodes, selectedId]);

  const startRun = useCallback(async () => {
    if (activeRunId || runBusy) return;
    if (dirtyRef.current) {
      const ok = await saveRef.current();
      if (!ok) return;
    }
    setRunBusy(true);
    try {
      const res = await runWorkflow(workflowId, runInput);
      if (!res.ok || !res.run) {
        toast(res.reason || '没能启动运行', 'error');
        return;
      }
      setRuns((current) => [res.run!, ...current.filter((run) => run.run_id !== res.run!.run_id)]);
      setSideTab('run');
      toast('运行已开始', 'success');
    } catch (e) {
      toast(messageOf(e, '运行启动失败'), 'error');
    } finally {
      setRunBusy(false);
    }
  }, [activeRunId, runBusy, runInput, workflowId]);

  const cancelRun = useCallback(
    async (runId: string) => {
      try {
        const res = await cancelWorkflowRun(workflowId, runId);
        if (!res.ok) {
          toast(res.reason || '取消失败', 'error');
        } else {
          toast('已请求取消', 'success');
          void refreshRuns();
        }
      } catch (e) {
        toast(messageOf(e, '取消失败'), 'error');
      }
    },
    [refreshRuns, workflowId],
  );

  const removeWorkflow = useCallback(async () => {
    if (!await confirmAction({ title: '删除这个工作流？', description: '工作流及其运行记录将被永久删除，此操作无法撤销。' })) return;
    try {
      const res = await deleteWorkflow(workflowId);
      if (!res.ok) {
        toast(res.reason || '删除失败', 'error');
        return;
      }
      toast('工作流已删除', 'success');
      navigate('/workflows');
    } catch (e) {
      toast(messageOf(e, '删除失败'), 'error');
    }
  }, [navigate, workflowId]);

  const goBack = async () => {
    if (dirty && !await confirmAction({ title: '放弃未保存的修改？', description: '离开后，这次对工作流的修改将丢失。', confirmLabel: '放弃修改', cancelLabel: '继续编辑', tone: 'warning' })) return;
    navigate('/workflows');
  };

  const selectedNode = nodes.find((item) => item.id === selectedId)?.data.node ?? null;
  const workflowNodes = useMemo(() => nodes.map((item) => item.data.node), [nodes]);

  return (
    <div className="main wf-page">
      <header className="main__topbar wf-topbar">
        <div className="wf-topbar__left">
          <button type="button" className="sch-back" onClick={goBack} title="返回工作流列表">
            <ArrowLeft size={15} />
          </button>
          <input
            className="wf-name-input"
            value={name}
            maxLength={80}
            placeholder="工作流名称"
            aria-label="工作流名称"
            onChange={(e) => {
              setName(e.target.value);
              setDirty(true);
            }}
          />
          {dirty && <span className="wf-dirty" title="有未保存的修改" />}
        </div>

        <div className="wf-topbar__right">
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => {
              void save().then((ok) => {
                if (ok) toast('已保存', 'success');
              });
            }}
            disabled={saving || !dirty}
            title="保存 (Ctrl S)"
          >
            {saving ? <Loader2 size={13} className="spin" /> : <Check size={13} />}
            保存
          </button>
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => setSideTab('run')}
            title="打开运行面板"
          >
            <Play size={13} />
            运行
          </button>
          <button
            type="button"
            className="btn btn--sm btn--danger"
            onClick={() => void removeWorkflow()}
            disabled={Boolean(activeRunId)}
            title="删除"
          >
            <Trash size={13} />
          </button>
        </div>
      </header>

      {error && (
        <div className="wf-banner wf-banner--error">
          <TriangleAlert size={13} aria-hidden />
          <span>{error}</span>
          <button type="button" className="icon-btn" onClick={() => setError('')} title="关闭">
            <X size={12} />
          </button>
        </div>
      )}

      {loading && (
        <div className="wf-loading">
          <ConversationSkeleton rows={4} label="正在加载工作流…" />
        </div>
      )}

      {!loading && missing && (
        <div className="wf-missing">
          <div className="empty-hint">工作流不存在或已被删除</div>
          <button type="button" className="btn btn--sm" onClick={() => navigate('/workflows')}>
            返回工作流列表
          </button>
        </div>
      )}

      {!loading && !missing && loadError && (
        <div className="wf-missing">
          <div className="empty-hint">{loadError}</div>
          <button type="button" className="btn btn--sm" onClick={() => void load()}>
            重试
          </button>
        </div>
      )}

      {!loading && !missing && !loadError && (
        <div className="wf-editor">
          <aside className="wf-palette">
            <div className="wf-palette__title">节点</div>
            {NODE_TYPES.map((type) => {
              const Icon = WORKFLOW_NODE_ICONS[type];
              const disabled =
                type === 'start' && nodes.some((item) => item.data.node.type === 'start');
              return (
                <button
                  key={type}
                  type="button"
                  className={`wf-palette__item wf-palette__item--${type}`}
                  draggable={!disabled}
                  disabled={disabled}
                  title={NODE_TYPE_META[type].hint}
                  onDragStart={(e) => {
                    e.dataTransfer.setData('application/xjms-node', type);
                    e.dataTransfer.effectAllowed = 'move';
                  }}
                  onClick={() => addNode(type)}
                >
                  <span className="wf-palette__icon">
                    <Icon size={13} />
                  </span>
                  <span>{NODE_TYPE_META[type].label}</span>
                  <Plus size={11} className="wf-palette__plus" />
                </button>
              );
            })}
          </aside>

          <div className="wf-canvas">
            <ReactFlowProvider>
              <WorkflowCanvas
                nodes={displayNodes}
                edges={edges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onConnect={onConnect}
                onNodeClick={(nodeId) => {
                  setSelectedId(nodeId);
                  setSideTab('config');
                }}
                onPaneClick={() => setSelectedId('')}
                onAddNodeAt={(type, position) => addNode(type, position)}
                isConnectionValid={(connection) =>
                  canConnect(dtoNodes(), dtoEdges(), connection).ok
                }
              />
            </ReactFlowProvider>
          </div>

          <aside className="wf-side">
            <div className="wf-side__tabs" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={sideTab === 'config'}
                className={`wf-side__tab${sideTab === 'config' ? ' is-active' : ''}`}
                onClick={() => setSideTab('config')}
              >
                配置
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={sideTab === 'run'}
                className={`wf-side__tab${sideTab === 'run' ? ' is-active' : ''}`}
                onClick={() => setSideTab('run')}
              >
                运行
                {activeRunId && <span className="wf-side__dot" aria-hidden />}
              </button>
            </div>

            <div className="wf-side__body">
              {sideTab === 'config' ? (
                selectedNode ? (
                  <NodeInspector
                    node={selectedNode}
                    nodes={workflowNodes}
                    meta={meta}
                    onChange={updateNode}
                    onDelete={deleteSelected}
                    onDuplicate={duplicateSelected}
                  />
                ) : (
                  <div className="wf-info">
                    <div className="wf-info__title">工作流</div>
                    <label className="wf-field">
                      <span className="wf-field__label">描述</span>
                      <textarea
                        className="wf-textarea"
                        rows={4}
                        value={description}
                        maxLength={500}
                        placeholder="这个工作流做什么"
                        onChange={(e) => {
                          setDescription(e.target.value);
                          setDirty(true);
                        }}
                      />
                    </label>
                    <div className="wf-info__stats">
                      <span>{nodes.length} 个节点</span>
                      <span>{edges.length} 条连线</span>
                    </div>
                    <div className="wf-info__hint">在画布上点选一个节点来编辑它</div>
                  </div>
                )
              ) : (
                <WorkflowRunPanel
                  runs={runs}
                  input={runInput}
                  busy={runBusy}
                  refreshing={refreshingRuns}
                  onInputChange={setRunInput}
                  onRun={() => void startRun()}
                  onCancel={(runId) => void cancelRun(runId)}
                  onRefresh={() => void refreshRuns()}
                />
              )}
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}

function WorkflowCanvas({
  nodes,
  edges,
  onNodesChange,
  onEdgesChange,
  onConnect,
  onNodeClick,
  onPaneClick,
  onAddNodeAt,
  isConnectionValid,
}: {
  nodes: WorkflowFlowNode[];
  edges: Edge[];
  onNodesChange: (changes: NodeChange<WorkflowFlowNode>[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  onConnect: (connection: Connection) => void;
  onNodeClick: (nodeId: string) => void;
  onPaneClick: () => void;
  onAddNodeAt: (type: WorkflowNodeType, position: { x: number; y: number }) => void;
  isConnectionValid: (connection: Connection) => boolean;
}) {
  const { screenToFlowPosition } = useReactFlow();

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onConnect={onConnect}
      onNodeClick={(_, node) => onNodeClick(node.id)}
      onPaneClick={onPaneClick}
      isValidConnection={(connection) => isConnectionValid(connection as Connection)}
      onDragOver={(e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
      }}
      onDrop={(e) => {
        e.preventDefault();
        const type = e.dataTransfer.getData('application/xjms-node');
        if (!isNodeType(type)) return;
        onAddNodeAt(type, screenToFlowPosition({ x: e.clientX, y: e.clientY }));
      }}
      defaultEdgeOptions={{
        type: 'smoothstep',
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
      }}
      deleteKeyCode={['Backspace', 'Delete']}
      fitView
      fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
      minZoom={0.2}
      maxZoom={1.4}
      proOptions={{ hideAttribution: true }}
    >
      <Background gap={22} size={1.1} color="var(--border-strong)" />
      <Controls showInteractive={false} position="bottom-left" />
    </ReactFlow>
  );
}
