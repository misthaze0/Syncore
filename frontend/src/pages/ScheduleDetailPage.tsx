import { confirmAction } from '@/stores/confirmStore';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import {
  ArrowLeft,
  ChevronRight,
  CircleCheck,
  CircleSlash,
  CircleX,
  Copy,
  Download,
  FileText,
  GitFork,
  Hourglass,
  Loader2,
  Pencil,
  Play,
  RefreshCw,
  Timer,
  Trash,
  TriangleAlert,
  X,
  type LucideIcon,
} from '@/components/icons';

import { ConversationSkeleton } from '@/components/ConversationSkeleton';
import { MarkdownRenderer } from '@/components/MarkdownRenderer';
import { PageShell } from '@/components/PageShell';
import { ScheduleEditor } from '@/components/ScheduleEditor';
import { Switch } from '@/components/settings2/kit';
import { useT } from '@/i18n';
import {
  ApiError,
  cancelScheduleRun,
  conversationFileUrl,
  deleteSchedule,
  fetchScheduleDetail,
  fetchTaskDetail,
  fetchWorkflowRun,
  fetchWorkflows,
  runScheduleNow,
  setScheduleEnabled,
} from '@/lib/api';
import {
  copyTitle,
  dayKeyOf,
  dayLabelOf,
  dayRelativeTo,
  clockOf,
  formatLocalDateTime,
  formatWindow,
  noNextRunHint,
  runStatusText,
} from '@/lib/schedule';
import { isSubtaskStepRunning } from '@/lib/humanize';
import { toast } from '@/stores/toastStore';
import type {
  ScheduledRunDTO,
  ScheduledRunStatus,
  ScheduledTaskDTO,
  SubtaskDTO,
  WorkflowRunDTO,
} from '@/types';

const POLL_MS = 5000;

function runStatusIcon(status: ScheduledRunStatus): LucideIcon {
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

function messageOf(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback;
}

export function ScheduleDetailPage() {
  const t = useT();
  const navigate = useNavigate();
  const { scheduleId = '' } = useParams();

  const [schedule, setSchedule] = useState<ScheduledTaskDTO | null>(null);
  const [runs, setRuns] = useState<ScheduledRunDTO[]>([]);
  const [totalRuns, setTotalRuns] = useState(0);
  const [storage, setStorage] = useState<'ok' | 'unavailable'>('ok');
  const [storageError, setStorageError] = useState('');
  const [missing, setMissing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [workflowName, setWorkflowName] = useState('');

  const load = useCallback(
    async (silent: boolean) => {
      if (!silent) setRefreshing(true);
      try {
        const resp = await fetchScheduleDetail(scheduleId, 50);
        if (resp.storage === 'unavailable') {
          setStorage('unavailable');
          setStorageError(resp.storage_error || t('scheduled.storageHint'));
        } else {
          setStorage('ok');
          setStorageError('');
          setSchedule(resp.schedule ?? null);
          setRuns(resp.runs);
          setTotalRuns(resp.total_runs);
          setMissing(false);
        }
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) {
          setMissing(true);
          setSchedule(null);
        } else {
          setStorage('unavailable');
          setStorageError(messageOf(e, t('scheduled.storageHint')));
        }
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [scheduleId, t],
  );

  useEffect(() => {
    void load(true);
  }, [load]);

  useEffect(() => {
    const workflowId = schedule?.workflow_id;
    if (!workflowId) {
      setWorkflowName('');
      return;
    }
    let alive = true;
    fetchWorkflows()
      .then((resp) => {
        if (!alive || resp.storage !== 'ok') return;
        setWorkflowName(
          resp.workflows.find((item) => item.workflow_id === workflowId)?.name ?? '',
        );
      })
      .catch(() => {
        /* 名字只是展示；拉不到时退回 id */
      });
    return () => {
      alive = false;
    };
  }, [schedule?.workflow_id]);

  const hasRunning = runs.some((r) => r.status === 'running');
  useEffect(() => {
    if (!hasRunning) return;
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    const timer = window.setInterval(() => void load(true), POLL_MS);
    return () => window.clearInterval(timer);
  }, [hasRunning, load]);

  const toggle = async (enabled: boolean) => {
    if (!schedule) return;
    setBusy(true);
    try {
      setSchedule(await setScheduleEnabled(schedule.schedule_id, enabled));
      void load(true);
    } catch (e) {
      toast(messageOf(e, t('scheduled.toggleFailed')), 'error');
    } finally {
      setBusy(false);
    }
  };

  const runNow = async () => {
    if (!schedule) return;
    setBusy(true);
    try {
      const res = await runScheduleNow(schedule.schedule_id);
      if (!res.ok) toast(res.reason || t('scheduled.runFailed'), 'error');
      else toast(t('scheduled.runStarted', { title: schedule.title }), 'success');
      void load(true);
    } catch (e) {
      toast(messageOf(e, t('scheduled.runFailed')), 'error');
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!schedule) return;
    if (!await confirmAction({ title: t('feedback.deleteScheduleTitle'), description: t('scheduled.deleteConfirm', { title: schedule.title }) })) return;
    setBusy(true);
    try {
      await deleteSchedule(schedule.schedule_id);
      toast(t('scheduled.deleted'), 'success');
      navigate('/schedules');
    } catch (e) {
      toast(messageOf(e, t('scheduled.deleteFailed')), 'error');
      setBusy(false);
    }
  };

  const groups = useMemo(() => {
    const out: Array<{ key: string; label: string; relative: string; runs: ScheduledRunDTO[] }> = [];
    for (const run of runs) {
      const key = dayKeyOf(run.started_at);
      const tail = out[out.length - 1];
      if (tail && tail.key === key) tail.runs.push(run);
      else out.push({ key, label: dayLabelOf(run.started_at) || key, relative: dayRelativeTo(run.started_at), runs: [run] });
    }
    return out;
  }, [runs]);

  const head = (
    <div className="sch-head">
      <button type="button" className="sch-back" onClick={() => navigate('/schedules')} title={t('scheduled.back')}>
        <ArrowLeft size={14} />
      </button>
      <span className="sch-head__title">
        <Timer size={14} />
        {schedule?.title ?? t('scheduled.title')}
      </span>
      {schedule && (
        <span className="sch-head__freq">
          {schedule.freq_text || schedule.freq.kind}
          {formatWindow(schedule.start_date, schedule.end_date) && (
            <em>{formatWindow(schedule.start_date, schedule.end_date)}</em>
          )}
        </span>
      )}
    </div>
  );

  const actions = (
    <div className="sch-actions">
      {schedule && (
        <>
          <Switch checked={schedule.enabled} disabled={busy} onChange={(v) => void toggle(v)} ariaLabel={t('scheduled.enable')} />
          <button type="button" className="icon-btn" disabled={busy} onClick={() => void runNow()} title={t('scheduled.runNow')}>
            {busy ? <Loader2 size={14} className="spin" /> : <Play size={14} />}
          </button>
          <button type="button" className="icon-btn" onClick={() => setEditing(true)} title={t('scheduled.edit')}>
            <Pencil size={14} />
          </button>
          <button
            type="button"
            className="icon-btn"
            title={t('scheduled.copy')}
            onClick={() =>
              navigate('/schedules', {
                state: {
                  preset: {
                    title: copyTitle(schedule.title),
                    instruction: schedule.instruction,
                    freq: schedule.freq,
                    kind: schedule.kind ?? 'chat',
                    workflow_id: schedule.workflow_id,
                  },
                },
              })
            }
          >
            <Copy size={14} />
          </button>
          <button type="button" className="icon-btn icon-btn--danger" disabled={busy} onClick={() => void remove()} title={t('scheduled.delete')}>
            <Trash size={14} />
          </button>
        </>
      )}
      <button type="button" className="btn btn--sm btn--ghost" onClick={() => void load(false)} title={t('tasks.refresh')}>
        <RefreshCw size={13} className={refreshing ? 'spin' : undefined} />
      </button>
    </div>
  );

  return (
    <PageShell head={head} actions={actions}>
      {loading && <ConversationSkeleton rows={4} />}

      {!loading && missing && (
        <div className="sch-missing">
          <div className="empty-hint">{t('scheduled.notFound')}</div>
          <button type="button" className="btn btn--sm" onClick={() => navigate('/schedules')}>
            {t('scheduled.back')}
          </button>
        </div>
      )}

      {!loading && storage === 'unavailable' && (
        <div className="subtask-panel subtask-panel--unavailable">
          <TriangleAlert size={14} aria-hidden />
          <span>{storageError || t('scheduled.storageHint')}</span>
        </div>
      )}

      {!loading && schedule && (
        <>
          <section className="sch-summary">
            <div className="sch-summary__row">
              <span className="sch-summary__k">{t('scheduled.nextPrefix')}</span>
              <span className="sch-summary__v">
                {schedule.next_run_at ? formatLocalDateTime(schedule.next_run_at) : noNextRunHint(schedule)}
              </span>
            </div>
            <div className="sch-summary__row">
              <span className="sch-summary__k">{t('scheduled.lastPrefix')}</span>
              <span className="sch-summary__v">
                {schedule.last_run_at
                  ? `${formatLocalDateTime(schedule.last_run_at)}${schedule.last_status ? ` · ${runStatusText(schedule.last_status)}` : ''}`
                  : t('scheduled.neverRun')}
              </span>
            </div>
            <div className="sch-summary__row">
              <span className="sch-summary__k">{t('scheduled.fieldFreq')}</span>
              <span className="sch-summary__v">
                {schedule.freq_text}
                {formatWindow(schedule.start_date, schedule.end_date) && (
                  <> · {formatWindow(schedule.start_date, schedule.end_date)}</>
                )}
              </span>
            </div>
            {schedule.kind === 'workflow' && (
              <div className="sch-summary__row">
                <span className="sch-summary__k">{t('scheduled.fieldWorkflow')}</span>
                <span className="sch-summary__v">
                  <button
                    type="button"
                    className="sch-link"
                    onClick={() => navigate(`/workflows/${schedule.workflow_id}`)}
                  >
                    <GitFork size={12} />
                    {workflowName || schedule.workflow_id || t('scheduled.workflowDeleted')}
                  </button>
                </span>
              </div>
            )}
            <div className="sch-summary__row">
              <span className="sch-summary__k">{t('scheduled.runsTotal')}</span>
              <span className="sch-summary__v">{totalRuns}</span>
            </div>
            <div className="sch-instr">
              <div className="sch-instr__label">
                {schedule.kind === 'workflow' ? t('scheduled.workflowInput') : t('scheduled.instrLabel')}
              </div>
              <p className="sch-instr__text">
                {schedule.instruction || (schedule.kind === 'workflow' ? t('scheduled.noneValue') : '')}
              </p>
            </div>
          </section>

          <h3 className="sch-timeline__title">{t('scheduled.timeline')}</h3>

          {runs.length === 0 ? (
            <div className="empty-hint">{t('scheduled.noRunsYet')}</div>
          ) : (
            <div className="sch-timeline">
              {groups.map((group) => (
                <section className="sch-daygroup" key={`${group.key}-${group.runs[0].run_id}`}>
                  <div className="sch-daygroup__head">
                    {group.relative && <span className="sch-daygroup__rel">{group.relative}</span>}
                    <span>{group.label}</span>
                  </div>
                  {group.runs.map((run) => (
                    <RunItem
                      key={run.run_id}
                      run={run}
                      scheduleId={schedule.schedule_id}
                      workflowId={schedule.workflow_id}
                      onChanged={() => void load(true)}
                    />
                  ))}
                </section>
              ))}
            </div>
          )}
        </>
      )}

      {editing && schedule && (
        <ScheduleEditor
          schedule={schedule}
          onClose={() => setEditing(false)}
          onSaved={(saved) => {
            setSchedule(saved);
            void load(true);
          }}
        />
      )}
    </PageShell>
  );
}

function RunItem({
  run,
  scheduleId,
  workflowId,
  onChanged,
}: {
  run: ScheduledRunDTO;
  scheduleId: string;
  workflowId?: string;
  onChanged: () => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [task, setTask] = useState<SubtaskDTO | null>(null);
  const [workflowRun, setWorkflowRun] = useState<WorkflowRunDTO | null>(null);
  const [taskError, setTaskError] = useState('');
  const [loading, setLoading] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  const Icon = runStatusIcon(run.status as ScheduledRunStatus);
  const spinning = run.status === 'running';
  const canExpand = Boolean(run.task_id || (run.workflow_run_id && workflowId));

  const toggleOpen = async () => {
    const next = !open;
    setOpen(next);
    if (!next || !canExpand) return;
    if (run.task_id) {
      if (task !== null) return;
      setLoading(true);
      try {
        setTask(await fetchTaskDetail(run.task_id));
        setTaskError('');
      } catch (e) {
        setTaskError(messageOf(e, t('scheduled.stepsFailed')));
      } finally {
        setLoading(false);
      }
      return;
    }
    if (!workflowId || !run.workflow_run_id || workflowRun !== null) return;
    setLoading(true);
    try {
      const resp = await fetchWorkflowRun(workflowId, run.workflow_run_id);
      setWorkflowRun(resp.run);
      setTaskError('');
    } catch (e) {
      setTaskError(messageOf(e, t('scheduled.stepsFailed')));
    } finally {
      setLoading(false);
    }
  };

  const cancel = async () => {
    setCancelling(true);
    try {
      const res = await cancelScheduleRun(scheduleId, run.run_id);
      if (!res.ok) toast(res.reason || t('scheduled.cancelFailed'), 'error');
      else toast(t('scheduled.cancelRequested'), 'success');
      onChanged();
    } catch (e) {
      toast(messageOf(e, t('scheduled.cancelFailed')), 'error');
    } finally {
      setCancelling(false);
    }
  };

  return (
    <article className={`sch-item sch-item--${run.status}`}>
      <div className="sch-item__head">
        <Icon size={14} className={spinning ? 'spin' : undefined} aria-hidden />
        <span className="sch-item__clock">{clockOf(run.started_at) || '—'}</span>
        <span className={`sch-item__status sch-item__status--${run.status}`}>{runStatusText(run.status)}</span>
        {run.trigger === 'manual' && <span className="sch-item__trigger">{t('scheduled.manualTrigger')}</span>}
        {spinning && (
          <button type="button" className="btn btn--sm btn--ghost" disabled={cancelling} onClick={() => void cancel()}>
            {cancelling ? <Loader2 size={12} className="spin" /> : <X size={12} />}
            {t('scheduled.cancel')}
          </button>
        )}
        {canExpand && (
          <button
            type="button"
            className="sch-item__steps-toggle"
            onClick={() => void toggleOpen()}
            aria-expanded={open}
          >
            <ChevronRight size={12} className={open ? 'subtask-panel__chevron subtask-panel__chevron--open' : 'subtask-panel__chevron'} />
            {open ? t('scheduled.hideSteps') : t('scheduled.showSteps')}
            {loading && <Loader2 size={11} className="spin" />}
          </button>
        )}
      </div>

      {open && (
        <div className="sch-item__steps">
          {taskError && <div className="sch-item__error">{taskError}</div>}
          {task && task.progress.length === 0 && !taskError && (
            <div className="empty-hint">{t('scheduled.noSteps')}</div>
          )}
          {task && (
            <ol className="subtask-item__steps">
              {task.progress.map((step) => (
                <li key={step.seq} className={`subtask-step subtask-step--${step.status}`}>
                  {isSubtaskStepRunning(step.status) && <Loader2 size={12} className="spin" aria-hidden />}
                  <span className="subtask-step__label">{step.label}</span>
                  {step.detail && <span className="subtask-step__detail">{step.detail}</span>}
                  {step.text && <div className="subtask-step__text">{step.text}</div>}
                </li>
              ))}
            </ol>
          )}
          {workflowRun && workflowRun.steps.length === 0 && !taskError && (
            <div className="empty-hint">{t('scheduled.noSteps')}</div>
          )}
          {workflowRun && workflowRun.steps.length > 0 && (
            <ol className="subtask-item__steps">
              {workflowRun.steps.map((step, index) => (
                <li key={`${step.node_id}-${index}`} className={`subtask-step subtask-step--${step.status}`}>
                  {step.status === 'failed' ? (
                    <CircleX size={12} aria-hidden />
                  ) : (
                    <CircleCheck size={12} aria-hidden />
                  )}
                  <span className="subtask-step__label">{step.title || step.node_id}</span>
                  {step.branch && (
                    <span className="subtask-step__detail">
                      {step.branch === 'true' ? '真' : '假'}
                    </span>
                  )}
                  {step.error && <div className="subtask-step__text">{step.error}</div>}
                </li>
              ))}
            </ol>
          )}
          {task && task.status === 'waiting_approval' && (
            <div className="sch-item__approve">
              <TriangleAlert size={12} aria-hidden />
              {t('scheduled.awaitingApproval')}
            </div>
          )}
        </div>
      )}

      {run.error && <p className="sch-item__error">{run.error}</p>}

      {run.result && (
        <div className="sch-item__result">
          <MarkdownRenderer content={run.result} />
        </div>
      )}

      {run.files && run.files.length > 0 && (
        <ul className="sch-files">
          {run.files.map((file) => (
            <li className="sch-file" key={file.path}>
              <FileText size={13} aria-hidden />
              <span className="sch-file__name" title={file.path}>
                {file.path}
              </span>
              {run.thread_id ? (
                <a className="sch-file__get" href={conversationFileUrl(run.thread_id, file.path)} download>
                  <Download size={12} />
                  {t('scheduled.download')}
                </a>
              ) : (
                <span className="sch-file__lost">{t('scheduled.noThread')}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}
