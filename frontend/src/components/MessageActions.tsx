/**
 * 单条消息底部的操作按钮条
 *
 * - 用户消息：复制、编辑、引用
 * - 助手消息：复制、重新生成（仅对最后一条非流式 assistant 显示）、引用
 * - 两种角色都有：删除所在的一轮、从这一轮分支新会话（ destructive 的两个，
 *   由 ChatPage 决定是否传入 —— 例如流式中不传）
 *
 * 参考 vercel/ai-chatbot 的 message-actions，但用零依赖实现。
 */

import { memo, useCallback, useState } from 'react';

import { Check, Copy, GitFork, Pencil, Quote, RotateCcw, Trash2 } from '@/components/icons';

import { useT } from '@/i18n';
import { toast } from '@/stores/toastStore';
import type { ChatMessage } from '@/types';

interface MessageActionsProps {
  message: ChatMessage;
  isLatestAssistant: boolean;
  isStreaming: boolean;
  onEdit?: (message: ChatMessage) => void;
  onRegenerate?: (message: ChatMessage) => void;
  onDeleteTurn?: (message: ChatMessage) => void;
  onFork?: (message: ChatMessage) => void;
  onQuote?: (message: ChatMessage) => void;
}

function useCopy() {
  const [copied, setCopied] = useState(false);
  const copy = useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      toast('复制失败，请选择消息后手动复制', 'error');
    }
  }, []);
  return { copied, copy };
}

export const MessageActions = memo(function MessageActions({
  message,
  isLatestAssistant,
  isStreaming,
  onEdit,
  onRegenerate,
  onDeleteTurn,
  onFork,
  onQuote,
}: MessageActionsProps) {
  const t = useT();
  const { copied, copy } = useCopy();
  const isUser = message.role === 'user';

  const handleCopy = useCallback(() => {
    void copy(message.content);
  }, [copy, message.content]);

  const handleEdit = useCallback(() => {
    onEdit?.(message);
  }, [onEdit, message]);

  const handleRegenerate = useCallback(() => {
    onRegenerate?.(message);
  }, [onRegenerate, message]);

  return (
    <div className="msg-actions">
      <button
        type="button"
        className="msg-actions__btn"
        onClick={handleCopy}
        title={copied ? t('common.copied') : t('common.copy')}
      >
        {copied ? <Check size={12} /> : <Copy size={12} />}
        {copied ? t('common.copied') : t('common.copy')}
      </button>

      {isUser && onEdit && (
        <button type="button" className="msg-actions__btn" onClick={handleEdit} title={t('msg.edit')}>
          <Pencil size={12} />
          {t('msg.edit').split(' ')[0]}
        </button>
      )}

      {!isUser && isLatestAssistant && !isStreaming && onRegenerate && (
        <button
          type="button"
          className="msg-actions__btn"
          onClick={handleRegenerate}
          title={t('msg.regenerate')}
        >
          <RotateCcw size={12} />
          {t('msg.regenerate').split(' ')[0]}
        </button>
      )}

      {onQuote && (
        <button
          type="button"
          className="msg-actions__btn"
          onClick={() => onQuote(message)}
          title={t('msg.quote')}
        >
          <Quote size={12} />
        </button>
      )}

      {onFork && (
        <button
          type="button"
          className="msg-actions__btn"
          onClick={() => onFork(message)}
          title={t('msg.fork')}
        >
          <GitFork size={12} />
        </button>
      )}

      {onDeleteTurn && !isStreaming && (
        <button
          type="button"
          className="msg-actions__btn msg-actions__btn--danger"
          onClick={() => onDeleteTurn(message)}
          title={t('msg.delete')}
        >
          <Trash2 size={12} />
        </button>
      )}
    </div>
  );
});
