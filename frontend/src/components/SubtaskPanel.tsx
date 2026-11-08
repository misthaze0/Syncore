import { confirmAction } from '@/stores/confirmStore';
/**
 * ★ 后台子任务面板（Supervisor 架构，见 ADR 0019）
 *
 * 「查一下差旅标准，然后写成文档发给张三」这类请求会派发后台任务。
 * 本面板是用户**唯一**能看到它们的地方 —— 所以它的取舍很关键：
 *
 * 1. **默认收起**。用户没派发任务时，这里连一行都不出现。
 *    后台任务是个内部机制，不该在每次对话里占位置。
 * 2. **基础页签只说人话**。「查差旅标准」而不是 `file_agent`；
 *    「进行中」而不是 `running`。专员名 / task_id / seq / usage
 *    一律不给 —— 它们是内部标识，对用户没有意义。
 * 3. **复用 `ToolCallView` 的图标与折叠样式**。
 *    五种状态（进行中 / 完成 / 失败 / 拒绝 / 等待）已经有了一套映射，
 *    再写一套必然漂移 —— 而漂移的表现是「同一种状态在两个地方颜色不同」。
 * 4. **不给进度条**。子 Agent 的步骤数不可预知（工具循环由模型决定），
 *    百分比要么是假的，要么要额外维护估算逻辑。
 *    步骤列表 + 当前步骤高亮，说的都是真话。
 *
 * ★ 两层折叠（2026-10 补）
 *
 * 面板一级：整块面板收起 / 展开（派了几个任务）。
 * 任务一级：**每个任务自己是一张可展开的行程卡** ——
 *   收起时只看到「任务名 + 状态 + 箭头」，
 *   展开后是它做到哪一步了。
 *
 * 为什么任务级也要折叠：一个任务可能有二三十步，几个任务叠起来
 * 会把回答挤到屏幕外。而用户大多数时候只想知道「跑完了没」，
 * 只有**一个**任务（通常是刚跑完的那个）才值得展开细读。
 * 所以默认策略是「进行中的展开、终态收起」—— 见 `SubtaskItem`。
 */

import { useEffect, useState } from 'react';

import {
  ChevronRight,
  CircleCheck,
  CircleSlash,
  CircleX,
  Hourglass,
  Loader2,
  MessageSquare,
  RotateCcw,
  TriangleAlert,
  type LucideIcon,
} from '@/components/icons';
import { MarkdownRenderer } from '@/components/MarkdownRenderer';
import { QuestionCard } from '@/components/QuestionCard';
import { nameOf } from '@/lib/artifacts';
import { answerQuestion, QuestionExpiredError, tasksStreamUrl } from '@/lib/api';
import { askedFromPayload } from '@/lib/questions';
import { isSubtaskStepRunning, subtaskStatus } from '@/lib/humanize';
import { backgroundTaskLabel } from '@/lib/messagePresentation';
import { parseSSE } from '@/lib/sse';
import { useAgentStore, type SubtaskView } from '@/stores/agentStore';
import type {
  QuestionAnswers,
  SubtaskProgressStep,
  SubtaskStatus,
  TaskStreamEvent,
} from '@/types';

/** 状态 → 图标（★ 直接复用 ToolCallView 的那套映射，见文件头第 3 条） */
function statusIcon(status: SubtaskStatus): LucideIcon {
  switch (status) {
    case 'pending':
      return Hourglass;
    case 'running':
      return Loader2;
    case 'waiting_approval':
      return Hourglass;
    // 停在提问上：等的是答案，画一个「问题」比画沙漏更说得清在等什么
    case 'waiting_input':
      return MessageSquare;
    // 中断但可续 —— 圆箭头比警告三角准确：它不是出错，是停在半路等人点头
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

/** 进度步骤的状态 → 图标 */
function stepIcon(status: string): LucideIcon {
  if (isSubtaskStepRunning(status)) return status === 'awaiting' ? Hourglass : Loader2;
  return status === 'ok' ? CircleCheck : CircleX;
}

export function SubtaskPanel() {
  const tasks = useAgentStore((s) => s.tasks);
  const taskStorage = useAgentStore((s) => s.taskStorage);
  const cancelTask = useAgentStore((s) => s.cancelTask);
  const refreshTasks = useAgentStore((s) => s.refreshTasks);
  const applyTaskEvent = useAgentStore((s) => s.applyTaskEvent);
  const threadId = useAgentStore((s) => s.threadId);

  const [expanded, setExpanded] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const list = Object.values(tasks);
  const total = list.length;

  useEffect(() => setExpanded(false), [threadId]);
  const current = list.find(task => task.status === 'waiting_approval') ?? list.find(task => task.status === 'running' || task.status === 'pending');
  const currentStep = current?.progress.filter(step => step.kind !== 'narrative').at(-1);

  // ★ 轮询已提升到应用级（`useTaskSync`）：任务结束在对话页外也要被感知，
  // 桌面助手的「后台任务已结束」播报才发生在完成那一刻，而不是切回对话页时。
  // 这里保留首次进来拉一次（页面刷新后补齐历史进度）。
  useEffect(() => {
    void refreshTasks({ silent: true });
  }, [refreshTasks]);

  // ★ 进度流订阅（**实时**通道）—— 与轮询并存，各司其职：
  // SSE 负责「快」（事件一发生就推，用户不必等下一个轮询周期）；
  // 轮询负责「稳」（SSE 断线 / 半开 / 页面后台时对齐）。
  //
  // 按会话订阅；`threadId` 变化（切会话）时自动重连。
  // 断开时静默 —— 这条流是**加速**，不是依赖（真相源是任务表 + 轮询）。
  useEffect(() => {
    if (!threadId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let wake: (() => void) | undefined;

    void (async () => {
      let failures = 0;
      while (!controller.signal.aborted) {
        try {
          const resp = await fetch(tasksStreamUrl(threadId), {
            signal: controller.signal,
            headers: { Accept: 'text/event-stream' },
          });
          if (!resp.ok || !resp.body) throw new Error('进度连接暂不可用');
          for await (const event of parseSSE<TaskStreamEvent>(resp)) {
            if (controller.signal.aborted) break;
            failures = 0;
            setReconnecting(false);
            applyTaskEvent(event);
          }
        } catch {
          // 重新连接会带回任务快照和已保存的最终汇报。
        }
        if (controller.signal.aborted) break;
        setReconnecting(true);
        await new Promise<void>(resolve => {
          wake = resolve;
          timer = setTimeout(resolve, Math.min(10000, 1000 * 2 ** Math.min(failures++, 3)));
        });
      }
    })();

    return () => { controller.abort(); clearTimeout(timer); wake?.(); };
  }, [threadId, applyTaskEvent]);

  // ---------- 存储不可用：如实说「查不到」，不显示「没有任务」 ----------
  if (taskStorage === 'unavailable') {
    return (
      <div className="subtask-panel subtask-panel--unavailable">
        <TriangleAlert size={14} aria-hidden />
        <span>后台任务状态暂不可查询</span>
      </div>
    );
  }

  // ---------- 从来没有过任务：连痕迹都不留 ----------
  if (total === 0) return null;

  return (
    <div className="subtask-panel">
      <button
        type="button"
        className="subtask-panel__toggle"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <ChevronRight
          size={14}
          className={expanded ? 'subtask-panel__chevron subtask-panel__chevron--open' : 'subtask-panel__chevron'}
          aria-hidden
        />
        {backgroundTaskLabel(list.map(task => task.status))}
      </button>

      {reconnecting && <p className="subtask-panel__current" role="status">正在重新连接，任务结果会自动补齐</p>}

      {!expanded && current && <p className="subtask-panel__current">{current.title}{currentStep ? ` · ${currentStep.label}` : ''}</p>}
      {expanded && (
        <ul className="subtask-panel__list">
          {list.map((task) => (
            <SubtaskItem
              key={task.taskId}
              task={task}
              onCancel={() => void cancelTask(task.taskId)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function SubtaskItem({
  task,
  onCancel,
}: {
  task: SubtaskView;
  onCancel: () => void;
}) {
  const resumeTask = useAgentStore((s) => s.resumeTask);
  const Icon = statusIcon(task.status);
  const spinning = task.status === 'running';
  const hasSteps = task.progress.length > 0;
  const hasDetails = hasSteps || !!task.result || !!task.files?.length;
  const interrupted = task.status === 'interrupted';

  /** ★ 默认「进行中的展开、终态收起」—— 见文件头「两层折叠」。
   *  只作为**初始值**：`useState` 的初值在组件挂载时求值一次，
   *  所以用户手动折叠之后不会再被任务状态变化强行拉开
   *  （那会变成「我明明收起来了它又自己弹开」）。 */
  const [open, setOpen] = useState(() => !isSubtaskTerminal(task.status));
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const threadId = useAgentStore((s) => s.threadId);
  const refreshTasks = useAgentStore((s) => s.refreshTasks);

  // ★ 倒计时的起点取**进度步骤的时间戳**，不是 Date.now()
  // 轮询每 2s 换一份任务对象；用当前时刻会让倒计时永远从满格重新开始。
  const pending = task.pendingQuestion;
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
   * 本地先落的回答。
   *
   * 回答成功后要等下一次轮询（≤2s）任务才会变成 running；这段时间里
   * 卡片必须立刻变成只读，否则用户会对着它再点一次 —— 而后端此时已经
   * 「找不到等待中的提问」，界面看起来像提交失败。
   */
  const [answeredLocal, setAnsweredLocal] = useState<{ id: string; answers: QuestionAnswers } | null>(null);
  const locallyAnswered = pending && answeredLocal?.id === pending.question_id ? answeredLocal : null;
  const cardAsked = asked && locallyAnswered
    ? { ...asked, status: 'answered' as const, answers: locallyAnswered.answers }
    : asked;

  /**
   * 回答后台任务里的提问。
   *
   * ★ 不复用 store 的 `answerQuestion`
   * 那份绑在**当前对话**的 `pendingQuestion` 上；后台任务可能属于另一个会话、
   * 也可能同时有好几个（不同员工）。这里按任务载荷里的四元组直接提交。
   */
  const answerTaskQuestion = async (answers: QuestionAnswers, skipped = false) => {
    if (!pending) return;
    try {
      await answerQuestion({
        // 归属以载荷自带的 thread_id 为准：任务可能属于另一个会话，
        // 用当前页面的 threadId 会在切会话后的短暂窗口里答错等待者
        threadId: pending.thread_id ?? threadId,
        callId: pending.call_id,
        questionId: pending.question_id,
        answers,
        skipped,
        runId: pending.run_id,
        employeeId: pending.employee_id,
      });
    } catch (e) {
      if (e instanceof QuestionExpiredError) {
        // 已超时 / 已在别处答过：刷一次让卡片消失，而不是留在原地报错
        await refreshTasks({ silent: true });
        return;
      }
      throw e;
    }
    setAnsweredLocal({ id: pending.question_id, answers: skipped ? {} : answers });
    await refreshTasks({ silent: true });
  };

  /**
   * 续跑。`reconcile` 模式下后端会先回 `needsConfirm` ——
   * 那不是失败，是「最后一步可能已经生效，你确认要重做吗」。
   * 与任务页共用确认框，明确说明重复执行的影响。
   */
  const handleResume = async () => {
    if (busy) return;
    setBusy(true);
    setActionError('');
    try {
      let resp = await resumeTask(task.taskId);
      if (!resp.ok && resp.needsConfirm) {
        const ok = await confirmAction({ title: '继续运行这个任务？', description: resp.reason ?? '最后一步可能已经执行过，继续会重做一次。请确认后再继续。', confirmLabel: '继续运行', tone: 'warning' });
        if (!ok) return;
        resp = await resumeTask(task.taskId, true);
      }
      if (!resp.ok && resp.reason) setActionError(resp.reason);
    } finally {
      setBusy(false);
    }
  };


  return (
    <li className="subtask-item">
      {/* ★ 标题行本身就是展开开关（截图里的交互）——
          比「标题 + 下面一行『查看进度（N 步）』」少一次点击、少一行文案。
          没有进度时不渲染成按钮（点了没反应比不能点更糟）。 */}
      {hasDetails ? (
        <button
          type="button"
          className="subtask-item__head subtask-item__head--button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          <ChevronRight
            size={12}
            className={
              open ? 'subtask-panel__chevron subtask-panel__chevron--open' : 'subtask-panel__chevron'
            }
            aria-hidden
          />
          <Icon size={14} className={spinning ? 'spin' : undefined} aria-hidden />
          {/* ★ 标题是人话任务名，不是 agent 名 */}
          <span className="subtask-item__title">{task.title}</span>
          <span className="subtask-item__status">{task.status === 'pending' && task.dependsOn?.length ? '等待前置资料' : subtaskStatus(task.status)}</span>
        </button>
      ) : (
        <div className="subtask-item__head">
          <Icon size={14} className={spinning ? 'spin' : undefined} aria-hidden />
          <span className="subtask-item__title">{task.title}</span>
          <span className="subtask-item__status">{task.status === 'pending' && task.dependsOn?.length ? '等待前置资料' : subtaskStatus(task.status)}</span>
        </div>
      )}

      {task.error && <p className="subtask-item__error">{task.error}</p>}
      {/* ★ 中断的说明（「「写入报告.docx」可能已经执行了…」）常驻可见 ——
          它正是用户点「继续」之前必须看到的那一句，不能藏在展开区里。 */}
      {task.resumeNote && <p className="subtask-item__note">{task.resumeNote}</p>}

      {/* ★ 提问卡原地可答 —— 后台任务停在「等你回答」时，
          关掉这张卡就等于让它在超时后基于假设继续跑 */}
      {task.status === 'waiting_input' && cardAsked && (
        <QuestionCard
          compact
          asked={cardAsked}
          onSubmit={locallyAnswered ? undefined : (answers) => answerTaskQuestion(answers)}
          onSkip={locallyAnswered ? undefined : () => answerTaskQuestion({}, true)}
        />
      )}

      {/* ---------- 进度步骤（不给进度条，见文件头第 4 条）---------- */}
      {open && hasSteps && (
        <>
          <ol className="subtask-item__steps">
            {task.progress.map((step) => (
              <SubtaskStep key={step.seq} step={step} />
            ))}
          </ol>
          {/* ★ 截断必须说出来 —— 悄悄截断与填 0 是同一类谎报 */}
          {task.totalSteps !== undefined && task.totalSteps > task.progress.length && (
            <p className="subtask-item__truncated">
              共 {task.totalSteps} 步，只显示最近 {task.progress.length} 步
            </p>
          )}
        </>
      )}

      {/* ---------- 结果 ---------- */}
      {open && task.status === 'succeeded' && task.result && (
        <div className="subtask-item__result">
          <p className="subtask-item__result-label">结果</p>
          <div className="subtask-item__result-text"><MarkdownRenderer content={task.result}/></div>

        </div>
      )}

      {open && task.files && task.files.length > 0 && <ul className="subtask-item__files">{task.files.map(file => <li key={file.path} title={file.path}>{nameOf(file.path)}</li>)}</ul>}
      {actionError && <p className="subtask-item__error">{actionError}</p>}
      {/* ---------- 操作 ---------- */}
      {/* 未结束的任务才给操作 —— 终态上「取消 / 继续」都没有任何含义 */}
      {!isSubtaskTerminal(task.status) && (
        <div className="subtask-item__foot">
          {interrupted && (
            <button
              type="button"
              className="subtask-item__resume"
              onClick={() => void handleResume()}
              disabled={busy}
            >
              {busy ? '继续中…' : '继续'}
            </button>
          )}
          <button type="button" className="subtask-item__cancel" onClick={onCancel}>
            {/* ★ 「放弃」而不是「取消」：中断的任务已经不跑了，用户要表达的是「不要了」 */}
            {interrupted ? '放弃' : '取消'}
          </button>
        </div>
      )}
    </li>
  );
}

/**
 * 一步进度。
 *
 * ★ `narrative` 单独一条渲染路径
 * 它不是「做了什么」而是「说了什么」—— 是一段正文（可能多行、可能带列表），
 * 塞进 `label` 那种「图标 + 一行字」的形状里会被压成一坨。
 * 所以它的图标位置留空、正文占满整行，读起来像一段旁白。
 */
function SubtaskStep({ step }: { step: SubtaskProgressStep }) {
  if (step.kind === 'narrative' && step.text) {
    return (
      <li className="subtask-step subtask-step--narrative">
        <span className="subtask-step__text">{step.text}</span>
      </li>
    );
  }

  const StepIcon = stepIcon(step.status);
  return (
    <li className={`subtask-step subtask-step--${step.status}`}>
      <StepIcon
        size={12}
        className={isSubtaskStepRunning(step.status) && step.status !== 'awaiting' ? 'spin' : undefined}
        aria-hidden
      />
      <span className="subtask-step__label">{step.label}</span>
      {/* ★ detail 是后端给的补充（「read」「命中 3 个来源」）。
          基础界面显示它，因为它也是人话；agent 名 / seq / call_id 才不给。 */}
      {step.detail && <span className="subtask-step__detail">{step.detail}</span>}
    </li>
  );
}

/** 终态任务：不会再变化（与 store 里同一套口径） */
function isSubtaskTerminal(status: SubtaskStatus): boolean {
  return (
    status === 'succeeded' ||
    status === 'failed' ||
    status === 'cancelled' ||
    status === 'orphaned'
  );
}
