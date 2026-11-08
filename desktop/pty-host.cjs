/**
 * PTY 宿主进程 —— 真伪终端的承载者（node-pty），跑在**系统 Node** 下
 *
 * ★ 为什么不直接在 Electron 主进程里 require node-pty
 *
 * node-pty 是原生模块，按 Node 的 ABI 编译；Electron 的 ABI 与同版本 Node
 * 不同，直接 require 大概率报 NODE_MODULE_VERSION 不匹配，rebuild 又要把
 * Visual Studio Build Tools 变成用户的安装前提。折中方案（也是 VS Code 的
 * 架构——pty host 是独立进程）：主进程用系统 Node 拉起本文件，两者通过
 * **stdin/stdout 上的 JSON 行协议**通信。ABI 问题就此消失——它只依赖
 * 用户机器上已有的 node。
 *
 * ★ 协议（每行一个 JSON 对象）
 *
 *   → { type: 'create', id, cwd, cols, rows, env? }   开一个会话
 *   → { type: 'input',  id, data }                     写入（键击/粘贴）
 *   → { type: 'resize', id, cols, rows }
 *   → { type: 'kill',   id }
 *   ← { type: 'data',   id, data }                     输出流（UTF-8 裸字节）
 *   ← { type: 'exit',   id, exitCode }                 会话结束
 *   ← { type: 'ready' }                                宿主就绪（握手）
 *
 * ★ 为什么终端不走后端的沙箱安全层
 *
 * 这个终端是**用户自己的**——每一击都是用户亲手敲的，与用户另开一个
 * PowerShell 没有任何区别（对照：agent 调工具执行命令才需要 HITL+黑名单，
 * 因为发起者是模型）。但 **cwd 遵循同一约定**：落在沙箱工作目录
 * （backend/.env 的 FILE_SANDBOX_DIR），让「终端里做的事」与「agent 看到的
 * 文件」在同一片目录上，产物可以互相衔接。env 用与后端 shell 工具同构的
 * 白名单（不透传业务密钥）——桌面壳的窗口渲染模型产出，攻击面比本地终端大，
 * 密钥不该进入它的可达范围。
 */

const os = require('os');
const path = require('path');
const fs = require('fs');
const readline = require('readline');

let pty;
try {
  pty = require('node-pty');
} catch (e) {
  process.stdout.write(JSON.stringify({ type: 'fatal', error: `node-pty 加载失败: ${e.message}` }) + '\n');
  process.exit(1);
}

const sessions = new Map(); // id → IPty
const sessionDetails = new Map();

// ---------- env 白名单（与 backend shell.py 的取舍同构，JS 侧的实现） ----------
const ENV_ALLOWLIST = new Set([
  // Windows
  'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP',
  'USERPROFILE', 'USERNAME', 'USERDOMAIN', 'PROCESSOR_ARCHITECTURE',
  'NUMBER_OF_PROCESSORS', 'PROGRAMFILES', 'PROGRAMDATA', 'APPDATA', 'LOCALAPPDATA',
  // POSIX
  'HOME', 'LANG', 'LC_ALL', 'TERM', 'TZ', 'SHELL',
  // 通用
  'PATH', 'NODE_OPTIONS',
]);
const ENV_DENY_RE = /(API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)/i;

function safeEnv() {
  const env = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (!ENV_ALLOWLIST.has(key.toUpperCase())) continue;
    if (ENV_DENY_RE.test(key)) continue;
    env[key] = value;
  }
  return env;
}

/** 从 backend/.env 读沙箱目录（相对路径按 backend 根解析），解析不了退回 os.homedir() */
function resolveCwd(backendDir) {
  try {
    const envText = fs.readFileSync(path.join(backendDir, '.env'), 'utf8');
    const m = envText.match(/^FILE_SANDBOX_DIR\s*=\s*(.+)$/m);
    if (m) {
      const raw = m[1].trim().replace(/^["']|["']$/g, '');
      const p = path.isAbsolute(raw) ? raw : path.join(backendDir, raw);
      fs.mkdirSync(p, { recursive: true });
      return p;
    }
  } catch {
    /* .env 不存在/读不了 → 退回默认 */
  }
  const fallback = path.join(backendDir, 'data', 'workspace');
  try {
    fs.mkdirSync(fallback, { recursive: true });
    return fallback;
  } catch {
    return os.homedir();
  }
}

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function createSession({ id, cwd, cols = 80, rows = 24, backendDir, shellProfile }) {
  if (sessions.has(id)) { send({ type: 'created', id, ...sessionDetails.get(id) }); return; }
  const shell = process.platform === 'win32'
    ? shellProfile === 'cmd' ? process.env.COMSPEC || 'cmd.exe' : 'powershell.exe'
    : process.env.SHELL || '/bin/bash';
  const effectiveCwd = cwd || resolveCwd(backendDir);

  const ptyProc = pty.spawn(shell, process.platform === 'win32' && shellProfile !== 'cmd' ? ['-NoLogo'] : [], {
    name: 'xterm-256color',
    cols,
    rows,
    cwd: effectiveCwd,
    env: safeEnv(),
  });

  sessions.set(id, ptyProc);
  sessionDetails.set(id, { cwd: effectiveCwd, shell: path.basename(shell) });
  send({ type: 'created', id, cwd: effectiveCwd, shell: path.basename(shell) });
  ptyProc.onData((data) => send({ type: 'data', id, data }));
  ptyProc.onExit(({ exitCode }) => {
    if (sessions.get(id) === ptyProc) { sessions.delete(id); sessionDetails.delete(id); }
    send({ type: 'exit', id, exitCode });
  });
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on('line', (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  let msg;
  try {
    msg = JSON.parse(trimmed);
  } catch {
    return; // 坏行忽略 —— 协议按行分帧，一行坏不影响其余
  }
  try {
    switch (msg.type) {
      case 'create':
        createSession(msg);
        break;
      case 'input':
        sessions.get(msg.id)?.write(String(msg.data ?? ''));
        break;
      case 'resize':
        sessions.get(msg.id)?.resize(Number(msg.cols) || 80, Number(msg.rows) || 24);
        break;
      case 'kill': {
        const s = sessions.get(msg.id);
        if (s) {
          sessions.delete(msg.id);
          sessionDetails.delete(msg.id);
          s.kill();
        }
        break;
      }
      default:
        break;
    }
  } catch (e) {
    send({ type: 'error', id: msg.id, error: e.message });
  }
});

rl.on('close', () => {
  // 主进程退出 → 杀光会话，不留孤儿 shell
  for (const s of sessions.values()) {
    try {
      s.kill();
    } catch {
      /* 已经死了 */
    }
  }
  process.exit(0);
});

// stdin 结束不一定立刻触发 close 的场景兜底：主进程死了就自杀
setTimeout(() => {}, 1 << 30);
process.stdout.write(JSON.stringify({ type: 'ready' }) + '\n');
