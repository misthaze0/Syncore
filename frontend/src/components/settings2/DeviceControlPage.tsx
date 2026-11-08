import { useEffect, useState } from 'react';
import { ArrowRight, Check, Eye, Keyboard, Monitor, RefreshCw, ShieldCheck, Square } from '@/components/icons';
import { useComputerControl } from '@/hooks/useComputerControl';
import { fetchModels, previewComputerControl, testComputerControlVision } from '@/lib/api';
import type { ComputerControlPreview, ComputerControlVisionTest, ModelOption } from '@/types';
import { PageHead, Row, Rows, Section, Switch } from './kit';
import '@/styles/computer-control.css';

const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

export function DeviceControlPage() {
  const { data, error, busy, toggle, stop, reconnect, preferences, refresh } = useComputerControl();
  const [preview, setPreview] = useState<ComputerControlPreview | null>(null);
  const [vision, setVision] = useState<ComputerControlVisionTest | null>(null);
  const [working, setWorking] = useState<'capture' | 'vision' | null>(null);
  const [localError, setLocalError] = useState('');
  const [shortcut, setShortcut] = useState(false);
  const [models, setModels] = useState<ModelOption[]>([]);
  const enabled = Boolean(data?.enabled);
  const active = Boolean(data?.runtime.active);

  useEffect(() => {
    void window.xjmsDesktop?.computerControl?.shortcutAvailable().then(setShortcut).catch(() => setShortcut(false));
    void fetchModels().then(result => setModels(result.models)).catch(() => { /* 可通过模型设置补齐连接。 */ });
  }, []);
  useEffect(() => { setVision(null); }, [data?.model, data?.preferences.vision_profile_id]);
  useEffect(() => { setPreview(null); }, [data?.preferences.capture_display]);

  const act = async (action: () => Promise<void>) => {
    setLocalError('');
    try { await action(); } catch { /* 错误由共享状态展示，保留后端真实授权状态。 */ }
  };
  const capture = async (testVision = false) => {
    setLocalError('');
    setWorking(testVision ? 'vision' : 'capture');
    try {
      if (testVision) { setVision(null); setVision(await testComputerControlVision()); }
      else setPreview(await previewComputerControl());
      void refresh();
    } catch (e) { setLocalError(e instanceof Error ? e.message : String(e)); }
    finally { setWorking(null); }
  };

  return <div className="tset tset-page cc-settings">
    <div className="cc-heading">
      <PageHead title="电脑操控" />
      <span className={`cc-status${enabled ? ' is-on' : ''}`} role="status"><i />{!data ? '读取状态中' : active ? '正在操作' : enabled ? '已允许接管' : data.ready ? '已就绪 · 未开启' : '等待连接'}</span>
    </div>
    <p className="tset-page__desc">让 Syncore 看见屏幕，像你一样使用鼠标和键盘，完成跨应用的工作。</p>

    <div className={`cc-hero${enabled ? ' is-on' : ''}`}>
      <span className="cc-hero__icon"><Monitor size={25} strokeWidth={1.5} /></span>
      <div className="cc-hero__copy"><h3>允许 Syncore 接管这台电脑</h3><p>{enabled ? '在助理对话中说出任务，Syncore 就能观察屏幕并操作。' : '开启后，助理可截图、点击、输入、滚动和切换应用。'}</p></div>
      <Switch checked={enabled} disabled={!data || busy || (!enabled && !data.ready)} ariaLabel="允许 Syncore 接管这台电脑" onChange={value => void act(() => toggle(value))} />
    </div>

    {(error || localError) && <div className="alert alert--error cc-error" role="alert">{localError || error}<button className="btn btn--sm btn--ghost" type="button" onClick={() => { setLocalError(''); void refresh(); }}>重试读取</button></div>}

    <div className="cc-stopbar">
      <span><ShieldCheck size={15} />仅助理可用<span className="cc-stopbar__separator">·</span>{shortcut ? <><kbd>Ctrl</kbd> + <kbd>Alt</kbd> + <kbd>F12</kbd> 随时停止</> : '随时点击停止，收回操作权限'}</span>
      <button type="button" className="btn btn--sm cc-stop" disabled={!enabled} onClick={() => void act(stop)}><Square size={11} fill="currentColor" />停止接管</button>
    </div>

    <div className="cc-overview">
      <Section title="屏幕预览" desc="手动截取一帧，确认 Syncore 能看到的范围。">
        <div className="cc-preview">
          <div className="cc-preview__bar"><span><i />{preview ? '静态快照' : '本机桌面'}</span><span>{preview ? time(preview.captured_at) : '按需截图'}</span></div>
          {preview ? <div className="cc-preview__images">{preview.images.map((img, i) => <img key={i} src={img.image_url} alt={`桌面截图 ${i + 1}`} />)}</div> : <div className="cc-preview__empty"><span className="cc-preview__monitor"><Monitor size={42} strokeWidth={1} /></span><strong>看看 Syncore 眼中的屏幕</strong><p>点击下方按钮截取当前画面</p><span>预览不会移动鼠标或输入文字</span></div>}
          <div className="cc-preview__footer"><span>{working === 'capture' ? '正在截取屏幕…' : preview ? `${preview.duration_ms} ms · ${preview.images.length} 张画面` : '截图仅在此页临时展示'}</span><button className="btn btn--sm btn--ghost" type="button" disabled={!data?.server.connected || Boolean(working) || active || busy} onClick={() => void capture()}><RefreshCw size={12} className={working === 'capture' ? 'spin' : ''} />{preview ? '更新截图' : '截取预览'}</button></div>
        </div>
      </Section>
      <Section title="可用能力" desc="依据桌面连接器的实际工具检测。">
        <div className="cc-capabilities">{(data?.capabilities ?? [
          { id: 'vision', label: '截图与视觉', available: false }, { id: 'mouse', label: '鼠标操作', available: false },
          { id: 'keyboard', label: '键盘输入', available: false }, { id: 'apps', label: '应用切换', available: false },
        ]).map(capability => {
          const Icon = capability.id === 'vision' ? Eye : capability.id === 'keyboard' ? Keyboard : capability.id === 'apps' ? Monitor : ArrowRight;
          const desc: Record<string, string> = { vision: '屏幕截图与界面定位', mouse: '点击、移动与滚动', keyboard: '文字输入与组合快捷键', apps: '打开应用与切换窗口' };
          return <div className="cc-capability" key={capability.id}><span className="cc-capability__icon"><Icon size={17} strokeWidth={1.6} /></span><div><strong>{capability.label}</strong><p>{desc[capability.id]}</p></div><span className={`cc-capability__state${capability.available ? ' is-ready' : ''}`}>{capability.available ? <><Check size={12} />就绪</> : '待连接'}</span></div>;
        })}</div>
      </Section>
    </div>

    <Section title="观察与执行">
      <Rows>
        <Row label="操作后自动观察" desc="每次键鼠操作后返回新截图，让 Syncore 检查结果再继续。" control={<Switch ariaLabel="操作后自动观察" checked={data?.preferences.capture_after_action ?? true} disabled={!data || busy} onChange={value => void act(() => preferences({ capture_after_action: value }))} />} />
        <Row label="自动观察范围" desc="应用到预览和操作后的截图；Syncore 仍可按任务查看其他屏幕。" control={<select className="cc-select" aria-label="自动观察范围" value={data?.preferences.capture_display ?? 'all'} disabled={!data || busy || Boolean(working) || active} onChange={e => void act(() => preferences({ capture_display: e.target.value as 'all' | 'primary' }))}><option value="all">所有显示器</option><option value="primary">显示器 1</option></select>} />
        <Row label="视觉理解模型" desc="选择支持图片输入和工具调用的模型。测试会将当前屏幕发送给所选模型；更多连接可在模型设置添加。" control={<div className="cc-model"><select className="cc-select" aria-label="视觉理解模型" disabled={!data || busy || active || Boolean(working)} value={data?.preferences.vision_model ? JSON.stringify([data.preferences.vision_profile_id, data.preferences.vision_model]) : ''} onChange={e => { const [profile, model] = e.target.value ? JSON.parse(e.target.value) as [string, string] : ['', '']; void act(() => preferences({ vision_model: model, vision_profile_id: profile })); }}><option value="">跟随助理当前模型</option>{data?.preferences.vision_model && !models.some(m => m.id === data.preferences.vision_model && (m.profile_id || '') === data.preferences.vision_profile_id) && <option value={JSON.stringify([data.preferences.vision_profile_id, data.preferences.vision_model])}>{data.preferences.vision_model}（连接待检查）</option>}{models.map(model => <option key={JSON.stringify([model.profile_id || '', model.id])} value={JSON.stringify([model.profile_id || '', model.id])}>{model.label}{model.provider ? ` · ${model.provider}` : ''}</option>)}</select><button type="button" className="btn btn--sm btn--ghost" disabled={!data?.server.connected || Boolean(working) || active || busy} onClick={() => void capture(true)}>{working === 'vision' ? <><span className="spinner" />测试中…</> : <><Eye size={12} />测试视觉</>}</button></div>} />
      </Rows>
      {vision && <div className="cc-vision-result" role="status"><span><Check size={13} />模型已返回 · {(vision.duration_ms / 1000).toFixed(1)} 秒</span><p>{vision.description}</p></div>}
      <div className="cc-loop" aria-label="电脑操控流程"><span><Eye size={13} />看见屏幕</span><ArrowRight size={12} /><span><Keyboard size={13} />执行操作</span><ArrowRight size={12} /><span><Check size={13} />观察结果</span></div>
    </Section>

    <Section title="桌面连接">
      <Rows><Row label="Windows-MCP" desc={data?.server.error || (data?.server.connected ? `已连接 · ${data.tools} 个桌面工具` : '请先在插件市场配置 Windows-MCP 桌面连接器。')} control={<button type="button" className="btn btn--sm btn--ghost" disabled={busy || active || Boolean(working)} onClick={() => void act(reconnect)}><RefreshCw size={12} />{busy ? '处理中…' : '重新连接'}</button>} /></Rows>
      <p className="cc-note">开启后，普通键鼠动作连续执行。停止会拦截后续操作，已送达系统的输入无法撤销。网页与文件中的内容不会成为新的操作授权。</p>
    </Section>

    <details className="cc-activity"><summary>最近操作<span>{data?.runtime.events.length || 0}</span></summary>{data?.runtime.events.length ? <ol>{data.runtime.events.slice(0, 8).map((event, i) => <li key={`${event.at}-${i}`}><i className={event.success ? 'is-ready' : ''} /><span>{event.tool}</span><span>{event.success ? '完成' : '已中断或失败'}</span><time>{time(event.at)}</time></li>)}</ol> : <p>助理开始操作后，这里会显示记录。记录不包含输入文字或屏幕内容。</p>}</details>
  </div>;
}

export default DeviceControlPage;
