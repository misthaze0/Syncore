import { MarkdownRenderer } from '@/components/MarkdownRenderer';
import { TypingIndicator } from '@/components/TypingIndicator';

/** 一轮可以发多条气泡；新增消息不会重新挂载已经读过的气泡。 */
export function AssistantReplies({ messages, streaming }: { messages: string[]; streaming?: boolean }) {
  return (
    <div className="assistant-replies">
      {messages.map((text, index) => text.trim() ? (
        // 错峰出现：只做视觉上的先后，不改正文到达节奏（那由后端 token 流决定）
        <div
          className="msg__bubble assistant-reply"
          key={index}
          style={{ animationDelay: `${Math.min(index * 90, 270)}ms` }}
        >
          <MarkdownRenderer content={text} />
        </div>
      ) : null)}
      {streaming && !messages[messages.length - 1]?.trim() && (
        <div className="msg__bubble assistant-reply assistant-reply--typing"><TypingIndicator /></div>
      )}
    </div>
  );
}
