/**
 * 右侧栏状态 —— 产物画布 / 浏览器 / 终端三个页签共用的开关与选中态
 *
 * ★ 为什么是全局 store 而不是 ChatPage 的 useState
 *
 * 打开面板的入口散在**消息深处**（`TurnFiles` 里每一行的「预览」按钮）、
 * 顶栏开关按钮、以及命令面板，而面板本身挂在布局层（它要占一列）。
 * 把状态提到 store，这些入口就不必穿 prop 链。
 *
 * ★ target 与 panelOpen 分开
 *
 * 「选中了哪个产物文件」和「面板开没开」是两个正交状态：
 * 关掉面板再打开，之前选中的文件应该还在（用户的阅读位置是个偏好）；
 * 而打开一个新产物必然伴随面板展开。切会话时两者一起清 ——
 * 面板里是「上一个会话产出的文件」，留着会和当前对话对不上。
 *
 * ★ 为什么只存「引用」不存内容
 *
 * 内容每次打开时按需从 `GET /files/content` 拉。存内容意味着要处理
 * 「文件被重新生成」的失效逻辑，而那正是最容易做错、又最难发现的一类缓存 bug。
 * 一次几百 KB 的本地请求，代价远低于一个过期缓存。
 */

import { create } from 'zustand';

import { isDesktopShell } from '@/types/desktop';
import { useWorkbenchStore } from '@/stores/workbenchStore';
import { normalizeBrowserUrl } from '@/lib/browser';

export type SidePanelTab = 'artifacts' | 'browser' | 'terminal';

export interface ArtifactTarget {
  /** 相对工作目录的路径 —— 也是取内容的 key */
  path: string;
  /** 文件名（展示用） */
  name: string;
  /** 产出的那一轮，便于以后做「跳回该轮」 */
  turnId?: string;
  threadId?: string;
}

interface SidePanelStore {
  /** 当前在产物页签里打开的文件；null = 产物页签显示空态 */
  target: ArtifactTarget | null;
  /** 面板是否展开（顶栏开关按钮控制） */
  panelOpen: boolean;
  /** 当前页签 */
  activeTab: SidePanelTab;
  /** 浏览器页签要打开的网页；null = 浏览器页签显示空态 */
  browserUrl: string | null;
  /** 每次 openBrowser 自增 —— 让「点同一个链接两次」也能重新导航 */
  browserNonce: number;
  /** 打开一个产物文件（必然伴随面板展开、切到产物页签） */
  open: (target: ArtifactTarget) => void;
  /** 关掉当前产物文件（面板保持展开 —— 用户可能想接着用终端/浏览器） */
  close: () => void;
  /** 在浏览器页签打开一个网页（必然伴随面板展开、切到浏览器页签） */
  openBrowser: (url: string) => void;
  /** 展开/收起面板；带 tab 时顺带切过去（收起状态点「浏览器」= 展开并显示浏览器） */
  togglePanel: (tab?: SidePanelTab) => void;
  /** 收起面板（不清 target —— 重开时恢复） */
  closePanel: () => void;
  setActiveTab: (tab: SidePanelTab) => void;
}

export const useSidePanelStore = create<SidePanelStore>((set) => ({
  target: null,
  panelOpen: false,
  activeTab: 'artifacts',
  browserUrl: null,
  browserNonce: 0,
  open: (target) => set({ target, panelOpen: true, activeTab: 'artifacts' }),
  close: () => set({ target: null }),
  openBrowser: (url) =>
    set((s) => ({ browserUrl: url, browserNonce: s.browserNonce + 1, panelOpen: true, activeTab: 'browser' })),
  togglePanel: (tab) =>
    set((s) => ({
      panelOpen: s.panelOpen && tab === undefined ? false : true,
      ...(tab ? { activeTab: tab } : {}),
    })),
  closePanel: () => set({ panelOpen: false }),
  setActiveTab: (tab) => set({ activeTab: tab, panelOpen: true }),
}));

// ---------- 兼容别名（原 artifactStore 的导出名，调用方零改动） ----------
export const useArtifactStore = useSidePanelStore;

/** 在非组件代码里也能开画布（事件回调、store 动作内） */
export function openArtifact(target: ArtifactTarget): void {
  useSidePanelStore.getState().open(target);
}

/** 关掉产物文件与整个面板（切会话时用 —— 两者都属于「上一个会话的上下文」） */
export function closeArtifact(): void {
  useSidePanelStore.getState().closePanel();
}

/**
 * 打开一个网页链接 —— 桌面端在右侧栏浏览器页签内嵌打开，Web 端回退新标签页。
 *
 * 浏览器页签靠 Electron `<webview>` 实现，只有桌面壳里有；Web 模式下没有它，
 * 就用浏览器原生的新标签页兜底，绝不让点击「没反应」。
 */
export function openWebLink(url: string): void {
  const target = normalizeBrowserUrl(url);
  if (!target) return;
  if (isDesktopShell() && useWorkbenchStore.getState().preferences.linkTarget === 'internal') {
    useSidePanelStore.getState().openBrowser(target);
  } else {
    window.open(target, '_blank', 'noopener,noreferrer');
  }
}
