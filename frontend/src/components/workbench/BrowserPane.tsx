import { createElement, useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, ChevronRight, ExternalLink, Globe, Home, Plus, RefreshCw, Square, TriangleAlert, X } from '@/components/icons';
import { normalizeBrowserUrl } from '@/lib/browser';
import { useSidePanelStore } from '@/stores/artifactStore';
import { useWorkbenchStore } from '@/stores/workbenchStore';
import { isDesktopShell } from '@/types/desktop';

interface Webview extends HTMLElement {
  goBack: () => void;
  goForward: () => void;
  reload: () => void;
  stop: () => void;
  getURL: () => string;
  canGoBack: () => boolean;
  canGoForward: () => boolean;
  setZoomFactor: (zoom: number) => void;
}
interface BrowserTab { id: string; url: string; title: string }
const blankTab = (url = ''): BrowserTab => ({ id: crypto.randomUUID(), url, title: '' });

export function BrowserPane({ visible }: { visible: boolean }) {
  const preferences = useWorkbenchStore(s => s.preferences);
  const browserUrl = useSidePanelStore(s => s.browserUrl);
  const browserNonce = useSidePanelStore(s => s.browserNonce);
  const [tabs, setTabs] = useState<BrowserTab[]>(() => [blankTab(normalizeBrowserUrl(preferences.homepage) || '')]);
  const [activeId, setActiveId] = useState('');
  const selected = tabs.find(tab => tab.id === activeId) || tabs[0];
  const selectedRef = useRef(selected.id);
  selectedRef.current = selected.id;
  const [focusNonce, setFocusNonce] = useState(0);
  const [request, setRequest] = useState<{ id: string; url: string; nonce: number } | null>(null);

  useEffect(() => {
    if (browserUrl) setRequest({ id: selectedRef.current, url: browserUrl, nonce: browserNonce });
  }, [browserUrl, browserNonce]);
  useEffect(() => window.xjmsDesktop?.browser?.onFocusAddress(() => {
    if (useSidePanelStore.getState().panelOpen && useSidePanelStore.getState().activeTab === 'browser') setFocusNonce(n => n + 1);
  }), []);

  const update = useCallback((id: string, patch: Partial<BrowserTab>) => {
    setTabs(tabs => tabs.map(tab => tab.id === id ? { ...tab, ...patch } : tab));
  }, []);
  const add = () => {
    const next = blankTab(normalizeBrowserUrl(preferences.homepage) || '');
    setTabs(tabs => [...tabs, next]); setActiveId(next.id); setFocusNonce(n => n + 1);
  };
  const close = (id: string) => {
    const index = tabs.findIndex(tab => tab.id === id);
    const remaining = tabs.filter(tab => tab.id !== id);
    if (!remaining.length) remaining.push(blankTab());
    setTabs(remaining);
    if (selected.id === id) setActiveId(remaining[Math.min(index, remaining.length - 1)].id);
  };

  return (
    <div className="wb-browser">
      <div className="wb-sessions" aria-label="网页标签">
        <div className="wb-sessions__list" role="tablist" aria-label="网页">
          {tabs.map(tab => (
            <div className={`wb-session${tab.id === selected.id ? ' is-active' : ''}`} key={tab.id}>
              <button type="button" role="tab" aria-selected={tab.id === selected.id} aria-controls={`browser-${tab.id}`} id={`browser-tab-${tab.id}`} onClick={() => setActiveId(tab.id)} title={tab.url || '新标签页'}>
                <Globe size={12} /><span>{tab.title || (tab.url ? new URL(tab.url).host : '新标签页')}</span>
              </button>
              <button type="button" className="wb-session__close" aria-label={`关闭 ${tab.title || '网页'}`} onClick={() => close(tab.id)}><X size={11} /></button>
            </div>
          ))}
        </div>
        <button type="button" className="icon-btn" onClick={add} title="新建网页标签" aria-label="新建网页标签"><Plus size={14} /></button>
      </div>
      {tabs.map(tab => (
        <div className="wb-browser__page" key={tab.id} role="tabpanel" id={`browser-${tab.id}`} aria-labelledby={`browser-tab-${tab.id}`} hidden={tab.id !== selected.id}>
          <BrowserPage tab={tab} update={update} visible={visible && tab.id === selected.id} focusNonce={tab.id === selected.id ? focusNonce : 0} request={request?.id === tab.id ? request : null} />
        </div>
      ))}
    </div>
  );
}

function BrowserPage({ tab, update, visible, focusNonce, request }: {
  tab: BrowserTab;
  update: (id: string, patch: Partial<BrowserTab>) => void;
  visible: boolean;
  focusNonce: number;
  request: { url: string; nonce: number } | null;
}) {
  const prefs = useWorkbenchStore(s => s.preferences);
  const desktop = isDesktopShell();
  const address = useRef<HTMLInputElement>(null);
  const view = useRef<Webview | null>(null);
  const initialUrl = useRef(tab.url);
  const lastRequest = useRef<number | null>(null);
  const ready = useRef(false);
  const pendingUrl = useRef('');
  const [src, setSrc] = useState(tab.url);
  const [input, setInput] = useState(tab.url);
  const [loading, setLoading] = useState(Boolean(tab.url));
  const [back, setBack] = useState(false);
  const [forward, setForward] = useState(false);
  const [error, setError] = useState('');
  const [inputError, setInputError] = useState('');

  const navigate = useCallback((raw: string) => {
    const url = normalizeBrowserUrl(raw);
    if (!url) { setInputError('请输入有效的网址，例如 https://example.com 或 localhost:3000'); return; }
    setInputError(''); setError(''); setInput(url);
    update(tab.id, { url });
    if (!desktop) { window.open(url, '_blank', 'noopener,noreferrer'); return; }
    if (!src) { initialUrl.current = url; setSrc(url); }
    else if (ready.current && view.current) { view.current.setAttribute('src', url); }
    else pendingUrl.current = url;
  }, [desktop, src, tab.id, update]);

  useEffect(() => {
    if (request && lastRequest.current !== request.nonce) {
      const initialNavigation = lastRequest.current === null && src && normalizeBrowserUrl(request.url) === initialUrl.current;
      lastRequest.current = request.nonce;
      if (!initialNavigation) navigate(request.url);
    }
  }, [request, navigate, src]);
  useEffect(() => {
    if (visible && focusNonce) { address.current?.focus(); address.current?.select(); }
  }, [focusNonce, visible]);

  // Electron emits native DOM events, not React's synthetic onDidNavigate events.
  // Keep src stable in React: changing it on redirects/back would add history entries.
  useEffect(() => {
    const guest = view.current;
    if (!guest || !src || !desktop) return;
    const sync = () => {
      const url = guest.getURL();
      if (normalizeBrowserUrl(url)) { setInput(url); update(tab.id, { url }); }
      setBack(guest.canGoBack()); setForward(guest.canGoForward());
    };
    const onReady = () => {
      ready.current = true;
      guest.setZoomFactor(useWorkbenchStore.getState().preferences.browserZoom / 100);
      if (pendingUrl.current) { guest.setAttribute('src', pendingUrl.current); pendingUrl.current = ''; }
      else sync();
    };
    const onNavigate = () => { sync(); setError(''); };
    const onStart = () => { setLoading(true); setError(''); };
    const onStop = () => { setLoading(false); if (ready.current) sync(); };
    const onTitle = (event: Event) => update(tab.id, { title: (event as Event & { title: string }).title });
    const onError = (event: Event) => {
      const failure = event as Event & { isMainFrame: boolean; errorCode: number; errorDescription: string };
      if (!failure.isMainFrame || failure.errorCode === -3) return;
      setLoading(false); setError(failure.errorDescription || `加载失败 (${failure.errorCode})`);
    };
    const listeners: Array<[string, EventListener]> = [
      ['dom-ready', onReady], ['did-navigate', onNavigate], ['did-navigate-in-page', onNavigate],
      ['did-start-loading', onStart], ['did-stop-loading', onStop], ['page-title-updated', onTitle], ['did-fail-load', onError],
    ];
    listeners.forEach(([name, listener]) => guest.addEventListener(name, listener));
    return () => { listeners.forEach(([name, listener]) => guest.removeEventListener(name, listener)); ready.current = false; };
  }, [src, desktop, tab.id, update]);
  useEffect(() => { if (ready.current) view.current?.setZoomFactor(prefs.browserZoom / 100); }, [prefs.browserZoom]);

  return (
    <div className="wb-browser__inner" onKeyDown={event => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'l') {
        event.preventDefault(); address.current?.focus(); address.current?.select();
      }
    }}>
      <form className="wb-browser__toolbar" onSubmit={event => { event.preventDefault(); navigate(input); }}>
        <div className="wb-browser__nav">
          <button type="button" className="icon-btn" title="后退" aria-label="后退" disabled={!back} onClick={() => view.current?.goBack()}><ArrowLeft size={14} /></button>
          <button type="button" className="icon-btn" title="前进" aria-label="前进" disabled={!forward} onClick={() => view.current?.goForward()}><ChevronRight size={15} /></button>
          <button type="button" className="icon-btn" title={loading ? '停止加载' : '刷新'} aria-label={loading ? '停止加载' : '刷新'} disabled={!src || !desktop} onClick={() => loading ? view.current?.stop() : view.current?.reload()}>{loading ? <Square size={10} /> : <RefreshCw size={13} />}</button>
        </div>
        <div className={`wb-address${inputError ? ' has-error' : ''}`}>
          <Globe size={13} />
          <input ref={address} value={input} onChange={event => { setInput(event.target.value); setInputError(''); }} onFocus={event => event.target.select()} placeholder="输入网址或 localhost 地址" aria-label="网页地址" aria-invalid={Boolean(inputError)} spellCheck={false} />
          <button type="submit" title="前往" aria-label="前往"><ChevronRight size={14} /></button>
        </div>
        {prefs.homepage && <button type="button" className="icon-btn" title="打开首页" aria-label="打开首页" onClick={() => navigate(prefs.homepage)}><Home size={14} /></button>}
        <button type="button" className="icon-btn" title="在系统浏览器打开" aria-label="在系统浏览器打开" disabled={!normalizeBrowserUrl(tab.url)} onClick={() => window.open(tab.url, '_blank', 'noopener,noreferrer')}><ExternalLink size={14} /></button>
      </form>
      {inputError && <div className="wb-inline-error" role="alert">{inputError}</div>}
      <div className={`wb-browser__progress${loading && desktop ? ' is-loading' : ''}`} />
      <div className="wb-browser__viewport">
        {src && desktop ? createElement('webview', {
          ref: view, className: 'wb-browser__webview', src: initialUrl.current,
          partition: 'persist:xjms-browser', allowpopups: 'true', webpreferences: 'contextIsolation=yes, sandbox=yes',
        }) : (
          <div className="wb-empty">
            <div className="wb-empty__icon"><Globe size={26} strokeWidth={1.5} /></div>
            <span className="wb-eyebrow">BROWSER</span>
            <h3>{desktop ? '把网页留在对话旁' : '从这里打开网页'}</h3>
            <p>{desktop ? '查阅来源、查看本地预览，切换面板也能接着浏览。' : '网页版将在新标签页打开链接。桌面版支持内嵌浏览。'}</p>
            <button type="button" className="btn btn--sm" onClick={() => address.current?.focus()}><Plus size={13} />打开一个网页</button>
            <span className="wb-empty__hint">支持网站地址与本地服务地址</span>
          </div>
        )}
        {error && desktop && <div className="wb-browser__failure" role="alert">
          <TriangleAlert size={24} /><h3>这个网页暂时无法打开</h3><p>{error}</p>
          <div><button type="button" className="btn btn--sm" onClick={() => view.current?.reload()}><RefreshCw size={13} />重新加载</button><button type="button" className="btn btn--sm btn--ghost" onClick={() => window.open(tab.url, '_blank', 'noopener,noreferrer')}><ExternalLink size={13} />系统浏览器打开</button></div>
        </div>}
      </div>
      <footer className="wb-status"><span className={`wb-status__dot${error ? ' is-error' : loading ? ' is-busy' : ''}`} />{error ? '加载失败' : loading && desktop ? '正在加载…' : desktop ? '内置浏览器' : '系统浏览器'}<span className="wb-status__right">{prefs.browserZoom}%</span></footer>
    </div>
  );
}
