import { useEffect, useState } from 'react';

import {
  CircleCheck,
  CircleSlash,
  CircleX,
  Hourglass,
  Loader2,
  Play,
  RefreshCw,
  TriangleAlert,
  X,
  type LucideIcon,
} from '@/components/icons';
import { clockOf, formatLocalDateTime } from '@/lib/schedule';
import { runStatusText, triggerText } from '@/lib/workflow';
import type { WorkflowRunDTO, WorkflowRunStatus } from '@/types';

interface WorkflowRunPanelProps {
  runs: WorkflowRunDTO[];
  input: string;
  busy: boolean;
  refreshing: boolean;
  onInputChange: (value: string) => void;
  onRun: () => void;
  onCancel: (runId: string) => void;
  onRefresh: () => void;
}

function runIcon(status: WorkflowRunStatus): LucideIcon {
  switch (status) {
    case 'running':
      return Loader2;
    case 'succeeded':
      return CircleCheck;
    case 'failed':
      return CircleX;
    case 'cancelled':
      return CircleSlash;
    case 'orphaned':
      return TriangleAlert;
    default:
      return Hourglass;
  }
}

function durationText(run: WorkflowRunDTO): string {
  if (!run.started_at) return '';
  const start = new Date(run.started_at).getTime();
  const end = run.finished_at ? new Date(run.finished_at).getTime() : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return '';
  const seconds = Math.round((end - start) / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

export function WorkflowRunPanel({
  runs,
  input,
  busy,
  refreshing,
  onInputChange,
  onRun,
  onCancel,
  onRefresh,
}: WorkflowRunPanelProps) {
  const [selectedId, setSelectedId] = useState('');

  useEffect(() => {
    if (runs.length === 0) {
      if (selectedId) setSelectedId('');
      return;
    }
    if (!runs.some((run) => run.run_id === selectedId)) {
      setSelectedId(runs[0].run_id);
    }
  }, [runs, selectedId]);

  const activeRun = runs.find((run) => run.status === 'running');
  const selected = runs.find((run) => run.run_id === selectedId) ?? runs[0];

  return (
    <div className="wf-runpanel">
      <div className="wf-runpanel__compose">
        <textarea
          className="wf-textarea"
          rows={3}
          value={input}
          placeholder="运行输入（会作为 {{input}}）"
          onChange={(e) => onInputChange(e.target.value)}
        />
        <div className="wf-runpanel__compose-actions">
          {activeRun ? (
            <button
              type="button"
              className="btn btn--sm btn--danger"
              disabled={busy}
              onClick={() => onCancel(activeRun.run_id)}
            >
              <X size={12} />
              取消运行
            </button>
          ) : (
            <button
              type="button"
              className="btn btn--sm btn--primary"
              disabled={busy}
              onClick={onRun}
            >
              {busy ? <Loader2 size={12} className="spin" /> : <Play size={12} />}
              运行
            </button>
          )}
        </div>
      </div>

      <div className="wf-runpanel__head">
        <span>运行记录</span>
        <button
          type="button"
          className="icon-btn"
          onClick={onRefresh}
          title="刷新运行记录"
          disabled={refreshing}
        >
          <RefreshCw size={12} className={refreshing ? 'spin' : undefined} />
        </button>
      </div>

      {runs.length === 0 ? (
        <div className="wf-runpanel__empty">还没有运行过</div>
      ) : (
        <ul className="wf-runlist">
          {runs.map((run) => {
            const Icon = runIcon(run.status as WorkflowRunStatus);
            return (
              <li key={run.run_id}>
                <button
                  type="button"
                  className={`wf-runlist__row${selected?.run_id === run.run_id ? ' is-active' : ''}`}
                  onClick={() => setSelectedId(run.run_id)}
                >
                  <Icon
                    size={13}
                    className={run.status === 'running' ? 'spin' : undefined}
                    aria-hidden
                  />
                  <span className="wf-runlist__time">
                    {clockOf(run.started_at) || formatLocalDateTime(run.started_at) || '—'}
                  </span>
                  <span className={`wf-runlist__status wf-status--${run.status}`}>
                    {runStatusText(run.status)}
                  </span>
                  <span className="wf-runlist__trigger">{triggerText(run.trigger)}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {selected && (
        <div className="wf-rundetail">
          <div className="wf-rundetail__meta">
            <span>{formatLocalDateTime(selected.started_at)}</span>
            <span>{durationText(selected)}</span>
            <span className={`wf-status--${selected.status}`}>{runStatusText(selected.status)}</span>
          </div>

          {selected.input && (
            <div className="wf-rundetail__block">
              <div className="wf-rundetail__label">输入</div>
              <pre className="wf-rundetail__pre">{selected.input}</pre>
            </div>
          )}

          {selected.steps.length > 0 && (
            <ol className="wf-steps">
              {selected.steps.map((step, index) => (
                <li key={`${step.node_id}-${index}`} className={`wf-step wf-step--${step.status}`}>
                  <span className="wf-step__dot" aria-hidden />
                  <div className="wf-step__body">
                    <div className="wf-step__title">
                      {step.title || step.node_id}
                      {step.branch && (
                        <em className="wf-step__branch">{step.branch === 'true' ? '真' : '假'}</em>
                      )}
                    </div>
                    {step.error && <div className="wf-step__error">{step.error}</div>}
                    {step.output && (
                      <details className="wf-step__details">
                        <summary>输出</summary>
                        <pre className="wf-step__pre">{step.output}</pre>
                      </details>
                    )}
                  </div>
                </li>
              ))}
            </ol>
          )}

          {selected.error && (
            <div className="wf-rundetail__error">
              <CircleX size={12} aria-hidden />
              {selected.error}
            </div>
          )}

          {selected.output && (
            <div className="wf-rundetail__block">
              <div className="wf-rundetail__label">最终输出</div>
              <pre className="wf-rundetail__pre">{selected.output}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
