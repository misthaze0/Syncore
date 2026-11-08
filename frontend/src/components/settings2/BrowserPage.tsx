import { useState } from 'react';
import { Check, ExternalLink, Globe, PanelRight, RotateCcw } from '@/components/icons';
import { normalizeBrowserUrl } from '@/lib/browser';
import { useWorkbenchStore, WORKBENCH_DEFAULTS } from '@/stores/workbenchStore';
import { isDesktopShell } from '@/types/desktop';
import { PageHead, Rows, Row, Section } from './kit';

export function BrowserPage() {
  const { preferences: prefs, update } = useWorkbenchStore();
  const [homepage, setHomepage] = useState(prefs.homepage);
  const [error, setError] = useState('');
  const desktop = isDesktopShell();
  const commitHomepage = () => {
    const normalized = homepage.trim() ? normalizeBrowserUrl(homepage) : '';
    if (normalized === null) { setError('请输入有效的 http 或 https 网址。'); return; }
    update({ homepage: normalized }); setHomepage(normalized); setError('');
  };

  return <div className="tset tset-page wb-settings">
    <div className="wb-settings__heading"><PageHead title="浏览器" /><span className="wb-settings__saved"><Check size={12} />自动保存</span></div>
    <p className="tset-page__desc">在对话旁查阅网页，让资料与工作保持在同一处。</p>
    <div className="wb-settings__hero">
      <div className="wb-settings__hero-icon"><Globe size={24} strokeWidth={1.5} /></div>
      <div><strong>浏览与对话，并排进行</strong><p>独立网页标签 · 保留登录状态 · 随时展开</p></div>
      <span className="badge badge--muted">{desktop ? '桌面版已就绪' : '网页版'}</span>
    </div>
    <Section title="打开链接">
      <div className="wb-setting-choices" role="group" aria-label="链接打开方式">
        {([
          ['internal', Globe, '内置浏览器', '在右侧工作台打开，随时回到对话'],
          ['system', ExternalLink, '系统浏览器', '使用你日常的浏览器查看网页'],
        ] as const).map(([value, Icon, title, desc]) => <button type="button" key={value} className={`wb-setting-choice${(desktop ? prefs.linkTarget : 'system') === value ? ' is-selected' : ''}`} aria-pressed={(desktop ? prefs.linkTarget : 'system') === value} disabled={!desktop && value === 'internal'} onClick={() => update({ linkTarget: value })}>
          <span className="wb-setting-choice__top"><Icon size={18} /><span className="wb-setting-choice__radio">{(desktop ? prefs.linkTarget : 'system') === value && <Check size={10} />}</span></span><strong>{title}</strong><span>{!desktop && value === 'internal' ? '在桌面版中使用' : desc}</span>
        </button>)}
      </div>
    </Section>
    <Section title="新标签页与显示">
      <Rows>
        <Row label="默认首页" desc="新建网页标签时打开，留空显示起始页。" stack control={<div className="wb-settings__field"><input className="wb-settings__input" aria-label="默认首页" placeholder="https://example.com" value={homepage} aria-invalid={Boolean(error)} onChange={event => { setHomepage(event.target.value); setError(''); }} onBlur={commitHomepage} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); commitHomepage(); event.currentTarget.blur(); } }} />{error && <span className="wb-inline-error" role="alert">{error}</span>}</div>} />
        <Row label="网页缩放" desc="只调整内置网页，立即应用到已打开的标签。" control={<select className="wb-settings__select" aria-label="网页缩放" value={prefs.browserZoom} onChange={event => update({ browserZoom: Number(event.target.value) })}>{[50, 75, 80, 90, 100, 110, 125, 150, 175, 200].map(value => <option key={value} value={value}>{value}%{value === 100 ? '（默认）' : ''}</option>)}</select>} />
      </Rows>
    </Section>
    <Section title="工作台布局">
      <Rows><Row label={<><PanelRight size={14} /> 并排宽度</>} desc="浏览器、终端与产物共用；也可以拖动工作台左侧边缘调整。" control={<div className="wb-settings__range"><input type="range" min="360" max="1000" step="1" aria-label="工作台宽度" value={prefs.panelWidth} onChange={event => update({ panelWidth: Number(event.target.value) })} /><output>{prefs.panelWidth}px</output></div>} /></Rows>
      <p className="wb-settings__note">窄窗口会自动使用浮层，给网页保留足够的阅读空间。</p>
    </Section>
    <div className="wb-settings__footer"><button type="button" className="btn btn--sm btn--ghost" onClick={() => { update({ linkTarget: 'internal', homepage: '', browserZoom: 100, panelWidth: WORKBENCH_DEFAULTS.panelWidth }); setHomepage(''); setError(''); }}><RotateCcw size={13} />恢复默认</button><span className="wb-settings__note">偏好保存在此设备</span></div>
  </div>;
}

export default BrowserPage;
