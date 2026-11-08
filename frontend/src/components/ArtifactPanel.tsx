/** File preview and the persistent browser / terminal workbench. */

import { useCallback, useEffect, useState } from 'react';

import {
  Check,
  Code,
  Copy,
  Download,
  ExternalLink,
  Eye,
  FileText,
  Loader2,
  TriangleAlert,
  X,
} from '@/components/icons';

import { MarkdownRenderer } from '@/components/MarkdownRenderer';
import { useT } from '@/i18n';
import { saveCanvasFile, convertCanvasFile, conversationFileInlineUrl, conversationFileUrl, fetchConversationFileContent } from '@/lib/api';
import { SpreadsheetPreview, csvCell } from '@/components/SpreadsheetPreview';
import {
  ARTIFACT_KIND_LABEL,
  artifactKind,
  languageOf,
  nameOf,
  parseCsv,
  suffixOf,
  type ArtifactKind,
} from '@/lib/artifacts';
import { formatBytes } from '@/lib/format';
import { useAgentStore } from '@/stores/agentStore';
import { useSidePanelStore } from '@/stores/artifactStore';
import { WorkbenchPanel } from '@/components/workbench/WorkbenchPanel';
import { toast } from '@/stores/toastStore';
import type { ConversationFileContent } from '@/types';

/** 新窗口打开时用的 MIME —— 决定浏览器是「渲染」还是「下载」 */
const MIME: Record<string, string> = {
  html: 'text/html',
  htm: 'text/html',
  svg: 'image/svg+xml',
  md: 'text/markdown',
  markdown: 'text/markdown',
  csv: 'text/csv',
  json: 'application/json',
};

/** 这些类型的「源码」和「预览」不一样，才值得给切换按钮 */
const HAS_SOURCE_VIEW: ReadonlySet<ArtifactKind> = new Set<ArtifactKind>([
  'html',
  'svg',
  'markdown',
  'csv',
]);

function CsvTable({ content, path }: { content: string; path: string }) {
  const rows = parseCsv(content, suffixOf(path) === 'tsv' ? '\t' : ',');
  const columns = rows.reduce((max, row) => Math.max(max, row.length), 0);
  return <SpreadsheetPreview data={{ kind: 'spreadsheet', truncated: rows.length > 500 || columns > 60, sheets: [{
    name: nameOf(path), rows: rows.slice(0, 500).map(row => row.slice(0, 60).map(csvCell)), total_rows: rows.length,
    total_columns: columns, truncated: rows.length > 500 || columns > 60, formula_count: 0,
  }] }} />;
}

function ImagePreview({ url, name }: { url: string; name: string }) {
  const [zoom, setZoom] = useState(0);
  const [failed, setFailed] = useState(false);
  const [naturalWidth, setNaturalWidth] = useState(0);
  if (failed) return <div className="artifact-state artifact-state--error">图片无法解码，请使用标题栏下载按钮查看原文件。</div>;
  return <div className="artifact-image">
    <div className="sheet-preview__toolbar"><button type="button" className="btn btn--sm" onClick={() => setZoom(0)}>适应画布</button><label>缩放 <select aria-label="图片缩放" value={zoom} onChange={event => setZoom(Number(event.target.value))}><option value={0}>自适应</option>{[25, 50, 75, 100, 150, 200, 300].map(value => <option key={value} value={value}>{value}%</option>)}</select></label></div>
    <div className={`artifact-image__scroll${zoom ? '' : ' artifact-image__scroll--fit'}`}><img src={url} alt={name} style={zoom && naturalWidth ? { width: naturalWidth * zoom / 100, maxWidth: 'none' } : undefined} onLoad={event => setNaturalWidth(event.currentTarget.naturalWidth)} onError={() => setFailed(true)} /></div>
  </div>;
}

// ══════════════════════════════════════════════════════
//  页签 1：产物画布（原 ArtifactPanel 的全部能力）
// ══════════════════════════════════════════════════════

function ArtifactsTab() {
  const t = useT();
  const target = useSidePanelStore((s) => s.target);
  const close = useSidePanelStore((s) => s.close);
  const activeThreadId = useAgentStore((s) => s.threadId);
  const threadId = target?.threadId ?? activeThreadId;

  const [data, setData] = useState<ConversationFileContent | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<'preview' | 'source'>('preview');
  const [copied, setCopied] = useState(false);
  const [retry, setRetry] = useState(0);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [format, setFormat] = useState('pdf');


  const path = target?.path ?? '';
  const draftKey = `canvas-draft:${threadId}:${path}`;


  useEffect(() => {
    if (!target) {
      setData(null);
      setError(null);
      return;
    }
    // 换了个文件就回到预览态、清掉上一份内容 —— 否则会有一瞬间显示旧文件
    setEditing(false);
    setView('preview');
    setData(null);
    setError(null);
    setCopied(false);

    let alive = true;
    setLoading(true);
    fetchConversationFileContent(threadId, target.path)
      .then((res) => {
        if (alive) setData(res);
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message.replace(/^请求失败 \(\d+\):\s*/, '') : t('panel.readFailed'));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, threadId, retry]);

  const content = editing ? draft : data?.content ?? '';
  const dirty = editing && draft !== data?.content;
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  const save = async () => {
    setBusy(true);
    try { await saveCanvasFile(threadId, path, draft, data?.content ?? ''); setData(previous => previous ? { ...previous, content: draft, size: new Blob([draft]).size } : previous); sessionStorage.removeItem(draftKey); setEditing(false); toast('已保存', 'success'); }
    catch (error) { toast(error instanceof Error ? error.message : '保存失败', 'error'); }
    finally { setBusy(false); }
  };

  const preview = data?.preview;
  const copyContent = content || (preview?.kind === 'document' ? preview.text : preview?.kind === 'spreadsheet'
    ? preview.sheets.map(sheet => `${sheet.name}\n${sheet.rows.map(row => row.map(cell => cell.text).join('\t')).join('\n')}`).join('\n\n') : '');
  const kind: ArtifactKind | null = path ? artifactKind(path) : null;
  const inline = kind !== null && ['image', 'pdf', 'audio', 'video'].includes(kind);

  const handleCopy = useCallback(() => {
    if (!copyContent) return;
    void navigator.clipboard.writeText(copyContent).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    }).catch(() => toast(t('feedback.copyFailed'), 'error'));
  }, [copyContent, t]);

  /**
   * 新窗口打开 —— 生成的 HTML 有脚本、有样式，独立窗口里看最接近它本来的样子
   * （面板里的 iframe 是嵌在聊天界面里的）。
   */
  const handleOpenWindow = useCallback(() => {
    if (inline) { window.open(conversationFileInlineUrl(threadId, path), '_blank', 'noopener,noreferrer'); return; }
    if (!content) return;
    const mime = MIME[suffixOf(path)] ?? 'text/plain';
    const url = URL.createObjectURL(new Blob([content], { type: `${mime};charset=utf-8` }));
    const opened = window.open(url, '_blank', 'noopener');
    if (!opened) {
      toast(t('panel.popupBlocked'), 'error');
    }
    // 给新窗口留足够时间加载，再回收这个 blob url
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }, [content, path, t, inline, threadId]);

  if (!target) {
    return (
      <div className="artifact-state artifact-state--empty">
        <FileText size={22} />
        <div>{t('panel.artifactsEmpty')}</div>
        <p>{t('panel.artifactsEmptyHint')}</p>
      </div>
    );
  }

  const showToggle = kind !== null && HAS_SOURCE_VIEW.has(kind);
  const effectiveView = showToggle ? view : 'preview';

  const renderBody = () => {
    if (loading) {
      return (
        <div className="artifact-state">
          <Loader2 size={16} className="spin" />
          {t('common.loading')}
        </div>
      );
    }
    if (error) {
      return (
        <div className="artifact-state artifact-state--error">
          <TriangleAlert size={15} />
          {error}
          <button type="button" className="btn btn--sm" onClick={() => setRetry(value => value + 1)}>重新加载</button>
          <a className="btn btn--sm" href={conversationFileUrl(threadId, path)} download><Download size={13} /> 下载原文件</a>
        </div>
      );
    }
    if (!data) return null;

    const preview = data.preview;
    if (preview?.kind === 'spreadsheet') return <SpreadsheetPreview key={`${threadId}:${path}:${retry}`} data={preview} />;
    if (preview?.kind === 'document') return <div className="artifact-document"><p className="artifact-panel__notice">{preview.notice}</p><div className="artifact-document__page">{preview.blocks?.length ? preview.blocks.map((block, i) => block.kind === 'table' ? <div key={i} className="artifact-table__scroll"><table className="artifact-table"><tbody>{block.rows.map((row, r) => <tr key={r}>{row.map((cell, c) => <td key={c}>{cell}</td>)}</tr>)}</tbody></table></div> : block.kind === 'heading' ? <h3 key={i}>{block.text}</h3> : <p key={i}>{block.text}</p>) : <pre>{preview.text || '文档中没有可提取的文字'}</pre>}</div></div>;
    if (preview?.kind === 'archive') return <div className="artifact-table__scroll"><div className="artifact-panel__notice">压缩包内共 {preview.total} 项，仅浏览目录，不解压或运行文件。</div><table className="artifact-table"><thead><tr><th>文件</th><th>大小</th></tr></thead><tbody>{preview.entries.map((entry, i) => <tr key={i}><td>{entry.name}</td><td>{formatBytes(entry.size)}</td></tr>)}</tbody></table></div>;
    const mediaUrl = conversationFileInlineUrl(threadId, path);
    if (kind === 'image') return <ImagePreview key={`${threadId}:${path}`} url={mediaUrl} name={nameOf(path)} />;
    if (kind === 'pdf') return <object className="artifact-frame" data={mediaUrl} type="application/pdf" aria-label={nameOf(path)}><div className="artifact-state">当前环境未能显示 PDF。<a href={mediaUrl} target="_blank" rel="noreferrer">在新窗口查看</a><a href={conversationFileUrl(threadId, path)} download>下载 PDF</a></div></object>;
    if (kind === 'audio' || kind === 'video') return <div className="artifact-media">{kind === 'video' ? <video key={mediaUrl} controls preload="metadata" src={mediaUrl} /> : <audio key={mediaUrl} controls preload="metadata" src={mediaUrl} />}<p>若当前环境不支持此编码，可下载原文件播放。</p></div>;

    if (!data.is_text) {
      return (
        <div className="artifact-state">
          <FileText size={18} />
          <div>{t('panel.notPreviewable')}</div>
          <a className="btn btn--sm" href={conversationFileUrl(threadId, path)} download>
            <Download size={13} /> {t('panel.downloadToOpen')}
          </a>
        </div>
      );
    }

    if (effectiveView === 'source' || kind === null || kind === 'code' || kind === 'text') {
      return (
        <pre className="artifact-code">
          <code>{content}</code>
        </pre>
      );
    }

    if (kind === 'markdown') {
      return (
        <div className="artifact-md">
          <MarkdownRenderer content={content} />
        </div>
      );
    }
    if (kind === 'csv') return <CsvTable key={`${threadId}:${path}`} content={content} path={path} />;
    if (kind === 'html' || kind === 'svg') {
      return (
        <iframe
          className="artifact-frame"
          title={nameOf(path)}
          // ★ 不加 allow-same-origin：模型写的内容等同于不可信代码，
          //   它能跑自己的脚本（图表需要），但碰不到父页面的任何东西。
          sandbox="allow-scripts allow-popups allow-forms"
          srcDoc={content}
        />
      );
    }
    return (
      <pre className="artifact-code">
        <code>{content}</code>
      </pre>
    );
  };

  return (
    <div className="artifact-tab">
      <div className="artifact-tab__filebar">
        <div className="artifact-panel__title">
          <span className="artifact-panel__name" title={path}>
            {nameOf(path)}
          </span>
          {kind && <span className="artifact-panel__kind">{ARTIFACT_KIND_LABEL[kind]}</span>}
          {data && <span className="artifact-panel__meta">{formatBytes(data.size)}</span>}
        </div>

        <div className="artifact-panel__actions">
          {showToggle && (
            <button
              type="button"
              className={`icon-btn${view === 'source' ? ' icon-btn--active' : ''}`}
              onClick={() => setView(view === 'source' ? 'preview' : 'source')}
              title={view === 'source' ? t('panel.showPreview') : t('panel.showSource')}
            >
              {view === 'source' ? <Eye size={13} /> : <Code size={13} />}
            </button>
          )}
          <button type="button" className="icon-btn" onClick={handleCopy} disabled={!copyContent} title={t('panel.copyFile')}>
            {copied ? <Check size={13} /> : <Copy size={13} />}
          </button>
          <button type="button" className="icon-btn" onClick={handleOpenWindow} disabled={!content && !inline} title={t('panel.openWindow')}>
            <ExternalLink size={13} />
          </button>
          <a className="icon-btn" href={conversationFileUrl(threadId, path)} download title={t('panel.downloadFile')}>
            <Download size={13} />
          </a>
          <button type="button" className="icon-btn" onClick={close} title={t('panel.closeFile')}>
            <X size={13} />
          </button>
        </div>
      </div>

      {data?.truncated && (
        <div className="artifact-panel__notice">
          {data.preview ? '当前预览仅显示部分内容，下载原文件可查看完整内容。' : t('panel.truncatedNotice', { size: formatBytes(data.size) })}
        </div>
      )}

      {kind === 'code' && (
        <div className="artifact-panel__sub">
          <span className="artifact-panel__lang">{languageOf(path)}</span>
        </div>
      )}

      {data && <div className="sheet-preview__toolbar">
        {data.is_text && !data.truncated && !editing && <button className="btn btn--sm" onClick={() => { setDraft(sessionStorage.getItem(draftKey) ?? data.content ?? ''); setEditing(true); }}>编辑文件</button>}
        {editing && <><button className="btn btn--sm" disabled={busy} onClick={() => void save()}>{busy ? '保存中…' : '保存'}</button><button className="btn btn--sm" disabled={busy} onClick={() => { if (!dirty || window.confirm('放弃未保存的修改？')) { sessionStorage.removeItem(draftKey); setEditing(false); } }}>取消</button><span>{dirty ? '未保存' : '编辑中'} · 下方预览随编辑更新</span></>}
        {['doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'pdf', 'odt', 'ods', 'odp', 'rtf'].includes(suffixOf(path)) && <><select aria-label="转换目标格式" value={format} onChange={event => setFormat(event.target.value)}>{['pdf', 'docx', 'xlsx', 'pptx'].map(item => <option key={item} value={item}>{item.toUpperCase()}</option>)}</select><button className="btn btn--sm" disabled={busy || format === suffixOf(path)} onClick={async () => { setBusy(true); try { await convertCanvasFile(threadId, path, format); toast('转换完成，已下载', 'success'); } catch (error) { toast(error instanceof Error ? error.message : '转换失败', 'error'); } finally { setBusy(false); } }}>{busy ? '处理中…' : '转换并下载'}</button></>}
      </div>}
      {editing && <textarea aria-label="编辑文件内容" className="artifact-editor" spellCheck={false} value={draft} disabled={busy} onChange={event => { setDraft(event.target.value); try { sessionStorage.setItem(draftKey, event.target.value); } catch { /* storage unavailable */ } }} onKeyDown={event => { if ((event.ctrlKey || event.metaKey) && event.key === 's') { event.preventDefault(); if (!busy) void save(); } }} />}
      <div className="artifact-panel__body">{renderBody()}</div>
    </div>
  );
}

/** Persistent workbench: tool switching and collapsing keep live sessions mounted. */
export function SidePanel({ onOpenSettings }: { onOpenSettings?: (tab: string) => void }) {
  return <WorkbenchPanel artifacts={<ArtifactsTab />} onOpenSettings={onOpenSettings} />;
}

export const ArtifactPanel = SidePanel;
