import { useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { FilePenLine, MessageSquare } from 'lucide-react';
import { useOfficeStore } from '@/stores/officeStore';
import { toast } from '@/stores/toastStore';

export function ModeSwitch({ mode }: { mode: 'chat' | 'office' }) {
  const navigate = useNavigate();
  const location = useLocation();
  const locked = useRef(false);
  const [pending, setPending] = useState(false);
  const change = async (next: 'chat' | 'office') => {
    if (locked.current || location.pathname === `/${next}`) return;
    locked.current = true;
    setPending(true);
    try {
      if (mode === 'office') {
        const office = useOfficeStore.getState();
        office.stopAssistant();
        if (!await office.save()) {
          toast('文档尚未保存，请处理后再切换', 'warning');
          return;
        }
      }
      // Keep the current surface visible until the editor bundle is ready.
      if (next === 'office') await import('@/pages/OfficePage');
      try { localStorage.setItem('xjms.mode', next); } catch { /* Session only. */ }
      navigate(next === 'office' ? '/office' : '/chat');
    } catch {
      toast('切换失败，请重试', 'error');
    } finally {
      locked.current = false;
      setPending(false);
    }
  };
  return (
    <nav className="mode-switch" data-mode={mode} aria-label="工作模式" aria-busy={pending}>
      <span className="mode-switch__indicator" aria-hidden="true" />
      <button type="button" aria-disabled={pending} className={mode === 'chat' ? 'is-active' : ''} aria-pressed={mode === 'chat'} onClick={() => void change('chat')}><MessageSquare size={15} /><span>对话</span></button>
      <button type="button" aria-disabled={pending} className={mode === 'office' ? 'is-active' : ''} aria-pressed={mode === 'office'} onClick={() => void change('office')}><FilePenLine size={15} /><span>办公</span></button>
    </nav>
  );
}
