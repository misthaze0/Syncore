import { Monitor, Square } from '@/components/icons';
import { useComputerControl } from '@/hooks/useComputerControl';
import { toast } from '@/stores/toastStore';
import '@/styles/computer-control.css';

export function ComputerControlStatus() {
  const { data, stop } = useComputerControl();
  if (!data?.enabled) return null;
  return <span className="cc-chat-status" role="status"><Monitor size={12} />{data.runtime.active ? '正在操作电脑' : '已允许电脑操控'}<button type="button" aria-label="停止电脑操控" title="立即停止电脑操控" onClick={() => void stop().then(() => toast('电脑操控已停止', 'success')).catch(e => toast(e instanceof Error ? e.message : String(e), 'error'))}><Square size={10} fill="currentColor" /></button></span>;
}
