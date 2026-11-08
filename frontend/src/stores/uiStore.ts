/**
 * 界面级 UI 状态 —— 目前只有侧边栏折叠。
 *
 * ★ 为什么从 Sidebar 的 useState 提升成 store
 *
 * 折叠开关搬进了标题栏（TitleBar，桌面端），而侧栏本体挂在布局层 ——
 * 两头各持一份 useState 必然失步。收进 store 后，标题栏的开关、
 * Ctrl+N 之外将来任何入口改的都是同一份状态。
 */

import { create } from 'zustand';

import { useSidePanelStore } from '@/stores/artifactStore';

interface UiState {
  officeFocused: boolean;
  setOfficeFocused: (focused: boolean) => void;
  officeLibraryOpen: boolean;
  setOfficeLibraryOpen: (open: boolean | ((value: boolean) => boolean)) => void;
  officeAssistantOpen: boolean;
  setOfficeAssistantOpen: (open: boolean | ((value: boolean) => boolean)) => void;
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (collapsed: boolean) => void;
  /**
   * 标题栏开关的统一入口：窄窗且右侧面板开着时，侧栏是被面板「挤走」的
   * （Sidebar 的 autoCollapsed）—— 此时点开关应该先关面板，而不是翻转折叠位。
   */
  toggleSidebar: () => void;
}

export const useUiStore = create<UiState>((set, get) => ({
  officeFocused: false,
  setOfficeFocused: officeFocused => set({ officeFocused }),
  officeLibraryOpen: !window.matchMedia('(max-width: 760px)').matches,
  setOfficeLibraryOpen: open => set(s => ({ officeLibraryOpen: typeof open === 'function' ? open(s.officeLibraryOpen) : open })),
  officeAssistantOpen: false,
  setOfficeAssistantOpen: open => set(s => ({ officeAssistantOpen: typeof open === 'function' ? open(s.officeAssistantOpen) : open })),
  sidebarCollapsed: window.matchMedia('(max-width: 760px)').matches,
  setSidebarCollapsed: (collapsed) => set({ sidebarCollapsed: collapsed }),
  toggleSidebar: () => {
    const narrow = window.matchMedia('(max-width: 1180px)').matches;
    if (narrow && useSidePanelStore.getState().panelOpen) {
      useSidePanelStore.getState().closePanel();
      set({ sidebarCollapsed: false });
      return;
    }
    set({ sidebarCollapsed: !get().sidebarCollapsed });
  },
}));
