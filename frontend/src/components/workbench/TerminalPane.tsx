import { useEffect, useRef, useState } from 'react';
import type { Terminal } from '@xterm/xterm';
import type { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import { Check, Copy, Loader2, Plus, RefreshCw, Square, Terminal as TerminalIcon, Trash, TriangleAlert, X } from '@/components/icons';
import { useThemeStore } from '@/hooks/useTheme';
import { toast } from '@/stores/toastStore';
import { useWorkbenchStore, type WorkbenchPreferences } from '@/stores/workbenchStore';
import { isDesktopShell } from '@/types/desktop';

function terminalTheme(dark: boolean) {
  const style = getComputedStyle(document.documentElement);
  const color = (name: string) => style.getPropertyValue(name).trim();
  const light = !dark && document.documentElement.dataset.colorScheme === 'light';
  return {
    background: dark ? '#15171c' : color('--panel'), foreground: dark ? '#dce0e8' : color('--text'),
    cursor: dark ? '#dce0e8' : color('--accent'), selectionBackground: dark ? '#414753' : color('--accent-border'),
    black: '#525663', red: light ? '#b83243' : '#e06c75', green: light ? '#287c42' : '#6eaf7c', yellow: light ? '#926a15' : '#d8b568',
    blue: light ? '#365ea7' : '#7b9fd4', magenta: light ? '#8b419e' : '#c397d8', cyan: light ? '#23727b' : '#66b6bf', white: light ? '#5a5f6a' : '#dce0e8',
  };
}

export function TerminalPane({ visible }: { visible: boolean }) {
  const [sessions, setSessions] = useState(() => [{ id: crypto.randomUUID(), number: 1 }]);
  const [active, setActive] = useState(sessions[0].id);
  const counter = useRef(1);
  const add = () => {
    const next = { id: crypto.randomUUID(), number: ++counter.current };
    setSessions(items => [...items, next]); setActive(next.id);
  };
  const close = (id: string) => {
    const index = sessions.findIndex(item => item.id === id);
    const remaining = sessions.filter(item => item.id !== id);
    setSessions(remaining);
    if (active === id) setActive(remaining[Math.min(index, remaining.length - 1)]?.id || '');
  };

  if (!isDesktopShell()) return <div className="wb-empty"><div className="wb-empty__icon"><TerminalIcon size={26} /></div><span className="wb-eyebrow">TERMINAL</span><h3>在桌面版使用终端</h3><p>桌面版提供完整交互式终端，可以运行命令、启动服务和保留会话。</p></div>;

  return <div className="wb-terminal">
    <div className="wb-sessions" aria-label="终端会话">
      <div className="wb-sessions__list" role="tablist" aria-label="终端会话">
        {sessions.map(item => <div className={`wb-session${active === item.id ? ' is-active' : ''}`} key={item.id}>
          <button type="button" role="tab" id={`terminal-tab-${item.id}`} aria-controls={`terminal-${item.id}`} aria-selected={active === item.id} onClick={() => setActive(item.id)}><TerminalIcon size={12} /><span>终端 {item.number}</span></button>
          <button type="button" className="wb-session__close" title="结束此会话" aria-label={`结束终端 ${item.number}`} onClick={() => close(item.id)}><X size={11} /></button>
        </div>)}
      </div>
      <button type="button" className="icon-btn" onClick={add} title="新建终端" aria-label="新建终端"><Plus size={14} /></button>
    </div>
    {sessions.length === 0 && <div className="wb-empty"><div className="wb-empty__icon"><TerminalIcon size={26} /></div><h3>开始一个新的终端会话</h3><p>每个会话独立运行，收起面板后仍会继续。</p><button type="button" className="btn btn--sm" onClick={add}><Plus size={13} />新建终端</button></div>}
    {sessions.map(item => <div className="wb-terminal__session" key={item.id} role="tabpanel" id={`terminal-${item.id}`} aria-labelledby={`terminal-tab-${item.id}`} hidden={active !== item.id}>
      <TerminalSession visible={visible && active === item.id} />
    </div>)}
  </div>;
}

function TerminalSession({ visible }: { visible: boolean }) {
  const prefs = useWorkbenchStore(s => s.preferences);
  const theme = useThemeStore(s => s.theme);
  const host = useRef<HTMLDivElement>(null);
  const terminal = useRef<Terminal | null>(null);
  const fit = useRef<FitAddon | null>(null);
  const sessionId = useRef('');
  const [generation, setGeneration] = useState(0);
  const [status, setStatus] = useState<'starting' | 'ready' | 'exited' | 'error'>('starting');
  const [info, setInfo] = useState({ cwd: '', shell: '' });
  const [error, setError] = useState('');
  const [selection, setSelection] = useState(false);
  const [copied, setCopied] = useState(false);
  const dark = prefs.terminalTheme === 'dark';
  const options = (preferences: WorkbenchPreferences) => ({
    fontSize: preferences.terminalFontSize,
    fontFamily: getComputedStyle(document.documentElement).getPropertyValue('--mono').trim() || 'Consolas, monospace',
    scrollback: preferences.terminalScrollback, cursorStyle: preferences.terminalCursor,
    cursorBlink: preferences.terminalBlink, theme: terminalTheme(preferences.terminalTheme === 'dark'),
  });

  useEffect(() => {
    const bridge = window.xjmsDesktop?.terminal;
    if (!bridge || !host.current) return;
    const mount = host.current;
    const id = `workbench-${crypto.randomUUID()}`;
    sessionId.current = id;
    let cancelled = false;
    let term: Terminal | null = null;
    let observer: ResizeObserver | null = null;
    let frame = 0;
    const disposers: Array<() => void> = [];
    setStatus('starting'); setError(''); setSelection(false); setInfo({ cwd: '', shell: '' });
    void (async () => {
      try {
        const [{ Terminal }, { FitAddon }] = await Promise.all([import('@xterm/xterm'), import('@xterm/addon-fit')]);
        if (cancelled) return;
        const preferences = useWorkbenchStore.getState().preferences;
        term = new Terminal({ ...options(preferences), allowProposedApi: false });
        const fitter = new FitAddon();
        terminal.current = term; fit.current = fitter;
        term.loadAddon(fitter); term.open(mount);
        const resize = () => {
          if (cancelled || mount.clientWidth < 20 || mount.clientHeight < 20) return;
          fitter.fit(); bridge.resize(id, term!.cols, term!.rows);
        };
        observer = new ResizeObserver(() => { cancelAnimationFrame(frame); frame = requestAnimationFrame(resize); });
        observer.observe(mount);
        const data = term.onData(value => bridge.input(id, value));
        const selectionChange = term.onSelectionChange(() => setSelection(Boolean(term?.hasSelection())));
        disposers.push(() => data.dispose(), () => selectionChange.dispose());
        disposers.push(bridge.onSessionData(payload => { if (payload.id === id) term?.write(payload.data); }));
        disposers.push(bridge.onExit(payload => {
          if (cancelled || (payload.id !== id && payload.id !== '*')) return;
          term?.write(`\r\n\x1b[90m[${payload.id === '*' ? '终端连接已断开' : `会话已结束，退出码 ${payload.exitCode}`}，可点击重新连接]\x1b[0m\r\n`);
          setStatus('exited');
        }));
        term.attachCustomKeyEventHandler(event => {
          if (event.type !== 'keydown' || !event.ctrlKey || !event.shiftKey) return true;
          if (event.code === 'KeyC' && term?.hasSelection()) {
            void navigator.clipboard.writeText(term.getSelection()).catch(() => toast('复制失败，请重试', 'error'));
            return false;
          }
          if (event.code === 'KeyV') {
            void navigator.clipboard.readText().then(text => { if (!cancelled) term?.paste(text); }).catch(() => toast('无法读取剪贴板，请使用系统粘贴', 'error'));
            return false;
          }
          return true;
        });
        resize();
        const created = await bridge.create(id, term.cols, term.rows, { shell: preferences.terminalShell });
        if (cancelled) { bridge.kill(id); return; }
        if (!created) throw new Error('终端未能启动，请检查系统终端和 Node.js 是否可用。');
        setInfo(created); setStatus(current => current === 'starting' ? 'ready' : current);
        resize();
      } catch (failure) {
        if (!cancelled) { setError(failure instanceof Error ? failure.message : String(failure)); setStatus('error'); }
      }
    })();
    return () => {
      cancelled = true; cancelAnimationFrame(frame); observer?.disconnect();
      disposers.forEach(dispose => dispose()); bridge.kill(id);
      term?.dispose(); mount.replaceChildren(); terminal.current = null; fit.current = null;
    };
    // Every restart gets its own ID, including React StrictMode's effect replay.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [generation]);

  useEffect(() => {
    if (terminal.current) Object.assign(terminal.current.options, options(prefs));
    if (visible && host.current?.clientWidth) {
      fit.current?.fit();
      if (terminal.current) window.xjmsDesktop?.terminal.resize(sessionId.current, terminal.current.cols, terminal.current.rows);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefs, theme, visible, status]);
  useEffect(() => { if (visible && status === 'ready') terminal.current?.focus(); }, [visible, status]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(terminal.current?.getSelection() || '');
      setCopied(true); window.setTimeout(() => setCopied(false), 1500);
    } catch { toast('复制失败，请重试', 'error'); }
  };

  return <div className={`wb-terminal__inner${dark ? ' wb-terminal__inner--dark' : ''}`}>
    <div className="wb-terminal__toolbar">
      <span className="wb-terminal__profile"><TerminalIcon size={13} />{info.shell || '本地终端'}</span>
      <div className="wb-terminal__actions">
        <button type="button" className="icon-btn" disabled={!selection} title="复制选中内容 · Ctrl+Shift+C" aria-label="复制选中内容" onClick={() => void copy()}>{copied ? <Check size={13} /> : <Copy size={13} />}</button>
        <button type="button" className="icon-btn" disabled={status !== 'ready'} title="中断当前命令 · Ctrl+C" aria-label="中断当前命令" onClick={() => { window.xjmsDesktop?.terminal.input(sessionId.current, '\x03'); terminal.current?.focus(); }}><Square size={10} /></button>
        <button type="button" className="icon-btn" title="清空终端显示" aria-label="清空终端显示" disabled={status === 'starting'} onClick={() => { terminal.current?.clear(); terminal.current?.focus(); }}><Trash size={13} /></button>
        {(status === 'exited' || status === 'error') && <button type="button" className="btn btn--sm btn--ghost" onClick={() => setGeneration(n => n + 1)}><RefreshCw size={12} />重新连接</button>}
      </div>
    </div>
    <div className="wb-terminal__screen">
      <div className="wb-terminal__mount" ref={host} />
      {status === 'starting' && <div className="wb-terminal__notice"><Loader2 size={17} className="spin" /><span>正在连接本地终端…</span></div>}
      {status === 'error' && <div className="wb-terminal__notice" role="alert"><TriangleAlert size={22} /><p>{error}</p></div>}
    </div>
    <footer className="wb-status"><span className={`wb-status__dot${status === 'error' || status === 'exited' ? ' is-error' : status === 'starting' ? ' is-busy' : ''}`} />{({ starting: '连接中', ready: '已连接', exited: '已结束', error: '连接失败' })[status]}<span className="wb-status__path" title={info.cwd}>{info.cwd}</span><span className="wb-status__right">{prefs.terminalFontSize}px</span></footer>
  </div>;
}
