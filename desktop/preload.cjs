/**
 * preload —— 给页面暴露一个**极小的**桌面桥
 *
 * ★ contextIsolation 下的标准做法
 *
 * 页面拿到的只有 `window.xjmsDesktop`，且每个方法都经过 contextBridge 的
 * 参数拷贝 —— 页面碰不到 ipcRenderer，更碰不到 Node。终端的写操作只接受
 * 字符串，事件只往外推字符串，目录选择框只回一条路径字符串（页面传不进
 * 路径，看哪个目录由用户在系统对话框里定）—— 桥面没有「传对象进来执行」的能力，
 * XSS 就算发生也借不到这里的东西。
 *
 * ★ 为什么没有 backend 相关的桥
 *
 * 前端与后端走 HTTP（CORS 由主进程桥接，见 main.js），不需要经 preload 绕行
 * —— 桥越小，安全面越小。
 */

const { contextBridge, ipcRenderer, webFrame } = require('electron');

contextBridge.exposeInMainWorld('xjmsDesktop', {
  /** 前端据此判断走 webview / xterm 还是 iframe / HTTP exec */
  isDesktop: true,
  platform: process.platform,
  version: process.versions.electron,
  computerControl: {
    shortcutAvailable: () => ipcRenderer.invoke('computer-control:shortcut-status'),
    onStopped: (callback) => {
      const listener = (_event, result) => callback(result);
      ipcRenderer.on('computer-control:stopped', listener);
      return () => ipcRenderer.removeListener('computer-control:stopped', listener);
    },
  },

  /**
   * 自绘标题栏（TitleBar 组件）用的窗口桥。
   * setOverlay 只收 { color, symbolColor } 两个 #RRGGBB，主进程还会再验一次；
   * 缩放在渲染层本地完成（webFrame），不发 IPC。
   */
  windowBar: {
    setOverlay: (patch) => ipcRenderer.invoke('window:overlay', patch),
    toggleFullscreen: () => ipcRenderer.invoke('window:toggleFullscreen'),
    quit: () => { void ipcRenderer.invoke('app:quit'); },
    zoomStep: (delta) => {
      const factor = Math.min(2, Math.max(0.5, webFrame.getZoomFactor() + delta));
      webFrame.setZoomFactor(factor);
      return factor;
    },
    zoomReset: () => {
      webFrame.setZoomFactor(1);
      return 1;
    },
  },

  terminal: {
    /** 开一个 PTY 会话（重复 id 幂等） */
    create: (id, cols, rows, options = {}) => ipcRenderer.invoke('terminal:create', { id, cols, rows, shell: options.shell }),
    /** 写入键击 */
    input: (id, data) => ipcRenderer.send('terminal:input', { id, data }),
    /** 尺寸变化（xterm fit 后调用） */
    resize: (id, cols, rows) => ipcRenderer.send('terminal:resize', { id, cols, rows }),
    /** 结束单个会话（关闭会话标签时，收起面板不结束） */
    kill: (id) => ipcRenderer.send('terminal:kill', { id }),
    /** 订阅输出流；返回退订函数 */
    onData: (callback) => {
      const listener = (_event, payload) => callback(payload.data);
      ipcRenderer.on('terminal:data', listener);
      return () => ipcRenderer.removeListener('terminal:data', listener);
    },
    onSessionData: (callback) => {
      const listener = (_event, payload) => callback(payload);
      ipcRenderer.on('terminal:data', listener);
      return () => ipcRenderer.removeListener('terminal:data', listener);
    },
    /** 订阅会话退出 */
    onExit: (callback) => {
      const listener = (_event, payload) => callback(payload);
      ipcRenderer.on('terminal:exit', listener);
      return () => ipcRenderer.removeListener('terminal:exit', listener);
    },
    /** PTY 宿主是否可用（系统 node 缺失 / 宿主崩溃时为 false —— 前端走 HTTP 回退） */
    ptyAvailable: () => ipcRenderer.invoke('terminal:ptyAvailable'),
  },

  browser: {
    onEscape: (callback) => {
      const listener = () => callback();
      ipcRenderer.on('workbench:escape', listener);
      return () => ipcRenderer.removeListener('workbench:escape', listener);
    },
    onFocusAddress: (callback) => {
      const listener = () => callback();
      ipcRenderer.on('browser:focus-address', listener);
      return () => ipcRenderer.removeListener('browser:focus-address', listener);
    },
  },

  dialog: {
    /** 打开系统的「选择文件夹」对话框；返回选中的绝对路径，用户取消则为 null */
    selectFolder: (title) => ipcRenderer.invoke('dialog:selectFolder', title),
  },

  /**
   * 壳能力（「设置 → 数据」用）：只接受绝对路径，主进程侧还会再验一次
   * 「存在且是目录」—— 这条桥打不开可执行文件，只定位文件夹。
   */
  shell: {
    /** 在系统资源管理器里打开一个文件夹；返回是否成功 */
    openPath: (target) => ipcRenderer.invoke('shell:openPath', String(target)),
  },

  /**
   * 桌宠桥：渲染层（pet.html）通过这里与主进程交互。
   *
   * 桥面同样保持「极小」：所有方法只收字符串/布尔/对象 delta，不收函数；
   * 拖拽用 send（单向），状态读用 invoke（要结果），状态写用 send（fire-and-forget）。
   * 桌宠页和主页共享 preload，但桌宠页只用 pet 命名空间，主页不用 ——
   * 两端都装着无害，contextBridge 的方法本来就是按需调用。
   */
  pet: {
    /** 拖拽开始：记录鼠标-窗口偏移；mousedowm 时调一次 */
    dragStart: () => ipcRenderer.send('pet:drag-start'),
    /** 拖拽移动：throttle 后转发，主进程改窗口坐标 */
    dragMove: () => ipcRenderer.send('pet:drag-move'),
    /** 拖拽结束：持久化坐标到 pet-state.json */
    dragEnd: () => ipcRenderer.send('pet:drag-end'),
    /** 切换整窗点击穿透：鼠标离开角色 sprite 时 true（穿透）、进入时 false（接收事件） */
    setClickThrough: (ignored) =>
      ipcRenderer.send('pet:set-click-through', Boolean(ignored)),
    setPanelOpen: (open) => ipcRenderer.send('pet:set-panel-open', Boolean(open)),
    getPointer: () => ipcRenderer.invoke('pet:get-pointer'),
    /** 单击桌宠 → 切主窗可见性（聚焦或显示） */
    toggleMain: () => ipcRenderer.invoke('pet:toggle-main'),
    openMain: (route) => ipcRenderer.invoke('pet:open-main', route),
    notifyMainReady: () => ipcRenderer.send('pet:main-ready'),
    onNavigate: (callback) => {
      const listener = (_event, route) => callback(route);
      ipcRenderer.on('pet:navigate', listener);
      return () => ipcRenderer.removeListener('pet:navigate', listener);
    },
    publishActivity: (activity) => ipcRenderer.send('pet:publish-activity', activity),
    getActivity: () => ipcRenderer.invoke('pet:get-activity'),
    onActivity: (callback) => {
      const listener = (_event, activity) => callback(activity);
      ipcRenderer.on('pet:activity', listener);
      return () => ipcRenderer.removeListener('pet:activity', listener);
    },
    /** 主窗发布运行时状态：权限档、助理是否在忙、输入框开关（主进程校验来源与取值） */
    publishRuntime: (runtime) => ipcRenderer.send('pet:publish-runtime', runtime),
    /** 读取当前运行时状态（桌宠启动时补一次） */
    getRuntime: () => ipcRenderer.invoke('pet:get-runtime'),
    onRuntime: (callback) => {
      const listener = (_event, runtime) => callback(runtime);
      ipcRenderer.on('pet:runtime', listener);
      return () => ipcRenderer.removeListener('pet:runtime', listener);
    },
    /** 桌宠发布「语音回合占用中」，主窗据此禁用助理输入框 */
    setVoiceBusy: (busy) => ipcRenderer.send('pet:set-voice-busy', Boolean(busy)),
    publishVoiceUpdate: (update) => ipcRenderer.send('pet:publish-voice', update),
    getVoiceTurn: () => ipcRenderer.invoke('pet:get-voice-turn'),
    onVoiceUpdate: (callback) => {
      const listener = (_event, update) => callback(update);
      ipcRenderer.on('pet:voice-update', listener);
      return () => ipcRenderer.removeListener('pet:voice-update', listener);
    },
    cancelVoice: () => ipcRenderer.send('pet:cancel-voice'),
    onCancelVoice: (callback) => {
      const listener = () => callback();
      ipcRenderer.on('pet:cancel-voice', listener);
      return () => ipcRenderer.removeListener('pet:cancel-voice', listener);
    },
    /** 主进程推送桌宠窗口可见性（隐藏时释放麦克风） */
    onVisibility: (callback) => {
      const listener = (_event, visible) => callback(Boolean(visible));
      ipcRenderer.on('pet:visibility', listener);
      return () => ipcRenderer.removeListener('pet:visibility', listener);
    },
    /** 隐藏桌宠（右键菜单触发；不退出） */
    hide: () => ipcRenderer.send('pet:hide'),
    /** 显示桌宠：窗口不存在时会现建，并持久化 enabled */
    show: () => ipcRenderer.invoke('pet:show'),
    /** 读取持久化状态：{enabled,x,y,character,alwaysOnTop,idleTimeoutMs,muteTts} */
    getState: () => ipcRenderer.invoke('pet:get-state'),
    /** 写入状态 delta：合并到现有 pet-state.json，不覆写整文件 */
    setState: (patch) => ipcRenderer.send('pet:set-state', patch),
    /** 订阅主进程推送的最新状态（设置页改动后广播）；返回退订函数 */
    onStateChanged: (callback) => {
      const listener = (_event, state) => callback(state);
      ipcRenderer.on('pet:state-changed', listener);
      return () => ipcRenderer.removeListener('pet:state-changed', listener);
    },
    /** 回到默认落点；返回写盘后的完整状态 */
    resetPosition: () => ipcRenderer.invoke('pet:reset-position'),
  },
});
