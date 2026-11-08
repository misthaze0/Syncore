/**
 * 提问载荷 → 界面形状的唯一转换处。
 *
 * 三个来源（主对话 SSE、任务表 `pending_question`、群组快照）发的是**同一个**
 * snake_case 载荷；转成 camelCase 的 `AskedQuestion` 只在这里做一次 ——
 * 各处自己拼的话，迟早有一处把 `question_id` 忘掉，而症状是「卡片点不动」。
 */

import type { AskedQuestion, QuestionPayload, QuestionState } from '@/types';

/**
 * @param status 实时收到的载荷是 `pending`；**历史重放**一律给 `expired` ——
 *   重新打开会话时那张卡已经不可能再答了，渲染成可点的只会让用户白点一次。
 *   随后的 `question_resolved` 会把它改成真实的结局（已回答 / 已跳过 / 已超时）。
 * @param askedAt 卡片首次出现的时刻（毫秒）。任务表的 `pending_question` 每次轮询
 *   都是新对象，用 `Date.now()` 会让倒计时永远重新开始 —— 调用方传进度步骤的 `at`。
 */
export function askedFromPayload(
  payload: QuestionPayload,
  status: QuestionState = 'pending',
  askedAt?: number,
): AskedQuestion {
  return {
    questionId: payload.question_id,
    callId: payload.call_id,
    questions: payload.questions ?? [],
    status,
    answers: {},
    ...(payload.requestor ? { requestor: payload.requestor } : {}),
    runId: payload.run_id ?? '',
    employeeId: payload.employee_id ?? '',
    ...(payload.thread_id ? { threadId: payload.thread_id } : {}),
    ...(payload.timeout_seconds ? { timeoutSeconds: payload.timeout_seconds } : {}),
    askedAt: askedAt ?? payload.asked_at ?? Date.now(),
  };
}
