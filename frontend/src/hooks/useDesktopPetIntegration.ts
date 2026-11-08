import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAgentStore } from '@/stores/agentStore';
import type { PetActivity } from '@/types/desktop';

type AgentState = ReturnType<typeof useAgentStore.getState>;

/** 终态口径与 agentStore 的 isSubtaskTerminal 一致 */
const TASK_TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'orphaned']);
/**
 * 已播报过完成的任务 —— 每个任务只播一次。
 * 轮询快照、双流重复事件、切页重同步都可能把同一次完成再推一遍；
 * 这个集合跨组件重挂载存活，重复到达时直接跳过。
 */
const announcedCompletions = new Set<string>();

/** 本次状态变化里「刚到达终态」的任务（之前存在且非终态 → 现在终态）。 */
function detectTaskCompletions(state: AgentState, previous?: AgentState): string[] {
  if (!previous) return [];
  const completed: string[] = [];
  for (const task of Object.values(state.tasks)) {
    if (!TASK_TERMINAL.has(task.status)) continue;
    const before = previous.tasks[task.taskId];
    if (!before || TASK_TERMINAL.has(before.status)) continue;
    if (announcedCompletions.has(task.taskId)) continue;
    completed.push(task.taskId);
    announcedCompletions.add(task.taskId);
  }
  return completed;
}

/** Share runtime state and project the pet's voice stream into the assistant UI. */
export function useDesktopPetIntegration() {
  const navigate = useNavigate();
  useEffect(() => {
    const bridge = window.xjmsDesktop?.pet;
    if (!bridge) return;
    const offNavigate = bridge.onNavigate((route) => navigate(route));
    bridge.notifyMainReady();

    /**
     * 桌面助手语音回合使用的输入框口径。
     *
     * 权限档位、联网、深度思考都取助理输入框此刻的值 —— 语音不该成为一条
     * 「权限更宽」的旁路。assistantBusy 让桌面助手知道主窗正在跑助理回合，
     * 命中唤醒也不并发开新回合。
     */
    let runtimeKey = '';
    const publishRuntime = (state: ReturnType<typeof useAgentStore.getState>) => {
      const runtime = {
        permissionMode: state.turnMode,
        assistantBusy: state.scope === 'assistant' && (state.isStreaming ||
          (!state.voiceBusy && (Boolean(state.pendingApproval) || Boolean(state.pendingQuestion)))),
        webEnabled: state.webEnabled,
        thinkingEnabled: state.thinkingEnabled,
      };
      const key = `${runtime.permissionMode}|${runtime.assistantBusy}|${runtime.webEnabled}|${runtime.thinkingEnabled}`;
      if (key === runtimeKey) return;
      runtimeKey = key;
      bridge.publishRuntime(runtime);
    };

    const publish = (state: ReturnType<typeof useAgentStore.getState>, previous?: ReturnType<typeof useAgentStore.getState>) => {
      publishRuntime(state);
      const completions = detectTaskCompletions(state, previous);
      if (previous && completions.length === 0 && state.isStreaming === previous.isStreaming && state.pendingApproval === previous.pendingApproval && state.error === previous.error && state.activeTaskCount === previous.activeTaskCount) return;
      const route = state.scope === 'assistant' ? '/assistant' : '/chat';
      let activity: PetActivity = { mood: 'idle', text: '', replyId: '', route };
      if (state.pendingApproval) activity = { ...activity, mood: 'approval', text: '有一步操作需要你确认，点这里回主窗查看。' };
      else if (state.error) activity = { ...activity, mood: 'error', text: '这次处理遇到了一点问题，回主窗看看吧。' };
      else if (completions.length > 0) activity = { ...activity, mood: 'done', text: '后台任务已结束，可以到任务页查看结果。', replyId: completions.map((id) => `task-done:${id}`).join('+'), route: '/tasks' };
      else if (state.isStreaming || state.activeTaskCount > 0) activity = { ...activity, mood: 'thinking', text: '我在处理，完成后告诉你。' };
      else if (previous?.isStreaming && previous.threadId === state.threadId) {
        const reply = [...state.messages].reverse().find((message) => message.role === 'assistant' && message.content.trim());
        if (reply?.interrupted || reply?.error) activity = { ...activity, mood: 'error', text: '这一轮回答未完成，回主窗查看后可以重新生成。' };
        else if (reply) activity = { mood: 'done', text: reply.content.replace(/```[\s\S]*?```/g, '［代码请在主窗查看］').replace(/[#*_`>]/g, '').slice(0, 180), replyId: reply.id, route };
      }
      bridge.publishActivity(activity);
    };
    publish(useAgentStore.getState());
    const offStore = useAgentStore.subscribe(publish);

    // 桌面助手发布「语音回合占用中」→ 锁住助理输入框
    const applyVoiceBusy = (busy: boolean) => {
      if (useAgentStore.getState().voiceBusy !== busy) useAgentStore.getState().setVoiceBusy(busy);
    };
    const offRuntime = bridge.onRuntime((runtime) => applyVoiceBusy(runtime.voiceBusy));
    const offVoice = bridge.onVoiceUpdate((update) => useAgentStore.getState().receivePetVoice(update));
    void bridge.getVoiceTurn().then((turn) => {
      if (turn) useAgentStore.getState().restorePetVoice(turn);
    }).catch(() => {});
    void bridge.getRuntime().then((runtime) => {
      if (runtime) applyVoiceBusy(runtime.voiceBusy);
    }).catch(() => {});
    return () => { offStore(); offNavigate(); offRuntime(); offVoice(); };
  }, [navigate]);
}
