import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import type { Editor } from '@tiptap/react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Check, ChevronDown, ChevronRight, Copy, Download, Ellipsis, FileText, HardDrive, History, ListTree, Maximize2, Minimize2, PanelRight, Presentation, Printer, RefreshCw, Sparkles, Star, Table2, Trash2, TriangleAlert, X } from 'lucide-react';

import { OfficeAssistant } from '@/components/office/OfficeAssistant';
import type { OfficeAssistantHandle } from '@/components/office/OfficeAssistant';
import { OfficeHistoryDialog, OfficeTemplateDialog } from '@/components/office/OfficeDialogs';
import { OfficeEditor } from '@/components/office/OfficeEditor';
import { OfficeHome, OfficeHomeAssistant } from '@/components/office/OfficeHome';
import { OfficeLibrary } from '@/components/office/OfficeLibrary';
import { OFFICE_KIND_LABELS } from '@/lib/officeTemplates';
import { officeApi } from '@/lib/officeApi';
import { confirmAction } from '@/stores/confirmStore';
import { useOfficeStore } from '@/stores/officeStore';
import { toast } from '@/stores/toastStore';
import { useUiStore } from '@/stores/uiStore';
import type { OfficeSelection } from '@/types/office';

function readWidth(): number { try { return Math.max(300, Math.min(560, Number(localStorage.getItem('xjms.office.assistantWidth')) || 350)); } catch { return 350; } }

export function OfficePage() {
  const state = useOfficeStore();
  const current = state.document;
  const navigate = useNavigate();
  const location = useLocation();
  const libraryOpen = useUiStore(s => s.officeLibraryOpen);
  const setLibraryOpen = useUiStore(s => s.setOfficeLibraryOpen);
  const assistantOpen = useUiStore(s => s.officeAssistantOpen);
  const setAssistantOpen = useUiStore(s => s.setOfficeAssistantOpen);
  const [outlineOpen, setOutlineOpen] = useState(false);
  const focusMode = useUiStore(s => s.officeFocused);
  const setFocusMode = useUiStore(s => s.setOfficeFocused);
  const [templates, setTemplates] = useState(false);
  const [history, setHistory] = useState(false);
  const [menu, setMenu] = useState<'export' | 'more' | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [selection, setSelection] = useState<OfficeSelection | null>(null);
  const [assistantWidth, setAssistantWidth] = useState(readWidth);
  const [dragging, setDragging] = useState(false);
  const [importing, setImporting] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [pendingAsk, setPendingAsk] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const rootRef = useRef<HTMLElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const assistantRef = useRef<OfficeAssistantHandle>(null);
  const dragDepth = useRef(0);
  const loadedRef = useRef(false);

  useEffect(() => {
    try { localStorage.setItem('xjms.mode', 'office'); } catch { /* Session only. */ }
    if (!loadedRef.current) { loadedRef.current = true; void useOfficeStore.getState().load(); }
    return () => { useOfficeStore.getState().stopAssistant(); void useOfficeStore.getState().save(); };
  }, []);

  useEffect(() => {
    if (!state.dirty || state.conflict || state.assistantAbort) return;
    const timer = window.setTimeout(() => { void useOfficeStore.getState().save(); }, 1000);
    return () => window.clearTimeout(timer);
  }, [state.dirty, state.editVersion, state.conflict, state.assistantAbort]);

  useEffect(() => {
    const flush = () => { void useOfficeStore.getState().save(); };
    const visibility = () => { if (window.document.visibilityState === 'hidden') flush(); };
    const beforeUnload = (event: BeforeUnloadEvent) => {
      const current = useOfficeStore.getState();
      if (current.dirty && !current.draftStored) { event.preventDefault(); event.returnValue = ''; }
    };
    window.addEventListener('pagehide', flush);
    window.addEventListener('beforeunload', beforeUnload);
    window.document.addEventListener('visibilitychange', visibility);
    return () => { window.removeEventListener('pagehide', flush); window.removeEventListener('beforeunload', beforeUnload); window.document.removeEventListener('visibilitychange', visibility); };
  }, []);

  useEffect(() => { setSelection(null); setHistory(false); setMenu(null); }, [current?.id]);
  useEffect(() => {
    const compact = window.matchMedia('(max-width: 760px)');
    const narrow = window.matchMedia('(max-width: 760px)');
    const collapseLibrary = () => { if (compact.matches) setLibraryOpen(false); };
    const collapsePanels = () => { if (narrow.matches) { setLibraryOpen(false); setAssistantOpen(false); } };
    compact.addEventListener('change', collapseLibrary);
    narrow.addEventListener('change', collapsePanels);
    collapseLibrary();
    collapsePanels();
    return () => { compact.removeEventListener('change', collapseLibrary); narrow.removeEventListener('change', collapsePanels); };
  }, []);
  useEffect(() => {
    if (!pendingAsk || !current || !editor || !assistantRef.current) return;
    assistantRef.current.ask(pendingAsk);
    setPendingAsk(null);
  }, [pendingAsk, current, editor]);

  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (window.document.querySelector('[aria-modal="true"]')) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        void useOfficeStore.getState().save('checkpoint').then(ok => { if (ok && useOfficeStore.getState().document) toast('文档和版本已保存', 'success'); });
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'n') { event.preventDefault(); void useOfficeStore.getState().create(); }
      if (event.key === 'Escape') { setMenu(null); setFocusMode(false); }
    };
    const create = () => { void useOfficeStore.getState().create(); };
    const library = () => {
      const { officeFocused, officeLibraryOpen } = useUiStore.getState();
      setLibraryOpen(officeFocused || !officeLibraryOpen);
      setFocusMode(false);
      if (window.innerWidth <= 760) setAssistantOpen(false);
    };
    const online = () => { if (useOfficeStore.getState().dirty) void useOfficeStore.getState().save(); };
    window.addEventListener('keydown', key);
    window.addEventListener('xjms:office-new', create);
    window.addEventListener('xjms:office-library-toggle', library);
    window.addEventListener('online', online);
    return () => { window.removeEventListener('keydown', key); window.removeEventListener('xjms:office-new', create); window.removeEventListener('xjms:office-library-toggle', library); window.removeEventListener('online', online); };
  }, []);

  useEffect(() => {
    if (!menu) return;
    const outside = (event: PointerEvent) => { if (!menuRef.current?.contains(event.target as Node)) setMenu(null); };
    window.document.addEventListener('pointerdown', outside);
    return () => window.document.removeEventListener('pointerdown', outside);
  }, [menu]);

  const ready = useCallback((value: Editor | null) => setEditor(value), []);
  const clearSelection = useCallback(() => setSelection(null), []);
  const ask = useCallback((prompt: string) => { setAssistantOpen(true); setFocusMode(false); if (window.innerWidth <= 760) setLibraryOpen(false); assistantRef.current?.ask(prompt); }, []);
  const openSettings = (tab = 'models') => navigate(`/settings/${tab}`, { state: { background: location } });
  const showAssistant = () => { setAssistantOpen(focusMode || !assistantOpen); setFocusMode(false); if (window.innerWidth <= 760) setLibraryOpen(false); };

  const importFile = async (file: File) => {
    if (importing) return;
    if (file.size > 5 * 1024 * 1024) { toast('文件超过 5 MB，请拆分后导入', 'warning'); return; }
    useOfficeStore.getState().stopAssistant();
    if (!await useOfficeStore.getState().save()) return;
    setImporting(true);
    useOfficeStore.setState({ opening: true });
    try {
      const result = await officeApi.import(file);
      if (!await useOfficeStore.getState().save()) { await useOfficeStore.getState().load(); return; }
      useOfficeStore.getState().adopt(result.document);
      toast(`已导入 ${file.name}`, 'success');
      if (result.warnings.length) toast(result.warnings.join(' '), 'info');
    } catch (error) { toast(error instanceof Error ? error.message : '导入失败', 'error'); }
    finally { setImporting(false); useOfficeStore.setState({ opening: false }); if (fileRef.current) fileRef.current.value = ''; }
  };

  const exportFile = async (format: string) => {
    setMenu(null);
    if (!current || exporting) return;
    if (!await useOfficeStore.getState().save()) { toast('请先保存文档，再导出', 'warning'); return; }
    if (format === 'pdf') { window.print(); return; }
    setExporting(true);
    try { await officeApi.export(current.id, current.title, format); toast('文档已导出', 'success'); }
    catch (error) { toast(error instanceof Error ? error.message : '导出失败', 'error'); }
    finally { setExporting(false); }
  };

  const startAI = async (prompt: string) => {
    const previous = useOfficeStore.getState().document?.id;
    await useOfficeStore.getState().create();
    if (useOfficeStore.getState().document?.id !== previous && useOfficeStore.getState().document) { setPendingAsk(prompt); setAssistantOpen(true); }
  };

  const rememberWidth = (value: number) => { try { localStorage.setItem('xjms.office.assistantWidth', String(value)); } catch { /* Session only. */ } };
  const savedLabel = state.conflict ? '保存存在冲突' : state.saveError ? (state.draftStored ? '保存失败 · 草稿已保留' : '尚未保存') : state.saving ? '正在保存…' : state.dirty ? '编辑中…' : '已保存';

  return <main ref={rootRef} className={`office-workspace${focusMode ? ' is-focused' : ''}${dragging ? ' is-resizing' : ''}`} data-library={libraryOpen} data-assistant={assistantOpen} style={{ '--office-assistant-width': `${assistantWidth}px` } as CSSProperties}
    onDragEnter={event => { if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); dragDepth.current += 1; setDragOver(true); } }}
    onDragOver={event => { if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; } }}
    onDragLeave={event => { if (event.dataTransfer.types.includes('Files')) { dragDepth.current -= 1; if (dragDepth.current <= 0) setDragOver(false); } }}
    onDrop={event => { if (!event.dataTransfer.files.length) return; event.preventDefault(); setDragOver(false); dragDepth.current = 0; if (event.dataTransfer.files.length > 1) toast('请一次导入一份文件', 'info'); void importFile(event.dataTransfer.files[0]); }}>
    <input ref={fileRef} type="file" className="office-file-input" accept=".docx,.xlsx,.pptx,.md,.markdown,.txt,.html,.htm,.csv" onChange={event => { const file = event.target.files?.[0]; if (file) void importFile(file); }} />
    <div className="office-body">
      <OfficeLibrary onClose={() => setLibraryOpen(false)} />
      {libraryOpen && <button className="office-library-scrim" aria-label="关闭文档库" onClick={() => setLibraryOpen(false)} />}
      <section className="office-main">
        <div className="office-document-bar"><div>{current ? <button className="office-breadcrumb" onClick={() => { setFocusMode(false); void state.home(); }}>文档</button> : <span className="office-breadcrumb">文档</span>}{current && <ChevronRight size={13} className="office-breadcrumb-chevron" />}{current ? <><span className={`office-kind-badge office-kind-badge--${current.kind}`}>{current.kind === 'spreadsheet' ? <Table2 size={13} /> : current.kind === 'presentation' ? <Presentation size={13} /> : <FileText size={13} />}{OFFICE_KIND_LABELS[current.kind]}</span><button className={`office-tool office-favorite${current.favorite ? ' is-active' : ''}`} title={current.favorite ? '取消收藏' : '收藏文档'} aria-label={current.favorite ? '取消收藏文档' : '收藏文档'} onClick={() => state.update({ favorite: !current.favorite })}><Star size={15} fill={current.favorite ? 'currentColor' : 'none'} /></button><button className={`office-save-status${state.saveError || state.conflict ? ' has-error' : ''}`} title="手动保存 (Ctrl S)" onClick={() => void state.save('checkpoint')}><span>{state.saving ? <span className="spinner" /> : state.saveError || state.conflict ? <TriangleAlert size={12} /> : state.dirty ? <span className="office-unsaved-dot" /> : <Check size={13} />}</span>{savedLabel}</button></> : null}</div><div className="office-document-tools">{current && <button className={`office-tool office-focus-tool${focusMode ? ' is-active' : ''}`} aria-label={focusMode ? '退出专注模式' : '进入专注模式'} title={focusMode ? '退出专注模式 (Esc)' : '专注模式'} onClick={() => setFocusMode(!focusMode)}>{focusMode ? <Minimize2 size={16} /> : <Maximize2 size={16} />}</button>}{current && <><button className={`office-tool${outlineOpen ? ' is-active' : ''}`} title="文档大纲" aria-label="切换文档大纲" onClick={() => setOutlineOpen(value => !value)}><ListTree size={16} /></button><button className="office-tool" title="版本历史" aria-label="版本历史" onClick={async () => { await state.save(); setHistory(true); }}><History size={16} /></button><div className="office-document-menus" ref={menuRef}><button className="office-tool" aria-label="更多文档操作" title="更多操作" aria-expanded={menu === 'more'} onClick={() => setMenu(value => value === 'more' ? null : 'more')}><Ellipsis size={18} /></button><button className="office-export-button" disabled={exporting} onClick={() => setMenu(value => value === 'export' ? null : 'export')} aria-expanded={menu === 'export'}><Download size={14} /><span>{exporting ? '导出中…' : '导出'}</span><ChevronDown size={12} /></button>{menu === 'export' && <div className="office-dropdown"><span className="office-section-label">导出为</span>{[['docx', 'Word 文档', FileText], ['pdf', 'PDF / 打印', Printer], ['md', 'Markdown', FileText], ['txt', '纯文本', FileText], ['html', 'HTML 网页', FileText], ['xlsx', 'Excel 表格', Table2], ['pptx', 'PowerPoint 演示', Presentation]].map(([format, label, Icon]) => { const FormatIcon = Icon as typeof FileText; return <button key={String(format)} onClick={() => void exportFile(String(format))}><FormatIcon size={15} /><span>{String(label)}</span><small>.{String(format)}</small></button>; })}</div>}{menu === 'more' && <div className="office-dropdown"><button className="office-focus-menu-item" onClick={() => { setMenu(null); setFocusMode(!focusMode); }}>{focusMode ? <Minimize2 size={15} /> : <Maximize2 size={15} />}{focusMode ? '退出专注模式' : '专注模式'}</button><button onClick={() => { setMenu(null); void state.duplicate(); }}><Copy size={15} />创建副本</button><button onClick={() => { setMenu(null); void exportFile('pdf'); }}><Printer size={15} />打印文档</button><hr /><button className="is-danger" onClick={() => { setMenu(null); void state.trash(); }}><Trash2 size={15} />移至回收站</button></div>}</div><span className="office-docbar-divider" /></>}<button className={`office-assistant-toggle${assistantOpen && !focusMode ? ' is-active' : ''}`} aria-pressed={assistantOpen && !focusMode} title="切换 Syncore 助手" onClick={showAssistant}><Sparkles size={15} /><span>Syncore 助手</span>{state.assistantAbort ? <span className="office-live-dot" /> : <PanelRight size={13} />}</button></div></div>
        {state.error && <div className="office-notice is-error" role="alert"><TriangleAlert size={15} /><span>{state.error}</span><button onClick={() => void state.load()}><RefreshCw size={13} />重试</button></div>}
        {state.conflict && <div className="office-notice is-error" role="alert"><TriangleAlert size={15} /><span>这份文档已在另一个窗口更新。本地修改已保留。</span><button onClick={() => void state.duplicate()}>保留为副本</button><button onClick={async () => { if (await confirmAction({ title: '加载已保存的版本？', description: '当前窗口未保存的改动会被替换。你也可以先选择“保留为副本”。', confirmLabel: '重新加载', tone: 'warning' })) await state.reload(); }}>重新加载</button></div>}
        {state.saveError && !state.conflict && <div className="office-notice is-error" role="alert"><HardDrive size={15} /><span>{state.saveError}。{state.draftStored ? '未保存的内容保留在草稿中。' : '请保持窗口打开，并重试保存。'}</span><button onClick={() => void state.save('manual')}><RefreshCw size={13} />重试保存</button></div>}
        {current ? <OfficeEditor key={current.id} document={current} onReady={ready} onSelection={setSelection} onAsk={ask} outlineOpen={outlineOpen} /> : <OfficeHome onTemplates={() => setTemplates(true)} onImport={() => fileRef.current?.click()} />}
      </section>
      <div className="office-resizer" role="separator" aria-label="调整助手宽度" aria-orientation="vertical" aria-valuemin={300} aria-valuemax={560} aria-valuenow={assistantWidth} tabIndex={0} onPointerDown={event => { event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId); setDragging(true); }} onPointerMove={event => { if (!dragging) return; const rect = rootRef.current?.getBoundingClientRect(); if (rect) setAssistantWidth(Math.max(300, Math.min(560, rect.right - event.clientX))); }} onPointerUp={event => { if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId); setDragging(false); rememberWidth(assistantWidth); }} onLostPointerCapture={() => setDragging(false)} onKeyDown={event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); const width = Math.max(300, Math.min(560, assistantWidth + (event.key === 'ArrowLeft' ? 20 : -20))); setAssistantWidth(width); rememberWidth(width); } }} />
      {assistantOpen && <button className="office-assistant-scrim" aria-label="关闭 Syncore 助手" onClick={() => setAssistantOpen(false)} />}
      <aside className="office-assistant" aria-label="Syncore 文档助手">{current ? <OfficeAssistant key={current.id} ref={assistantRef} document={current} editor={editor} selection={selection} onClearSelection={clearSelection} onClose={() => setAssistantOpen(false)} onSettings={() => openSettings()} /> : <OfficeHomeAssistant onStart={prompt => void startAI(prompt)} />}</aside>
    </div>
    {templates && <OfficeTemplateDialog onClose={() => setTemplates(false)} />}
    {history && current && <OfficeHistoryDialog document={current} onClose={() => setHistory(false)} />}
    {(dragOver || importing) && <div className="office-drop-overlay"><div>{importing ? <><span className="spinner" /><h2>正在导入…</h2></> : <><Download size={35} strokeWidth={1.5} /><h2>导入文件</h2><p>Word、Excel、PPT、Markdown、TXT、HTML、CSV</p><button className="office-tool" aria-label="取消拖放" onClick={() => { setDragOver(false); dragDepth.current = 0; }}><X size={16} /></button></>}</div></div>}
  </main>;
}
