/**
 * 群组时间线上可操作的审批卡（护栏 6 的界面半边）。
 *
 * ★ 为什么这块非得存在
 * 阶段 2 之前，任务板派发的危险工具**在群组频道上一个事件都没有**：审批只走员工
 * 私聊那条流。后果不是「界面少一块」——用户看不见，就永远批不了，300 秒后被自动
 * 拒绝，而卡片显示成「卡住了」，看上去像员工干砸了。
 *
 * ★ 「是谁在请求」是这一卡片的必填项
 * 群聊里三个员工共用 `thread_id`，`call_id` 是模型给的短 id、并发下会撞。
 * 所以这里同时把 `run_id` / `employee_id` 带回去：少一维后端就匹配不上，
 * 宁可让用户看到「该审批已过期」，也不能让甲点的批准放行乙的 `delete`。
 */

import { useState } from 'react';

import { Check, ShieldAlert, X } from '@/components/icons';
import { useT } from '@/i18n';
import type { GroupApprovalRequest } from '@/types';

interface Props {
  approval: GroupApprovalRequest;
  onDecide: (approved: boolean, scope?: 'once' | 'same_operation') => Promise<void>;
}

/** 入参必须**批准前**看得见，否则用户是在盲签 —— 太长时截断，完整值留给 title */
function describeArgs(args: Record<string, unknown>): string {
  const text = Object.entries(args ?? {})
    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join('  ');
  return text.length > 160 ? `${text.slice(0, 160)}…` : text;
}

export function ApprovalCard({ approval, onDecide }: Props) {
  const t = useT();
  const [busy, setBusy] = useState(false);
  const [remember, setRemember] = useState(false);

  const decide = async (approved: boolean) => {
    if (busy) return;
    setBusy(true);
    try {
      await onDecide(approved, approved && remember && approval.grant_allowed ? 'same_operation' : 'once');
    } finally {
      setBusy(false);
    }
  };

  const args = describeArgs(approval.args);

  return (
    <div className="ap-card">
      <div className="ap-card__head">
        <ShieldAlert size={13} aria-hidden />
        <strong>{t('group.approvalTitle')}</strong>
        {/* 请求者是这一列唯一的区分依据 —— 三条并发弹窗长得一模一样就等于没有 */}
        <span className="ap-card__who">{approval.requestor || approval.employee_id}</span>
      </div>
      <div className="ap-card__body">
        <div className="ap-card__tool" title={approval.tool_name}>
          {approval.tool_label || approval.tool_name || '一个操作'}
        </div>
        {approval.reason && <div className="ap-card__reason">{approval.reason}</div>}
        {args && (
          <div className="ap-card__args" title={JSON.stringify(approval.args)}>
            {args}
          </div>
        )}
      </div>
      <div className="ap-card__actions">
        {approval.grant_allowed && <label className="approval-expiry"><input type="checkbox" checked={remember} disabled={busy} onChange={event => setRemember(event.target.checked)} />临时允许相同操作（最多 {approval.grant_max_uses || 10} 次，{Math.ceil((approval.grant_ttl_seconds || 900) / 60)} 分钟；仅当前会话与请求者）</label>}
        <button
          type="button"
          className="btn btn--sm ap-card__ok"
          disabled={busy}
          onClick={() => void decide(true)}
          title={t('group.approvalNoDelegate')}
        >
          <Check size={12} />
          {t('group.approve')}
        </button>
        <button
          type="button"
          className="btn btn--sm btn--ghost ap-card__no"
          disabled={busy}
          onClick={() => void decide(false)}
        >
          <X size={12} />
          {t('group.reject')}
        </button>
      </div>
    </div>
  );
}
