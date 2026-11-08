/**
 * Electron 桌面桥的类型声明（见 desktop/preload.cjs）
 *
 * 桥只暴露终端 IPC、一个系统目录选择框、一个桌面标识和桌面助手 IPC —— 浏览器的
 * <webview> 标签不需要桥（webviewTag 打开后直接可用），所以这里没有别的东西。
 *
 * Web 模式下 `window.xjmsDesktop === undefined`：网页链接在新标签打开，
 * 终端说明桌面版可用；目录选择使用应用内目录浏览器。
 */

export interface DesktopTerminalBridge {
  create: (id: string, cols: number, rows: number, options?: { shell?: 'default' | 'powershell' | 'cmd' }) => Promise<{ cwd: string; shell: string } | false>;
  input: (id: string, data: string) => void;
  resize: (id: string, cols: number, rows: number) => void;
  kill: (id: string) => void;
  onData: (callback: (data: string) => void) => () => void;
  onSessionData: (callback: (payload: { id: string; data: string }) => void) => () => void;
  onExit: (callback: (payload: { id: string; exitCode: number }) => void) => () => void;
  ptyAvailable: () => Promise<boolean>;
}

export interface DesktopDialogBridge {
  /** 打开系统「选择文件夹」对话框；取消返回 null */
  selectFolder: (title?: string) => Promise<string | null>;
}

export interface DesktopShellBridge {
  /**
   * 在系统资源管理器里打开一个文件夹。
   *
   * 主进程只接受绝对路径且必须已存在、且必须是目录 —— 这条桥不是「打开任意文件」，
   * 它只用来把用户带到「数据」页列出的那个文件夹。
   */
  openPath: (path: string) => Promise<boolean>;
}

/** 自绘标题栏（TitleBar 组件）可用的窗口桥；见 desktop/preload.cjs 的 windowBar */
export interface DesktopWindowBarBridge {
  /** 主题切换时同步 titleBarOverlay 配色；主进程只收 { color, symbolColor } */
  setOverlay: (patch: { color?: string; symbolColor?: string }) => Promise<boolean>;
  toggleFullscreen: () => Promise<boolean>;
  quit: () => void;
  /** 缩放一档（正放大负缩小），返回新的 zoomFactor */
  zoomStep: (delta: number) => number;
  /** 重置缩放，返回新的 zoomFactor */
  zoomReset: () => number;
}

/** 桌面助手持久化状态；与 desktop/main.js 的 DEFAULT_PET_STATE 同构 */
export interface PetState {
  /** 桌面助手窗是否启用（仅控制启动时是否创建窗，已创建后只看 isVisible） */
  enabled: boolean;
  /** 窗口左上角 x；null 表示未持久化 → 用默认右下角 */
  x: number | null;
  /** 窗口左上角 y */
  y: number | null;
  /** Legacy character key; current brand artwork uses heiress. */
  character: 'heiress' | 'office';
  /** alwaysOnTop 级别：'floating' | 'screen-saver' | 'normal' */
  alwaysOnTop: 'floating' | 'screen-saver' | 'normal';
  /** 无事件超时进入 sleeping 态的毫秒数 */
  idleTimeoutMs: number;
  /** 是否静音 TTS（字幕气泡仍显示） */
  muteTts: boolean;
  scale: number;
  quietMode: boolean;
  reducedMotion: boolean;
  /** 桌面助手显示时监听唤醒词（默认关：麦克风是一项显式授权） */
  wakeEnabled: boolean;
  /** 用户自定义唤醒词；展示形式可带标点，发给后端时归一化 */
  wakeWord: string;
  /** 成功唤醒后的回应；播完才开始听命令，唤醒词音频不会进入转写 */
  wakeAck: string;
  remindersEnabled: boolean;
  reminderMinutes: number;
  focusMinutes: number;
  /** Running timers use an absolute deadline so sleep/restarts do not reset them. */
  focusEndsAt: number | null;
  focusRemainingMs: number | null;
}

export type PetRoute = '/assistant' | '/chat' | '/tasks' | '/schedules' | '/settings/pet';
export interface PetActivity {
  mood: 'idle' | 'thinking' | 'approval' | 'error' | 'done';
  text: string;
  replyId: string;
  route?: PetRoute;
}

/**
 * 主窗 → 桌面助手的运行时快照。
 *
 * 权限档位属于「这一轮助理怎么执行」，只存在主窗 store；桌面助手语音回合必须
 * 拿到同一份值，才能和打字输入保持完全一致的权限口径。
 */
export interface PetVoiceTurn {
  id: string;
  message: string;
  events: import('@/types').AgentEvent[];
  complete: boolean;
  sequence: number;
}

export type PetVoiceUpdate = (
  | { type: 'start'; id: string; message: string }
  | { type: 'event'; id: string; event: import('@/types').AgentEvent }
  | { type: 'end'; id: string }
) & { sequence?: number };

export interface PetRuntime {
  /** 助理输入框当前档位；空串 = 跟随设置默认档 */
  permissionMode: '' | 'read_only' | 'manual' | 'auto' | 'full';
  /** 主窗是否正在跑助理回合（含等待审批） */
  assistantBusy: boolean;
  webEnabled: boolean;
  thinkingEnabled: boolean;
  /** 桌面助手语音回合是否占用 assistant_main（桌面助手写、主窗读） */
  voiceBusy: boolean;
}

export interface DesktopPetBridge {
  /** 拖拽开始：mousedown 时调一次 */
  dragStart: () => void;
  /** 拖拽移动：mousemove throttle 后转发 */
  dragMove: () => void;
  /** 拖拽结束：mouseup 时持久化坐标 */
  dragEnd: () => void;
  /** 切换整窗点击穿透：鼠标离开 sprite 时 true、进入时 false */
  setClickThrough: (ignored: boolean) => void;
  /** Allow keyboard focus only while the user has opened the panel. */
  setPanelOpen: (open: boolean) => void;
  /** Cursor in the pet window's CSS coordinates, including outside its bounds. */
  getPointer: () => Promise<{ x: number; y: number } | null>;
  /** 单击桌面助手 → 切换主窗可见性（聚焦或显示） */
  toggleMain: () => Promise<boolean>;
  openMain: (route: PetRoute) => Promise<boolean>;
  notifyMainReady: () => void;
  onNavigate: (callback: (route: PetRoute) => void) => () => void;
  publishActivity: (activity: PetActivity) => void;
  getActivity: () => Promise<PetActivity | null>;
  onActivity: (callback: (activity: PetActivity) => void) => () => void;
  /** 主窗发布运行时快照（权限档、助理忙碌、输入框开关） */
  publishRuntime: (runtime: Omit<PetRuntime, 'voiceBusy'>) => void;
  /** 读取当前运行时快照（桌面助手启动时补一次） */
  getRuntime: () => Promise<PetRuntime | null>;
  onRuntime: (callback: (runtime: PetRuntime) => void) => () => void;
  /** 桌面助手发布「语音回合占用中」 */
  setVoiceBusy: (busy: boolean) => void;
  publishVoiceUpdate: (update: PetVoiceUpdate) => void;
  getVoiceTurn: () => Promise<PetVoiceTurn | null>;
  onVoiceUpdate: (callback: (update: PetVoiceUpdate) => void) => () => void;
  cancelVoice: () => void;
  onCancelVoice: (callback: () => void) => () => void;
  /** 主进程推送桌面助手窗口可见性 */
  onVisibility: (callback: (visible: boolean) => void) => () => void;
  /** 隐藏桌面助手（右键菜单触发；不退出） */
  hide: () => void;
  /** 显示桌面助手：窗口不存在时会现建，并持久化 enabled */
  show: () => Promise<boolean>;
  /** 读取持久化状态 */
  getState: () => Promise<PetState>;
  /** 写入状态 delta：合并到现有 pet-state.json */
  setState: (patch: Partial<PetState>) => void;
  /** 订阅主进程广播的最新状态（设置页改动后）；返回退订函数 */
  onStateChanged: (callback: (state: PetState) => void) => () => void;
  /** 回到默认落点（主屏右下角）；返回写盘后的完整状态 */
  resetPosition: () => Promise<PetState>;
}

export interface DesktopBridge {
  computerControl?: {
    shortcutAvailable: () => Promise<boolean>;
    onStopped: (callback: (result: { ok: boolean }) => void) => () => void;
  };
  isDesktop: true;
  platform: string;
  version: string;
  terminal: DesktopTerminalBridge;
  browser: {
    onFocusAddress: (callback: () => void) => () => void;
    onEscape: (callback: () => void) => () => void;
  };
  dialog: DesktopDialogBridge;
  shell: DesktopShellBridge;
  windowBar?: DesktopWindowBarBridge;
  pet: DesktopPetBridge;
}

declare global {
  interface Window {
    xjmsDesktop?: DesktopBridge;
  }
}

/** 是否运行在 Electron 桌面壳里 */
export function isDesktopShell(): boolean {
  return typeof window !== 'undefined' && window.xjmsDesktop?.isDesktop === true;
}

/**
 * 是否由前端自绘标题栏（Windows 桌面壳：主窗 titleBarStyle 'hidden'）。
 * 只有这种情况才渲染 TitleBar、隐藏侧栏自带的收起按钮；
 * 其他环境（浏览器 / 非 Windows 桌面壳）保持系统标题栏 + 侧栏内开关。
 */
export function hasCustomTitleBar(): boolean {
  return isDesktopShell() && window.xjmsDesktop?.platform === 'win32' && window.xjmsDesktop?.windowBar != null;
}
