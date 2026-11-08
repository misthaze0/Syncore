import { create } from 'zustand';
import type { AttachmentMeta } from '@/types';

export interface PendingAttachment {
  localId: string;
  status: 'uploading' | 'ready' | 'error';
  meta?: AttachmentMeta;
  error?: string;
}

type Update<T> = T | ((previous: T) => T);
interface Draft { text: string; attachments: PendingAttachment[] }
const EMPTY_DRAFT: Draft = { text: '', attachments: [] };

/** Session-only drafts: route changes preserve text and in-flight attachments. */
export const useChatDraftStore = create<{
  drafts: Record<string, Draft>;
  setText: (threadId: string, value: Update<string>) => void;
  setAttachments: (threadId: string, value: Update<PendingAttachment[]>) => void;
}>((set) => ({
  drafts: {},
  setText: (threadId, value) => set(state => {
    const current = state.drafts[threadId] ?? EMPTY_DRAFT;
    const text = typeof value === 'function' ? value(current.text) : value;
    const drafts = { ...state.drafts, [threadId]: { ...current, text } };
    if (!text && !current.attachments.length) delete drafts[threadId];
    return { drafts };
  }),
  setAttachments: (threadId, value) => set(state => {
    const current = state.drafts[threadId] ?? EMPTY_DRAFT;
    const attachments = typeof value === 'function' ? value(current.attachments) : value;
    const drafts = { ...state.drafts, [threadId]: { ...current, attachments } };
    if (!current.text && !attachments.length) delete drafts[threadId];
    return { drafts };
  }),
}));

export const emptyDraftAttachments = EMPTY_DRAFT.attachments;
