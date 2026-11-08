import { create } from 'zustand';

export interface ConfirmOptions {
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: 'danger' | 'warning' | 'neutral';
}

interface ConfirmRequest extends ConfirmOptions {
  resolve: (confirmed: boolean) => void;
}

interface ConfirmStore {
  request: ConfirmRequest | null;
  settle: (confirmed: boolean) => void;
}

export const useConfirmStore = create<ConfirmStore>((set, get) => ({
  request: null,
  settle: (confirmed) => {
    const request = get().request;
    if (!request) return;
    set({ request: null });
    request.resolve(confirmed);
  },
}));

/** One decision at a time: repeated clicks must not queue stale destructive actions. */
export function confirmAction(options: ConfirmOptions): Promise<boolean> {
  if (useConfirmStore.getState().request) return Promise.resolve(false);
  return new Promise((resolve) => {
    useConfirmStore.setState({ request: { ...options, resolve } });
  });
}
