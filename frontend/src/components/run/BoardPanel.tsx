/**
 * 任务板 —— 状态层的界面投影（批次分组 + 谁在做的 + 卡住的原因）。
 *
 * ★ 为什么是表格而不是拖拽看板
 * 卡片状态由「员工认领 + 执行真相投影」决定，不由鼠标位置决定（施工图 §6）。
 * 做成可拖拽会变成「拖动改了状态，但没人认领、没有 task、没有工件」——
 * 那正是本项目要治的「界面与闸机各信一套」。
 *
 * ★ owner 为空是常态，不是异常
 * 建卡 ≠ 指派：协调者只起草，谁做由员工自己抢。所以「待认领」必须显眼，
 * 否则用户会以为系统在派活，而实际上它在等有人接。
 */

import { useMemo, useState } from 'react';

import { useT } from '@/i18n';
import type { BoardCard, CardStatus, Employee } from '@/types';

const CARD_BADGE: Record<CardStatus, string> = {
  todo: 'bd-card--todo',
  in_progress: 'bd-card--running',
  blocked: 'bd-card--blocked',
  review: 'bd-card--review',
  done: 'bd-card--done',
  cancelled: 'bd-card--cancelled',
};

interface Props {
  cards: BoardCard[];
  employees: Employee[];
  onGoToEmployee?: (employeeId: string) => void;
  onReview?: (cardId: string, accepted: boolean, reason: string) => Promise<void>;
}

export function BoardPanel({ cards, employees, onGoToEmployee, onReview }: Props) {
  const t = useT();
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [rejection, setRejection] = useState('');
  const [reason, setReason] = useState('');
  const review = async (id: string, accepted: boolean) => {
    setBusy(id); setError('');
    try { await onReview?.(id, accepted, reason); setRejection(''); setReason(''); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(''); }
  };

  const names = useMemo(() => {
    const m = new Map<string, string>();
    employees.forEach((e) => m.set(e.id, e.name));
    return m;
  }, [employees]);

  const batches = useMemo(() => {
    const grouped = new Map<number, BoardCard[]>();
    cards.forEach((c) => {
      const list = grouped.get(c.batch) ?? [];
      list.push(c);
      grouped.set(c.batch, list);
    });
    return [...grouped.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([batch, list]) => ({
        batch,
        cards: list.slice().sort((a, b) => (a.updated_at < b.updated_at ? -1 : 1)),
      }));
  }, [cards]);

  if (cards.length === 0) return null;

  return (
    <section className="board">
      {error && <div className="workspace-error" role="alert">{error}</div>}
      <header className="board__head" title={t('group.boardHint')}>
        <strong>{t('group.board')}</strong>
      </header>
      {batches.map(({ batch, cards: list }) => (
        <div key={batch} className="board__batch">
          <div className="board__batch-label">
            {t('group.batch')} {batch + 1}
            {batch > 0 && <span className="board__batch-wait">{t('group.batchWaiting')}</span>}
          </div>
          <table className="board__table">
            <thead>
              <tr>
                <th>{t('group.colTask')}</th>
                <th>{t('group.colStatus')}</th>
                <th>{t('group.colOwner')}</th>
                <th>{t('group.colDepends')}</th>
              </tr>
            </thead>
            <tbody>
              {list.map((c) => (
                <tr key={c.card_id} className={`bd-row bd-row--${c.status}`}>
                  <td className="bd-cell__title">
                    <span className="bd-title">{c.title}</span>
                    {c.blocked_reason && (
                      <span className="bd-reason" title={c.blocked_reason}>
                        {c.blocked_reason}
                      </span>
                    )}
                  </td>
                  <td>
                    <span className={`bd-badge ${CARD_BADGE[c.status] ?? 'bd-card--todo'}`}>
                      {c.status_label}
                    </span>
                    {c.status === 'review' && onReview && <div className="board-review"><button type="button" className="btn btn--sm" disabled={busy === c.card_id} onClick={() => void review(c.card_id, true)}>验收通过</button><button type="button" className="btn btn--sm btn--ghost" disabled={busy === c.card_id} onClick={() => setRejection(c.card_id)}>退回重做</button>{rejection === c.card_id && <div><input aria-label="退回原因" value={reason} onChange={e => setReason(e.target.value)} placeholder="说明需要修改的内容" /><button type="button" className="btn btn--sm" disabled={busy === c.card_id || !reason.trim()} onClick={() => void review(c.card_id, false)}>确认退回</button></div>}</div>}
                  </td>
                  <td>
                    {c.owner_employee_id ? (
                      onGoToEmployee ? (
                        <button
                          type="button"
                          className="bd-owner bd-owner--link"
                          onClick={() => onGoToEmployee(c.owner_employee_id)}
                          title={t('group.openWorkbench')}
                        >
                          {names.get(c.owner_employee_id) ?? c.owner_employee_id}
                        </button>
                      ) : (
                        <span className="bd-owner">
                          {names.get(c.owner_employee_id) ?? c.owner_employee_id}
                        </span>
                      )
                    ) : (
                      <span className="bd-owner bd-owner--none">—</span>
                    )}
                  </td>
                  <td className="bd-deps">
                    {c.depends_on.length > 0
                      ? list
                          .filter((o) => c.depends_on.includes(o.card_id))
                          .map((o) => o.title)
                          .join('、') || `${c.depends_on.length}`
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}
