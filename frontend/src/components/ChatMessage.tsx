/**
 * 消息渲染 —— 支持 Markdown、来源标注、工具调用卡片、消息操作
 */

import { memo, useEffect, useRef, useState } from 'react';

import { Bell, Brain, CircleCheck, Clock, Download, History, Paperclip, TriangleAlert, Volume2, Zap } from '@/components/icons';

import assistantAvatar from '@/assets/brand/app-icon.png';

import { MarkdownRenderer } from '@/components/MarkdownRenderer';
import { AssistantReplies } from '@/components/AssistantReplies';
import { MessageActions } from '@/components/MessageActions';
import { MessageProcess } from '@/components/MessageProcess';
import { MessageSources } from '@/components/MessageSources';
import { QuestionCard } from '@/components/QuestionCard';
import { TurnFiles } from '@/components/TurnFiles';
import { TypingIndicator } from '@/components/TypingIndicator';
import { downloadAttachmentFile, synthesizeSpeech } from '@/lib/api';
import { toolActivity } from '@/lib/messagePresentation';
import { HOOK_EVENT_LABEL, formatUsageLine, usageDetail } from '@/lib/humanize';
import { toast } from '@/stores/toastStore';
import type { ChatMessage as ChatMessageType, QuestionAnswers } from '@/types';

interface ChatMessageProps {
  message: ChatMessageType;
  isLatestAssistant: boolean;
  isStreaming: boolean;
  /**
   * 助理会话的呈现：过程信息（工具/思考）与用量都收进折叠区。
   *
   * 她平时是在聊天，不是交工作报告 —— 把「1.2s · 843 tokens」摆在每句家常话
   * 底下会立刻把人拉回「这是个系统」的感觉。普通对话保持原样（那里常是任务）。
   */
  assistantChrome?: boolean;
  onEdit?: (message: ChatMessageType) => void;
  onRegenerate?: (message: ChatMessageType) => void;
  onDeleteTurn?: (message: ChatMessageType) => void;
  onFork?: (message: ChatMessageType) => void;
  onQuote?: (message: ChatMessageType) => void;
  /**
   * 回答模型主动提问（`ask_user`）。
   *
   * 只有**当前还活着**的那张卡才拿得到这两个回调 —— 历史重放、
   * 已回答、已超时的卡一律不传，卡片自己会渲染成只读（见 QuestionCard）。
   */
  onAnswerQuestion?: (answers: QuestionAnswers) => Promise<void>;
  onSkipQuestion?: () => Promise<void>;
}

export const ChatMessage = memo(function ChatMessage({
  message,
  isLatestAssistant,
  isStreaming,
  assistantChrome = false,
  onEdit,
  onRegenerate,
  onDeleteTurn,
  onFork,
  onQuote,
  onAnswerQuestion,
  onSkipQuestion,
}: ChatMessageProps) {
  const isUser = message.role === 'user';
  const toolCalls = message.toolCalls ?? [];
  const sources = message.sources ?? [];
  const isProactive = !isUser && message.initiatedBy === 'assistant';
  const usageLine = !isUser && !message.streaming
    ? formatUsageLine(message.usage, message.durationMs)
    : '';
  const hasProcess = toolCalls.length > 0 || Boolean(message.reasoning);
  // 助理会话：用量只在展开「工作过程」时出现；没有工具/思考的纯聊天不显示。
  // 普通对话保持回答下方那行小字（那里多半是真的办了一件事，成本值得说）。
  const usageInProcess = assistantChrome && hasProcess ? usageLine : '';
  const showUsageLine = Boolean(usageLine) && !assistantChrome;
  // 朗读
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const audioUrl = useRef<string | null>(null);
  const speechRequest = useRef(0);
  const [speechLoading, setSpeechLoading] = useState(false);
  const releaseAudio = () => { if (audioRef.current) { audioRef.current.onended = null; audioRef.current.onerror = null; audioRef.current.pause(); } audioRef.current = null; if (audioUrl.current) URL.revokeObjectURL(audioUrl.current); audioUrl.current = null; };
  useEffect(() => () => { speechRequest.current++; releaseAudio(); }, []);
  const [speaking, setSpeaking] = useState(false);
  const [speechError, setSpeechError] = useState<string | null>(null);

  const toggleSpeak = async () => {
    if (speaking) {
      speechRequest.current++;
      releaseAudio();
      setSpeaking(false);
      return;
    }
    if (speechLoading) return;
    const request = ++speechRequest.current;
    setSpeechLoading(true);
    setSpeechError(null);
    try {
      const blob = await synthesizeSpeech(message.content);
      if (request !== speechRequest.current) return;
      const url = URL.createObjectURL(blob);
      audioUrl.current = url;
      const audio = new Audio(url);
      audio.onended = () => {
        setSpeaking(false);
        releaseAudio();
      };
      audio.onerror = () => {
        setSpeaking(false);
        setSpeechError('音频播放失败');
        releaseAudio();
      };
      audioRef.current = audio;
      setSpeaking(true);
      await audio.play();
    } catch (e) {
      if (request !== speechRequest.current) return;
      releaseAudio();
      setSpeechError(e instanceof Error ? e.message : '语音合成失败');
      setSpeaking(false);
    } finally { if (request === speechRequest.current) setSpeechLoading(false); }
  };

  return (
    <div className={`msg msg--${isUser ? 'user' : 'assistant'}${isProactive ? ' msg--proactive' : ''}`}>
      <div className="msg__avatar" aria-hidden>
        {isUser ? '我' : <img src={assistantAvatar} alt="" />}
      </div>

      <div className="msg__body">
        <div className={!isUser && message.replyMessages ? 'msg__result' : 'msg__bubble'}>
          {isUser ? (
            <>
              {(message.attachments?.length ?? 0) > 0 && (
                <div className="msg__attachments">
                  {message.attachments!.map((a) => (
                    <span
                      className="attachment-chip"
                      key={a.attachment_id}
                      title={`对话附件 · ${a.chars > 0 ? `已读取 ${a.chars} 字` : '没有解析出文本'}${a.truncated ? '（过长已截断）' : ''}`}
                    >
                      <Paperclip size={11} />
                      <span className="attachment-chip__name">{a.filename}</span>
                      {a.chars === 0 && <span className="attachment-chip__warn">未能读取</span>}
                      {a.truncated && <span className="attachment-chip__warn">已截断</span>}
                      {a.original_available !== false && (
                        <button
                          type="button"
                          className="attachment-chip__download"
                          title="下载原件（24 小时内有效）"
                          aria-label={`下载原件 ${a.filename}`}
                          onClick={(event) => {
                            event.stopPropagation();
                            void downloadAttachmentFile(a.attachment_id, a.filename).catch(
                              (error: unknown) =>
                                toast(
                                  error instanceof Error ? error.message : '原件下载失败',
                                  'error',
                                ),
                            );
                          }}
                        >
                          <Download size={11} />
                        </button>
                      )}
                    </span>
                  ))}
                </div>
              )}
              {message.content}
            </>
          ) : (
            <>
              <MessageProcess
                tools={toolCalls}
                reasoning={message.reasoning}
                streaming={message.streaming}
                usageText={usageInProcess}
                usageTitle={usageDetail(message.usage)}
              />

              {/* 完成汇报的完成标记：顺序上在总结正文**前面** —— 用户看到
                  「上面那段分发对话 → 这里完成 → 下面是总结」的因果链 */}
              {typeof message.taskCount === 'number' && message.taskCount > 0 && (
                <div className={'msg__task-report' + (message.streaming ? ' msg__task-report--running' : message.taskStatus && message.taskStatus !== 'succeeded' ? ' msg__task-report--attention' : '')}>
                  {message.taskStatus && message.taskStatus !== 'succeeded' ? <TriangleAlert size={12} aria-hidden /> : <CircleCheck size={12} aria-hidden />}
                  {message.streaming ? '正在整理任务结果' : message.taskStatus === 'partial' ? '部分完成 · 仍有步骤未交付' : message.taskStatus === 'failed' ? '任务未完成' : `${message.taskCount} 项后台任务已完成`}
                </div>
              )}

              {message.replyMessages ? (
                <AssistantReplies messages={message.replyMessages} streaming={message.streaming} />
              ) : message.content ? (
                <MarkdownRenderer content={message.content} />
              ) : message.streaming &&
                toolCalls.length === 0 &&
                !message.reasoning &&
                !message.askedQuestions?.some((q) => q.status === 'pending') ? (
                // 停在提问上时那一轮的 SSE 还开着 —— 画一个「正在输入」等于
                // 告诉她马上会说话，而实际上球在用户这边
                <TypingIndicator />
              ) : null}

              {/* 本轮产出的文件 —— 用户问「你到底给我做了什么」时的直接答案 */}
              {(message.files?.length ?? 0) > 0 && <TurnFiles files={message.files!} />}

              <MessageSources sources={sources} tools={toolCalls}/>

              {/* 模型主动提问：内联卡片，不是审批弹窗 */}
              {(message.askedQuestions?.length ?? 0) > 0 && (
                <div className="msg__questions">
                  {message.askedQuestions!.map((asked) => (
                    <QuestionCard
                      key={asked.questionId}
                      asked={asked}
                      onSubmit={onAnswerQuestion}
                      onSkip={onSkipQuestion}
                    />
                  ))}
                </div>
              )}
            </>
          )}
        </div>

        {/* 消息操作条 */}
        {message.content && !message.streaming && (
          <MessageActions
            message={message}
            isLatestAssistant={isLatestAssistant}
            isStreaming={isStreaming}
            onEdit={onEdit}
            onRegenerate={onRegenerate}
            onDeleteTurn={onDeleteTurn}
            onFork={onFork}
            onQuote={onQuote}
          />
        )}

        {/* 预授权自动放行回执 */}
        {!isUser && (message.autoApprovals?.length ?? 0) > 0 && (
          <div className="msg__auto-approvals">
            {message.autoApprovals!.map((note) => (
              <div
                className="auto-approval"
                key={note.callId}
                title={`已按你的预授权自动放行 · ${
                  note.remaining > 0
                    ? `本次授权还剩 ${note.remaining} 次`
                    : '本次授权已用尽，下次会重新向你确认'
                }`}
              >
                <span className="auto-approval__icon">
                  <Zap size={12} />
                </span>
                <span className="auto-approval__text">按预授权允许：{toolActivity({ toolName: note.toolName, toolLabel: note.toolLabel, args: toolCalls.find(tool => tool.callId === note.callId)?.args ?? {} }, 'success')}</span>
              </div>
            ))}
          </div>
        )}

        {/*
          钩子要给用户看的一句话（Phase 5）。
          非阻塞、不进正文、不改工具状态 —— 一条脚本没有打断对话的资格，
          它拦截时会走审批/拒绝那条正路，不会以这个事件出现。
        */}
        {!isUser && (message.hookNotices?.length ?? 0) > 0 && (
          <div className="msg__hook-notices">
            {message.hookNotices!.map((note, i) => (
              <div className="hook-notice" key={`${note.event}-${note.content}-${i}`}>
                <span className="hook-notice__icon">
                  <Bell size={12} />
                </span>
                <span className="hook-notice__tag">{HOOK_EVENT_LABEL[note.event]}</span>
                <span className="hook-notice__text">
                  {note.content}
                  {note.toolName ? <code> {note.toolLabel || note.toolName}</code> : null}
                </span>
              </div>
            ))}
          </div>
        )}

        {message.error && (
          <div className="msg__error">
            <TriangleAlert size={12} style={{ flexShrink: 0, marginTop: 2 }} />
            {message.error}
          </div>
        )}

        {/*
          非正常收尾的轮次（只在从会话历史恢复时才有这个信息）。
          必须显式说出来：否则用户看到一个半截回答，会以为是恢复过程把它截断了。
        */}
        {!isUser && message.interrupted && (
          <div className="msg__interrupted">
            <History size={12} style={{ flexShrink: 0, marginTop: 2 }} />
            {message.interrupted.status === 'interrupted'
              ? '这一轮停在人工确认等待，没有生成完整回答'
              : `这一轮没有正常收尾（${message.interrupted.label}）`}
          </div>
        )}

        {!isUser && message.content && !message.streaming && (
          <div className="msg__speak">
            <button type="button" className="speak-btn" disabled={speechLoading} onClick={toggleSpeak} title={speechError ?? '朗读这条回答'}>
              {speaking ? (
                <>
                  <Volume2 size={13} /> 停止朗读
                </>
              ) : (
                <>
                  <Volume2 size={13} /> {speechLoading ? '准备音频…' : '朗读'}
                </>
              )}
            </button>
            {speechError && <span className="speak-btn__error">{speechError}</span>}
          </div>
        )}

        {!isUser && (message.memoryNotes?.length ?? 0) > 0 && (
          <div className="msg__memory-notes">
            {message.memoryNotes!.map((note, i) => (
              <div className="memory-note" key={`${note}-${i}`} title="已写入长期记忆，后续对话会自动带上这条偏好">
                <span className="memory-note__icon">
                  <Brain size={12} />
                </span>
                <span>{note}</span>
              </div>
            ))}
          </div>
        )}

        {/*
          本轮用量（P1-10）：回答下方一行小字「1.2s · 843 tokens」。
          只放**用户能直接感知**的两个数；明细（输入/输出/思考占比）收进悬停 ——
          第一屏不需要一张账单（渐进式披露）。
          拿不到任何数据就**整行不渲染**：显示「0 tokens」比不显示更糟。
        */}
        {showUsageLine && (
          <div className="msg__usage" title={usageDetail(message.usage)}>
            <Clock size={11} />
            {usageLine}
          </div>
        )}
      </div>
    </div>
  );
});
