/**
 * 侧边栏（2026-10 重构）—— ChatGPT / Linear 范式
 *
 * 结构：品牌区 / 新建会话 + 一级页面（任务·工作空间·知识库·插件市场）/ 会话历史（含搜索）/ 设置 + 状态。
 *
 * 设计约定（见 docs 的前端重构说明）：
 * - 品牌绿只出现在「新建会话」主按钮、置顶标记与状态点上，不再铺满侧栏；
 * - 当前会话用中性深一度底色表达，不用绿色（强调色一多就回到「土」）；
 * - 底部不再显示内部 thread id，而是「模型 + 版本」—— 用户看得懂的身份信息。
 *
 * ★ 2026-10：从 ChatPage 提升到 AppLayout
 * 它现在是「对话」与「后台任务」两个页面共有的壳，路由切换不再重建它 ——
 * 折叠状态、搜索词、列表滚动位置在页面间自然保留。
 */

import { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { Bot, Database, ListTodo, Network, Package, Plus, Timer, Users } from '@/components/icons';

import { ConversationList } from '@/components/ConversationList';
import { useT } from '@/i18n';
import { shortModelName } from '@/lib/api';
import { useAgentStore } from '@/stores/agentStore';
import { useSidePanelStore } from '@/stores/artifactStore';
import { useUiStore } from '@/stores/uiStore';

export function Sidebar() {
  /** 折叠位在 uiStore：标题栏（桌面端）和这里改的是同一份状态 */
  const collapsed = useUiStore((s) => s.sidebarCollapsed);
  const setCollapsed = useUiStore((s) => s.setSidebarCollapsed);
  const [compactWindow, setCompactWindow] = useState(() => window.matchMedia('(max-width: 760px)').matches);
  const [narrowWindow, setNarrowWindow] = useState(() => window.matchMedia('(max-width: 1180px)').matches);
  const panelOpen = useSidePanelStore((s) => s.panelOpen);
  const autoCollapsed = narrowWindow && panelOpen;

  useEffect(() => {
    const media = window.matchMedia('(max-width: 1180px)');
    const update = () => setNarrowWindow(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  useEffect(() => {
    const media = window.matchMedia('(max-width: 760px)');
    const update = () => { setCompactWindow(media.matches); if (media.matches) setCollapsed(true); };
    update();
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  const navigate = useNavigate();
  const location = useLocation();
  const t = useT();
  /**
   * 设置弹窗打开时 location 已是 `/settings`，但来源页仍垫在下面。
   * 侧栏的高亮和「助理页没有新建会话」都要看来源页，不能跟着弹窗走。
   */
  const background = (location.state as { background?: { pathname: string } } | null)?.background;
  const currentPath = background?.pathname ?? location.pathname;
  /**
   * 一级入口的当前态。
   * 「助理」是那条固定单例会话的页面（见 agentStore.scope）—— 侧栏列表
   * 不随她换，所以这里的当前态只回答一个问题：「我此刻在哪个页面」。
   */
  const navCls = (path: string) => `sb__lib${currentPath === path || currentPath.startsWith(`${path}/`) ? ' sb__lib--active' : ''}`;
  /** 助理页没有「新建会话」：全场就那一条，新建无处可去 */
  const onAssistant = currentPath === '/assistant';
  // 后台任务数：入口上挂一个小徽标 —— 有任务在跑时，这个入口才值得被看见
  const activeTaskCount = useAgentStore((s) => s.activeTaskCount);
  // 模型名与版本号走 store：设置里换掉默认模型后，这里要跟着变
  // （各自持有一份的话，常驻的侧栏会一直显示旧名字，看起来像「换了没生效」）
  const info = useAgentStore((s) => s.serviceInfo);
  const loadServiceInfo = useAgentStore((s) => s.loadServiceInfo);

  const closeDrawer = () => {
    if (window.matchMedia('(max-width: 760px)').matches) setCollapsed(true);
  };
  const openPage = (path: string) => {
    closeDrawer();
    navigate(path);
  };
  const newSession = () => {
    // 「新建会话」只属于普通对话。站在助理页按 Ctrl+N 时不能就地新建
    // （助理 scope 下 newSession 是空操作，会被当成「按了没反应」）——
    // 先带回对话页，scope 跟着切换，下一次按键才真的开新会话。
    if (useAgentStore.getState().scope === 'assistant') {
      openPage('/chat');
      return;
    }
    useAgentStore.getState().newSession();
    openPage('/chat');
  };

  // Ctrl / Cmd + N 新建会话 —— 与按钮上的快捷键提示保持一致
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (document.querySelector('[aria-modal="true"]')) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n') {
        e.preventDefault();
        newSession();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 拿不到就退化成「本地运行」，不该因此报错或留一个空洞
  useEffect(() => {
    void loadServiceInfo();
  }, [loadServiceInfo]);

  /**
   * 收起态：有自绘标题栏时入口就在标题栏上（这里什么都不渲染）；
   * 其他环境没有标题栏，保留浮动按钮当唯一的展开入口。
   */
  if (collapsed || autoCollapsed) return null;

  const status = info
    ? [info.model ? shortModelName(info.model) : '', info.version ? `v${info.version}` : '']
        .filter(Boolean)
        .join(' · ') || t('sidebar.online')
    : t('sidebar.local');

  return (
    <>
    {compactWindow && <button className="sb-mobile-scrim" aria-label={t('sidebar.collapse')} onClick={() => setCollapsed(true)} />}
    <aside className="sb">
      <div className="sb__actions">
        {!onAssistant && (
          <button type="button" className="sb__new" onClick={newSession} title="新建会话 (Ctrl N)">
            <Plus size={15} />
            <span>{t('sidebar.newSession')}</span>
            <kbd className="sb__kbd">Ctrl N</kbd>
          </button>
        )}
        <button
          type="button"
          className={navCls('/assistant')}
          onClick={() => openPage('/assistant')}
          title={t('sidebar.assistant')}
        >
          <Bot size={14} />
          <span>{t('sidebar.assistant')}</span>
        </button>
        <button
          type="button"
          className={navCls('/tasks')}
          onClick={() => openPage('/tasks')}
          title={t('tasks.title')}
        >
          <ListTodo size={14} />
          <span>{t('tasks.title')}</span>
          {activeTaskCount > 0 && <span className="sb__badge">{activeTaskCount}</span>}
        </button>
        <button
          type="button"
          className={navCls('/schedules')}
          onClick={() => openPage('/schedules')}
          title={t('scheduled.title')}
        >
          <Timer size={14} />
          <span>{t('scheduled.title')}</span>
        </button>
        <button
          type="button"
          className={navCls('/workflows')}
          onClick={() => openPage('/workflows')}
          title={t('sidebar.workflows')}
        >
          <Network size={14} />
          <span>{t('sidebar.workflows')}</span>
        </button>
        <button
          type="button"
          className={navCls('/workspace')}
          onClick={() => openPage('/workspace')}
          title={t('workspace.title')}
        >
          <Users size={14} />
          <span>{t('workspace.title')}</span>
        </button>
        <button
          type="button"
          className={navCls('/knowledge')}
          onClick={() => openPage('/knowledge')}
          title={t('sidebar.knowledge')}
        >
          <Database size={14} />
          <span>{t('sidebar.knowledge')}</span>
        </button>
        <button
          type="button"
          className={navCls('/market')}
          onClick={() => openPage('/market')}
          title={t('sidebar.market')}
        >
          <Package size={14} />
          <span>{t('sidebar.market')}</span>
        </button>
      </div>

      <div className="sb__body">
        <ConversationList onSelect={closeDrawer} />
      </div>

      <div className="sb__foot">
        <div className="sb__status" title={info?.model ? t('sidebar.currentModel', { model: info.model }) : t('sidebar.offline')}>
          <span className={`sb__status-dot${info ? '' : ' sb__status-dot--off'}`} />
          {status}
        </div>
      </div>
    </aside>
    </>
  );
}
