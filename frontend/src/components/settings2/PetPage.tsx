import { useCallback, useEffect, useState } from 'react';
import { checkWakeWord, fetchVoiceSettings, fetchWakeStatus } from '@/lib/api';
import type { WakeStatusResponse } from '@/lib/api';
import type { VoiceSettingsResponse } from '@/types';
import { DEFAULT_PET_STATE, PET_ART } from '@/lib/pet';
import type { PetState } from '@/types/desktop';
import { Dot, PageHead, Row, Rows, Section, Switch } from './kit';

const LEVELS: Array<{ value: PetState['alwaysOnTop']; label: string }> = [
  { value: 'normal', label: '普通' }, { value: 'floating', label: '悬浮' }, { value: 'screen-saver', label: '覆盖全屏' },
];

export function PetPage() {
  const bridge = window.xjmsDesktop?.pet;
  const [state, setState] = useState<PetState | null>(null);
  const [error, setError] = useState('');
  const [wakeDraft, setWakeDraft] = useState('');
  const [wakeError, setWakeError] = useState('');
  const [wakeSaving, setWakeSaving] = useState(false);
  const [ackDraft, setAckDraft] = useState('');
  const [ackError, setAckError] = useState('');
  const [wakeStatus, setWakeStatus] = useState<WakeStatusResponse | null>(null);
  const [wakeStatusError, setWakeStatusError] = useState('');
  const [wakeStatusLoading, setWakeStatusLoading] = useState(false);
  const [voiceSettings, setVoiceSettings] = useState<VoiceSettingsResponse | null>(null);
  const load = useCallback(async () => {
    if (!bridge) return;
    try { setState({ ...DEFAULT_PET_STATE, ...await bridge.getState() }); setError(''); }
    catch { setError('暂时无法读取桌面助手设置，请重试。'); }
  }, [bridge]);
  useEffect(() => {
    void load();
    return bridge?.onStateChanged((value) => setState({ ...DEFAULT_PET_STATE, ...value }));
  }, [bridge, load]);
  const patch = (delta: Partial<PetState>) => {
    setState((value) => value ? { ...value, ...delta } : value);
    bridge?.setState(delta);
  };

  const loadWakeStatus = useCallback(async () => {
    setWakeStatusLoading(true);
    try {
      setWakeStatus(await fetchWakeStatus());
      setWakeStatusError('');
    } catch {
      setWakeStatus(null);
      setWakeStatusError('读取不到本地模型状态，确认后端已启动');
    } finally {
      setWakeStatusLoading(false);
    }
  }, []);
  useEffect(() => {
    if (bridge) void loadWakeStatus();
  }, [bridge, loadWakeStatus]);
  useEffect(() => {
    let alive = true;
    if (bridge) void fetchVoiceSettings().then((value) => { if (alive) setVoiceSettings(value); }).catch(() => {});
    return () => { alive = false; };
  }, [bridge]);

  const savedWakeWord = state?.wakeWord;
  useEffect(() => {
    if (savedWakeWord) setWakeDraft(savedWakeWord);
  }, [savedWakeWord]);
  const savedWakeAck = state?.wakeAck;
  useEffect(() => {
    if (savedWakeAck) setAckDraft(savedWakeAck);
  }, [savedWakeAck]);

  const commitWakeAck = () => {
    if (!state) return;
    const draft = ackDraft.trim();
    if (draft === state.wakeAck) { setAckError(''); return; }
    if (!draft) { setAckError('唤醒回应不能为空'); return; }
    setAckError('');
    patch({ wakeAck: draft });
  };

  const commitWakeWord = async () => {
    if (!state || wakeSaving) return;
    const draft = wakeDraft.trim();
    if (draft === state.wakeWord) { setWakeError(''); return; }
    setWakeSaving(true);
    try {
      const result = await checkWakeWord(draft);
      if (!result.ok) { setWakeError(result.reason || '这个唤醒词不能使用'); return; }
      setWakeError('');
      patch({ wakeWord: result.word });
    } catch {
      setWakeError('暂时无法校验唤醒词，确认后端已启动');
    } finally {
      setWakeSaving(false);
    }
  };

  if (!bridge) return <div className="tset tset-page"><PageHead title="桌面助手" /><p className="empty-hint">在桌面版中打开，即可启用桌面助手。</p></div>;
  return <div className="tset tset-page">
    <PageHead title="桌面助手" desc="Syncore 桌面助手驻留桌面，随时响应任务与提醒。" />
    {error && <div className="alert alert--error">{error} <button type="button" className="btn btn--sm" onClick={() => void load()}>重试</button></div>}
    {!state ? (!error && <div className="empty-hint"><span className="spinner" /> 正在读取设置…</div>) : <>
      <div className="pet-settings-preview">
        <div className="pet-settings-preview__portrait"><img src={PET_ART} alt="Syncore 桌面助手形象" /></div>
        <div><span className="pet-settings-preview__brand">SYNCORE</span><h3>桌面上的工作入口</h3><p>轻量驻留 · 快捷唤起 · 语音交互</p><span className="pet-settings-preview__tip">单击互动 · 双击打开工作台 · 右键快捷面板</span></div>
      </div>
      <Section title="桌面驻留">
        <Rows>
          <Row label="显示桌面助手" desc="也可以从系统托盘唤回" control={<Switch checked={state.enabled} ariaLabel="显示桌面助手" onChange={(value) => patch({ enabled: value })} />} />
          <Row label="语音播报" desc="播报新回复与计时提醒，关闭后仍显示文字" control={<Switch checked={!state.muteTts} ariaLabel="语音播报" onChange={(value) => patch({ muteTts: !value })} />} />
          <Row label="免打扰" desc="暂停日常提示与语音；保留确认提示和专注计时" control={<Switch checked={state.quietMode} ariaLabel="免打扰" onChange={(value) => patch({ quietMode: value })} />} />
          <Row label="安静休憩" desc="没有互动后进入安静状态" control={<select className="select select--sm" aria-label="安静休憩等待时间" value={state.idleTimeoutMs} onChange={(event) => patch({ idleTimeoutMs: Number(event.target.value) })}>{[60000, 300000, 900000].map((value) => <option key={value} value={value}>{value / 60000} 分钟</option>)}</select>} />
        </Rows>
      </Section>
      <Section title="语音唤醒" desc="桌面助手显示且未开启免打扰时监听，只用于助理对话。">
        <Rows>
          <Row
            label="语音唤醒"
            desc="先叫唤醒词，听到回应后再说话；回答播完自动回到等待"
            title="唤醒词检测在本机完成；提问的识别和回答的声音由「模型 → 语音模型」中的配置决定"
            control={<Switch checked={state.wakeEnabled} ariaLabel="语音唤醒" onChange={(value) => patch({ wakeEnabled: value })} />}
          />
          <Row
            label="唤醒词"
            desc={wakeError ? <span className="wake-word__error" role="alert">{wakeError}</span> : '2–10 个汉字，回车或移开光标保存'}
            control={
              <input
                className="input wake-word__input"
                aria-label="唤醒词"
                aria-invalid={Boolean(wakeError)}
                placeholder="你好，助手"
                value={wakeDraft}
                maxLength={24}
                disabled={wakeSaving}
                onChange={(event) => { setWakeDraft(event.target.value); if (wakeError) setWakeError(''); }}
                onBlur={() => void commitWakeWord()}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') { event.preventDefault(); event.currentTarget.blur(); }
                }}
              />
            }
          />
          <Row
            label="唤醒回应"
            desc={ackError ? <span className="wake-word__error" role="alert">{ackError}</span> : '成功唤醒后助手回应的一句话，说完才开始听你的问题'}
            control={
              <input
                className="input wake-word__input"
                aria-label="唤醒回应"
                aria-invalid={Boolean(ackError)}
                placeholder="我在"
                value={ackDraft}
                maxLength={50}
                onChange={(event) => { setAckDraft(event.target.value); if (ackError) setAckError(''); }}
                onBlur={commitWakeAck}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') { event.preventDefault(); event.currentTarget.blur(); }
                }}
              />
            }
          />
          <Row
            label="唤醒模型（本地）"
            title={wakeStatus?.path}
            desc={
              wakeStatusLoading ? '正在检测…'
                : wakeStatusError ? <span className="wake-word__error" role="alert">{wakeStatusError}</span>
                  : wakeStatus?.available ? <><Dot ok />{wakeStatus.model} · 已就绪，只负责听唤醒词</>
                    : <><Dot ok={false} />{wakeStatus?.reason || '未就绪'}</>
            }
            control={<button type="button" className="btn btn--sm" disabled={wakeStatusLoading} onClick={() => void loadWakeStatus()}>重新检测</button>}
          />
          <Row label="提问识别" desc={voiceSettings
            ? voiceSettings.config.VOICE_STT_BACKEND === 'local' ? '本机 SenseVoice 将你的话转成文字' : `${voiceSettings.config.VOICE_STT_MODEL} · 云端语音转文字`
            : '使用「模型 → 语音模型」中保存的识别配置'} />
          <Row label="回答声音" desc={voiceSettings
            ? voiceSettings.config.VOICE_TTS_BACKEND === 'edge' ? `Edge · ${voiceSettings.config.VOICE_TTS_VOICE_EDGE}` : `${voiceSettings.config.VOICE_TTS_MODEL} · 音色 ${voiceSettings.config.VOICE_TTS_VOICE}`
            : '唤醒回应、回答和提醒共用已保存的朗读模型'}
            control={<a className="btn btn--sm" href="#/settings/models?tab=voice">调整回答声音</a>} />
        </Rows>
      </Section>
      <Section title="专注与休息" desc="随身面板中可开始、暂停和继续专注，计时会记住进度。">
        <Rows>
          <Row label="默认专注时长" control={<div className="seg" aria-label="默认专注时长">{[15, 25, 45].map((value) => <button type="button" key={value} className={`seg__btn${state.focusMinutes === value ? ' seg__btn--on' : ''}`} aria-pressed={state.focusMinutes === value} onClick={() => patch({ focusMinutes: value })}>{value} 分钟</button>)}</div>} />
          <Row label="休息提醒" desc="专注中和免打扰时暂停日常休息提示" control={<Switch checked={state.remindersEnabled} ariaLabel="休息提醒" onChange={(value) => patch({ remindersEnabled: value })} />} />
          <Row label="提醒间隔" control={<select className="select select--sm" aria-label="休息提醒间隔" value={state.reminderMinutes} onChange={(event) => patch({ reminderMinutes: Number(event.target.value) })}>{[15, 30, 45, 60, 90, 120].map((value) => <option key={value} value={value}>{value} 分钟</option>)}</select>} />
        </Rows>
      </Section>
      <Section title="外观与窗口">
        <Rows>
          <Row label="形象大小" control={<div className="seg" aria-label="形象大小">{[{ value: .75, label: '小巧' }, { value: 1, label: '标准' }, { value: 1.25, label: '舒展' }].map((option) => <button type="button" key={option.value} className={`seg__btn${state.scale === option.value ? ' seg__btn--on' : ''}`} aria-pressed={state.scale === option.value} onClick={() => patch({ scale: option.value })}>{option.label}</button>)}</div>} />
          <Row label="减少动态效果" control={<Switch checked={state.reducedMotion} ariaLabel="减少动态效果" onChange={(value) => patch({ reducedMotion: value })} />} />
          <Row label="置顶层级" control={<div className="seg" aria-label="置顶层级">{LEVELS.map((option) => <button type="button" key={option.value} className={`seg__btn${state.alwaysOnTop === option.value ? ' seg__btn--on' : ''}`} aria-pressed={state.alwaysOnTop === option.value} onClick={() => patch({ alwaysOnTop: option.value })}>{option.label}</button>)}</div>} />
          <Row label="默认位置" desc="回到主屏右下角，自动避开任务栏" control={<button type="button" className="btn btn--sm" onClick={() => void bridge.resetPosition().then(setState).catch(() => setError('位置重置失败，请重试。'))}>重置位置</button>} />
        </Rows>
      </Section>
    </>}
  </div>;
}

export default PetPage;
