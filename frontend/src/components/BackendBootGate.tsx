/**
 * 桌面端「后端启动门」—— 后端还没就绪时先转圈，而不是让界面空转报错
 *
 * 桌面壳现在「窗口先行、后端后台拉起」（见 desktop/main.js）。窗口打开时后端
 * 可能还在冷启动（导入依赖 + 起检索栈），此时直接渲染会看到一串连不上的错误态。
 * 这里在桌面端把首屏挡住：HTTP 轮询 /api/info，直到 200 才放行真正的界面。
 *
 * ★ 为什么走 HTTP 轮询而不是主进程 IPC 事件
 *
 * preload 有一条明确约束：故意不放 backend 桥，前后端只走 HTTP（桥越小，安全面
 * 越小，见 desktop/preload.cjs）。就绪信号本就是「后端能不能答 /api/info」，
 * 前端自己轮询即可，不必为它撑大桥面。
 *
 * ★ 为什么只在桌面端生效
 *
 * Web 模式下后端要么在、要么是真挂了 —— 用「启动中」遮罩把一次性的连接失败
 * 伪装成慢启动只会误导。所以非桌面端直接放行，行为与改动前完全一致。
 */

import { useEffect, useRef, useState } from 'react';

import { Loader2, TriangleAlert } from '@/components/icons';
import { API_BASE } from '@/lib/api';
import { isDesktopShell } from '@/types/desktop';

type Phase = 'checking' | 'ready' | 'failed';

/** 探测间隔：与主进程 waitForBackend 的节奏同量级，够快又不刷屏 */
const POLL_MS = 500;
/** 放弃阈值：比主进程给后端的 90s 冷启动预算再多留 10s 余量 */
const GIVE_UP_MS = 100_000;
const PROBE_TIMEOUT_MS = 3_000;

export function BackendBootGate({ children }: { children: React.ReactNode }) {
  const desktop = isDesktopShell();
  // 非桌面端初始即 ready：不轮询、不挡屏，渲染路径与改动前一致
  const [phase, setPhase] = useState<Phase>(desktop ? 'checking' : 'ready');
  const [elapsed, setElapsed] = useState(0);
  const startedAt = useRef(Date.now());

  useEffect(() => {
    if (!desktop || phase !== 'checking') return;
    let alive = true;
    let timer = 0;
    let probe: AbortController | null = null;

    const tick = async () => {
      if (!alive) return;
      setElapsed(Date.now() - startedAt.current);
      probe = new AbortController();
      const timeout = window.setTimeout(() => probe?.abort(), PROBE_TIMEOUT_MS);
      try {
        const res = await fetch(`${API_BASE}/info`, { cache: 'no-store', signal: probe.signal });
        if (res.ok) {
          if (alive) setPhase('ready');
          return;
        }
      } catch {
        /* 后端还没起：连接被拒是预期内的，继续下一轮 */
      } finally {
        window.clearTimeout(timeout);
      }
      if (!alive) return;
      if (Date.now() - startedAt.current > GIVE_UP_MS) {
        setPhase('failed');
        return;
      }
      timer = window.setTimeout(tick, POLL_MS);
    };
    void tick();

    return () => {
      alive = false;
      window.clearTimeout(timer);
      probe?.abort();
    };
  }, [desktop, phase]);

  if (phase === 'ready') return <>{children}</>;

  const secs = Math.floor(elapsed / 1000);

  if (phase === 'checking') {
    return (
      <div className="boot-gate" role="status" aria-live="polite">
        <Loader2 size={30} className="spin boot-gate__spinner" />
        <div className="boot-gate__title">正在启动本地服务</div>
        <p className="boot-gate__hint">
          首次启动需要加载模型与检索组件，通常十几秒，请稍候…
          {secs > 0 && <span className="boot-gate__secs">（{secs}s）</span>}
        </p>
      </div>
    );
  }

  return (
    <div className="boot-gate" role="alert">
      <TriangleAlert size={28} className="boot-gate__error-icon" />
      <div className="boot-gate__title">后端服务未能就绪</div>
      <p className="boot-gate__hint">
        本地服务在 {Math.floor(GIVE_UP_MS / 1000)} 秒内没有响应。
        可能是 Python 依赖未安装，或端口已被其它程序占用，详见应用日志。
      </p>
      <button
        type="button"
        className="btn btn--primary"
        onClick={() => {
          startedAt.current = Date.now();
          setElapsed(0);
          setPhase('checking');
        }}
      >
        重试
      </button>
    </div>
  );
}
