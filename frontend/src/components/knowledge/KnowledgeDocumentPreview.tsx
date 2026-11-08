import { useEffect, useRef, useState } from 'react';
import { FileText, RefreshCw, Search, X } from '@/components/icons';
import { formatBytes, formatIsoRelative } from '@/lib/format';
import { getKnowledgeChunks, type KnowledgeChunks } from '@/lib/knowledgeApi';
import type { IndexDocument } from '@/types';
import { ChunkText, DocumentStatus } from './KnowledgeParts';

export function KnowledgeDocumentPreview({ doc, revision, busy, onClose, onReindex, onTest }: {
  doc: IndexDocument; revision: number; busy: boolean; onClose: () => void;
  onReindex: () => void; onTest: () => void;
}) {
  const previewRef = useRef<HTMLElement>(null);
  const [data, setData] = useState<KnowledgeChunks | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    previewRef.current?.focus({ preventScroll: true });
    previewRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [doc.source]);
  useEffect(() => { if (offset >= doc.chunks && offset > 0) setOffset(0); }, [doc.chunks, offset]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError('');
    void getKnowledgeChunks(doc.source, offset, controller.signal).then(result => {
      if (!controller.signal.aborted) setData(result);
    }).catch((err: unknown) => {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : String(err));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [doc.source, offset, revision, retry]);

  return <aside ref={previewRef} tabIndex={-1} className="kb-preview" aria-label={`${doc.source} 的已入库内容`} onKeyDown={event => { if (event.key === 'Escape') onClose(); }}>
    <div className="kb-preview__heading">
      <div><span className="kb-eyebrow">文档详情</span><h2><FileText size={17} />{doc.source.split('/').pop()}</h2></div>
      <button type="button" className="btn btn--sm btn--ghost" onClick={onClose} aria-label="关闭文档详情"><X size={16} /></button>
    </div>
    <div className="kb-preview__meta">
      <DocumentStatus doc={doc} /><span>{formatBytes(doc.size)}</span><span>{doc.chunks} 个文本块</span>
    </div>
    <p className="kb-preview__source" title={doc.path}>{doc.source}</p>
    {doc.error && <div className="kb-notice kb-notice--error" role="alert">{doc.error}</div>}
    {doc.warnings?.map(warning => <div className="kb-notice" key={warning}>{warning}</div>)}
    {!doc.on_disk && <div className="kb-notice">原文件已不在原处。已入库内容仍可查看；更新时请重新上传。</div>}
    <div className="kb-preview__actions">
      <button type="button" className="btn btn--sm btn--primary" onClick={onTest}><Search size={13} />检索这份文档</button>
      <button type="button" className="btn btn--sm btn--ghost" disabled={busy || !doc.on_disk} onClick={onReindex}><RefreshCw size={13} />重新索引</button>
    </div>
    <div className="kb-preview__section-title"><h3>已入库片段</h3><span>{loading ? '读取中…' : `${data?.total ?? doc.chunks} 块`}</span></div>
    <p className="kb-caption">查看实际参与检索的内容，检查文字是否完整。</p>
    {error ? <div className="kb-notice kb-notice--error" role="alert">{error}<button type="button" className="kb-text-button" onClick={() => setRetry(retry + 1)}>重试</button></div>
      : loading ? <div className="kb-loading" role="status"><span className="spinner" />正在读取片段…</div>
      : <>
        {data?.warnings.map(warning => <div className="kb-notice" key={warning}>{warning}</div>)}
        {data?.chunks.map(chunk => <article className="kb-chunk" key={chunk.chunk_index}>
          <div className="kb-chunk__meta"><span>片段 {chunk.chunk_index + 1}</span><span>{chunk.text.length} 字符{chunk.page > 0 ? ` · 第 ${chunk.page} 页` : ''}</span></div>
          <ChunkText text={chunk.text} />
        </article>)}
        {data && data.total === 0 && <p className="kb-caption">这份文档没有可检索的文字，请检查原文件后重新索引。</p>}
      </>}
    {data && data.total > 8 && <div className="kb-pagination">
      <span>{Math.floor(offset / 8) + 1} / {Math.ceil(data.total / 8)} 页</span>
      <button type="button" className="btn btn--sm btn--ghost" disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - 8))}>上一页</button>
      <button type="button" className="btn btn--sm btn--ghost" disabled={loading || offset + 8 >= data.total} onClick={() => setOffset(offset + 8)}>下一页</button>
    </div>}
    <footer className="kb-preview__footer">入库于 {formatIsoRelative(doc.indexed_at)}<span title={doc.path}>{doc.path}</span></footer>
  </aside>;
}
