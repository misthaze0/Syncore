export type OfficeKind = 'document' | 'spreadsheet' | 'presentation';
export type SaveReason = 'autosave' | 'manual' | 'ai' | 'restore' | 'checkpoint';

export interface OfficeMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  created_at: string;
  status: 'complete' | 'applied' | 'pending' | 'cancelled' | 'error';
  change?: string;
}

export interface OfficeDocument {
  id: string;
  title: string;
  content: string;
  kind: OfficeKind;
  favorite: boolean;
  trashed: boolean;
  revision: number;
  created_at: string;
  updated_at: string;
  messages: OfficeMessage[];
}

export interface OfficeSummary extends Omit<OfficeDocument, 'content' | 'messages'> {
  excerpt: string;
  characters: number;
}

export interface OfficeVersion {
  id: string;
  title: string;
  revision: number;
  created_at: string;
  label: string;
  content?: string;
}

export interface OfficeSelection {
  from: number;
  to: number;
  text: string;
  html: string;
}

export interface OfficeAIResult {
  html: string;
  reply: string;
  title: string;
  scope: 'document' | 'selection';
}

export type OfficeAIEvent =
  | { type: 'status'; text: string }
  | { type: 'token'; text: string }
  | ({ type: 'result' } & OfficeAIResult)
  | { type: 'done' }
  | { type: 'error'; message: string };
