import { fetchConversationFiles } from './api';

const inFlight = new Map<string, ReturnType<typeof fetchConversationFiles>>();
/** A long conversation can show many file cards, all backed by one thread inventory. */
export function fetchMessageFiles(threadId: string) {
  const existing = inFlight.get(threadId);
  if (existing) return existing;
  const request = fetchConversationFiles(threadId).finally(() => inFlight.delete(threadId));
  inFlight.set(threadId, request);
  return request;
}
