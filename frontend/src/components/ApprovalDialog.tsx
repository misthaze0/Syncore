/**
 * ★ HITL 人工审批弹窗
 *
 * 危险级工具执行前弹出，用户点击「批准」/「拒绝」后通过 POST /api/approvals 回传决策。
 *
 * ★ 为什么主区域是一句话，而不是一坨 JSON
 *
 * 原来弹窗把 `{"path": "demo.txt"}` 直接摆给用户。结果是**用户看不懂自己在批什么**，
 * 只能凭「弹了两次都没事」盲点 —— 审批就从「把关」退化成了「走过场」，
 * 而走过场的审批比没有审批更糟：它给了你一种「有人在看着」的错觉。
 *
 * 所以主区域改成一句人话（由 `lib/humanize.ts` 生成）：「将删除 demo.txt」。
 * 原始参数收进「展开看详情」—— 需要核对细节的人随时能展开，
 * 不需要的人不被它挡住。
 *
 * ⚠️ 这里**只展示**，不做校验：路径合不合法是后端的事（`resolve_in_sandbox`）。
 * 前端写一份路径校验，等于给安全边界养了第二个会漂移的副本。
 */

import { useEffect, useRef, useState } from 'react';

import { ChevronRight, ShieldAlert } from '@/components/icons';

import { useModalFocus, isTopmostModal } from '@/hooks/useModalFocus';
import { describeApprovalRequest } from '@/lib/humanize';

interface ApprovalDialogProps {
  open: boolean;
  toolName: string;
  /** 面向用户的中文名（后端下发）；缺省时用工具名兜底 */
  toolLabel?: string;
  args: Record<string, unknown>;
  reason: string;
  /**
   * 「是谁在请求这次审批」（护栏 6）。
   *
   * 工作空间里一个群组可能同时有三名成员在等审批，而弹窗标题原本只说
   * 「这个操作需要你确认」—— 三条一模一样的弹窗等于让用户盲签：
   * 他批的是「同意删这个文件」，还是「同意**甲**删这个文件」？
   * 主对话没有这一维，留空则整句不显示。
   */
  requestor?: string;
  grantAllowed?: boolean;
  grantMaxUses?: number;
  grantTtlSeconds?: number;
  expiresAt?: number;
  onApprove: (scope?: 'once' | 'same_operation') => void;
  onReject: () => void;
  /** 是否正在提交（禁用按钮防重复点击） */
  submitting?: boolean;
}

export function ApprovalDialog(props: ApprovalDialogProps) {
  return props.open ? <ApprovalDialogBody {...props}/> : null;
}

function ApprovalDialogBody({
  open,
  toolName,
  toolLabel,
  args,
  reason,
  requestor = '',
  onApprove,
  onReject,
  submitting = false,
  grantAllowed = false,
  grantMaxUses = 10,
  grantTtlSeconds = 900,
  expiresAt,
}: ApprovalDialogProps) {
  const [scope, setScope] = useState<'once' | 'same_operation'>('once');
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const expired = Boolean(expiresAt && now >= expiresAt);
  const dialogRef = useRef<HTMLDivElement>(null);
  const rejectRef = useRef<HTMLButtonElement>(null);
  useModalFocus(dialogRef);
  useEffect(() => { rejectRef.current?.focus({ preventScroll: true }); }, []);

  // 默认聚焦拒绝按钮，Tab 保持在弹窗内；提交中不会重复拒绝。
  useEffect(() => {
    if (!open) return;

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting && isTopmostModal(dialogRef.current)) { e.stopImmediatePropagation(); onReject(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onReject, submitting]);

  if (!open) return null;

  const argsJson = JSON.stringify(args ?? {}, null, 2);
  const summary = describeApprovalRequest(toolName, args, toolLabel);

  return (
    <div
      className="modal-mask"
      onClick={(e) => {
        // 点击遮罩视为拒绝，避免误操作直接执行
        if (e.target === e.currentTarget && !submitting) onReject();
      }}
      role="presentation"
    >
      <div ref={dialogRef} tabIndex={-1} className="modal" role="dialog" aria-modal="true" aria-labelledby="approval-title">
        <div className="modal__head">
          <div className="modal__icon">
            <ShieldAlert size={19} />
          </div>
          <div style={{ flex: 1 }}>
            <div className="modal__title" id="approval-title">
              {requestor ? `${requestor} 想执行一个操作` : '这个操作需要你确认'}
            </div>
          </div>
        </div>

        <div className="modal__body">
          {/* 一句话说清「你正在批准什么」 */}
          <div className="modal__summary">{summary}</div>

          {reason && <div className="modal__reason">{reason}</div>}

          <fieldset className="approval-scope" disabled={submitting || expired}>
            <legend>授权范围</legend>
            <label><input type="radio" name="approval-scope" checked={scope === 'once'} onChange={() => setScope('once')} />仅批准本次操作</label>
            {grantAllowed && <label><input type="radio" name="approval-scope" checked={scope === 'same_operation'} onChange={() => setScope('same_operation')} />短时允许相同操作<span>仅当前会话与请求者，相同工具和全部参数。后续最多 {grantMaxUses} 次，{Math.ceil(grantTtlSeconds / 60)} 分钟后到期，可在设置中撤销。</span></label>}
            {!grantAllowed && <p>这一步需要逐次确认。</p>}
          </fieldset>
          {expiresAt && <p className="approval-expiry" role="status">{expired ? '该审批已到期，请等待任务更新。' : `${Math.max(0, Math.ceil(expiresAt - now))} 秒后到期；未批准将跳过操作。`}</p>}

          <details className="modal__details">
            <summary>
              <ChevronRight size={12} className="modal__details-chevron" />
              展开看详情
            </summary>
            <div className="modal__field">
              <div className="modal__field-label">操作</div>
              <div className="modal__field-value">
                {toolLabel && toolLabel !== toolName ? `${toolLabel}（${toolName}）` : toolName}
              </div>
            </div>
            <div className="modal__field">
              <div className="modal__field-label">原始参数</div>
              <div className="modal__field-value">
                {argsJson === '{}' ? '（无参数）' : argsJson}
              </div>
            </div>
          </details>
        </div>

        <div className="modal__foot">
          <button ref={rejectRef} type="button" className="btn" onClick={onReject} disabled={submitting}>
            拒绝执行
          </button>
          <button
            type="button"
            className="btn btn--danger"
            onClick={() => onApprove(grantAllowed ? scope : 'once')}
            disabled={submitting || expired}
          >
            {submitting ? <span className="spinner" /> : null}
            {scope === 'same_operation' && grantAllowed ? '批准并临时授权' : '批准本次'}
          </button>
        </div>
      </div>
    </div>
  );
}
