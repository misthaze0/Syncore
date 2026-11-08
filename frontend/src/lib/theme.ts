export const THEMES = ['light', 'dark', 'velvet'] as const;
export type Theme = typeof THEMES[number];
export type ThemePreference = Theme | 'system';

export const THEME_STORAGE_KEY = 'xjms.theme';
export const THEME_LABELS: Record<Theme, string> = { light: '浅色', dark: '暗色', velvet: '绛红' };

export function isTheme(value: unknown): value is Theme {
  return THEMES.includes(value as Theme);
}

export function parseThemePreference(value: unknown): ThemePreference {
  return value === 'system' || isTheme(value) ? value : 'system';
}

export function nextTheme(theme: Theme): Theme {
  return THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
}

export function themeScheme(theme: Theme): 'light' | 'dark' {
  return theme === 'light' ? 'light' : 'dark';
}
