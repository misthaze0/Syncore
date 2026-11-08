/**
 * 「本轮权限」胶囊 + 浮层
 *
 * 档位是单调的：默认档由后端给出，这里只能选更紧的，
 * 所以浮层里只有「默认（当前档）」与真正更紧的那几项。
 */

import { useEffect, useRef, useState } from 'react';

import { Check, ChevronDown, ShieldCheck } from '@/components/icons';
import { useT } from '@/i18n';
import type { PermissionModeInfo, PermissionModeValue } from '@/types';

interface TurnModePickerProps {
  value: PermissionModeValue | '';
  options: PermissionModeInfo[];
  defaultLabel: string;
  onChange: (mode: PermissionModeValue | '') => void;
}

/** 与 `.turnmode__panel` 的宽度保持一致（对齐判断要用） */
const PANEL_WIDTH = 300;

export function TurnModePicker({ value, options, defaultLabel, onChange }: TurnModePickerProps) {
  const t = useT();
  const [open, setOpen] = useState(false);
  // 浮层固定宽度；靠右的胶囊在窄窗口里左对齐会顶出视口，这时改成右对齐
  const [alignRight, setAlignRight] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const toggle = () => {
    if (!open) {
      const rect = rootRef.current?.getBoundingClientRect();
      setAlignRight(!!rect && rect.left + PANEL_WIDTH > window.innerWidth - 12);
    }
    setOpen((v) => !v);
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  // tighten_options 是从紧到松的前缀，looseness 最大的那一项就是默认档
  const defaultOption = options.reduce<PermissionModeInfo | null>(
    (acc, m) => (!acc || m.looseness > acc.looseness ? m : acc),
    null,
  );
  const tighter = defaultOption ? options.filter((m) => m.looseness < defaultOption.looseness) : [];
  if (!defaultOption || tighter.length === 0) return null;

  const current = options.find((m) => m.value === value) ?? null;
  const label = current?.label || defaultLabel || defaultOption.label;

  const choose = (next: PermissionModeValue | '') => {
    onChange(next);
    setOpen(false);
  };

  return (
    <div className="turnmode" ref={rootRef}>
      <button
        type="button"
        className={`composer__chip${value ? ' composer__chip--on composer__mode--on' : ''}`}
        onClick={toggle}
        aria-expanded={open}
        aria-haspopup="listbox"
        title={current?.hint || t('input.modeTip')}
      >
        <ShieldCheck size={13} />
        <span className="composer__chip-text">
          {t('input.modeNow')} · {label}
        </span>
        <ChevronDown size={12} className={`composer__chip-caret${open ? ' is-open' : ''}`} />
      </button>

      {open && (
        <div
          className={`turnmode__panel${alignRight ? ' turnmode__panel--right' : ''}`}
          role="listbox"
          aria-label={t('input.modeNow')}
        >
          <button
            type="button"
            role="option"
            aria-selected={!value}
            className={`turnmode__opt${value ? '' : ' turnmode__opt--on'}`}
            onClick={() => choose('')}
          >
            <span className="turnmode__text">
              <span className="turnmode__label">{t('input.modeFollow')}</span>
              <span className="turnmode__sub">{defaultLabel || defaultOption.label}</span>
            </span>
            {!value && <Check size={13} className="turnmode__check" />}
          </button>
          {tighter.map((m) => (
            <button
              key={m.value}
              type="button"
              role="option"
              aria-selected={value === m.value}
              className={`turnmode__opt${value === m.value ? ' turnmode__opt--on' : ''}`}
              onClick={() => choose(m.value)}
            >
              <span className="turnmode__text">
                <span className="turnmode__label">{m.label}</span>
                <span className="turnmode__sub">{m.hint}</span>
              </span>
              {value === m.value && <Check size={13} className="turnmode__check" />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default TurnModePicker;
