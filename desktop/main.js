/**
 * Syncore —— Electron 主进程
 *
 * ★ 职责边界（为什么壳这么薄）
 *
 * 桌面壳只做四件事：**拉起后端、开窗口、托盘常驻、桥接安全策略**。
 * 它不复制任何业务逻辑 —— 窗口里跑的就是生产构建的前端（dist/），
 * 后端就是 `python -m uvicorn app.main:app`。壳坏了换壳，业务一行不动。
 *
 * ★ 后端生命周期
 *
 * 1. 启动时先探一次 `/api/info`（只读配置、不探活依赖、秒回）：端口上已有
 *    存活的后端（比如用户自己开着开发后端）就**直接复用**，不再拉起第二个 ——
 *    8000 端口被占着的报错比多跑一个进程难排查得多；
 * 2. 没有才 spawn `python -m uvicorn`（cwd = backend 目录），输出写进日志文件；
 * 3. 退出时杀掉**整个进程树**（uvicorn 的 reload/子进程在 Windows 上
 *    不会随父进程退出，只 kill 主 PID 会留孤儿）。
 *
 * ★ CORS 为什么在壳里解决
 *
 * 前端以 file:// 加载（Origin 是 null），后端默认只放行 localhost:5173/3000。
 * 改后端 CORS 配置是为桌面场景放松 Web 场景的安全边界，不划算。
 * 在这里做两条改写，后端与前端都零改动：
 * - 发往后端的请求：Origin 改写成后端认识的白名单值（预检因此通过）；
 * - 后端的响应：access-control-allow-origin 注入 `*`
 *   （前端 fetch 不带凭据，`*` 合法）。
 * 改写只对 `http://127.0.0.1:<port>/api/*` 生效，其它请求一概不碰。
 */

const {
  app,
  BrowserWindow,
  Menu,
  dialog,
  shell,
  session,
  ipcMain,
  screen,
  Tray,
  nativeImage,
  globalShortcut,
} = require('electron');

// 硬件加速默认开启：禁用后全窗口走 SwiftShader 软件光栅化，在 1.5x 缩放的
// 高分屏上滚动/动画/backdrop-filter 会明显掉帧（网页端有 GPU，对比之下更明显）。
// 确实开不了 GPU 的机器（远程桌面 / 无 GPU 沙箱 / 驱动异常）用
// XJMS_SOFTWARE_RENDER=1 强制软件渲染作为逃生开关。
if (process.env.XJMS_SOFTWARE_RENDER === '1') app.disableHardwareAcceleration();
const { spawn, execFile } = require('child_process');
const fs = require('fs');
const path = require('path');
const http = require('http');
const readline = require('readline');

const BACKEND_PORT = Number(process.env.XJMS_BACKEND_PORT || 8000);
const BACKEND_ORIGIN = `http://127.0.0.1:${BACKEND_PORT}`;
// 后端「存活」探测用 /api/info：只读配置、不探活依赖，几十毫秒返回。
// 绝不能拿 /api/health 当启动判据 —— 它逐个探活 Milvus/ES/Redis/Neo4j，
// 依赖容器（Docker）没起时单次要 30+ 秒，会把「已就绪但依赖降级」的后端误判成没起，
// 进而重复拉起 uvicorn → 端口冲突 → 误报「后端启动失败」。
const BACKEND_INFO = `${BACKEND_ORIGIN}/api/info`;

// ---------- 桌面助手透明置顶窗 ----------
// 桌面助手窗尺寸是角色层的逻辑画布大小；具体角色 sprite 居中渲染，背景全透明。
// 340×500 为逻辑尺寸，按设置整体缩放，保持形象和面板比例一致。
const { DEFAULT_PET_STATE, normalizePetState, petSize, clampPetBounds } = require('./pet-state.cjs');
// pet 窗在主窗 ready-to-show 后延时 800ms 再创建：避免与主窗冷启动争抢
// 渲染进程初始化资源，也让主窗先 paint 出内容（用户体验上「先有应用再有桌面助手」）。
const PET_DELAY_MS = 800;

/** 输入框能选的四档 + 空串（跟随设置默认）。运行时桥只认这份白名单。 */
const PET_PERMISSION_MODES = ['', 'read_only', 'manual', 'auto', 'full'];

/** 开发模式：加载 vite dev server 而不是打包产物（XJMS_DEV=1 时启用） */
const DEV_MODE = process.env.XJMS_DEV === '1';
const DEV_URL = 'http://localhost:5173';



/** 打包后 backend 在 resources/backend，开发态在仓库的 backend/ */
function backendDir() {
  if (DEV_MODE || !app.isPackaged) {
    return path.join(__dirname, '..', 'backend');
  }
  return path.join(process.resourcesPath, 'backend');
}

/** 打包后在 resources/app-dist；本地优先 dist-desktop，兼容旧 dist 构建。 */
function frontendDir() {
  if (DEV_MODE || !app.isPackaged) {
    const desktopBuild = path.join(__dirname, '..', 'frontend', 'dist-desktop');
    if (fs.existsSync(path.join(desktopBuild, 'index.html'))) return desktopBuild;
    return path.join(__dirname, '..', 'frontend', 'dist');
  }
  return path.join(process.resourcesPath, 'app-dist');
}

/** 探一次后端存活（/api/info）；网络层任何失败都算「没起来」，绝不抛出 */
function probeHealth(timeoutMs = 800) {
  return new Promise((resolve) => {
    const req = http.get(BACKEND_INFO, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.setTimeout(timeoutMs, () => {
      req.destroy();
      resolve(false);
    });
    req.on('error', () => resolve(false));
  });
}

/** 等后端就绪；deadline 内没就绪就放弃 */
async function waitForBackend(deadlineMs) {
  const deadline = Date.now() + deadlineMs;
  while (Date.now() < deadline) {
    if (await probeHealth()) return true;
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

let backendProc = null;
let logStream = null;

// ---------- 桌面助手状态持久化 ----------
// pet-state.json 存坐标/形象/开关/置顶级别/空闲超时/静音 —— 都是用户偏好，
// 离开用户数据目录就没法跨启动恢复（桌面助手每次开都回到屏幕左上角是糟糕体验）。
// 文件可能不存在（首次启动），读取吞错返回默认值；写入走「写临时文件再 rename」
// 避免写过程中崩溃留半个 JSON。
let petWindow = null;
let petDragOffset = null; // 拖拽期间记录「鼠标 - 窗口左上角」偏移，move 时算窗口新坐标

function petStateFile() {
  return path.join(app.getPath('userData'), 'pet-state.json');
}

function loadPetState() {
  try {
    const raw = fs.readFileSync(petStateFile(), 'utf8');
    const parsed = JSON.parse(raw);
    return normalizePetState(parsed);
  } catch {
    return { ...DEFAULT_PET_STATE };
  }
}

function savePetState(state) {
  const merged = normalizePetState(state);
  const tmp = `${petStateFile()}.tmp`;
  fs.mkdirSync(path.dirname(petStateFile()), { recursive: true });
  fs.writeFileSync(tmp, JSON.stringify(merged, null, 2), 'utf8');
  fs.renameSync(tmp, petStateFile());
  for (const win of [mainWindow, petWindow]) {
    if (win && !win.isDestroyed()) win.webContents.send('pet:state-changed', merged);
  }
  return merged;
}

/** 按 enabled 建窗 / 销窗（落盘由调用方负责）；托盘勾选状态随之刷新 */
function applyPetEnabled(enabled) {
  if (enabled) {
    const win = createPetWindow();
    if (!win.isDestroyed() && !win.isVisible()) win.show();
  } else if (petWindow && !petWindow.isDestroyed()) {
    petWindow.destroy();
    petWindow = null;
  }
  refreshTrayMenu();
}

/** 托盘菜单用：落盘并立即生效 */
function setPetEnabled(enabled) {
  savePetState({ ...loadPetState(), enabled });
  applyPetEnabled(enabled);
}

// ---------- PTY 宿主（终端页签的真伪终端，见 pty-host.cjs） ----------
let ptyHost = null;
let ptyHostReady = false;
const pendingTerminalCreates = new Map();
let ptyHostBuffer = '';
/** 当前生存的窗口（终端事件要发给它；多窗口场景取最后创建的） */
let mainWindow = null;
let mainPetReady = false;
let pendingPetRoute = null;

function ptyHostLogPath() {
  const dir = path.join(app.getPath('userData'), 'logs');
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, 'pty-host.log');
}

function startPtyHost() {
  if (ptyHost) return;
  const log = fs.createWriteStream(ptyHostLogPath(), { flags: 'a' });

  // 打包后 pty-host 与 node-pty 平铺在 resources/pty-host（系统 Node 读不了 asar）；
  // 开发态就在 desktop/ 目录里 require node_modules/node-pty。
  const script = app.isPackaged
    ? path.join(process.resourcesPath, 'pty-host', 'pty-host.cjs')
    : path.join(__dirname, 'pty-host.cjs');
  const cwd = app.isPackaged ? path.dirname(script) : __dirname;

  ptyHostReady = false;
  // 系统 node：pty-host 跑在 Node ABI 下，与 Electron ABI 解耦（见 pty-host.cjs 头注）
  ptyHost = spawn('node', [script], {
    cwd,
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
  });

  const rl = readline.createInterface({ input: ptyHost.stdout });
  rl.on('line', (line) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    let msg;
    try {
      msg = JSON.parse(trimmed);
    } catch {
      log.write(`[bad line] ${trimmed.slice(0, 200)}\n`);
      return;
    }
    if (msg.type === 'ready') {
      ptyHostReady = true;
      return;
    }
    if (msg.type === 'created' || msg.type === 'error') {
      const complete = pendingTerminalCreates.get(msg.id);
      if (complete) complete(msg.type === 'created' ? { cwd: msg.cwd, shell: msg.shell } : false);
      return;
    }
    // data / exit / error → 转发给窗口；窗口可能还没建好（会话创建前的输出）
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send(`terminal:${msg.type === 'data' ? 'data' : 'exit'}`, msg);
    }
  });

  ptyHost.stderr.on('data', (d) => log.write(`[stderr] ${d.toString()}`));
  ptyHost.on('error', error => {
    log.write(`[启动失败] ${error.message}\n`);
    ptyHost = null; ptyHostReady = false;
    for (const complete of pendingTerminalCreates.values()) complete(false);
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send('terminal:exit', { id: '*', exitCode: -1 });
  });
  ptyHost.on('exit', (code) => {
    log.write(`===== pty 宿主退出（code=${code}）=====\n`);
    ptyHost = null;
    ptyHostReady = false;
    for (const complete of pendingTerminalCreates.values()) complete(false);
    // 让前端的真终端切回 HTTP 回退模式
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('terminal:exit', { id: '*', exitCode: code });
    }
  });
}

function killPtyHost() {
  if (!ptyHost) return;
  const proc = ptyHost;
  ptyHost = null;
  ptyHostReady = false;
  if (process.platform === 'win32') {
    execFile('taskkill', ['/PID', String(proc.pid), '/T', '/F'], () => {});
  } else {
    try {
      proc.kill('SIGTERM');
    } catch {
      /* 已退出 */
    }
  }
}

function ptySend(obj) {
  if (!ptyHost || !ptyHostReady) return false;
  ptyHost.stdin.write(JSON.stringify(obj) + '\n');
  return true;
}

function registerTerminalIpc() {
  /** 宿主就绪（若未启动则先启动），deadline 内没就绪返回 false */
  async function ensureHost(deadlineMs = 5000) {
    if (ptyHostReady) return true;
    startPtyHost();
    const deadline = Date.now() + deadlineMs;
    while (Date.now() < deadline) {
      if (ptyHostReady) return true;
      if (!ptyHost) return false; // 宿主拉起后立刻死了（node 缺失等）
      await new Promise((r) => setTimeout(r, 100));
    }
    return false;
  }

  ipcMain.handle('terminal:ptyAvailable', () => ensureHost());

  // create：确保宿主活着再转发；不可用时返回 false（前端据此走 HTTP 回退）
  ipcMain.handle('terminal:create', async (_e, { id, cols, rows, shell: shellProfile }) => {
    if (!(await ensureHost())) return false;
    if (typeof id !== 'string' || !id || pendingTerminalCreates.has(id)) return false;
    return new Promise(resolve => {
      const timer = setTimeout(() => { ptySend({ type: 'kill', id }); complete(false); }, 8000);
      const complete = result => {
        clearTimeout(timer); pendingTerminalCreates.delete(id); resolve(result);
      };
      pendingTerminalCreates.set(id, complete);
      if (!ptySend({ type: 'create', id, cols, rows, shellProfile, backendDir: backendDir() })) complete(false);
    });
  });

  ipcMain.on('terminal:input', (_e, { id, data }) => ptySend({ type: 'input', id, data }));
  ipcMain.on('terminal:resize', (_e, { id, cols, rows }) => ptySend({ type: 'resize', id, cols, rows }));
  ipcMain.on('terminal:kill', (_e, { id }) => ptySend({ type: 'kill', id }));
}

/**
 * 系统目录选择框（「新建工作区」用）
 *
 * 页面只给一个标题字符串，路径由用户在系统对话框里选 —— 桥不接受页面传进来的
 * 路径参数，也就没有「借这条桥看任意目录」的口子（对话框本身不落盘、不读内容）。
 */
function registerDialogIpc() {
  ipcMain.handle('dialog:selectFolder', async (event, title) => {
    const win = BrowserWindow.fromWebContents(event.sender) ?? mainWindow;
    if (!win || win.isDestroyed()) return null;
    const { canceled, filePaths } = await dialog.showOpenDialog(win, {
      title: typeof title === 'string' ? title.slice(0, 120) : undefined,
      // createDirectory：工作区常常是个新建的空目录，让用户在对话框里顺手建掉，
      // 不必先去资源管理器里绕一圈
      properties: ['openDirectory', 'createDirectory'],
    });
    return canceled || filePaths.length === 0 ? null : filePaths[0];
  });
}

/**
 * 在资源管理器里打开一个文件夹（「设置 → 数据」的「打开」）
 *
 * 桥面只接受**绝对路径且必须是已存在的目录**：它不能拿去打开一个可执行文件，
 * 也不接受相对路径（那会跟着壳的工作目录漂移）。路径来自后端的工作区清单。
 */
function registerShellIpc() {
  ipcMain.handle('shell:openPath', async (_e, target) => {
    if (typeof target !== 'string' || !target.trim()) return false;
    const resolved = path.resolve(target);
    try {
      if (!fs.statSync(resolved).isDirectory()) return false;
    } catch {
      return false;
    }
    // openPath 成功时返回空串，失败时返回一段给人看的错误
    const err = await shell.openPath(resolved);
    return !err;
  });
}

/**
 * 自绘标题栏（前端 TitleBar）需要的窗口桥。
 *
 * overlay：前端主题切换时把 titleBarOverlay 的底色/键色同步成界面同款 ——
 * 只收两个 #RRGGBB 字符串，别的字段（height 等）不开放给页面改。
 * toggleFullscreen / quit：视图菜单的「全屏」与文件菜单的「退出」。
 */
function registerWindowIpc() {
  ipcMain.handle('window:overlay', (event, patch) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (!win || win.isDestroyed() || process.platform !== 'win32') return false;
    const clean = {};
    for (const key of ['color', 'symbolColor']) {
      if (typeof patch?.[key] === 'string' && /^#[0-9a-fA-F]{6}$/.test(patch[key])) clean[key] = patch[key];
    }
    if (!Object.keys(clean).length) return false;
    try {
      win.setTitleBarOverlay(clean);
      return true;
    } catch {
      return false;
    }
  });
  ipcMain.handle('window:toggleFullscreen', (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (!win || win.isDestroyed()) return false;
    win.setFullScreen(!win.isFullScreen());
    return win.isFullScreen();
  });
  ipcMain.handle('app:quit', () => {
    app.isQuitting = true;
    app.quit();
  });
}

function backendLogPath() {
  const dir = path.join(app.getPath('userData'), 'logs');
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, 'backend.log');
}

/**
 * Windows 上必须 taskkill /T 杀整棵树：uvicorn（尤其带 --reload 的开发后端）
 * 会派生 worker 子进程，只 kill 主 PID 会留下占着 8000 端口的孤儿。
 */
function killBackendTree() {
  if (!backendProc || backendProc.killed) return;
  const proc = backendProc;
  const pid = proc.pid;
  backendProc = null;
  if (process.platform === 'win32') {
    execFile('taskkill', ['/PID', String(pid), '/T', '/F'], () => {});
  } else {
    try {
      process.kill(-pid, 'SIGTERM'); // 设了 detached 的进程组
    } catch {
      try {
        proc.kill('SIGTERM');
      } catch {
        /* 进程已经不在了 —— 正是想要的结果 */
      }
    }
  }
}

/**
 * 解析后端 python：backend/.venv → 本机 Anaconda → PATH 上的 python。
 * 与 start.ps1 的 Get-PythonCmd 同一顺序。不能只依赖 PATH —— 全局 python
 * 可能是托管解释器，没装 fastapi/langgraph，起后端会直接 ImportError。
 */
function resolvePython() {
  if (process.platform !== 'win32') return 'python3';
  const candidates = [
    path.join(backendDir(), '.venv', 'Scripts', 'python.exe'),
    'B:\\Anconda3\\python.exe',
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return 'python';
}

/** 拉起 uvicorn。内部已就绪的判断在调用方（先 probe 再 spawn） */
function startBackend() {
  const dir = backendDir();
  const python = resolvePython();

  logStream = fs.createWriteStream(backendLogPath(), { flags: 'a' });
  logStream.write(`\n===== ${new Date().toISOString()} 启动后端（${dir}）=====\n`);

  // Windows 上 detached 让子进程自成进程组，taskkill /T 之外多一条杀整组的路
  backendProc = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--port', String(BACKEND_PORT)], {
    cwd: dir,
    detached: process.platform !== 'win32',
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  backendProc.stdout.pipe(logStream);
  backendProc.stderr.pipe(logStream);
  backendProc.on('exit', (code) => {
    if (logStream) logStream.write(`===== 后端退出（code=${code}）=====\n`);
    backendProc = null;
  });
}

/**
 * 麦克风权限：只放行音频，不放开视频。
 *
 * 桌面助手是 `file://` 页面，Chromium 对 getUserMedia 会走权限询问；不装这个
 * handler 时 Electron 的默认行为是全放行（包括摄像头）。这里把媒体权限收窄
 * 到「音频」，其余权限保持 Electron 的默认放行，避免主窗现有功能出现回归。
 */
function installPetMediaPermissions() {
  if (!session) return;
  const ses = session.defaultSession;
  ses.setPermissionRequestHandler((_webContents, permission, callback, details) => {
    if (permission !== 'media') {
      callback(true);
      return;
    }
    const types = details?.mediaTypes ?? [];
    callback(!types.includes('video'));
  });
  ses.setPermissionCheckHandler((_webContents, permission, _origin, details) => {
    if (permission === 'media') {
      // check 回调的字段是 mediaType（单数），request 才是 mediaTypes 数组
      const types = details?.mediaTypes ?? (details?.mediaType ? [details.mediaType] : []);
      return !types.includes('video');
    }
    return true;
  });
}

/** CORS 桥接（见文件头说明）。只在窗口创建前装一次 */
function installCorsBridge() {
  const filter = { urls: [`${BACKEND_ORIGIN}/*`] };

  session.defaultSession.webRequest.onBeforeSendHeaders(filter, (details, callback) => {
    const headers = { ...details.requestHeaders, Origin: 'http://localhost:5173' };
    callback({ requestHeaders: headers });
  });

  session.defaultSession.webRequest.onHeadersReceived(filter, (details, callback) => {
    const headers = { ...details.responseHeaders };
    // 前端 fetch 不带凭据，`*` 即可；覆盖而非追加，避免与后端自己的头重复
    headers['access-control-allow-origin'] = ['*'];
    headers['access-control-allow-headers'] = ['*'];
    headers['access-control-allow-methods'] = ['GET,POST,PUT,PATCH,DELETE,OPTIONS'];
    callback({ responseHeaders: headers });
  });
}

/** 显示并聚焦主窗；窗口已被销毁时重建（托盘唤回走这里） */
function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return createWindow();
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
  return mainWindow;
}

function configureEmbeddedBrowser(win) {
  win.webContents.on('will-attach-webview', (_event, preferences) => {
    delete preferences.preload;
    preferences.nodeIntegration = false;
    preferences.contextIsolation = true;
    preferences.sandbox = true;
  });
  win.webContents.on('did-attach-webview', (_event, guest) => {
    guest.setWindowOpenHandler(({ url }) => {
      if (/^https?:\/\//i.test(url)) void shell.openExternal(url);
      return { action: 'deny' };
    });
    guest.on('will-navigate', (event, url) => {
      if (!/^https?:\/\//i.test(url)) event.preventDefault();
    });
    guest.on('before-input-event', (event, input) => {
      if (input.type !== 'keyDown') return;
      if (input.key === 'Escape') win.webContents.send('workbench:escape');
      if ((input.control || input.meta) && input.key.toLowerCase() === 'l') {
        event.preventDefault(); win.webContents.send('browser:focus-address');
      }
    });
  });
}

function createWindow() {
  mainPetReady = false;
  const win = new BrowserWindow({
    width: 1360,
    height: 900,
    minWidth: 960,
    minHeight: 640,
    title: 'Syncore',
    // 应用图标（任务栏 / 窗口左上角）：与 favicon、侧栏品牌 mark 同一个资产
    icon: path.join(__dirname, 'build', 'icon.ico'),
    backgroundColor: '#f6f6f8',
    // Windows 上自绘标题栏（前端 TitleBar 组件）：导航、菜单、侧栏开关都在这条栏里；
    // 窗口三键仍由系统 titleBarOverlay 绘制，保留 Snap 布局与原生手感。
    // 非 Windows 保留系统标题栏，前端据 preload 里的 platform 判断要不要画 TitleBar。
    titleBarStyle: process.platform === 'win32' ? 'hidden' : 'default',
    titleBarOverlay:
      process.platform === 'win32'
        ? { color: '#f6f6f8', symbolColor: '#4b4b52', height: 40 }
        : undefined,
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      // 壳不加载任何远程内容，不需要 Node 能力透传给页面
      contextIsolation: true,
      nodeIntegration: false,
      // 侧栏「浏览器」页签在桌面端用 <webview> 真嵌入（不受 X-Frame-Options 限制）
      webviewTag: true,
      preload: path.join(__dirname, 'preload.cjs'),
      // 打包后不开放 DevTools；开发态保留，便于调试渲染层
      devTools: DEV_MODE || !app.isPackaged,
    },
  });

  // 产物里的外链（web_search 引用等）走系统浏览器；blob: 是画布「新窗口打开」
  // 的产物，必须留在应用内开新窗 —— 交给系统浏览器只会失败
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('blob:')) {
      return { action: 'allow', overrideBrowserWindowOptions: { autoHideMenuBar: true } };
    }
    if (/^https?:/i.test(url)) {
      shell.openExternal(url);
    }
    return { action: 'deny' };
  });
  win.webContents.on('did-start-loading', () => { mainPetReady = false; });
  configureEmbeddedBrowser(win);

  if (DEV_MODE) {
    win.loadURL(DEV_URL);
  } else {
    win.loadFile(path.join(frontendDir(), 'index.html'));
  }

  win.once('ready-to-show', () => win.show());
  mainWindow = win;
  // 关闭 = 收进托盘，应用继续在后台跑；退出走托盘菜单。
  // 没装成托盘时不能拦关闭，否则用户找不到退出入口。
  win.on('close', (e) => {
    if (!app.isQuitting && tray) {
      e.preventDefault();
      win.hide();
    }
  });
  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null;
  });
  return win;
}

/**
 * 桌面助手透明置顶窗 —— 独立于主窗的 BrowserWindow。
 *
 * ★ 为什么透明窗要这样调参
 *
 * - transparent: true + backgroundColor: '#00000000' —— 整窗 alpha 通道透明，
 *   pet.tsx 渲染的角色 sprite 之外的区域直接穿透到桌面；
 * - frame: false + skipTaskbar: true —— 不画标题栏、不上任务栏，桌面助手不是「应用窗」；
 * - alwaysOnTop + level 'floating' —— 高于普通窗但不盖系统通知/截图工具；
 * - resizable: false + hasShadow: false —— 防止 Windows 给透明窗加默认阴影边；
 * - focusable: false —— 让桌面助手窗不抢主窗焦点；点击桌面助手改由 Pet.tsx 自己处理
 *   click 事件转 IPC，不依赖窗口获得焦点。
 *
 * 默认点击穿透，渲染层按图片 alpha 与可见控件判断命中。
 * forward:true 继续转发鼠标移动，角色可在命中后恢复接收点击。
 */
/** 桌面助手默认落点：主屏右下角，避开任务栏与屏幕边缘 */
function defaultPetPosition(state = loadPetState()) {
  const workArea = screen.getPrimaryDisplay().workArea;
  const size = petSize(state.scale);
  return {
    x: Math.max(workArea.x, workArea.x + workArea.width - size.width - 40),
    y: Math.max(workArea.y, workArea.y + workArea.height - size.height - 20),
  };
}

function fitPetWindow(state = loadPetState()) {
  if (!petWindow || petWindow.isDestroyed()) return;
  const previous = petWindow.getBounds();
  const size = petSize(state.scale);
  // Keep the character's feet in place when changing size.
  const bounds = { ...size, x: previous.x + previous.width - size.width, y: previous.y + previous.height - size.height };
  petWindow.setBounds(clampPetBounds(bounds, screen.getDisplayMatching(bounds).workArea), false);
  petWindow.webContents.setZoomFactor(state.scale);
  petWindow.setAlwaysOnTop(state.alwaysOnTop !== 'normal', state.alwaysOnTop === 'normal' ? undefined : state.alwaysOnTop);
}

function createPetWindow() {
  if (petWindow && !petWindow.isDestroyed()) return petWindow;

  const state = loadPetState();
  const fallback = defaultPetPosition();
  const size = petSize(state.scale);
  const initial = { ...size, x: state.x ?? fallback.x, y: state.y ?? fallback.y };
  const bounds = clampPetBounds(initial, screen.getDisplayMatching(initial).workArea);

  petWindow = new BrowserWindow({
    ...bounds,
    transparent: true,
    frame: false,
    skipTaskbar: true,
    resizable: false,
    hasShadow: false,
    focusable: false,
    alwaysOnTop: state.alwaysOnTop !== 'normal',
    backgroundColor: '#00000000',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      // 隐藏窗口时也要保持音频采集与 WebSocket：默认的后台节流会把
      // ScriptProcessor 的节奏拉长，唤醒会变得迟钝。
      backgroundThrottling: false,
      preload: path.join(__dirname, 'preload.cjs'),
      devTools: DEV_MODE || !app.isPackaged,
    },
  });
  petWindow.setAlwaysOnTop(state.alwaysOnTop !== 'normal', state.alwaysOnTop === 'normal' ? undefined : state.alwaysOnTop);
  petWindow.setIgnoreMouseEvents(true, { forward: true });
  petWindow.webContents.on('did-finish-load', () => {
    if (!petWindow || petWindow.isDestroyed()) return;
    petWindow.webContents.setZoomFactor(loadPetState().scale);
    sendPetVisibility();
    petWindow.webContents.send('pet:runtime', { ...latestPetRuntime });
  });
  petWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  petWindow.on('show', sendPetVisibility);
  petWindow.on('hide', () => {
    resetPetVoiceBusy();
    sendPetVisibility();
  });

  if (DEV_MODE) {
    // 开发态用 query 区分，让 vite 把 pet.html 路由到 pet 入口
    petWindow.loadURL(`${DEV_URL}/pet.html`);
  } else {
    petWindow.loadFile(path.join(frontendDir(), 'pet.html'));
  }

  // 桌面助手窗的关闭 = 隐藏而非退出：从托盘/设置面板再次唤起。
  // 这里把 close 改成 hide，避免桌面助手被误关后只能重启应用。
  petWindow.on('close', (e) => {
    if (!app.isQuitting) {
      e.preventDefault();
      petWindow.hide();
    }
  });

  petWindow.on('closed', () => {
    petDragOffset = null;
    petWindow = null;
    resetPetVoiceBusy();
  });

  return petWindow;
}

/**
 * 桌面助手 IPC：拖拽、点击穿透、主窗可见性、状态读写。
 *
 * 拖拽走主进程 setBounds 而不是 CSS `-webkit-app-region: drag`：
 * 透明 frameless 窗上 CSS drag 区容易和角色 sprite 区冲突，且 drag 时不发 mousemove
 * 事件，没法做 hover 切穿透。主进程跟手最稳。
 */
let latestPetActivity = { mood: 'idle', text: '', replyId: '' };

/**
 * 主窗 → 桌面助手的运行时状态。
 *
 * 权限档位和「助理是否在忙」只存在主窗 store 里；桌面助手是另一个渲染进程，
 * 不能直接读 store。这里做一次最小同步：主窗发布，桌面助手订阅；`voiceBusy`
 * 反过来由桌面助手发布，主窗据此禁用输入框。
 */
let latestPetRuntime = {
  permissionMode: '',
  assistantBusy: false,
  webEnabled: true,
  thinkingEnabled: false,
  voiceBusy: false,
};
const petVoiceRelay = require('./pet-voice.cjs').createPetVoiceRelay();

function sendPetVoiceUpdate(update) {
  if (update && mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('pet:voice-update', update);
  }
}

function petVisible() {
  return Boolean(
    petWindow &&
    !petWindow.isDestroyed() &&
    (typeof petWindow.isVisible !== 'function' || petWindow.isVisible()),
  );
}

function sendPetVisibility() {
  if (!petWindow || petWindow.isDestroyed()) return;
  petWindow.webContents.send('pet:visibility', petVisible());
}

function broadcastPetRuntime() {
  for (const win of [mainWindow, petWindow]) {
    if (win && !win.isDestroyed() && win.webContents) {
      win.webContents.send('pet:runtime', { ...latestPetRuntime });
    }
  }
}

function resetPetVoiceBusy() {
  sendPetVoiceUpdate(petVoiceRelay.end());
  if (!latestPetRuntime.voiceBusy) return;
  latestPetRuntime = { ...latestPetRuntime, voiceBusy: false };
  broadcastPetRuntime();
}

function registerPetIpc() {
  const isPet = (event) => petWindow && !petWindow.isDestroyed() && event.sender === petWindow.webContents;
  const isMain = (event) => mainWindow && !mainWindow.isDestroyed() && event.sender === mainWindow.webContents;
  const trusted = (event) => isPet(event) || isMain(event);
  // 拖拽开始：记录「鼠标当前坐标 - 窗口左上角」偏移；move 时用 cursor - offset = 新窗口坐标。
  ipcMain.on('pet:drag-start', (event) => {
    if (!isPet(event)) return;
    const win = BrowserWindow.fromWebContents(event.sender);
    if (!win || win.isDestroyed()) return;
    const cursor = screen.getCursorScreenPoint();
    const [wx, wy] = win.getPosition();
    petDragOffset = { x: cursor.x - wx, y: cursor.y - wy, ...petSize(loadPetState().scale) };
  });

  // 拖拽移动：渲染层 throttle 后转发鼠标坐标；主进程算新窗口坐标并 setPosition。
  // 不直接用 cursor - 偏移：跨屏幕（多显示器边界）时 cursor 可能跳到负坐标， setPosition
  // 负值合法但不直觉，保持原算法即可。
  ipcMain.on('pet:drag-move', (event) => {
    if (!isPet(event)) return;
    const win = BrowserWindow.fromWebContents(event.sender);
    if (!win || win.isDestroyed() || !petDragOffset) return;
    const cursor = screen.getCursorScreenPoint();
    const bounds = { width: petDragOffset.width, height: petDragOffset.height, x: cursor.x - petDragOffset.x, y: cursor.y - petDragOffset.y };
    win.setBounds(clampPetBounds(bounds, screen.getDisplayNearestPoint(cursor).workArea), false);
  });

  // 拖拽结束：持久化新坐标。
  ipcMain.on('pet:drag-end', (event) => {
    if (!isPet(event)) return;
    const win = BrowserWindow.fromWebContents(event.sender);
    petDragOffset = null;
    if (!win || win.isDestroyed()) return;
    const bounds = win.getBounds();
    const area = screen.getDisplayMatching(bounds).workArea;
    const right = area.x + area.width - bounds.width;
    if (Math.abs(bounds.x - area.x) < 16) bounds.x = area.x;
    if (Math.abs(bounds.x - right) < 16) bounds.x = right;
    const fitted = clampPetBounds(bounds, area);
    win.setBounds(fitted, false);
    const { x, y } = fitted;
    const state = loadPetState();
    state.x = x;
    state.y = y;
    savePetState(state);
  });

  // 整窗点击穿透切换：forward:true 让穿透状态下 mousemove 仍转发到页面，
  // 页面据此在鼠标进入 sprite 区域时切回非穿透。
  ipcMain.on('pet:set-click-through', (event, ignored) => {
    if (!isPet(event)) return;
    const win = BrowserWindow.fromWebContents(event.sender);
    if (!win || win.isDestroyed()) return;
    win.setIgnoreMouseEvents(Boolean(ignored), { forward: Boolean(ignored) });
  });

  ipcMain.on('pet:set-panel-open', (event, open) => {
    if (!isPet(event)) return;
    petWindow.setFocusable(Boolean(open));
    if (open) {
      petWindow.setIgnoreMouseEvents(false);
      petWindow.focus();
    }
  });

  // 单击桌面助手 → 切主窗可见性：主窗可见时聚焦，隐藏时显示。
  ipcMain.handle('pet:toggle-main', (event) => {
    if (!trusted(event)) return false;
    showMainWindow();
    return true;
  });

  ipcMain.handle('pet:open-main', (event, route) => {
    if (!trusted(event) || !['/assistant', '/chat', '/tasks', '/schedules', '/settings/pet'].includes(route)) return false;
    const win = showMainWindow();
    pendingPetRoute = route;
    if (mainPetReady && !win.webContents.isLoading()) {
      pendingPetRoute = null;
      win.webContents.send('pet:navigate', route);
    }
    return true;
  });

  ipcMain.on('pet:main-ready', (event) => {
    if (!isMain(event)) return;
    mainPetReady = true;
    if (pendingPetRoute) {
      const route = pendingPetRoute;
      pendingPetRoute = null;
      mainWindow.webContents.send('pet:navigate', route);
    }
  });

  ipcMain.on('pet:publish-activity', (event, activity) => {
    if (!isMain(event) || !activity || !['idle', 'thinking', 'approval', 'error', 'done'].includes(activity.mood)) return;
    latestPetActivity = { mood: activity.mood, text: String(activity.text ?? '').slice(0, 240), replyId: String(activity.replyId ?? '').slice(0, 160), route: ['/assistant', '/chat', '/tasks'].includes(activity.route) ? activity.route : '/chat' };
    if (petWindow && !petWindow.isDestroyed()) petWindow.webContents.send('pet:activity', latestPetActivity);
  });
  ipcMain.handle('pet:get-activity', (event) => trusted(event) ? latestPetActivity : null);
  ipcMain.handle('pet:get-pointer', (event) => {
    if (!isPet(event)) return null;
    const cursor = screen.getCursorScreenPoint();
    const bounds = petWindow.getBounds();
    const zoom = petWindow.webContents.getZoomFactor();
    return { x: (cursor.x - bounds.x) / zoom, y: (cursor.y - bounds.y) / zoom };
  });

  // 主窗发布「本轮权限档 + 助理是否在忙 + 输入框开关」；桌面助手只读。
  ipcMain.on('pet:publish-runtime', (event, runtime) => {
    if (!isMain(event) || !runtime || typeof runtime !== 'object' || Array.isArray(runtime)) return;
    const next = {
      ...latestPetRuntime,
      permissionMode: PET_PERMISSION_MODES.includes(runtime.permissionMode) ? runtime.permissionMode : '',
      assistantBusy: Boolean(runtime.assistantBusy),
      webEnabled: runtime.webEnabled !== false,
      thinkingEnabled: Boolean(runtime.thinkingEnabled),
    };
    if (next.permissionMode === latestPetRuntime.permissionMode &&
        next.assistantBusy === latestPetRuntime.assistantBusy &&
        next.webEnabled === latestPetRuntime.webEnabled &&
        next.thinkingEnabled === latestPetRuntime.thinkingEnabled) return;
    latestPetRuntime = next;
    broadcastPetRuntime();
  });

  // 桌面助手发布「语音回合占用中」；只允许桌面助手写，主窗据此禁用助理输入框。
  ipcMain.on('pet:set-voice-busy', (event, busy) => {
    if (!isPet(event)) return;
    const value = Boolean(busy);
    if (value === latestPetRuntime.voiceBusy) return;
    latestPetRuntime = { ...latestPetRuntime, voiceBusy: value };
    broadcastPetRuntime();
  });

  ipcMain.handle('pet:get-runtime', (event) => trusted(event) ? { ...latestPetRuntime } : null);

  ipcMain.on('pet:publish-voice', (event, update) => {
    if (!isPet(event)) return;
    sendPetVoiceUpdate(petVoiceRelay.accept(update));
  });
  ipcMain.handle('pet:get-voice-turn', (event) => isMain(event) ? petVoiceRelay.get() : null);
  ipcMain.on('pet:cancel-voice', (event) => {
    if (!trusted(event) || !petWindow || petWindow.isDestroyed()) return;
    petWindow.webContents.send('pet:cancel-voice');
  });

  // 隐藏桌面助手：右键菜单触发。等同「关闭」—— 持久化 enabled=false 并销毁窗口，
  // 否则 Live2D 的 60fps ticker 会在看不见的窗里继续烧 CPU；重新打开走设置页。
  ipcMain.on('pet:hide', (event) => {
    if (!trusted(event)) return;
    savePetState({ ...loadPetState(), enabled: false });
    applyPetEnabled(false);
  });

  // 状态读写：渲染层启动时 getState 取初始坐标/形象/设置；用户改设置后 setState 持久化。
  ipcMain.handle('pet:get-state', (event) => trusted(event) ? loadPetState() : null);
  ipcMain.on('pet:set-state', (event, patch) => {
    if (!trusted(event) || !patch || typeof patch !== 'object' || Array.isArray(patch)) return;
    // patch 是 delta，合并到现有状态；不直接覆写整文件避免丢字段
    const state = loadPetState();
    const next = savePetState({ ...state, ...patch });
    if (next.scale !== state.scale || next.alwaysOnTop !== state.alwaysOnTop) fitPetWindow(next);
    // 开关：运行时建窗/销毁窗，不必重启应用
    if (typeof patch.enabled === 'boolean') {
      applyPetEnabled(next.enabled);
    }
  });

  // 显示桌面助手：窗口不存在就现建；持久化 enabled 让下次启动也记得。
  ipcMain.handle('pet:show', (event) => {
    if (!trusted(event)) return false;
    setPetEnabled(true);
    return true;
  });

  // 回到默认落点；返回写盘后的完整状态，设置页直接拿它刷新 UI。
  ipcMain.handle('pet:reset-position', (event) => {
    if (!trusted(event)) return null;
    const { x, y } = defaultPetPosition();
    const next = { ...loadPetState(), x, y };
    savePetState(next);
    if (petWindow && !petWindow.isDestroyed()) petWindow.setBounds({ ...petSize(next.scale), x, y }, false);
    return next;
  });

  screen.on('display-removed', () => fitPetWindow());
  screen.on('display-metrics-changed', () => fitPetWindow());
}

// ---------- 系统托盘 ----------
const CONTROL_STOP_SHORTCUT = 'Control+Alt+F12';
let controlStopShortcutAvailable = false;
function stopDesktopControl() {
  const request = http.request(`${BACKEND_ORIGIN}/api/computer-control/stop`, {
    method: 'POST', timeout: 5000,
    headers: { 'Content-Type': 'application/json', 'Content-Length': 2 },
  }, response => {
    const ok = response.statusCode === 200;
    response.resume();
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send('computer-control:stopped', { ok });
    if (!ok) console.error('[computer-control] Stop failed:', response.statusCode);
  });
  request.on('timeout', () => request.destroy(new Error('Stop timeout')));
  request.on('error', error => {
    console.error('[computer-control]', error.message);
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send('computer-control:stopped', { ok: false });
  });
  request.end('{}');
}

// 托盘让主窗关掉后应用依然留在后台（后端、桌面助手、定时任务不中断），
// 用户从图标随时唤回主窗或退出。没装成托盘时关闭主窗仍是直接退出。
let tray = null;

/** 菜单随桌面助手开关重建：勾选状态读 pet-state.json，避免两处各存一份 */
function refreshTrayMenu() {
  if (!tray) return;
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: '打开工作台', click: () => showMainWindow() },
      { label: '停止电脑操控', click: stopDesktopControl },
      {
        label: '显示桌面助手',
        type: 'checkbox',
        checked: loadPetState().enabled,
        click: (item) => setPetEnabled(item.checked),
      },
      { type: 'separator' },
      { label: '退出 Syncore', click: () => app.quit() },
    ]),
  );
}

function createTray() {
  if (tray) return tray;
  const iconPath = path.join(__dirname, 'build', 'icon.ico');
  const icon = nativeImage.createFromPath(iconPath);
  if (icon.isEmpty()) {
    // 图标缺失就退回「无托盘」模式：关闭主窗依旧退出，不至于把应用锁在后台
    console.warn(`[tray] 图标缺失，跳过托盘：${iconPath}`);
    return null;
  }
  tray = new Tray(icon);
  tray.setToolTip('Syncore');
  // Windows 上左键单击托盘默认无动作；单击/双击都把主窗叫回来
  tray.on('click', () => showMainWindow());
  tray.on('double-click', () => showMainWindow());
  refreshTrayMenu();
  return tray;
}

/**
 * 探测 → 复用或拉起 → 等后端就绪。
 *
 * 返回 'reused'（端口上已有存活后端）/ 'ok'（本壳拉起并就绪）/ 'failed'
 * （90s 内没就绪，已弹错误框）。调用方**不必 await**：桌面端正常启动时窗口
 * 已经先开，后端在后台把这件事跑完，就绪与否由前端加载态（BackendBootGate）
 * 用 HTTP 轮询 /api/info 自己感知 —— 壳不新增 backend 桥（见 preload.cjs 的设计约束）。
 */
async function ensureBackend() {
  // 探测走 /api/info（只读配置、秒回）。依赖（Milvus/ES/Redis/Neo4j）是否 up
  // 由后端自己降级处理，不构成「后端是否起来」的判据。
  if ((await probeHealth(3000)) || (await probeHealth(3000))) {
    return 'reused';
  }
  startBackend();
  // 后端冷启动要导入依赖 + 起检索栈（本地模型已挪到后台预热，不在阻塞路径），
  // 实测十余秒；给足 90 秒余量，避免「首次启动慢」被误判成「启动失败」。
  const ok = await waitForBackend(90_000);
  if (!ok) {
    dialog.showErrorBox(
      '后端启动失败',
      `后端服务在 90 秒内没有就绪。\n\n` +
        `常见原因：\n` +
        `1. 未安装 Python 依赖（backend/ 目录下执行 pip install -r requirements.txt）；\n` +
        `2. 找不到可用的 Python（本壳依次尝试 backend\\.venv → B:\\Anconda3 → PATH 上的 python）；\n` +
        `3. 端口 ${BACKEND_PORT} 被非本应用的程序占用；\n` +
        `4. 首次启动较慢（加载本地模型），可再等一会儿重试。\n\n` +
        `详细日志：${backendLogPath()}`,
    );
    return 'failed';
  }
  return 'ok';
}

async function main() {
  // 默认菜单带「切换开发者工具」等调试入口，打包后不应出现；
  // 开发态保留菜单，方便本地调试
  if (!DEV_MODE) Menu.setApplicationMenu(null);

  registerTerminalIpc();
  controlStopShortcutAvailable = globalShortcut.register(CONTROL_STOP_SHORTCUT, stopDesktopControl);
  ipcMain.handle('computer-control:shortcut-status', () => controlStopShortcutAvailable);
  registerDialogIpc();
  registerShellIpc();
  registerWindowIpc();
  registerPetIpc();
  installCorsBridge();
  installPetMediaPermissions();
  createTray();

  // ★ 桌面端「窗口先行」：先把窗口拉起来，前端用加载态（转圈）等后端就绪；
  //   后端探测/拉起放后台跑，不再把用户卡在冷启动上。就绪信号走 HTTP（前端轮询
  //   /api/info），与 preload「不放 backend 桥」的设计约束一致。
  const win = createWindow();

  // 桌面助手窗延时创建：等主窗 paint 出内容（约 800ms 经验值），避免两个渲染进程
  // 同时初始化争抢资源；只在 pet-state.json 里 enabled=true 时才创建 ——
  // 首次用户没开过桌面助手，pet-state.json 不存在，DEFAULT_PET_STATE.enabled=false，
  // 桌面助手不会突然冒出来吓人。开启入口在主窗设置面板。
  win.once('ready-to-show', () => {
    const state = loadPetState();
    if (state.enabled) {
      setTimeout(() => {
        if (!app.isQuitting && loadPetState().enabled) createPetWindow();
      }, PET_DELAY_MS);
    }
  });

  void ensureBackend();
}

// 主窗关闭 = 收进托盘继续后台运行（有托盘时）；退出走托盘菜单，退出时
// 后端 + pty 宿主一并回收。
// ⚠️ app.exit() 会跳过 will-quit / before-quit —— 需要「先杀再退出」的路径
// 必须显式调用 killBackendTree() / killPtyHost()
function killAllChildren() {
  killBackendTree();
  killPtyHost();
  if (petWindow && !petWindow.isDestroyed()) {
    // 标记退出，让 pet 窗的 close 钩子放行 destroy 而不是 hide
    app.isQuitting = true;
    petWindow.destroy();
    petWindow = null;
  }
}
// before-quit 先置退出标记：主窗 close 钩子据此放行，否则隐藏中的主窗
// 会把 app.quit() 拦下来（close 事件先于 will-quit 触发）
app.on('before-quit', () => {
  app.isQuitting = true;
});
app.on('window-all-closed', () => {
  if (tray && !app.isQuitting) return; // 托盘常驻：不跟着最后一个窗口退出
  killAllChildren();
  app.quit();
});
app.on('will-quit', () => {
  globalShortcut.unregister(CONTROL_STOP_SHORTCUT);
  if (backendProc || ptyHost || petWindow) killAllChildren();
});
process.on('exit', killAllChildren);

app.whenReady().then(main);
