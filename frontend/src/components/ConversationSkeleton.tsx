/**
 * 骨架屏 —— 会话恢复 / 列表加载 / 任务页首拉的占位
 *
 * ★ 为什么是骨架而不是 spinner
 *
 * 「正在恢复会话」要重建的是**一屏**内容（多轮问答 + 工具卡片），
 * 一个转圈的小图标传达不了这个量级；骨架条同时预告了「内容长什么形状」，
 * 恢复完成后视线的落点是连续的。与 Shimmer 细节无关 —— 动画只是
 * 让「它在动」这个事实可感知，避免被当成页面卡死。
 */

import { useT } from '@/i18n';

interface SkeletonProps {
  /** 骨架行数（默认 5 —— 大约一屏对话的密度） */
  rows?: number;
  /** 是否显示顶部提示文字（默认显示「正在恢复会话历史…」） */
  hint?: boolean;
  /** 列表和任务页可使用与当前页面一致的加载提示。 */
  label?: string;
}

export function ConversationSkeleton({ rows = 5, hint = true, label }: SkeletonProps) {
  const t = useT();
  const text = label ?? t('conv.skeleton');
  const items = Array.from({ length: rows }, (_, i) => i);

  return (
    <div className="skeleton" role="status" aria-label={text}>
      {hint && <div className="skeleton__hint">{text}</div>}
      {items.map((i) => (
        <div className={`skeleton__row ${i % 2 === 0 ? 'skeleton__row--user' : ''}`} key={i}>
          <div className="skeleton__avatar" />
          <div className="skeleton__body">
            <div className="skeleton__line" style={{ width: `${72 - (i % 3) * 14}%` }} />
            <div className="skeleton__line skeleton__line--short" style={{ width: `${48 - (i % 2) * 10}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}
