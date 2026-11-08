import { memo, useId, useState } from 'react';
import { ChevronRight, CircleCheck, CircleSlash, CircleX, Globe, Hourglass, Loader2, TriangleAlert, type LucideIcon } from '@/components/icons';
import { MarkdownRenderer } from '@/components/MarkdownRenderer';
import { splitWebResult, toolActivity, type PresentedToolStatus } from '@/lib/messagePresentation';
import { openWebLink } from '@/stores/artifactStore';
import type { ToolCallInfo } from '@/types';

interface ToolCallViewProps {
  toolName: string;
  toolLabel?: string;
  args: Record<string, unknown>;
  result?: string;
  success?: boolean;
  pending?: boolean;
  dangerLevel?: ToolCallInfo['dangerLevel'];
  status?: ToolCallInfo['status'];
  reason?: string;
  durationMs?: number;
  settled?: boolean;
}
const STATUS_ICON: Record<PresentedToolStatus, LucideIcon> = { pending: Loader2, awaiting_approval: Hourglass, success: CircleCheck, failed: CircleX, rejected: CircleSlash, interrupted: TriangleAlert };
const STATUS_TEXT: Record<PresentedToolStatus, string> = { pending: '进行中', awaiting_approval: '等你确认', success: '已完成', failed: '未成功', rejected: '未执行', interrupted: '已中断' };

function WebResult({ result }: { result: string }) {
  const { body, sources } = splitWebResult(result);
  return <div className="tool-call__websearch">
    {body && <MarkdownRenderer content={body}/>}
    {sources.length > 0 && <ul className="tool-call__source-list">{sources.map(url => <li key={url}>
      <button type="button" className="tool-call__source-link" onClick={() => openWebLink(url)} title={url}><Globe size={12}/><span className="tool-call__source-url">{new URL(url).hostname}</span></button>
    </li>)}</ul>}
  </div>;
}
function readableResult(result: string) {
  try {
    const value = JSON.parse(result);
    if (value?.plugin === 'builtin-office' && value.schema_version === '1.0' && typeof value.summary === 'string') {
      const lines = [value.summary];
      if (typeof value.content === 'string' && value.content) lines.push(value.content);
      if (value.operation === 'capabilities') {
        const data = value.data;
        if (Array.isArray(data?.native?.read)) lines.push('可读取：' + data.native.read.join('、'));
        if (Array.isArray(data?.native?.edit)) lines.push('可编辑：' + data.native.edit.join('、'));
        lines.push('本地格式转换：' + (data?.filee ? '已配置' : '待配置'));
      }
      if (Array.isArray(value.artifacts)) {
        for (const artifact of value.artifacts) if (typeof artifact.path === 'string') lines.push('文件：' + artifact.path);
      }
      if (Array.isArray(value.warnings)) lines.push(...value.warnings.filter((item: unknown) => typeof item === 'string'));
      return lines.join('\n\n');
    }
    return JSON.stringify(value, null, 2);
  } catch { return result; }
}
export const ToolCallView = memo(function ToolCallView({ toolName, toolLabel, args, result, success, pending, dangerLevel = 'safe', status, reason, durationMs, settled = false }: ToolCallViewProps) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const storedStatus = status ?? (pending ? 'awaiting_approval' : success === undefined ? 'pending' : success ? 'success' : 'failed');
  const resolvedStatus: PresentedToolStatus = ['pending', 'awaiting_approval'].includes(storedStatus) && settled ? 'interrupted' : storedStatus;
  const StatusIcon = STATUS_ICON[resolvedStatus];
  const hasResult = typeof result === 'string' && result.length > 0;
  return <div className={'tool-call tool-call--' + resolvedStatus}>
    <button type="button" className="tool-call__head" onClick={() => setOpen(value => !value)} aria-expanded={open} aria-controls={bodyId}>
      <span className={'tool-call__status tool-call__status--' + resolvedStatus}><StatusIcon size={13} className={resolvedStatus === 'pending' ? 'spin' : undefined}/></span>
      <span className="tool-call__name">{toolActivity({ toolName, toolLabel, args }, resolvedStatus)}</span>
      <span className="tool-call__spacer"/>
      {durationMs !== undefined && <span className="tool-call__elapsed">{durationMs >= 1000 ? (durationMs / 1000).toFixed(1) + 's' : Math.round(durationMs) + 'ms'}</span>}
      <span className="tool-call__duration">{STATUS_TEXT[resolvedStatus]}</span>
      <ChevronRight size={13} className={'tool-call__chevron' + (open ? ' tool-call__chevron--open' : '')}/>
    </button>
    {open && <div className="tool-call__body" id={bodyId}>
      {reason && <div className="tool-call__reason"><TriangleAlert size={12}/>{reason}</div>}
      {hasResult ? <>{['web_search', 'web_fetch'].includes(toolName) ? <WebResult result={result}/> : <pre className={'tool-call__code' + (resolvedStatus === 'failed' ? ' tool-call__code--error' : '')} tabIndex={0} aria-label="执行结果">{readableResult(result)}</pre>}</> : !reason && <p className="tool-call__label">{resolvedStatus === 'awaiting_approval' ? '确认后才会执行' : resolvedStatus === 'rejected' ? '这个操作没有执行' : resolvedStatus === 'interrupted' ? '本轮已结束，没有收到执行结果' : resolvedStatus === 'success' ? '执行完成，没有返回正文' : resolvedStatus === 'failed' ? '未能完成这个操作' : '正在等待执行结果'}</p>}
      <details className="tool-call__parameters"><summary>操作详情{dangerLevel === 'danger' ? ' · 需要确认的操作' : ''}</summary><code>{toolLabel && toolLabel !== toolName ? `${toolLabel} · ${toolName}` : toolName}</code><pre className="tool-call__code" tabIndex={0}>{JSON.stringify(args, null, 2)}</pre></details>
    </div>}
  </div>;
});
