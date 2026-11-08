/**
 * 流式输出打字指示器
 */

export function TypingIndicator() {
  return (
    <span className="typing" aria-label="正在生成回答">
      <span className="typing__dot" />
      <span className="typing__dot" />
      <span className="typing__dot" />
    </span>
  );
}
