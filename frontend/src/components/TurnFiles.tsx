/** Deliver generated files immediately; coalesce inventory reads across turns. */

import { Fragment, useEffect, useState } from 'react';

import { Code, Download, Eye, FileSpreadsheet, FileText, History, Package, Presentation } from '@/components/icons';
import {
  conversationFileRevisionUrl,
  conversationFileUrl,
  conversationFilesZipUrl,
  restoreConversationFileRevision,
} from '@/lib/api';
import { isPreviewable, nameOf } from '@/lib/artifacts';
import { fetchMessageFiles } from '@/lib/messageFiles';
import { formatBytes } from '@/lib/format';
import { useAgentStore } from '@/stores/agentStore';
import { useArtifactStore } from '@/stores/artifactStore';
import { toast } from '@/stores/toastStore';
import { confirmAction } from '@/stores/confirmStore';
import type { ConversationFile, TurnFileRef } from '@/types';

function revisionTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

const SUFFIX_ICON: Record<string, typeof FileText> = {
  docx: FileText,
  doc: FileText,
  md: FileText,
  txt: FileText,
  pdf: FileText,
  xlsx: FileSpreadsheet,
  xls: FileSpreadsheet,
  csv: FileSpreadsheet,
  pptx: Presentation,
  ppt: Presentation,
  html: Code,
};

interface TurnFilesProps {
  files: TurnFileRef[];
  threadId?: string;
  preview?: boolean;
}

export function TurnFiles({ files, threadId: suppliedThreadId, preview = true }: TurnFilesProps) {
  const activeThreadId = useAgentStore((s) => s.threadId);
  const threadId = suppliedThreadId ?? activeThreadId;
  const openArtifact = useArtifactStore((s) => s.open);
  const [open, setOpen] = useState(true);
  const [detail, setDetail] = useState<Record<string, ConversationFile> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [historyFor, setHistoryFor] = useState<string | null>(null);
  const [busyRevision, setBusyRevision] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);

  const [showAll, setShowAll] = useState(false);
  const fileKey = files.map(file => file.path).join("\n");
  useEffect(() => {
    setDetail(null); setError(null); setShowAll(false);
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    const load = async () => {
      try {
        const data = await fetchMessageFiles(threadId);
        if (!alive) return;
        setDetail(Object.fromEntries(data.files.map(file => [file.path, file])));
        // done can arrive just before the backend finishes persisting the turn.
        if (files.some(file => !data.files.some(item => item.path === file.path)) && attempts++ < 2) timer = setTimeout(() => void load(), 1000);
      } catch (error) { if (alive) setError(error instanceof Error ? error.message : '无法读取文件信息'); }
    };
    void load();
    return () => { alive = false; clearTimeout(timer); };
  }, [threadId, fileKey, refreshToken]);

  const toggle = () => setOpen(value => !value);

  const restoreVersion = async (path: string, revisionId: string) => {
    if (busyRevision) return;
    if (!await confirmAction({ title: '恢复这个文件版本？', description: `「${nameOf(path)}」的当前内容将被替换为选中的历史版本。`, confirmLabel: '恢复版本', tone: 'warning' })) return;
    setBusyRevision(revisionId);
    try {
      await restoreConversationFileRevision(threadId, path, revisionId);
      toast('已恢复到选中的版本', 'success');
      setRefreshToken((value) => value + 1);
    } catch (error) {
      toast(error instanceof Error ? error.message : '恢复失败', 'error');
    } finally {
      setBusyRevision(null);
    }
  };

  const rows = files.map((f) => {
    const meta = detail?.[f.path];
    return {
      ref: f,
      // detail 还没拉回来 / 该路径未被记录：按「可用」处理，让下载按钮可点，
      // 真丢了后端会返回 404 并以 toast 之外的方式表现在浏览器里（这比把
      // 按钮灰掉、用户不知道为什么更好）。
      exists: meta ? meta.exists : true,
      size: meta?.size ?? 0,
      suffix: meta?.suffix ?? f.path.split('.').pop()?.toLowerCase() ?? '',
      revisions: meta?.revisions ?? [],
    };
  });

  const available = rows.filter((r) => r.exists).length;
  return (
    <div className="turn-files">
      <button type="button" className="turn-files__toggle" onClick={toggle} aria-expanded={open}>
        <Package size={13} />
        <span>
          已生成 {files.length} 个文件
          {detail && available < files.length ? `（${files.length - available} 个已不在）` : ''}
        </span>
        <span className="turn-files__chevron">{open ? '▾' : '▸'}</span>
      </button>

      {open && (
        <div className="turn-files__list">
          {(showAll ? rows : rows.slice(0, 3)).map(({ ref, exists, size, suffix, revisions }) => {
            const Icon = SUFFIX_ICON[suffix] ?? FileText;
            const canPreview = preview && exists;
            const historyOpen = historyFor === ref.path;
            return (
              <Fragment key={ref.path}>
                <div className={`turn-file${exists ? '' : ' turn-file--missing'}`}>
                  <span className="turn-file__icon">
                    <Icon size={14} />
                  </span>
                  <span className="turn-file__name" title={ref.path}>
                    {nameOf(ref.path)}
                  </span>
                  {size > 0 && <span className="turn-file__size">{formatBytes(size)}</span>}
                  {revisions.length > 0 && (
                    <button
                      type="button"
                      className="turn-file__history"
                      onClick={() => setHistoryFor(historyOpen ? null : ref.path)}
                      aria-expanded={historyOpen}
                      title="查看历史版本"
                    >
                      <History size={12} />
                      {revisions.length} 版
                    </button>
                  )}
                  {exists ? (
                    <>
                      {canPreview && (
                        <button
                          type="button"
                          className="turn-file__preview"
                          onClick={() => openArtifact({ path: ref.path, name: ref.path, threadId })}
                          title="在画布中预览"
                        >
                          <Eye size={13} />
                          {isPreviewable(ref.path) ? '预览' : '查看'}
                        </button>
                      )}
                      <a
                        className="turn-file__download"
                        href={conversationFileUrl(threadId, ref.path)}
                        download
                        title="下载这个文件"
                        aria-label={"下载 " + nameOf(ref.path)}
                      >
                        <Download size={13} />
                      </a>
                    </>
                  ) : (
                    <span className="turn-file__missing" title="文件已被删除或移动">
                      已不在
                    </span>
                  )}
                </div>
                {historyOpen && (
                  <div className="turn-file__revisions">
                    {[...revisions].reverse().map((rev, index) => (
                      <div className="turn-file__revision" key={rev.revision_id}>
                        <span className="turn-file__revision-time">{revisionTime(rev.created_at)}</span>
                        <span className="turn-file__revision-size">{formatBytes(rev.size)}</span>
                        {index === 0 && <span className="turn-file__revision-tag">最新快照</span>}
                        {rev.restored_from && <span className="turn-file__revision-tag">恢复</span>}
                        <span className="turn-file__revision-actions">
                          {index > 0 && (
                            <button
                              type="button"
                              className="btn btn--sm btn--ghost"
                              disabled={busyRevision !== null}
                              onClick={() => void restoreVersion(ref.path, rev.revision_id)}
                            >
                              {busyRevision === rev.revision_id ? '恢复中…' : '恢复'}
                            </button>
                          )}
                          <a
                            className="turn-file__revision-download"
                            href={conversationFileRevisionUrl(threadId, ref.path, rev.revision_id)}
                            download
                            title="下载这个版本"
                          >
                            <Download size={12} /> 下载
                          </a>
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </Fragment>
            );
          })}

          {rows.length > 3 && <button type="button" className="btn btn--sm btn--ghost" onClick={() => setShowAll(value => !value)}>{showAll ? '收起' : `查看其余 ${rows.length - 3} 个文件`}</button>}
          {rows.length > 1 && (
            <a className="turn-files__zip" href={conversationFilesZipUrl(threadId)} download>
              <Download size={12} /> 下载会话全部文件
            </a>
          )}
          {error && <div className="turn-files__error" role="status">文件状态暂未确认：{error}</div>}
        </div>
      )}
    </div>
  );
}
