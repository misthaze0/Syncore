/**
 * 例外规则段 —— 唯一的 allow / ask / deny 规则 CRUD 面板。
 *
 * ★ 为什么是独立文件
 * 规则和「档位 / 留痕」是两件事：档位改的是默认行为，规则改的是个别工具的
 * 覆盖。规则真正的主人现在是「工具治理」页（那里还有按工具一行的快捷下拉），
 * 所以组件抽出来，由那一页直接挂在自己下面 —— 同一份 `rules.json` 不该有
 * 两个编辑入口，否则两边各改各的会让人以为改了就生效。
 *
 * ★ 规则存了立刻生效，没有「应用」按钮
 * 判据在后端：每次判定都读规则表的内存快照，写接口会同步更新它。
 * 「改了但没生效」在安全功能上是最不能接受的状态，所以这里不缓写。
 *
 * ★ 参数匹配是 power-user 路径
 * 留空 = 整个工具；填了才按参数值细分。日常的「这个工具免批准」应当用
 * 工具治理页那一行的下拉去做（它会写成不带参数匹配的整工具规则），
 * 这里是给「只免某类参数」这种细活用的。
 */

import { useCallback, useState } from 'react';

import { Pencil, Plus, Trash } from '@/components/icons';
import {
  createPermissionRule,
  deletePermissionRule,
  setPermissionRuleEnabled,
  updatePermissionRule,
} from '@/lib/api';
import type {
  PermissionRule,
  PermissionRulesResponse,
  RuleBehavior,
  RuleWritePayload,
} from '@/types';
import { Row, Rows, Section, Switch, Table } from './kit';

/** 规则行为 → 颜色。deny 用 danger 是刻意的：列表扫一眼要能看出哪条在挡事 */
export const BEHAVIOR_TONE: Record<RuleBehavior, string> = {
  deny: 'badge badge--danger',
  ask: 'badge badge--accent',
  allow: 'badge badge--safe',
};

const BEHAVIOR_ORDER: RuleBehavior[] = ['deny', 'ask', 'allow'];

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** 表单草稿：没填的字段不提交，交给后端补默认值（arg 由它挑、pattern 空 = 整个工具） */
type RuleDraft = Omit<RuleWritePayload, 'enabled'>;

export function RulesSection({
  data,
  error,
  onChanged,
}: {
  data: PermissionRulesResponse | null;
  error: string | null;
  onChanged: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<RuleDraft>({});
  /** 非 null = 表单正在改这条规则。通配符写错一个字符不该「删了重建」 */
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);

  const save = useCallback(async () => {
    setBusy(true);
    setFormError(null);
    const body: RuleWritePayload = { ...draft, behavior: draft.behavior ?? 'ask', tool: (draft.tool ?? '').trim() };
    try {
      if (editing) await updatePermissionRule(editing, body);
      else await createPermissionRule(body);
      setDraft({ behavior: body.behavior });
      setEditing(null);
      setOpen(false);
      await onChanged();
    } catch (e) {
      // 后端的校验文案就是给人看的（「工具名不能为空…」），不再包一层
      setFormError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }, [draft, editing, onChanged]);

  const startEdit = useCallback((rule: PermissionRule) => {
    setEditing(rule.rule_id);
    setDraft({
      behavior: rule.behavior,
      tool: rule.tool,
      arg: rule.arg,
      pattern: rule.pattern,
      note: rule.note,
    });
    setOpen(true);
  }, []);

  const cancel = useCallback(() => {
    setEditing(null);
    setDraft({});
    setOpen(false);
  }, []);

  const toggle = useCallback(
    async (ruleId: string, enabled: boolean) => {
      try {
        await setPermissionRuleEnabled(ruleId, enabled);
        await onChanged();
      } catch (e) {
        setFormError(errMsg(e));
      }
    },
    [onChanged],
  );

  const remove = useCallback(
    async (ruleId: string) => {
      setRemoving(ruleId);
      try {
        await deletePermissionRule(ruleId);
        await onChanged();
      } catch (e) {
        setFormError(errMsg(e));
      } finally {
        setRemoving(null);
      }
    },
    [onChanged],
  );

  const stats = data?.stats;

  return (
    <Section title="例外规则">
      {error && <div className="alert alert--error">{error}</div>}
      {formError && <div className="alert alert--error">{formError}</div>}

      {data === null ? (
        <div className="empty-hint">
          <span className="spinner" /> 正在读取规则…
        </div>
      ) : (
        <div className="stat-strip">
          <span className="badge badge--danger" title="deny 优先于 allow 与 ask">deny 优先</span>
          <span>
            生效 <b>{stats ? stats.enabled : '—'}</b>
          </span>
          <span>
            已停用 <b>{stats ? stats.total - stats.enabled : '—'}</b>
          </span>
          {BEHAVIOR_ORDER.map((b) => (
            <span key={b}>
              {data.behaviors.find((x) => x.value === b)?.label ?? b}{' '}
              <b>{stats?.by_behavior[b] ?? 0}</b>
            </span>
          ))}
        </div>
      )}

      {data !== null && (
        <div className="set-filters">
          <span className="set-filters__meta">
            {data.rules.length > 0 ? `共 ${data.rules.length} 条` : ''}
          </span>
          <div className="set-filters__right">
            <button type="button" className="btn btn--sm btn--ghost" onClick={open ? cancel : () => setOpen(true)}>
              <Plus size={13} /> {open ? (editing ? '取消编辑' : '收起') : '新增规则'}
            </button>
          </div>
        </div>
      )}

      {open && (
        <>
          <Rows>
            <Row
              label="行为"
              title={data?.behaviors.find((b) => b.value === (draft.behavior ?? 'ask'))?.hint}
              control={
                <select
                  className="select"
                  value={draft.behavior ?? 'ask'}
                  onChange={(e) => setDraft({ ...draft, behavior: e.target.value as RuleBehavior })}
                >
                  {(data?.behaviors ?? []).map((b) => (
                    <option key={b.value} value={b.value}>
                      {b.label}
                    </option>
                  ))}
                </select>
              }
            />
            <Row
              label="工具名"
              control={
                <input
                  className="input input--narrow"
                  placeholder="例如 calendar_delete 或 mcp_notion_*（支持 * 与 ?）"
                  value={draft.tool ?? ''}
                  onChange={(e) => setDraft({ ...draft, tool: e.target.value })}
                />
              }
            />
            <Row
              label="参数匹配"
              control={
                <input
                  className="input input--narrow"
                  placeholder="留空 = 整个工具；例如 *.env"
                  value={draft.pattern ?? ''}
                  title={`${data?.pattern_hint ?? '支持 * 与 ?；留空匹配该工具的整个调用'}；填了才按参数值细分`}
                  onChange={(e) => setDraft({ ...draft, pattern: e.target.value })}
                />
              }
            />
            <Row
              label="说明"
              control={
                <input
                  className="input"
                  placeholder="例如：不许删日历上的任何事"
                  value={draft.note ?? ''}
                  title="拦截时原样回显"
                  onChange={(e) => setDraft({ ...draft, note: e.target.value })}
                />
              }
            />
          </Rows>
          <div className="set-toolbar">
            {editing && (
              <button type="button" className="btn btn--sm btn--ghost" onClick={cancel}>
                取消
              </button>
            )}
            <button
              type="button"
              className="btn btn--sm btn--primary"
              disabled={busy || !(draft.tool ?? '').trim()}
              onClick={() => void save()}
            >
              {busy && <span className="spinner" />} {editing ? '保存修改' : '保存'}
            </button>
          </div>
        </>
      )}

      {data !== null && ((data.rules.length ?? 0) === 0 ? (
        <Rows>
          <Row label="暂无规则" />
        </Rows>
      ) : (
        <Table
          columns={[
            { key: 'behavior', title: '行为', width: 90 },
            { key: 'desc', title: '规则' },
            { key: 'note', title: '说明', muted: true },
            { key: 'enabled', title: '启用', width: 70 },
            { key: 'ops', title: '', width: 100 },
          ]}
          rows={data?.rules ?? []}
          rowKey={(r) => r.rule_id}
          render={(r, c) => {
            if (c.key === 'behavior') {
              return <span className={BEHAVIOR_TONE[r.behavior]}>{r.behavior_label}</span>;
            }
            if (c.key === 'desc') {
              return (
                <span className="clamp1" title={r.behavior_hint}>
                  <code style={{ fontSize: 12 }}>{r.description}</code>
                </span>
              );
            }
            if (c.key === 'note') return <span className="clamp1">{r.note || '—'}</span>;
            if (c.key === 'enabled') {
              return (
                <Switch
                  checked={r.enabled}
                  ariaLabel={`${r.description} 启用`}
                  onChange={(v) => void toggle(r.rule_id, v)}
                />
              );
            }
            return (
              <>
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  title="改这条规则"
                  onClick={() => startEdit(r)}
                >
                  <Pencil size={12} />
                </button>{' '}
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  disabled={removing === r.rule_id}
                  onClick={() => void remove(r.rule_id)}
                >
                  {removing === r.rule_id ? <span className="spinner" /> : <Trash size={12} />}
                </button>
              </>
            );
          }}
        />
      ))}
    </Section>
  );
}

export default RulesSection;
