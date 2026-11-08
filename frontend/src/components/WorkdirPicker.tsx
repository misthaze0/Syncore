/**
 * 「选择工作区」那颗胶囊 + 它弹出的清单
 *
 * 界面参照 Qoder 桌面版的工作区选择器，但**词与物都对齐到本项目的后端**：
 * 这里选的是一个本地目录（`app/workdir`），不是「工作空间 = 群组 + 员工」那套。
 *
 * ★ 底部只有两个动作：新建工作区 / 默认工作区
 * 「默认」就是原来的自动分配 —— 一个对话窗口一个产物文件夹（`2026102901`），
 * 这个窗口里的每一轮都写进同一个文件夹；名字由后端在第一次真正用到时定
 * （见 `app/workdir/store.py`）。新会话进来看到的就是它亮着。
 * 「不指定工作区」这一档已经从界面里去掉了：它和默认的差别只剩「文件落在沙箱
 * 而不是落在对话文件夹」，而用户在这里真正要选的其实是
 * 「用默认，还是用我自己的项目目录」。后端仍然接受空绑定（`workdir_id=''`），
 * 历史会话与「设置 → 数据」的入口都不受影响 —— 只是不再给新会话留这个岔路。
 *
 * ★ 选了工作区不等于解除沙箱
 * 它只是把那个目录**加进**可达范围。越界判定、权限档位、审批都照旧，
 * 判据在后端（`core/sandbox.writable_roots`）。
 *
 * ★ 键盘走的是同一条序列
 * 列表项 + 新建 + 默认 拼成 `rows`，↑↓ 一路循环，不用先离开列表再按 Tab。
 * 鼠标划过某行会把光标挪过去 —— 两套输入共用一个高亮，避免出现「按回车选中了
 * 刚才鼠标扫过的那项」这种谁也说不清的状态。
 */

import { useEffect, useRef, useState } from 'react';

import {
  BookOpen,
  Bot,
  Brain,
  Check,
  ChevronDown,
  Code,
  Database,
  Flag,
  Folder,
  FolderOpen,
  Globe,
  Heart,
  Layers,
  Monitor,
  Plus,
  Search,
  Sparkles,
  Star,
  Terminal,
  Wine,
  X,
  Zap,
  type IconType,
} from '@/components/icons';
import { WorkdirCreateDialog } from '@/components/WorkdirCreateDialog';
import { useT } from '@/i18n';
import { AUTO_WORKDIR_ID } from '@/lib/api';
import { useWorkdirStore } from '@/stores/workdirStore';
import { toast } from '@/stores/toastStore';
import type { WorkdirEntry } from '@/types';

/**
 * 图标名 → 组件。
 *
 * ⚠️ 键必须与后端 `app/workdir/store.py::ICON_NAMES` 一一对应。
 * 两边各留一份清单是没办法的事：后端要校验（不认识的图标名不许入库），
 * 前端要渲染（不认识的名字总得有个回落）。对不上的后果不报错，
 * 只是那个工作区显示成文件夹 —— 所以这里一个都不多、一个都不少。
 */
const ICON_MAP: Record<string, IconType> = {
  folder: Folder,
  code: Code,
  terminal: Terminal,
  database: Database,
  globe: Globe,
  book: BookOpen,
  brain: Brain,
  bot: Bot,
  sparkles: Sparkles,
  wine: Wine,
  star: Star,
  heart: Heart,
  zap: Zap,
  flag: Flag,
  layers: Layers,
  monitor: Monitor,
};

/** 给新建对话框用的同一份图标清单（顺序即界面上的顺序） */
export const WORKDIR_ICONS = Object.entries(ICON_MAP);

/** 认不出来的图标名一律回落到文件夹（后端可能先于前端加了新名字） */
export function workdirIcon(name: string): IconType {
  return ICON_MAP[name] ?? Folder;
}

/** ↑↓ 走过的一行：某个已授权目录，或底部两个动作之一 */
type Row =
  | { kind: 'workdir'; entry: WorkdirEntry }
  | { kind: 'create' }
  | { kind: 'default' };

interface WorkdirPickerProps {
  /** 当前会话 id —— 绑定就记在它名下 */
  threadId: string;
}

export function WorkdirPicker({ threadId }: WorkdirPickerProps) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  const workdirs = useWorkdirStore((s) => s.workdirs);
  const loading = useWorkdirStore((s) => s.loading);
  const boundId = useWorkdirStore((s) => s.boundId);
  const boundMode = useWorkdirStore((s) => s.boundMode);
  const boundEntry = useWorkdirStore((s) => s.boundEntry);
  const load = useWorkdirStore((s) => s.load);
  const bind = useWorkdirStore((s) => s.bind);
  const remove = useWorkdirStore((s) => s.remove);

  // 自动分配的目录不在 `workdirs` 清单里，得靠绑定返回的那份条目
  const bound = boundEntry ?? workdirs.find((w) => w.id === boundId) ?? null;
  const autoActive = boundMode === 'auto' || boundEntry?.origin === 'auto';

  const q = query.trim().toLowerCase();
  const visible = q
    ? workdirs.filter((w) => `${w.name} ${w.path}`.toLowerCase().includes(q))
    : workdirs;

  const rows: Row[] = [
    ...visible.map<Row>((entry) => ({ kind: 'workdir', entry })),
    { kind: 'create' },
    { kind: 'default' },
  ];

  // 点外面收起；Esc 只关浮层（浮层开着时它比什么都优先）
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  // 筛选把行数改少之后，光标可能落在已经不存在的下标上
  useEffect(() => {
    setCursor((c) => Math.min(c, Math.max(rows.length - 1, 0)));
  }, [rows.length]);

  // 键盘走出来的高亮必须看得见：列表是会滚的
  useEffect(() => {
    if (!open) return;
    panelRef.current
      ?.querySelector<HTMLElement>(`[data-row="${cursor}"]`)
      ?.scrollIntoView({ block: 'nearest' });
  }, [cursor, open]);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next) {
      setQuery('');
      // 光标停在当前绑定的那一项上；没绑定（或自动档）就从头开始
      const idx = workdirs.findIndex((w) => w.id === boundId);
      setCursor(idx > 0 ? idx : 0);
      void load();
    }
  };

  const pick = async (value: string) => {
    setOpen(false);
    try {
      await bind(threadId, value);
    } catch (e) {
      toast(e instanceof Error ? e.message : t('workdir.bindFailed'), 'error');
    }
  };

  const drop = async (entry: WorkdirEntry) => {
    try {
      await remove(entry.id);
      toast(t('workdir.revoked'), 'success');
    } catch (e) {
      toast(e instanceof Error ? e.message : t('workdir.revokeFailed'), 'error');
    }
  };

  const activate = (row: Row | undefined) => {
    if (!row) return;
    if (row.kind === 'create') {
      setOpen(false);
      setCreating(true);
      return;
    }
    if (row.kind === 'default') {
      void pick(AUTO_WORKDIR_ID);
      return;
    }
    if (row.entry.exists) void pick(row.entry.id);
    else toast(t('workdir.missing'), 'error');
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      e.stopPropagation();
      const step = e.key === 'ArrowDown' ? 1 : -1;
      setCursor((c) => (c + step + rows.length) % rows.length);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      e.stopPropagation();
      activate(rows[cursor]);
    }
  };

  // 自动分配：目录还没建时显示「自动分配」，建好之后直接显示文件夹名
  const autoLabel = boundEntry?.origin === 'auto' ? boundEntry.name : t('workdir.auto');
  const label = autoActive ? autoLabel : bound ? bound.name : t('workdir.pick');
  const firstRow = visible.length;

  return (
    <div className="workdir" ref={rootRef}>
      <button
        type="button"
        className={`composer__chip${bound || autoActive ? ' composer__chip--on' : ''}`}
        onClick={toggle}
        aria-expanded={open}
        aria-haspopup="listbox"
        title={
          autoActive
            ? boundEntry
              ? t('workdir.tipBound', { path: boundEntry.path })
              : t('workdir.autoTip')
            : bound
              ? t('workdir.tipBound', { path: bound.path })
              : t('workdir.tipNone')
        }
      >
        {autoActive ? (
          <Sparkles size={13} />
        ) : bound ? (
          <span className="workdir__dot" style={{ background: bound.color }} aria-hidden="true" />
        ) : (
          <Folder size={13} />
        )}
        <span className="composer__chip-text">{label}</span>
        <ChevronDown size={12} className={`composer__chip-caret${open ? ' is-open' : ''}`} />
      </button>

      {open && (
        <div className="workdir__panel" ref={panelRef} onKeyDown={onKeyDown} aria-label={t('workdir.pick')}>
          <div className={`workdir__search${query ? ' is-filled' : ''}`}>
            <Search size={13} />
            <input
              className="workdir__search-input"
              value={query}
              placeholder={t('workdir.search')}
              spellCheck={false}
              autoFocus
              role="combobox"
              aria-expanded={open}
              aria-controls="workdir-list"
              aria-activedescendant={`workdir-row-${cursor}`}
              onChange={(e) => setQuery(e.target.value)}
              aria-label={t('workdir.search')}
            />
            {query && (
              <button
                type="button"
                className="workdir__clear"
                onClick={() => setQuery('')}
                aria-label={t('common.clearSearch')}
              >
                <X size={12} />
              </button>
            )}
          </div>

          {workdirs.length > 0 && (
            <div className="workdir__group">
              {q
                ? t('workdir.matchCount', { n: visible.length })
                : t('workdir.authorized', { n: workdirs.length })}
            </div>
          )}

          <div className="workdir__list" id="workdir-list" role="listbox" aria-label={t('workdir.pick')}>
            {loading && workdirs.length === 0 ? (
              <div className="workdir__skeleton" aria-hidden="true">
                <i />
                <i />
                <i />
              </div>
            ) : visible.length === 0 ? (
              <div className="workdir__empty">
                <span className="workdir__empty-icon" aria-hidden="true">
                  {q ? <Search size={15} /> : <FolderOpen size={15} />}
                </span>
                {q ? t('workdir.noMatch') : t('workdir.empty')}
              </div>
            ) : (
              visible.map((w, i) => {
                const Icon = workdirIcon(w.icon);
                const active = w.id === boundId && !autoActive;
                return (
                  <div
                    key={w.id}
                    id={`workdir-row-${i}`}
                    data-row={i}
                    className={`workdir__item${active ? ' workdir__item--active' : ''}${
                      cursor === i ? ' is-cursor' : ''
                    }`}
                    role="option"
                    aria-selected={active}
                    title={w.path}
                    onMouseEnter={() => setCursor(i)}
                    onClick={() => (w.exists ? void pick(w.id) : toast(t('workdir.missing'), 'error'))}
                  >
                    <span className="workdir__icon" style={{ color: w.color }}>
                      <Icon size={14} />
                    </span>
                    <span className="workdir__text">
                      <span className="workdir__name">{w.name}</span>
                      <span className={`workdir__path${w.exists ? '' : ' workdir__path--gone'}`}>
                        {w.exists ? w.path : t('workdir.missing')}
                      </span>
                    </span>
                    {active && (
                      <span className="workdir__check" aria-hidden="true">
                        <Check size={13} />
                      </span>
                    )}
                    <button
                      type="button"
                      className="workdir__remove"
                      title={t('workdir.revokeTip')}
                      aria-label={t('workdir.revokeTip')}
                      onClick={(e) => {
                        e.stopPropagation();
                        void drop(w);
                      }}
                    >
                      <X size={12} />
                    </button>
                  </div>
                );
              })
            )}
          </div>

          <div className="workdir__foot">
            <button
              type="button"
              id={`workdir-row-${firstRow}`}
              data-row={firstRow}
              className={`workdir__action${cursor === firstRow ? ' is-cursor' : ''}`}
              onMouseEnter={() => setCursor(firstRow)}
              onClick={() => {
                setOpen(false);
                setCreating(true);
              }}
            >
              <span className="workdir__action-icon" aria-hidden="true">
                <Plus size={14} />
              </span>
              <span className="workdir__action-label">{t('workdir.create')}</span>
              <span className="workdir__action-hint">{t('workdir.createHint')}</span>
            </button>
            <button
              type="button"
              id={`workdir-row-${firstRow + 1}`}
              data-row={firstRow + 1}
              className={`workdir__action workdir__action--default${
                autoActive ? ' is-active' : ''
              }${cursor === firstRow + 1 ? ' is-cursor' : ''}`}
              onMouseEnter={() => setCursor(firstRow + 1)}
              onClick={() => void pick(AUTO_WORKDIR_ID)}
              title={t('workdir.autoTip')}
            >
              <span className="workdir__action-icon" aria-hidden="true">
                <Sparkles size={14} />
              </span>
              <span className="workdir__action-label">{t('workdir.default')}</span>
              <span className="workdir__action-hint">{autoActive ? autoLabel : t('workdir.auto')}</span>
              {autoActive && (
                <span className="workdir__action-check" aria-hidden="true">
                  <Check size={13} />
                </span>
              )}
            </button>
          </div>

          <div className="workdir__hint">
            <kbd>↑</kbd>
            <kbd>↓</kbd>
            {t('workdir.hintMove')}
            <kbd>Enter</kbd>
            {t('workdir.hintPick')}
            <kbd>Esc</kbd>
            {t('workdir.hintClose')}
          </div>
        </div>
      )}

      {creating && <WorkdirCreateDialog threadId={threadId} onClose={() => setCreating(false)} />}
    </div>
  );
}
