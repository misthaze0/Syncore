import { RefreshCw } from '@/components/icons';
import { KnowledgeScopeEditor } from '@/components/settings2/KnowledgeScopeEditor';
import { formatIsoRelative } from '@/lib/format';
import type { IndexDocument, IndexStatus, ScanPlan, ScanReport } from '@/types';

export function KnowledgeSyncPanel({ status, docs, plan, report, scanning, disabled, onPreview, onApply }: {
  status: IndexStatus | null; docs: IndexDocument[] | null; plan: ScanPlan | null; report: ScanReport | null;
  scanning: boolean; disabled: boolean; onPreview: () => void; onApply: () => void;
}) {
  report = report ?? (plan ? null : status?.last_scan ?? null);
  return <div className="kb-management">
    <section className="kb-panel">
      <div className="kb-section-heading"><div><h2>索引运行状态</h2><p>文档清单与检索索引保持一致，资料才能被完整找到。</p></div></div>
      {status ? <>
        <div className="kb-storage-grid"><div><span>文档清单</span><strong>{status.tracked_chunks.toLocaleString()} <small>块</small></strong></div><div><span>向量索引</span><strong>{status.milvus_chunks?.toLocaleString() ?? '不可用'} <small>{status.milvus_chunks === null ? '' : '块'}</small></strong></div><div><span>关键词索引</span><strong>{status.es_enabled === false ? '未启用' : status.es_docs?.toLocaleString() ?? '不可用'} <small>{status.es_docs === null ? '' : '块'}</small></strong></div></div>
        {status.health?.issues.map(issue => <div className="kb-notice" key={issue}>{issue}</div>)}
        {status.milvus_error && <details className="kb-error-detail"><summary>向量索引错误详情</summary><p>{status.milvus_error}</p></details>}
        {status.es_error && <details className="kb-error-detail"><summary>关键词索引错误详情</summary><p>{status.es_error}</p></details>}
      </> : <p className="kb-caption">暂时无法读取索引状态，请刷新重试。</p>}
    </section>
    <section className="kb-panel">
      <div className="kb-section-heading"><div><h2>目录同步</h2><p>发现新增、修改和移除的文件，按变更更新知识库。</p></div><button type="button" className="btn btn--sm btn--ghost" disabled={disabled || scanning || !status?.watch_dirs.length} onClick={onPreview}>{scanning ? <span className="spinner" /> : <RefreshCw size={13} />}检查变更</button></div>
      {status?.watch_dirs.length ? <div className="kb-watch-dirs">{status.watch_dirs.map(path => <div key={path}><span className="kb-watch-dirs__path">{path}</span><span className="kb-chip">{status.watch_dirs_exist?.find(item => item.dir === path)?.exists === false ? '目录不可用' : '已连接'}</span></div>)}</div> : <div className="kb-notice kb-notice--neutral">当前没有监听目录，上传文档后即可使用知识库。</div>}
      <p className="kb-caption">{status?.watcher.running ? `每 ${status.interval_seconds} 秒自动检查` : '自动同步未运行'} · {status?.last_scan?.finished_at ? `最近扫描 ${formatIsoRelative(status.last_scan.finished_at)}` : '尚无扫描记录'}</p>
      {status && status.watcher.consecutive_failures > 0 && <div className="kb-notice" role="alert">自动同步已连续失败 {status.watcher.consecutive_failures} 次：{status.watcher.last_error}</div>}
      {scanning && <div className="kb-loading" role="status"><span className="spinner" />正在检查或同步文件，请稍候…</div>}
      {plan && <div className="kb-scan-result">
        <h3>{plan.has_changes ? '检测到以下变更' : '资料已是最新'}</h3><p>新增 {plan.added.length} · 更新 {plan.modified.length} · 移除 {plan.deleted.length} · 未变化 {plan.unchanged}</p>
        {[...plan.added.map(item => ({ ...item, action: '新增' })), ...plan.modified.map(item => ({ ...item, action: '更新' })), ...plan.deleted.map(item => ({ ...item, action: '移除' }))].map(item => <div className="kb-scan-result__item" key={`${item.action}:${item.source}`}><span>{item.action}</span>{item.source}</div>)}
        {plan.warnings?.map(warning => <div className="kb-notice" key={warning}>{warning}</div>)}
        {plan.conflicts.map(item => <div className="kb-notice" key={item.source}>来源冲突，已跳过：{item.source}</div>)}
        {plan.skipped_overflow > 0 && <div className="kb-notice">有 {plan.skipped_overflow} 个文件超出本次扫描上限，已保留它们的原索引。</div>}
        {plan.has_changes && <button type="button" className="btn btn--primary" disabled={disabled || scanning} onClick={onApply}>同步这些变更</button>}
      </div>}
      {report && <div className="kb-scan-result" aria-live="polite"><h3>{report.ok ? '同步完成' : '同步结束，有项目需要处理'}</h3><p>新增 {report.added.length} · 更新 {report.modified.length} · 移除 {report.deleted.length} · 失败 {report.failed.length}{report.duration_ms != null ? ` · ${report.duration_ms.toLocaleString()} ms` : ''}</p>{report.failed.map(item => <div className="kb-notice kb-notice--error" key={item.source}>{item.source}：{item.error}</div>)}{report.warnings?.map(warning => <div className="kb-notice" key={warning}>{warning}</div>)}{report.conflicts?.map(item => <div className="kb-notice" key={item.source}>来源冲突，已跳过：{item.source}</div>)}{Boolean(report.skipped_overflow) && <div className="kb-notice">有 {report.skipped_overflow} 个文件超出扫描上限。</div>}</div>}
    </section>
    <section className="kb-panel kb-scope-panel"><KnowledgeScopeEditor docs={docs} /></section>
  </div>;
}
