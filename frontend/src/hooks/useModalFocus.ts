import { useEffect, type RefObject } from 'react';
import { acquireModalInert } from '@/lib/modalInert';

const MODAL_SELECTOR = '[role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]';
const FOCUSABLE_SELECTOR = 'a[href], button, input, select, textarea, [tabindex], [contenteditable="true"]';

function visible(element: HTMLElement): boolean {
  return element.getClientRects().length > 0 && getComputedStyle(element).visibility !== 'hidden';
}

/** 嵌套弹窗打开时，键盘事件只由最上层处理。 */
export function isTopmostModal(element: HTMLElement | null): boolean {
  const modals = [...document.querySelectorAll<HTMLElement>(MODAL_SELECTOR)]
    .filter((modal) => !modal.closest('[inert]') && visible(modal));
  return modals[modals.length - 1] === element;
}

export function useModalFocus(ref: RefObject<HTMLElement>, scopeRef?: RefObject<HTMLElement>): void {
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    // 日期等浮层可以和对话框并列放在遮罩中，仍属于同一次键盘交互。
    const scope = scopeRef?.current ?? dialog;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const releaseInert: Array<() => void> = [];

    // 逐层屏蔽弹窗分支外的页面，同时兼容弹窗位于布局内或根节点下两种路由。
    let branch: HTMLElement = scope;
    while (branch.parentElement) {
      for (const sibling of branch.parentElement.children) {
        // Notifications remain readable and dismissible above a modal.
        if (sibling instanceof HTMLElement && sibling !== branch && !sibling.matches('[data-modal-feedback]')) {
          releaseInert.push(acquireModalInert(sibling));
        }
      }
      branch = branch.parentElement;
      if (branch === document.body) break;
    }

    const feedback = () => [...document.querySelectorAll<HTMLElement>('[data-modal-feedback]')];
    const focusable = () => [...new Set([
      ...scope.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ...feedback().flatMap((region) => [...region.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)]),
    ])]
      .filter((element) => element.tabIndex >= 0 && !element.matches(':disabled') && !element.closest('[inert]') && visible(element));
    const focusInside = () => {
      const elements = focusable();
      const input = elements.find((element) => element.matches('input:not([type="hidden"]), textarea, select'));
      (input ?? elements[0] ?? dialog).focus({ preventScroll: true });
    };
    const onFocus = (event: FocusEvent) => {
      if (isTopmostModal(dialog) && !scope.contains(event.target as Node) && !feedback().some((region) => region.contains(event.target as Node))) focusInside();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Tab' || !isTopmostModal(dialog)) return;
      const elements = focusable();
      event.preventDefault();
      if (!elements.length) {
        dialog.focus();
      } else {
        // Portaled feedback can precede the dialog in DOM order.
        const current = elements.indexOf(document.activeElement as HTMLElement);
        const next = event.shiftKey
          ? (current <= 0 ? elements.length - 1 : current - 1)
          : (current + 1) % elements.length;
        elements[next].focus();
      }
    };

    document.addEventListener('focusin', onFocus);
    document.addEventListener('keydown', onKey);
    focusInside();
    return () => {
      document.removeEventListener('focusin', onFocus);
      document.removeEventListener('keydown', onKey);
      for (const release of releaseInert) release();
      if (previousFocus?.isConnected && !previousFocus.closest('[inert]')) previousFocus.focus({ preventScroll: true });
    };
  }, [ref, scopeRef]);
}
