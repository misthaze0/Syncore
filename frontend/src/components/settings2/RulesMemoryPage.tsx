import { confirmAction } from '@/stores/confirmStore';
/**
 * 规则与记忆页 —— 长期记忆（旧 MemoryPanel 的 TRAE 重排）。
 *
 * 设计取舍：
 * - 记忆按 spec 重排：统计条 + 常驻新增行 + 规范表格。失效条目不隐藏 ——
 *   「被否定但可恢复」是长期记忆的重要状态，藏起来用户会觉得系统自作主张。
 * - 注入原文收进折叠区：大多数时候不用看，但它是「模型到底看到了什么」
 *   的唯一可验证出口，必须保留。
 *
 * ★ 助手人设已迁往「设置 → 助理」的人设卡（2026-10）。
 */

import { useCallback, useEffect, useState } from 'react';

import { Pencil, Plus, RotateCcw, Trash2, X } from '@/components/icons';
import {
  clearOwnerMemory,
  createMemory,
  deleteMemory,
  fetchMemory,
  fetchMemoryPromptBlock,
  restoreMemory,
  reinforceMemory,
  updateMemory,
} from '@/lib/api';
import { formatIsoRelative } from '@/lib/format';
import type {
  MemoryItem,
  MemoryListResponse,
  MemoryPromptBlockResponse,
} from '@/types';
import { MEMORY_CATEGORY_LABEL, MEMORY_SOURCE_LABEL } from '@/types';
import { PageHead, Row, Rows, Section, Table, type TableColumn } from './kit';

/** 关系事实的固定 key → 标签与展示顺序（与后端 RELATIONSHIP_KEYS 对齐） */
const OWNER_KEY_LABEL: Record<string, string> = {
  address: '称呼',
  birthday: '生日',
  important_dates: '重要日子',
  routine: '作息',
  likes: '喜好',
  current_focus: '最近在忙',
};
const OWNER_KEY_ORDER = ['address', 'birthday', 'important_dates', 'routine', 'likes', 'current_focus'];
const OWNER_CATEGORIES = new Set(['relationship', 'life']);
const MEMORY_STATE_LABEL = { core: '核心', fresh: '清晰', fading: '淡化', dormant: '沉睡', inactive: '失效' };

function memoryLabel(item: MemoryItem): string {
  if (!item.active) return '失效';
  return item.memory_state ? MEMORY_STATE_LABEL[item.memory_state] : '生效';
}

export function RulesMemoryPage() {
  /* ---------- 记忆 ---------- */
  const [memory, setMemory] = useState<MemoryListResponse | null>(null);
  const [block, setBlock] = useState<MemoryPromptBlockResponse | null>(null);
  const [newValue, setNewValue] = useState('');
  const [newCategory, setNewCategory] = useState('other');
  const [adding, setAdding] = useState(false);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [m, b] = await Promise.all([fetchMemory(), fetchMemoryPromptBlock()]);
      setMemory(m);
      setBlock(b);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onAddMemory = useCallback(async () => {
    const value = newValue.trim();
    if (!value) return;
    setAdding(true);
    setError(null);
    setNotice(null);
    try {
      const res = await createMemory({ value, category: newCategory });
      setNotice(`已${res.action === 'updated' ? '更新' : '添加'}：${res.item.value}`);
      setNewValue('');
      setMemory(await fetchMemory());
      setBlock(await fetchMemoryPromptBlock());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setAdding(false);
    }
  }, [newValue, newCategory]);

  const onDeleteMemory = useCallback(async (item: MemoryItem) => {
    if (!await confirmAction({ title: '忘记这条记忆？', description: `「${item.value}」将被删除，后续对话将不再使用这条记忆。`, confirmLabel: '忘记' })) return;
    setError(null);
    setNotice(null);
    try {
      await deleteMemory(item.key);
      setMemory(await fetchMemory());
      setBlock(await fetchMemoryPromptBlock());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const onRestoreMemory = useCallback(async (item: MemoryItem) => {
    setError(null);
    setNotice(null);
    try {
      await restoreMemory(item.key);
      setNotice(`已恢复「${item.value}」`);
      setMemory(await fetchMemory());
      setBlock(await fetchMemoryPromptBlock());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const onReinforceMemory = useCallback(async (item: MemoryItem) => {
    setError(null);
    setNotice(null);
    try {
      const result = await reinforceMemory(item.key);
      setNotice(result.reinforced ? `已再次确认「${item.value}」` : '刚刚确认过，无需重复');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [load]);

  const onTogglePinned = useCallback(async (item: MemoryItem) => {
    setError(null);
    setNotice(null);
    try {
      await updateMemory(item.key, { value: item.value, pinned: !item.pinned });
      setNotice(item.pinned ? '已取消长期保留' : '已设为长期保留');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [load]);

  const startEdit = useCallback((item: MemoryItem) => {
    setEditingKey(item.key);
    setEditingValue(item.value);
  }, []);

  const onSaveEdit = useCallback(async () => {
    const value = editingValue.trim();
    if (!editingKey || !value) return;
    setError(null);
    setNotice(null);
    try {
      await updateMemory(editingKey, { value });
      setNotice(`已更新：${value}`);
      setEditingKey(null);
      setEditingValue('');
      setMemory(await fetchMemory());
      setBlock(await fetchMemoryPromptBlock());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [editingKey, editingValue]);

  const onClearOwner = useCallback(async () => {
    if (!await confirmAction({ title: '清空个人信息？', description: '称呼、生日、作息等将全部删除，工作偏好会保留。此操作无法撤销。', confirmLabel: '清空' })) return;
    setError(null);
    setNotice(null);
    try {
      const res = await clearOwnerMemory();
      setNotice(`已清空 ${res.cleared} 条个人信息`);
      setEditingKey(null);
      setMemory(await fetchMemory());
      setBlock(await fetchMemoryPromptBlock());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const memoryColumns: TableColumn[] = [
    { key: 'value', title: '内容' },
    { key: 'category', title: '类别', muted: true, width: 90 },
    { key: 'source', title: '来源', muted: true, width: 100 },
    { key: 'status', title: '记忆状态', width: 120 },
    { key: 'actions', title: '操作', width: 240 },
  ];

  const items = memory?.items ?? [];
  const stats = memory?.stats ?? null;
  const activeCount = items.filter((i) => i.active).length;
  const inactiveCount = items.length - activeCount;
  const ownerItems = items
    .filter((i) => i.active && OWNER_CATEGORIES.has(i.category))
    .sort((a, b) => {
      const ai = OWNER_KEY_ORDER.indexOf(a.key);
      const bi = OWNER_KEY_ORDER.indexOf(b.key);
      return (ai < 0 ? OWNER_KEY_ORDER.length : ai) - (bi < 0 ? OWNER_KEY_ORDER.length : bi);
    });

  return (
    <div className="tset tset-page">
      <PageHead title="规则与记忆" />

      <Section title="个人档案">
        {ownerItems.length === 0 ? (
          <div className="empty-hint">暂无个人信息</div>
        ) : (
          <div className="owner-profile">
            {ownerItems.map((item) => (
              <div className="owner-profile__item" key={item.key}>
                <span className="owner-profile__label">
                  {OWNER_KEY_LABEL[item.key] ?? MEMORY_CATEGORY_LABEL[item.category] ?? '关于你'}
                </span>
                {editingKey === item.key ? (
                  <span className="owner-profile__edit">
                    <input
                      className="input"
                      value={editingValue}
                      autoFocus
                      onChange={(e) => setEditingValue(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault();
                          void onSaveEdit();
                        } else if (e.key === 'Escape') {
                          setEditingKey(null);
                        }
                      }}
                    />
                    <button
                      type="button"
                      className="btn btn--sm btn--primary"
                      onClick={() => void onSaveEdit()}
                      disabled={!editingValue.trim()}
                    >
                      保存
                    </button>
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost"
                      onClick={() => setEditingKey(null)}
                      title="取消"
                    >
                      <X size={13} />
                    </button>
                  </span>
                ) : (
                  <>
                    <span className="owner-profile__value" title={item.evidence || undefined}>
                      {item.value}
                    </span>
                    <span className="owner-profile__source">
                      {memoryLabel(item)} · {MEMORY_SOURCE_LABEL[item.source] ?? item.source}
                    </span>
                    <span className="owner-profile__actions">
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost"
                        onClick={() => startEdit(item)}
                        title={`修改「${item.value}」`}
                      >
                        <Pencil size={13} />
                      </button>
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost"
                        onClick={() => void onDeleteMemory(item)}
                        title={`删除「${item.value}」`}
                      >
                        <Trash2 size={13} />
                      </button>
                    </span>
                  </>
                )}
              </div>
            ))}
          </div>
        )}
        {ownerItems.length > 0 && (
          <div className="set-foot">
            <span>{ownerItems.length} 条个人信息</span>
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={() => void onClearOwner()}
            >
              <Trash2 size={13} /> 清空个人信息
            </button>
          </div>
        )}
      </Section>

      <Section title="长期记忆">
        {stats && (
          <div className="stat-strip">
            <span>
              总条数 <b>{stats.total}</b>
            </span>
            <span>
              有效记录 <b>{activeCount}</b>
            </span>
            <span>
              失效 <b>{inactiveCount}</b>
            </span>
            {stats.decay_enabled && <span>沉睡 <b>{stats.by_state?.dormant ?? 0}</b></span>}
            {stats.updated_at && (
              <span>
                最近更新 <b>{formatIsoRelative(stats.updated_at)}</b>
              </span>
            )}
          </div>
        )}

        <Rows>
          <Row
            label="新增偏好"
            control={
              <span style={{ display: 'flex', gap: 8, flex: 1, maxWidth: 460 }}>
                <input
                  className="input"
                  value={newValue}
                  placeholder="例如：写报告时先给结论再给依据"
                  onChange={(e) => setNewValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && newValue.trim()) void onAddMemory();
                  }}
                />
                <select
                  className="select"
                  value={newCategory}
                  onChange={(e) => setNewCategory(e.target.value)}
                >
                  {Object.entries(MEMORY_CATEGORY_LABEL).map(([key, label]) => (
                    <option key={key} value={key}>
                      {label}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="btn btn--sm btn--primary"
                  onClick={() => void onAddMemory()}
                  disabled={adding || !newValue.trim()}
                >
                  {adding ? <span className="spinner" /> : <Plus size={13} />} 添加
                </button>
              </span>
            }
          />
        </Rows>

        {error && <div className="alert alert--error">{error}</div>}
        {notice && <div className="alert alert--info">{notice}</div>}

        <Table
          columns={memoryColumns}
          rows={items}
          rowKey={(item) => item.key}
          render={(item, col) => {
            if (col.key === 'value') {
              return (
                <span className="clamp1" style={{ display: 'block', maxWidth: 380 }} title={item.value}>
                  {item.value}
                </span>
              );
            }
            if (col.key === 'category') {
              return MEMORY_CATEGORY_LABEL[item.category] ?? item.category;
            }
            if (col.key === 'source') {
              return MEMORY_SOURCE_LABEL[item.source] ?? item.source;
            }
            if (col.key === 'status') {
              return item.active ? (
                <span
                  className={`badge ${item.memory_state === 'dormant' ? 'badge--muted' : 'badge--safe'}`}
                  title={item.retention == null ? undefined : `记忆强度 ${Math.round(item.retention * 100)}%；已强化 ${item.reinforcement_count ?? 0} 次`}
                >
                  {memoryLabel(item)}{item.memory_state !== 'core' && item.retention != null ? ` ${Math.round(item.retention * 100)}%` : ''}
                </span>
              ) : (
                <span
                  className="badge badge--muted"
                  title={item.inactive_reason || '已被否定'}
                >
                  失效
                </span>
              );
            }
            return (
              <span className="set-table__actions">
                {item.active && (item.memory_state === 'fading' || item.memory_state === 'dormant') && (
                  <button type="button" className="btn btn--sm" onClick={() => void onReinforceMemory(item)}>
                    再次确认
                  </button>
                )}
                {item.active && item.memory_state && (item.memory_state !== 'core' || item.pinned) && (
                  <button type="button" className="btn btn--sm btn--ghost" onClick={() => void onTogglePinned(item)}>
                    {item.pinned ? '取消保留' : '长期保留'}
                  </button>
                )}
                {!item.active && (
                  <button
                    type="button"
                    className="btn btn--sm"
                    onClick={() => void onRestoreMemory(item)}
                    title="恢复这条记忆并重新注入"
                  >
                    <RotateCcw size={13} /> 恢复
                  </button>
                )}
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  onClick={() => void onDeleteMemory(item)}
                  title={`忘记「${item.value}」`}
                >
                  删除
                </button>
              </span>
            );
          }}
          empty={
            loading ? (
              <div className="empty-hint">
                <span className="spinner" /> 正在加载…
              </div>
            ) : (
              <div className="empty-hint">暂无记忆记录</div>
            )
          }
        />

        <details className="fold">
          <summary className="fold__summary">注入原文</summary>
          <div className="fold__body">
            {block ? (
              block.empty && !block.owner_block ? (
                <div className="empty-hint">当前没有可注入的记忆</div>
              ) : (
                <>
                  <div className="mem-block-group">
                    <div className="mem-block-group__title">偏好</div>
                    {block.empty ? (
                      <div className="empty-hint">没有可注入的工作偏好</div>
                    ) : (
                      <pre className="mem-block">{block.block}</pre>
                    )}
                  </div>
                  <div className="mem-block-group">
                    <div className="mem-block-group__title">关于你</div>
                    {block.owner_block ? (
                      <pre className="mem-block">{block.owner_block}</pre>
                    ) : (
                      <div className="empty-hint">没有关于你的信息</div>
                    )}
                  </div>
                </>
              )
            ) : (
              <div className="empty-hint">加载中…</div>
            )}
          </div>
        </details>

        <div className="set-foot">
          <span>
            注入 {(block?.chars ?? 0) + (block?.owner_chars ?? 0)} 字符
          </span>
          <span>
            偏好上限 {stats?.max_chars ?? '—'} 字符 / {stats?.max_items ?? '—'} 条；关于你上限{' '}
            {stats?.owner_max_items ?? '—'} 条
          </span>
        </div>
      </Section>
    </div>
  );
}

export default RulesMemoryPage;
