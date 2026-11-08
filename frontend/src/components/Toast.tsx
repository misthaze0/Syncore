import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { TriangleAlert, Check, Info, X, type LucideIcon } from '@/components/icons';
import { useT } from '@/i18n';
import { useToastStore, type ToastItem } from '@/stores/toastStore';

const ICON: Record<ToastItem['type'], LucideIcon> = {
  success: Check,
  error: Info,
  warning: TriangleAlert,
  info: Info,
};

function ToastCard({ item }: { item: ToastItem }) {
  const remove = useToastStore((s) => s.removeToast);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const remaining = useRef(item.duration);
  const t = useT();
  const Icon = ICON[item.type];
  const paused = hovered || focused;

  useEffect(() => {
    if (paused || item.duration <= 0) return;
    const started = Date.now();
    const timer = window.setTimeout(() => remove(item.id), remaining.current);
    return () => {
      window.clearTimeout(timer);
      remaining.current = Math.max(0, remaining.current - (Date.now() - started));
    };
  }, [item.id, item.duration, paused, remove]);

  return (
    <div className={`toast toast--${item.type}`} onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}
      onFocus={() => setFocused(true)} onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false);
      }}>
      <span className="toast__icon"><Icon size={16} /></span>
      <span className="toast__message" role={item.type === 'error' ? 'alert' : 'status'} aria-atomic="true">{item.message}</span>
      <button type="button" className="toast__close" onClick={() => remove(item.id)} aria-label={t('feedback.dismiss')}>
        <X size={14} />
      </button>
    </div>
  );
}

export function ToastContainer() {
  const toasts = useToastStore((s) => s.toasts);
  const t = useT();
  return createPortal(
    <div className="toast-stack" role="region" data-modal-feedback aria-label={t('feedback.notifications')}>
      {toasts.map((item) => <ToastCard key={item.id} item={item} />)}
    </div>, document.body,
  );
}
