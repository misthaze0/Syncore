/**
 * 递给输入框的一句话（跨页面的一次性草稿）
 *
 * 现在只有一个用户：插件市场的「通过 Syncore 创建」—— 开一个新任务，
 * 把创建向导那段话放进输入框，让她先问需求，而不是让用户自己从零写。
 *
 * ★ 为什么不用 router state
 * 输入框的文本活在 ChatInput 内部，填它要等组件挂上并且拿到 ref。
 * 用 store 的话，写的人（市场页）不用关心读的人什么时候存在；
 * `take` 的语义保证这段话只会被贴一次 —— 刷新页面不该再冒出来一遍。
 */

import { create } from 'zustand';

interface ComposerStore {
  pending: string | null;
  put: (text: string) => void;
  take: () => string | null;
}

export const useComposerStore = create<ComposerStore>((set, get) => ({
  pending: null,
  put: (text) => set({ pending: text }),
  take: () => {
    const value = get().pending;
    if (value !== null) set({ pending: null });
    return value;
  },
}));

export function putComposerDraft(text: string): void {
  useComposerStore.getState().put(text);
}

/** 取走草稿（取完即清）；没有则 null */
export function takeComposerDraft(): string | null {
  return useComposerStore.getState().take();
}
