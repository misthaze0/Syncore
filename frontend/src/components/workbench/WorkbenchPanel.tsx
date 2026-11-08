import { useEffect, useRef, useState, type ReactNode } from 'react';
import { FileText, Globe, PanelRight, Settings, Terminal, X } from '@/components/icons';
import { useT } from '@/i18n';
import { useSidePanelStore, type SidePanelTab } from '@/stores/artifactStore';
import { useWorkbenchStore } from '@/stores/workbenchStore';
import { BrowserPane } from './BrowserPane';
import { TerminalPane } from './TerminalPane';

const TABS = [
  { key: 'artifacts' as const, icon: FileText, label: 'panel.tabArtifacts' },
  { key: 'browser' as const, icon: Globe, label: 'panel.tabBrowser' },
  { key: 'terminal' as const, icon: Terminal, label: 'panel.tabTerminal' },
];

export function WorkbenchPanel({ artifacts, onOpenSettings }: { artifacts: ReactNode; onOpenSettings?: (tab: string) => void }) {
  const t = useT();
  const { panelOpen, activeTab, setActiveTab, closePanel } = useSidePanelStore();
  const width = useWorkbenchStore(s => s.preferences.panelWidth);
  const update = useWorkbenchStore(s => s.update);
  const [expanded, setExpanded] = useState(false);
  const [visited, setVisited] = useState<Set<SidePanelTab>>(() => new Set());
  const panel = useRef<HTMLElement>(null);
  const drag = useRef<{ x: number; width: number } | null>(null);
  useEffect(() => {
    if (panelOpen) setVisited(previous => previous.has(activeTab) ? previous : new Set([...previous, activeTab]));
  }, [panelOpen, activeTab]);
  useEffect(() => {
    if (!panelOpen) setExpanded(false);
  }, [panelOpen]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && expanded && !document.querySelector('[aria-modal="true"]')) {
        event.preventDefault(); setExpanded(false);
      }
    };
    window.addEventListener('keydown', onKey, true);
    const offEscape = window.xjmsDesktop?.browser?.onEscape(() => {
      if (!document.querySelector('[aria-modal="true"]')) setExpanded(false);
    });
    return () => { window.removeEventListener('keydown', onKey, true); offEscape?.(); };
  }, [expanded]);

  return <aside ref={panel} hidden={!panelOpen} className={`artifact-panel side-panel wb-panel${expanded ? ' wb-panel--expanded' : ''}`} aria-label="工作台" style={{ '--workbench-width': `${width}px` } as React.CSSProperties}>
    <header className="wb-panel__header">
      <span className="wb-panel__title"><span className="wb-panel__mark" />工作台</span>
      <div className="wb-panel__actions">
        {onOpenSettings && <button type="button" className="icon-btn" title="工作台设置" aria-label="工作台设置" onClick={() => onOpenSettings(activeTab === 'terminal' ? 'terminal' : 'browser')}><Settings size={14} /></button>}
        <button type="button" className="icon-btn" title={expanded ? '恢复并排' : '展开工作台'} aria-label={expanded ? '恢复并排' : '展开工作台'} aria-pressed={expanded} onClick={() => setExpanded(value => !value)}><PanelRight size={14} /></button>
        <button type="button" className="icon-btn" title="收起工作台" aria-label="收起工作台" onClick={closePanel}><X size={15} /></button>
      </div>
    </header>
    <div className="side-panel__tabs" role="tablist" aria-label="工作台工具">
      {TABS.map((tab, index) => <button type="button" key={tab.key} role="tab" id={`workbench-tab-${tab.key}`} aria-controls={`workbench-${tab.key}`} aria-selected={activeTab === tab.key} tabIndex={activeTab === tab.key ? 0 : -1} className={`side-panel__tab${activeTab === tab.key ? ' side-panel__tab--active' : ''}`} onClick={() => setActiveTab(tab.key)} onKeyDown={event => {
        const next = event.key === 'ArrowRight' ? (index + 1) % TABS.length : event.key === 'ArrowLeft' ? (index + TABS.length - 1) % TABS.length : event.key === 'Home' ? 0 : event.key === 'End' ? TABS.length - 1 : -1;
        if (next < 0) return;
        event.preventDefault(); setActiveTab(TABS[next].key); document.getElementById(`workbench-tab-${TABS[next].key}`)?.focus();
      }}><tab.icon size={14} />{t(tab.label)}</button>)}
    </div>
    <div className="side-panel__body">
      <div className="wb-panel__pane" role="tabpanel" id="workbench-artifacts" aria-labelledby="workbench-tab-artifacts" hidden={activeTab !== 'artifacts'}>{visited.has('artifacts') && artifacts}</div>
      <div className="wb-panel__pane" role="tabpanel" id="workbench-browser" aria-labelledby="workbench-tab-browser" hidden={activeTab !== 'browser'}>{visited.has('browser') && <BrowserPane visible={panelOpen && activeTab === 'browser'} />}</div>
      <div className="wb-panel__pane" role="tabpanel" id="workbench-terminal" aria-labelledby="workbench-tab-terminal" hidden={activeTab !== 'terminal'}>{visited.has('terminal') && <TerminalPane visible={panelOpen && activeTab === 'terminal'} />}</div>
    </div>
    {!expanded && <div className="wb-panel__resize" role="separator" aria-label="调整工作台宽度" aria-orientation="vertical" aria-valuemin={360} aria-valuemax={1000} aria-valuenow={width} tabIndex={0} onDoubleClick={() => update({ panelWidth: 520 })} onKeyDown={event => {
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); update({ panelWidth: Math.max(360, Math.min(1000, width + (event.key === 'ArrowLeft' ? 24 : -24))) }); }
    }} onPointerDown={event => {
      drag.current = { x: event.clientX, width: panel.current?.getBoundingClientRect().width || width };
      event.currentTarget.setPointerCapture(event.pointerId); event.preventDefault();
    }} onPointerMove={event => {
      if (!drag.current) return;
      update({ panelWidth: Math.round(Math.max(360, Math.min(1000, drag.current.width + drag.current.x - event.clientX))) });
    }} onPointerUp={event => { drag.current = null; event.currentTarget.releasePointerCapture(event.pointerId); }} onPointerCancel={() => { drag.current = null; }} />}
  </aside>;
}
