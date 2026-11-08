import { Check, RotateCcw, Terminal } from '@/components/icons';
import { useWorkbenchStore, WORKBENCH_DEFAULTS } from '@/stores/workbenchStore';
import { PageHead, Row, Rows, Section, Switch } from './kit';

export function TerminalPage() {
  const { preferences: prefs, update } = useWorkbenchStore();
  const windows = window.xjmsDesktop?.platform === 'win32';
  return <div className="tset tset-page wb-settings">
    <div className="wb-settings__heading"><PageHead title="终端" /><span className="wb-settings__saved"><Check size={12} />自动保存</span></div>
    <p className="tset-page__desc">在工作台运行命令。多个会话独立工作，收起面板后继续运行。</p>
    <div className={`wb-terminal-preview${prefs.terminalTheme === 'dark' ? ' wb-terminal-preview--dark' : ''}`} style={{ fontSize: prefs.terminalFontSize }} aria-label="终端显示预览">
      <div className="wb-terminal-preview__bar"><Terminal size={12} /><span>显示预览</span><span>UTF-8</span></div>
      <div className="wb-terminal-preview__body"><div><span className="wb-terminal-preview__prompt">❯</span> echo "Hello, Syncore"</div><div className="wb-terminal-preview__result">Hello, Syncore</div><div><span className="wb-terminal-preview__prompt">❯</span> <span className={`wb-terminal-preview__cursor wb-terminal-preview__cursor--${prefs.terminalCursor}${prefs.terminalBlink ? ' is-blinking' : ''}`} /></div></div>
    </div>
    <Section title="会话">
      <Rows>
        <Row label="默认终端" desc="更改后应用到新会话，现有会话继续运行。" control={<select className="wb-settings__select" aria-label="默认终端" disabled={!windows} value={windows ? prefs.terminalShell : 'default'} onChange={event => update({ terminalShell: event.target.value as typeof prefs.terminalShell })}><option value="default">{windows ? '自动（PowerShell）' : '系统默认终端'}</option>{windows && <><option value="powershell">PowerShell</option><option value="cmd">命令提示符</option></>}</select>} />
        <Row label="保留输出行数" desc="每个会话可向上回看的历史输出。" control={<select className="wb-settings__select" aria-label="保留输出行数" value={prefs.terminalScrollback} onChange={event => update({ terminalScrollback: Number(event.target.value) })}>{[1000, 5000, 10000, 50000].map(value => <option value={value} key={value}>{value.toLocaleString()} 行</option>)}</select>} />
      </Rows>
    </Section>
    <Section title="外观">
      <Rows>
        <Row label="配色" control={<select className="wb-settings__select" aria-label="终端配色" value={prefs.terminalTheme} onChange={event => update({ terminalTheme: event.target.value as typeof prefs.terminalTheme })}><option value="follow">跟随应用主题</option><option value="dark">始终深色</option></select>} />
        <Row label="字号" control={<div className="wb-settings__range"><input type="range" min="10" max="20" step="1" aria-label="终端字号" value={prefs.terminalFontSize} onChange={event => update({ terminalFontSize: Number(event.target.value) })} /><output>{prefs.terminalFontSize}px</output></div>} />
        <Row label="光标样式" control={<select className="wb-settings__select" aria-label="光标样式" value={prefs.terminalCursor} onChange={event => update({ terminalCursor: event.target.value as typeof prefs.terminalCursor })}><option value="bar">竖线</option><option value="block">方块</option><option value="underline">下划线</option></select>} />
        <Row label="光标闪烁" control={<Switch ariaLabel="光标闪烁" checked={prefs.terminalBlink} onChange={terminalBlink => update({ terminalBlink })} />} />
      </Rows>
    </Section>
    <p className="wb-settings__note">选中后按 Ctrl + Shift + C 复制，Ctrl + Shift + V 粘贴，Ctrl + C 中断命令。</p>
    <div className="wb-settings__footer"><button type="button" className="btn btn--sm btn--ghost" onClick={() => update({ terminalFontSize: WORKBENCH_DEFAULTS.terminalFontSize, terminalScrollback: WORKBENCH_DEFAULTS.terminalScrollback, terminalTheme: 'follow', terminalCursor: 'bar', terminalBlink: true, terminalShell: 'default' })}><RotateCcw size={13} />恢复默认</button><span className="wb-settings__note">显示调整立即生效</span></div>
  </div>;
}
