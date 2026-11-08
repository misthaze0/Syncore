import { create } from 'zustand';

import { officeApi, OfficeConflict } from '@/lib/officeApi';
import { toast } from '@/stores/toastStore';
import type { OfficeDocument, OfficeKind, OfficeSummary, SaveReason } from '@/types/office';

const ACTIVE_KEY = 'xjms.office.active';
const DRAFT_PREFIX = 'xjms.office.draft.';
let saveFlight: Promise<boolean> | null = null;

function storageRead(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}

function remember(document: OfficeDocument): boolean {
  try { localStorage.setItem(DRAFT_PREFIX + document.id, JSON.stringify(document)); return true; } catch { return false; }
}

function forget(id: string): void {
  try { localStorage.removeItem(DRAFT_PREFIX + id); } catch { /* Optional storage. */ }
}

function summary(document: OfficeDocument): OfficeSummary {
  const element = new DOMParser().parseFromString(document.content, 'text/html');
  const text = element.body.textContent || '';
  const { content: _content, messages: _messages, ...rest } = document;
  return { ...rest, excerpt: text.slice(0, 100), characters: text.replace(/\s/g, '').length };
}

interface OfficeState {
  documents: OfficeSummary[];
  document: OfficeDocument | null;
  loading: boolean;
  opening: boolean;
  dirty: boolean;
  draftStored: boolean;
  saving: boolean;
  error: string | null;
  saveError: string | null;
  conflict: OfficeDocument | null;
  editVersion: number;
  assistantAbort: AbortController | null;
  load: () => Promise<void>;
  open: (id: string) => Promise<void>;
  home: () => Promise<void>;
  create: (title?: string, content?: string, kind?: OfficeKind) => Promise<void>;
  adopt: (document: OfficeDocument) => void;
  update: (patch: Partial<Pick<OfficeDocument, 'title' | 'content' | 'favorite' | 'trashed' | 'messages'>>) => void;
  save: (reason?: SaveReason) => Promise<boolean>;
  duplicate: () => Promise<void>;
  reload: () => Promise<void>;
  trash: () => Promise<void>;
  restore: (id: string) => Promise<void>;
  stopAssistant: () => void;
}

export const useOfficeStore = create<OfficeState>((set, get) => ({
  documents: [], document: null, loading: false, opening: false, dirty: false, draftStored: false, saving: false,
  error: null, saveError: null, conflict: null, editVersion: 0, assistantAbort: null,

  load: async () => {
    set({ loading: true, error: null });
    try {
      const { documents } = await officeApi.list();
      set({ documents });
      const active = storageRead(ACTIVE_KEY);
      if (!get().document && active && documents.some(item => item.id === active && !item.trashed)) await get().open(active);
    } catch (error) {
      set({ error: error instanceof Error ? error.message : '文档列表加载失败' });
    } finally { set({ loading: false }); }
  },

  adopt: (document) => {
    try { localStorage.setItem(ACTIVE_KEY, document.id); } catch { /* Session only. */ }
    const documents = [summary(document), ...get().documents.filter(item => item.id !== document.id)];
    set({ document, documents, dirty: false, draftStored: false, saveError: null, conflict: null, editVersion: 0, error: null });
  },

  open: async (id) => {
    if (get().opening || get().document?.id === id) return;
    set({ opening: true });
    try {
      get().stopAssistant();
      if (get().dirty && !await get().save()) return;
      const document = await officeApi.get(id);
      get().adopt(document);
      const raw = storageRead(DRAFT_PREFIX + id);
      if (raw) {
        try {
          const draft = JSON.parse(raw) as OfficeDocument;
          if (draft.id === id && typeof draft.content === 'string' && typeof draft.title === 'string' && Array.isArray(draft.messages)) {
            // An interrupted response is never resumed as a pending operation after refresh.
            draft.messages = draft.messages.map(item => item.status === 'pending' ? { ...item, status: 'cancelled', content: item.content || '上次生成已中断，文档已保留。' } : item);
            set({ document: { ...document, ...draft }, dirty: true, draftStored: true, conflict: draft.revision === document.revision ? null : document });
            toast('已恢复上次未保存的草稿', 'info');
          }
        } catch { /* Malformed local drafts must not prevent opening the saved document. */ }
      }
    } catch (error) { toast(error instanceof Error ? error.message : '文档打开失败', 'error'); }
    finally { set({ opening: false }); }
  },

  home: async () => {
    if (get().opening) return;
    get().stopAssistant();
    if (get().dirty && !await get().save()) return;
    try { localStorage.removeItem(ACTIVE_KEY); } catch { /* Session only. */ }
    set({ document: null, saveError: null, conflict: null });
  },

  create: async (title = '未命名文档', content = '<p></p>', kind = 'document') => {
    if (get().opening) return;
    set({ opening: true });
    try {
      get().stopAssistant();
      if (get().dirty && !await get().save()) return;
      get().adopt(await officeApi.create(title, content, kind));
    }
    catch (error) { toast(error instanceof Error ? error.message : '创建失败', 'error'); }
    finally { set({ opening: false }); }
  },

  update: (patch) => {
    const current = get().document;
    if (!current) return;
    const document = { ...current, ...patch };
    const draftStored = remember(document);
    set({ document, dirty: true, draftStored, editVersion: get().editVersion + 1,
      documents: get().documents.map(item => item.id === document.id ? summary(document) : item),
    });
  },

  save: async (reason = 'autosave') => {
    if (saveFlight) {
      const ok = await saveFlight;
      return ok && get().dirty ? get().save(reason) : ok;
    }
    const current = get();
    if (!current.document || (!current.dirty && reason !== 'checkpoint')) return true;
    if (current.conflict) return false;
    const document = { ...current.document, title: current.document.title.trim() || '未命名文档' };
    const editVersion = current.editVersion;
    set({ saving: true, saveError: null });
    saveFlight = (async () => {
      try {
        const saved = await officeApi.update(document, reason);
        const latest = get();
        if (latest.document?.id === saved.id) {
          const changed = latest.editVersion !== editVersion;
          const merged = changed ? { ...latest.document, revision: saved.revision, updated_at: saved.updated_at } : { ...saved, content: document.content };
          set({ document: merged, dirty: changed, draftStored: changed ? remember(merged) : false, documents: latest.documents.map(item => item.id === saved.id ? summary(merged) : item) });
          if (!changed) forget(saved.id);
        }
        return true;
      } catch (error) {
        if (error instanceof OfficeConflict) set({ conflict: error.document });
        set({ saveError: error instanceof Error ? error.message : '保存失败，请重试' });
        return false;
      } finally { set({ saving: false }); }
    })();
    try { return await saveFlight; } finally { saveFlight = null; }
  },

  duplicate: async () => {
    get().stopAssistant();
    const document = get().document;
    if (!document) return;
    try {
      const copy = await officeApi.create(`${document.title.slice(0, 190)} · 副本`, document.content, document.kind, document.messages.filter(item => item.status !== 'pending'));
      forget(document.id);
      get().adopt(copy);
      toast('已创建文档副本', 'success');
    } catch (error) { toast(error instanceof Error ? error.message : '创建副本失败', 'error'); }
  },

  reload: async () => {
    const document = get().document;
    if (!document) return;
    try {
      const saved = await officeApi.get(document.id);
      get().stopAssistant();
      forget(document.id);
      get().adopt(saved);
    } catch (error) { toast(error instanceof Error ? error.message : '重新加载失败', 'error'); }
  },

  trash: async () => {
    get().stopAssistant();
    get().update({ trashed: true });
    if (await get().save('manual')) {
      set({ document: null });
      try { localStorage.removeItem(ACTIVE_KEY); } catch { /* Session only. */ }
      toast('已移至回收站，可随时恢复', 'success');
    }
  },

  restore: async (id) => {
    try {
      const document = await officeApi.get(id);
      const restored = await officeApi.update({ ...document, trashed: false }, 'manual');
      set({ documents: get().documents.map(item => item.id === id ? summary(restored) : item) });
      await get().open(id);
      toast('文档已恢复', 'success');
    } catch (error) { toast(error instanceof Error ? error.message : '恢复失败', 'error'); }
  },

  stopAssistant: () => {
    if (!get().assistantAbort) return;
    get().assistantAbort?.abort();
    set({ assistantAbort: null });
    const messages = get().document?.messages.map(item => item.status === 'pending' ? { ...item, status: 'cancelled' as const, content: item.content || '已停止生成，文档已保留。' } : item);
    if (messages) get().update({ messages });
  },
}));
