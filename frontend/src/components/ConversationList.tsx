/**
 * 会话历史列表（侧边栏）—— 一行一条 + 「…」菜单
 *
 * 一条会话 = 一个 thread。点它就把那段对话恢复出来接着聊，
 * 数据来自后端落盘的轮次文件（见 `app/conversations/`）。
 *
 * ★ 这一版把行内三个图标钮收进了一个「…」菜单
 * 230px 宽的侧栏里一排图标会把标题挤得只剩几个字，而置顶 / 改名 / 删除都是低频动作。
 * 现在行里只留标题和一个入口，右键任意一行唤出的是同一个菜单（桌面用户的肌肉记忆）。
 *
 * ★ 分组标题（置顶 / 今天 / 昨天 / 更早）已去掉
 * 列表按最后活跃倒序、置顶排前，扫一眼就能定位；常驻分组标题在窄侧栏里只是噪音。
 *
 * ★ 删除与批量清空使用统一确认框，说明影响后再执行。
 *
 * ★ 搜索为什么在侧栏而不只在命令面板
 *
 * 「我记得问过这个，但想不起来是哪个会话」是独立且高频的诉求 ——
 * 它需要「结果里带着命中的那句话」。面板搜会话标题（本地、零延迟），
 * 这里搜对话正文（后端、带片段），两者搜的东西不同。
 *
 * ★ 这份列表恒为「普通对话」
 *
 * 助理是固定单例（`assistant_main`），不进列表、不在此处管理。
 * 在助理页里侧栏也不会被换成「助理的会话」—— 主界面的消息记录
 * 在任何页面都原样可见，这正是用户要的。
 */

import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import { Check, FileX, ListTodo, Loader2, MoreHorizontal, Pencil, Pin, RefreshCw, Search, Trash2, X } from '@/components/icons';

import { ConversationSkeleton } from '@/components/ConversationSkeleton';
import { searchConversations } from '@/lib/api';
import { useT } from '@/i18n';
import { formatIsoRelative } from '@/lib/format';
import { useAgentStore } from '@/stores/agentStore';
import { sortByPinned, usePinStore } from '@/stores/pinStore';
import { toast } from '@/stores/toastStore';
import { confirmAction, useConfirmStore } from '@/stores/confirmStore';
import type { ConversationSearchHit, ConversationSummary } from '@/types';
import { CONVERSATION_STATUS_LABEL } from '@/types';

/** 该会话最后一次收尾不正常时给出提示文案（收进 tooltip） */
function statusHint(summary: ConversationSummary): string | null {
  const status = summary.last_status;
  if (!status || status === 'ok') return null;
  return CONVERSATION_STATUS_LABEL[status] ?? status;
}

/** 搜索防抖：太短会把每个字符变成一次请求，太长会有「卡住了」的感觉 */
const SEARCH_DEBOUNCE_MS = 280;

/** 菜单与视口的安全间距 */
const MENU_EDGE_PADDING = 8;

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function isAbort(e: unknown): boolean {
  return typeof e === 'object' && e !== null && (e as { name?: string }).name === 'AbortError';
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(value, Math.max(min, max)));
}

interface MenuItem {
  key: string;
  label: string;
  icon: ReactNode;
  danger?: boolean;
  disabled?: boolean;
  /** 在这一项上方画一条分隔线（用来把「删除」和其余分开） */
  sep?: boolean;
  hint?: string;
  onClick: () => void;
}

/**
 * 菜单的锚点。
 *
 * `align: 'end'` 给「…」按钮用（右对齐，菜单不会顶出侧栏右边界），
 * `align: 'start'` 给右键用（左上角跟着光标）。
 */
interface MenuAnchor {
  left: number;
  top: number;
  right: number;
  bottom: number;
  align: 'start' | 'end';
  trigger?: HTMLElement | null;
}

function RowMenu({ anchor, items, onClose }: { anchor: MenuAnchor; items: MenuItem[]; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  // 第一次渲染先摆到屏幕外量一下尺寸，再落到算好的位置 —— 否则会在右下角闪一帧
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const { width, height } = el.getBoundingClientRect();
    const left = anchor.align === 'end' ? anchor.right - width : anchor.left;
    const below = anchor.bottom + 4;
    const fitsBelow = below + height + MENU_EDGE_PADDING <= window.innerHeight;
    setPos({
      left: clamp(left, MENU_EDGE_PADDING, window.innerWidth - width - MENU_EDGE_PADDING),
      top: fitsBelow
        ? below
        : clamp(anchor.top - height - 4, MENU_EDGE_PADDING, window.innerHeight - height - MENU_EDGE_PADDING),
    });
    el.focus();
    return () => {
      if (anchor.trigger?.isConnected && !anchor.trigger.closest('[inert]')) anchor.trigger.focus({ preventScroll: true });
    };
  }, [anchor]);

  // 菜单是 fixed 定位的，列表一滚动它就飘在别处 —— 收起比跟着算位置省事
  useEffect(() => {
    const close = () => onClose();
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    return () => {
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('resize', close);
    };
  }, [onClose]);

  useEffect(() => {
    const onPointerDown = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) onClose();
    };
    document.addEventListener('pointerdown', onPointerDown);
    return () => document.removeEventListener('pointerdown', onPointerDown);
  }, [onClose]);

  return (
    <div
      className="conv-menu"
      ref={ref}
      role="menu"
      tabIndex={-1}
      style={pos ? { left: pos.left, top: pos.top } : { left: -9999, top: -9999, visibility: 'hidden' }}
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.stopPropagation();
          onClose();
        }
      }}
    >
      {items.map((item) => (
        <Fragment key={item.key}>
          {item.sep && <span className="conv-menu__sep" />}
          <button
            type="button"
            role="menuitem"
            className={`conv-menu__item${item.danger ? ' conv-menu__item--danger' : ''}`}
            title={item.hint}
            disabled={item.disabled}
            onClick={() => {
              onClose();
              item.onClick();
            }}
          >
            {item.icon}
            <span>{item.label}</span>
          </button>
        </Fragment>
      ))}
    </div>
  );
}

export function ConversationList({ onSelect }: { onSelect?: () => void }) {
  const t = useT();
  const conversations = useAgentStore((s) => s.conversations);
  const loading = useAgentStore((s) => s.conversationsListLoading);
  const loaded = useAgentStore((s) => s.conversationsLoaded);
  const loadError = useAgentStore((s) => s.conversationsError);
  const reload = () => {
    const state = useAgentStore.getState();
    void (state.bootstrapped ? state.refreshConversations() : state.bootstrap());
  };
  const threadId = useAgentStore((s) => s.threadId);
  const isStreaming = useAgentStore((s) => s.isStreaming);
  const removeConversation = useAgentStore((s) => s.removeConversation);
  const removeConversations = useAgentStore((s) => s.removeConversations);
  const clearAll = useAgentStore((s) => s.clearAllConversations);
  const rename = useAgentStore((s) => s.renameConversation);

  // 列表在任务页 / 工作空间 / 助理页都看得见，点它就得把人带回对话页 ——
  // 否则只是把那一行点亮，聊天区还在原地，看起来像点了没反应。
  // 该回哪个页面由 store 的 openThread 一处决定（命令面板走的是同一个入口）：
  // 这份列表永远是普通对话那批记录，助理是固定单例，不在列表里。
  const selectThread = useAgentStore((s) => s.openThread);
  const openThread = (id: string) => {
    onSelect?.();
    void selectThread(id);
  };

  // 置顶是本地视图偏好（见 pinStore 的说明）—— 不改后端数据，只改这里的排序
  const pinned = usePinStore((s) => s.pinned);
  const togglePin = usePinStore((s) => s.toggle);
  const ordered = sortByPinned(conversations, pinned);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');

  // ---------- 「…」/ 右键菜单 ----------
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [menuAnchor, setMenuAnchor] = useState<MenuAnchor | null>(null);

  // ---------- 多选（null = 不在多选模式）----------
  const [selected, setSelected] = useState<string[] | null>(null);
  const [deleting, setDeleting] = useState(false);

  // ---------- 搜索 ----------
  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<ConversationSearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState('');
  const activeQuery = query.trim();

  // 防抖 + 取消过期请求。打字过程中保留上一批结果直到新结果到达 ——
  // 先清空会让侧栏每敲一个键闪一下白。
  useEffect(() => {
    if (!activeQuery) {
      setHits(null);
      setSearching(false);
      setSearchError('');
      return;
    }

    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setSearching(true);
      searchConversations(activeQuery, { signal: controller.signal })
        .then((res) => {
          setHits(res.results);
          setSearchError('');
        })
        .catch((e) => {
          if (isAbort(e)) return; // 输入变了，这个结果已经没人要了
          setHits([]);
          setSearchError(errorText(e));
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearching(false);
        });
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [activeQuery]);

  // 列表在别处刷新后，正在编辑 / 确认 / 勾选的行可能已经不存在了 —— 不清掉会留悬空态
  useEffect(() => {
    const alive = new Set(conversations.map((c) => c.thread_id));
    if (editingId && !alive.has(editingId)) setEditingId(null);
    if (menuFor && !alive.has(menuFor)) {
      setMenuFor(null);
      setMenuAnchor(null);
    }
    if (selected && selected.some((id) => !alive.has(id))) {
      setSelected(selected.filter((id) => alive.has(id)));
    }
  }, [conversations, editingId, menuFor, selected]);

  // 多选模式下 Esc 退出（菜单自己处理自己的 Esc）
  useEffect(() => {
    if (!selected) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || useConfirmStore.getState().request) return;
      setSelected(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selected]);

  const startEdit = useCallback((summary: ConversationSummary) => {
    setEditingId(summary.thread_id);
    setDraftTitle(summary.title);
  }, []);

  const commitEdit = useCallback(() => {
    if (!editingId) return;
    const title = draftTitle.trim();
    setEditingId(null);
    if (title) void rename(editingId, title);
  }, [draftTitle, editingId, rename]);

  const closeMenu = useCallback(() => {
    setMenuFor(null);
    setMenuAnchor(null);
  }, []);

  const openMenuAt = useCallback((summary: ConversationSummary, anchor: MenuAnchor) => {
    setEditingId(null);
    setMenuFor(summary.thread_id);
    setMenuAnchor(anchor);
  }, []);

  const togglePick = useCallback((id: string) => {
    setSelected((cur) => {
      const list = cur ?? [];
      return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
    });
  }, []);

  const deleteOne = useCallback(async (summary: ConversationSummary) => {
    if (deleting || isStreaming) return;
    if (!await confirmAction({ title: t('feedback.deleteChatTitle'), description: t('feedback.deleteChatDescription', { title: summary.title }) })) return;
    setDeleting(true);
    try {
      await removeConversation(summary.thread_id);
    } finally { setDeleting(false); }
  }, [deleting, isStreaming, removeConversation, t]);

  const deleteSelected = useCallback(async () => {
    if (!selected?.length || deleting || isStreaming) return;
    if (!await confirmAction({ title: t('feedback.deleteChatsTitle', { n: selected.length }), description: t('feedback.deleteChatsDescription') })) return;
    setDeleting(true);
    try {
      const { removed, failed } = await removeConversations(selected);
      const alive = new Set(useAgentStore.getState().conversations.map((item) => item.thread_id));
      setSelected(failed ? selected.filter((id) => alive.has(id)) : null);
      toast(failed ? t('conv.bulkPartial', { removed, failed }) : t('conv.bulkDone', { n: removed }), failed ? 'error' : 'success');
    } finally { setDeleting(false); }
  }, [deleting, isStreaming, removeConversations, selected, t]);

  const clearConversations = useCallback(async () => {
    if (deleting || isStreaming) return;
    if (!await confirmAction({ title: t('feedback.clearChatsTitle'), description: t('feedback.clearChatsDescription', { n: conversations.length }), confirmLabel: t('feedback.clear') })) return;
    setDeleting(true);
    try {
      await clearAll();
    } finally { setDeleting(false); }
  }, [clearAll, conversations.length, deleting, isStreaming, t]);

  const selectMode = selected !== null;
  const pickedCount = selected?.length ?? 0;
  const allPicked = selectMode && conversations.length > 0 && pickedCount === conversations.length;

  const menuConversation = menuFor ? ordered.find((c) => c.thread_id === menuFor) : undefined;
  const menuItems: MenuItem[] = menuConversation
    ? [
        {
          key: 'rename',
          label: t('conv.rename'),
          icon: <Pencil size={14} />,
          onClick: () => startEdit(menuConversation),
        },
        {
          key: 'pin',
          label: pinned.includes(menuConversation.thread_id) ? t('conv.unpin') : t('conv.pin'),
          hint: pinned.includes(menuConversation.thread_id) ? undefined : t('conv.pinHint'),
          icon: <Pin size={14} />,
          onClick: () => togglePin(menuConversation.thread_id),
        },
        {
          key: 'multi',
          label: t('conv.multiSelect'),
          icon: <ListTodo size={14} />,
          onClick: () => setSelected([menuConversation.thread_id]),
        },
        {
          key: 'delete',
          label: t('conv.delete'),
          icon: <Trash2 size={14} />,
          danger: true,
          disabled: deleting || isStreaming,
          hint: isStreaming ? t('feedback.waitForReply') : undefined,
          sep: true,
          onClick: () => void deleteOne(menuConversation),
        },
      ]
    : [];

  return (
    <section className="cl">
      <div className="conv-list__head">
        <span>{t('conv.history')}</span>
        <button
          type="button"
          className="icon-btn"
          aria-label={t('conv.refresh')}
          title={t('conv.refresh')}
          disabled={loading}
          onClick={reload}
        >
          <RefreshCw size={13} className={loading ? 'spin' : undefined} />
        </button>
      </div>
      {/* 搜索框：搜的是对话正文，不是标题 —— 见文件头的说明 */}
      <div className="conv-search">
        <Search size={13} className="conv-search__icon" />
        <input
          className="conv-search__input"
          value={query}
          placeholder={t('conv.search')}
          aria-label={t('conv.searchAria')}
          spellCheck={false}
          onChange={(e) => {
            setQuery(e.target.value);
            // 搜索结果里没有勾选态，多选会变成一个看不见又退不出的状态 —— 一开始搜就退掉
            if (e.target.value.trim()) {
              setSelected(null);
            }
          }}
          onKeyDown={(e) => {
            if (e.key === 'Escape') setQuery('');
          }}
        />
        {searching && <Loader2 size={12} className="spin conv-search__spin" />}
        {activeQuery && (
          <button
            type="button"
            className="icon-btn conv-search__clear"
            onClick={() => setQuery('')}
            title={t('conv.clearSearch')}
          >
            <X size={12} />
          </button>
        )}
      </div>

      {activeQuery ? (
        /* ---------- 搜索结果：带命中片段（这里才需要多行） ---------- */
        <div className="conv-list">
          {searchError && <div className="empty-hint">搜索失败：{searchError}</div>}

          {!searchError && hits && hits.length === 0 && !searching && (
            <div className="empty-hint">{t('conv.searchEmpty', { q: activeQuery })}</div>
          )}

          {hits?.map((hit) => (
            <div
              className={`search-hit${hit.thread_id === threadId ? ' search-hit--active' : ''}`}
              key={hit.thread_id}
            >
              <button
                type="button"
                className="search-hit__head"
                onClick={() => openThread(hit.thread_id)}
                title="打开这个会话"
              >
                <span className="search-hit__title">{hit.title}</span>
                <span className="search-hit__time">{formatIsoRelative(hit.updated_at)}</span>
              </button>

              <div className="search-hit__meta">
                命中 {hit.match_count} 处
                {hit.match_count > hit.matches.length ? `（前 ${hit.matches.length} 处）` : ''}
              </div>

              {hit.matches.map((m) => (
                <button
                  type="button"
                  className="search-hit__row"
                  key={`${m.turn_id}-${m.role}`}
                  onClick={() => openThread(hit.thread_id)}
                  title="打开这个会话"
                >
                  <span className={`search-hit__role search-hit__role--${m.role === 'user' ? 'user' : 'assistant'}`}>
                    {m.role === 'user' ? '你' : '助手'}
                  </span>
                  <span className="search-hit__text">
                    {m.snippet.prefix}
                    <mark>{m.snippet.match}</mark>
                    {m.snippet.suffix}
                  </span>
                </button>
              ))}
            </div>
          ))}
        </div>
      ) : (
        /* ---------- 正常列表：单行，元信息全在 tooltip ---------- */
        <div className="conv-list" aria-busy={loading}>
          {loadError && (
            <div className="conv-list__notice" role="status">
              <div className="conv-list__notice-head">
                <strong>{t('conv.loadFailed')}</strong>
                <button type="button" className="btn btn--ghost btn--sm" onClick={reload} disabled={loading}>
                  {t('conv.retry')}
                </button>
              </div>
              <p>{t(conversations.length ? 'conv.loadFailedWithHistory' : 'conv.loadFailedHint')}</p>
              <details>
                <summary>{t('conv.errorDetails')}</summary>
                <p>{loadError}</p>
              </details>
            </div>
          )}

          {ordered.map((summary) => {
            const active = summary.thread_id === threadId;
            const hint = statusHint(summary);
            const isPinned = pinned.includes(summary.thread_id);
            const picked = selected?.includes(summary.thread_id) ?? false;

            if (editingId === summary.thread_id) {
              return (
                <div className="conv-item conv-item--editing" key={summary.thread_id}>
                  <input
                    className="conv-item__input"
                    value={draftTitle}
                    autoFocus
                    maxLength={40}
                    onChange={(e) => setDraftTitle(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') commitEdit();
                      if (e.key === 'Escape') setEditingId(null);
                    }}
                  />
                  <button type="button" className="icon-btn" onClick={commitEdit} title="保存标题">
                    <Check size={13} />
                  </button>
                  <button type="button" className="icon-btn" onClick={() => setEditingId(null)} title="取消">
                    <X size={13} />
                  </button>
                </div>
              );
            }

            // tooltip 一行说清：标题 + 轮次 + 创建时间 + 异常状态 + 文件缺失
            const tipParts = [summary.title];
            if (hint) tipParts.push(hint);
            if (!summary.exists) tipParts.push(t('conv.deletedFile'));
            tipParts.push(t('conv.turnsTip', { n: summary.turns, time: formatIsoRelative(summary.updated_at) }));

            return (
              <div
                className={`conv-item${active ? ' conv-item--active' : ''}${
                  summary.exists ? '' : ' conv-item--missing'
                }${isPinned ? ' conv-item--pinned' : ''}${menuFor === summary.thread_id ? ' conv-item--menu' : ''}${
                  picked ? ' conv-item--picked' : ''
                }`}
                key={summary.thread_id}
                onContextMenu={(e) => {
                  if (selectMode) return;
                  e.preventDefault();
                  openMenuAt(summary, {
                    trigger: e.currentTarget.querySelector<HTMLElement>('.conv-item__more'),
                    left: e.clientX,
                    top: e.clientY,
                    right: e.clientX,
                    bottom: e.clientY,
                    align: 'start',
                  });
                }}
              >
                {selectMode && (
                  <button
                    type="button"
                    className={`conv-check${picked ? ' conv-check--on' : ''}`}
                    role="checkbox"
                    aria-checked={picked}
                    disabled={deleting}
                    aria-label={summary.title}
                    title={picked ? t('conv.unpick') : t('conv.pick')}
                    onClick={() => togglePick(summary.thread_id)}
                  >
                    {picked && <Check size={11} />}
                  </button>
                )}

                <button
                  type="button"
                  className="conv-item__main"
                  onClick={() => (selectMode ? togglePick(summary.thread_id) : openThread(summary.thread_id))}
                  title={tipParts.join('\n')}
                >
                  {isPinned && <Pin size={11} className="conv-item__pin" />}
                  {!summary.exists && <FileX size={11} style={{ flexShrink: 0, opacity: 0.55 }} />}
                  <span className="conv-item__title">{summary.title}</span>
                  <span className="conv-item__time">{formatIsoRelative(summary.updated_at)}</span>
                </button>

                {!selectMode && (
                  <button
                    type="button"
                    className="icon-btn conv-item__more"
                    title={t('conv.more')}
                    aria-haspopup="menu"
                    onClick={(e) => {
                      const rect = e.currentTarget.getBoundingClientRect();
                      openMenuAt(summary, {
                        trigger: e.currentTarget,
                        left: rect.left,
                        top: rect.top,
                        right: rect.right,
                        bottom: rect.bottom,
                        align: 'end',
                      });
                    }}
                  >
                    <MoreHorizontal size={14} />
                  </button>
                )}
              </div>
            );
          })}

          {conversations.length === 0 && loaded && !loading && !loadError && <div className="empty-hint">{t('conv.empty')}</div>}

          {conversations.length === 0 && !loadError && (loading || !loaded) && (
            <ConversationSkeleton rows={3} label={t('conv.loading')} />
          )}
        </div>
      )}

      {selectMode && !activeQuery && (
        <div className="conv-bar">
          <div className="conv-bar__head">
            <span className="conv-bar__text">
              {t('conv.selected', { n: pickedCount })}
            </span>
            {conversations.length > 0 && (
              <button
                type="button"
                className="conv-bar__link"
                disabled={deleting}
                onClick={() => setSelected(allPicked ? [] : conversations.map((c) => c.thread_id))}
              >
                {allPicked ? t('conv.clearSelection') : t('conv.selectAll')}
              </button>
            )}
          </div>
          <div className="conv-bar__actions">
            <button
              type="button"
              className="btn btn--danger btn--sm"
              disabled={pickedCount === 0 || deleting || isStreaming}
              onClick={() => void deleteSelected()}
            >
              {t('conv.deleteSelected')}
            </button>
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              disabled={deleting}
              onClick={() => setSelected(null)}
            >
              {t('common.cancel')}
            </button>
          </div>
        </div>
      )}

      {!selectMode && !activeQuery && conversations.length > 0 && (
        <div className="conv-list__foot">
          <button
            type="button"
            className="btn btn--ghost btn--sm btn--block"
            onClick={() => void clearConversations()}
            disabled={isStreaming || deleting}
            title={t('conv.clearAll')}
          >
            <Trash2 size={12} />
            {t('conv.clearAll')}
          </button>
        </div>
      )}

      {menuFor && menuAnchor && <RowMenu anchor={menuAnchor} items={menuItems} onClose={closeMenu} />}
    </section>
  );
}
