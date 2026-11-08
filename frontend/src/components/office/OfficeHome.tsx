import { useState } from 'react';
import { ArrowRight, ArrowUpRight, Clock3, FileText, LayoutTemplate, Plus, Presentation, Sparkles, Star, Table2, Upload } from 'lucide-react';
import { OFFICE_KIND_LABELS, OFFICE_TEMPLATES, officeDate } from '@/lib/officeTemplates';
import type { OfficeTemplate } from '@/lib/officeTemplates';
import { useOfficeStore } from '@/stores/officeStore';
import { useUiStore } from '@/stores/uiStore';

export function TemplateCard({ template, onClick }: { template: OfficeTemplate; onClick: () => void }) {
  return <button className={`office-template office-template--${template.color}`} onClick={onClick}><div className="office-template-art" aria-hidden="true"><div className="office-template-sheet">{template.id === 'blank' ? <Plus size={28} strokeWidth={1} /> : <><span>{template.kind === 'presentation' ? 'PRESENTATION' : template.kind === 'spreadsheet' ? 'DATA REPORT' : 'SYNCORE · DOCUMENT'}</span><strong>{template.title}</strong><i /><i /><i />{template.kind === 'spreadsheet' || template.id === 'weekly' ? <div className="office-template-mini-table"><b /><b /><b /><b /><b /><b /></div> : <><em /><i /><i /></>}</>}</div><span className="office-template-arrow"><ArrowUpRight size={15} /></span></div><div className="office-template-copy"><strong>{template.title}</strong></div></button>;
}

export function OfficeHome({ onTemplates, onImport }: { onTemplates: () => void; onImport: () => void }) {
  const documents = useOfficeStore(state => state.documents);
  const loading = useOfficeStore(state => state.loading);
  const [kind, setKind] = useState('all');
  const libraryOpen = useUiStore(s => s.officeLibraryOpen);
  const recent = documents.filter(item => !item.trashed && (kind === 'all' || item.kind === kind)).slice(0, 12);
  return <div className="office-home"><div className="office-home-intro"><h1>我的文档</h1><div>{!libraryOpen && <button className="office-primary" onClick={() => void useOfficeStore.getState().create()}><Plus size={16} />新建文档</button>}<button className="office-secondary" onClick={onImport}><Upload size={15} />导入文件</button></div></div>
    <section className="office-home-section"><header><h2><LayoutTemplate size={16} />模板</h2><button className="office-text-button" onClick={onTemplates}>查看全部<ArrowRight size={13} /></button></header><div className="office-template-grid">{OFFICE_TEMPLATES.filter(template => template.id !== 'blank').slice(0, 4).map(template => <TemplateCard key={template.id} template={template} onClick={() => void useOfficeStore.getState().create(template.id === 'blank' ? undefined : template.title, template.content, template.kind)} />)}</div></section>
    <section className="office-home-section office-recents"><header><h2><Clock3 size={16} />最近文档 <small>{documents.filter(item => !item.trashed).length}</small></h2><div className="office-kind-tabs" role="group" aria-label="文档类型">{[['all', '全部'], ['document', '文档'], ['spreadsheet', '表格'], ['presentation', '演示']].map(([value, label]) => <button key={value} className={kind === value ? 'is-active' : ''} aria-pressed={kind === value} onClick={() => setKind(value)}>{label}</button>)}</div></header>{loading && !recent.length ? <div className="office-home-loading"><span className="spinner" />正在加载…</div> : recent.length ? <div className="office-recent-table"><div className="office-recent-table-head"><span>文档名称</span><span>类型</span><span>最近编辑</span></div>{recent.map(item => {
      const Icon = item.kind === 'spreadsheet' ? Table2 : item.kind === 'presentation' ? Presentation : FileText;
      return <button key={item.id} onClick={() => void useOfficeStore.getState().open(item.id)}><span><i className={`office-file-icon office-file-icon--${item.kind}`}><Icon size={17} /></i><strong>{item.title || '未命名文档'}</strong>{item.favorite && <Star size={11} fill="currentColor" />}</span><span>{OFFICE_KIND_LABELS[item.kind]}</span><span>{officeDate(item.updated_at)}<ArrowUpRight size={14} /></span></button>;
    })}</div> : <div className="office-recents-empty"><span><FileText size={25} strokeWidth={1.3} /></span><div><strong>{kind === 'all' ? '暂无文档' : '暂无此类文档'}</strong></div></div>}</section></div>;
}

export function OfficeHomeAssistant({ onStart }: { onStart: (prompt: string) => void }) {
  const [prompt, setPrompt] = useState('');
  return <div className="office-home-assistant"><div className="office-home-assistant-label"><Sparkles size={16} /><strong>文档助手</strong></div><div className="office-home-assistant-art"><div><Sparkles size={33} strokeWidth={1.3} /></div></div><h2>起草文档</h2><form onSubmit={event => { event.preventDefault(); if (prompt.trim()) onStart(prompt); }}><textarea aria-label="文档要求" placeholder="输入主题与内容要求…" rows={4} maxLength={12000} value={prompt} onChange={event => setPrompt(event.target.value)} /><button className="office-primary" disabled={!prompt.trim()} type="submit"><Sparkles size={14} />生成草稿<ArrowRight size={14} /></button></form><div className="office-home-prompt-label">快捷指令</div>{[{ label: '工作周报', prompt: '帮我拟一份本周工作的周报框架' }, { label: '项目邮件', prompt: '写一封专业的项目跟进邮件' }, { label: '项目方案', prompt: '拟一份新项目启动方案' }].map(item => <button className="office-home-prompt" key={item.label} onClick={() => onStart(item.prompt)}><span>{item.label}</span><ArrowUpRight size={13} /></button>)}</div>;
}
