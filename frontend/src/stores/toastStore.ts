import { create } from 'zustand';

export type ToastType = 'info' | 'success' | 'warning' | 'error';

export interface ToastItem {
  id: string;
  message: string;
  type: ToastType;
  duration: number;
}

interface ToastStore {
  toasts: ToastItem[];
  addToast: (message: string, type?: ToastType, duration?: number) => void;
  removeToast: (id: string) => void;
}

let idCounter = 0;
const DURATION: Record<ToastType, number> = { success: 2400, info: 3600, warning: 6000, error: 8000 };

export const useToastStore = create<ToastStore>((set, get) => ({
  toasts: [],
  addToast: (message, type = 'info', duration = DURATION[type]) => {
    if (!message.trim()) return;
    // Repeated feedback restarts its lifetime instead of filling the screen.
    const existing = get().toasts.find((item) => item.message === message && item.type === type);
    const id = `${Date.now()}-${++idCounter}`;
    const remaining = get().toasts.filter((item) => item.id !== existing?.id);
    set({ toasts: [...remaining, { id, message, type, duration }].slice(-3) });
  },
  removeToast: (id) => {
    set({ toasts: get().toasts.filter((t) => t.id !== id) });
  },
}));

export function toast(message: string, type: ToastType = 'info'): void {
  useToastStore.getState().addToast(message, type);
}
