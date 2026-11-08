import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Database, FileText, RefreshCw, Search, Settings, Upload, Zap } from '@/components/icons';
import { PageShell } from '@/components/PageShell';
import { KnowledgeCatalog } from '@/components/knowledge/KnowledgeCatalog';
import { KnowledgeDocumentPreview } from '@/components/knowledge/KnowledgeDocumentPreview';
import { KnowledgeRetrievalTest } from '@/components/knowledge/KnowledgeRetrievalTest';
import { KnowledgeSyncPanel } from '@/components/knowledge/KnowledgeSyncPanel';
import { KnowledgeUploadPanel } from '@/components/knowledge/KnowledgeUploadPanel';
import {
  applyKnowledgeScan, getKnowledgeDocuments, getKnowledgeStatus, previewKnowledgeScan,
  reindexKnowledgeDocument, removeKnowledgeDocument, type KnowledgeCatalog as Catalog,
} from '@/lib/knowledgeApi';
import { confirmAction } from '@/stores/confirmStore';
import { useKnowledgeStore } from '@/stores/knowledgeStore';
import type { IndexDocument, IndexStatus, ScanPlan, ScanReport } from '@/types';
import '@/styles/knowledge.css';

const TABS = [{ id: 'documents', label: '文档资料', icon: FileText }, { id: 'test', label: '检索测试', icon: Search }, { id: 'manage', label: '管理与同步', icon: Settings }] as const;
type Tab = typeof TABS[number]['id'];
const messageOf = (error: unknown) => error instanceof Error ? error.message : String(error);

export function KnowledgePage() {
  const inputRef = useRef<HTMLInputElement>(null);
  const operation = useRef(false);
  const uploadsRevision = useKnowledgeStore(state => state.revision);
  const uploading = useKnowledgeStore(state => state.uploading);
  const [tab, setTab] = useState<Tab>('documents');
  const [status, setStatus] = useState<IndexStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [statusError, setStatusError] = useState('');
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [catalogError, setCatalogError] = useState('');
  const [query, setQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const [state, setState] = useState('all');
  const [sort, setSort] = useState('recent');
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<IndexDocument | null>(null);
  const [testSource, setTestSource] = useState<IndexDocument | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [plan, setPlan] = useState<ScanPlan | null>(null);
  const [report, setReport] = useState<ScanReport | null>(null);
  const [notice, setNotice] = useState<{ text: string; error?: boolean } | null>(null);
  const [scopeDocs, setScopeDocs] = useState<IndexDocument[] | null>(null);
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  const disabled = busy !== null || scanning || uploading;

  useEffect(() => { const timer = setTimeout(() => setDebouncedQuery(query), 250); return () => clearTimeout(timer); }, [query]);
  useEffect(() => {
    const controller = new AbortController();
    setStatusLoading(true); setStatusError('');
    void getKnowledgeStatus(controller.signal).then(data => { if (!controller.signal.aborted) setStatus(data); })
      .catch((error: unknown) => { if (!controller.signal.aborted) setStatusError(messageOf(error)); })
      .finally(() => { if (!controller.signal.aborted) setStatusLoading(false); });
    return () => controller.abort();
  }, [revision, uploadsRevision]);
  useEffect(() => {
    const controller = new AbortController();
    setCatalogLoading(true); setCatalogError('');
    void getKnowledgeDocuments({ q: debouncedQuery, state, sort, offset, limit: 20 }, controller.signal).then(data => {
      if (controller.signal.aborted) return;
      if (offset > 0 && !data.documents.length) { setOffset(Math.max(0, Math.floor((data.total - 1) / 20) * 20)); return; }
      setCatalog(data);
      setSelected(current => current ? data.documents.find(doc => doc.source === current.source) ?? current : null);
    }).catch((error: unknown) => { if (!controller.signal.aborted) setCatalogError(messageOf(error)); })
      .finally(() => { if (!controller.signal.aborted) setCatalogLoading(false); });
    return () => controller.abort();
  }, [debouncedQuery, state, sort, offset, revision, uploadsRevision]);
  useEffect(() => {
    if (tab !== 'manage') return;
    const controller = new AbortController();
    void getKnowledgeDocuments({ limit: 200 }, controller.signal).then(data => { if (!controller.signal.aborted) setScopeDocs(data.documents); })
      .catch(() => { /* The independent catalog error remains visible above. */ });
    return () => controller.abort();
  }, [tab, revision, uploadsRevision]);

  async function reindex(doc: IndexDocument) {
    if (operation.current || disabled) return;
    operation.current = true; setBusy(doc.source); setNotice(null);
    try {
      const result = await reindexKnowledgeDocument(doc.source);
      setNotice({ text: result.warnings.length ? result.warnings.join('；') : `「${doc.source}」已重新索引，共 ${result.chunks} 个文本块。` });
    } catch (error) { setNotice({ text: messageOf(error), error: true }); }
    finally { operation.current = false; setBusy(null); refresh(); }
  }
  async function remove(doc: IndexDocument) {
    if (operation.current || disabled) return;
    operation.current = true;
    try {
      if (!await confirmAction({ title: '从知识库移除这份文档？', description: `「${doc.source}」的 ${doc.chunks} 个文本块将从检索索引中移除，原文件保留。监听目录中的文件在后续同步时可能再次入库。`, confirmLabel: '移除文档', tone: 'warning' })) return;
      setBusy(doc.source); setNotice(null);
      await removeKnowledgeDocument(doc.source);
      if (selected?.source === doc.source) setSelected(null);
      if (testSource?.source === doc.source) setTestSource(null);
      setNotice({ text: `已从知识库移除「${doc.source}」。` });
    } catch (error) { setNotice({ text: messageOf(error), error: true }); }
    finally { operation.current = false; setBusy(null); refresh(); }
  }
  async function previewScan() {
    if (operation.current || disabled) return;
    operation.current = true; setTab('manage'); setScanning(true); setPlan(null); setReport(null); setNotice(null);
    try { setPlan(await previewKnowledgeScan()); }
    catch (error) { setNotice({ text: messageOf(error), error: true }); }
    finally { operation.current = false; setScanning(false); }
  }
  async function applyScan() {
    if (operation.current || disabled) return;
    operation.current = true; setScanning(true); setNotice(null);
    try { setReport(await applyKnowledgeScan()); setPlan(null); }
    catch (error) { setNotice({ text: messageOf(error), error: true }); }
    finally { operation.current = false; setScanning(false); refresh(); }
  }
  async function openSource(source: string) {
    const known = catalog?.documents.find(doc => doc.source === source);
    try {
      const doc = known ?? (await getKnowledgeDocuments({ q: source, limit: 200 })).documents.find(item => item.source === source);
      if (!doc) { setNotice({ text: '这个来源未在文档清单中登记，请检查索引状态。', error: true }); return; }
      setSelected(doc); setTab('documents');
    } catch (error) { setNotice({ text: messageOf(error), error: true }); }
  }
  function tabKeys(event: KeyboardEvent<HTMLButtonElement>, current: number) {
    let next = current;
    if (event.key === 'ArrowRight') next = (current + 1) % TABS.length;
    else if (event.key === 'ArrowLeft') next = (current + TABS.length - 1) % TABS.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = TABS.length - 1;
    else return;
    event.preventDefault(); setTab(TABS[next].id); document.getElementById(`kb-tab-${TABS[next].id}`)?.focus();
  }
  const summary = catalog?.summary;
  const health = statusError ? '无法读取' : statusLoading && !status ? '检查中' : status?.health?.state === 'healthy' ? '运行正常' : status ? '需要关注' : '未连接';

  return <PageShell title="知识库" icon={Database} className="knowledge-page" actions={<>
    <button type="button" className="btn btn--sm btn--ghost" disabled={statusLoading || catalogLoading} onClick={refresh} title="刷新文档清单与索引状态">{statusLoading || catalogLoading ? <span className="spinner" /> : <RefreshCw size={13} />}刷新</button>
    <button type="button" className="btn btn--sm btn--ghost" disabled={disabled || !status?.watch_dirs.length} onClick={() => void previewScan()} title={status?.watch_dirs.length ? '检查监听目录中的文件变更' : '尚未配置监听目录'}><Zap size={13} />同步目录</button>
    <button type="button" className="btn btn--sm btn--primary" onClick={() => inputRef.current?.click()}><Upload size={14} />添加文档</button>
  </>}>
    <div className="kb-overview" aria-label="知识库概览">
      <div><span>知识文档</span><strong>{summary?.documents.toLocaleString() ?? '—'}<small>份</small></strong>{Boolean(summary?.needs_attention) && <p>{summary?.needs_attention} 份待处理</p>}</div>
      <div><span>可检索片段</span><strong>{summary?.chunks.toLocaleString() ?? '—'}<small>块</small></strong></div>
      <div className="kb-overview__health"><span>索引状态</span><strong className={health === '运行正常' ? 'kb-healthy-text' : ''}><i className={`kb-health-dot ${health === '运行正常' ? 'is-healthy' : ''}`} />{health}</strong><p>{status?.busy || uploading || scanning || busy ? '正在处理资料' : status?.health?.issues[0] ?? ''}</p></div>
    </div>
    <KnowledgeUploadPanel inputRef={inputRef} status={status} />
    {statusError && <div className="kb-notice kb-notice--error" role="alert">索引状态读取失败：{statusError}</div>}
    {notice && <div className={`kb-notice ${notice.error ? 'kb-notice--error' : 'kb-notice--neutral'}`} role={notice.error ? 'alert' : 'status'}>{notice.text}<button type="button" className="kb-text-button" onClick={() => setNotice(null)}>关闭</button></div>}
    {busy && <div className="kb-notice kb-notice--neutral" role="status"><span className="spinner" />正在处理「{busy}」…</div>}
    <div className="kb-tabs" role="tablist" aria-label="知识库功能">{TABS.map((item, index) => <button type="button" key={item.id} id={`kb-tab-${item.id}`} role="tab" aria-selected={tab === item.id} aria-controls={`kb-panel-${item.id}`} tabIndex={tab === item.id ? 0 : -1} onClick={() => setTab(item.id)} onKeyDown={event => tabKeys(event, index)}><item.icon size={15} />{item.label}{item.id === 'documents' && summary && <span>{summary.documents}</span>}</button>)}</div>
    <div role="tabpanel" id="kb-panel-documents" aria-labelledby="kb-tab-documents" hidden={tab !== 'documents'}>
      {tab === 'documents' && <div className={`kb-document-layout ${selected ? 'has-preview' : ''}`}>
        <KnowledgeCatalog data={catalog} query={query} state={state} sort={sort} offset={offset} loading={catalogLoading} error={catalogError} selected={selected?.source ?? null} disabled={disabled} onQuery={value => { setQuery(value); setOffset(0); }} onState={value => { setState(value); setOffset(0); }} onSort={value => { setSort(value); setOffset(0); }} onOffset={setOffset} onSelect={setSelected} onReindex={doc => void reindex(doc)} onRemove={doc => void remove(doc)} onUpload={() => inputRef.current?.click()} onRetry={refresh} />
        {selected && <KnowledgeDocumentPreview key={selected.source} doc={selected} revision={revision + uploadsRevision} busy={disabled} onClose={() => setSelected(null)} onReindex={() => void reindex(selected)} onTest={() => { setTestSource(selected); setTab('test'); }} />}
      </div>}
    </div>
    <div role="tabpanel" id="kb-panel-test" aria-labelledby="kb-tab-test" hidden={tab !== 'test'}>
      <KnowledgeRetrievalTest source={testSource} status={status} onClearSource={() => setTestSource(null)} onOpenSource={source => void openSource(source)} />
    </div>
    <div role="tabpanel" id="kb-panel-manage" aria-labelledby="kb-tab-manage" hidden={tab !== 'manage'}>
      {tab === 'manage' && <KnowledgeSyncPanel status={status} docs={scopeDocs} plan={plan} report={report} scanning={scanning} disabled={disabled} onPreview={() => void previewScan()} onApply={() => void applyScan()} />}
    </div>
  </PageShell>;
}
export default KnowledgePage;
