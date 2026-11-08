import { describeToolCall } from './humanize';
import type { ToolCallInfo, ToolCallStatus, SubtaskStatus, SourceDoc } from '@/types';

export type PresentedToolStatus = ToolCallStatus | 'interrupted';

export function toolStatus(tool: Pick<ToolCallInfo, 'status'>, streaming: boolean): PresentedToolStatus {
  return ['pending', 'awaiting_approval'].includes(tool.status) && !streaming ? 'interrupted' : tool.status;
}

export function toolActivity(tool: Pick<ToolCallInfo, 'toolName' | 'toolLabel' | 'args'>, status: PresentedToolStatus): string {
  const action = describeToolCall(tool.toolName, tool.args, tool.toolLabel);
  return status === 'pending' ? action : action.replace(/^正在/, '');
}

export function safeSourceUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

/** Accept the source lists produced by both web tools, without treating arbitrary text as a URL. */
export function splitWebResult(result: string): { body: string; sources: string[] } {
  const lines = result.split(/\r?\n/);
  const marker = lines.findIndex(line => /^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:来源|参考来源|Sources?)(?:\*\*)?\s*[:：]?\s*$/i.test(line));
  const candidates = marker >= 0 ? lines.slice(marker + 1) : lines;
  const sources = candidates.flatMap(line => {
    const candidate = line.trim().replace(/^(?:[-*•]|\d+[.)])\s+/, '');
    const markdown = /^\[[^\]]*\]\((https?:\/\/[^\s]+)\)/i.exec(candidate);
    const value = markdown?.[1] ?? (/^<?https?:\/\/\S+>?$/i.test(candidate) ? candidate.replace(/^<|>$/g, '') : '');
    const url = safeSourceUrl(value);
    return url ? [url] : [];
  });
  return { body: marker >= 0 ? lines.slice(0, marker).join('\n').trimEnd() : result, sources: [...new Set(sources)] };
}

export function webSources(tools: Pick<ToolCallInfo, 'toolName' | 'result' | 'status'>[]): string[] {
  return [...new Set(tools.flatMap(tool => tool.status === 'success' && ['web_search', 'web_fetch'].includes(tool.toolName)
    ? splitWebResult(tool.result ?? '').sources : []))];
}

export type MessageCitation = Pick<SourceDoc, 'source' | 'page' | 'preview'>;

/** Read the numbered citations emitted by builtin kb_search, not arbitrary file mentions. */
export function knowledgeSources(tools: Pick<ToolCallInfo, 'toolName' | 'result' | 'status'>[]): MessageCitation[] {
  return tools.flatMap(tool => {
    if (tool.toolName !== 'kb_search' || tool.status !== 'success') return [];
    const result: MessageCitation[] = [];
    for (const line of (tool.result ?? '').split(/\r?\n/)) {
      const header = /^\d+\.\s+(.+?)(?:（第\s*(\d+)\s*页）)?｜/.exec(line);
      if (header) result.push({ source: header[1], page: Number(header[2] ?? 0), preview: '' });
      else if (/^\s{3}\S/.test(line) && result.length) result[result.length - 1].preview += line.trim() + '\n';
    }
    return result;
  });
}

export function approvalToolStates(tools: ToolCallInfo[], approved: boolean): ToolCallInfo[] {
  return tools.map(tool => tool.status === 'awaiting_approval'
    ? { ...tool, status: approved ? 'pending' : 'rejected' } : tool);
}

export function backgroundTaskLabel(statuses: SubtaskStatus[]): string {
  // ★ 两种「停在人身上」分开数：等确认（批不批）与等你回答（选哪个）
  // 混成一句「等待中」的话，用户点开发现要答一道题，会以为点错了。
  const waiting = statuses.filter(status => status === 'waiting_approval').length;
  const answering = statuses.filter(status => status === 'waiting_input').length;
  const active = statuses.filter(status => ['pending', 'running', 'waiting_approval', 'waiting_input'].includes(status)).length;
  // ★ interrupted 不算 active（它不动、也不烧 token），但它是**要用户动手**的一项 ——
  // 混进「未完成」里会让人以为只是一次失败，而其实只差点一下「继续」。
  const resumable = statuses.filter(status => status === 'interrupted').length;
  const incomplete = statuses.filter(status => ['failed', 'cancelled', 'orphaned'].includes(status)).length;
  if (waiting) return `${waiting} 项后台任务等待确认`;
  if (answering) return `${answering} 项后台任务等你回答`;
  if (resumable) return `${resumable} 项后台任务等你继续`;
  if (active) return `${active} 项后台任务进行中`;
  if (incomplete) return `后台任务 · ${incomplete} 项未完成`;
  return statuses.length ? `${statuses.length} 项后台任务已完成` : '后台任务';
}
