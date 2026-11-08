import { confirmAction } from '@/stores/confirmStore';
/**
 * 快捷指令页 —— 管理「可插入输入框的问法」
 *
 * 与工作流是两回事：这里存的是**一段话**，选中后填进输入框，用户还能改、
 * 能带附件；它不执行任何步骤。内置几条是产品内容（只读），用户新建的才落盘。
 *
 * 管理入口只有这一页 —— 输入框的 `/` 菜单与命令面板都只读同一份清单。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { Copy, Pencil, Plus, RefreshCw, Trash, Zap } from '@/components/icons';

import {
  createQuickCommand,
  deleteQuickCommand,
  fetchQuickCommands,
  updateQuickCommand,
} from '@/lib/api';
import { toast } from '@/stores/toastStore';
import type { QuickCommand } from '@/types';

import { PageHead, Row, Rows, Section, Table } from './kit';

const MAX_TITLE = 60;
const MAX_BODY = 4000;
const MAX_CATEGORY = 30;

interface Draft {
  /** 空串 = 新建 */
  id: string;
  title: string;
  body: string;
  category: string;
}

const EMPTY_DRAFT: Draft = { id: '', title: '', body: '', category: '通用' };

function errMsg(e: unknown): string {
  return e instanceof Error && e.message ? e.message : String(e);
}

export function QuickCommandsPage() {
  const [commands, setCommands] = useState<QuickCommand[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [error, setError] = useState('');

  const [formOpen, setFormOpen] = useState(false);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);
  const [deletingId, setDeletingId] = useState('');
  const formRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async (silent = false) => {
    if (!silent) setRefreshing(true);
    try {
      const resp = await fetchQuickCommands();
      setCommands(resp.commands);
      setCategories(resp.categories);
      setLoadError('');
    } catch (e) {
      setLoadError(errMsg(e));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const mine = useMemo(() => commands.filter((c) => !c.builtin), [commands]);
  const builtins = useMemo(() => commands.filter((c) => c.builtin), [commands]);

  const editing = Boolean(draft.id);

  const focusForm = useCallback(() => {
    window.setTimeout(() => {
      formRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }, 0);
  }, []);

  const startCreate = useCallback(() => {
    setDraft(EMPTY_DRAFT);
    setError('');
    setFormOpen(true);
    focusForm();
  }, [focusForm]);

  const startEdit = useCallback(
    (command: QuickCommand) => {
      setDraft({
        id: command.id,
        title: command.title,
        body: command.body,
        category: command.category,
      });
      setError('');
      setFormOpen(true);
      focusForm();
    },
    [focusForm],
  );

  const copyBuiltin = useCallback(
    (command: QuickCommand) => {
      const title = `${command.title}（副本）`.slice(0, MAX_TITLE);
      setDraft({ id: '', title, body: command.body, category: command.category });
      setError('');
      setFormOpen(true);
      focusForm();
    },
    [focusForm],
  );

  const closeForm = useCallback(() => {
    setFormOpen(false);
    setDraft(EMPTY_DRAFT);
    setError('');
  }, []);

  const save = useCallback(async () => {
    const title = draft.title.trim();
    const body = draft.body.trim();
    const category = draft.category.trim() || '通用';
    if (!title || !body) {
      setError('标题与内容都不能为空');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const payload = { title, body, category };
      if (draft.id) {
        await updateQuickCommand(draft.id, payload);
        toast('已保存修改', 'success');
      } else {
        await createQuickCommand(payload);
        toast(`已新建「${title}」`, 'success');
      }
      closeForm();
      await load(true);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setSaving(false);
    }
  }, [draft, closeForm, load]);

  const remove = useCallback(
    async (command: QuickCommand) => {
      if (!await confirmAction({ title: '删除这个快捷指令？', description: `「${command.title}」将被永久删除，此操作无法撤销。` })) return;
      setDeletingId(command.id);
      setError('');
      try {
        await deleteQuickCommand(command.id);
        if (draft.id === command.id) closeForm();
        await load(true);
      } catch (e) {
        setError(errMsg(e));
      } finally {
        setDeletingId('');
      }
    },
    [draft.id, closeForm, load],
  );

  return (
    <div className="tset tset-page">
      <PageHead title="快捷指令" />

      <Section title="自定义指令">
        {loadError && <div className="alert alert--error">{loadError}</div>}

        <div className="set-filters">
          <span className="set-filters__meta">
            {loading
              ? ''
              : formOpen
                ? editing
                  ? `正在编辑「${draft.title || '未命名'}」`
                  : '正在新建'
                : `${mine.length} 条自定义 · ${builtins.length} 条预设`}
          </span>
          <div className="set-filters__right">
            <button
              type="button"
              className="btn btn--sm btn--primary"
              onClick={formOpen ? closeForm : startCreate}
            >
              <Plus size={13} /> {formOpen ? '收起' : '新建指令'}
            </button>
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              disabled={refreshing}
              onClick={() => void load()}
            >
              <RefreshCw size={13} className={refreshing ? 'spin' : undefined} /> 刷新
            </button>
          </div>
        </div>

        {formOpen && (
          <div ref={formRef}>
            {error && <div className="alert alert--error">{error}</div>}
            <Rows>
              <Row
                label="标题"
                control={
                  <input
                    className="input qc-form__field"
                    value={draft.title}
                    maxLength={MAX_TITLE}
                    placeholder="例如：审一下这段文字"
                    onChange={(e) => setDraft({ ...draft, title: e.target.value })}
                  />
                }
              />
              <Row
                label="分类"
                control={
                  <>
                    <input
                      className="input qc-form__field"
                      value={draft.category}
                      maxLength={MAX_CATEGORY}
                      list="qc-category-options"
                      placeholder="如：知识问答 / 写作"
                      title="可以填新分类；输入框的 / 菜单按分类展示"
                      onChange={(e) => setDraft({ ...draft, category: e.target.value })}
                    />
                    <datalist id="qc-category-options">
                      {categories.map((c) => (
                        <option key={c} value={c} />
                      ))}
                    </datalist>
                  </>
                }
              />
              <Row
                label="内容"
                stack
                control={
                  <textarea
                    className="persona-form__area"
                    rows={4}
                    value={draft.body}
                    maxLength={MAX_BODY}
                    placeholder="插入输入框的内容，用 {{占位}} 标记待填部分"
                    onChange={(e) => setDraft({ ...draft, body: e.target.value })}
                  />
                }
              />
            </Rows>
            <div className="set-toolbar">
              <button type="button" className="btn btn--sm btn--ghost" onClick={closeForm}>
                取消
              </button>
              <button
                type="button"
                className="btn btn--sm btn--primary"
                disabled={saving || !draft.title.trim() || !draft.body.trim()}
                onClick={() => void save()}
              >
                {saving && <span className="spinner" />} {editing ? '保存修改' : '保存'}
              </button>
            </div>
          </div>
        )}

        <Table<QuickCommand>
          columns={[
            { key: 'title', title: '标题', width: 220 },
            { key: 'category', title: '分类', width: 110, muted: true },
            { key: 'body', title: '内容' },
            { key: 'ops', title: '', width: 96 },
          ]}
          rows={mine}
          rowKey={(c) => c.id}
          render={(c, col) => {
            if (col.key === 'title') {
              return (
                <span className="clamp1" title={c.title}>
                  {c.title}
                </span>
              );
            }
            if (col.key === 'category') return c.category;
            if (col.key === 'body') {
              return (
                <span className="clamp1" title={c.body}>
                  {c.body}
                </span>
              );
            }
            return (
              <>
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  title="编辑"
                  onClick={() => startEdit(c)}
                >
                  <Pencil size={12} />
                </button>{' '}
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  title="删除"
                  disabled={deletingId === c.id}
                  onClick={() => void remove(c)}
                >
                  {deletingId === c.id ? <span className="spinner" /> : <Trash size={12} />}
                </button>
              </>
            );
          }}
          empty={
            loading ? (
              <div className="empty-hint">
                <span className="spinner" /> 正在加载…
              </div>
            ) : loadError ? (
              <div className="empty-hint">指令清单加载失败</div>
            ) : (
              <div className="empty-hint">
                <Zap size={14} /> 暂无自定义指令
              </div>
            )
          }
        />
      </Section>

      <Section title="预设指令">
        <Table<QuickCommand>
          columns={[
            { key: 'title', title: '标题', width: 220 },
            { key: 'category', title: '分类', width: 110, muted: true },
            { key: 'body', title: '内容' },
            { key: 'ops', title: '', width: 96 },
          ]}
          rows={builtins}
          rowKey={(c) => c.id}
          render={(c, col) => {
            if (col.key === 'title') {
              return (
                <span className="clamp1" title={c.title}>
                  {c.title}
                </span>
              );
            }
            if (col.key === 'category') return c.category;
            if (col.key === 'body') {
              return (
                <span className="clamp1" title={c.body}>
                  {c.body}
                </span>
              );
            }
            return (
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                title="复制为自定义指令"
                onClick={() => copyBuiltin(c)}
              >
                <Copy size={12} />
              </button>
            );
          }}
          empty={
            loading ? (
              <div className="empty-hint">
                <span className="spinner" /> 正在加载…
              </div>
            ) : (
              <div className="empty-hint">暂无预设指令</div>
            )
          }
        />
      </Section>
    </div>
  );
}

export default QuickCommandsPage;
