import { confirmAction } from '@/stores/confirmStore';
/**
 * 后台任务页 —— 任务的一级入口
 *
 * ★ 为什么值得一个独立页面
 *
 * SubtaskPanel 挂在对话流末尾，跟着会话走：切了会话就看不到其它会话的任务，
 * 任务跑完了回到原会话才能找到结果。Supervisor 架构的核心承诺是
 * 「任务活得比产生它的那一轮更久」—— 一级入口就是这个承诺的界面兑现：
 * 跨会话聚合全部任务，正在跑的排最前，随时回来看结果。
 *
 * ★ 数据来源
 *
 * `GET /api/tasks` 不带 thread_id 时返回**全部**会话的任务（后端已支持），
 * 进度更新复用 agentStore 的 refreshTasks —— 不行，那个按当前会话拉。
 * 这里自己按「全部任务」轮询，与 SubtaskPanel 的会话内轮询互不干扰：
 * 两边各自拉各自的清单，折叠都走后端任务表，不存在状态打架。
 */

import { useCallback, useEffect, useState } from 'react';

import {
  ChevronRight,
  CircleCheck,
  CircleSlash,
  CircleX,
  Hourglass,
  ListTodo,
  Loader2,
  MessageSquare,
  RefreshCw,
  RotateCcw,
  Trash2,
  TriangleAlert,
  type LucideIcon,
} from '@/components/icons';

import { ConversationSkeleton } from '@/components/ConversationSkeleton';
import { EmptyState } from '@/components/EmptyState';
import { PageShell } from '@/components/PageShell';
import { QuestionCard } from '@/components/QuestionCard';
import { useT } from '@/i18n';
import {
  answerQuestion,
  deleteTask,
  fetchTasks,
  QuestionExpiredError,
  resumeTask,
} from '@/lib/api';
import { isSubtaskStepRunning, subtaskStatus } from '@/lib/humanize';
import { askedFromPayload } from '@/lib/questions';
import type { QuestionAnswers, SubtaskDTO, SubtaskStatus } from '@/types';

/** 状态 → 图标（与 SubtaskPanel 同一套映射，同一种状态两处颜色一致） */
function statusIcon(status: SubtaskStatus): LucideIcon {
  switch (status) {
    case 'pending':
      return Hourglass;
    case 'running':
      return Loader2;
    case 'waiting_approval':
      return Hourglass;
    case 'waiting_input':
      return MessageSquare;
    case 'interrupted':
      return RotateCcw;
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

const TERMINAL: SubtaskStatus[] = ['succeeded', 'failed', 'cancelled', 'orphaned'];
/** 会自己往前走的任务 —— `interrupted` 不算（它在等人，轮询它没有意义） */
const ACTIVE: SubtaskStatus[] = ['pending', 'running', 'waiting_approval', 'waiting_input'];
const POLL_MS = 3000;

/** 状态分组排序权重：非终态在前，同组按开始时间倒序 */
function statusWeight(status: SubtaskStatus): number {
  return TERMINAL.includes(status) ? 1 : 0;
}

export function TasksPage() {
  const t = useT();
  const [tasks, setTasks] = useState<SubtaskDTO[]>([]);
  const [storage, setStorage] = useState<'ok' | 'unavailable'>('ok');
  const [storageError, setStorageError] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (silent: boolean) => {
    if (!silent) setRefreshing(true);
    try {
      // 不带 thread_id = 跨全部会话
      const resp = await fetchTasks('');
      if (resp.storage === 'unavailable') {
        setStorage('unavailable');
        setStorageError(resp.storage_error ?? '');
      } else {
        setStorage('ok');
        setTasks(resp.tasks);
      }
    } catch {
      setStorage('unavailable');
      setStorageError('');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  // 首拉 + 有非终态任务时轮询（页面不可见时停 —— 后台标签页轮询费电且堆积过期响应）
  useEffect(() => {
    void load(true);
  }, [load]);

  const hasActive = tasks.some((task) => ACTIVE.includes(task.status));
  useEffect(() => {
    if (!hasActive) return;
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    const timer = window.setInterval(() => void load(true), POLL_MS);
    const onVisible = () => {
      if (document.visibilityState === 'visible') void load(true);
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [hasActive, load]);

  const ordered = [...tasks].sort((a, b) => {
    const byStatus = statusWeight(a.status) - statusWeight(b.status);
    if (byStatus !== 0) return byStatus;
    return (b.started_at ?? b.created_at ?? '').localeCompare(a.started_at ?? a.created_at ?? '');
  });
  const activeCount = tasks.filter((task) => ACTIVE.includes(task.status)).length;

  return (
    <PageShell
      title={t('tasks.title')}
      icon={ListTodo}
      className="tasks-page"
      meta={activeCount > 0 && <span className="page-status"><Loader2 size={12} className="spin" />{t('tasks.active')} {activeCount}</span>}
      actions={
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
        </>
      }
    >
      {loading && <ConversationSkeleton rows={4} label={t('common.loading')} />}

      {!loading && storage === 'unavailable' && (
        <div className="subtask-panel subtask-panel--unavailable">
          <TriangleAlert size={14} aria-hidden />
          <span>{storageError || t('tasks.storageUnavailable')}</span>
        </div>
      )}

      {!loading && storage === 'ok' && ordered.length === 0 && (
        <EmptyState
          fill
          icon={ListTodo}
          title={t('tasks.empty')}
        />
      )}

      {!loading && storage === 'ok' && ordered.length > 0 && (
        <ul className="tasks-page__list">
          {ordered.map((task) => (
            <TaskRow key={task.task_id} task={task} onRefresh={() => void load(true)} />
          ))}
        </ul>
      )}
    </PageShell>
  );
}

function TaskRow({ task, onRefresh }: { task: SubtaskDTO; onRefresh: () => void }) {
  const t = useT();
  const [detailOpen, setDetailOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const Icon = statusIcon(task.status);
  const spinning = task.status === 'running';

  // ★ 倒计时起点取进度步骤的时间戳（理由同 SubtaskPanel：轮询每 3s 换一份对象）
  const pending = task.pending_question;
  const questionAt = task.progress.find(
    (step) => step.kind === 'question' && step.status === 'awaiting',
  )?.at;
  const asked = pending
    ? askedFromPayload(
        pending,
        'pending',
        questionAt && !Number.isNaN(Date.parse(questionAt)) ? Date.parse(questionAt) : undefined,
      )
    : null;

  /**
   * 回答成功后等下一次轮询（≤3s）任务才会离开 waiting_input；
   * 这段时间里卡片必须立刻只读，否则用户会再点一次、拿到「已失效」。
   */
  const [answeredLocal, setAnsweredLocal] = useState<{ id: string; answers: QuestionAnswers } | null>(null);
  const locallyAnswered = pending && answeredLocal?.id === pending.question_id ? answeredLocal : null;
  const cardAsked = asked && locallyAnswered
    ? { ...asked, status: 'answered' as const, answers: locallyAnswered.answers }
    : asked;

  const answerTaskQuestion = async (answers: QuestionAnswers, skipped = false) => {
    if (!pending) return;
    try {
      await answerQuestion({
        threadId: task.thread_id,
        callId: pending.call_id,
        questionId: pending.question_id,
        answers,
        skipped,
        runId: pending.run_id,
        employeeId: pending.employee_id,
      });
    } catch (e) {
      if (e instanceof QuestionExpiredError) {
        onRefresh();
        return;
      }
      throw e;
    }
    setAnsweredLocal({ id: pending.question_id, answers: skipped ? {} : answers });
    onRefresh();
  };

  /** 续跑。`needs_confirm` = 最后一步可能已生效，先让用户核对（同 SubtaskPanel） */
  const handleResume = async () => {
    if (busy) return;
    setBusy(true);
    setActionError('');
    try {
      let resp = await resumeTask(task.task_id);
      if (!resp.ok && resp.needs_confirm) {
        if (!await confirmAction({ title: t('feedback.resumeTitle'), description: resp.reason ?? t('tasks.resumeConfirmFallback'), confirmLabel: t('feedback.resumeAction'), tone: 'warning' })) return;
        resp = await resumeTask(task.task_id, true);
      }
      if (!resp.ok) {
        setActionError(resp.reason ?? t('tasks.resumeFailed'));
        return;
      }
      onRefresh();
    } catch {
      setActionError(t('tasks.resumeFailed'));
    } finally {
      setBusy(false);
    }
  };

  /** 删除已结束的任务。进行中的不显示按钮（后端也会拒绝），interrupted 提示删了就不能续 */
  const handleDelete = async () => {
    if (busy) return;
    const confirmKey =
      task.status === 'interrupted' ? 'tasks.deleteConfirmInterrupted' : 'tasks.deleteConfirm';
    if (!await confirmAction({ title: t('feedback.deleteTaskTitle'), description: t(confirmKey) })) return;
    setBusy(true);
    setActionError('');
    try {
      const resp = await deleteTask(task.task_id);
      if (!resp.ok) {
        setActionError(resp.reason ?? t('tasks.deleteFailed'));
        return;
      }
      onRefresh();
    } catch {
      setActionError(t('tasks.deleteFailed'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="task-card">
      <div className="task-card__head">
        <Icon size={15} className={spinning ? 'spin' : undefined} aria-hidden />
        <span className="task-card__title">{task.title}</span>
        <span className="task-card__status">{subtaskStatus(task.status)}</span>
      </div>

      {task.error && <p className="task-card__error">{task.error}</p>}
      {task.resume_note && <p className="task-card__note">{task.resume_note}</p>}

      {task.status === 'waiting_input' && cardAsked && (
        <QuestionCard
          compact
          asked={cardAsked}
          onSubmit={locallyAnswered ? undefined : (answers) => answerTaskQuestion(answers)}
          onSkip={locallyAnswered ? undefined : () => answerTaskQuestion({}, true)}
        />
      )}

      {task.progress.length > 0 && (
        <button
          type="button"
          className="subtask-item__steps-toggle"
          onClick={() => setDetailOpen((v) => !v)}
          aria-expanded={detailOpen}
        >
          <ChevronRight
            size={12}
            className={detailOpen ? 'subtask-panel__chevron subtask-panel__chevron--open' : 'subtask-panel__chevron'}
            aria-hidden
          />
          {detailOpen ? t('tasks.hideProgress') : t('tasks.viewProgress', { n: task.progress.length })}
        </button>
      )}

      {detailOpen && task.progress.length > 0 && (
        <ol className="subtask-item__steps">
          {task.progress.map((step) => (
            <li key={step.seq} className={`subtask-step subtask-step--${step.status}`}>
              {isSubtaskStepRunning(step.status) && <Loader2 size={12} className="spin" aria-hidden />}
              <span className="subtask-step__label">{step.label}</span>
              {step.detail && <span className="subtask-step__detail">{step.detail}</span>}
            </li>
          ))}
        </ol>
      )}

      {task.status === 'succeeded' && task.result && (
        <div className="task-card__result">
          <p className="subtask-item__result-label">{t('tasks.result')}</p>
          <p className="subtask-item__result-text">{task.result}</p>
          {task.files && task.files.length > 0 && (
            <p className="subtask-item__files">{t('tasks.filesProduced', { n: task.files.length })}</p>
          )}
        </div>
      )}

      {task.total_steps !== undefined && task.total_steps > task.progress.length && (
        <p className="subtask-item__truncated">
          {t('tasks.truncated', { total: task.total_steps, shown: task.progress.length })}
        </p>
      )}

      {actionError && <p className="task-card__error">{actionError}</p>}

      {!ACTIVE.includes(task.status) && (
        <div className="task-card__actions">
          {task.status === 'interrupted' && (
            <button
              type="button"
              className="subtask-item__resume"
              onClick={() => void handleResume()}
              disabled={busy}
            >
              {busy ? t('tasks.resuming') : t('tasks.resume')}
            </button>
          )}
          <button
            type="button"
            className="task-card__delete"
            onClick={() => void handleDelete()}
            disabled={busy}
            title={t('tasks.delete')}
          >
            <Trash2 size={12} aria-hidden />
            {t('tasks.delete')}
          </button>
        </div>
      )}
    </li>
  );
}
