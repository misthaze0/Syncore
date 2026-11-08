/**
 * 三套主题（浅色 / 暗色 / 绛红）及跟随系统偏好。
 *
 * 不引入新依赖，通过 html 元素的 data-theme 属性切换 CSS 变量。
 * 偏好持久化到 localStorage；选「跟随系统」时监听系统配色变化实时跟随。
 *
 * ★ 为什么状态放在 zustand store 而不是 useState
 *
 * 主题有三个触发点：顶栏按钮、命令面板（Ctrl+K）、设置 → 外观。
 * store 让「谁能改主题」不取决于组件树长什么样。
 *
 * 初始值在模块加载时就写进 document（而不是等第一个组件挂载），
 * 否则首屏会先按 light 画一帧再跳到 dark。
 */

import { create } from 'zustand';
import { THEME_STORAGE_KEY, nextTheme, parseThemePreference, themeScheme, type Theme, type ThemePreference } from '@/lib/theme';
export type { Theme, ThemePreference } from '@/lib/theme';

const STORAGE_KEY = THEME_STORAGE_KEY;

function systemTheme(): Theme {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function getInitialPreference(): ThemePreference {
  if (typeof window === 'undefined') return 'system';
  try { return parseThemePreference(window.localStorage.getItem(STORAGE_KEY)); } catch { /* Storage may be unavailable. */ }
  return 'system';
}

function applyTheme(theme: Theme): void {
  if (typeof document === 'undefined') return;
  document.documentElement.setAttribute('data-theme', theme);
  document.documentElement.setAttribute('data-color-scheme', themeScheme(theme));
  document.documentElement.style.colorScheme = themeScheme(theme);
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', { light: '#f6f6f8', dark: '#101114', velvet: '#241d27' }[theme]);
}

function persist(pref: ThemePreference): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, pref);
  } catch {
    /* 隐私模式下写不进去 —— 退化成「本次会话内有效」，不该因此报错 */
  }
}

/** 偏好 → 实际生效主题 */
export function resolveTheme(pref: ThemePreference): Theme {
  return pref === 'system' ? systemTheme() : pref;
}

interface ThemeStore {
  preference: ThemePreference;
  theme: Theme;
  /** 选择一套主题，或跟随系统的明暗模式。 */
  setPreference: (pref: ThemePreference) => void;
  /** 直接指定生效主题（顶栏按钮、命令面板用）—— 偏好也跟着变 */
  setTheme: (theme: Theme) => void;
  toggle: () => void;
}

export const useThemeStore = create<ThemeStore>((set, get) => {
  const initial = getInitialPreference();
  applyTheme(resolveTheme(initial));

  // 「跟随系统」时监听系统切换 —— 只有这一处副作用，卸载不需要（store 与页面同生命周期）
  if (typeof window !== 'undefined') {
    window
      .matchMedia('(prefers-color-scheme: dark)')
      .addEventListener('change', () => {
        if (get().preference !== 'system') return;
        const theme = systemTheme();
        applyTheme(theme);
        set({ theme });
      });
    // Settings, the main window and the pet share a preference without writing
    // it back when receiving a change from another window.
    window.addEventListener('storage', event => {
      if (event.key !== STORAGE_KEY && event.key !== null) return;
      const preference = parseThemePreference(event.newValue);
      const theme = resolveTheme(preference);
      applyTheme(theme);
      set({ preference, theme });
    });
  }

  return {
    preference: initial,
    theme: resolveTheme(initial),
    setPreference: (pref) => {
      persist(pref);
      applyTheme(resolveTheme(pref));
      set({ preference: pref, theme: resolveTheme(pref) });
    },
    setTheme: (theme) => {
      persist(theme);
      applyTheme(theme);
      set({ preference: theme, theme });
    },
    toggle: () => {
      const next = nextTheme(get().theme);
      persist(next);
      applyTheme(next);
      set({ preference: next, theme: next });
    },
  };
});

export function useTheme(): {
  theme: Theme;
  preference: ThemePreference;
  toggleTheme: () => void;
  setTheme: (t: Theme) => void;
  setPreference: (p: ThemePreference) => void;
} {
  return {
    theme: useThemeStore((s) => s.theme),
    preference: useThemeStore((s) => s.preference),
    toggleTheme: useThemeStore((s) => s.toggle),
    setTheme: useThemeStore((s) => s.setTheme),
    setPreference: useThemeStore((s) => s.setPreference),
  };
}
