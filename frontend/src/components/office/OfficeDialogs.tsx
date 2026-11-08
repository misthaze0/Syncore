import { useEffect, useRef, useState } from 'react';
import { Check, Clock3, History, LayoutTemplate, RefreshCw, Search, X } from 'lucide-react';
import { useModalFocus } from '@/hooks/useModalFocus';
import { OfficePreview } from '@/components/office/OfficePreview';
import { TemplateCard } from '@/components/office/OfficeHome';
import { OFFICE_TEMPLATES } from '@/lib/officeTemplates';
import { officeApi } from '@/lib/officeApi';
import { useOfficeStore } from '@/stores/officeStore';
import { toast } from '@/stores/toastStore';
import type { OfficeDocument, OfficeVersion } from '@/types/office';

export function OfficeTemplateDialog({ onClose }: { onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useModalFocus(ref);
  const [query, setQuery] = useState('');
  useEffect(() => { const key = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); }; window.addEventListener('keydown', key); return () => window.removeEventListener('keydown', key); }, [onClose]);
  return <div className="office-modal-overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><div ref={ref} className="office-modal office-templates-modal" role="dialog" aria-modal="true" aria-labelledby="office-templates-title" tabIndex={-1}><header><div><h2 id="office-templates-title"><LayoutTemplate size={20} />模板</h2></div><button className="office-tool" aria-label="关闭模板中心" onClick={onClose}><X size={18} /></button></header><div className="office-template-search"><Search size={15} /><input aria-label="搜索模板" placeholder="搜索周报、会议、项目…" value={query} onChange={event => setQuery(event.target.value)} /></div><div className="office-template-grid">{OFFICE_TEMPLATES.filter(template => `${template.title}${template.description}${template.category}`.includes(query)).map(template => <TemplateCard key={template.id} template={template} onClick={async () => { await useOfficeStore.getState().create(template.id === 'blank' ? undefined : template.title, template.content, template.kind); onClose(); }} />)}</div>{!OFFICE_TEMPLATES.some(template => `${template.title}${template.description}${template.category}`.includes(query)) && <p className="office-modal-empty">未找到相关模板</p>}</div></div>;
}

export function OfficeHistoryDialog({ document, onClose }: { document: OfficeDocument; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useModalFocus(ref);
  const [versions, setVersions] = useState<OfficeVersion[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [version, setVersion] = useState<OfficeVersion | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [restoring, setRestoring] = useState(false);
  useEffect(() => {
    let alive = true;
    void officeApi.versions(document.id).then(result => { if (alive) setVersions(result.versions); }).catch(reason => { if (alive) setError(reason instanceof Error ? reason.message : '版本加载失败'); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [document.id]);
  useEffect(() => {
    let alive = true;
    setVersion(null);
    if (selected) void officeApi.version(document.id, selected).then(result => { if (alive) setVersion(result); }).catch(reason => { if (alive) setError(reason instanceof Error ? reason.message : '版本读取失败'); });
    return () => { alive = false; };
  }, [selected, document.id]);
  useEffect(() => { const key = (event: KeyboardEvent) => { if (event.key === 'Escape' && !restoring) onClose(); }; window.addEventListener('keydown', key); return () => window.removeEventListener('keydown', key); }, [onClose, restoring]);
  const restore = async () => {
    if (!version?.content) return;
    setRestoring(true);
    useOfficeStore.getState().stopAssistant();
    useOfficeStore.getState().update({ title: version.title, content: version.content });
    const ok = await useOfficeStore.getState().save('restore');
    setRestoring(false);
    if (ok) { toast('已恢复所选版本，恢复前的内容仍保留在历史中', 'success'); onClose(); }
  };
  const timestamp = (value: string) => new Date(value).toLocaleString('zh-CN', { month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  return <div className="office-modal-overlay" onMouseDown={event => { if (event.target === event.currentTarget && !restoring) onClose(); }}><div ref={ref} className="office-modal office-history-modal" role="dialog" aria-modal="true" aria-labelledby="office-history-title" tabIndex={-1}><header><div><h2 id="office-history-title"><History size={19} />版本历史</h2></div><button className="office-tool" aria-label="关闭版本历史" disabled={restoring} onClick={onClose}><X size={18} /></button></header><div className="office-history-body"><aside><button className={!selected ? 'is-active' : ''} onClick={() => { setSelected(null); setError(''); }}><span><Check size={14} />当前版本</span><small>{timestamp(document.updated_at)}</small></button>{loading ? <p><span className="spinner" />加载版本…</p> : versions.map(item => <button key={item.id} className={selected === item.id ? 'is-active' : ''} onClick={() => { setSelected(item.id); setError(''); }}><span><Clock3 size={13} />{item.label}</span><small>{timestamp(item.created_at)} · v{item.revision}</small></button>)}{!loading && !versions.length && <p>暂无历史版本</p>}</aside><section>{error ? <div className="office-error-card">{error}</div> : selected && !version ? <div className="office-home-loading"><span className="spinner" />读取版本…</div> : <><div className="office-version-title">{version?.title || document.title}</div><OfficePreview content={selected ? version?.content || '' : document.content} /></>}</section></div><footer><button className="office-secondary" disabled={restoring} onClick={onClose}>关闭</button><button className="office-primary" disabled={!selected || !version || restoring} onClick={() => void restore()}><RefreshCw size={14} />{restoring ? '正在恢复…' : '恢复此版本'}</button></footer></div></div>;
}
