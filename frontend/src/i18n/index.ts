/**
 * 国际化 —— 零依赖实现（zh-CN 为源语言，en 为翻译）
 *
 * ★ 为什么不引 i18next
 *
 * 全项目的依赖取向是「能零依赖就零依赖」（见 MessageActions / icons 的注释）。
 * 这里的需求只有一个：按 key 取文案 + 简单插值 + 切语言时刷新 ——
 * 一个 zustand store 加一个查表函数就够了，i18next 的命名空间、
 * 复数规则、延迟加载在本项目里全都用不上。
 *
 * ★ 约定
 * - key 用「区域.名称」的扁平写法（`chat.newSession`），不搞嵌套对象 ——
 *   查一个 key 不需要三层解引用，缺 key 时一眼能看出来。
 * - zh 是源语言：en 里缺的 key **回退到 zh**（而不是显示 key 本身）——
 *   对一个以中文为主的产品，漏翻一句显示中文远好过显示 `a.b.c`。
 * - 插值用 `{name}` 占位：`t('conv.turnsTip', { n: 3 })`。
 * - 词典外的动态内容（后端返回的标题、模型生成的文本）不翻译，原样显示。
 */

import { useCallback } from 'react';

import { create } from 'zustand';

import { en } from './en';
import { zh } from './zh';

export type Lang = 'zh' | 'en';
type Dict = Record<string, string>;

const DICTS: Record<Lang, Dict> = { zh, en };
const STORAGE_KEY = 'xjms.lang';

function readInitialLang(): Lang {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved === 'zh' || saved === 'en') return saved;
  } catch {
    /* 隐私模式：用默认语言 */
  }
  // 跟随浏览器语言 —— 明确装了中文系统的人不该看到英文界面
  return navigator.language?.toLowerCase().startsWith('zh') ? 'zh' : 'zh';
}

interface LangStore {
  lang: Lang;
  setLang: (lang: Lang) => void;
  toggle: () => void;
}

export const useLangStore = create<LangStore>((set, get) => ({
  lang: readInitialLang(),
  setLang: (lang) => {
    try {
      window.localStorage.setItem(STORAGE_KEY, lang);
    } catch {
      /* 存不了就只影响本次会话 */
    }
    document.documentElement.setAttribute('lang', lang === 'zh' ? 'zh-CN' : 'en');
    set({ lang });
  },
  toggle: () => get().setLang(get().lang === 'zh' ? 'en' : 'zh'),
}));

/** 纯函数翻译（store 外 / 事件回调里用；读取当前语言） */
export function t(key: string, params?: Record<string, string | number>): string {
  const lang = useLangStore.getState().lang;
  return translate(lang, key, params);
}

function translate(lang: Lang, key: string, params?: Record<string, string | number>): string {
  const dict = DICTS[lang];
  let text = dict[key] ?? DICTS.zh[key];
  if (text === undefined) return key;
  if (params) {
    for (const [name, value] of Object.entries(params)) {
      text = text.split(`{${name}}`).join(String(value));
    }
  }
  return text;
}

/**
 * 组件内用（订阅语言变化，切换时自动重渲染）。
 *
 * 直接 import { t } 在组件里用不会跟着语言切换刷新 —— 必须经过这里。
 */
export function useT(): (key: string, params?: Record<string, string | number>) => string {
  const lang = useLangStore((s) => s.lang);
  return useCallback((key, params) => translate(lang, key, params), [lang]);
}
