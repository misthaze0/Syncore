import type { EmployeeCapabilityOption } from '@/types';

const KIND_LABELS = { plugin: '插件', skill: '技能', mcp: '连接器' };

export function EmployeeCapabilities({ options, selected, loading, error, onToggle, onRetry, onMarket }: {
  options: EmployeeCapabilityOption[];
  selected: string[];
  loading: boolean;
  error: string | null;
  onToggle: (name: string) => void;
  onRetry: () => void;
  onMarket: () => void;
}) {
  return <details className="team-tool-settings team-capabilities" open={error ? true : undefined}>
    <summary>工具与技能{loading ? ' · 正在读取' : error ? ' · 读取失败' : selected.length ? ` · 已选 ${selected.length} 项` : ''}</summary>
    <div className="team-capabilities__header"><span>插件市场 · 已安装</span><button type="button" onClick={onMarket}>管理扩展 ↗</button></div>
    {loading ? <p className="team-capabilities__hint" role="status">正在读取已安装扩展…</p> : error ? <div className="workspace-error" role="alert">已安装扩展读取失败<button type="button" onClick={onRetry}>重试</button></div> : options.length ? <div className="team-capabilities__list">
      {options.map(option => <label key={option.name}><input type="checkbox" aria-label={option.display_name} checked={selected.includes(option.name)} onChange={() => onToggle(option.name)} /><span><strong>{option.display_name}</strong>{option.description && <small>{option.description}</small>}</span><em>{KIND_LABELS[option.kind]}</em></label>)}
    </div> : <div className="team-capabilities__empty"><span>暂无可分配的已安装扩展</span><button type="button" onClick={onMarket}>去插件市场添加</button></div>}
  </details>;
}
