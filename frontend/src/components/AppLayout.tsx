/**
 * 应用布局 —— 侧边栏 + 页面出口（Outlet）
 *
 * 侧栏从 ChatPage 提升到这里：它现在是「对话」与「后台任务」两个页面共有的壳。
 * 放在布局层，切页面时侧栏的状态（折叠、搜索词、列表滚动位置）自然保留。
 *
 * ★ 会话列表的数据也必须在这里拉
 *
 * `bootstrap()` 原来在 ChatPage 里调用，壳却已经提到布局层 —— 于是首屏落在
 * /tasks、/workspace 时没人去拉历史，侧栏只剩一句「还没有会话」。
 * 拉数据的时机跟着被它喂的那个组件一起上移，幂等由 store 的 `bootstrapped` 守卫保证。
 */

import { useEffect, useLayoutEffect, useRef } from 'react';
import { Outlet, useLocation } from 'react-router-dom';

import { BackendBootGate } from '@/components/BackendBootGate';
import { Sidebar } from '@/components/Sidebar';
import { TitleBar } from '@/components/TitleBar';
import { AppHeader } from '@/components/AppHeader';
import { useAgentStore } from '@/stores/agentStore';
import { hasCustomTitleBar } from '@/types/desktop';

export function AppLayout() {
  return <BackendBootGate><ReadyAppLayout /></BackendBootGate>;
}

/** 只有服务就绪后才挂载：启动门必须同时挡住数据请求和界面。 */
function ReadyAppLayout() {
  const location = useLocation();
  const bootstrap = useAgentStore((s) => s.bootstrap);
  const titled = hasCustomTitleBar();
  const background = (location.state as { background?: { pathname: string } } | null)?.background;
  const office = (background?.pathname ?? location.pathname).startsWith('/office');
  const contentRef = useRef<HTMLDivElement>(null);
  const path = background?.pathname ?? location.pathname;

  useLayoutEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const surface = contentRef.current?.querySelector(office ? '.office-main' : '.main');
    const animation = surface?.animate([{ opacity: .45, transform: 'translateY(5px)' }, { opacity: 1, transform: 'translateY(0)' }], { duration: 200, easing: 'cubic-bezier(.2,.7,.2,1)' });
    return () => animation?.cancel();
  }, [path, office]);

  useEffect(() => {
    void bootstrap();
    const recover = () => {
      const state = useAgentStore.getState();
      if (!state.conversationsError) return;
      void (state.bootstrapped ? state.refreshConversations() : state.bootstrap());
    };
    window.addEventListener('online', recover);
    return () => {
      window.removeEventListener('online', recover);
    };
  }, [bootstrap]);

  return (
    <>
      {/* 自绘标题栏占一行，下面才是「侧栏 + 页面」的横排 ——
          侧栏收起后左上角不再出现浮动按钮与顶栏标题打架的问题 */}
      <div className="app-frame">
        {titled && <TitleBar />}
        <AppHeader office={office} />
        <div ref={contentRef} className={`app app--salon app--unified${path.startsWith('/workspace') ? ' app--workspace' : ''}${office ? ' app--office' : ''}`}>
          {!office && <Sidebar />}
          <Outlet />
        </div>
      </div>
    </>
  );
}
