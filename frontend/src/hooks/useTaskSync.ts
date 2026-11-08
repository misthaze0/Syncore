import { useEffect } from 'react';

import { useAgentStore } from '@/stores/agentStore';

/**
 * 应用级后台任务轮询（对话页外的兜底同步）。
 *
 * ★ 为什么不在 SubtaskPanel 里轮询
 * SubtaskPanel 只挂在对话页 —— 用户切到任务页/设置页时它卸载，`activeTaskCount`
 * 就停在旧值。桌面助手的「后台任务已结束」播报靠这个计数检测完成，计数不新鲜
 * 会让播报推迟到「用户点开对话页、重新同步」那一刻 —— 表现为「点一下播一次」。
 * 轮询提到应用级后，任务结束在任何页面都能在 2s 内被感知，播报发生在完成时。
 *
 * 对话页上的实时通道（SSE 进度流）仍由 SubtaskPanel 持有；这里只兜底，
 * 两个通道最终都折叠进同一个 `refreshTasks`（整表替换，不抢 seq）。
 */
export function useTaskSync() {
  const activeTaskCount = useAgentStore((s) => s.activeTaskCount);
  const isStreaming = useAgentStore((s) => s.isStreaming);
  const refreshTasks = useAgentStore((s) => s.refreshTasks);

  useEffect(() => {
    if (activeTaskCount === 0 || isStreaming) return;
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;

    const timer = setInterval(() => {
      void refreshTasks({ silent: true });
    }, 2000);
    return () => clearInterval(timer);
  }, [activeTaskCount, isStreaming, refreshTasks]);
}
