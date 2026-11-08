/**
 * 护栏触发通知 —— 回答「为什么这件事停在这儿」。
 *
 * ★ 为什么这条要单独占一个位置，而不是只写进群消息
 * 六护栏共同的硬要求是「必须明说」。少了它，用户看到的只是「任务莫名失败」，
 * 而实际发生过的是「第 3 轮没有任何人接卡，所以这次协作被判失败」——
 * 后者是产品行为，前者像 bug。
 *
 * `message` 是后端组好的成品句子（含轮数、卡数），这里只补「是哪条护栏」的名字，
 * 不在前端重述原因：重述必然与后端组句的那处不一致。
 */

import { ShieldAlert, X } from '@/components/icons';
import { useT } from '@/i18n';
import type { GuardrailNotice as Notice } from '@/types';

interface Props {
  notices: Notice[];
  onDismiss: (index: number) => void;
}

export function GuardrailNotice({ notices, onDismiss }: Props) {
  const t = useT();
  if (notices.length === 0) return null;

  // 缺 key 时 `t` 返回 key 本身（不是空串），所以「是不是已知护栏」必须按下标判断，
  // 不能靠 `||` 兜底 —— 否则将来后端加了 7 号护栏，界面会显示 `group.guardrail.7`。
  const nameOf = (n: number) => (n >= 1 && n <= 6 ? t(`group.guardrail.${n}`) : t('group.guardrailUnknown'));

  return (
    <div className="guardrails">
      {notices.map((item, i) => (
        <div key={`${item.code}-${item.run_id}-${i}`} className="guardrail">
          <ShieldAlert size={13} aria-hidden />
          <span className="guardrail__name">{nameOf(item.guardrail)}</span>
          <span className="guardrail__text">{item.message}</span>
          <button
            type="button"
            className="guardrail__x"
            onClick={() => onDismiss(i)}
            title={t('common.close')}
          >
            <X size={11} />
          </button>
        </div>
      ))}
    </div>
  );
}
