/**
 * 详情弹窗壳（2026-10 重构）—— Linear 设置页范式
 *
 * 遮罩 + 居中对话框；左侧 264px 分组导航（顶部带搜索框），右侧内容区。
 * X / Esc / 点遮罩三种方式关闭。
 *
 * ★ 导航搜索：设置项一多，「找」就是成本 —— 与命令面板同一条理由，
 * 但这里搜的是设置页本身（label + hint 子串匹配，零依赖）。
 * 搜索态下分组标题退场（CSS `.dmodal__nav--searching`），结果是一张平铺清单。
 *
 * ★ 为什么是弹窗而不是独立页面
 *
 * 设置是「对话的旁路操作」：弹窗让对话常驻底层，关掉即回原处，
 * 会话滚动位置与输入框草稿都保得住。
 */

import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

import { Search, X, type LucideIcon } from '@/components/icons';
import { isTopmostModal, useModalFocus } from '@/hooks/useModalFocus';

export interface ModalNavItem {
  key: string;
  icon: LucideIcon;
  label: string;
  hint: string;
  /** 导航搜索用的别名/术语关键词（可选） */
  keywords?: string;
}

export interface ModalNavGroup {
  label: string;
  items: ModalNavItem[];
}

interface DetailModalProps {
  ariaLabel: string;
  groups: ModalNavGroup[];
  activeKey: string;
  onSelect: (key: string) => void;
  onClose: () => void;
  children: ReactNode;
}

export function DetailModal({
  ariaLabel,
  groups,
  activeKey,
  onSelect,
  onClose,
  children,
}: DetailModalProps) {
  const [query, setQuery] = useState('');
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalFocus(dialogRef);

  // Esc 关闭 —— 弹窗的最低礼貌
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !e.defaultPrevented && isTopmostModal(dialogRef.current)) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const filteredGroups = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return groups;
    return groups
      .map((g) => ({
        ...g,
        items: g.items.filter((i) =>
          `${i.label} ${i.hint} ${i.keywords ?? ''}`.toLowerCase().includes(q),
        ),
      }))
      .filter((g) => g.items.length > 0);
  }, [groups, query]);

  const searching = query.trim().length > 0;

  return (
    <div
      className="dmodal-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div ref={dialogRef} className="dmodal" role="dialog" aria-modal="true" aria-label={ariaLabel} tabIndex={-1}>
        <button type="button" className="dmodal__close" onClick={onClose} title="关闭 (Esc)">
          <X size={16} />
        </button>

        <aside className={`dmodal__nav${searching ? ' dmodal__nav--searching' : ''}`}>
          <div className="dmodal__search">
            <Search size={13} />
            <input
              className="dmodal__search-input"
              value={query}
              placeholder="搜索设置"
              spellCheck={false}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape' && query) {
                  e.preventDefault();
                  e.stopPropagation();
                  setQuery('');
                }
              }}
              aria-label="搜索设置项"
            />
          </div>

          {filteredGroups.map((group, i) => (
            // 分组标签是给人看的，不保证唯一（多组故意留空），所以 key 要带上位置：
            // 只按 label 会撞 key，React 可能把其中一组整个丢掉。
            <section key={`${group.label}-${i}`} className="dmodal__group">
              {group.label && <div className="dmodal__group-label">{group.label}</div>}
              {group.items.map((item) => (
                <button
                  type="button"
                  key={item.key}
                  className={`dmodal__nav-item${item.key === activeKey ? ' dmodal__nav-item--active' : ''}`}
                  onClick={() => onSelect(item.key)}
                  title={item.hint}
                >
                  <item.icon size={15} />
                  {item.label}
                </button>
              ))}
            </section>
          ))}

          {searching && filteredGroups.length === 0 && (
            <div className="dmodal__nav-empty">没有匹配的设置项</div>
          )}
        </aside>

        <div className="dmodal__main">
          <div className="dmodal__scroll">{children}</div>
        </div>
      </div>
    </div>
  );
}
