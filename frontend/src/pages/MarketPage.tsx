import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Check, Download, Globe, Package, Plus, Sparkles, UploadCloud, Wand2, Wrench, X } from '@/components/icons';
import { PageShell } from '@/components/PageShell';
import { PageSearch } from '@/components/PageSearch';
import { EmptyState } from '@/components/EmptyState';
import { fetchMarketEntry, fetchMarketBrief, installMarketEntry, setMarketEnabled, uninstallMarketEntry, uploadMarketBundle, installMarketLocal, refreshMarket, rescanSkills } from '@/lib/api';
import { useAgentStore } from '@/stores/agentStore';
import { putComposerDraft } from '@/stores/composerStore';
import { useModalFocus, isTopmostModal } from '@/hooks/useModalFocus';
import { Switch } from '@/components/settings2/kit';
import { ExtensionConnection, ExtensionTools, SkillContent } from '@/components/extensions/ExtensionControls';
import { toast } from '@/stores/toastStore';
import type { MarketEntry, MarketKind } from '@/types';
import { capabilityTitle, extensionStatus, extensionSummary, fetchExtensionPages, fetchInstalledInventory } from '@/lib/extensions';

const KIND = { skill: '技能', mcp: '连接器', plugin: '组合包' };
const ICON = { skill: Wand2, mcp: Globe, plugin: Package };
export function MarketPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const [view, setView] = useState<'discover' | 'installed'>(() => new URLSearchParams(location.search).get('view') === 'installed' ? 'installed' : 'discover');
  const [kind, setKind] = useState<MarketKind | ''>('');
  const [query, setQuery] = useState('');
  const [limit, setLimit] = useState(24);
  const [items, setItems] = useState<MarketEntry[]>([]);
  const [installed, setInstalled] = useState<MarketEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [toolsRevision, setToolsRevision] = useState(0);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<MarketEntry | null>(null);
  const [dialog, setDialog] = useState<'add' | 'connection' | 'tools' | null>(() => new URLSearchParams(location.search).get('panel') === 'tools' ? 'tools' : null);
  const [busy, setBusy] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const sequence = useRef(0);
  const load = useCallback(async () => {
    const request = ++sequence.current;
    setLoading(true); setError('');
    try {
      const [result, local] = await Promise.all([
        fetchExtensionPages({ view, kinds: kind ? [kind] : undefined, q: query }, limit),
        fetchInstalledInventory(),
      ]);
      if (request !== sequence.current) return;
      setItems(result.items); setInstalled(local.items); setHasMore(result.has_more); setToolsRevision(n => n + 1);
      setSelected(old => old ? [...local.items, ...result.items].find(e => e.canonical === old.canonical && e.kind === old.kind) ?? old : null);
      if (result.failures.length) setError('部分目录暂时无法连接，请稍后重试。');
    } catch (e) { if (request === sequence.current) { setItems([]); setHasMore(false); setError(String(e instanceof Error ? e.message : e)); } }
    finally { if (request === sequence.current) setLoading(false); }
  }, [view, kind, query, limit]);
  useEffect(() => { setLoading(true); const timer = setTimeout(() => void load(), 180); return () => { clearTimeout(timer); sequence.current++; }; }, [load]);
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    setView(params.get('view') === 'installed' ? 'installed' : 'discover');
    if (params.get('panel') === 'tools') { setSelected(null); setDialog('tools'); }
  }, [location.search]);
  const act = async (action: () => Promise<unknown>, message: string) => {
    setBusy(true);
    try { await action(); toast(message, 'success'); await load(); }
    catch (e) { toast(e instanceof Error ? e.message : String(e), 'error'); await load(); }
    finally { setBusy(false); }
  };
  const install = (entry: MarketEntry) => act(async () => {
    const fresh = await fetchMarketEntry(entry.canonical);
    if (!fresh.install_ref) throw new Error('这个扩展已添加或暂时无法添加');
    const result = await installMarketEntry(fresh.install_ref);
    if (result.entry) setSelected(result.entry);
  }, '已添加');
  const choose = (entry: MarketEntry) => { setDialog(null); setSelected(entry); setConfirmRemove(false); };
  const close = () => { if (!busy) { setSelected(null); setDialog(null); setConfirmRemove(false); } };
  const current = selected;
  const Icon = current ? ICON[current.kind] : Package;
  const parent = current?.via_bundle ? installed.find(entry => entry.kind === 'plugin' && entry.canonical === current.via_bundle) : undefined;
  return <PageShell
    title="插件市场"
    icon={Package}
    className="ext-market"
    actions={<button type="button" className="btn btn--sm btn--primary" onClick={() => setDialog('add')}><Plus size={14} />添加扩展</button>}
    toolbar={<>
      <div className="page-tabs" role="tablist" aria-label="扩展"><button type="button" role="tab" aria-selected={view === 'discover'} onClick={() => { setView('discover'); setLimit(24); }}>发现</button><button type="button" role="tab" aria-selected={view === 'installed'} onClick={() => { setView('installed'); setLimit(24); }}>已添加</button></div>
      <div className="page-toolbar__actions"><button type="button" className="btn btn--sm btn--ghost" onClick={() => setDialog('tools')}><Wrench size={14} />全部能力</button><PageSearch value={query} onChange={value => { setQuery(value); setLimit(24); }} placeholder="搜索你想做的事" /></div>
    </>}
  >
      <div className="page-filters" aria-label="扩展类型">{(['', 'skill', 'mcp', 'plugin'] as const).map(k => <button type="button" className="page-filter" key={k} aria-pressed={kind === k} onClick={() => { setKind(k); setLimit(24); }}>{k ? KIND[k] : '全部'}</button>)}</div>
      {error && <div className="ext-error" role="alert">{error}<button className="btn btn--sm" onClick={() => void load()}>重试</button></div>}
      {loading ? <div className="ext-grid" aria-label="正在加载扩展">{[0, 1, 2, 3, 4, 5].map(n => <div className="ext-skeleton" key={n} />)}</div> : items.length ? <div className="ext-grid">{items.map(entry => {
        const EntryIcon = ICON[entry.kind]; const status = extensionStatus(entry);
        return <article className="ext-card" key={entry.kind + ':' + entry.canonical}>
          <button className="ext-card__open" aria-label={'查看 ' + entry.name} onClick={() => choose(entry)}><span className={'ext-icon ext-icon--' + entry.kind}><EntryIcon size={23} /></span><h2>{entry.name}</h2><p>{extensionSummary(entry)}</p></button>
          <div className="ext-card__footer"><span>{KIND[entry.kind]}{entry.source === 'official' ? ' · 官方' : entry.source === 'remote' ? ' · 社区' : ' · 本地'}</span>{entry.installed ? <button className={'ext-state' + (entry.attention ? ' ext-state--warn' : !entry.enabled ? ' ext-state--off' : '')} onClick={() => choose(entry)}>{entry.enabled && !entry.attention && <Check size={12} />} {status}</button> : <button className="ext-install" aria-label={'添加 ' + entry.name} disabled={busy || !entry.install_ref} onClick={() => void install(entry)}><Plus size={13} />添加</button>}</div>
        </article>;
      })}</div> : error ? null : <EmptyState fill icon={Package} title={query || kind ? '没有找到匹配的扩展' : '还没有添加扩展'} action={<button type="button" className="btn" onClick={() => { setView('discover'); setKind(''); setQuery(''); }}>浏览全部</button>} />}
      {hasMore && !loading && <button className="btn ext-more" onClick={() => setLimit(n => n + 24)}>显示更多</button>}
    {current && <ExtensionDialog title={current.name} onClose={close}>
      {parent && <button className="btn btn--sm btn--ghost ext-parent" onClick={() => choose(parent)}>‹ {parent.name}</button>}
      <div className="ext-detail-title"><span className={'ext-icon ext-icon--' + current.kind}><Icon size={26} /></span><div><h2>{current.name}</h2><span>{KIND[current.kind]} · {current.publisher || (current.source === 'official' ? '官方' : '本地扩展')}</span></div></div>
      <p className="ext-description">{extensionSummary(current)}</p>
      <div className="ext-detail-actions">{!current.installed ? <button className="btn btn--primary" disabled={busy || !current.install_ref} onClick={() => void install(current)}><Download size={14} />{busy ? '添加中…' : '添加'}</button> : <>{current.toggleable === false ? <span>随应用内置</span> : <label className="ext-enable"><Switch checked={current.enabled} disabled={busy} ariaLabel={'启用 ' + current.name} onChange={enabled => void act(() => setMarketEnabled(current.canonical, enabled), enabled ? '已启用' : '已停用')} />{current.enabled ? '已启用' : '已停用'}</label>}{current.uninstallable && <button className="btn btn--sm btn--ghost" disabled={busy} onClick={() => setConfirmRemove(true)}>移除</button>}</>}</div>
      {confirmRemove && <div className="ext-error">移除「{current.name}」？<div><button className="btn btn--sm" onClick={() => setConfirmRemove(false)}>取消</button><button className="btn btn--sm" disabled={busy} onClick={() => void act(async () => { await uninstallMarketEntry(current.canonical); setSelected(null); }, '已移除')}>确认移除</button></div></div>}
      {current.installed && current.attention && !current.update_available && <div className="ext-error">{current.configuration_required ? '填写连接信息后即可使用' : current.status_note || '暂时不可用'}</div>}
      {current.kind === 'mcp' && current.installed && <ExtensionConnection key={current.canonical + ':' + current.enabled} name={current.canonical} required={current.required_env} onBusyChange={setBusy} onSaved={load} />}
      {current.kind === 'skill' && current.installed && <SkillContent name={current.canonical} />}
      {current.builtin && <section className="ext-section"><h3>内置能力</h3><p>{current.status_note}</p>{current.provided_tools?.map(tool => <div className="ext-member" key={tool.name}><span>{capabilityTitle(tool)}</span></div>)}</section>}
      {current.kind === 'plugin' && !current.builtin && <section className="ext-section"><h3>包含的能力</h3>{current.components.map(c => { const child = installed.find(i => i.kind === c.kind && i.canonical === c.ref); return <button className="ext-member" key={c.kind + c.ref} disabled={!child} onClick={() => child && choose(child)}><span>{child?.name || c.name || c.ref}</span><span>{KIND[c.kind as MarketKind] || '能力'} {child ? '›' : ''}</span></button>; })}</section>}
      {current.kind === 'mcp' && current.installed && <ExtensionTools key={current.canonical + ':' + toolsRevision} server={current.canonical} />}
      <details className="ext-advanced"><summary>详细信息</summary><dl><dt>版本</dt><dd>{current.installed_version || current.version || '—'}</dd><dt>来源</dt><dd>{current.source === 'official' ? '官方目录' : current.source === 'remote' ? '社区目录' : '本地添加，未经审核'}</dd></dl>{current.status_note && <p>{current.status_note}</p>}{current.update_available && <p>目录有新版本。更新前请先备份自定义内容。</p>}</details>
    </ExtensionDialog>}
    {dialog === 'tools' && <ExtensionDialog title="全部能力" onClose={close}><ExtensionTools /></ExtensionDialog>}
    {dialog === 'connection' && <ExtensionDialog title="连接服务" onClose={close}><ExtensionConnection onBusyChange={setBusy} onSaved={async (name) => { setDialog(null); setView('installed'); if (name) choose(await fetchMarketEntry(name)); await load(); }} /></ExtensionDialog>}
    {dialog === 'add' && <ExtensionDialog title="添加扩展" onClose={close}>
      <ImportFile onDone={async () => { setDialog(null); setView('installed'); await load(); }} />
      <button className="ext-add-choice" onClick={() => setDialog('connection')}><Globe size={22} /><span>连接服务</span><span>›</span></button>
      <button className="ext-add-choice" disabled={busy} onClick={() => void act(async () => { const brief = await fetchMarketBrief('skill'); useAgentStore.getState().newSession(); putComposerDraft(brief.prompt); navigate('/chat'); }, '已准备好草稿')}><Sparkles size={22} /><span>让 Syncore 创建</span><span>›</span></button>
      <AdvancedImport onDone={async () => { setDialog(null); setView('installed'); await load(); }} />
    </ExtensionDialog>}
  </PageShell>;
}
function ImportFile({ onDone }: { onDone: () => Promise<void> }) {
  const input = useRef<HTMLInputElement>(null); const [file, setFile] = useState<File | null>(null); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  return <section className="ext-import"><input ref={input} hidden type="file" accept=".zip,.md" onChange={e => { setFile(e.target.files?.[0] ?? null); setError(''); }} /><button className="ext-add-choice" onClick={() => input.current?.click()}><UploadCloud size={22} /><span>{file?.name || '导入文件'}</span><span>ZIP / MD</span></button>{file && <button className="btn btn--primary" disabled={busy} onClick={async () => { setBusy(true); try { const result = await uploadMarketBundle(file, 'auto'); if (result.installed?.some(key => key.startsWith('mcp:'))) toast('已添加；插件里的连接器默认停用，确认启动命令后再启用', 'success'); await onDone(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); } }}>{busy ? '添加中…' : '添加这个文件'}</button>}{error && <p role="alert" className="ext-error">{error}</p>}</section>;
}
function AdvancedImport({ onDone }: { onDone: () => Promise<void> }) {
  const [source, setSource] = useState(''); const [kind, setKind] = useState<'skill' | 'plugin'>('skill'); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const run = async (action: () => Promise<unknown>) => { setBusy(true); setError(''); try { await action(); await onDone(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); } };
  return <details className="ext-advanced"><summary>高级添加</summary><label>从路径或地址添加<input className="ext-input" aria-label="扩展路径或地址" value={source} onChange={e => setSource(e.target.value)} placeholder="本机目录、ZIP 路径或技能包网址" /></label><select className="ext-input" aria-label="文件内容类型" value={kind} onChange={e => setKind(e.target.value as 'skill' | 'plugin')}><option value="skill">技能</option><option value="plugin">组合包</option></select><button className="btn" disabled={busy || !source.trim()} onClick={() => void run(() => installMarketLocal({ kind, source: source.trim() }))}>添加</button><button className="btn btn--ghost" disabled={busy} onClick={() => void run(async () => { const result = await refreshMarket(); await rescanSkills(); if (result.failures.length) throw new Error('部分目录暂时无法读取，请稍后重试'); })}>重新读取目录</button>{error && <p className="ext-error" role="alert">{error}</p>}</details>;
}
export function ExtensionDialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null); useModalFocus(ref);
  useEffect(() => { const key = (e: KeyboardEvent) => { if (e.key === 'Escape' && isTopmostModal(ref.current)) onClose(); }; window.addEventListener('keydown', key); return () => window.removeEventListener('keydown', key); }, [onClose]);
  return <div className="ext-overlay" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><section ref={ref} className="ext-dialog" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}><header><strong>{title}</strong><button className="btn btn--sm btn--ghost" aria-label="关闭扩展详情" onClick={onClose}><X size={18} /></button></header><div className="ext-dialog__body">{children}</div></section></div>;
}
