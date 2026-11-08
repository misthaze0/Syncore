/**
 * 用量记录页 —— 每个对话窗口花了多少 token、分别花在哪个模型上。
 *
 * 设计取舍：
 * - 数据源是**用量账本**（`/usage/*`），不再是链路追踪。trace 有 30 天保留期、
 *   内存只留 500 条，用它记账会出现「上周还有 40 万，这周只剩 8 万」且无从解释；
 *   账本只增不删，界面上也就没有任何删除入口（见「说明」区）。
 * - 曲线、卡片、窗口表、明细表共用**同一次 overview 扫描**，只有明细翻页单独请求 ——
 *   四个视图若各算各的，「表格合计 ≠ 曲线最后一天」这种对不上是最难查的 bug。
 * - 窗口表点一行 = 给明细表加一个过滤（不是跳转、不是展开）。含义靠选中态本身。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';

import { RefreshCw } from '@/components/icons';
import { fetchUsageOverview, fetchUsageRecords } from '@/lib/api';
import type { UsageOverviewResponse, UsageRecord, UsageWindowKind } from '@/types';
import { PageHead, Row, Rows, Section, Table } from './kit';
import { UsageTrendChart } from './UsageTrendChart';

/** 时间范围档位。`0` 表示全部历史（后端 `days` 留空） */
const RANGES = [
  { value: 7, label: '7 天' },
  { value: 30, label: '30 天' },
  { value: 90, label: '90 天' },
  { value: 0, label: '全部' },
];

const PAGE_SIZE = 30;

const WINDOW_KIND_LABELS: Record<UsageWindowKind, string> = {
  chat: '会话',
  employee: '员工私聊',
  subtask: '后台任务',
  assistant: '助理会话',
};

/** 用途 → 界面词。取值集合与后端 `app/usage/ledger.py` 的 PURPOSE_* 一致 */
const PURPOSE_LABELS: Record<string, string> = {
  answer: '对话回答',
  subagent: '管家执行',
};

function fmtInt(n: number): string {
  return n.toLocaleString('zh-CN');
}

/** 10月20日 13:38 —— 明细表的时间列 */
function fmtTime(tsMs: number): string {
  const date = new Date(tsMs);
  if (Number.isNaN(date.getTime())) return '—';
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  return `${date.getMonth() + 1}月${date.getDate()}日 ${hh}:${mm}`;
}

/** 耗时 chip：<1min / 9min / 1.2h —— 参照的是「这一笔花了多久」，不是排队多久 */
function fmtDuration(ms: number): string {
  if (!ms || ms < 1000) return '<1s';
  const minutes = ms / 60000;
  if (minutes < 1) return '<1min';
  if (minutes < 60) return `${Math.round(minutes)}min`;
  return `${(minutes / 60).toFixed(1)}h`;
}

function clamp(text: string, max = 26): string {
  const flat = (text || '').replace(/\s+/g, ' ').trim();
  if (!flat) return '—';
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

function fmtBytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
}

export function UsagePage() {
  const [days, setDays] = useState(30);
  const [model, setModel] = useState('');
  const [thread, setThread] = useState('');
  const [overview, setOverview] = useState<UsageOverviewResponse | null>(null);
  const [records, setRecords] = useState<UsageRecord[]>([]);
  const [recordTotal, setRecordTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // days=0（全部）→ 不给后端 days 参数，但 overview 需要一个跨度才能算曲线，
  // 用 3650 天（10 年）当「全部」的实际取值，曲线就是全量分桶。
  const span = days === 0 ? 3650 : days;

  const loadOverview = useCallback(async () => {
    try {
      const resp = await fetchUsageOverview({ days: span, model: model || undefined });
      setOverview(resp);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [span, model]);

  const loadRecords = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await fetchUsageRecords({
        limit: PAGE_SIZE,
        offset: 0,
        days: days === 0 ? undefined : days,
        model: model || undefined,
        threadId: thread || undefined,
      });
      setRecords(resp.records);
      setRecordTotal(resp.total);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [days, model, thread]);

  useEffect(() => {
    void loadOverview();
  }, [loadOverview]);

  useEffect(() => {
    void loadRecords();
  }, [loadRecords]);

  const loadMore = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await fetchUsageRecords({
        limit: PAGE_SIZE,
        offset: records.length,
        days: days === 0 ? undefined : days,
        model: model || undefined,
        threadId: thread || undefined,
      });
      setRecords((prev) => [...prev, ...resp.records]);
      setRecordTotal(resp.total);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [records.length, days, model, thread]);

  const totals = overview?.totals;
  const cards = useMemo(() => {
    if (!totals) return [];
    const share = (n: number) => (totals.total_tokens > 0 ? `${Math.round((n / totals.total_tokens) * 100)}%` : '—');
    return [
      {
        label: '总 Token',
        value: fmtInt(totals.total_tokens),
        sub: `${fmtInt(totals.calls)} 次调用 · ${fmtInt(totals.windows)} 个对话窗口`,
      },
      { label: '输入 Token', value: fmtInt(totals.prompt_tokens), sub: `占比 ${share(totals.prompt_tokens)}` },
      { label: '输出 Token', value: fmtInt(totals.completion_tokens), sub: `占比 ${share(totals.completion_tokens)}` },
      {
        label: '思考 Token',
        value: fmtInt(totals.reasoning_tokens),
        sub: totals.reasoning_tokens > 0 ? `占输出 ${totals.completion_tokens > 0 ? Math.round((totals.reasoning_tokens / totals.completion_tokens) * 100) : 0}%` : '本期没有思考型模型调用',
      },
    ];
  }, [totals]);

  const threadLabel = useMemo(() => {
    if (!thread) return '';
    const hit = overview?.per_window.find((w) => w.thread_id === thread);
    return hit ? hit.label : clamp(thread, 16);
  }, [thread, overview]);

  return (
    <div className="tset tset-page tset-page--wide">
      <PageHead
        title="用量记录"
        hint="只统计对话回答链路；账本只增不删。"
      />

      {error && <div className="alert alert--error">{error}</div>}

      <div className="set-filters">
        <div className="seg" role="group" aria-label="时间范围">
          {RANGES.map((r) => (
            <button
              key={r.value}
              type="button"
              className={`seg__btn${days === r.value ? ' seg__btn--on' : ''}`}
              onClick={() => setDays(r.value)}
            >
              {r.label}
            </button>
          ))}
        </div>
        <div className="set-filters__right">
          <select
            className="select select--sm"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            aria-label="按模型过滤"
          >
            <option value="">全部</option>
            {(overview?.models ?? []).map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => {
              void loadOverview();
              void loadRecords();
            }}
            disabled={loading}
          >
            {loading ? <span className="spinner" /> : <RefreshCw size={13} />} 刷新
          </button>
        </div>
      </div>

      <div className="usage-cards">
        {cards.map((card) => (
          <div key={card.label} className="usage-card">
            <div className="usage-card__label">{card.label}</div>
            <div className="usage-card__value">{card.value}</div>
            <div className="usage-card__sub">{card.sub}</div>
          </div>
        ))}
      </div>

      {/* 未上报是账目缺口，属于必须可见的数据，不塞进 tooltip */}
      {totals && totals.unreported > 0 && (
        <div className="alert alert--warn">
          {`${fmtInt(totals.unreported)} 次调用网关没有返回用量，这些笔记为 0 —— 实际花费不低于本表数字`}
        </div>
      )}

      <Section title="每日趋势">
        {(overview?.per_day.length ?? 0) === 0 ? (
          <Rows>
            <Row label="该时间段暂无记录" />
          </Rows>
        ) : (
          <UsageTrendChart points={overview!.per_day} series={model || '全部模型'} />
        )}
      </Section>

      <Section title="对话窗口用量">
        <Table
          columns={[
            { key: 'window', title: '对话窗口' },
            { key: 'kind', title: '类型', muted: true, width: 92 },
            { key: 'turns', title: '轮次', width: 64, numeric: true },
            { key: 'models', title: '模型' },
            { key: 'total', title: 'Token 合计', width: 110, numeric: true },
            { key: 'last', title: '最近', width: 116, muted: true },
          ]}
          rows={overview?.per_window ?? []}
          rowKey={(w) => w.thread_id}
          render={(row, col) => {
            if (col.key === 'window') {
              const on = thread === row.thread_id;
              return (
                <button
                  type="button"
                  className={on ? 'usage-link usage-link--on' : 'usage-link'}
                  title={row.label}
                  onClick={() => setThread(on ? '' : row.thread_id)}
                >
                  {clamp(row.label)}
                </button>
              );
            }
            if (col.key === 'kind') {
              return <span className="badge badge--muted">{WINDOW_KIND_LABELS[row.window_kind] ?? '会话'}</span>;
            }
            if (col.key === 'turns') return fmtInt(row.turns);
            if (col.key === 'models') {
              return (
                <span className="usage-mchips">
                  {row.models.map((m) => (
                    <span
                      key={m.model}
                      className="usage-mchip"
                      title={`${m.model}：${fmtInt(m.total_tokens)} token`}
                    >
                      <span className="usage-mchip__name">{clamp(m.model, 18)}</span>
                      <span className="usage-mchip__val">{fmtInt(m.total_tokens)}</span>
                    </span>
                  ))}
                </span>
              );
            }
            if (col.key === 'total') return <strong>{fmtInt(row.total_tokens)}</strong>;
            return <span className="usage-nowrap">{fmtTime(row.last_ts_ms ?? 0)}</span>;
          }}
          empty={
            <Rows>
              <Row label="暂无记录" />
            </Rows>
          }
        />
      </Section>

      <Section title="用量明细">
        {thread && (
          <div className="usage-filter">
            <span className="usage-filter__chip">
              {`对话窗口：${clamp(threadLabel, 20)}`}
              <button type="button" className="usage-filter__clear" onClick={() => setThread('')} aria-label="清除窗口筛选">
                ×
              </button>
            </span>
          </div>
        )}
        <Table
          columns={[
            { key: 'time', title: '时间', width: 150 },
            { key: 'window', title: '对话窗口' },
            { key: 'purpose', title: '用途', width: 110 },
            { key: 'model', title: '模型', width: 150 },
            { key: 'prompt', title: '输入', width: 82, numeric: true },
            { key: 'completion', title: '输出', width: 82, numeric: true },
            { key: 'reasoning', title: '思考', width: 82, numeric: true },
            { key: 'total', title: '合计', width: 92, numeric: true },
          ]}
          rows={records}
          rowKey={(r) => r.record_id}
          render={(row, col) => {
            if (col.key === 'time') {
              return (
                <span className="usage-time">
                  {fmtTime(row.ts_ms)}
                  <span className="usage-time__dur" title={row.status === 'ok' ? '本次调用耗时' : '本次调用失败，耗时为已生成部分'}>
                    {fmtDuration(row.duration_ms)}
                  </span>
                </span>
              );
            }
            if (col.key === 'window') {
              return (
                <span title={row.label}>
                  {clamp(row.label)}
                  {row.agent && row.agent !== 'supervisor' && (
                    <span className="usage-win-agent">{` ${clamp(row.agent, 10)}`}</span>
                  )}
                </span>
              );
            }
            if (col.key === 'purpose') return <span className="usage-nowrap">{PURPOSE_LABELS[row.purpose] ?? row.purpose}</span>;
            if (col.key === 'model') return <span className="usage-model">{clamp(row.model, 20)}</span>;
            if (!row.reported) {
              return col.key === 'total' ? (
                <span className="badge badge--muted" title="网关这次没有返回 usage，记为 0">未上报</span>
              ) : (
                <span className="set-table__muted">—</span>
              );
            }
            if (col.key === 'prompt') return fmtInt(row.prompt_tokens);
            if (col.key === 'completion') return fmtInt(row.completion_tokens);
            if (col.key === 'reasoning') {
              return row.reasoning_tokens > 0 ? fmtInt(row.reasoning_tokens) : <span className="set-table__muted">—</span>;
            }
            return <strong>{fmtInt(row.total_tokens)}</strong>;
          }}
          empty={<Rows><Row label="暂无记录" /></Rows>}
        />
        {records.length > 0 && (
          <div className="set-toolbar">
            <span className="set-toolbar__meta">{`已显示 ${fmtInt(records.length)} / ${fmtInt(recordTotal)} 条`}</span>
            {records.length < recordTotal && (
              <button type="button" className="btn btn--sm" onClick={() => void loadMore()} disabled={loading}>
                {loading ? <span className="spinner" /> : null} 加载更多
              </button>
            )}
          </div>
        )}
      </Section>

      <Section title="模型汇总">
        <Table
          columns={[
            { key: 'model', title: '模型' },
            { key: 'calls', title: '调用', width: 80, numeric: true },
            { key: 'prompt', title: '输入', width: 100, numeric: true },
            { key: 'completion', title: '输出', width: 100, numeric: true },
            { key: 'reasoning', title: '思考', width: 100, numeric: true },
            { key: 'total', title: '合计', width: 110, numeric: true },
            { key: 'share', title: '占比', width: 120 },
          ]}
          rows={overview?.per_model ?? []}
          rowKey={(m) => m.model}
          render={(row, col) => {
            if (col.key === 'model') return <span className="usage-nowrap" title={row.model}>{clamp(row.model, 28)}</span>;
            if (col.key === 'calls') return fmtInt(row.calls);
            if (col.key === 'prompt') return fmtInt(row.prompt_tokens);
            if (col.key === 'completion') return fmtInt(row.completion_tokens);
            if (col.key === 'reasoning') {
              return row.reasoning_tokens > 0 ? fmtInt(row.reasoning_tokens) : <span className="set-table__muted">—</span>;
            }
            if (col.key === 'total') return <strong>{fmtInt(row.total_tokens)}</strong>;
            const share = totals && totals.total_tokens > 0 ? row.total_tokens / totals.total_tokens : 0;
            return (
              <span className="usage-share">
                <span className="usage-share__track">
                  <span className="usage-share__fill" style={{ width: `${Math.max(share * 100, 2)}%` }} />
                </span>
                <span className="usage-share__text">{`${Math.round(share * 100)}%`}</span>
              </span>
            );
          }}
          empty={<Rows><Row label="该时间段暂无记录" /></Rows>}
        />
      </Section>

      <div className="set-foot">
        <span>账本</span>
        <code>{overview?.ledger.directory ?? '—'}</code>
        <span>
          {overview
            ? `${fmtInt(overview.ledger.shards)} 个分片 · ${fmtBytes(overview.ledger.total_bytes)}`
            : '—'}
        </span>
        <span
          className="badge badge--accent"
          title="没有删除接口、没有保留期；删掉会话也不会清掉它已花的账，要清理只能停服后手工删文件"
        >
          只增不删
        </span>
      </div>
    </div>
  );
}

export default UsagePage;
