/**
 * 会话置顶
 *
 * ★ 为什么这个功能值得做
 *
 * 会话列表按「最后活跃时间」倒序，于是**每天都会用到的那条**（例如某个长期项目）
 * 会被新开的闲聊一路挤到看不见。用户唯一的出路是记住标题再搜 —— 而搜索还没有。
 * 置顶是成本最小、收益最直接的一条：把重要的固定在上方。
 *
 * ★ 为什么存在浏览器本地（localStorage）而不是后端
 *
 * 置顶是**个人视图偏好**，不是对话数据的一部分：它不影响后端如何存储/检索这段对话，
 * 也不该在导出、分享、跨设备时跟着走。放在本地，等于「这台机器上的我把它钉住了」。
 * 代价说清楚：换浏览器 / 清缓存就没了 —— 这也是它与「改名」（真的改在服务端）的分界线。
 */

import { create } from 'zustand';

import type { ConversationSummary } from '@/types';

const STORAGE_KEY = 'xjms.pinnedThreads';

function readPinned(): string[] {
  if (typeof window === 'undefined') return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

function writePinned(pinned: string[]): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(pinned));
  } catch {
    /* 隐私模式：置顶退化成「本次会话内有效」，不该因此报错 */
  }
}

interface PinStore {
  pinned: string[];
  toggle: (threadId: string) => void;
  clear: () => void;
}

export const usePinStore = create<PinStore>((set, get) => ({
  pinned: readPinned(),
  toggle: (threadId) => {
    const current = get().pinned;
    const next = current.includes(threadId)
      ? current.filter((id) => id !== threadId)
      : [threadId, ...current];
    writePinned(next);
    set({ pinned: next });
  },
  clear: () => {
    writePinned([]);
    set({ pinned: [] });
  },
}));

/**
 * 置顶的排在前面（按置顶顺序），其余保持后端给的顺序（最后活跃倒序）。
 *
 * 只按「当前列表里真实存在的 id」排序 —— 已删除会话的残留置顶记录
 * 自然被忽略，不必为此专门清理存储，也不会在界面上留下空位。
 */
export function sortByPinned(
  conversations: ConversationSummary[],
  pinned: string[],
): ConversationSummary[] {
  if (pinned.length === 0) return conversations;
  const rank = new Map(pinned.map((id, index) => [id, index]));
  const isPinned = (c: ConversationSummary) => rank.has(c.thread_id);

  return [
    ...conversations.filter(isPinned).sort((a, b) => rank.get(a.thread_id)! - rank.get(b.thread_id)!),
    ...conversations.filter((c) => !isPinned(c)),
  ];
}
