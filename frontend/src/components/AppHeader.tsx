import { Heart, Moon, PanelLeft, Settings, Sun } from 'lucide-react';
import { useLocation, useNavigate } from 'react-router-dom';
import brandMark from '@/assets/brand/header-icon.png';
import { ModeSwitch } from '@/components/ModeSwitch';
import { useTheme } from '@/hooks/useTheme';
import { useT } from '@/i18n';
import { nextTheme } from '@/lib/theme';
import { useUiStore } from '@/stores/uiStore';
import { useSidePanelStore } from '@/stores/artifactStore';
import { useEffect, useState } from 'react';

export function AppHeader({ office }: { office: boolean }) {
  const navigate = useNavigate();
  const location = useLocation();
  const t = useT();
  const { theme, toggleTheme } = useTheme();
  const [narrow, setNarrow] = useState(() => window.matchMedia('(max-width: 1180px)').matches);
  const panelOpen = useSidePanelStore(s => s.panelOpen);
  const collapsed = useUiStore(s => office ? !s.officeLibraryOpen || s.officeFocused : s.sidebarCollapsed || (narrow && panelOpen));
  useEffect(() => {
    const media = window.matchMedia('(max-width: 1180px)');
    const update = () => setNarrow(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  const toggleNavigation = () => {
    if (office) window.dispatchEvent(new CustomEvent('xjms:office-library-toggle'));
    else useUiStore.getState().toggleSidebar();
  };
  return <header className="app-header">
    <div className="app-header__identity">
      <button type="button" className="app-header__tool" onClick={toggleNavigation} aria-label={collapsed ? '展开导航' : '收起导航'} aria-expanded={!collapsed} title={collapsed ? '展开导航' : '收起导航'}><PanelLeft size={18} /></button>
      <div className="app-header__brand"><img src={brandMark} alt="" /><span>Syncore</span></div>
    </div>
    <ModeSwitch mode={office ? 'office' : 'chat'} />
    <div className="app-header__actions">
      <button type="button" className="app-header__tool" onClick={toggleTheme} title={t(`theme.switch.${nextTheme(theme)}`)} aria-label={t(`theme.switch.${nextTheme(theme)}`)}>{nextTheme(theme) === 'dark' ? <Moon size={17} /> : nextTheme(theme) === 'velvet' ? <Heart size={17} /> : <Sun size={17} />}</button>
      <button type="button" className="app-header__tool" onClick={() => navigate('/settings', { state: { background: location } })} aria-label={t('common.settings')} title={t('common.settings')}><Settings size={17} /></button>
    </div>
  </header>;
}
