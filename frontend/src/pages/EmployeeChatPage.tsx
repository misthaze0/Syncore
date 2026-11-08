import { confirmAction } from '@/stores/confirmStore';
/** 员工私聊：默认查看团队工作记录，个人对话独立保存。 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';

import { RefreshCw, Send } from '@/components/icons';

import { WorkspaceShell } from '@/components/workspace/WorkspaceShell';
import { EmployeeAvatar } from '@/components/workspace/EmployeeAvatar';
import { useWorkspaceStore } from '@/stores/workspaceStore';
import { shortModelName } from '@/lib/api';
import { MarkdownRenderer } from '@/components/MarkdownRenderer';
import { MessageProcess } from '@/components/MessageProcess';
import { MessageSources } from '@/components/MessageSources';
import { ApprovalCard } from '@/components/run/ApprovalCard';
import { ApprovalDialog } from '@/components/ApprovalDialog';
import { QuestionCard } from '@/components/QuestionCard';
import { TurnFiles } from '@/components/TurnFiles';
import { approvalToolStates } from '@/lib/messagePresentation';
import { askedFromPayload } from '@/lib/questions';
import { useT } from '@/i18n';
import {
  employeeChatStream,
  fetchConversation,
  ApiError,
  answerQuestion,
  QuestionExpiredError,
  fetchEmployee,
  fetchEmployeeThread,
  fetchEmployeeWorks,
  resumeTask,
  submitApproval,
} from '@/lib/api';
import { subtaskStatus } from '@/lib/humanize';
import type {
  AgentEvent,
  AskedQuestion,
  DangerLevel,
  Employee,
  EmployeeWorkDTO,
  GroupApprovalRequest,
  QuestionAnswers,
  ToolCallInfo,
  SourceDoc,
  TurnFileRef,
} from '@/types';

interface EmpToolCall {
  callId: string;
  toolName: string;
  toolLabel?: string;
  args: Record<string, unknown>;
  status: ToolCallInfo['status'];
  result?: string;
  durationMs?: number;
  dangerLevel?: DangerLevel;
  reason?: string;
}

interface EmpMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  reasoning?: string;
  streaming?: boolean;
  error?: string;
  toolCalls: EmpToolCall[];
  sources?: SourceDoc[];
  files?: TurnFileRef[];
  askedQuestions?: AskedQuestion[];
}

interface PendingApproval {
  requestId?: string;
  grantAllowed?: boolean;
  grantMaxUses?: number;
  grantTtlSeconds?: number;
  expiresAt?: number;
  callId: string;
  toolName: string;
  toolLabel?: string;
  reason: string;
  args: Record<string, unknown>;
  /**
   * 护栏 6 的定位维度与「是谁在请求」。
   *
   * 后端按 `(thread_id, call_id, run_id, employee_id)` 四维**精确**匹配等待者，
   * 所以提交时必须原样带回这两维 —— 少一维就判成「没有这个等待」，
   * 而那次危险操作其实还在等着（超时后自动拒绝）。
   */
  runId: string;
  employeeId: string;
  requestor: string;
}

function uid(): string {
  return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** 按 question_id 改一张卡（历史重放与实时事件共用，幂等）。 */
function patchAskedIn(
  messages: EmpMessage[],
  questionId: string,
  fn: (q: AskedQuestion) => AskedQuestion,
): EmpMessage[] {
  return messages.map((m) =>
    m.askedQuestions?.some((q) => q.questionId === questionId)
      ? {
          ...m,
          askedQuestions: m.askedQuestions.map((q) => (q.questionId === questionId ? fn(q) : q)),
        }
      : m,
  );
}

/**
 * 「为团队干过的活」—— 私聊工作台的执行层那一屏（施工图 §4.3）。
 *
 * ★ 这里是全文唯一出现的地方
 * 群聊时间线里只有他自己打模型产出的几句摘要；进度、工具调用与返回、结果全文
 * 只在这一个组件里展开。反过来说，这一屏查不到时必须说「查不了」，
 * 不能渲染成「他什么都没干过」—— 那是把读取失败说成了员工摸鱼。
 */
function EmployeeWorks({ employeeId }: { employeeId: string }) {
  const t = useT();
  const [works, setWorks] = useState<EmployeeWorkDTO[]>([]);
  const [unavailable, setUnavailable] = useState('');
  const [loading, setLoading] = useState(true);
  const [approvals, setApprovals] = useState<GroupApprovalRequest[]>([]);
  const [approvalError, setApprovalError] = useState('');
  const [resumeBusy, setResumeBusy] = useState('');
  const [resumeError, setResumeError] = useState('');
  const requestSeq = useRef(0);

  const load = useCallback(() => {
    const seq = ++requestSeq.current;
    void fetchEmployeeWorks(employeeId, { limit: 20 })
      .then((resp) => {
        if (seq !== requestSeq.current) return;
        setWorks(resp.works ?? []);
        setApprovals(resp.approvals ?? []);
        setUnavailable(
          resp.storage === 'unavailable'
            ? resp.storage_error || t('employee.worksUnavailable')
            : '',
        );
      })
      .catch((e) => {
        if (seq !== requestSeq.current) return;
        setWorks([]);
        setUnavailable(errorText(e));
      })
      .finally(() => { if (seq === requestSeq.current) setLoading(false); });
  }, [employeeId, t]);

  useEffect(() => {
    load();
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') load(); }, 3000);
    return () => { window.clearInterval(timer); requestSeq.current += 1; };
  }, [load]);

  /** 续跑一项被进程重启打断的任务（同 SubtaskPanel / 任务页的确认口径） */
  const resumeWork = async (taskId: string) => {
    setResumeBusy(taskId);
    setResumeError('');
    try {
      let resp = await resumeTask(taskId);
      if (!resp.ok && resp.needs_confirm) {
        if (!await confirmAction({ title: t('feedback.resumeTitle'), description: resp.reason ?? t('employee.worksResumeConfirm'), confirmLabel: t('feedback.resumeAction'), tone: 'warning' })) return;
        resp = await resumeTask(taskId, true);
      }
      if (!resp.ok) {
        setResumeError(resp.reason ?? t('employee.worksResumeFailed'));
        return;
      }
      load();
    } catch {
      setResumeError(t('employee.worksResumeFailed'));
    } finally {
      setResumeBusy('');
    }
  };

  return (
    <section className="emp-works" aria-label="工作记录">
      <header className="emp-works__summary" title={t('employee.worksHint')}>
        <span>工作记录</span>
        <span className="emp-works__count">{loading ? '…' : works.length}</span>
        <button
          type="button"
          className="btn btn--sm btn--ghost"
          onClick={(e) => {
            e.preventDefault();
            load();
          }}
          title={t('employee.worksRefresh')}
        >
          <RefreshCw size={12} />
        </button>
      </header>
      {unavailable && <div className="subtask-panel subtask-panel--unavailable">{unavailable}</div>}
      {approvalError && <div className="workspace-error" role="alert">{approvalError}</div>}
      {resumeError && <div className="workspace-error" role="alert">{resumeError}</div>}
      {approvals.map(approval => <ApprovalCard key={`${approval.thread_id}:${approval.call_id}`} approval={approval} onDecide={async (approved, scope) => {
        setApprovalError('');
        try { await submitApproval(approval.thread_id, approval.call_id, approved, approval.run_id, approval.employee_id, scope, approval.request_id); load(); }
        catch (error) { setApprovalError(errorText(error)); }
      }} />)}
      {!unavailable && !loading && works.length === 0 && (
        <div className="empty-hint">{t('employee.worksEmpty')}</div>
      )}
      <ul className="emp-works__list">
        {works.map((w, index) => (
          <li key={w.task_id}>
            <details className="emp-work" open={index === 0 || ['pending', 'running', 'waiting_approval', 'waiting_input', 'interrupted'].includes(w.status)}>
              <summary className="emp-work__head">
                <span className={`emp-work__status emp-work__status--${w.status}`}>
                  {subtaskStatus(w.status) || w.status}
                </span>
                <span className="emp-work__title">{w.title || w.task_id}</span>
                <span className="emp-work__time">{(w.finished_at || w.created_at || '').slice(5, 16)}</span>
              </summary>
              {w.instruction && (
                <div className="emp-work__block">
                  <div className="emp-work__label">{t('employee.worksInstruction')}</div>
                  <div className="emp-work__instr">{w.instruction}</div>
                </div>
              )}
              {w.status === 'waiting_input' && (
                <div className="emp-work__block">
                  <EmployeeWorkQuestion work={w} onAnswered={load} />
                </div>
              )}
              {w.progress && w.progress.length > 0 && (
                <div className="emp-work__block">
                  <div className="emp-work__label">
                    {t('employee.worksSteps')}
                    {w.total_steps ? t('employee.worksStepsOf', { shown: w.progress.length, total: w.total_steps }) : ''}
                  </div>
                  <ol className="emp-work__steps">
                    {w.progress.map((s) => (
                      <li key={s.seq} className={`emp-step emp-step--${s.status}`}>
                        <span className="emp-step__label">{s.label}</span>
                        {s.detail && <span className="emp-step__detail">{s.detail}</span>}
                        {s.text && <div className="emp-step__text">{s.text}</div>}
                      </li>
                    ))}
                  </ol>
                </div>
              )}
              {w.error && (
                <div className="emp-work__block">
                  <div className="emp-work__label">{t('employee.worksError')}</div>
                  <div className="emp-work__error">{w.error}</div>
                </div>
              )}
              {w.files && w.files.length > 0 && (
                <div className="emp-work__block">
                  <div className="emp-work__label">{t('employee.worksFiles')}</div>
                  <ul className="emp-work__files">
                    {w.files.map((f) => (
                      <li key={f.path}>
                        <code>{f.path}</code>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {w.result && (
                <div className="emp-work__block">
                  <div className="emp-work__label">{t('employee.worksResult')}</div>
                  <div className="emp-work__result">
                    <MarkdownRenderer content={w.result} />
                  </div>
                </div>
              )}
              {w.status === 'interrupted' && (
                <div className="emp-work__block">
                  {w.resume_note && <div className="emp-work__note">{w.resume_note}</div>}
                  <button
                    type="button"
                    className="btn btn--sm"
                    disabled={resumeBusy === w.task_id}
                    onClick={() => void resumeWork(w.task_id)}
                  >
                    {resumeBusy === w.task_id ? t('employee.worksResuming') : t('employee.worksResume')}
                  </button>
                </div>
              )}
            </details>
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * 「工作记录」里的一张提问卡。
 *
 * 员工的后台任务停下来等回答时，这一屏是他私聊工作台里唯一能作答的地方
 * （群组时间线与主对话的任务面板都按各自的 thread_id 过滤，照不到这里）。
 */
function EmployeeWorkQuestion({
  work,
  onAnswered,
}: {
  work: EmployeeWorkDTO;
  onAnswered: () => void;
}) {
  const payload = work.pending_question;
  /**
   * 本地先落的回答。
   *
   * ★ 必须按 question_id 记住「哪一张」答过了
   * 同一个任务可能先后问好几次；只存一份 answers 的话，第二张卡一出来
   * 就直接是只读的 —— 用户会以为「这题不用答」。
   */
  const [answeredLocal, setAnsweredLocal] = useState<{ id: string; answers: QuestionAnswers } | null>(null);
  if (!payload) return null;
  const answered = answeredLocal?.id === payload.question_id ? answeredLocal.answers : null;

  const stepAt = work.progress.find((step) => step.kind === 'question' && step.status === 'awaiting')?.at;
  const asked = askedFromPayload(
    payload,
    'pending',
    stepAt && !Number.isNaN(Date.parse(stepAt)) ? Date.parse(stepAt) : undefined,
  );
  const cardAsked = answered ? { ...asked, status: 'answered' as const, answers: answered } : asked;

  const submit = async (answers: QuestionAnswers, skipped = false) => {
    try {
      await answerQuestion({
        threadId: work.thread_id,
        callId: payload.call_id,
        questionId: payload.question_id,
        answers,
        skipped,
        runId: payload.run_id,
        employeeId: payload.employee_id,
      });
    } catch (e) {
      if (e instanceof QuestionExpiredError) {
        onAnswered();
        return;
      }
      throw e;
    }
    setAnsweredLocal({ id: payload.question_id, answers: skipped ? {} : answers });
    onAnswered();
  };

  return (
    <QuestionCard
      compact
      asked={cardAsked}
      onSubmit={answered ? undefined : (answers) => submit(answers)}
      onSkip={answered ? undefined : () => submit({}, true)}
    />
  );
}

export function EmployeeChatPage() {
  const t = useT();
  const { employeeId = '' } = useParams();

  const [employee, setEmployee] = useState<Employee | null>(null);
  const [threadId, setThreadId] = useState('');
  const [messages, setMessages] = useState<EmpMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [pending, setPending] = useState<PendingApproval | null>(null);
  const [pendingQuestion, setPendingQuestion] = useState<AskedQuestion | null>(null);
  const [pendingError, setPendingError] = useState('');
  const [fatal, setFatal] = useState('');
  const [tab, setTab] = useState<'work' | 'chat'>('work');
  const liveEmployee = useWorkspaceStore(state => state.employees.find(item => item.id === employeeId));
  const person = liveEmployee || employee;
  const generation = useRef(0);

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  /**
   * 当前挂着的那张提问卡。
   *
   * 除了驱动重渲染，还用 ref 留一份：SSE 事件在异步循环里连续到达，
   * 只读闭包里的 state 会把「同一张卡重复到达」「回答后立刻收到结局」
   * 判错 —— 症状是卡片弹回来，或者点了没反应。
   */
  const pendingQuestionRef = useRef<AskedQuestion | null>(null);

  useEffect(() => {
    let cancelled = false;
    generation.current += 1;
    pendingQuestionRef.current = null;
    setEmployee(null); setMessages([]); setThreadId(''); setFatal(''); setDraft(''); setPending(null); setPendingQuestion(null); setStreaming(false); setTab('work');
    void fetchEmployee(employeeId).then(resp => { if (!cancelled) setEmployee(resp.employee); }).catch(error => { if (!cancelled) setFatal(errorText(error)); });
    void fetchEmployeeThread(employeeId).then(async resp => {
      if (cancelled) return;
      try {
        const history = await fetchConversation(resp.thread_id);
        if (cancelled) return;
        setMessages(history.turns.flatMap(turn => {
          const calls: EmpToolCall[] = [];
          const asked: AskedQuestion[] = [];
          for (const event of turn.events || []) {
            if (event.type === 'tool_call') calls.push({ callId: event.call_id, toolName: event.tool_name, toolLabel: event.tool_label, args: event.args || {}, status: 'pending' });
            if (event.type === 'tool_result') { const call = calls.find(item => item.callId === event.call_id); if (call) { call.status = event.success ? 'success' : 'failed'; call.result = event.result; } }
            // 历史里的提问一律按「已失效」重画：重新打开会话时那张卡已经
            // 不可能再答了，渲染成可点的只会让用户白点一次。
            // 结局事件随后会把它改成真实的（已回答 / 已跳过 / 已超时）。
            if (event.type === 'question') asked.push(askedFromPayload(event, 'expired'));
            if (event.type === 'question_resolved') {
              const target = asked.find(item => item.questionId === event.question_id);
              if (target) {
                target.status = event.status;
                target.answers = event.answers ?? {};
                if (event.reason) target.reason = event.reason;
              }
            }
          }
          return [
            { id: turn.turn_id + '-user', role: 'user' as const, content: turn.user_input, toolCalls: [] },
            {
              id: turn.turn_id + '-assistant',
              role: 'assistant' as const,
              content: turn.answer,
              reasoning: turn.reasoning,
              error: turn.error,
              toolCalls: calls,
              files: turn.files,
              ...(asked.length ? { askedQuestions: asked } : {}),
            },
          ];
        }));
      } catch (error) { if (!cancelled && !(error instanceof ApiError && error.status === 404)) { setFatal('私聊记录读取失败，请重新打开此会话。'); return; } }
      if (!cancelled) setThreadId(resp.thread_id);
    }).catch(error => { if (!cancelled) setFatal(errorText(error)); });
    return () => { cancelled = true; generation.current += 1; abortRef.current?.abort(); };
  }, [employeeId]);

  // 自动滚到底部
  useEffect(() => {
    if (tab === 'chat') scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, tab]);

  const patch = (id: string, fn: (m: EmpMessage) => EmpMessage) => {
    setMessages((prev) => prev.map((m) => (m.id === id ? fn(m) : m)));
  };

  const setLiveQuestion = (next: AskedQuestion | null) => {
    pendingQuestionRef.current = next;
    setPendingQuestion(next);
  };

  const applyEvent = (event: AgentEvent, assistantId: string) => {
    switch (event.type) {
      case 'token':
        patch(assistantId, (m) => ({ ...m, content: m.content + event.content }));
        break;
      case 'reasoning':
        patch(assistantId, (m) => ({ ...m, reasoning: (m.reasoning ?? '') + event.content }));
        break;
      case 'sources':
        patch(assistantId, message => ({ ...message, sources: event.content }));
        break;
      case 'state_update':
        if (event.state_key === 'approval') patch(assistantId, message => ({ ...message, toolCalls: approvalToolStates(message.toolCalls.map(tool => ({ ...tool, dangerLevel: tool.dangerLevel ?? 'safe' })), Boolean(event.value)) }));
        break;
      case 'tool_call':
        patch(assistantId, (m) => {
          if (m.toolCalls.some((tc) => tc.callId === event.call_id)) return m;
          return {
            ...m,
            toolCalls: [
              ...m.toolCalls,
              {
                callId: event.call_id,
                toolName: event.tool_name,
                toolLabel: event.tool_label,
                args: event.args ?? {},
                status: 'pending',
                dangerLevel: (event.danger_level ?? 'safe') as DangerLevel,
              },
            ],
          };
        });
        break;
      case 'tool_result':
        setPending(current => current?.callId === event.call_id ? null : current);
        patch(assistantId, (m) => ({
          ...m,
          toolCalls: m.toolCalls.map((tc) =>
            tc.callId === event.call_id
              ? {
                  ...tc,
                  status: (event.success
                    ? 'success'
                    : ['awaiting_approval', 'rejected'].includes(tc.status)
                      ? 'rejected'
                      : 'failed') as ToolCallInfo['status'],
                  result: event.result,
                  durationMs: event.duration_ms,
                }
              : tc,
          ),
        }));
        break;
      case 'interrupt':
        setPending({
          requestId: event.request_id,
          grantAllowed: event.grant_allowed,
          grantMaxUses: event.grant_max_uses,
          grantTtlSeconds: event.grant_ttl_seconds,
          expiresAt: event.expires_at,
          callId: event.call_id,
          toolName: event.tool_name,
          toolLabel: event.tool_label,
          reason: event.reason,
          args: event.args ?? {},
          runId: event.run_id ?? '',
          employeeId: event.employee_id ?? '',
          requestor: event.requestor ?? '',
        });
        setPendingError('');
        patch(assistantId, (m) => ({
          ...m,
          toolCalls: m.toolCalls.map((tc) =>
            tc.callId === event.call_id ? { ...tc, status: 'awaiting_approval', reason: event.reason } : tc,
          ),
        }));
        break;
      case 'question': {
        const asked = askedFromPayload(event);
        if (pendingQuestionRef.current?.questionId !== asked.questionId) setLiveQuestion(asked);
        patch(assistantId, (m) =>
          m.askedQuestions?.some((q) => q.questionId === asked.questionId)
            ? m
            : { ...m, askedQuestions: [...(m.askedQuestions ?? []), asked] },
        );
        break;
      }
      case 'question_resolved':
        if (pendingQuestionRef.current?.questionId === event.question_id) setLiveQuestion(null);
        setMessages((prev) =>
          patchAskedIn(prev, event.question_id, (q) => ({
            ...q,
            status: event.status,
            answers: event.answers ?? {},
            ...(event.reason ? { reason: event.reason } : {}),
          })),
        );
        break;
      case 'done':
        patch(assistantId, (m) => ({
          ...m,
          streaming: false,
          files: event.files ?? [],
          ...(event.reasoning ? { reasoning: event.reasoning, content: event.answer ?? m.content } : {}),
        }));
        break;
      case 'error':
        patch(assistantId, (m) => ({ ...m, error: event.content, streaming: false }));
        break;
      default:
        break;
    }
  };

  const send = async () => {
    const text = draft.trim();
    if (!text || streaming || !threadId || !person?.enabled) return;
    const epoch = generation.current;
    setDraft('');
    const assistantId = uid();
    setMessages((prev) => [
      ...prev,
      { id: uid(), role: 'user', content: text, toolCalls: [] },
      { id: assistantId, role: 'assistant', content: '', toolCalls: [], streaming: true },
    ]);
    setStreaming(true);

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      for await (const event of employeeChatStream(employeeId, {
        message: text,
        threadId,
        signal: controller.signal,
      })) {
        if (generation.current === epoch) applyEvent(event, assistantId);
      }
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === 'AbortError';
      if (!aborted) patch(assistantId, (m) => ({ ...m, error: errorText(e), streaming: false }));
    } finally {
      if (generation.current !== epoch) return;
      abortRef.current = null;
      setStreaming(false);
      setPending(null);
      // 流断了（网络掉线 / 服务端异常）而提问还挂着：后端会把这个等待
      // 结算成 cancelled，这张卡再也答不了了 —— 收成只读，别留个能点的假象。
      const orphan = pendingQuestionRef.current;
      if (orphan) {
        setLiveQuestion(null);
        setMessages((prev) =>
          patchAskedIn(prev, orphan.questionId, (q) => ({ ...q, status: 'expired', reason: 'cancelled' })),
        );
      }
      patch(assistantId, message => ({ ...message, streaming: false }));
    }
  };

  const decide = async (approved: boolean, scope: 'once' | 'same_operation' = 'once') => {
    if (!pending || !threadId) return;
    const target = pending;
    setPending(null);
    setPendingError('');
    try {
      await submitApproval(threadId, target.callId, approved, target.runId, target.employeeId, scope, target.requestId);
    } catch (e) {
      // 后端对「四维没对上」是硬失败。放回去 + 说清原因，否则用户只看到
      // 提示条自己消失了，以为批过了 —— 而那次危险操作其实还在等。
      setPending(target);
      setPendingError(errorText(e));
    }
  };

  /**
   * 回答（或跳过）员工的问题。
   *
   * 回答走 HTTP 而不是这条 SSE —— 等待方在后端独立地醒，
   * 所以就算前端流断了，只要回答成功，员工也会继续干活。
   */
  const answerAsked = async (asked: AskedQuestion, answers: QuestionAnswers, skipped = false) => {
    if (!threadId) throw new Error('会话还没准备好，请稍后再试。');
    try {
      await answerQuestion({
        threadId,
        callId: asked.callId,
        questionId: asked.questionId,
        answers,
        skipped,
        runId: asked.runId,
        employeeId: asked.employeeId,
      });
    } catch (e) {
      if (e instanceof QuestionExpiredError) {
        // 卡已失效（超时 / 另一个窗口答过）：收成只读，这不是提交失败
        if (pendingQuestionRef.current?.questionId === asked.questionId) setLiveQuestion(null);
        setMessages((prev) => patchAskedIn(prev, asked.questionId, (q) => ({ ...q, status: 'expired' })));
        return;
      }
      throw e;
    }
    const status = skipped ? ('skipped' as const) : ('answered' as const);
    if (pendingQuestionRef.current?.questionId === asked.questionId) setLiveQuestion(null);
    setMessages((prev) =>
      patchAskedIn(prev, asked.questionId, (q) => ({
        ...q,
        status,
        answers: skipped ? {} : answers,
        ...(skipped ? { reason: 'skipped' } : {}),
      })),
    );
  };

  const stop = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
    const live = pendingQuestionRef.current;
    if (live) {
      setLiveQuestion(null);
      setMessages((prev) =>
        patchAskedIn(prev, live.questionId, (q) => ({ ...q, status: 'expired', reason: 'cancelled' })),
      );
    }
  };

  return <WorkspaceShell workspaceId={person?.workspace_id}>
    <div className="main employee-page team-chat">
      <header className="team-chat__header">
        <EmployeeAvatar name={person?.name || '同事'} />
        <div className="team-chat__heading"><h1>{person?.name || '数字同事'}</h1><p>{person?.tagline || '数字员工'} · 私聊</p></div>
        <span className="employee-model-label">{shortModelName(person?.model || '默认模型')}</span>
      </header>
      <div className="employee-tabs" role="tablist" aria-label="员工会话">
        <button type="button" role="tab" aria-selected={tab === 'work'} className={tab === 'work' ? 'is-active' : ''} onClick={() => setTab('work')}>工作记录</button>
        <button type="button" role="tab" aria-selected={tab === 'chat'} className={tab === 'chat' ? 'is-active' : ''} onClick={() => setTab('chat')}>和 TA 聊聊{streaming ? ' · 正在回复' : ''}</button>
      </div>
      <div className="main__scroll" ref={scrollRef}>
        <div className="main__inner employee-page__body">
          {fatal && <div className="subtask-panel subtask-panel--unavailable">{fatal}</div>}
          {!fatal && (
            <>
              {tab === 'work' && <EmployeeWorks key={employeeId} employeeId={employeeId} />}
              {tab === 'chat' && !messages.length && <div className="team-chat__empty"><EmployeeAvatar name={person?.name || '同事'} size="large" /><h2>和{person?.name || '同事'}聊聊</h2><p>直接交代一件事，或追问工作中的细节。</p></div>}
            </>
          )}

          {tab === 'chat' && messages.map((m) =>
            m.role === 'user' ? (
              <div key={m.id} className="emp-user">
                <div className="emp-user__bubble">{m.content}</div>
              </div>
            ) : (
              <div key={m.id} className="emp-assistant">
                <MessageProcess tools={m.toolCalls} reasoning={m.reasoning} streaming={m.streaming}/>
                {m.content && (
                  <div className="emp-assistant__content">
                    <MarkdownRenderer content={m.content} />
                  </div>
                )}
                {m.files && m.files.length > 0 && <TurnFiles files={m.files} threadId={threadId} preview={false}/>}
                {m.askedQuestions && m.askedQuestions.length > 0 && (
                  <div className="msg__questions">
                    {m.askedQuestions.map((q) => {
                      const live = q.questionId === pendingQuestion?.questionId;
                      return (
                        <QuestionCard
                          key={q.questionId}
                          asked={q}
                          onSubmit={live ? (answers) => answerAsked(q, answers) : undefined}
                          onSkip={live ? () => answerAsked(q, {}, true) : undefined}
                        />
                      );
                    })}
                  </div>
                )}
                <MessageSources sources={m.sources} tools={m.toolCalls}/>
                {m.error && <div className="subtask-panel subtask-panel--unavailable">{m.error}</div>}
                {m.streaming && !m.content && !m.toolCalls.length && !m.reasoning && !m.askedQuestions?.length && <span className="emp-typing">正在准备回答…</span>}
              </div>
            ),
          )}
        </div>
      </div>

      {pending && <ApprovalDialog key={pending.requestId || pending.callId} open toolName={pending.toolName} toolLabel={pending.toolLabel} args={pending.args} reason={pendingError ? `${pending.reason}（提交未生效：${pendingError}）` : pending.reason} requestor={pending.requestor} grantAllowed={pending.grantAllowed} grantMaxUses={pending.grantMaxUses} grantTtlSeconds={pending.grantTtlSeconds} expiresAt={pending.expiresAt} onApprove={scope => void decide(true, scope)} onReject={() => void decide(false)} />}

      {tab === 'chat' && <footer className="team-chat__composer"><form onSubmit={event => { event.preventDefault(); void send(); }}>
        <textarea aria-label="发给数字同事" value={draft} maxLength={20000} onChange={event => setDraft(event.target.value)} placeholder={`发消息给 ${person?.name || '同事'}…`} rows={2} disabled={!threadId || !person?.enabled} onKeyDown={event => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); }
        }} />
        <div className="team-chat__composer-actions"><span>仅此私聊可见</span>{streaming ? <button type="button" className="btn btn--sm" onClick={stop}>{t('input.stop')}</button> : <button type="submit" className="team-send" disabled={!draft.trim() || !threadId || !person?.enabled} aria-label="发送私聊消息"><Send size={17} /></button>}</div>
      </form></footer>}
    </div>
  </WorkspaceShell>;
}
