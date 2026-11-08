import { useEffect } from 'react';
import { useComputerControlStore } from '@/stores/computerControlStore';
import { toast } from '@/stores/toastStore';

/** 只轮询文字状态；屏幕截图必须由用户主动点击或桌面操作触发。 */
export function useComputerControl() {
  const state = useComputerControlStore();
  useEffect(() => {
    const refresh = () => { if (!document.hidden) void useComputerControlStore.getState().refresh(); };
    refresh();
    const timer = window.setInterval(refresh, 3000);
    const off = window.xjmsDesktop?.computerControl?.onStopped(result => {
      if (!result.ok) toast('停止请求未能送达，请在电脑操控设置中重试。', 'error');
      void useComputerControlStore.getState().refresh();
    });
    document.addEventListener('visibilitychange', refresh);
    window.addEventListener('focus', refresh);
    return () => {
      window.clearInterval(timer);
      off?.();
      document.removeEventListener('visibilitychange', refresh);
      window.removeEventListener('focus', refresh);
    };
  }, []);
  return state;
}
