import { create } from 'zustand';

export interface WorkbenchPreferences {
  linkTarget: 'internal' | 'system';
  homepage: string;
  browserZoom: number;
  panelWidth: number;
  terminalFontSize: number;
  terminalScrollback: number;
  terminalCursor: 'block' | 'bar' | 'underline';
  terminalBlink: boolean;
  terminalTheme: 'follow' | 'dark';
  terminalShell: 'default' | 'powershell' | 'cmd';
}

export const WORKBENCH_DEFAULTS: WorkbenchPreferences = {
  linkTarget: 'internal', homepage: '', browserZoom: 100, panelWidth: 520,
  terminalFontSize: 13, terminalScrollback: 5000, terminalCursor: 'bar',
  terminalBlink: true, terminalTheme: 'follow', terminalShell: 'default',
};
const KEY = 'xjms.workbench';

function readPreferences(): WorkbenchPreferences {
  const prefs = { ...WORKBENCH_DEFAULTS };
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || '{}');
    for (const key of Object.keys(prefs) as Array<keyof WorkbenchPreferences>) {
      const value = saved?.[key];
      if (typeof value === typeof prefs[key]) Object.assign(prefs, { [key]: value });
    }
  } catch { /* Storage is optional; preferences still work for this session. */ }
  if (!['internal', 'system'].includes(prefs.linkTarget)) prefs.linkTarget = 'internal';
  if (!['block', 'bar', 'underline'].includes(prefs.terminalCursor)) prefs.terminalCursor = 'bar';
  if (!['follow', 'dark'].includes(prefs.terminalTheme)) prefs.terminalTheme = 'follow';
  if (!['default', 'powershell', 'cmd'].includes(prefs.terminalShell)) prefs.terminalShell = 'default';
  for (const [key, min, max] of [
    ['panelWidth', 360, 1000], ['browserZoom', 50, 200],
    ['terminalFontSize', 10, 20], ['terminalScrollback', 1000, 50000],
  ] as const) prefs[key] = Number.isFinite(prefs[key]) ? Math.max(min, Math.min(max, prefs[key])) : WORKBENCH_DEFAULTS[key];
  return prefs;
}

interface WorkbenchStore {
  preferences: WorkbenchPreferences;
  update: (patch: Partial<WorkbenchPreferences>) => void;
}

export const useWorkbenchStore = create<WorkbenchStore>((set) => ({
  preferences: readPreferences(),
  update: (patch) => set((state) => {
    const preferences = { ...state.preferences, ...patch };
    try { localStorage.setItem(KEY, JSON.stringify(preferences)); } catch { /* Session only. */ }
    return { preferences };
  }),
}));
