/**
 * 应用路由 —— react-router（HashRouter）
 *
 * ★ 为什么是 hash 路由而不是 BrowserRouter
 *
 * 桌面级应用经常直接双击打开 dist/index.html（file:// 协议），
 * 或者部署在任意的子路径后面 —— BrowserRouter 在这种环境里都会挂。
 * hash 路由对部署方式零要求，与旧的 `#settings/<tab>` 语义也天然兼容。
 *
 * ★ 设置是「叠在来源页上」的弹窗，不是一次页面跳转
 *
 * 从任何页面打开设置时，把当前 location 放进新 location 的 `state.background`，
 * 主 Routes 用 `location={background}` 继续渲染来源页，另起一组 Routes 只画弹窗。
 * 这样任务、定时任务、工作空间、插件市场、知识库（含详情页）在设置开关之间
 * 不会被卸载或替换成对话页，关闭时 `navigate(-1)` 原样退回。
 *
 * 只按 pathname 记「从哪来」是撑不住的：参数化路由、页面内展开状态、
 * 滚动位置都会丢；背景路由把整棵来源子树留在原位，天然不丢。
 *
 * ★ 旧链接兼容（它们是「可以发出去的公开地址」）
 *
 * - `#settings` / `#settings/<tab>`（直接打开，没有 background）→ 对话 + 设置弹窗。
 *
 * 兼容的做法：`/settings`、`/settings/:tab` 同时登记在主路由里，
 * 没有 background 时由 `LegacySettingsRoute` 渲染「对话页 + 设置弹窗」。
 *
 * ★ 为什么 Sidebar 提到布局层（AppLayout）
 *
 * 侧栏原来是 ChatPage 的一部分，加任务页之后它成了两个页面共有的壳 ——
 * 放在布局里，切页面时侧栏状态（折叠、搜索词）自然保留，
 * 也不用两个页面各自挂一份。
 */

import { lazy, Suspense, useEffect, useState } from 'react';
import {
  HashRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
} from 'react-router-dom';
import type { Location } from 'react-router-dom';

import { ToastContainer } from '@/components/Toast';
import { ConfirmDialogHost } from '@/components/ConfirmDialog';
import { AppLayout } from '@/components/AppLayout';
import { ChatPage } from '@/pages/ChatPage';
import { EmployeeChatPage } from '@/pages/EmployeeChatPage';
import { GroupPage } from '@/pages/GroupPage';
import { MarketPage } from '@/pages/MarketPage';
import { ScheduleDetailPage } from '@/pages/ScheduleDetailPage';
import { SchedulesPage } from '@/pages/SchedulesPage';
import { SettingsModal } from '@/pages/SettingsModal';
import { TasksPage } from '@/pages/TasksPage';
import { WorkflowEditorPage } from '@/pages/WorkflowEditorPage';
import { WorkflowsPage } from '@/pages/WorkflowsPage';
import { WorkspacePage } from '@/pages/WorkspacePage';
import { useAgentStore } from '@/stores/agentStore';
import { useDesktopPetIntegration } from '@/hooks/useDesktopPetIntegration';
import { useTaskSync } from '@/hooks/useTaskSync';

const OfficePage = lazy(() => import('@/pages/OfficePage').then(module => ({ default: module.OfficePage })));
const KnowledgePage = lazy(() => import('@/pages/KnowledgePage'));

function initialRoute(): string {
  try { return localStorage.getItem('xjms.mode') === 'office' ? '/office' : '/chat'; }
  catch { return '/chat'; }
}

/** 从设置弹窗搬到侧栏一级页面的旧页签 —— 旧地址照常打得开，直接改道过去 */
const MOVED_TABS: Record<string, string> = { plugins: '/market', indexdocs: '/knowledge', mcp: '/market?view=installed', skills: '/market?view=installed', toolsgov: '/market?view=installed&panel=tools' };

/** 在 Routes 里捕获旧 hash 并改写成「对话 + 设置弹窗」 */
function LegacySettingsRoute() {
  const location = useLocation();
  const navigate = useNavigate();
  // `#settings/knowledge` → pathname `/settings/knowledge`（HashRouter 去掉 #）
  const p = location.pathname.replace(/^\/+/, '');
  const isSettings = p === 'settings' || p.startsWith('settings/');
  // tab 为空串时传 undefined（弹窗打开默认页）。
  const tab =
    p.startsWith('settings/') && p.length > 'settings/'.length
      ? p.slice('settings/'.length)
      : undefined;

  if (!isSettings) return null;

  const moved = MOVED_TABS[tab ?? ''];
  if (moved) return <Navigate to={moved} replace />;

  return (
    <ChatWithSettings
      tab={tab}
      onClose={() => navigate('/chat', { replace: true })}
    />
  );
}

/**
 * 对话 / 助理页的公共入口。
 * 打开设置时把整个来源 location 放进 `state.background`：
 * 主路由继续画这一页，弹窗单独叠在上面，关闭即原样退回。
 */
function ChatRoute({ chrome }: { chrome?: 'chat' | 'assistant' }) {
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <ChatPage
      chrome={chrome}
      onOpenSettings={(tab) =>
        navigate(tab ? `/settings/${tab}` : '/settings', { state: { background: location } })
      }
    />
  );
}

/**
 * 设置弹窗的浮层。来源页由主路由按 `state.background` 保持挂载，
 * 这里只负责弹窗本身；关闭走 `navigate(-1)`，回到打开它的那一页。
 */
function SettingsOverlay({ tab }: { tab?: string }) {
  const { tab: pathTab } = useParams<{ tab: string }>();
  const navigate = useNavigate();
  const initialTab = tab ?? pathTab;
  // 关掉设置中心后重拉模型清单与生效模型：「设置 → 模型」改的就是这两样，
  // 不重拉的话输入框那颗模型钮会一直显示旧的候选，直到刷新页面。
  useEffect(() => () => {
    void useAgentStore.getState().loadModels();
    void useAgentStore.getState().loadServiceInfo(true);
  }, []);
  if (initialTab && MOVED_TABS[initialTab]) return <Navigate to={MOVED_TABS[initialTab]} replace />;
  return (
    // key 带 tab：弹窗已开时再切页签必须重挂载才能换掉初始页
    <SettingsModal
      key={initialTab ?? 'default'}
      initialTab={initialTab}
      onClose={() => navigate(-1)}
    />
  );
}

/** 直接访问旧链接时的兜底：没有来源页，就把对话页垫在弹窗下面 */
function ChatWithSettings({ tab, onClose }: { tab?: string; onClose: () => void }) {
  const navigate = useNavigate();
  useEffect(() => () => {
    void useAgentStore.getState().loadModels();
    void useAgentStore.getState().loadServiceInfo(true);
  }, []);
  return (
    <>
      <ChatPage
        onOpenSettings={(t) =>
          navigate(t ? `/settings/${t}` : '/settings', { replace: true })
        }
      />
      <SettingsModal key={tab ?? 'default'} initialTab={tab} onClose={onClose} />
    </>
  );
}

function AppRoutes() {
  useDesktopPetIntegration();
  useTaskSync();
  const location = useLocation();
  // 打开设置时记下的来源页；没有它就是直接访问 `#/settings/...` 的旧链接
  const background = location.state?.background as Location | undefined;

  return (
    <>
      <Routes location={background ?? location}>
        <Route element={<AppLayout />}>
          <Route path="/" element={<Navigate to={initialRoute()} replace />} />
          <Route path="/chat" element={<ChatRoute />} />
          <Route path="/office" element={<Suspense fallback={<div className="main office-editor-loading"><span className="spinner" />正在打开办公空间…</div>}><OfficePage /></Suspense>} />
          {/* 「助理」= 那条固定单例会话（`assistant_main`，agentStore.scope='assistant'）：
              不能新建、不分支、不进侧栏列表 —— 侧栏在任何页面显示的都是普通对话的记录。
              与普通对话各自躺在服务端历史里，互不可见、永不串台。 */}
          <Route path="/assistant" element={<ChatRoute chrome="assistant" />} />
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="/schedules" element={<SchedulesPage />} />
          <Route path="/schedules/:scheduleId" element={<ScheduleDetailPage />} />
          <Route path="/workflows" element={<WorkflowsPage />} />
          <Route path="/workflows/:workflowId" element={<WorkflowEditorPage />} />
          <Route path="/workspace" element={<WorkspacePage />} />
          <Route path="/workspace/groups/:groupId" element={<GroupPage />} />
          <Route path="/workspace/employees/:employeeId" element={<EmployeeChatPage />} />
          {/* 侧栏一级页面：目录和管理页都要宽度，不放弹窗里 */}
          <Route path="/market" element={<MarketPage />} />
          <Route path="/knowledge" element={<Suspense fallback={<main className="main" role="status" style={{ display: 'grid', placeItems: 'center' }}><span><span className="spinner" /> 正在打开知识库…</span></main>}><KnowledgePage /></Suspense>} />
          {/* 旧链接：#settings/<tab> —— 没有来源页时渲染「对话 + 设置弹窗」 */}
          <Route path="/settings" element={<LegacySettingsRoute />} />
          <Route path="/settings/:tab" element={<LegacySettingsRoute />} />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Route>
      </Routes>
      {/* 有来源页时，弹窗叠在它上面；主路由已经停在来源页，不会换掉它 */}
      {background && (
        <Routes>
          <Route path="/settings" element={<SettingsOverlay />} />
          <Route path="/settings/:tab" element={<SettingsOverlay />} />
        </Routes>
      )}
    </>
  );
}

export default function App() {
  // 入口 hash 是旧格式时先改写成路由格式，避免 HashRouter 落到通配路由再弹回来
  const [normalized, setNormalized] = useState(false);

  useEffect(() => {
    const h = window.location.hash.replace(/^#\/?/, '');
    if (h && !h.startsWith('/')) {
      window.location.hash = `#/${h}`;
    }
    setNormalized(true);
  }, []);

  if (!normalized) return null;

  return (
    <HashRouter>
      <AppRoutes />
      <ToastContainer />
      <ConfirmDialogHost />
    </HashRouter>
  );
}
