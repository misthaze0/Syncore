/**
 * ★ 命令面板（Ctrl / Cmd + K）
 *
 * 一个输入框搜一切：会话、设置项、功能入口。
 *
 * ★ 为什么这个功能值得做
 *
 * 界面上的功能一多，「找东西」本身就变成了成本：想改人设要先想「在设置里」，
 * 打开设置再选 tab。命令面板把「路径」换成「名字」——
 * 用户记得住「记忆」两个字，记不住它藏在设置的第几个 tab 里。
 * 这是上层功能变多之后的**必要配套**，不是锦上添花。
 *
 * ★ 键位为什么从 Ctrl+K 挪到 Ctrl+N
 *
 * Ctrl+K 是命令面板的**事实标准**（VS Code / Linear / Notion / ChatGPT 全是它），
 * 新建会话没有这种约定。把标准键位还给标准功能，用户从别的软件带过来的肌肉记忆才成立。
 *
 * ★ 搜索实现
 *
 * 纯前端子串匹配（label + hint + keywords 三个字段）。刻意不引入模糊搜索库：
 * 命令数量在几十条量级，子串匹配的命中率已经够用，而多一个依赖就多一份打包体积 ——
 * 这个项目一直的原则是「能不引库就不引」。
 * 中文没有分词问题（直接 includes），英文标识符用小写化对齐。
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import {
  Download,
  MessageSquare,
  Moon,
  Plus,
  Search,
  Sun,
  Heart,
  Type,
  Zap,
  type LucideIcon,
} from '@/components/icons';

import { FONT_LABEL, useFontStore } from '@/hooks/useFontSize';
import { useThemeStore } from '@/hooks/useTheme';
import { THEMES, THEME_LABELS } from '@/lib/theme';
import { fetchQuickCommands } from '@/lib/api';
import { SETTINGS_PALETTE } from '@/lib/humanize';
import { useAgentStore } from '@/stores/agentStore';
import type { QuickCommand } from '@/types';

interface PaletteAction {
  id: string;
  group: string;
  label: string;
  hint?: string;
  icon: LucideIcon;
  keywords?: string;
  run: () => void;
}

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  /** 助理页不传 —— 她全场只有那一条会话，面板里不该出现一个点了没反应的「新建会话」 */
  onNewSession?: () => void;
  /** 打开设置弹窗的某个页签（key 与 SettingsModal 的 TABS 对齐） */
  onOpenSettings: (tab?: string) => void;
  /** 没有对话内容时不传 —— 面板里就不会出现一个点了没反应的命令 */
  onExport?: () => void;
  /** 选中一条快捷指令 = 把问法填进输入框。不传则不出现这一组 */
  onUseQuickCommand?: (command: QuickCommand) => void;
}

export function CommandPalette({
  open,
  onClose,
  onNewSession,
  onOpenSettings,
  onExport,
  onUseQuickCommand,
}: CommandPaletteProps) {
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState(0);
  const [quickCommands, setQuickCommands] = useState<QuickCommand[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const conversations = useAgentStore((s) => s.conversations);
  const openThread = useAgentStore((s) => s.openThread);
  const theme = useThemeStore((s) => s.theme);
  const setTheme = useThemeStore((s) => s.setTheme);
  const fontLevel = useFontStore((s) => s.level);
  const cycleFont = useFontStore((s) => s.cycle);

  // 每次打开都回到干净状态：搜索词清空、光标回第一项
  useEffect(() => {
    if (!open) return;
    setQuery('');
    setCursor(0);
    window.setTimeout(() => inputRef.current?.focus(), 0);
  }, [open]);

  // 快捷指令按打开时拉一次：设置里刚改过的条目要能立刻搜到。
  // 拉不到就不出现这一组 —— 面板其余功能不受影响。
  useEffect(() => {
    if (!open || !onUseQuickCommand) return;
    let alive = true;
    void fetchQuickCommands()
      .then((res) => {
        if (alive) setQuickCommands(res.commands);
      })
      .catch(() => {
        if (alive) setQuickCommands([]);
      });
    return () => {
      alive = false;
    };
  }, [open, onUseQuickCommand]);

  const entries = useMemo<PaletteAction[]>(() => {
    const actions: PaletteAction[] = [];

    if (onNewSession) {
      actions.push({
        id: 'act.new-session',
        group: '操作',
        label: '新建会话',
        hint: 'Ctrl N',
        icon: Plus,
        keywords: 'new chat 新建 会话',
        run: onNewSession,
      });
    }

    if (onExport) {
      actions.push({
        id: 'act.export',
        group: '操作',
        label: '导出这段对话为 Markdown',
        icon: Download,
        keywords: 'export 导出 下载 markdown 保存',
        run: onExport,
      });
    }

    actions.push(...THEMES.map(value => ({
      id: `act.theme.${value}`, group: '外观', label: `切换到${THEME_LABELS[value]}主题`,
      hint: theme === value ? '当前主题' : undefined,
      icon: value === 'light' ? Sun : value === 'dark' ? Moon : Heart,
      keywords: 'theme dark light velvet 主题 绛红 暗色 浅色 夜间',
      run: () => setTheme(value),
    })));
    actions.push(
      {
        id: 'act.font',
        group: '操作',
        label: `对话字号：${FONT_LABEL[fontLevel]}（点击切换下一档）`,
        icon: Type,
        keywords: 'font size 字号 字体 大小 缩放',
        run: cycleFont,
      },
    );

    // 快捷指令 —— 选中只是把问法填进输入框（不执行、不发消息），
    // 所以运行回调由使用方给：助理页与普通对话的输入框不是同一个实例。
    if (onUseQuickCommand) {
      for (const command of quickCommands) {
        actions.push({
          id: `qc.${command.id}`,
          group: '快捷指令',
          label: command.title,
          hint: command.category,
          icon: Zap,
          keywords: command.body,
          run: () => onUseQuickCommand(command),
        });
      }
    }

    // 设置项 —— 清单定义在 lib/humanize.ts，与设置弹窗的页签一一对应；
    // 带 route 的两条（插件市场 / 知识库）走一级页面，改 hash 就是改路由，
    // 与 ChatPage 的 onOpenSettings 同一套写法。
    for (const entry of SETTINGS_PALETTE) {
      actions.push({
        id: entry.id,
        group: entry.group,
        label: entry.label,
        hint: entry.hint,
        icon: entry.icon,
        keywords: entry.keywords,
        run: () => {
          if (entry.route) window.location.hash = `#${entry.route}`;
          else onOpenSettings(entry.settingsTab);
        },
      });
    }

    // 会话 —— 只放最近 30 条：命令面板是「快速跳转」，不是会话管理界面
    for (const conversation of conversations.slice(0, 30)) {
      actions.push({
        id: `conv.${conversation.thread_id}`,
        group: '会话',
        label: conversation.title,
        hint: `${conversation.turns} 轮`,
        icon: MessageSquare,
        keywords: conversation.thread_id,
        // openThread 而不是 openConversation：面板里的这批也是普通对话的会话，
        // 站在助理页选一条 = 要离开助理页（少做一步就是「顶栏助理、内容主对话」的串台）
        run: () => void openThread(conversation.thread_id),
      });
    }

    return actions;
  }, [
    conversations,
    fontLevel,
    onExport,
    onNewSession,
    onOpenSettings,
    onUseQuickCommand,
    openThread,
    quickCommands,
    theme,
    setTheme,
    cycleFont,
  ]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return entries;
    return entries.filter((e) =>
      `${e.label} ${e.hint ?? ''} ${e.keywords ?? ''}`.toLowerCase().includes(q),
    );
  }, [entries, query]);

  // 按 group 分组渲染（顺序沿用 entries 的自然顺序：操作 → 设置 → 高级 → 会话）
  const grouped = useMemo(() => {
    const map = new Map<string, PaletteAction[]>();
    for (const entry of filtered) {
      const bucket = map.get(entry.group);
      if (bucket) bucket.push(entry);
      else map.set(entry.group, [entry]);
    }
    return [...map.entries()];
  }, [filtered]);

  // 结果变了就把光标收回第一项 —— 否则可能停在一个已经不存在的位置上
  useEffect(() => {
    setCursor(0);
  }, [query]);

  // 选中项滚动进视野（长列表里键盘操作必须看得到选中谁）
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>('[data-active="true"]');
    el?.scrollIntoView({ block: 'nearest' });
  }, [cursor, filtered]);

  if (!open) return null;

  const run = (action: PaletteAction) => {
    onClose();
    action.run();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setCursor((c) => (filtered.length === 0 ? 0 : (c + 1) % filtered.length));
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      setCursor((c) => (filtered.length === 0 ? 0 : (c - 1 + filtered.length) % filtered.length));
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      const target = filtered[cursor];
      if (target) run(target);
    }
  };

  let flatIndex = -1;

  return (
    <div
      className="cmdk-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="cmdk" role="dialog" aria-modal="true" aria-label="命令面板">
        <div className="cmdk__search">
          <Search size={15} />
          <input
            ref={inputRef}
            className="cmdk__input"
            value={query}
            placeholder="搜索会话、设置、功能…"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
          />
          <kbd className="cmdk__esc">Esc</kbd>
        </div>

        <div className="cmdk__list" ref={listRef}>
          {grouped.map(([group, items]) => (
            <section className="cmdk__group" key={group}>
              <div className="cmdk__group-label">{group}</div>
              {items.map((item) => {
                flatIndex += 1;
                const active = flatIndex === cursor;
                return (
                  <button
                    type="button"
                    key={item.id}
                    className={`cmdk__item${active ? ' cmdk__item--active' : ''}`}
                    data-active={active ? 'true' : undefined}
                    onMouseMove={() => {
                      if (!active) setCursor(flatIndex);
                    }}
                    onClick={() => run(item)}
                  >
                    <span className="cmdk__item-icon">
                      <item.icon size={15} />
                    </span>
                    <span className="cmdk__item-label">{item.label}</span>
                    {item.hint && <span className="cmdk__item-hint">{item.hint}</span>}
                  </button>
                );
              })}
            </section>
          ))}

          {filtered.length === 0 && <div className="cmdk__empty">没有匹配的结果</div>}
        </div>

        <div className="cmdk__foot">
          <span>
            <kbd>↑</kbd>
            <kbd>↓</kbd> 选择
          </span>
          <span>
            <kbd>Enter</kbd> 执行
          </span>
          <span>
            <kbd>Esc</kbd> 关闭
          </span>
        </div>
      </div>
    </div>
  );
}
