import { create } from 'zustand';
import { fetchComputerControl, setComputerControl, stopComputerControl, updateComputerControlPreferences, reconnectComputerControl } from '@/lib/api';
import type { ComputerControlStatus, ComputerControlPreferences } from '@/types';

interface ControlState {
  data: ComputerControlStatus | null;
  error: string;
  busy: boolean;
  refresh: () => Promise<void>;
  toggle: (enabled: boolean) => Promise<void>;
  stop: () => Promise<void>;
  reconnect: () => Promise<void>;
  preferences: (patch: Partial<ComputerControlPreferences>) => Promise<void>;
}

let revision = 0;
let pending: Promise<void> | null = null;
export const useComputerControlStore = create<ControlState>((set) => {
  async function change(action: () => Promise<ComputerControlStatus>) {
    const current = ++revision;
    set({ busy: true, error: '' });
    try {
      const data = await action();
      if (current === revision) set({ data });
    } catch (e) {
      if (current === revision) set({ error: e instanceof Error ? e.message : String(e) });
      throw e;
    } finally {
      if (current === revision) set({ busy: false });
    }
  }
  return {
    data: null, error: '', busy: false,
    refresh: () => {
      if (pending) return pending;
      const current = revision;
      pending = fetchComputerControl().then(data => {
        if (current === revision) set({ data });
      }).catch(e => {
        if (current === revision) set({ error: e instanceof Error ? e.message : String(e) });
      }).finally(() => { pending = null; });
      return pending;
    },
    toggle: enabled => change(() => setComputerControl(enabled)),
    stop: () => change(stopComputerControl),
    reconnect: () => change(reconnectComputerControl),
    preferences: patch => change(() => updateComputerControlPreferences(patch)),
  };
});
