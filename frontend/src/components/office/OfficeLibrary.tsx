import { useMemo, useState } from 'react';
import { FileText, FolderOpen, Plus, Presentation, Search, Star, Table2, Trash2, X } from 'lucide-react';
import { officeDate } from '@/lib/officeTemplates';
import { useOfficeStore } from '@/stores/officeStore';

export function OfficeLibrary({ onClose }: { onClose: () => void }) {
  const documents = useOfficeStore(state => state.documents);
  const current = useOfficeStore(state => state.document);
  const loading = useOfficeStore(state => state.loading);
  const opening = useOfficeStore(state => state.opening);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<'all' | 'favorite' | 'trash'>('all');
  const items = useMemo(() => documents.filter(item => (filter === 'trash' ? item.trashed : !item.trashed && (filter !== 'favorite' || item.favorite)) && `${item.title} ${item.excerpt}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())), [documents, filter, query]);
  const activeCount = documents.filter(item => !item.trashed).length;

  const selectDocument = async (id?: string) => {
    const store = useOfficeStore.getState();
    const previousId = store.document?.id;
    if (id) await store.open(id);
    else await store.create();
    const openedId = useOfficeStore.getState().document?.id;
    if (openedId && (id ? openedId === id : openedId !== previousId) && window.matchMedia('(max-width: 760px)').matches) onClose();
  };

  return <aside className="office-library" aria-label="文档库">
    <div className="office-library-top"><div className="office-library-label"><FolderOpen size={16} /><strong>文档库</strong></div><button className="office-library-mobile-close office-tool" onClick={onClose} aria-label="收起文档库"><X size={16} /></button></div>
    <button className="office-new-document office-primary" onClick={() => void selectDocument()} disabled={opening}><Plus size={16} /><span>新建文档</span><kbd>Ctrl N</kbd></button>
    <div className="office-library-search"><Search size={14} /><input aria-label="搜索文档" placeholder="搜索文档…" value={query} onChange={event => setQuery(event.target.value)} />{query && <button onClick={() => setQuery('')} aria-label="清除搜索"><X size={12} /></button>}</div>
    <nav className="office-library-nav" aria-label="文档筛选"><button className={filter === 'all' ? 'is-active' : ''} onClick={() => setFilter('all')}><FileText size={15} /><span>全部文档</span><small>{activeCount}</small></button><button className={filter === 'favorite' ? 'is-active' : ''} onClick={() => setFilter('favorite')}><Star size={15} /><span>我的收藏</span><small>{documents.filter(item => !item.trashed && item.favorite).length || ''}</small></button><button className={filter === 'trash' ? 'is-active' : ''} onClick={() => setFilter('trash')}><Trash2 size={15} /><span>回收站</span></button></nav>
    <div className="office-library-list-label"><span>{query ? '搜索结果' : filter === 'trash' ? '回收站' : '最近编辑'}</span><small>{items.length}</small></div>
    <div className="office-document-list">{loading && !documents.length ? <div className="office-library-skeleton"><i /><i /><i /></div> : items.length ? items.map(item => {
      const Icon = item.kind === 'spreadsheet' ? Table2 : item.kind === 'presentation' ? Presentation : FileText;
      return <button key={item.id} className={`office-document-item${current?.id === item.id ? ' is-active' : ''}`} disabled={opening} onClick={() => void (item.trashed ? useOfficeStore.getState().restore(item.id) : selectDocument(item.id))} title={item.trashed ? `恢复 ${item.title}` : item.title}><span className={`office-file-icon office-file-icon--${item.kind}`}><Icon size={17} /></span><span><strong>{item.title || '未命名文档'}</strong><small>{item.trashed ? '恢复文档' : officeDate(item.updated_at)}</small></span>{item.favorite && !item.trashed && <Star size={11} className="office-document-star" fill="currentColor" />}</button>;
    }) : <div className="office-library-empty"><FolderOpen size={24} /><p>{query ? '没有找到相关文档' : filter === 'trash' ? '回收站为空' : filter === 'favorite' ? '暂无收藏' : '暂无文档'}</p></div>}</div>
    <div className="office-library-footer"><div><span className="office-live-dot" /><span>本地工作空间</span></div></div>
  </aside>;
}
