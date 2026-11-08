/**
 * Hooks 页 —— 在关键节点跑用户自己的脚本（Phase 5）
 *
 * ★ 这一页以前是样式复刻，现在接的是真接口
 *
 * 后端落在 `data/hooks/hooks.json` + `data/hooks/runs.jsonl`。
 * 清单、开关、增删改、试跑、留痕五件事都在 `/api/hooks` 上。
 *
 * ★ 「试跑」两个字不许骗人
 *
 * 它**真的执行**那条命令 —— 脚本会读写文件、会发请求。
 * 界面上不写"模拟"，并且把这次发给脚本的 stdin JSON 原样列出来：
 * 用户照着它写脚本，不用猜键名。总开关关着时也能试跑（正是要靠它决定开不开），
 * 但结果只回给界面，不参与任何权限判定。
 *
 * ★ 钩子只能收紧
 *
 * 脚本里写 `{"decision":"allow"}` 会被后端丢弃并回一句提示（`ignored_allow`）。
 * 这页必须把那句提示显示出来 —— 如果界面把 allow 显示成「已放行」，
 * 用户就真以为自己配好了豁免。
 */

import { useCallback, useEffect, useState } from 'react';

import { FlaskConical, History, Pencil, Plus, RefreshCw, Trash } from '@/components/icons';

import {
  createHook,
  deleteHook,
  fetchHookRuns,
  fetchHooks,
  setHookEnabled,
  setHooksEnabled,
  testHook,
  updateHook,
} from '@/lib/api';
import type {
  HookEvent,
  HookInfo,
  HookRunRow,
  HookTestResponse,
  HooksResponse,
  HookWritePayload,
} from '@/types';
import { HOOK_EVENT_LABEL } from '@/lib/humanize';

import { PageHead, Row, Rows, Section, Switch, Table } from './kit';

const EXIT_TONE = (code: number, timedOut: boolean): string => {
  if (timedOut) return 'badge badge--danger';
  if (code === 0) return 'badge badge--safe';
  if (code === 2) return 'badge badge--accent';
  return 'badge badge--muted';
};

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function HooksPage() {
  const [data, setData] = useState<HooksResponse | null>(null);
  const [runs, setRuns] = useState<HookRunRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [switching, setSwitching] = useState(false);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<HookWritePayload>({ event: 'PreToolUse', command: '' });
  /** 非 null = 表单正在改这条钩子（不是新建）。改命令不用「删了再建」 */
  const [editing, setEditing] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState<string | null>(null);
  const [trialTool, setTrialTool] = useState('');
  const [testResult, setTestResult] = useState<HookTestResponse | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [hooks, log] = await Promise.all([fetchHooks(), fetchHookRuns(30)]);
      setData(hooks);
      setRuns(log.runs);
    } catch (e) {
      setError(errMsg(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggleGlobal = useCallback(
    async (enabled: boolean) => {
      setSwitching(true);
      try {
        setData(await setHooksEnabled(enabled));
      } catch (e) {
        setError(errMsg(e));
      } finally {
        setSwitching(false);
      }
    },
    [],
  );

  const save = useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      const body: HookWritePayload = { ...draft, command: (draft.command ?? '').trim() };
      if (editing) await updateHook(editing, body);
      else await createHook(body);
      setDraft({ event: draft.event, command: '' });
      setEditing(null);
      setOpen(false);
      setData(await fetchHooks());
    } catch (e) {
      // 后端的校验文案就是给人看的（命令太长、事件不认识…），不再包一层
      setError(errMsg(e));
    } finally {
      setSaving(false);
    }
  }, [draft, editing]);

  const startEdit = useCallback((hook: HookInfo) => {
    setEditing(hook.hook_id);
    setDraft({
      event: hook.event,
      command: hook.command,
      tool: hook.tool,
      timeout: hook.timeout,
      enabled: hook.enabled,
      note: hook.note,
    });
    setOpen(true);
  }, []);

  const toggleOne = useCallback(async (hook: HookInfo, enabled: boolean) => {
    try {
      await setHookEnabled(hook.hook_id, enabled);
      setData(await fetchHooks());
    } catch (e) {
      setError(errMsg(e));
    }
  }, []);

  const remove = useCallback(async (hook: HookInfo) => {
    try {
      await deleteHook(hook.hook_id);
      setData(await fetchHooks());
    } catch (e) {
      setError(errMsg(e));
    }
  }, []);

  const run = useCallback(
    async (hook: HookInfo) => {
      setTesting(hook.hook_id);
      setError(null);
      try {
        const tool = trialTool.trim();
        setTestResult(await testHook(hook.hook_id, tool ? { tool_name: tool } : {}));
        setRuns((await fetchHookRuns(30)).runs);
      } catch (e) {
        setError(errMsg(e));
      } finally {
        setTesting(null);
      }
    },
    [trialTool],
  );

  const events = data?.events ?? [];

  return (
    <div className="tset tset-page">
      <PageHead title="Hooks" />

      <Section title="总开关">
        {error && <div className="alert alert--error">{error}</div>}
        {data === null ? (
          <div className="empty-hint">
            <span className="spinner" /> 正在读取钩子…
          </div>
        ) : (
          <Rows>
            <Row
              label="启用 Hooks"
              desc={`${data.enabled} / ${data.total} 条启用`}
              control={
                <>
                  {switching && <span className="spinner" />}
                  {data.global_enabled && (
                    <span className="badge badge--danger" title="钩子脚本由本机直接执行，不经过审批">
                      本机执行 · 免批准
                    </span>
                  )}
                  <Switch
                    checked={data.global_enabled}
                    disabled={switching}
                    ariaLabel="Hooks 总开关"
                    onChange={(v) => void toggleGlobal(v)}
                  />
                </>
              }
            />
            <Row
              label="脚本的运行环境"
              desc={`工作目录 ${data.runtime.cwd} · 白名单环境变量 · 超时默认 ${data.runtime.default_timeout_seconds}s`}
            />
            <Row label="传参方式" desc={data.runtime.payload} />
          </Rows>
        )}
      </Section>

      <Section title="钩子">
        <div className="stat-strip">
          {events.map((e) => (
            <span key={e.value}>
              {e.label} <b>{data?.stats.by_event[e.value as HookEvent] ?? 0}</b>
              {e.decides ? '（可拦截）' : ''}
            </span>
          ))}
        </div>

        <div className="set-filters">
          <span className="set-filters__meta">{data ? `${data.hooks.length} 条` : ''}</span>
          <div className="set-filters__right">
            <button type="button" className="btn btn--sm btn--primary" onClick={() => setOpen((v) => !v)}>
              <Plus size={13} /> {open ? '收起' : editing ? '编辑中' : '新建钩子'}
            </button>
            <button type="button" className="btn btn--sm btn--ghost" onClick={() => void load()}>
              <RefreshCw size={13} /> 刷新
            </button>
          </div>
        </div>

        {open && (
          <Rows>
            <Row
              label="触发事件"
              control={
                <select
                  className="select"
                  value={draft.event ?? 'PreToolUse'}
                  onChange={(e) => setDraft({ ...draft, event: e.target.value as HookEvent })}
                  title="只有标了「可拦截」的节点能拦下操作，其余节点只能补充与提醒"
                >
                  {events.map((e) => (
                    <option key={e.value} value={e.value}>
                      {e.label}
                      {e.decides ? '（可拦截）' : ''}
                    </option>
                  ))}
                </select>
              }
            />
            <Row
              label="命令"
              control={
                <input
                  className="input"
                  placeholder="例如 python D:/tools/pre_check.py"
                  title="原样执行这一串，不做变量替换；参数从标准输入的 JSON 读。退出码 0 正常、2 拦下这一步、其它视为脚本出错"
                  value={draft.command ?? ''}
                  onChange={(e) => setDraft({ ...draft, command: e.target.value })}
                />
              }
            />
            <Row
              label="限定工具"
              control={
                <input
                  className="input input--narrow"
                  placeholder="如 calendar_*，留空=所有工具"
                  value={draft.tool ?? ''}
                  onChange={(e) => setDraft({ ...draft, tool: e.target.value })}
                />
              }
            />
            <Row
              label="备注"
              control={
                <input
                  className="input input--narrow"
                  placeholder="可选"
                  title="仅作备注，不参与执行"
                  value={draft.note ?? ''}
                  onChange={(e) => setDraft({ ...draft, note: e.target.value })}
                />
              }
            />
          </Rows>
        )}

        {open && (
          <div className="set-toolbar">
            {editing && (
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => {
                  setEditing(null);
                  setDraft({ event: 'PreToolUse', command: '' });
                  setOpen(false);
                }}
              >
                取消
              </button>
            )}
            <button
              type="button"
              className="btn btn--sm btn--primary"
              disabled={saving || !(draft.command ?? '').trim()}
              title={
                (draft.command ?? '').trim()
                  ? undefined
                  : '先填一条要执行的命令'
              }
              onClick={() => void save()}
            >
              {saving && <span className="spinner" />} {editing ? '保存修改' : '保存'}
            </button>
          </div>
        )}

        <Table<HookInfo>
          columns={[
            { key: 'event', title: '节点', width: 110 },
            { key: 'command', title: '命令' },
            { key: 'tool', title: '限定工具', muted: true },
            { key: 'timeout', title: '超时', width: 70, numeric: true },
            { key: 'enabled', title: '启用', width: 70 },
            { key: 'ops', title: '', width: 150 },
          ]}
          rows={data?.hooks ?? []}
          rowKey={(h) => h.hook_id}
          render={(h, c) => {
            if (c.key === 'event') return <span className="badge badge--muted">{h.event_label}</span>;
            if (c.key === 'command') {
              return (
                <span className="hook-cmd clamp1" title={h.note ? `${h.command}\n${h.note}` : h.command}>
                  {h.command}
                </span>
              );
            }
            if (c.key === 'tool') return h.tool || '全部';
            if (c.key === 'timeout') return `${h.effective_timeout}s`;
            if (c.key === 'enabled') {
              return (
                <Switch
                  checked={h.enabled}
                  ariaLabel={`${h.command.slice(0, 24)} 启用`}
                  onChange={(v) => void toggleOne(h, v)}
                />
              );
            }
            return (
              <>
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  title="编辑这条钩子"
                  onClick={() => startEdit(h)}
                >
                  <Pencil size={12} />
                </button>{' '}
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  disabled={testing === h.hook_id}
                  title="试跑这条钩子（真实执行）"
                  onClick={() => void run(h)}
                >
                  {testing === h.hook_id ? <span className="spinner" /> : <FlaskConical size={12} />}
                </button>{' '}
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  title="删除这条钩子"
                  onClick={() => void remove(h)}
                >
                  <Trash size={12} />
                </button>
              </>
            );
          }}
          empty={
            <div className="alert alert--info">暂无钩子</div>
          }
        />
      </Section>

      {testResult && (
        <Section
          title="试跑结果"
          hint="真实执行，结果不参与权限判定；退出码 0 正常、2 拦下这一步、其它视为脚本出错。"
        >
          <Rows>
            <Row
              label="退出码"
              desc={`耗时 ${testResult.duration_ms}ms${testResult.timed_out ? ' · 已超时被杀掉' : ''}`}
              control={
                <span className={EXIT_TONE(testResult.exit_code, testResult.timed_out)}>
                  {testResult.exit_code}
                </span>
              }
            />
            <Row
              label="钩子返回"
              desc={
                testResult.verdict.decision === 'deny'
                  ? `这条操作会被拦下：${testResult.verdict.reason}`
                  : testResult.verdict.decision === 'ask'
                    ? `这一步会强制要你确认：${testResult.verdict.reason}`
                    : testResult.verdict.notice ||
                      (testResult.verdict.context
                        ? '只补进上下文，不表态'
                        : '没表态 —— 退出码 0，且 stdout 里没有可识别的字段')
              }
              control={<span className="badge badge--muted">{HOOK_EVENT_LABEL[testResult.hook.event]}</span>}
            />
            {testResult.verdict.ignored_allow && (
              <Row
                label="被忽略的 allow"
                desc="脚本返回 allow，但钩子不能代为放行，已忽略；放行只能在设置里完成。"
                control={<span className="badge badge--danger">已忽略</span>}
              />
            )}
          </Rows>
          {testResult.stdout && (
            <>
              <p className="set-label">stdout（原样）</p>
              <pre className="hook-raw">{testResult.stdout}</pre>
            </>
          )}
          {testResult.stderr && (
            <>
              <p className="set-label">stderr（原样）</p>
              <pre className="hook-raw">{testResult.stderr}</pre>
            </>
          )}
          <p className="set-label">发给脚本的 JSON</p>
          <pre className="hook-raw">{JSON.stringify(testResult.payload, null, 2)}</pre>
          <Rows>
            <Row
              label="试跑工具名"
              control={
                <input
                  className="input input--narrow"
                  placeholder="read_file"
                  title={
                    testResult.hook.tool
                      ? `这条钩子只匹配 ${testResult.hook.tool}，换个匹配得上的工具名才试得出效果`
                      : '填一个工具名，用它拼一份示例参数发给脚本；留空按 read_file'
                  }
                  value={trialTool}
                  onChange={(e) => setTrialTool(e.target.value)}
                />
              }
            />
          </Rows>
        </Section>
      )}

      <Section title="执行记录">
        <div className="stat-strip">
          <span>
            本次显示 <b>{runs.length}</b>
          </span>
          <span className="hook-cmd">
            日志文件 <b>{data?.paths.run_log ?? '—'}</b>
          </span>
        </div>
        <div className="set-toolbar">
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => void fetchHookRuns(30).then((r) => setRuns(r.runs))}
          >
            <History size={13} /> 重新拉取
          </button>
        </div>
        <Table<HookRunRow>
          columns={[
            { key: 'at', title: '时间', muted: true, width: 150 },
            { key: 'event', title: '节点', width: 100 },
            { key: 'cmd', title: '命令' },
            { key: 'exit', title: '退出码', width: 70, numeric: true },
            { key: 'ms', title: '耗时', width: 80, numeric: true },
            { key: 'say', title: '判定' },
          ]}
          rows={runs}
          rowKey={(r, i) => `${r.logged_at}-${r.hook_id}-${i}`}
          render={(r, c) => {
            if (c.key === 'at') return new Date(r.logged_at).toLocaleTimeString();
            if (c.key === 'event') {
              return (
                <span className="badge badge--muted">
                  {HOOK_EVENT_LABEL[r.event]}
                  {r.test ? ' · 试跑' : ''}
                </span>
              );
            }
            if (c.key === 'cmd') return <span className="hook-cmd clamp1" title={r.command}>{r.command}</span>;
            if (c.key === 'exit') return <span className={EXIT_TONE(r.exit_code, r.timed_out)}>{r.exit_code}</span>;
            if (c.key === 'ms') return `${r.duration_ms}ms`;
            return (
              <span className="clamp1" title={r.reason || r.notice || ''}>
                {r.decision
                  ? `${r.decision}：${r.reason}`
                  : r.notice || (r.timed_out ? '超时' : r.exit_code === 0 ? '不表态' : `脚本自己出错了（${r.reason}）`)}
              </span>
            );
          }}
          empty={<div className="alert alert--info">暂无执行记录</div>}
        />
      </Section>
    </div>
  );
}

export default HooksPage;
