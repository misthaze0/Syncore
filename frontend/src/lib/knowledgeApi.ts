import { API_BASE } from '@/lib/api';
import type { FileUploadResponse, IndexDocument, IndexStatus, ScanPlan, ScanReport } from '@/types';

export interface KnowledgeCatalog {
  documents: IndexDocument[];
  total: number;
  limit: number;
  offset: number;
  summary: { documents: number; chunks: number; needs_attention: number };
}

export interface KnowledgeChunk { chunk_index: number; text: string; page: number }
export interface KnowledgeChunks {
  source: string;
  chunks: KnowledgeChunk[];
  total: number;
  offset: number;
  warnings: string[];
}
export type RetrievalMode = 'hybrid' | 'vector' | 'bm25' | 'hybrid_graph';
export interface RetrievalHit extends KnowledgeChunk {
  source: string;
  score?: number | null;
  rrf_score?: number | null;
  rerank_score?: number | null;
  retrieval?: string;
  recalled_by?: string[];
}
export interface RetrievalTestResult {
  query: string;
  mode: RetrievalMode;
  results: RetrievalHit[];
  duration_ms: number;
  reranked: boolean;
  lanes: Array<{ name: string; status: 'ok' | 'error' | 'disabled'; hits: number; error?: string }>;
  warnings: string[];
}
export interface KnowledgeUploadResponse extends FileUploadResponse {
  warnings: string[];
  unchanged: boolean;
  keyword_indexed: boolean | null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    const data = await response.json().catch(() => null) as { detail?: unknown } | null;
    throw new Error(typeof data?.detail === 'string' ? data.detail : `请求失败（${response.status}）`);
  }
  return response.json() as Promise<T>;
}

function post<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  return request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal });
}

export const getKnowledgeStatus = (signal?: AbortSignal) => request<IndexStatus>('/index/status', { signal });
export function getKnowledgeDocuments(options: { q?: string; state?: string; sort?: string; limit?: number; offset?: number } = {}, signal?: AbortSignal) {
  const params = new URLSearchParams();
  Object.entries(options).forEach(([key, value]) => { if (value !== undefined && value !== '') params.set(key, String(value)); });
  return request<KnowledgeCatalog>(`/index/documents?${params}`, { signal });
}
export function getKnowledgeChunks(source: string, offset = 0, signal?: AbortSignal) {
  return request<KnowledgeChunks>(`/index/chunks?${new URLSearchParams({ source, offset: String(offset), limit: '8' })}`, { signal });
}
export const removeKnowledgeDocument = (source: string) => request<{ ok: boolean }>(`/index/documents/${encodeURIComponent(source)}`, { method: 'DELETE' });
export const reindexKnowledgeDocument = (source: string) => post<{ ok: boolean; chunks: number; warnings: string[] }>('/index/documents/reindex', { source });
export const previewKnowledgeScan = () => post<ScanPlan>('/index/scan?dry_run=true');
export const applyKnowledgeScan = () => post<ScanReport>('/index/scan');
export const testKnowledgeRetrieval = (body: { query: string; top_k: number; mode: RetrievalMode; rerank: boolean; source?: string }, signal?: AbortSignal) => post<RetrievalTestResult>('/index/search', body, signal);
export async function uploadKnowledgeFile(file: File): Promise<KnowledgeUploadResponse> {
  const body = new FormData();
  body.append('file', file);
  const response = await request<KnowledgeUploadResponse>('/files/upload', { method: 'POST', body });
  if (!response.success) throw new Error(response.message || '入库失败，请重试');
  return { ...response, warnings: response.warnings ?? [] };
}
