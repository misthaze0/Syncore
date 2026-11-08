import { confirmAction } from '@/stores/confirmStore';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import {
  ChevronDown,
  CircleCheck,
  CircleSlash,
  CircleX,
  Copy,
  GitFork,
  Hourglass,
  Info,
  ListTodo,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  Timer,
  Trash,
  TriangleAlert,
  X,
  type LucideIcon,
} from '@/components/icons';

import { ConversationSkeleton } from '@/components/ConversationSkeleton';
import { EmptyState } from '@/components/EmptyState';
import { PageShell } from '@/components/PageShell';
import { PageSearch } from '@/components/PageSearch';
import { ScheduleEditor, type SchedulePreset } from '@/components/ScheduleEditor';
import { Switch } from '@/components/settings2/kit';
import { useT } from '@/i18n';
import {
  deleteSchedule,
  fetchScheduleRuns,
  fetchSchedules,
  fetchWorkflows,
  runScheduleNow,
  setScheduleEnabled,
} from '@/lib/api';
import {
  SCHEDULE_TEMPLATES,
  copyTitle,
  formatLocalDateTime,
  formatWindow,
  noNextRunHint,
  runStatusText,
  type ScheduleTemplate,
} from '@/lib/schedule';
import { toast } from '@/stores/toastStore';
import type {
  ScheduledRunDTO,
  ScheduledRunStatus,
  ScheduledTaskDTO,
  SchedulerStatus,
} from '@/types';

type Tab = 'tasks' | 'runs';
type Filter = 'all' | 'enabled' | 'paused';

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

export function SchedulesPage() {
  const t = useT();
  const navigate = useNavigate();
  const location = useLocation();
  const incomingPreset = (location.state as { preset?: SchedulePreset } | null)?.preset;

  const [tab, setTab] = useState<Tab>('tasks');
  const [schedules, setSchedules] = useState<ScheduledTaskDTO[]>([]);
  const [runs, setRuns] = useState<ScheduledRunDTO[]>([]);
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null);
  const [storage, setStorage] = useState<'ok' | 'unavailable'>('ok');
  const [storageError, setStorageError] = useState('');

  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [busyId, setBusyId] = useState('');

  const [editor, setEditor] = useState<{ schedule?: ScheduledTaskDTO; preset?: SchedulePreset } | null>(
    incomingPreset ? { preset: incomingPreset } : null,
  );
  const [workflowNames, setWorkflowNames] = useState<Record<string, string>>({});
  const [showTemplates, setShowTemplates] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [batch, setBatch] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);

  const load = useCallback(
    async (silent: boolean) => {
      if (!silent) setRefreshing(true);
      try {
        const resp = await fetchSchedules();
        if (resp.storage === 'unavailable') {
          setStorage('unavailable');
          setStorageError(resp.storage_error || t('scheduled.storageHint'));
          setScheduler(resp.scheduler ?? null);
        } else {
          setStorage('ok');
          setStorageError('');
          setSchedules(resp.schedules);
          setScheduler(resp.scheduler ?? null);
        }
        if (tab === 'runs') {
          const r = await fetchScheduleRuns();
          if (r.storage === 'unavailable') {
            setStorage('unavailable');
            setStorageError(r.storage_error || t('scheduled.storageHint'));
          } else {
            setRuns(r.runs);
          }
        }
      } catch (e) {
        setStorage('unavailable');
        setStorageError(messageOf(e, t('scheduled.storageHint')));
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [tab, t],
  );

  useEffect(() => {
    void load(true);
  }, [load]);

  useEffect(() => {
    let alive = true;
    fetchWorkflows()
      .then((resp) => {
        if (!alive || resp.storage !== 'ok') return;
        setWorkflowNames(
          Object.fromEntries(resp.workflows.map((item) => [item.workflow_id, item.name])),
        );
      })
      .catch(() => {
        /* 名字只是展示；拉不到时卡片退回工作流 id */
      });
    return () => {
      alive = false;
    };
  }, []);

  const hasRunning = useMemo(
    () =>
      schedules.some((s) => s.last_status === 'running') ||
      runs.some((r) => r.status === 'running'),
    [schedules, runs],
  );

  useEffect(() => {
    if (!hasRunning) return;
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    const timer = window.setInterval(() => void load(true), POLL_MS);
    return () => window.clearInterval(timer);
  }, [hasRunning, load]);

  useEffect(() => {
    if (!menuOpen) return;
    const onDown = () => setMenuOpen(false);
    window.addEventListener('click', onDown);
    return () => window.removeEventListener('click', onDown);
  }, [menuOpen]);

  const counts = useMemo(
    () => ({
      all: schedules.length,
      enabled: schedules.filter((s) => s.enabled).length,
      paused: schedules.filter((s) => !s.enabled).length,
    }),
    [schedules],
  );

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return schedules.filter((s) => {
      if (filter === 'enabled' && !s.enabled) return false;
      if (filter === 'paused' && s.enabled) return false;
      if (!needle) return true;
      return (
        s.title.toLowerCase().includes(needle) ||
        s.instruction.toLowerCase().includes(needle) ||
        (s.freq_text ?? '').toLowerCase().includes(needle)
      );
    });
  }, [schedules, filter, query]);

  const toggle = async (schedule: ScheduledTaskDTO, enabled: boolean) => {
    setBusyId(schedule.schedule_id);
    try {
      const next = await setScheduleEnabled(schedule.schedule_id, enabled);
      setSchedules((prev) => prev.map((s) => (s.schedule_id === next.schedule_id ? next : s)));
    } catch (e) {
      toast(messageOf(e, t('scheduled.toggleFailed')), 'error');
    } finally {
      setBusyId('');
    }
  };

  const runNow = async (schedule: ScheduledTaskDTO) => {
    setBusyId(schedule.schedule_id);
    try {
      const res = await runScheduleNow(schedule.schedule_id);
      if (!res.ok) {
        toast(res.reason || t('scheduled.runFailed'), 'error');
      } else {
        toast(t('scheduled.runStarted', { title: schedule.title }), 'success');
        void load(true);
      }
    } catch (e) {
      toast(messageOf(e, t('scheduled.runFailed')), 'error');
    } finally {
      setBusyId('');
    }
  };

  const remove = async (schedule: ScheduledTaskDTO) => {
    if (!await confirmAction({ title: t('feedback.deleteScheduleTitle'), description: t('scheduled.deleteConfirm', { title: schedule.title }) })) return;
    setBusyId(schedule.schedule_id);
    try {
      await deleteSchedule(schedule.schedule_id);
      setSchedules((prev) => prev.filter((s) => s.schedule_id !== schedule.schedule_id));
      setPicked((prev) => prev.filter((id) => id !== schedule.schedule_id));
    } catch (e) {
      toast(messageOf(e, t('scheduled.deleteFailed')), 'error');
    } finally {
      setBusyId('');
    }
  };

  const duplicate = (schedule: ScheduledTaskDTO) => {
    setEditor({
      preset: {
        title: copyTitle(schedule.title),
        instruction: schedule.instruction,
        freq: schedule.freq,
        kind: schedule.kind ?? 'chat',
        workflow_id: schedule.workflow_id,
      },
    });
  };

  const batchApply = async (action: 'enabled' | 'paused' | 'delete') => {
    const targets = schedules.filter((s) => picked.includes(s.schedule_id));
    if (targets.length === 0) return;
    if (action === 'delete') {
      if (!await confirmAction({ title: t('feedback.deleteSchedulesTitle', { n: targets.length }), description: t('scheduled.deleteMany', { n: targets.length }) })) return;
    }
    setBusyId('batch');
    let failed = 0;
    for (const target of targets) {
      try {
        if (action === 'delete') {
          await deleteSchedule(target.schedule_id);
          setSchedules((prev) => prev.filter((s) => s.schedule_id !== target.schedule_id));
        } else {
          const next = await setScheduleEnabled(target.schedule_id, action === 'enabled');
          setSchedules((prev) => prev.map((s) => (s.schedule_id === next.schedule_id ? next : s)));
        }
      } catch {
        failed += 1;
      }
    }
    setBusyId('');
    setPicked([]);
    toast(
      failed === 0
        ? t('scheduled.batchDone', { n: targets.length })
        : t('scheduled.batchPartly', { ok: targets.length - failed, failed }),
      failed === 0 ? 'success' : 'error',
    );
  };

  const tabs = (
    <div className="page-tabs" role="tablist" aria-label={t('scheduled.title')}>
      <button
        type="button"
        role="tab"
        aria-selected={tab === 'tasks'}
        onClick={() => setTab('tasks')}
      >
        {t('scheduled.tabTasks')}
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={tab === 'runs'}
        onClick={() => setTab('runs')}
      >
        {t('scheduled.tabRuns')}
      </button>
    </div>
  );

  const actions = (
    <>
      <button
        type="button"
        className="btn btn--sm btn--ghost"
        onClick={() => void load(false)}
        disabled={refreshing}
        title={t('tasks.refresh')}
      >
        <RefreshCw size={13} className={refreshing ? 'spin' : undefined} />
        {t('tasks.refresh')}
      </button>
      <div className="sch-new">
        <button
          type="button"
          className="btn btn--sm btn--primary"
          onClick={() => setEditor({})}
          title={t('scheduled.newManual')}
        >
          <Plus size={13} />
          {t('scheduled.new')}
        </button>
        <button
          type="button"
          className={`btn btn--sm btn--primary sch-new__caret${menuOpen ? ' is-open' : ''}`}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={(e) => {
            e.stopPropagation();
            setMenuOpen((v) => !v);
          }}
          title={t('scheduled.newMore')}
        >
          <ChevronDown size={13} />
        </button>
        {menuOpen && (
          <div className="sch-new__menu" role="menu">
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setMenuOpen(false);
                setEditor({});
              }}
            >
              {t('scheduled.newManual')}
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setMenuOpen(false);
                setShowTemplates(true);
              }}
            >
              {t('scheduled.newFromTemplate')}
            </button>
          </div>
        )}
      </div>
    </>
  );

  return (
    <PageShell
      title={t('scheduled.title')}
      icon={Timer}
      meta={scheduler?.enabled && scheduler.running && scheduler.consecutive_failures === 0 && <span className="sch-live" title={t('scheduled.schedLiveTip')}><i className="sch-live__dot" aria-hidden />{t('scheduled.schedLive')}</span>}
      actions={actions}
      toolbar={<>{tabs}{tab === 'tasks' && <PageSearch value={query} onChange={setQuery} placeholder={t('scheduled.searchPlaceholder')} />}</>}
    >
      {storage === 'unavailable' && (
        <div className="subtask-panel subtask-panel--unavailable">
          <TriangleAlert size={14} aria-hidden />
          <span>{storageError || t('scheduled.storageHint')}</span>
        </div>
      )}

      {storage !== 'unavailable' && scheduler && <SchedulerNotice status={scheduler} />}

      {loading && <ConversationSkeleton rows={3} label={t('common.loading')} />}

      {!loading && tab === 'tasks' && (
        <>
          <div className="sch-filters">
            {(
              [
                ['all', `${t('scheduled.filterAll')} ${counts.all}`],
                ['enabled', `${t('scheduled.filterEnabled')} ${counts.enabled}`],
                ['paused', `${t('scheduled.filterPaused')} ${counts.paused}`],
              ] as Array<[Filter, string]>
            ).map(([key, label]) => (
              <button
                type="button"
                key={key}
                className={`sch-filter${filter === key ? ' is-active' : ''}`}
                aria-pressed={filter === key}
                onClick={() => setFilter(key)}
              >
                {label}
              </button>
            ))}
            <button
              type="button"
              className={`sch-filter sch-filter--batch${batch ? ' is-active' : ''}`}
              aria-pressed={batch}
              onClick={() => {
                setBatch((v) => !v);
                setPicked([]);
              }}
            >
              {t('scheduled.batch')}
            </button>
          </div>

          {visible.length === 0 ? (
            counts.all === 0 ? (
              <EmptyState
                fill
                icon={Timer}
                title={t('scheduled.empty')}
                action={<>
                  <button type="button" className="btn btn--primary btn--sm" onClick={() => setEditor({})}>
                    <Plus size={13} />
                    {t('scheduled.new')}
                  </button>
                  <button type="button" className="btn btn--sm" onClick={() => setShowTemplates(true)}>
                    {t('scheduled.newFromTemplate')}
                  </button>
                </>}
              />
            ) : (
              <EmptyState fill icon={Timer} title={t('scheduled.noMatch')} />
            )
          ) : (
            <div className="sch-grid-cards">
              {visible.map((schedule) => (
                <ScheduleCard
                  key={schedule.schedule_id}
                  schedule={schedule}
                  busy={busyId === schedule.schedule_id}
                  batch={batch}
                  picked={picked.includes(schedule.schedule_id)}
                  workflowName={
                    schedule.workflow_id ? workflowNames[schedule.workflow_id] ?? '' : ''
                  }
                  onPick={(v) =>
                    setPicked((prev) => (v ? [...prev, schedule.schedule_id] : prev.filter((id) => id !== schedule.schedule_id)))
                  }
                  onOpen={() => navigate(`/schedules/${schedule.schedule_id}`)}
                  onToggle={(enabled) => void toggle(schedule, enabled)}
                  onRun={() => void runNow(schedule)}
                  onCopy={() => duplicate(schedule)}
                  onDelete={() => void remove(schedule)}
                />
              ))}
            </div>
          )}

          {batch && picked.length > 0 && (
            <div className="sch-batchbar">
              <span>{t('scheduled.batchSelected', { n: picked.length })}</span>
              <button type="button" className="btn btn--sm" disabled={busyId === 'batch'} onClick={() => void batchApply('enabled')}>
                {t('scheduled.batchEnable')}
              </button>
              <button type="button" className="btn btn--sm" disabled={busyId === 'batch'} onClick={() => void batchApply('paused')}>
                {t('scheduled.batchPause')}
              </button>
              <button type="button" className="btn btn--sm btn--danger" disabled={busyId === 'batch'} onClick={() => void batchApply('delete')}>
                <Trash size={12} />
                {t('scheduled.batchDelete')}
              </button>
              <button type="button" className="btn btn--sm btn--ghost" onClick={() => setPicked([])}>
                {t('scheduled.batchClear')}
              </button>
            </div>
          )}
        </>
      )}

      {!loading && tab === 'runs' && (
        <>
          {runs.length === 0 ? (
            <EmptyState fill icon={ListTodo} title={t('scheduled.noRuns')} />
          ) : (
            <ul className="sch-runs">
              {runs.map((run) => (
                <li key={run.run_id}>
                  <RunRow
                    run={run}
                    onOpen={() => navigate(`/schedules/${run.schedule_id}`)}
                  />
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      {editor && (
        <ScheduleEditor
          schedule={editor.schedule}
          preset={editor.preset}
          onClose={() => setEditor(null)}
          onSaved={(saved) =>
            setSchedules((prev) => {
              const exists = prev.some((s) => s.schedule_id === saved.schedule_id);
              if (exists) return prev.map((s) => (s.schedule_id === saved.schedule_id ? saved : s));
              return [saved, ...prev];
            })
          }
        />
      )}

      {showTemplates && (
        <TemplatePicker
          onClose={() => setShowTemplates(false)}
          onPick={(template) => {
            setShowTemplates(false);
            setEditor({ preset: template });
          }}
        />
      )}
    </PageShell>
  );
}

function SchedulerNotice({ status }: { status: SchedulerStatus }) {
  const t = useT();
  if (status.enabled && !status.running) {
    return (
      <div className="sch-notice sch-notice--warn">
        <TriangleAlert size={13} aria-hidden />
        <span>{t('scheduled.schedStopped')}</span>
      </div>
    );
  }
  if (status.consecutive_failures > 0) {
    return (
      <div className="sch-notice sch-notice--error">
        <TriangleAlert size={13} aria-hidden />
        <span>
          {t('scheduled.schedFailing', { n: status.consecutive_failures })}
          {status.last_error ? `：${status.last_error}` : ''}
        </span>
      </div>
    );
  }
  if (!status.enabled) {
    return (
      <div className="sch-notice">
        <Info size={13} aria-hidden />
        <span>{t('scheduled.schedOff')}</span>
      </div>
    );
  }
  return null;
}

function ScheduleCard({
  schedule,
  busy,
  batch,
  picked,
  workflowName,
  onPick,
  onOpen,
  onToggle,
  onRun,
  onCopy,
  onDelete,
}: {
  schedule: ScheduledTaskDTO;
  busy: boolean;
  batch: boolean;
  picked: boolean;
  workflowName: string;
  onPick: (v: boolean) => void;
  onOpen: () => void;
  onToggle: (enabled: boolean) => void;
  onRun: () => void;
  onCopy: () => void;
  onDelete: () => void;
}) {
  const t = useT();
  const window_ = formatWindow(schedule.start_date, schedule.end_date);
  const last = schedule.last_status ? runStatusText(schedule.last_status) : '';

  return (
    <article className={`sch-card${schedule.enabled ? '' : ' sch-card--paused'}`}>
      <div className="sch-card__head">
        {batch && (
          <input
            type="checkbox"
            className="sch-card__check"
            checked={picked}
            onChange={(e) => onPick(e.target.checked)}
            aria-label={schedule.title}
          />
        )}
        <button type="button" className="sch-card__title" onClick={onOpen} title={t('scheduled.openTip')}>
          {schedule.title}
        </button>
        <span className="sch-card__switch">
          <Switch
            checked={schedule.enabled}
            disabled={busy}
            onChange={(v) => onToggle(v)}
            ariaLabel={schedule.enabled ? t('scheduled.pause') : t('scheduled.resume')}
          />
        </span>
      </div>

      {schedule.kind === 'workflow' && (
        <span className="sch-card__kind">
          <GitFork size={11} />
          {workflowName || schedule.workflow_id || t('scheduled.workflowDeleted')}
        </span>
      )}

      <p className="sch-card__desc">
        {schedule.instruction || (schedule.kind === 'workflow' ? t('scheduled.noInput') : '')}
      </p>

      <div className="sch-card__freq">
        <Hourglass size={12} aria-hidden />
        <span>{schedule.freq_text || schedule.freq.kind}</span>
        {window_ && <span className="sch-card__window">{window_}</span>}
      </div>

      <div className="sch-card__foot">
        <span className="sch-card__next" title={schedule.next_run_at ?? ''}>
          {schedule.next_run_at ? (
            <>
              {t('scheduled.nextPrefix')} {formatLocalDateTime(schedule.next_run_at)}
            </>
          ) : (
            noNextRunHint(schedule)
          )}
        </span>
        {last && (
          <span className={`sch-card__last sch-card__last--${schedule.last_status}`}>
            {t('scheduled.lastPrefix')} {last}
          </span>
        )}
        <span className="sch-card__ctl">
          <button
            type="button"
            className="icon-btn"
            disabled={busy}
            onClick={onRun}
            title={t('scheduled.runNow')}
          >
            {busy ? <Loader2 size={13} className="spin" /> : <Play size={13} />}
          </button>
          <button type="button" className="icon-btn" onClick={onCopy} title={t('scheduled.copy')}>
            <Copy size={13} />
          </button>
          <button type="button" className="icon-btn icon-btn--danger" onClick={onDelete} title={t('scheduled.delete')}>
            <Trash size={13} />
          </button>
        </span>
      </div>
    </article>
  );
}

function RunRow({ run, onOpen }: { run: ScheduledRunDTO; onOpen: () => void }) {
  const t = useT();
  const Icon = runStatusIcon(run.status as ScheduledRunStatus);
  const spinning = run.status === 'running';

  return (
    <button type="button" className={`sch-run sch-run--${run.status}`} onClick={onOpen}>
      <span className="sch-run__badge">
        <Icon size={15} className={spinning ? 'spin' : undefined} aria-hidden />
      </span>
      <span className="sch-run__main">
        <span className="sch-run__title">
          {run.schedule_title || run.schedule_id}
          <em className="sch-run__status">{runStatusText(run.status)}</em>
          {run.trigger === 'manual' && <em className="sch-run__trigger">{t('scheduled.manualTrigger')}</em>}
        </span>
        <span className="sch-run__line">
          {run.error ? run.error : run.result || (run.status === 'running' ? t('scheduled.runningNow') : '')}
        </span>
      </span>
      {run.files && run.files.length > 0 && (
        <span className="sch-run__files">{t('tasks.filesProduced', { n: run.files.length })}</span>
      )}
    </button>
  );
}

function TemplatePicker({
  onClose,
  onPick,
}: {
  onClose: () => void;
  onPick: (template: ScheduleTemplate) => void;
}) {
  const t = useT();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      className="modal-mask"
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="modal sch-templates" role="dialog" aria-modal="true" aria-labelledby="sch-tpl-title">
        <div className="modal__head">
          <div style={{ flex: 1 }}>
            <div className="modal__title" id="sch-tpl-title">
              {t('scheduled.templateTitle')}
            </div>
          </div>
          <button type="button" className="sch-editor__close" onClick={onClose} title={t('common.close')}>
            <X size={15} />
          </button>
        </div>
        <div className="modal__body sch-templates__body">
          {SCHEDULE_TEMPLATES.map((tpl) => (
            <button type="button" key={tpl.key} className="sch-tpl" onClick={() => onPick(tpl)}>
              <span className="sch-tpl__title">{tpl.title}</span>
              <span className="sch-tpl__desc">{tpl.desc}</span>
            </button>
          ))}
        </div>
        <div className="modal__foot">
          <button type="button" className="btn" onClick={onClose}>
            {t('common.cancel')}
          </button>
        </div>
      </div>
    </div>
  );
}
