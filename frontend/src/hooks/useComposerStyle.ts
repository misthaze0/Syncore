/**
 * 输入框（对话框）风格
 *
 * 只做一个开关：素净的卡片，还是缠上荆棘、压一朵玫瑰的卡片。
 * 装饰全住在 CSS 的伪元素里，选择器写成「全局属性命中 .composer__card」
 * 加「设置页小样自己带一个类」两条并列 —— 这样弹窗里两个选项同时都能画出真效果，
 * 不必为了预览去改全局状态。
 *
 * 与壁纸不同，这里没有强度可调：纹样要么有要么没有，拧半档只会得到一张脏了的卡。
 */

import { create } from 'zustand';

const STORAGE_KEY = 'xjms.composerStyle';

export type ComposerStyle = 'plain' | 'rose';

export interface ComposerStyleMeta {
  id: ComposerStyle;
  label: string;
  /** 小样下面那行小字：说清这版长什么样，别让人靠猜 */
  hint: string;
}

export const COMPOSER_STYLES: ComposerStyleMeta[] = [
  { id: 'plain', label: '素净', hint: '现在的默认样子' },
  { id: 'rose', label: '玫瑰荆棘', hint: '鎏金框角缀玫瑰，顶边垂宝石链' },
];

function isComposerStyle(v: string | null): v is ComposerStyle {
  return v === 'plain' || v === 'rose';
}

function getInitial(): ComposerStyle {
  if (typeof window === 'undefined') return 'plain';
  const saved = window.localStorage.getItem(STORAGE_KEY);
  return isComposerStyle(saved) ? saved : 'plain';
}

function apply(style: ComposerStyle): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  if (style === 'plain') root.removeAttribute('data-composer');
  else root.setAttribute('data-composer', 'rose');
}

interface ComposerStyleStore {
  composerStyle: ComposerStyle;
  setComposerStyle: (style: ComposerStyle) => void;
}

export const useComposerStyleStore = create<ComposerStyleStore>((set) => {
  const initial = getInitial();
  apply(initial);
  return {
    composerStyle: initial,
    setComposerStyle: (composerStyle) => {
      try {
        window.localStorage.setItem(STORAGE_KEY, composerStyle);
      } catch {
        /* 隐私模式下写不进去 —— 退化成「本次会话内有效」，不该因此报错 */
      }
      apply(composerStyle);
      set({ composerStyle });
    },
  };
});
