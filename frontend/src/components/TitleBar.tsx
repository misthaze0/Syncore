/**
 * 自绘标题栏 —— 仅 Windows 桌面壳（hasCustomTitleBar()）渲染。
 *
 * 结构：后退 / 前进 / 侧栏开关 ＋ 文件·编辑·视图·帮助 四组菜单 ＋ 拖拽区。
 * 窗口三键（最小化 / 最大化 / 关闭）由系统 titleBarOverlay 绘制在右端，
 * 配色由这里在主题切换时同步过去（windowBar.setOverlay）。
 *
 * ★ 为什么整条栏都是拖拽区
 *
 * titleBarStyle 'hidden' 之后窗口没有可拖的标题栏，-webkit-app-region: drag
 * 落在这条栏上；交互控件显式标 no-drag，否则点不到。双击空白处切换最大化
 * 是 drag 区的系统能力，不用自己实现。
 */

import { useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate, useNavigationType } from 'react-router-dom';

import { ArrowLeft, ArrowRight } from '@/components/icons';
import { useT } from '@/i18n';
import { THEME_LABELS, nextTheme } from '@/lib/theme';
import { useAgentStore } from '@/stores/agentStore';
import { useUiStore } from '@/stores/uiStore';
import { useTheme } from '@/hooks/useTheme';

interface MenuItem {
  label: string;
  hint?: string;
  onSelect: () => void;
}

/** 读 computed style 里的 rgb()/rgba() 颜色，转成 overlay 要求的 #RRGGBB */
function rgbToHex(value: string): string | null {
  const m = value.match(/rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/);
  if (!m) return null;
  return `#${[m[1], m[2], m[3]].map((n) => Number(n).toString(16).padStart(2, '0')).join('')}`;
}

/** 浏览器会话栈的镜像：只为本组件的「后退 / 前进」按钮提供可用态 */
function useHistoryDepth(pathname: string) {
  const navType = useNavigationType();
  const stack = useRef<{ list: string[]; idx: number }>({ list: [pathname], idx: 0 });
  const [, bump] = useState(0);
  useEffect(() => {
    const s = stack.current;
    if (s.list[s.idx] === pathname) return;
    if (s.idx > 0 && s.list[s.idx - 1] === pathname) s.idx -= 1;
    else if (s.idx < s.list.length - 1 && s.list[s.idx + 1] === pathname) s.idx += 1;
    else {
      s.list = [...s.list.slice(0, s.idx + 1), pathname];
      s.idx += 1;
    }
    bump((n) => n + 1);
  }, [pathname, navType]);
  return { canBack: stack.current.idx > 0, canForward: stack.current.idx < stack.current.list.length - 1 };
}

export function TitleBar() {
  const t = useT();
  const navigate = useNavigate();
  const location = useLocation();
  const collapsed = useUiStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useUiStore((s) => s.toggleSidebar);
  const { theme, toggleTheme } = useTheme();
  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  /** 设置弹窗开着时 location 已是 /settings，来源页垫在下面 —— 与侧栏同一套取舍 */
  const background = (location.state as { background?: { pathname: string } } | null)?.background;
  const currentPath = background?.pathname ?? location.pathname;
  const office = currentPath.startsWith('/office');
  const toggleNavigation = () => {
    if (office) window.dispatchEvent(new CustomEvent('xjms:office-library-toggle'));
    else toggleSidebar();
  };
  const { canBack, canForward } = useHistoryDepth(location.pathname);

  const openSettings = (tab?: string) => {
    if (currentPath.startsWith('/settings')) return;
    navigate(tab ? `/settings/${tab}` : '/settings', { state: { background: location } });
  };

  // 与侧栏「新建会话」同一套行为：助理 scope 下先带回对话页（见 Sidebar 注释）
  const newSession = () => {
    if (currentPath.startsWith('/office')) {
      window.dispatchEvent(new CustomEvent('xjms:office-new'));
      return;
    }
    if (useAgentStore.getState().scope === 'assistant') {
      navigate('/chat');
      return;
    }
    useAgentStore.getState().newSession();
    navigate('/chat');
  };

  // 点击栏外 / Esc 收起菜单
  useEffect(() => {
    if (!openMenu) return;
    const onDown = (e: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpenMenu(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpenMenu(null);
    };
    document.addEventListener('pointerdown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [openMenu]);

  // Ctrl + / - / 0 缩放（Electron 不带系统菜单后没有这些加速键，这里自己接上）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const bar = window.xjmsDesktop?.windowBar;
      if (!bar || !(e.ctrlKey || e.metaKey) || e.altKey) return;
      if (e.key === '=' || e.key === '+') {
        e.preventDefault();
        bar.zoomStep(0.1);
      } else if (e.key === '-') {
        e.preventDefault();
        bar.zoomStep(-0.1);
      } else if (e.key === '0') {
        e.preventDefault();
        bar.zoomReset();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // 主题切换时把 overlay 配色同步成标题栏同款（读自己的 computed style，不用维护色表）
  useEffect(() => {
    const bar = window.xjmsDesktop?.windowBar;
    if (!bar?.setOverlay || !rootRef.current) return;
    const sync = () => {
      const cs = getComputedStyle(rootRef.current!);
      const color = rgbToHex(cs.backgroundColor);
      const symbolColor = rgbToHex(cs.color);
      if (color && symbolColor) void bar.setOverlay({ color, symbolColor });
    };
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-color-scheme'] });
    return () => observer.disconnect();
  }, []);

  const fileItems: MenuItem[] = [
    { label: currentPath.startsWith('/office') ? '新建文档' : t('sidebar.newSession'), hint: 'Ctrl N', onSelect: newSession },
    { label: t('common.settings'), onSelect: () => openSettings() },
    { label: t('tbar.quit'), onSelect: () => window.xjmsDesktop?.windowBar?.quit() },
  ];
  const editItems: MenuItem[] = [
    { label: t('tbar.search'), hint: 'Ctrl K', onSelect: () => window.dispatchEvent(new CustomEvent('xjms:palette-open')) },
  ];
  const viewItems: MenuItem[] = [
    { label: office ? '切换文档库' : collapsed ? t('sidebar.expand') : t('sidebar.collapse'), onSelect: toggleNavigation },
    { label: t('tbar.themeSwitch', { label: THEME_LABELS[nextTheme(theme)] }), onSelect: toggleTheme },
    { label: t('tbar.zoomIn'), hint: 'Ctrl +', onSelect: () => window.xjmsDesktop?.windowBar?.zoomStep(0.1) },
    { label: t('tbar.zoomOut'), hint: 'Ctrl -', onSelect: () => window.xjmsDesktop?.windowBar?.zoomStep(-0.1) },
    { label: t('tbar.zoomReset'), hint: 'Ctrl 0', onSelect: () => window.xjmsDesktop?.windowBar?.zoomReset() },
    { label: t('tbar.fullscreen'), onSelect: () => void window.xjmsDesktop?.windowBar?.toggleFullscreen() },
  ];
  const helpItems: MenuItem[] = [
    { label: t('tbar.shortcuts'), onSelect: () => openSettings('shortcuts') },
    { label: t('tbar.about'), onSelect: () => openSettings('about') },
  ];
  const menus: Array<{ id: string; label: string; items: MenuItem[] }> = [
    { id: 'file', label: t('tbar.file'), items: fileItems },
    { id: 'edit', label: t('tbar.edit'), items: editItems },
    { id: 'view', label: t('tbar.view'), items: viewItems },
    { id: 'help', label: t('tbar.help'), items: helpItems },
  ];

  return (
    <div className="tbar" ref={rootRef}>
      <div className="tbar__nav">
        <button
          type="button"
          className="tbar__icon-btn"
          disabled={!canBack}
          onClick={() => navigate(-1)}
          title={t('tbar.back')}
          aria-label={t('tbar.back')}
        >
          <ArrowLeft size={15} />
        </button>
        <button
          type="button"
          className="tbar__icon-btn"
          disabled={!canForward}
          onClick={() => navigate(1)}
          title={t('tbar.forward')}
          aria-label={t('tbar.forward')}
        >
          <ArrowRight size={15} />
        </button>
      </div>

      <div className="tbar__menus">
        {menus.map((menu) => (
          <div className="tbar__menu" key={menu.id}>
            <button
              type="button"
              className={`tbar__menu-btn${openMenu === menu.id ? ' tbar__menu-btn--open' : ''}`}
              onClick={() => setOpenMenu(openMenu === menu.id ? null : menu.id)}
            >
              {menu.label}
            </button>
            {openMenu === menu.id && (
              <div className="tbar__dropdown" role="menu" aria-label={menu.label}>
                {menu.items.map((item, i) => (
                  <button
                    key={i}
                    type="button"
                    role="menuitem"
                    className="tbar__item"
                    onClick={() => {
                      setOpenMenu(null);
                      item.onSelect();
                    }}
                  >
                    <span>{item.label}</span>
                    {item.hint && <kbd className="tbar__kbd">{item.hint}</kbd>}
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* 拖拽区：盖住菜单右侧到窗口控制按钮之前的整段 */}
      <div className="tbar__spacer" />
    </div>
  );
}
