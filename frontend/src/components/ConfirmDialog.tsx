import { useEffect, useId, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useLocation } from 'react-router-dom';

import { X } from '@/components/icons';
import { isTopmostModal, useModalFocus } from '@/hooks/useModalFocus';
import { useT } from '@/i18n';
import { useConfirmStore, type ConfirmOptions } from '@/stores/confirmStore';

export function ConfirmDialogHost() {
  const request = useConfirmStore((s) => s.request);
  const location = useLocation();

  // A decision belongs to the page where it was requested.
  useEffect(() => () => useConfirmStore.getState().settle(false), [location]);

  return request ? createPortal(<ConfirmDialog request={request} />, document.body) : null;
}

function ConfirmDialog({ request }: { request: ConfirmOptions }) {
  const settle = useConfirmStore((s) => s.settle);
  const t = useT();
  const id = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalFocus(dialogRef);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || !isTopmostModal(dialogRef.current)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      settle(false);
    };
    // Capture Escape before the settings / editor underneath can handle it.
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [settle]);

  return (
    <div className="feedback-mask" onMouseDown={(event) => {
      if (event.target === event.currentTarget) settle(false);
    }}>
      <div ref={dialogRef} className="feedback-dialog" role="alertdialog" aria-modal="true"
        aria-labelledby={`${id}-title`} aria-describedby={request.description ? `${id}-description` : undefined} tabIndex={-1}>
        <h2 id={`${id}-title`} className="feedback-dialog__title">{request.title}</h2>
        {request.description && <p id={`${id}-description`} className="feedback-dialog__description">{request.description}</p>}
        <div className="feedback-dialog__actions">
          <button type="button" className="feedback-dialog__button" onClick={() => settle(false)}>
            {request.cancelLabel ?? t('common.cancel')}
          </button>
          <button type="button" className={`feedback-dialog__button feedback-dialog__button--${request.tone ?? 'danger'}`}
            onClick={() => settle(true)}>
            {request.confirmLabel ?? t('common.delete')}
          </button>
        </div>
        <button type="button" className="feedback-dialog__close" aria-label={t('feedback.close')} title="Esc" onClick={() => settle(false)}>
          <X size={18} />
        </button>
      </div>
    </div>
  );
}
