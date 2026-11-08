/**
 * 模型主动提问的卡片（`ask_user` 的界面半边）。
 *
 * ★ 它不是审批卡的换皮
 * 审批问的是「这份操作放不放行」——二元、有风险、点错就出事，所以走阻塞弹窗；
 * 提问问的是「这几个方案选哪个」——多选、无风险、答错可以改，所以是**内联**的：
 * 回答它的同时，用户还能看见上下文。
 *
 * ★ 为什么每题都有「其他」
 * 模型的选项是从上下文里猜的，猜不全才是常态。没有自由输入，用户只能挑一个
 * 最接近的，而模型会把它当成真实意图继续干活 —— 偏差从这里开始。
 * 「其他」的所有权在用户，选项的所有权在模型。
 *
 * ★ 允许只答一部分
 * 后端把没答的问题标成「未回答」交给模型按合理假设继续（不是整张作废）。
 * 「跳过」是另一回事：一个都不答、让模型别再问。两者按钮分开，语义才不糊。
 *
 * ★ 只读态
 * 历史会话 / 已回答 / 已跳过 / 超时的卡片不再可点：留一张能点但点了没反应的卡，
 * 比留一张明确写着「已失效」的卡更糟。
 */

import { useEffect, useMemo, useState } from 'react';

import {
  CircleCheck,
  CircleSlash,
  Clock,
  Hourglass,
  MessageSquare,
  Send,
  Sparkles,
  TriangleAlert,
} from '@/components/icons';
import { useT } from '@/i18n';
import type {
  AskedQuestion,
  QuestionAnswers,
  QuestionOption,
  QuestionSpec,
  QuestionState,
} from '@/types';

interface Props {
  asked: AskedQuestion;
  /** 提交回答。抛错时卡片保留输入并显示错误（不静默吞掉）。 */
  onSubmit?: (answers: QuestionAnswers) => Promise<void>;
  /** 跳过：让模型基于现有信息自己决定，并且不要再问同一组问题。 */
  onSkip?: () => Promise<void>;
  /** 紧凑排版（嵌在任务面板 / 群组时间线里时少一点留白） */
  compact?: boolean;
}

function emptyDraft(questions: QuestionSpec[]): QuestionAnswers {
  const draft: QuestionAnswers = {};
  for (const q of questions) draft[q.id] = { selected: [], other: '' };
  return draft;
}

function hasAnyAnswer(answers: QuestionAnswers): boolean {
  return Object.values(answers).some((a) => a.selected.length > 0 || a.other.trim().length > 0);
}

function statusLabel(status: QuestionState, t: (key: string) => string): string {
  switch (status) {
    case 'answered':
      return t('question.answered');
    case 'skipped':
      return t('question.skipped');
    case 'timeout':
      return t('question.timedOut');
    case 'expired':
      return t('question.expired');
    default:
      return '';
  }
}

function StatusIcon({ status }: { status: QuestionState }) {
  switch (status) {
    case 'answered':
      return <CircleCheck size={11} aria-hidden />;
    case 'skipped':
      return <CircleSlash size={11} aria-hidden />;
    case 'timeout':
      return <Hourglass size={11} aria-hidden />;
    case 'expired':
      return <TriangleAlert size={11} aria-hidden />;
    default:
      return null;
  }
}

/** 剩余秒数（只在真的有时限、且卡片还活着时倒数） */
function useRemainingSeconds(asked: AskedQuestion, enabled: boolean): number | null {
  const deadline = asked.timeoutSeconds ? asked.askedAt + asked.timeoutSeconds * 1000 : null;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled || deadline === null) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [enabled, deadline]);
  if (deadline === null) return null;
  return Math.max(0, Math.round((deadline - now) / 1000));
}

function formatClock(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function QuestionCard({ asked, onSubmit, onSkip, compact = false }: Props) {
  const t = useT();
  const live = asked.status === 'pending' && Boolean(onSubmit);
  const [draft, setDraft] = useState<QuestionAnswers>(() => emptyDraft(asked.questions));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const remaining = useRemainingSeconds(asked, live);
  const answers = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(draft).filter(
          ([, a]) => a.selected.length > 0 || a.other.trim().length > 0,
        ),
      ) as QuestionAnswers,
    [draft],
  );
  const canSend = hasAnyAnswer(answers);

  const pick = (question: QuestionSpec, option: QuestionOption) => {
    setDraft((prev) => {
      const cur = prev[question.id] ?? { selected: [], other: '' };
      if (question.multi_select) {
        const selected = cur.selected.includes(option.label)
          ? cur.selected.filter((label) => label !== option.label)
          : [...cur.selected, option.label];
        return { ...prev, [question.id]: { ...cur, selected } };
      }
      // 单选：选了选项就清掉「其他」——同时留着两个答案时模型无法判断以谁为准
      return { ...prev, [question.id]: { selected: [option.label], other: '' } };
    });
    setError('');
  };

  const typeOther = (question: QuestionSpec, value: string) => {
    setDraft((prev) => {
      const cur = prev[question.id] ?? { selected: [], other: '' };
      // 单选时自由输入覆盖选项；多选时它是补充（两样一起交给模型）
      return {
        ...prev,
        [question.id]: { selected: question.multi_select ? cur.selected : [], other: value },
      };
    });
    setError('');
  };

  const submit = async () => {
    if (!onSubmit || busy || !canSend) return;
    setBusy(true);
    setError('');
    try {
      await onSubmit(answers);
    } catch (e) {
      setError(e instanceof Error ? e.message : t('question.sendFailed'));
    } finally {
      setBusy(false);
    }
  };

  const skip = async () => {
    if (!onSkip || busy) return;
    setBusy(true);
    setError('');
    try {
      await onSkip();
    } catch (e) {
      setError(e instanceof Error ? e.message : t('question.sendFailed'));
    } finally {
      setBusy(false);
    }
  };

  const title = asked.requestor
    ? t('question.titleFrom').replace('{who}', asked.requestor)
    : t('question.title');

  return (
    <section
      className={`q-card q-card--${asked.status}${compact ? ' q-card--compact' : ''}`}
      aria-label={t('question.aria')}
    >
      <header className="q-card__head">
        <MessageSquare size={13} aria-hidden />
        <span className="q-card__title">{title}</span>
        {live && remaining !== null && remaining <= 120 && (
          <span className="q-card__clock" title={t('question.timeoutHint')}>
            <Clock size={11} aria-hidden />
            {formatClock(remaining)}
          </span>
        )}
        {!live && asked.status !== 'pending' && (
          <span className={`q-card__state q-card__state--${asked.status}`}>
            <StatusIcon status={asked.status} />
            {statusLabel(asked.status, t)}
          </span>
        )}
      </header>

      <ol className="q-card__questions">
        {asked.questions.map((question, index) => {
          const current = draft[question.id] ?? { selected: [], other: '' };
          const answered = asked.answers[question.id];
          return (
            <li className="q-question" key={question.id}>
              {question.header && <span className="q-question__header">{question.header}</span>}
              <div className="q-question__line">
                {asked.questions.length > 1 && (
                  <span className="q-question__index">{index + 1}</span>
                )}
                <span className="q-question__text">{question.question}</span>
                {live && question.multi_select && (
                  <span className="q-question__multi">{t('question.multi')}</span>
                )}
              </div>

              {live ? (
                <>
                  <div
                    className="q-options"
                    role={question.multi_select ? 'group' : 'radiogroup'}
                    aria-label={question.question}
                  >
                    {question.options.map((option) => {
                      const checked = current.selected.includes(option.label);
                      return (
                        <label
                          key={option.label}
                          className={`q-option${checked ? ' is-checked' : ''}${
                            question.multi_select ? ' q-option--multi' : ''
                          }`}
                        >
                          <input
                            type={question.multi_select ? 'checkbox' : 'radio'}
                            name={`${asked.questionId}-${question.id}`}
                            checked={checked}
                            disabled={busy}
                            onChange={() => pick(question, option)}
                          />
                          <span className="q-option__mark" aria-hidden>
                            {checked && <CircleCheck size={10} />}
                          </span>
                          <span className="q-option__body">
                            <span className="q-option__label">
                              {option.label}
                              {option.recommended && (
                                <span className="q-option__rec">
                                  <Sparkles size={10} aria-hidden />
                                  {t('question.recommended')}
                                </span>
                              )}
                            </span>
                            {option.description && (
                              <span className="q-option__desc">{option.description}</span>
                            )}
                          </span>
                        </label>
                      );
                    })}
                    <label className="q-other">
                      <input
                        type="text"
                        value={current.other}
                        disabled={busy}
                        maxLength={2000}
                        placeholder={t('question.otherPlaceholder')}
                        onChange={(e) => typeOther(question, e.target.value)}
                      />
                    </label>
                  </div>
                </>
              ) : answered && (answered.selected.length > 0 || answered.other) ? (
                <div className="q-answer">
                  {answered.selected.map((label) => (
                    <span className="q-answer__chip" key={label}>
                      <CircleCheck size={10} aria-hidden />
                      {label}
                    </span>
                  ))}
                  {answered.other && <span className="q-answer__other">{answered.other}</span>}
                </div>
              ) : (
                <span className="q-answer__none">{t('question.unanswered')}</span>
              )}
            </li>
          );
        })}
      </ol>

      {error && (
        <p className="q-card__error" role="alert">
          <TriangleAlert size={11} aria-hidden />
          {error}
        </p>
      )}

      {live && (
        <footer className="q-card__foot">
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            disabled={busy || !onSkip}
            onClick={() => void skip()}
            title={t('question.skipHint')}
          >
            {t('question.skip')}
          </button>
          <button
            type="button"
            className="btn btn--sm btn--primary"
            disabled={busy || !canSend}
            onClick={() => void submit()}
            title={canSend ? t('question.sendHint') : t('question.needAnswer')}
          >
            <Send size={11} aria-hidden />
            {busy ? t('question.sending') : t('question.send')}
          </button>
        </footer>
      )}
    </section>
  );
}
