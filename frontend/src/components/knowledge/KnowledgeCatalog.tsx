import { ChevronRight, FileText, RefreshCw, Trash2 } from '@/components/icons';
import { PageSearch } from '@/components/PageSearch';
import { formatBytes, formatIsoRelative } from '@/lib/format';
import type { KnowledgeCatalog as Catalog } from '@/lib/knowledgeApi';
import type { IndexDocument } from '@/types';
import { DocumentStatus, KnowledgeEmpty } from './KnowledgeParts';

export function KnowledgeCatalog({ data, query, state, sort, offset, loading, error, selected, disabled,
  onQuery, onState, onSort, onOffset, onSelect, onReindex, onRemove, onUpload, onRetry }: {
  data: Catalog | null; query: string; state: string; sort: string; offset: number; loading: boolean;
  error: string; selected: string | null; disabled: boolean;
  onQuery: (value: string) => void; onState: (value: string) => void; onSort: (value: string) => void;
  onOffset: (value: number) => void; onSelect: (doc: IndexDocument) => void;
  onReindex: (doc: IndexDocument) => void; onRemove: (doc: IndexDocument) => void;
  onUpload: () => void; onRetry: () => void;
}) {
  return <section className="kb-catalog" aria-label="文档清单">
    <div className="kb-catalog__toolbar">
      <PageSearch value={query} onChange={onQuery} placeholder="搜索文档名称或来源…" />
      <div className="kb-catalog__filters">
        <select className="select" value={state} onChange={event => onState(event.target.value)} aria-label="按文档状态筛选">
          <option value="all">全部状态</option><option value="indexed">已入库</option><option value="warning">部分就绪</option><option value="error">需要处理</option><option value="missing">原文件缺失</option><option value="empty">无文本</option>
        </select>
        <select className="select" value={sort} onChange={event => onSort(event.target.value)} aria-label="文档排序方式"><option value="recent">最近入库</option><option value="name">按名称</option><option value="chunks">按文本块数</option></select>
      </div>
    </div>
    {error && <div className="kb-notice kb-notice--error" role="alert">{error}<button type="button" className="kb-text-button" onClick={onRetry}>重新加载</button></div>}
    {loading && !data ? <div className="kb-loading" role="status"><span className="spinner" />正在读取文档…</div>
      : data && data.documents.length > 0 ? <>
        <div className="kb-table-scroll" aria-busy={loading}><table className="kb-table">
          <thead><tr><th scope="col">文档</th><th scope="col">状态</th><th scope="col">文本块</th><th scope="col">入库时间</th><th scope="col"><span className="sr-only">操作</span></th></tr></thead>
          <tbody>{data.documents.map(doc => <tr key={doc.source} className={selected === doc.source ? 'is-selected' : ''}>
            <td><button type="button" className="kb-document" aria-pressed={selected === doc.source} onClick={() => onSelect(doc)}>
              <span className="kb-document__icon"><FileText size={19} /></span><span className="kb-document__copy"><strong title={doc.source}>{doc.source.split('/').pop()}</strong><span title={doc.source}>{doc.source.includes('/') ? doc.source : `${doc.extension?.slice(1).toUpperCase() || '文档'} · ${formatBytes(doc.size)}`}</span></span><ChevronRight size={13} />
            </button></td>
            <td><DocumentStatus doc={doc} />{!doc.on_disk && <span className="kb-missing">原文件缺失</span>}</td>
            <td className="kb-table__number">{doc.chunks.toLocaleString()}</td><td className="kb-table__time" title={doc.indexed_at}>{formatIsoRelative(doc.indexed_at)}</td>
            <td><div className="kb-table__actions"><button type="button" className="btn btn--sm btn--ghost" disabled={disabled || !doc.on_disk} onClick={() => onReindex(doc)} aria-label={`重新索引 ${doc.source}`} title={doc.on_disk ? '重新解析并索引这份文档' : '原文件缺失，请重新上传'}><RefreshCw size={13} /></button><button type="button" className="btn btn--sm btn--ghost kb-delete" disabled={disabled} onClick={() => onRemove(doc)} aria-label={`删除 ${doc.source}`} title="从知识库移除"><Trash2 size={13} /></button></div></td>
          </tr>)}</tbody>
        </table></div>
        <div className="kb-pagination"><span>{loading ? '更新中…' : `共 ${data.total} 份 · ${offset + 1}–${offset + data.documents.length}`}</span><button type="button" className="btn btn--sm btn--ghost" disabled={loading || offset === 0} onClick={() => onOffset(Math.max(0, offset - 20))}>上一页</button><button type="button" className="btn btn--sm btn--ghost" disabled={loading || offset + 20 >= data.total} onClick={() => onOffset(offset + 20)}>下一页</button></div>
      </> : data && !error ? <><KnowledgeEmpty title={query || state !== 'all' ? '没有符合条件的文档' : '暂无知识文档'} />{!query && state === 'all' && <div className="kb-empty__action"><button type="button" className="btn btn--primary" onClick={onUpload}>上传第一份文档</button></div>}</> : null}
  </section>;
}
