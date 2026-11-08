/**
 * 升级版 Agent 流式 Hook
 *
 * 把 zustand store 中分散的 Agent 运行时状态聚合成一个语义化接口，
 * 组件只需 `const { messages, send, approve, reject } = useAgentStream()`。
 *
 * SSE 的解析与事件分发在 `stores/agentStore.ts` + `lib/sse.ts` 中完成。
 */

import { useCallback } from 'react';

import { useAgentStore } from '@/stores/agentStore';
import type {
  AttachmentMeta,
  ChatMessage,
  ModelOption,
  PendingApproval,
  PendingQuestion,
  PermissionModeInfo,
  PermissionModeValue,
  QuestionAnswers,
  ToolCallInfo,
} from '@/types';

export interface UseAgentStreamResult {
  /** 消息列表 */
  messages: ChatMessage[];
  /** 当前意图 */
  intent: string | null;
  intentConfidence: number | null;
  /** Agent 调用链 */
  agentChain: string[];
  /** 本轮工具调用记录 */
  toolCalls: ToolCallInfo[];
  /** 待审批操作（非空则弹出审批弹窗） */
  pendingApproval: PendingApproval | null;
  /** 正等着回答的提问（非空时那张卡在消息里可操作） */
  pendingQuestion: PendingQuestion | null;
  /** 提交提问的回答（`skipped` 让模型自己决定并停止再问） */
  answerQuestion: (answers: QuestionAnswers, opts?: { skipped?: boolean }) => Promise<void>;
  /** 是否正在流式输出 */
  isStreaming: boolean;
  /** 错误信息 */
  error: string | null;
  /** 当前会话 ID */
  threadId: string;
  /**
   * 有几个后台子任务在跑（Supervisor 架构，见 ADR 0019）。
   *
   * 顶栏据此显示「后台有 N 个任务在进行」——
   * 否则用户看到「对话」两个字会以为系统闲着，
   * 而实际上有任务在跑。
   */
  activeTaskCount: number;

  /** 发送消息（可带已上传好的对话附件） */
  send: (input: string, attachments?: AttachmentMeta[]) => void;
  /** 批准待审批操作 */
  approve: (scope?: 'once' | 'same_operation') => Promise<void>;
  /** 拒绝待审批操作 */
  reject: () => Promise<void>;
  /** 中断当前流 */
  abort: () => void;
  /** 终止当前会话所有还在跑的后台任务（顶栏停止入口） */
  cancelActiveTasks: () => Promise<void>;
  /** 开启新会话 */
  newSession: () => void;
  /** 清空错误 */
  clearError: () => void;
  /** 重新生成某条 assistant 消息 */
  regenerate: (message: ChatMessage) => void;
  /** 编辑某条用户消息并重新发送 */
  editMessage: (messageId: string, newText: string) => void;
  /** 删除某条消息所在的一轮（一问一答） */
  deleteTurn: (message: ChatMessage) => Promise<void>;
  /** 从某条消息所在的一轮分支出新会话 */
  forkFromMessage: (message: ChatMessage) => Promise<void>;

  /** 「已从历史恢复 N 轮上下文」这类一次性提示；为空表示没有 */
  restoreNote: string | null;
  dismissRestoreNote: () => void;
  /** 会话列表是否在加载（也用作「正在恢复历史会话」的指示） */
  historyLoading: boolean;

  // ---------- 本轮选项（联网 / 深度思考）与模型切换 ----------
  /** 是否允许联网搜索（存本地，跨会话保持） */
  webEnabled: boolean;
  setWebEnabled: (value: boolean) => void;
  /** 深度思考开关（存本地，跨会话保持；默认关） */
  thinkingEnabled: boolean;
  setThinkingEnabled: (value: boolean) => void;
  /** 把某个模型切成全局默认（对话里的模型选择就是改配置，没有「只覆盖一句」） */
  switchModel: (model: ModelOption) => Promise<void>;
  /** 候选清单 = 「设置 → 模型」里配好那些（含当前默认） */
  availableModels: ModelOption[];
  /** 本轮权限档位（空串 = 跟随默认档；只能收紧） */
  turnMode: PermissionModeValue | '';
  setTurnMode: (mode: PermissionModeValue | '') => void;
  /** 可以选的那几档（后端算出来的收紧候选，空数组就不渲染选择器） */
  turnModeOptions: PermissionModeInfo[];
  /** 默认档的人话名字 */
  defaultModeLabel: string;
}

export function useAgentStream(): UseAgentStreamResult {
  const messages = useAgentStore((s) => s.messages);
  const intent = useAgentStore((s) => s.intent);
  const intentConfidence = useAgentStore((s) => s.intentConfidence);
  const agentChain = useAgentStore((s) => s.agentChain);
  const toolCalls = useAgentStore((s) => s.toolCalls);
  const pendingApproval = useAgentStore((s) => s.pendingApproval);
  const pendingQuestion = useAgentStore((s) => s.pendingQuestion);
  const answerQuestion = useAgentStore((s) => s.answerQuestion);
  const isStreaming = useAgentStore((s) => s.isStreaming);
  const error = useAgentStore((s) => s.error);
  const threadId = useAgentStore((s) => s.threadId);
  const activeTaskCount = useAgentStore((s) => s.activeTaskCount);

  const sendAction = useAgentStore((s) => s.send);
  const approveAction = useAgentStore((s) => s.approve);
  const rejectAction = useAgentStore((s) => s.reject);
  const abort = useAgentStore((s) => s.abort);
  const cancelActiveTasks = useAgentStore((s) => s.cancelActiveTasks);
  const newSessionAction = useAgentStore((s) => s.newSession);
  const clearError = useAgentStore((s) => s.clearError);
  const regenerateAction = useAgentStore((s) => s.regenerate);
  const editMessageAction = useAgentStore((s) => s.editMessage);
  const deleteTurnAction = useAgentStore((s) => s.deleteTurn);
  const forkFromMessageAction = useAgentStore((s) => s.forkFromMessage);

  const restoreNote = useAgentStore((s) => s.restoreNote);
  const dismissRestoreNote = useAgentStore((s) => s.dismissRestoreNote);
  const historyLoading = useAgentStore((s) => s.conversationsLoading);

  // 本轮选项：联网开关与模型。都存本地（跨会话保持），默认由后端定义。
  const webEnabled = useAgentStore((s) => s.webEnabled);
  const setWebEnabled = useAgentStore((s) => s.setWebEnabled);
  const thinkingEnabled = useAgentStore((s) => s.thinkingEnabled);
  const setThinkingEnabled = useAgentStore((s) => s.setThinkingEnabled);
  const switchModel = useAgentStore((s) => s.switchModel);
  const availableModels = useAgentStore((s) => s.availableModels);
  // 本轮权限档位：只在输入框里往紧里收，默认档来自后端配置
  const turnMode = useAgentStore((s) => s.turnMode);
  const setTurnMode = useAgentStore((s) => s.setTurnMode);
  const turnModeOptions = useAgentStore((s) => s.turnModeOptions);
  const defaultModeLabel = useAgentStore((s) => s.defaultModeLabel);

  const send = useCallback(
    (input: string, attachments?: AttachmentMeta[]) => void sendAction(input, attachments),
    [sendAction],
  );
  const newSession = useCallback(() => newSessionAction(), [newSessionAction]);

  return {
    messages,
    intent,
    intentConfidence,
    agentChain,
    toolCalls,
    pendingApproval,
    pendingQuestion,
    answerQuestion,
    isStreaming,
    error,
    threadId,
    activeTaskCount,
    send,
    approve: approveAction,
    reject: rejectAction,
    abort,
    cancelActiveTasks,
    newSession,
    clearError,
    regenerate: regenerateAction,
    editMessage: editMessageAction,
    deleteTurn: deleteTurnAction,
    forkFromMessage: forkFromMessageAction,
    restoreNote,
    dismissRestoreNote,
    historyLoading,
    webEnabled,
    setWebEnabled,
    thinkingEnabled,
    setThinkingEnabled,
    switchModel,
    availableModels,
    turnMode,
    setTurnMode,
    turnModeOptions,
    defaultModeLabel,
  };
}
