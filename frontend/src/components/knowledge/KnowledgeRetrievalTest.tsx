import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowRight, Search, X } from '@/components/icons';
import { testKnowledgeRetrieval, type RetrievalMode, type RetrievalTestResult } from '@/lib/knowledgeApi';
import type { IndexDocument, IndexStatus } from '@/types';
import { ChunkText, KnowledgeEmpty } from './KnowledgeParts';

const LANES: Record<string, string> = { vector: '语义召回', bm25: '关键词召回', graph: '图谱召回' };
const MODES: Record<RetrievalMode, string> = { hybrid: '混合检索', vector: '语义检索', bm25: '关键词检索', hybrid_graph: '混合 + 图谱' };
export function KnowledgeRetrievalTest({ source, status, onClearSource, onOpenSource }: {
  source: IndexDocument | null; status: IndexStatus | null; onClearSource: () => void; onOpenSource: (source: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<RetrievalMode>('hybrid');
  const [topK, setTopK] = useState(5);
  const [rerank, setRerank] = useState<boolean | null>(null);
  const effectiveRerank = rerank ?? status?.retrieval?.rerank_enabled ?? true;
  const [result, setResult] = useState<RetrievalTestResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const controllerRef = useRef<AbortController | null>(null);
  useEffect(() => () => controllerRef.current?.abort(), []);
  useEffect(() => {
    controllerRef.current?.abort();
    setLoading(false); setResult(null); setError('');
  }, [source?.source]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!query.trim() || loading) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true); setError(''); setResult(null);
    try {
      const data = await testKnowledgeRetrieval({ query: query.trim(), top_k: topK, mode, rerank: effectiveRerank, source: source?.source }, controller.signal);
      if (!controller.signal.aborted) setResult(data);
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : String(err));
    } finally { if (!controller.signal.aborted) setLoading(false); }
  }

  return <div className="kb-test">
    <form className="kb-test__form" onSubmit={event => void submit(event)}>
      <div className="kb-section-heading"><div><h2>检索测试</h2><p>用真实问题检查能找到哪些资料，定位回答缺少依据的原因。</p></div><span className="kb-chip">仅检索，不生成回答</span></div>
      <label className="kb-field"><span>测试问题</span><textarea value={query} onChange={event => setQuery(event.target.value)} rows={3} maxLength={2000} required disabled={loading} placeholder="例如：项目的交付标准是什么？报销需要准备哪些材料？" onKeyDown={event => {
        if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); }
      }} /></label>
      {source && <div className="kb-test__scope"><span>仅检索：<strong>{source.source}</strong></span><button type="button" className="btn btn--sm btn--ghost" disabled={loading} onClick={onClearSource} aria-label="改为检索全部文档"><X size={13} /></button></div>}
      <div className="kb-test__controls">
        <label className="kb-field"><span>检索方式</span><select className="select" value={mode} disabled={loading} onChange={event => setMode(event.target.value as RetrievalMode)}>{Object.entries(MODES).filter(([key]) => key !== 'hybrid_graph' || status?.retrieval?.graph_enabled).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        <label className="kb-field"><span>返回片段数</span><select className="select" value={topK} disabled={loading} onChange={event => setTopK(Number(event.target.value))}>{[3, 5, 10, 20].map(value => <option key={value} value={value}>{value} 条</option>)}</select></label>
        <label className="kb-checkbox"><input type="checkbox" checked={effectiveRerank} disabled={loading} onChange={event => setRerank(event.target.checked)} />结果重排</label>
        <button type="submit" className="btn btn--primary" disabled={loading || !query.trim()}>{loading ? <span className="spinner" /> : <Search size={14} />}{loading ? '正在检索…' : '开始测试'}</button>
      </div>
      <p className="kb-caption">混合检索兼顾语义和精确关键词。此处的参数仅用于本次测试。</p>
    </form>
    {error && <div className="kb-notice kb-notice--error" role="alert">{error}</div>}
    {loading && <div className="kb-loading" role="status"><span className="spinner" />正在召回与排序，请稍候…</div>}
    {result ? <section className="kb-test__results" aria-label="检索测试结果" aria-live="polite">
      <div className="kb-section-heading"><div><h2>命中 {result.results.length} 个片段</h2><p>“{result.query}” · {MODES[result.mode]} · {result.duration_ms.toLocaleString()} ms</p></div><span className="kb-chip">{result.reranked ? '已应用重排' : '召回排序'}</span></div>
      <div className="kb-test__lanes">{result.lanes.filter(lane => lane.status !== 'disabled').map(lane => <span key={lane.name} className={`kb-chip ${lane.status === 'error' ? 'kb-error-text' : ''}`} title={lane.error}>{LANES[lane.name] ?? lane.name} · {lane.status === 'error' ? '不可用' : `${lane.hits} 条`}</span>)}</div>
      {result.warnings.map(warning => <div className="kb-notice" key={warning}>{warning}</div>)}
      {result.results.map((hit, index) => {
        const score = hit.rerank_score ?? hit.rrf_score ?? hit.score;
        const scoreLabel = hit.rerank_score != null ? '重排分' : hit.rrf_score != null ? 'RRF 分' : hit.retrieval === 'bm25' ? 'BM25 分' : '相似度';
        return <article className="kb-result" key={`${hit.source}:${hit.chunk_index}:${index}`}>
          <div className="kb-result__heading"><span className="kb-result__rank">{String(index + 1).padStart(2, '0')}</span><button type="button" className="kb-result__source" onClick={() => onOpenSource(hit.source)} title="查看来源文档">{hit.source}<ArrowRight size={13} /></button><span className="kb-result__score">{score != null ? `${scoreLabel} ${score.toFixed(4)}` : '无分数'}</span></div>
          <div className="kb-result__meta"><span>片段 {hit.chunk_index + 1}{hit.page > 0 ? ` · 第 ${hit.page} 页` : ''}</span><span>{(hit.recalled_by ?? [hit.retrieval ?? '']).map(lane => LANES[lane] ?? lane).join(' · ')}</span></div>
          <ChunkText text={hit.text} />
        </article>;
      })}
      {result.results.length === 0 && <KnowledgeEmpty title={result.warnings.length ? '本次未获得检索结果' : '没有找到相关片段'} />}
      {result.results.length > 0 && <p className="kb-caption">分数用于当前检索的排序，不代表回答正确率；不同检索方式的分数不宜直接比较。</p>}
    </section> : !loading && !error && <KnowledgeEmpty title="暂无检索结果" />}
  </div>;
}
