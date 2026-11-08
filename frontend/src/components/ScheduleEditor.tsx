import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import {
  Bot,
  Calendar,
  Check,
  ChevronDown,
  Folder,
  GitFork,
  Globe,
  Loader2,
  Network,
  ShieldAlert,
  Sparkles,
  X,
} from '@/components/icons';
import { Switch } from '@/components/settings2/kit';
import { useT } from '@/i18n';
import { isTopmostModal, useModalFocus } from '@/hooks/useModalFocus';
import {
  createSchedule,
  fetchAgents,
  fetchModels,
  fetchPermissionMode,
  fetchWorkflows,
  fetchWorkdirs,
  updateSchedule,
} from '@/lib/api';
import { DEFAULT_FREQ, WEEK_OPTIONS, formatWindow } from '@/lib/schedule';
import { toast } from '@/stores/toastStore';
import type {
  AgentDef,
  ModelOption,
  PermissionModeInfo,
  ScheduleFreq,
  ScheduledTaskDTO,
  ScheduleWritePayload,
  WorkdirEntry,
  WorkflowDTO,
} from '@/types';

type FreqKind = ScheduleFreq['kind'];

const KINDS: Array<{ value: FreqKind; label: string }> = [
  { value: 'daily', label: '每天' },
  { value: 'weekly', label: '每周' },
  { value: 'monthly', label: '每月' },
  { value: 'interval', label: '每隔几天' },
  { value: 'cron', label: '自定义' },
];

const TITLE_MAX = 80;

function messageOf(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback;
}

/** 已有任务的 freq → 表单字段（缺的东西一律给该类型自己的默认值，而不是空） */
function freqToForm(freq: ScheduleFreq | undefined): { time: string; days: number[]; day: number; everyDays: number; expr: string } {
  const f = freq ?? DEFAULT_FREQ;
  return {
    time: 'time' in f && f.time ? f.time : '09:00',
    days: f.kind === 'weekly' && f.days.length > 0 ? [...f.days] : [0],
    day: f.kind === 'monthly' ? f.day : 1,
    everyDays: f.kind === 'interval' ? f.every_days : 2,
    expr: f.kind === 'cron' ? f.expr : '0 9 * * 1-5',
  };
}

/** 表单字段 → `freq`；返回 null 表示这一趟不该提交（并已通过 `error` 告诉用户缺什么） */
function formToFreq(
  kind: FreqKind,
  form: { time: string; days: number[]; day: number; everyDays: number; expr: string },
): { freq?: ScheduleFreq; error?: string } {
  const hhmm = /^\d{2}:\d{2}$/.test(form.time);
  if (kind === 'cron') {
    const expr = form.expr.trim();
    if (!expr) return { error: '请填写 cron 表达式（五分字段：分 时 日 月 周）' };
    return { freq: { kind: 'cron', expr } };
  }
  if (!hhmm) return { error: '执行时间要写成 HH:MM，比如 09:00' };
  if (kind === 'daily') return { freq: { kind: 'daily', time: form.time } };
  if (kind === 'weekly') {
    if (form.days.length === 0) return { error: '每周至少要点选一个星期几' };
    return { freq: { kind: 'weekly', time: form.time, days: [...form.days].sort((a, b) => a - b) } };
  }
  if (kind === 'monthly') {
    if (!Number.isInteger(form.day) || form.day < 1 || form.day > 31) {
      return { error: '每月的日期要在 1~31 之间' };
    }
    return { freq: { kind: 'monthly', time: form.time, day: form.day } };
  }
  if (!Number.isInteger(form.everyDays) || form.everyDays < 1) {
    return { error: '间隔天数至少 1 天' };
  }
  return { freq: { kind: 'interval', time: form.time, every_days: form.everyDays } };
}

/** 新建时的预填（模版、「从任务新建」两条路都只改这几样，其余走默认） */
export type SchedulePreset = {
  title?: string;
  instruction?: string;
  freq?: ScheduleFreq;
  kind?: 'chat' | 'workflow';
  workflow_id?: string;
};

interface ScheduleEditorProps {
  /** 传了就是编辑；不传是新建 */
  schedule?: ScheduledTaskDTO;
  /** 新建时的预填（模版、「从任务新建」都走这里） */
  preset?: SchedulePreset;
  onClose: () => void;
  onSaved: (schedule: ScheduledTaskDTO) => void;
}

export function ScheduleEditor({ schedule, preset, onClose, onSaved }: ScheduleEditorProps) {
  const t = useT();
  const editing = Boolean(schedule);

  const navigate = useNavigate();
  const [title, setTitle] = useState(schedule?.title ?? preset?.title ?? '');
  const [instruction, setInstruction] = useState(schedule?.instruction ?? preset?.instruction ?? '');
  const [scheduleKind, setScheduleKind] = useState<'chat' | 'workflow'>(
    schedule?.kind ?? preset?.kind ?? 'chat',
  );
  const [workflowId, setWorkflowId] = useState(schedule?.workflow_id ?? preset?.workflow_id ?? '');
  const [kind, setKind] = useState<FreqKind>(schedule?.freq.kind ?? preset?.freq?.kind ?? 'daily');
  const [form, setForm] = useState(() =>
    freqToForm(schedule?.freq ?? preset?.freq ?? DEFAULT_FREQ),
  );
  const [startDate, setStartDate] = useState(schedule?.start_date ?? '');
  const [endDate, setEndDate] = useState(schedule?.end_date ?? '');
  const [enabled, setEnabled] = useState(schedule?.enabled ?? true);

  const [agent, setAgent] = useState(schedule?.agent ?? '');
  const [model, setModel] = useState(schedule?.model ?? '');
  const [workdirId, setWorkdirId] = useState(schedule?.workdir_id ?? '');
  const [permissionMode, setPermissionMode] = useState(schedule?.permission_mode ?? '');
  const [web, setWeb] = useState(schedule?.web_enabled ?? true);
  const [graph, setGraph] = useState(schedule?.graph_enabled ?? true);

  const [agents, setAgents] = useState<AgentDef[]>([]);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [workdirs, setWorkdirs] = useState<WorkdirEntry[]>([]);
  const [modes, setModes] = useState<PermissionModeInfo[]>([]);
  const [workflows, setWorkflows] = useState<WorkflowDTO[]>([]);
  const [optionsError, setOptionsError] = useState('');
  const [loadingOptions, setLoadingOptions] = useState(true);

  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [windowOpen, setWindowOpen] = useState(false);
  const [windowAt, setWindowAt] = useState({ top: 0, left: 0 });
  const windowAnchor = useRef<HTMLDivElement | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const overlayRef = useRef<HTMLDivElement>(null);
  useModalFocus(dialogRef, overlayRef);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      const problems: string[] = [];
      const [a, m, w, p, f] = await Promise.allSettled([
        fetchAgents(),
        fetchModels(),
        fetchWorkdirs(),
        fetchPermissionMode(),
        fetchWorkflows(),
      ]);
      if (!alive) return;
      if (a.status === 'fulfilled') {
        setAgents(a.value.agents.filter((item) => item.enabled && !item.resident));
      } else {
        problems.push('专员清单');
      }
      if (m.status === 'fulfilled') setModels(m.value.models);
      if (w.status === 'fulfilled') setWorkdirs(w.value.workdirs.filter((d) => d.exists));
      else problems.push('工作区');
      if (p.status === 'fulfilled') setModes(p.value.catalog);
      if (f.status === 'fulfilled' && f.value.storage === 'ok') setWorkflows(f.value.workflows);
      else problems.push('工作流清单');
      setOptionsError(problems.length ? `${problems.join('、')}没拉到，可选项暂时只有默认值` : '');
      setLoadingOptions(false);
    };
    void load();
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || submitting || !isTopmostModal(dialogRef.current)) return;
      // Esc 先收弹层，再关弹窗 —— 一次按键只退一层
      if (windowOpen) {
        setWindowOpen(false);
        return;
      }
      onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [submitting, windowOpen, onClose]);

  useEffect(() => {
    if (!windowOpen) return;
    const onDown = (e: MouseEvent) => {
      if (windowAnchor.current && !windowAnchor.current.contains(e.target as Node)) {
        setWindowOpen(false);
      }
    };
    window.addEventListener('mousedown', onDown);
    return () => window.removeEventListener('mousedown', onDown);
  }, [windowOpen]);

  const toggleDay = (value: number) => {
    setForm((prev) => ({
      ...prev,
      days: prev.days.includes(value) ? prev.days.filter((d) => d !== value) : [...prev.days, value],
    }));
  };

  const openWindowPopover = () => {
    const rect = windowAnchor.current?.getBoundingClientRect();
    if (rect) setWindowAt({ top: rect.bottom + 6, left: rect.left });
    setWindowOpen((v) => !v);
  };

  const submit = async () => {
    if (submitting) return;
    if (!title.trim()) {
      setError(t('scheduled.needTitle'));
      return;
    }
    if (scheduleKind === 'workflow') {
      if (!workflowId) {
        setError(t('scheduled.needWorkflow'));
        return;
      }
    } else if (!instruction.trim()) {
      setError(t('scheduled.needInstruction'));
      return;
    }
    const built = formToFreq(kind, form);
    if (!built.freq) {
      setError(built.error ?? '排期不完整');
      return;
    }
    const payload: ScheduleWritePayload = {
      title: title.trim(),
      kind: scheduleKind,
      workflow_id: scheduleKind === 'workflow' ? workflowId : '',
      instruction: instruction.trim(),
      freq: built.freq,
      start_date: startDate,
      end_date: endDate,
      enabled,
      agent,
      model,
      web_enabled: web,
      graph_enabled: graph,
      permission_mode: permissionMode,
      workdir_id: workdirId,
    };
    setSubmitting(true);
    setError('');
    try {
      const saved = schedule
        ? await updateSchedule(schedule.schedule_id, payload)
        : await createSchedule(payload);
      onSaved(saved);
      toast(schedule ? t('scheduled.saved') : t('scheduled.created'), 'success');
      onClose();
    } catch (e) {
      setError(messageOf(e, t('scheduled.saveFailed')));
      setSubmitting(false);
    }
  };

  const windowText = formatWindow(startDate, endDate) || t('scheduled.windowNever');
  const loosest = modes.length > 0 ? Math.max(...modes.map((m) => m.looseness)) : -1;
  const modeLoose = Boolean(permissionMode) &&
    (modes.find((m) => m.value === permissionMode)?.looseness ?? -1) >= loosest;

  return (
    <div
      ref={overlayRef}
      className="modal-mask"
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !submitting) onClose();
      }}
    >
      <div ref={dialogRef} className="modal sch-editor" role="dialog" aria-modal="true" aria-labelledby="sch-editor-title" tabIndex={-1}>
        <div className="modal__head">
          <div className="modal__title" id="sch-editor-title" style={{ flex: 1 }}>
            {editing ? t('scheduled.editTitle') : t('scheduled.newTitle')}
          </div>
          <button type="button" className="sch-editor__close" onClick={onClose} title={t('common.close')}>
            <X size={15} />
          </button>
        </div>

        <div className="modal__body sch-editor__body">
          <input
            className="sch-input sch-input--title"
            value={title}
            maxLength={TITLE_MAX}
            placeholder={t('scheduled.titlePlaceholder')}
            aria-label={t('scheduled.fieldTitle')}
            onChange={(e) => setTitle(e.target.value)}
          />

          <div className="sch-mode" role="group" aria-label={t('scheduled.modeLabel')}>
            <button
              type="button"
              className={`sch-mode__btn${scheduleKind === 'chat' ? ' is-active' : ''}`}
              aria-pressed={scheduleKind === 'chat'}
              onClick={() => setScheduleKind('chat')}
            >
              <Bot size={12} />
              {t('scheduled.modeChat')}
            </button>
            <button
              type="button"
              className={`sch-mode__btn${scheduleKind === 'workflow' ? ' is-active' : ''}`}
              aria-pressed={scheduleKind === 'workflow'}
              onClick={() => setScheduleKind('workflow')}
            >
              <GitFork size={12} />
              {t('scheduled.modeWorkflow')}
            </button>
          </div>

          {scheduleKind === 'chat' ? (
          <div className="sch-composer">
            <textarea
              className="sch-composer__ta"
              rows={4}
              value={instruction}
              placeholder={t('scheduled.instructionPlaceholder')}
              aria-label={t('scheduled.fieldInstruction')}
              onChange={(e) => setInstruction(e.target.value)}
            />
            <div className="sch-composer__tray">
              <label className="sch-pill">
                <Bot size={12} />
                <select
                  value={agent}
                  aria-label={t('scheduled.fieldAgent')}
                  onChange={(e) => setAgent(e.target.value)}
                >
                  <option value="">{t('scheduled.agentDefault')}</option>
                  {agents.map((a) => (
                    <option key={a.name} value={a.name}>
                      {a.display_name || a.name}
                    </option>
                  ))}
                  {agent && !agents.some((a) => a.name === agent) && (
                    <option value={agent}>{`${agent}（当前清单里没有）`}</option>
                  )}
                </select>
                <ChevronDown size={12} className="sch-pill__caret" />
              </label>

              <label className={`sch-pill${model ? ' sch-pill--on' : ''}`}>
                <Sparkles size={12} />
                <select
                  value={model}
                  aria-label={t('scheduled.fieldModel')}
                  onChange={(e) => setModel(e.target.value)}
                >
                  <option value="">{t('scheduled.modelDefault')}</option>
                  {models.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                  {model && !models.some((m) => m.id === model) && (
                    <option value={model}>{`${model}（未配置）`}</option>
                  )}
                </select>
                <ChevronDown size={12} className="sch-pill__caret" />
              </label>

              <label className={`sch-pill${workdirId ? ' sch-pill--on' : ''}`}>
                <Folder size={12} />
                <select
                  value={workdirId}
                  aria-label={t('scheduled.fieldWorkdir')}
                  onChange={(e) => setWorkdirId(e.target.value)}
                >
                  <option value="">{t('scheduled.workdirDefault')}</option>
                  {workdirs.map((d) => (
                    <option key={d.id} value={d.id} title={d.path}>
                      {d.name}
                    </option>
                  ))}
                  {workdirId && !workdirs.some((d) => d.id === workdirId) && (
                    <option value={workdirId}>{`${workdirId}（目录已不在）`}</option>
                  )}
                </select>
                <ChevronDown size={12} className="sch-pill__caret" />
              </label>

              <label className={`sch-pill${modeLoose ? ' sch-pill--danger' : permissionMode ? ' sch-pill--on' : ''}`}>
                <ShieldAlert size={12} />
                <select
                  value={permissionMode}
                  aria-label={t('scheduled.fieldMode')}
                  onChange={(e) => setPermissionMode(e.target.value)}
                >
                  <option value="">{t('scheduled.modeDefault')}</option>
                  {modes.map((m) => (
                    <option key={m.value} value={m.value} title={m.hint}>
                      {m.label}
                    </option>
                  ))}
                </select>
                <ChevronDown size={12} className="sch-pill__caret" />
              </label>

              {loadingOptions && <Loader2 size={11} className="spin sch-tray__loading" />}

              <span className="sch-tray__spacer" />

              <button
                type="button"
                className={`sch-toggle${web ? ' is-on' : ''}`}
                aria-pressed={web}
                onClick={() => setWeb((v) => !v)}
              >
                <Globe size={12} />
                {t('scheduled.fieldWeb')}
              </button>
              <button
                type="button"
                className={`sch-toggle${graph ? ' is-on' : ''}`}
                aria-pressed={graph}
                onClick={() => setGraph((v) => !v)}
              >
                <Network size={12} />
                {t('scheduled.fieldGraph')}
              </button>
            </div>
          </div>
          ) : (
            <div className="sch-composer sch-composer--workflow">
              <div className="sch-wf-pick">
                <GitFork size={13} />
                <select
                  value={workflowId}
                  aria-label={t('scheduled.pickWorkflow')}
                  onChange={(e) => setWorkflowId(e.target.value)}
                >
                  <option value="">
                    {workflows.length > 0 ? t('scheduled.pickWorkflow') : t('scheduled.noWorkflows')}
                  </option>
                  {workflows.map((workflow) => (
                    <option key={workflow.workflow_id} value={workflow.workflow_id}>
                      {workflow.name}
                    </option>
                  ))}
                  {workflowId && !workflows.some((workflow) => workflow.workflow_id === workflowId) && (
                    <option value={workflowId}>{`${t('scheduled.workflowDeleted')}（${workflowId}）`}</option>
                  )}
                </select>
                <ChevronDown size={12} className="sch-pill__caret" />
                <button
                  type="button"
                  className="sch-wf-pick__new"
                  onClick={() => navigate('/workflows')}
                >
                  {t('scheduled.manageWorkflows')}
                </button>
              </div>
              <textarea
                className="sch-composer__ta"
                rows={3}
                value={instruction}
                placeholder={t('scheduled.workflowInputPlaceholder')}
                title={t('scheduled.workflowInputHint')}
                aria-label={t('scheduled.workflowInput')}
                onChange={(e) => setInstruction(e.target.value)}
              />
            </div>
          )}

          <div className="sch-schedule">
            <label className="sch-ctl">
              <span>{t('scheduled.ctlRepeat')}</span>
              <select value={kind} onChange={(e) => setKind(e.target.value as FreqKind)}>
                {KINDS.map((k) => (
                  <option key={k.value} value={k.value}>
                    {k.label}
                  </option>
                ))}
              </select>
              <ChevronDown size={12} className="sch-ctl__caret" />
            </label>

            {kind === 'monthly' && (
              <label className="sch-ctl">
                <span>{t('scheduled.monthlyDay')}</span>
                <input
                  type="number"
                  min={1}
                  max={31}
                  value={form.day}
                  onChange={(e) => setForm((p) => ({ ...p, day: Number(e.target.value) }))}
                />
              </label>
            )}

            {kind === 'interval' && (
              <label className="sch-ctl">
                <span>{t('scheduled.everyDays')}</span>
                <input
                  type="number"
                  min={1}
                  max={365}
                  value={form.everyDays}
                  onChange={(e) => setForm((p) => ({ ...p, everyDays: Number(e.target.value) }))}
                />
              </label>
            )}

            {kind === 'cron' ? (
              <label className="sch-ctl sch-ctl--grow">
                <input
                  className="sch-ctl__mono"
                  value={form.expr}
                  spellCheck={false}
                  placeholder="0 9 * * 1-5"
                  aria-label="cron"
                  onChange={(e) => setForm((p) => ({ ...p, expr: e.target.value }))}
                />
              </label>
            ) : (
              <label className="sch-ctl">
                <span>{t('scheduled.atTime')}</span>
                <input
                  type="time"
                  value={form.time}
                  onChange={(e) => setForm((p) => ({ ...p, time: e.target.value }))}
                />
              </label>
            )}

            <div className="sch-ctl sch-ctl--pop" ref={windowAnchor}>
              <Calendar size={12} className="sch-ctl__lead" />
              <span>{t('scheduled.ctlWindow')}</span>
              <button type="button" className="sch-ctl__btn" aria-expanded={windowOpen} onClick={openWindowPopover}>
                {windowText}
              </button>
              <ChevronDown
                size={12}
                className={`sch-ctl__caret${windowOpen ? ' is-open' : ''}`}
              />
            </div>

            {kind === 'weekly' && (
              <div className="sch-days" role="group" aria-label={t('scheduled.pickWeekdays')}>
                {WEEK_OPTIONS.map((d) => (
                  <button
                    type="button"
                    key={d.value}
                    className={`sch-day${form.days.includes(d.value) ? ' is-active' : ''}`}
                    aria-pressed={form.days.includes(d.value)}
                    onClick={() => toggleDay(d.value)}
                  >
                    {d.label}
                  </button>
                ))}
              </div>
            )}
          </div>

          {optionsError && <div className="sch-options-error">{optionsError}</div>}
          {error && <div className="sch-error">{error}</div>}
        </div>

        <div className="modal__foot sch-editor__foot">
          <label className="sch-enable">
            <Switch checked={enabled} onChange={setEnabled} ariaLabel={t('scheduled.enable')} />
            <span>{t('scheduled.enable')}</span>
          </label>
          <div className="sch-foot__spacer" />
          <button type="button" className="btn" onClick={onClose} disabled={submitting}>
            {t('common.cancel')}
          </button>
          <button type="button" className="btn btn--primary" onClick={() => void submit()} disabled={submitting}>
            {submitting ? <Loader2 size={13} className="spin" /> : <Check size={13} />}
            {editing ? t('scheduled.saveConfirm') : t('scheduled.createConfirm')}
          </button>
        </div>
      </div>

      {windowOpen && (
        <div className="sch-window-pop" style={{ top: windowAt.top, left: windowAt.left }} role="dialog">
          <label className="sch-window-pop__field">
            <span>{t('scheduled.from')}</span>
            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          </label>
          <label className="sch-window-pop__field">
            <span>{t('scheduled.until')}</span>
            <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          </label>
          {(startDate || endDate) && (
            <button
              type="button"
              className="sch-window-pop__clear"
              onClick={() => {
                setStartDate('');
                setEndDate('');
              }}
            >
              {t('scheduled.windowClear')}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
