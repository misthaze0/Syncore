/**
 * 主聊天页面
 *
 * 结构：侧边栏 + （顶栏状态条 / 消息滚动区 / 输入区）+ HITL 审批弹窗
 *
 * ★ 滚动跟随为什么是「贴底才跟随」
 *
 * 自动滚动如果无条件滚底，用户往上翻阅历史时会被新 token 一把拽回来 ——
 * 阅读被打断比错过滚动本身糟糕得多。所以这里区分两个状态：
 * - 贴底（距底 < 120px）：跟随滚动，这是「正在看回答」的用户的预期；
 * - 离底：不跟随，改为显示「N 条新消息」按钮，点击回底并恢复跟随。
 * 「用户主动滚到底」与「点按钮」都会把 sticky 重新置位。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  ArrowDown,
  Bot,
  Download,
  History,
  Languages,
  MessageSquare,
  PanelRight,
  Search,
  Square,
  TriangleAlert,
  Type,
  X,
} from '@/components/icons';

import brandMark from '@/assets/brand/wordmark.png';
import { AgentStatus } from '@/components/AgentStatus';
import { ComputerControlStatus } from '@/components/ComputerControlStatus';
import { SubtaskPanel } from '@/components/SubtaskPanel';
import { ApprovalDialog } from '@/components/ApprovalDialog';
import { ArtifactPanel } from '@/components/ArtifactPanel';
import { ChatInput, type ChatInputRef } from '@/components/ChatInput';
import { ChatMessage } from '@/components/ChatMessage';
import { CommandPalette } from '@/components/CommandPalette';
import { ConversationSkeleton } from '@/components/ConversationSkeleton';
import { useAgentStream } from '@/hooks/useAgentStream';
import { useFontSize } from '@/hooks/useFontSize';
import { useWallpaper } from '@/hooks/useWallpaper';
import { useT, useLangStore } from '@/i18n';
import { fetchChannels, fetchPersona } from '@/lib/api';
import { conversationToMarkdown, downloadTextFile, safeFileName } from '@/lib/exportConversation';
import { toolActivity } from '@/lib/messagePresentation';
import { intentProgress, subtaskSummary } from '@/lib/humanize';
import { closeArtifact, useSidePanelStore } from '@/stores/artifactStore';
import { takeComposerDraft } from '@/stores/composerStore';
import { toast } from '@/stores/toastStore';
import { confirmAction } from '@/stores/confirmStore';
import { useWorkdirStore } from '@/stores/workdirStore';
import { useAgentStore } from '@/stores/agentStore';
import type {
  AttachmentMeta,
  ChannelState,
  ChatMessage as ChatMessageType,
  PersonaProfile,
  QuickCommand,
} from '@/types';

/** 一次渲染多少条消息（更早的折叠进「加载更早消息」按钮） */
const MESSAGE_WINDOW = 60;

/** 距底部多少像素以内算「贴底」（跟随滚动的判定阈值） */
const STICKY_THRESHOLD_PX = 120;

export function ChatPage({
  onOpenSettings,
  chrome: requestedChrome = 'chat',
}: {
  onOpenSettings: (tab?: string) => void;
  /** 'assistant'：助理页 —— 那条固定单例会话 + 顶栏换成助理身份与渠道状态（侧栏仍是普通对话列表） */
  chrome?: 'chat' | 'assistant';
}) {
  const {
    messages,
    intent,
    agentChain,
    pendingApproval,
    pendingQuestion,
    answerQuestion,
    isStreaming,
    error,
    threadId,
    activeTaskCount,
    send,
    approve,
    reject,
    abort,
    cancelActiveTasks,
    newSession,
    clearError,
    regenerate,
    editMessage,
    deleteTurn,
    forkFromMessage,
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
  } = useAgentStream();
  const activeScope = useAgentStore((s) => s.scope);
  const chrome = isStreaming ? (activeScope === 'assistant' ? 'assistant' : 'chat') : requestedChrome;
  const { label: fontLabel, cycle: cycleFont } = useFontSize();
  const { wallpaper } = useWallpaper();
  const t = useT();
  useEffect(() => {
    if (requestedChrome === 'chat') {
      try { localStorage.setItem('xjms.mode', 'chat'); } catch { /* Session only. */ }
    }
  }, [requestedChrome]);
  const toggleLang = useLangStore((s) => s.toggle);
  // 右侧栏（产物 / 浏览器 / 终端）：顶栏开关 + 高亮态
  const panelOpen = useSidePanelStore((s) => s.panelOpen);
  const togglePanel = useSidePanelStore((s) => s.togglePanel);

  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<ChatInputRef>(null);
  /**
   * 「贴底跟随」标记：true 时 token 到达就滚底。
   * 用 ref 而不是 state —— 滚动事件高频触发，state 会把每次滚动都变成重渲染。
   */
  const stickyRef = useRef(true);
  const [showScrollButton, setShowScrollButton] = useState(false);
  /** 离底期间到达的新消息数（贴底时清零）—— 按钮上显示「N 条新消息」 */
  const [unreadCount, setUnreadCount] = useState(0);
  /** 窗口化：渲染最近 N 条，更早的折叠（长会话不卡） */
  const [windowSize, setWindowSize] = useState(MESSAGE_WINDOW);
  const [editingId, setEditingId] = useState<string | null>(null);
  /** 被引用的消息（转成「回复：原文」前缀放进输入框） */
  const [quotingId, setQuotingId] = useState<string | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  // 人设卡：欢迎态与助理顶栏的头像/名字/开场白都跟着它走 —— 用户在设置里改了，这里立刻可见
  const [persona, setPersona] = useState<PersonaProfile | null>(null);
  const personaName = persona?.name || 'Syncore';

  useEffect(() => {
    fetchPersona()
      .then((res) => setPersona(res.profile))
      .catch(() => {
        /* 人设接口不可用时用默认样子 —— 欢迎语不该因此报错 */
      });
  }, []);

  /**
   * 命名空间切换：/chat 与 /assistant 是两场互不相干的对话。
   *
   * ★ 为什么必须在挂载时就切
   * 之前两个入口共享同一份会话（「同一段对话，另一个顶栏」）——
   * 于是「点开一个历史对话，再点侧栏助理」会出现顶栏是助理、
   * 内容却是刚才那场对话的串台。现在 chrome 决定 store 的 scope。
   *
   * ★ 切的是聊天区，**不是侧栏**
   * 助理有且只有一条固定会话（`assistant_main`）：不能新建、不分支、
   * 不进侧栏列表。侧栏那份普通对话记录在哪个页面都原样待着，
   * 进出助理页只是换「聊天区此刻显示哪根 thread」，谁的历史也不动。
   */
  const setScope = useAgentStore((s) => s.setScope);
  const voiceBusy = useAgentStore((s) => s.voiceBusy);
  useEffect(() => {
    void setScope(requestedChrome === 'assistant' ? 'assistant' : 'main');
  }, [requestedChrome, setScope, isStreaming]);

  /**
   * 渠道状态：null = 还没拿到或拿不到（后端没这个接口时就是拿不到）。
   * 拿不到就整块不显示 —— 把「没问到」渲染成「没绑定」，是界面上最省事也最坏的一种撒谎。
   */
  const [channels, setChannels] = useState<ChannelState[] | null>(null);
  useEffect(() => {
    if (chrome !== 'assistant') return;
    let alive = true;
    fetchChannels()
      .then((res) => {
        if (alive) setChannels(res.channels);
      })
      .catch(() => {
        /* 见上：静默，不显示 */
      });
    return () => {
      alive = false;
    };
  }, [chrome]);

  // 插件市场「通过 Syncore 创建」跳过来时，输入框里已经写好那段向导（取走即清，刷新不再出现）
  useEffect(() => {
    const draft = takeComposerDraft();
    if (draft) inputRef.current?.setText(draft);
  }, []);

  // Ctrl / Cmd + K 打开命令面板
  // （新建会话已让位到 Ctrl+N —— 见 Sidebar 的注释：Ctrl+K 是命令面板的事实标准键位）
  // 标题栏「编辑 → 搜索」也走这里：发 xjms:palette-open 事件（面板只挂在这一页）。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (document.querySelector('[aria-modal="true"]')) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPaletteOpen(true);
      }
    };
    const onPaletteOpen = () => setPaletteOpen(true);
    window.addEventListener('keydown', onKey);
    window.addEventListener('xjms:palette-open', onPaletteOpen);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('xjms:palette-open', onPaletteOpen);
    };
  }, []);

  // 换会话就收起画布：面板里是「上一个会话产出的文件」，
  // 留在屏幕上会和当前这段对话对不上（关掉比显示一份可能已过期的内容更诚实）
  useEffect(() => {
    closeArtifact();
  }, [threadId]);

  // 换会话还要问一次「这段对话在哪个目录里干活」。
  // 每次现问后端、本地不留副本 —— 绑定只有一个真相（`app/workdir`），
  // 前端猜一份的话，就会出现「胶囊显示着 A 项目、后端其实跑在沙箱里」这种
  // 两边都觉得自己没错的分裂，而那正是文件写错地方的前兆。
  const syncWorkdir = useWorkdirStore((s) => s.syncBinding);
  useEffect(() => {
    void syncWorkdir(threadId);
  }, [threadId, syncWorkdir]);

  // 切会话：回到贴底状态、重置窗口（新会话从头看，不该继承上一场的阅读位置）
  useEffect(() => {
    stickyRef.current = true;
    setUnreadCount(0);
    setWindowSize(MESSAGE_WINDOW);
    setQuotingId(null);
  }, [threadId]);

  // ---------- 滚动 ----------
  // 判断「是否贴底」的唯一入口。滚动事件与 token 到达共用一个判定函数，
  // 保证「按钮显隐」「是否跟随」两个状态永远来自同一个事实。
  const updateScrollState = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < STICKY_THRESHOLD_PX;
    stickyRef.current = nearBottom;
    setShowScrollButton(!nearBottom);
    if (nearBottom) setUnreadCount(0);
  }, []);

  // 用户滚动：只更新贴底判定，不主动滚
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.addEventListener('scroll', updateScrollState, { passive: true });
    return () => el.removeEventListener('scroll', updateScrollState);
  }, [updateScrollState]);

  // 新消息 / token 到达：贴底才跟随；离底时累计未读数
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (stickyRef.current) {
      el.scrollTop = el.scrollHeight;
      setUnreadCount(0);
    } else if (!isStreaming && !voiceBusy) {
      // 一轮完整结束（不是 token 中途）才计一条 —— 按 token 计数会闪成秒表
      setUnreadCount((c) => c + 1);
    }
    setShowScrollButton(!stickyRef.current);
  }, [messages, isStreaming, voiceBusy]);

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    stickyRef.current = true;
    setUnreadCount(0);
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  }, []);

  // 窗口化渲染：只渲染最近 windowSize 条
  const visibleMessages = useMemo(
    () => (messages.length > windowSize ? messages.slice(-windowSize) : messages),
    [messages, windowSize],
  );
  const hiddenCount = Math.max(0, messages.length - visibleMessages.length);

  const handleEdit = useCallback((message: ChatMessageType) => {
    setQuotingId(null);
    setEditingId(message.id);
    inputRef.current?.setText(message.content);
  }, []);

  const handleQuote = useCallback(
    (message: ChatMessageType) => {
      setEditingId(null);
      setQuotingId(message.id);
      const preview = message.content.length > 120 ? `${message.content.slice(0, 120)}…` : message.content;
      inputRef.current?.setText(`${t('input.quotePrefix')}${preview}\n`);
    },
    [t],
  );

  // 命令面板里选中快捷指令 = 把问法填进输入框（与 `/` 菜单同一条落点）
  const handleUseQuickCommand = useCallback((command: QuickCommand) => {
    setEditingId(null);
    setQuotingId(null);
    inputRef.current?.setText(command.body);
  }, []);

  const handleSend = useCallback(
    (text: string, attachments?: AttachmentMeta[]) => {
      if (editingId) {
        editMessage(editingId, text);
        setEditingId(null);
      } else {
        setQuotingId(null);
        send(text, attachments);
      }
    },
    [editingId, editMessage, send],
  );

  const handleDeleteTurn = useCallback(
    async (message: ChatMessageType) => {
      if (!await confirmAction({ title: t('feedback.deleteTurnTitle'), description: t('feedback.deleteTurnDescription') })) return;
      await deleteTurn(message);
    },
    [deleteTurn, t],
  );

  const handleFork = useCallback(
    (message: ChatMessageType) => {
      void forkFromMessage(message);
    },
    [forkFromMessage, t],
  );

  /**
   * 导出当前对话为 Markdown。
   *
   * 文件名取首轮用户输入（「你到底聊了什么」最贴近的一句），
   * 用 `safeFileName` 清掉路径分隔符等非法字符 —— 用户的一句话里什么都有可能出现。
   */
  const handleExport = useCallback(() => {
    const title = safeFileName(
      messages.find((m) => m.role === 'user')?.content ?? '',
      '对话记录',
    );
    downloadTextFile(`${title}.md`, conversationToMarkdown(messages, title));
    toast(t('chat.exported'), 'success');
  }, [messages, t]);

  /** 「跳过」= 什么都不答，让模型按自己的判断继续（与只答一部分不同） */
  const handleSkipQuestion = useCallback(
    () => answerQuestion({}, { skipped: true }),
    [answerQuestion],
  );

  const hasMessages = messages.length > 0;
  const lastAssistantId =
    messages.length > 0 && messages[messages.length - 1].role === 'assistant'
      ? messages[messages.length - 1].id
      : '';

  const liveMessage = messages.find(message => message.id === lastAssistantId);
  const waitingForApproval = liveMessage?.toolCalls?.some(tool => tool.status === 'awaiting_approval');
  // ★ 提问与审批并列：都是「停在你身上」，但说的是不同的话
  // 顶栏写「等待你回答」而卡片就在下面 —— 用户不需要再猜该去哪儿点。
  const waitingForAnswer = Boolean(pendingQuestion);
  const currentTool = liveMessage?.toolCalls?.filter(tool => tool.status === 'pending').at(-1);
  const activityLabel = waitingForApproval
    ? '等待你确认'
    : waitingForAnswer
      ? '等待你回答'
      : currentTool
        ? toolActivity(currentTool, 'pending')
        : intentProgress(intent ?? 'general');

  /**
   * 输入区 —— 同一份节点用在两处：
   * - 欢迎态：嵌在主视觉里（.main__composer--inline）
   * - 有消息：钉在底部（.main__composer）
   * 两处互斥渲染，所以 ref 永远指向当前挂载的那一个。
   */
  const composerNode = (
    <>
      {(editingId || quotingId) && (
        <div className="composer__editing">
          <span>{editingId ? t('input.editing') : t('input.quoting')}</span>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => {
              setEditingId(null);
              setQuotingId(null);
              inputRef.current?.setText('');
            }}
          >
            {t('common.cancel')}
          </button>
        </div>
      )}
      <ChatInput
        ref={inputRef}
        onSend={handleSend}
        onStop={abort}
        isStreaming={isStreaming}
        disabled={chrome === 'assistant' && voiceBusy}
        placeholder={t('chat.composerPrompt')}
        threadId={threadId}
        webEnabled={webEnabled}
        onToggleWeb={setWebEnabled}
        thinkingEnabled={thinkingEnabled}
        onToggleThinking={setThinkingEnabled}
        turnMode={turnMode}
        turnModeOptions={turnModeOptions}
        defaultModeLabel={defaultModeLabel}
        onTurnModeChange={setTurnMode}
        models={availableModels}
        onModelChange={(model) => void switchModel(model)}
        onOpenSettings={onOpenSettings}
      />
      {chrome === 'assistant' && voiceBusy && (
        <div className="composer__editing" role="status">
          <span>正在语音对话，文字会同步显示在这里</span>
          <button type="button" className="btn btn--ghost btn--sm" onClick={() => window.xjmsDesktop?.pet?.cancelVoice()}>停止语音</button>
        </div>
      )}
    </>
  );

  return (
    <>
      <div className="main chat-salon">
        {/* ---------- 顶栏：实时状态 ---------- */}
        <header className="main__topbar">
          <div className="salon-heading"><div className="main__title">
            {/*
              这一轮在忙什么 —— 说动词，不说分类名。
              「rag · 0.87」对用户是乱码，「正在查资料…」才是他要的答案：
              还要等多久 / 它在干嘛。
            */}
            {isStreaming ? (
              <span className="main__busy">
                {!waitingForApproval && !waitingForAnswer && <span className="spinner" />}
                <span className="main__activity">{activityLabel}</span>
              </span>
            ) : activeTaskCount > 0 ? (
              /* ★ 后台还有任务在跑时，顶栏要说得出来 ——
                 否则用户看到「对话」两个字，会以为系统闲着，
                 而实际上有任务在跑（这正是 Supervisor 架构的核心场景）。
                 停止钮是「终止任务」的入口：流没在跑时输入框里没有停止按钮，
                 不在这里给入口，用户就只能去子任务面板逐个取消。 */
              <span className="main__busy">
                <span className="spinner" />
                {subtaskSummary(activeTaskCount, 0)}
                <button
                  type="button"
                  className="btn btn--sm btn--ghost main__busy-stop"
                  onClick={() => void cancelActiveTasks()}
                  title={t('chat.stopTasks')}
                  aria-label={t('chat.stopTasks')}
                >
                  <Square size={11} fill="currentColor" />
                </button>
              </span>
            ) : chrome === 'assistant' ? (
              <AssistantIdentity
                persona={persona}
                channels={channels}
                restoring={historyLoading && !hasMessages}
                onManage={() => onOpenSettings('assistant')}
              />
            ) : (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <MessageSquare size={14} />
                {historyLoading && !hasMessages ? t('chat.restoring') : t('chat.title')}
              </span>
            )}
            {/* 专员链路暴露的是内部编排：只在出错时显示，给排查留一条线索 */}
            {agentChain.length > 0 && Boolean(error) && (
              <AgentStatus chain={agentChain} running={isStreaming} compact />
            )}
          </div></div>

          <div className="salon-toolbar">
            {chrome === 'assistant' && <ComputerControlStatus />}
            {/* 命令面板的可发现入口：快捷键只有记得住的人才会用 */}
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={() => setPaletteOpen(true)}
              title={t('chat.searchPlaceholder')}
              aria-label={t('chat.searchPlaceholder')}
            >
              <Search size={14} />
            </button>
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={cycleFont} title={t('chat.fontSizeTip', { label: fontLabel })}
              aria-label={t('chat.fontSizeTip', { label: fontLabel })}
            >
              <Type size={14} />
            </button>
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={() => toggleLang()}
              title={t('chat.langTip')}
              aria-label={t('chat.langTip')}
            >
              <Languages size={14} />
            </button>
            <button
              type="button"
              className={`btn btn--sm btn--ghost${panelOpen ? ' btn--active' : ''}`}
              onClick={() => togglePanel()}
              title={t('panel.toggleTip')}
              aria-label={t('panel.toggleTip')}
              aria-pressed={panelOpen}
            >
              <PanelRight size={14} />
            </button>
            {/*
              模型选择器已经搬进输入区（2026-10 改版）：它是「这一句用谁回答」，
              和「在哪句话里说」是同一层决定，放在顶栏反而像全局配置。
            */}
            {hasMessages && (
              <>
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  onClick={handleExport}
                  title={t('chat.exportTip')}
                >
                  <Download size={13} />
                  {t('common.export')}
                </button>
              </>
            )}
          </div>
        </header>

        {/* 壁纸压在「顶栏以下」这块区域的最底下：顶栏高度由内容撑出来，
            图层跟着这块走，图片上沿就是内容区上沿，不会被顶栏切掉一截。
            样式与浓度见 styles/layout.css */}
        <div className="main__wallpaper-area">
        {wallpaper !== 'none' && <div className="main__wallpaper" aria-hidden="true" />}
        {/* ---------- 消息区（欢迎态是一张品牌主视觉：柔光背景 + 内嵌输入框） ---------- */}
        <div
          className={`main__scroll${hasMessages ? '' : ' main__scroll--welcome'}`}
          ref={scrollRef}
        >
          <div className="main__inner">
            {restoreNote && (
              <div className="alert alert--info">
                <History size={14} style={{ flexShrink: 0, marginTop: 2 }} />
                <span style={{ flex: 1 }}>{restoreNote}</span>
                <button
                  type="button"
                  className="alert__close"
                  onClick={dismissRestoreNote}
                  aria-label={t('common.close')}
                >
                  <X size={13} />
                </button>
              </div>
            )}

            {error && (
              <div className="alert alert--error">
                <TriangleAlert size={14} style={{ flexShrink: 0, marginTop: 2 }} />
                <span style={{ flex: 1 }}>{error}</span>
                <button type="button" className="alert__close" onClick={clearError} aria-label={t('common.close')}>
                  <X size={13} />
                </button>
              </div>
            )}

            {/* 会话恢复中：骨架屏（恢复要拉的轮次可能很多，spinner 说不了「正在重建一屏内容」） */}
            {historyLoading && !hasMessages && <ConversationSkeleton />}

            {hasMessages ? (
              <>
                {hiddenCount > 0 && (
                  <button
                    type="button"
                    className="load-earlier"
                    onClick={() => setWindowSize((n) => n + MESSAGE_WINDOW)}
                  >
                    加载更早消息（还有 {hiddenCount} 条）
                  </button>
                )}
                {visibleMessages.map((message) => (
                  <ChatMessage
                    key={message.id}
                    message={message}
                    isLatestAssistant={message.id === lastAssistantId}
                    isStreaming={isStreaming || (chrome === 'assistant' && voiceBusy)}
                    assistantChrome={chrome === 'assistant'}
                    onEdit={message.role === 'user' ? handleEdit : undefined}
                    // 她主动开口的那一轮没有提问，重新生成没有意义
                    onRegenerate={
                      message.role === 'assistant' && message.initiatedBy !== 'assistant'
                        ? regenerate
                        : undefined
                    }
                    onDeleteTurn={handleDeleteTurn}
                    // 分支会造新会话 —— 助理只有那一条，她的页面不给这个入口
                    onFork={chrome === 'assistant' ? undefined : handleFork}
                    onQuote={handleQuote}
                    // 只有**当前**那一张还活着；历史重放出来的卡是只读的
                    onAnswerQuestion={
                      pendingQuestion &&
                      message.askedQuestions?.some(
                        (q) => q.questionId === pendingQuestion.questionId,
                      )
                        ? answerQuestion
                        : undefined
                    }
                    onSkipQuestion={
                      pendingQuestion &&
                      message.askedQuestions?.some(
                        (q) => q.questionId === pendingQuestion.questionId,
                      )
                        ? handleSkipQuestion
                        : undefined
                    }
                  />
                ))}
                {/* ★ 后台子任务面板：它不属于任何一条消息 ——
                    子任务活得比产生它的那一轮更久（关页面也在跑），
                    所以挂在消息流末尾而不是某条消息内部。
                    没有任务时它自己返回 null，不占任何位置。 */}
                <SubtaskPanel />
              </>
            ) : historyLoading ? null : (
              /* 欢迎态 = 一张品牌主视觉：mark → 品牌标识 → 主张 → 绛红细线 → 内嵌输入框
                 只有助理页的主视觉跟着人设走（那才是「她的脸」）；
                 普通对话页是产品门面，永远用品牌字标，不借人设的脸 */
              <div className="welcome">
                {chrome === 'assistant' && persona?.avatar ? (
                  <img className="welcome__mark welcome__mark--persona" src={persona.avatar} alt="" aria-hidden="true" />
                ) : (
                  <img className="welcome__mark welcome__mark--brand" src={brandMark} alt="Syncore" />
                )}
                <h1 className="welcome__title">{chrome === 'assistant' ? personaName : t('chat.welcomeTitle')}</h1>
                {/* 开场白是「她」的第一句话：只属于助理页，普通对话的欢迎态
                    不替她打招呼（两边现在是两场各不相干的对话） */}
                {chrome === 'assistant' && persona?.greeting && <p className="welcome__brandline">{persona.greeting}</p>}

                {/* ★ 输入框就在主视觉里 —— 欢迎态下底部不再挂输入区 */}
                <div className="main__composer main__composer--inline">{composerNode}</div>
              </div>
            )}
          </div>
        </div>

        {/* ---------- 输入区（只在有消息时置底；欢迎态下输入框在主视觉里） ---------- */}
        {hasMessages && (
          <div className="main__composer">
            <div className="main__composer-inner">
              {/* 回到底部 / 未读提示按钮：离底时才出现。
                  放在输入区里面是为了让它贴着卡片上沿 —— 卡片会随打字长高，
                  脱离它的定位只能靠一个写死的 bottom。 */}
              {showScrollButton && (
                <button type="button" className="scroll-to-bottom" onClick={scrollToBottom} title={t('scroll.bottom')}>
                  <ArrowDown size={16} />
                  {unreadCount > 0 && <span className="scroll-to-bottom__badge">{unreadCount}</span>}
                </button>
              )}
              {composerNode}
            </div>
          </div>
        )}
        </div>
      </div>

      {/* ---------- 产出物画布（右侧） ---------- */}
      <ArtifactPanel onOpenSettings={onOpenSettings} />

      {/* ---------- 命令面板 ---------- */}
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        // 助理页不给「新建会话」：面板里出现一条点了没反应的命令比不出现更糟
        onNewSession={chrome === 'assistant' ? undefined : newSession}
        onOpenSettings={onOpenSettings}
        onExport={hasMessages ? handleExport : undefined}
        onUseQuickCommand={handleUseQuickCommand}
      />

      {/* ---------- HITL 审批弹窗 ---------- */}
      <ApprovalDialog
        key={pendingApproval?.requestId || pendingApproval?.callId || 'approval'}
        open={Boolean(pendingApproval)}
        toolName={pendingApproval?.toolName ?? ''}
        toolLabel={pendingApproval?.toolLabel}
        args={pendingApproval?.args ?? {}}
        reason={pendingApproval?.reason ?? ''}
        requestor={pendingApproval?.requestor ?? ''}
        onApprove={(scope) => void approve(scope)}
        grantAllowed={pendingApproval?.grantAllowed}
        grantMaxUses={pendingApproval?.grantMaxUses}
        grantTtlSeconds={pendingApproval?.grantTtlSeconds}
        expiresAt={pendingApproval?.expiresAt}
        onReject={() => void reject()}
      />
    </>
  );
}

/**
 * 助理顶栏的身份块 —— 只在闲时出现（忙起来顶栏让位给「正在查资料…」，
 * 那是比「这是助理」更有用的信息，与对话页同一套取舍）。
 *
 * 身份跟着人设卡走：设了头像/名字就显示她的脸和她的名字，没设才退回默认。
 *
 * 渠道那一截是**按钮**：它同时是「现在绑了没有」的读数和「去哪儿绑」的入口。
 * 做成两块（读数 + 旁边一个小齿轮）反而更差 —— 齿轮没人知道它是干嘛的。
 */
function AssistantIdentity({
  persona,
  channels,
  restoring,
  onManage,
}: {
  persona: PersonaProfile | null;
  channels: ChannelState[] | null;
  restoring: boolean;
  onManage: () => void;
}) {
  const t = useT();
  const bound = (channels ?? []).filter((c) => c.bound);
  const running = bound.some((c) => c.running);
  return (
    <span className="assistant-id">
      <span className="assistant-id__name">
        {persona?.avatar ? (
          <img className="assistant-id__avatar" src={persona.avatar} alt="" aria-hidden="true" />
        ) : (
          <Bot size={14} />
        )}
        {restoring ? t('chat.restoring') : persona?.name || t('assistant.title')}
      </span>
      {channels && (
        <button
          type="button"
          className="assistant-id__channels"
          onClick={onManage}
          title={
            running
              ? t('assistant.connectedTip')
              : bound.length > 0
                ? t('assistant.boundTip')
                : t('assistant.noneTip')
          }
        >
          {bound.length > 0 ? (
            <>
              {running ? t('assistant.connectedPrefix') : t('assistant.boundPrefix')}
              {bound.map((c) => (
                <span key={c.key} className="assistant-id__chip">
                  <span
                    className={`assistant-id__dot ${
                      c.running ? 'assistant-id__dot--running' : 'assistant-id__dot--on'
                    }`}
                  />
                  {c.label}
                </span>
              ))}
            </>
          ) : (
            <>
              <span className="assistant-id__dot" />
              {t('assistant.none')}
            </>
          )}
        </button>
      )}
    </span>
  );
}
