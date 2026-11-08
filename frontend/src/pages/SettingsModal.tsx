/**
 * 设置仅保留应用偏好与运行配置。
 * 技能与连接器集中到插件市场；AI 权限与审批在设置中统一管理。
 */

import { useState } from 'react';

import {
  BarChart3,
  Bell,
  BookOpen,
  Bot,
  Cat,
  Database,
  Globe,
  Info,
  Keyboard,
  Monitor,
  Palette,
  Server,
  ShieldCheck,
  Sparkles,
  Terminal,
  Zap,
} from '@/components/icons';

import { DetailModal, type ModalNavGroup, type ModalNavItem } from '@/components/DetailModal';
import { AboutPanel } from '@/components/settings/AboutPanel';
import { AppearancePanel } from '@/components/settings/AppearancePanel';
import { ShortcutsPanel } from '@/components/settings/ShortcutsPanel';
import { AgentsPage } from '@/components/settings2/AgentsPage';
import { AssistantPage } from '@/components/settings2/AssistantPage';
import { BrowserPage } from '@/components/settings2/BrowserPage';
import { DataPage } from '@/components/settings2/DataPage';
import { DeviceControlPage } from '@/components/settings2/DeviceControlPage';
import { HooksPage } from '@/components/settings2/HooksPage';
import { ModelsPage } from '@/components/settings2/ModelsPage';
import { PetPage } from '@/components/settings2/PetPage';
import { QuickCommandsPage } from '@/components/settings2/QuickCommandsPage';
import { RulesMemoryPage } from '@/components/settings2/RulesMemoryPage';
import { UsagePage } from '@/components/settings2/UsagePage';
import { TerminalPage } from '@/components/settings2/TerminalPage';
import { PermissionsPage } from '@/components/settings2/PermissionsPage';
import { isDesktopShell } from '@/types/desktop';

export type SettingsTab =
  | 'appearance'
  | 'pet'
  | 'shortcuts'
  | 'about'
  | 'assistant'
  | 'usage'
  | 'data'
  | 'agents'
  | 'models'
  | 'browser'
  | 'terminal'
  | 'device'
  | 'quickcmd'
  | 'rulesmemory'
  | 'permissions'
  | 'hooks';

const ITEMS: ModalNavItem[] = [
  // 通用（外观/快捷键/关于沿用旧实现）
  { key: 'appearance', icon: Palette, label: '外观', hint: '主题、字号等视觉偏好', keywords: '外观 主题 暗色 浅色 字号 appearance theme' },
  // 桌面助手只存在于桌面壳：网页端没有透明置顶窗，入口也不出现
  ...(isDesktopShell()
    ? [{ key: 'pet', icon: Cat, label: '桌面助手', hint: '桌面助手的显示、置顶与静音', keywords: '桌面助手 桌面 悬浮 透明 置顶 Syncore pet' }]
    : []),
  { key: 'shortcuts', icon: Keyboard, label: '快捷键', hint: '全部键盘操作一览', keywords: '快捷键 键盘 shortcut' },
  { key: 'about', icon: Info, label: '关于', hint: '版本与运行状态', keywords: '关于 版本 about' },
  // TRAE 主体
  { key: 'assistant', icon: Sparkles, label: '助理', hint: '人设卡与渠道接入', keywords: '助手 助理 人设 人设卡 角色卡 头像 开场白 微信 persona assistant' },
  { key: 'usage', icon: BarChart3, label: '用量记录', hint: 'Token 用量与调用统计', keywords: '用量 token 统计 usage' },
  { key: 'data', icon: Database, label: '数据', hint: '对话产生的文件夹与占用', keywords: '数据 存储 磁盘 文件夹 工作区 清理 data storage folder' },
  { key: 'agents', icon: Bot, label: '智能体', hint: '专员的定义、工具与模型', keywords: '智能体 agent supervisor 专员 子任务 自定义' },
  { key: 'models', icon: Server, label: '模型', hint: '回答与检索用的模型', keywords: '模型 llm embedding rerank qwen' },
  { key: 'browser', icon: Globe, label: '浏览器', hint: '链接、首页、缩放与工作台布局', keywords: '浏览器 链接 联网 browser 工作台 宽度 缩放 首页' },
  { key: 'terminal', icon: Terminal, label: '终端', hint: '终端会话、字体、光标与配色', keywords: '终端 terminal shell powershell cmd 字号 光标 命令行' },
  { key: 'device', icon: Monitor, label: '电脑操控', hint: '屏幕视觉、键鼠操作与接管状态', keywords: '电脑 操控 桌面 鼠标 键盘 截图 视觉 助理 windows mcp' },
  { key: 'permissions', icon: ShieldCheck, label: 'AI 权限与审批', hint: '默认权限、工具规则、临时授权与审批记录', keywords: '权限 审批 安全 授权 只读 自动 完全访问 permissions approval' },
  { key: 'quickcmd', icon: Zap, label: '快捷指令', hint: '输入 / 插入的常用问法', keywords: '快捷指令 问法 提示词 斜杠 slash prompt' },
  { key: 'rulesmemory', icon: BookOpen, label: '规则与记忆', hint: '人设与长期偏好', keywords: '规则 记忆 人设 偏好 persona memory' },
  { key: 'hooks', icon: Bell, label: 'Hooks', hint: '会话节点的自动动作', keywords: 'hooks 钩子 自动' },
];

const inGroup = (keys: SettingsTab[]): ModalNavItem[] =>
  ITEMS.filter((i) => keys.includes(i.key as SettingsTab));

/** 通用偏好、助理、运行配置与个性化。 */
const GROUPS: ModalNavGroup[] = [
  { label: '通用', items: inGroup(['appearance', 'pet', 'shortcuts', 'about']) },
  { label: '', items: inGroup(['assistant', 'usage', 'data']) },
  {
    label: '',
    items: inGroup(['agents', 'models', 'permissions', 'browser', 'terminal', 'device']),
  },
  { label: '', items: inGroup(['quickcmd', 'rulesmemory', 'hooks']) },
];

const ALL_KEYS = new Set(ITEMS.map((i) => i.key));

export function SettingsModal({
  onClose,
  initialTab,
}: {
  onClose: () => void;
  /** 直接定位到某个页签（侧栏、命令面板、`#settings/<tab>` 链接都走这里） */
  initialTab?: string;
}) {
  const [tab, setTab] = useState<SettingsTab>(
    initialTab && ALL_KEYS.has(initialTab) ? (initialTab as SettingsTab) : 'appearance',
  );

  return (
    <DetailModal
      ariaLabel="设置"
      groups={GROUPS}
      activeKey={tab}
      onSelect={(key) => setTab(key as SettingsTab)}
      onClose={onClose}
    >
      {tab === 'appearance' && <AppearancePanel />}
      {tab === 'pet' && <PetPage />}
      {tab === 'shortcuts' && <ShortcutsPanel />}
      {tab === 'about' && <AboutPanel />}
      {tab === 'assistant' && <AssistantPage />}
      {tab === 'usage' && <UsagePage />}
      {tab === 'data' && <DataPage />}
      {tab === 'agents' && <AgentsPage />}
      {tab === 'models' && <ModelsPage />}
      {tab === 'browser' && <BrowserPage />}
      {tab === 'terminal' && <TerminalPage />}
      {tab === 'device' && <DeviceControlPage />}
      {tab === 'permissions' && <PermissionsPage />}
      {tab === 'quickcmd' && <QuickCommandsPage />}
      {tab === 'rulesmemory' && <RulesMemoryPage />}
      {tab === 'hooks' && <HooksPage />}
    </DetailModal>
  );
}

export default SettingsModal;
