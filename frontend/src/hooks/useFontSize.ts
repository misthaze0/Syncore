/**
 * 对话字号调节
 *
 * ★ 为什么值得做：读长回答是高频动作，而「字太小」是每个人都会遇到一次的问题。
 * 用浏览器缩放解决会把整个界面（侧栏、按钮）一起放大，得不偿失。
 *
 * ★ 实现方式：只改一个 CSS 变量 `--chat-font`，挂在 `:root` 上。
 * 消息气泡与它内部的一切（Markdown、引用、表格）都从这个变量取字号，
 * 于是「字号」只有一处定义 —— 不会出现正文变大了、表格还是小字的情况。
 * 偏好持久化到 localStorage，与主题同一套做法。
 *
 * 与主题同理，状态放 store：顶栏按钮和命令面板都要能改它。
 */

import { create } from 'zustand';

const STORAGE_KEY = 'xjms.fontSize';

export type FontLevel = 's' | 'm' | 'l' | 'xl';

/** 四档，依次变大。默认 m 与原来的固定 15px 一致（升级不该改变默认观感） */
const SIZES: Record<FontLevel, string> = {
  s: '14px',
  m: '15px',
  l: '17px',
  xl: '19px',
};

const ORDER: FontLevel[] = ['s', 'm', 'l', 'xl'];

export const FONT_LABEL: Record<FontLevel, string> = {
  s: '小',
  m: '标准',
  l: '大',
  xl: '特大',
};

function getInitialLevel(): FontLevel {
  if (typeof window === 'undefined') return 'm';
  const saved = window.localStorage.getItem(STORAGE_KEY) as FontLevel | null;
  return saved && ORDER.includes(saved) ? saved : 'm';
}

function applyLevel(level: FontLevel): void {
  if (typeof document === 'undefined') return;
  document.documentElement.style.setProperty('--chat-font', SIZES[level]);
  try {
    window.localStorage.setItem(STORAGE_KEY, level);
  } catch {
    /* 隐私模式下写不进去 —— 退化成「本次会话内有效」，不该因此报错 */
  }
}

interface FontStore {
  level: FontLevel;
  setLevel: (level: FontLevel) => void;
  cycle: () => void;
}

export const useFontStore = create<FontStore>((set, get) => {
  const initial = getInitialLevel();
  applyLevel(initial);
  return {
    level: initial,
    setLevel: (level) => {
      applyLevel(level);
      set({ level });
    },
    cycle: () => {
      const next = ORDER[(ORDER.indexOf(get().level) + 1) % ORDER.length];
      applyLevel(next);
      set({ level: next });
    },
  };
});

export function useFontSize(): {
  level: FontLevel;
  label: string;
  cycle: () => void;
} {
  const level = useFontStore((s) => s.level);
  const cycle = useFontStore((s) => s.cycle);
  return { level, label: FONT_LABEL[level], cycle };
}
