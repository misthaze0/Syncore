import { API_BASE } from '@/lib/api';
import { parseSSE } from '@/lib/sse';
import { safeFileName } from '@/lib/exportConversation';
import type { OfficeAIEvent, OfficeDocument, OfficeKind, OfficeMessage, OfficeSelection, OfficeSummary, OfficeVersion, SaveReason } from '@/types/office';

const ROOT = `${API_BASE}/office`;

export class OfficeConflict extends Error {
  constructor(public document: OfficeDocument) {
    super('文档已在另一个窗口更新，本地修改已保留');
  }
}

async function checked(response: Response): Promise<Response> {
  if (response.ok) return response;
  const data = await response.json().catch(() => null);
  if (response.status === 409 && data?.detail?.document) throw new OfficeConflict(data.detail.document);
  const message = typeof data?.detail === 'string' ? data.detail : data?.detail?.message;
  throw new Error(message || `操作失败（${response.status}），请稍后重试`);
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await checked(await fetch(`${ROOT}${path}`, init));
  return response.json() as Promise<T>;
}

const body = (method: string, value: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });

export const officeApi = {
  list: () => json<{ documents: OfficeSummary[] }>('/documents'),
  get: (id: string) => json<OfficeDocument>(`/documents/${id}`),
  create: (title: string, content: string, kind: OfficeKind = 'document', messages: OfficeMessage[] = []) => json<OfficeDocument>('/documents', body('POST', { title, content, kind, messages })),
  update: (document: OfficeDocument, reason: SaveReason) => json<OfficeDocument>(`/documents/${document.id}`, body('PUT', {
    expected_revision: document.revision, title: document.title.trim() || '未命名文档', content: document.content,
    kind: document.kind, favorite: document.favorite, trashed: document.trashed, messages: document.messages.slice(-100), reason,
  })),
  versions: (id: string) => json<{ versions: OfficeVersion[] }>(`/documents/${id}/versions`),
  version: (id: string, versionId: string) => json<OfficeVersion>(`/documents/${id}/versions/${versionId}`),
  import: (file: File) => {
    const form = new FormData();
    form.append('file', file);
    return json<{ document: OfficeDocument; warnings: string[] }>('/import', { method: 'POST', body: form });
  },
  export: async (id: string, title: string, format: string) => {
    const response = await checked(await fetch(`${ROOT}/documents/${id}/export?format=${encodeURIComponent(format)}`));
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = `${safeFileName(title, '文档')}.${format}`;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 30000);
  },
};

export async function* assistOfficeDocument(document: OfficeDocument, message: string, mode: 'chat' | 'edit', selection: OfficeSelection | null, signal: AbortSignal): AsyncGenerator<OfficeAIEvent> {
  const response = await checked(await fetch(`${ROOT}/documents/${document.id}/assist`, {
    ...body('POST', { message, title: document.title, content: document.content, mode,
      selection: selection?.text || '', selection_html: selection?.html || '', history: document.messages.filter(item => item.status !== 'pending' && item.status !== 'error').slice(-12),
    }), signal,
  }));
  yield* parseSSE<OfficeAIEvent>(response);
}
