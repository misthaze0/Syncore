import { confirmAction } from '@/stores/confirmStore';
/**
 * 助理页 —— 人设卡（预览 + 表单，所见即所得）、作用范围与微信远程通道。
 * 保存语义沿用「脏检查 + 部分更新」：只提交与已存版本的差异，保存后下一轮回答生效。
 * 「全局生效」是范围开关而非内容编辑，点击即落盘，不跟表单草稿一起等保存。
 * 可见文字只留标签与控件；机制说明一律进 title 悬停提示。
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { Check, Plus, QrCode, RotateCcw, X } from '@/components/icons';
import {
  fetchChannels,
  fetchPersona,
  fetchPersonaFragment,
  resetPersona,
  updatePersona,
} from '@/lib/api';
import { toast } from '@/stores/toastStore';
import type {
  ChannelState,
  PersonaFragmentResponse,
  PersonaProfile,
  PersonaResponse,
} from '@/types';
import { PERSONA_TONE_CUSTOM } from '@/types';

import { PageHead, Row, Rows, Section, Switch } from './kit';
import { WechatQrDialog } from './WechatQrDialog';

/** 头像边长（px）：上传时先在 canvas 里缩到这么大再编码，人设文件才不会被原图撑爆 */
const AVATAR_SIZE = 256;
const MAX_TRAITS = 8;
const MAX_EXAMPLES = 6;
const MAX_QUIRKS = 4;

/** 读成 data URL 并缩成正方形小图（居中裁切）；超限或不是图片时返回 null */
function fileToAvatarDataUrl(file: File): Promise<string | null> {
  return new Promise((resolve) => {
    if (!file.type.startsWith('image/')) {
      resolve(null);
      return;
    }
    const reader = new FileReader();
    reader.onerror = () => resolve(null);
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => resolve(null);
      img.onload = () => {
        const canvas = document.createElement('canvas');
        canvas.width = AVATAR_SIZE;
        canvas.height = AVATAR_SIZE;
        const ctx = canvas.getContext('2d');
        if (!ctx) {
          resolve(null);
          return;
        }
        const side = Math.min(img.width, img.height);
        ctx.drawImage(
          img,
          (img.width - side) / 2,
          (img.height - side) / 2,
          side,
          side,
          0,
          0,
          AVATAR_SIZE,
          AVATAR_SIZE,
        );
        resolve(canvas.toDataURL('image/png'));
      };
      img.src = String(reader.result);
    };
    reader.readAsDataURL(file);
  });
}

/** 通道状态行：连接状态与绑定时间都可见；拿不到状态是警告，保持可见；未绑定不出说明文字 */
function boundLine(wechat: ChannelState | null): string {
  if (!wechat) return '无法获取通道状态';
  if (!wechat.bound) return '';
  const at = wechat.bound_at ? new Date(wechat.bound_at * 1000) : null;
  const when = at
    ? `${at.toLocaleDateString()} ${at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
    : '';
  const head = wechat.running ? '已连接' : '凭据已保存，通道未运行';
  return when ? `${head} · ${when}` : head;
}

export function AssistantPage() {
  const [persona, setPersona] = useState<PersonaResponse | null>(null);
  const [draft, setDraft] = useState<PersonaProfile | null>(null);
  const [saving, setSaving] = useState(false);
  const [savingGlobal, setSavingGlobal] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [traitDraft, setTraitDraft] = useState('');
  const [quirkDraft, setQuirkDraft] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);
  const [fragment, setFragment] = useState<PersonaFragmentResponse | null>(null);
  const [fragmentLoading, setFragmentLoading] = useState(false);

  const [wechat, setWechat] = useState<ChannelState | null>(null);
  const [qrOpen, setQrOpen] = useState(false);

  const refreshChannels = useCallback(() => {
    fetchChannels()
      .then((res) => setWechat(res.channels.find((c) => c.key === 'wechat') ?? null))
      .catch(() => setWechat(null));
  }, []);

  const refreshFragment = useCallback(() => {
    setFragmentLoading(true);
    fetchPersonaFragment()
      .then(setFragment)
      .catch(() => setFragment(null))
      .finally(() => setFragmentLoading(false));
  }, []);

  useEffect(() => {
    fetchPersona()
      .then((p) => {
        setPersona(p);
        setDraft(p.profile);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    refreshChannels();
  }, [refreshChannels]);

  const dirty =
    draft !== null && persona !== null && JSON.stringify(draft) !== JSON.stringify(persona.profile);

  const onSave = useCallback(async () => {
    if (!draft || !persona || !dirty) return;
    setSaving(true);
    setError(null);
    try {
      const p = persona.profile;
      const changes: Parameters<typeof updatePersona>[0] = {};
      if (draft.name !== p.name) changes.name = draft.name;
      if (draft.tagline !== p.tagline) changes.tagline = draft.tagline;
      if (draft.tone !== p.tone) changes.tone = draft.tone;
      if (draft.tone_custom !== p.tone_custom) changes.tone_custom = draft.tone_custom;
      if (draft.instructions !== p.instructions) changes.instructions = draft.instructions;
      if ((draft.avatar ?? '') !== (p.avatar ?? '')) changes.avatar = draft.avatar ?? '';
      if (JSON.stringify(draft.traits ?? []) !== JSON.stringify(p.traits ?? [])) changes.traits = draft.traits ?? [];
      if ((draft.greeting ?? '') !== (p.greeting ?? '')) changes.greeting = draft.greeting ?? '';
      if (JSON.stringify(draft.examples ?? []) !== JSON.stringify(p.examples ?? [])) changes.examples = draft.examples ?? [];
      if ((draft.preset ?? '') !== (p.preset ?? '')) changes.preset = draft.preset ?? '';
      if ((draft.address_me ?? '') !== (p.address_me ?? '')) changes.address_me = draft.address_me ?? '';
      if (JSON.stringify(draft.quirks ?? []) !== JSON.stringify(p.quirks ?? [])) changes.quirks = draft.quirks ?? [];
      if ((draft.time_awareness ?? true) !== (p.time_awareness ?? true)) {
        changes.time_awareness = draft.time_awareness ?? true;
      }
      if ((draft.proactive ?? 'light') !== (p.proactive ?? 'light')) {
        changes.proactive = draft.proactive ?? 'light';
      }
      if ((draft.quiet_start ?? '22:00') !== (p.quiet_start ?? '22:00')) {
        changes.quiet_start = draft.quiet_start ?? '22:00';
      }
      if ((draft.quiet_end ?? '08:00') !== (p.quiet_end ?? '08:00')) {
        changes.quiet_end = draft.quiet_end ?? '08:00';
      }
      const minGap = draft.proactive_min_gap_hours ?? 12;
      if (minGap !== (p.proactive_min_gap_hours ?? 12)) changes.proactive_min_gap_hours = minGap;
      const dailyMax = draft.proactive_daily_max ?? 1;
      if (dailyMax !== (p.proactive_daily_max ?? 1)) changes.proactive_daily_max = dailyMax;
      // 保存即使用：界面上没有启停开关，保存后一律注入 Prompt
      changes.enabled = true;
      const res = await updatePersona(changes);
      setPersona((prev) => (prev ? { ...prev, profile: res.profile } : prev));
      setDraft(res.profile);
      toast('人设卡已保存，下一轮回答生效', 'success');
      void refreshFragment();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, [draft, persona, dirty, refreshFragment]);

  /**
   * 作用范围即时保存：开关本身就是「点击即生效」的语义。
   * 只更新草稿里的范围标记与时间戳 —— 表单里其他未保存的编辑保持原样。
   */
  const onToggleGlobal = useCallback(
    async (next: boolean) => {
      if (!persona || savingGlobal) return;
      setSavingGlobal(true);
      setError(null);
      try {
        const res = await updatePersona({ apply_globally: next });
        setPersona((prev) => (prev ? { ...prev, profile: res.profile } : prev));
        setDraft((d) =>
          d
            ? {
                ...d,
                apply_globally: res.profile.apply_globally,
                updated_at: res.profile.updated_at,
              }
            : d,
        );
        toast(next ? '人设卡已对对话与助理全局生效' : '人设卡仅在助理里生效', 'success');
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setSavingGlobal(false);
      }
    },
    [persona, savingGlobal],
  );

  const onReset = useCallback(async () => {
    if (!await confirmAction({ title: '恢复默认人设卡？', description: '当前的自定义内容与全局生效开关都会被重置。', confirmLabel: '恢复默认', tone: 'warning' })) return;
    setSaving(true);
    setError(null);
    try {
      const res = await resetPersona();
      setPersona((prev) => (prev ? { ...prev, profile: res.profile } : prev));
      setDraft(res.profile);
      toast('已恢复默认人设卡', 'success');
      void refreshFragment();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, [refreshFragment]);

  const onDiscard = useCallback(() => {
    if (!persona) return;
    setDraft(persona.profile);
    setTraitDraft('');
    setQuirkDraft('');
  }, [persona]);

  /** 预设只填充草稿里「说话风格」相关的几项，名字/头像/称呼/主动问候都不动 */
  const applyPreset = useCallback(
    (key: string) => {
      const preset = persona?.presets?.[key];
      if (!preset || !draft) return;
      setDraft({
        ...draft,
        preset: key,
        tone: preset.tone,
        traits: [...(preset.traits ?? [])],
        instructions: preset.instructions,
        examples: [...(preset.examples ?? [])],
        quirks: [...(preset.quirks ?? [])],
      });
    },
    [draft, persona],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        if (dirty && !saving) void onSave();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [dirty, saving, onSave]);

  const onPickAvatar = useCallback(
    async (file: File | undefined) => {
      if (!file || !draft) return;
      const dataUrl = await fileToAvatarDataUrl(file);
      if (!dataUrl) {
        toast('这张图读不出来，换一张试试', 'error');
        return;
      }
      setDraft({ ...draft, avatar: dataUrl });
    },
    [draft],
  );

  const addTrait = useCallback(() => {
    const value = traitDraft.trim().slice(0, 12);
    if (!value || !draft) return;
    const traits = draft.traits ?? [];
    if (traits.length >= MAX_TRAITS) {
      toast(`性格标签最多 ${MAX_TRAITS} 个`, 'warning');
      return;
    }
    if (traits.includes(value)) {
      setTraitDraft('');
      return;
    }
    setDraft({ ...draft, traits: [...traits, value] });
    setTraitDraft('');
  }, [traitDraft, draft]);

  const addQuirk = useCallback(() => {
    const value = quirkDraft.trim().slice(0, 16);
    if (!value || !draft) return;
    const quirks = draft.quirks ?? [];
    if (quirks.length >= MAX_QUIRKS) {
      toast(`小习惯最多 ${MAX_QUIRKS} 个`, 'warning');
      return;
    }
    if (quirks.includes(value)) {
      setQuirkDraft('');
      return;
    }
    setDraft({ ...draft, quirks: [...quirks, value] });
    setQuirkDraft('');
  }, [quirkDraft, draft]);

  const personaTones = persona?.tone_presets ?? {};
  const isCustomTone = draft?.tone === PERSONA_TONE_CUSTOM;
  const traits = draft?.traits ?? [];
  const examples = draft?.examples ?? [];
  const quirks = draft?.quirks ?? [];
  const presets = persona?.presets ?? {};
  const proactiveOn = (draft?.proactive ?? 'light') !== 'off';
  const timeAwareness = draft?.time_awareness !== false;

  return (
    <div className="tset tset-page">
      <PageHead title="助理" />

      <Section title="人设卡">
        {!draft ? (
          <div className="empty-hint">
            {error ? `未能加载人设卡：${error}` : '正在读取人设卡…'}
          </div>
        ) : (
          <>
            {Object.keys(presets).length > 0 && (
              <div className="persona-presets">
                {Object.entries(presets).map(([key, preset]) => (
                  <button
                    key={key}
                    type="button"
                    className={`persona-preset-card${
                      draft.preset === key ? ' persona-preset-card--on' : ''
                    }`}
                    onClick={() => applyPreset(key)}
                    title="只填充语气、性格、小习惯、说明与示例，不改名字、头像、称呼和主动问候"
                  >
                    <span className="persona-preset-card__label">{preset.label}</span>
                    <span className="persona-preset-card__desc">{preset.description}</span>
                  </button>
                ))}
              </div>
            )}

            <div className="persona-card">
              <button
                type="button"
                className="persona-card__avatar"
                onClick={() => fileRef.current?.click()}
                title={draft.avatar ? '点击更换头像' : '点击上传头像'}
              >
                {draft.avatar ? (
                  <img src={draft.avatar} alt="" />
                ) : (
                  <span className="persona-card__avatar-fallback">{draft.name.slice(0, 1) || 'S'}</span>
                )}
              </button>
              <input
                ref={fileRef}
                type="file"
                accept="image/*"
                style={{ display: 'none' }}
                onChange={(e) => {
                  void onPickAvatar(e.target.files?.[0]);
                  e.target.value = '';
                }}
              />
              <div className="persona-card__body">
                <div className="persona-card__name">
                  {draft.name || '未命名'}
                </div>
                {draft.tagline && <div className="persona-card__tagline">{draft.tagline}</div>}
                {traits.length > 0 && (
                  <div className="persona-card__traits">
                    {traits.map((tr) => (
                      <span key={tr} className="badge badge--accent">{tr}</span>
                    ))}
                  </div>
                )}
                {draft.greeting && <div className="persona-card__greeting">{draft.greeting}</div>}
              </div>
              {draft.avatar && (
                <button
                  type="button"
                  className="btn btn--sm btn--ghost persona-card__clear-avatar"
                  onClick={() => setDraft({ ...draft, avatar: '' })}
                  title="移除头像"
                >
                  <X size={13} />
                </button>
              )}
            </div>

            <Rows>
              <Row
                label="名字"
                control={
                  <input
                    className="input persona-form__name"
                    value={draft.name}
                    placeholder="Syncore"
                    spellCheck={false}
                    maxLength={20}
                    onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  />
                }
              />
              <Row
                label="定位"
                control={
                  <input
                    className="input persona-form__tagline"
                    value={draft.tagline}
                    placeholder="企业级 AI 工作台"
                    spellCheck={false}
                    maxLength={60}
                    onChange={(e) => setDraft({ ...draft, tagline: e.target.value })}
                  />
                }
              />
              <Row
                label="称呼我"
                title="留空则用「你」"
                control={
                  <input
                    className="input persona-form__address"
                    value={draft.address_me ?? ''}
                    placeholder="你"
                    spellCheck={false}
                    maxLength={20}
                    onChange={(e) => setDraft({ ...draft, address_me: e.target.value })}
                  />
                }
              />
              <Row
                stack
                label="性格标签"
                title={`最多 ${MAX_TRAITS} 个`}
                control={
                  <div className="persona-traits">
                    {traits.map((tr) => (
                      <span key={tr} className="badge badge--accent">
                        {tr}
                        <button
                          type="button"
                          className="persona-traits__x"
                          onClick={() => setDraft({ ...draft, traits: traits.filter((x) => x !== tr) })}
                          title={`移除「${tr}」`}
                        >
                          <X size={10} />
                        </button>
                      </span>
                    ))}
                    {traits.length < MAX_TRAITS && (
                      <span className="persona-traits__entry">
                        <input
                          className="input persona-traits__input"
                          value={traitDraft}
                          maxLength={12}
                          placeholder="专业、可靠、严谨…"
                          onChange={(e) => setTraitDraft(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') {
                              e.preventDefault();
                              addTrait();
                            }
                          }}
                          onBlur={addTrait}
                        />
                        <button
                          type="button"
                          className="btn btn--sm btn--ghost"
                          onClick={addTrait}
                          disabled={!traitDraft.trim()}
                          title="添加标签"
                        >
                          <Plus size={13} />
                        </button>
                      </span>
                    )}
                  </div>
                }
              />
              <Row
                stack
                label="小习惯"
                title={`最多 ${MAX_QUIRKS} 个`}
                control={
                  <div className="persona-traits">
                    {quirks.map((qk) => (
                      <span key={qk} className="badge badge--accent">
                        {qk}
                        <button
                          type="button"
                          className="persona-traits__x"
                          onClick={() => setDraft({ ...draft, quirks: quirks.filter((x) => x !== qk) })}
                          title={`移除「${qk}」`}
                        >
                          <X size={10} />
                        </button>
                      </span>
                    ))}
                    {quirks.length < MAX_QUIRKS && (
                      <span className="persona-traits__entry">
                        <input
                          className="input persona-traits__input"
                          value={quirkDraft}
                          maxLength={16}
                          placeholder="好嘞、慢慢来…"
                          onChange={(e) => setQuirkDraft(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') {
                              e.preventDefault();
                              addQuirk();
                            }
                          }}
                          onBlur={addQuirk}
                        />
                        <button
                          type="button"
                          className="btn btn--sm btn--ghost"
                          onClick={addQuirk}
                          disabled={!quirkDraft.trim()}
                          title="添加小习惯"
                        >
                          <Plus size={13} />
                        </button>
                      </span>
                    )}
                  </div>
                }
              />
              <Row
                label="语气"
                title={personaTones[draft.tone] ?? '回答的整体语气'}
                control={
                  <select
                    className="select"
                    value={draft.tone}
                    onChange={(e) => setDraft({ ...draft, tone: e.target.value })}
                  >
                    {Object.keys(personaTones).length > 0 ? (
                      Object.entries(personaTones).map(([key, desc]) => (
                        <option key={key} value={key}>
                          {key === PERSONA_TONE_CUSTOM ? '自定义' : desc || key}
                        </option>
                      ))
                    ) : (
                      <option value={draft.tone}>{draft.tone}</option>
                    )}
                  </select>
                }
              />
              {isCustomTone && (
                <Row
                  stack
                  label="语气原文"
                  control={
                    <textarea
                      className="persona-form__area"
                      rows={3}
                      value={draft.tone_custom}
                      placeholder="像老朋友聊天，多用短句，偶尔开点无伤大雅的玩笑"
                      onChange={(e) => setDraft({ ...draft, tone_custom: e.target.value })}
                    />
                  }
                />
              )}
              <Row
                stack
                label="开场白"
                title="留空则使用默认开场白"
                control={
                  <textarea
                    className="persona-form__area"
                    rows={2}
                    maxLength={200}
                    value={draft.greeting ?? ''}
                    placeholder="你只管吩咐，剩下的交给我。"
                    onChange={(e) => setDraft({ ...draft, greeting: e.target.value })}
                  />
                }
              />
              <Row
                stack
                label="对话示例"
                title={`最多 ${MAX_EXAMPLES} 条`}
                control={
                  <div className="persona-examples">
                    {examples.map((ex, i) => (
                      <div key={i} className="persona-examples__row">
                        <textarea
                          className="persona-form__area persona-form__area--chat"
                          rows={2}
                          maxLength={200}
                          value={ex}
                          placeholder="好嘞，这就去查～"
                          onChange={(e) => {
                            const next = [...examples];
                            next[i] = e.target.value;
                            setDraft({ ...draft, examples: next });
                          }}
                        />
                        <button
                          type="button"
                          className="btn btn--sm btn--ghost persona-examples__del"
                          onClick={() => setDraft({ ...draft, examples: examples.filter((_, j) => j !== i) })}
                          title="删除这条示例"
                        >
                          <X size={13} />
                        </button>
                      </div>
                    ))}
                    {examples.length < MAX_EXAMPLES && (
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost persona-examples__add"
                        onClick={() => setDraft({ ...draft, examples: [...examples, ''] })}
                      >
                        <Plus size={13} /> 添加示例
                      </button>
                    )}
                  </div>
                }
              />
              <Row
                stack
                label="自由说明"
                control={
                  <textarea
                    className="persona-form__area persona-form__area--tall"
                    rows={4}
                    value={draft.instructions}
                    placeholder="我是 X 餐厅的运营，回答永远优先考虑成本控制…"
                    onChange={(e) => setDraft({ ...draft, instructions: e.target.value })}
                  />
                }
              />
            </Rows>

            </>
        )}
      </Section>

      <Section title="时间">
        <Rows>
          <Row
            label="时间感知"
            control={
              <Switch
                checked={timeAwareness}
                onChange={(v) => draft && setDraft({ ...draft, time_awareness: v })}
                disabled={!draft}
                ariaLabel="时间感知"
              />
            }
          />
        </Rows>
      </Section>

      <Section title="问候">
        {!draft ? (
          <div className="empty-hint">正在读取人设卡…</div>
        ) : (
          <Rows>
            <Row
              label="主动问候"
              control={
                <Switch
                  checked={proactiveOn}
                  onChange={(v) => setDraft({ ...draft, proactive: v ? 'light' : 'off' })}
                  disabled={!draft}
                  ariaLabel="主动问候"
                />
              }
            />
            <Row
              label="免打扰"
              control={
                <span className="persona-time-row">
                  <input
                    type="time"
                    className="input persona-time-row__time"
                    value={draft.quiet_start ?? '22:00'}
                    disabled={!proactiveOn}
                    onChange={(e) =>
                      setDraft({ ...draft, quiet_start: e.target.value || '22:00' })
                    }
                  />
                  <span className="persona-time-row__sep">至</span>
                  <input
                    type="time"
                    className="input persona-time-row__time"
                    value={draft.quiet_end ?? '08:00'}
                    disabled={!proactiveOn}
                    onChange={(e) =>
                      setDraft({ ...draft, quiet_end: e.target.value || '08:00' })
                    }
                  />
                </span>
              }
            />
            <Row
              label="问候间隔"
              control={
                <span className="persona-time-row">
                  <input
                    type="number"
                    className="input persona-time-row__num"
                    min={1}
                    max={168}
                    value={draft.proactive_min_gap_hours ?? 12}
                    disabled={!proactiveOn}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        proactive_min_gap_hours: Math.min(168, Math.max(1, Number(e.target.value) || 1)),
                      })
                    }
                  />
                  <span className="persona-time-row__unit">小时</span>
                </span>
              }
            />
            <Row
              label="每日上限"
              control={
                <span className="persona-time-row">
                  <input
                    type="number"
                    className="input persona-time-row__num"
                    min={1}
                    value={draft.proactive_daily_max ?? 1}
                    disabled={!proactiveOn}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        proactive_daily_max: Math.max(1, Math.trunc(Number(e.target.value) || 1)),
                      })
                    }
                  />
                  <span className="persona-time-row__unit">次</span>
                </span>
              }
            />
          </Rows>
        )}
      </Section>

      <Section title="全局生效">
        <Rows>
          <Row
            label="应用到全部对话"
            control={
              <Switch
                checked={persona?.profile.apply_globally ?? false}
                onChange={(v) => void onToggleGlobal(v)}
                disabled={!persona || savingGlobal}
                ariaLabel="全局生效"
              />
            }
          />
        </Rows>
      </Section>

      <Section title="远程通道">
        <Rows>
          <Row
            label={
              <>
                <QrCode size={13} /> 微信助理
              </>
            }
            desc={boundLine(wechat) || undefined}
            control={
              <>
                <span
                  className={`badge ${wechat?.running ? 'badge--safe' : 'badge--muted'}`}
                  title={
                    wechat?.running
                      ? '微信发来的消息会由这台电脑上的助理回复'
                      : wechat?.bound
                        ? '凭据还在，但通道进程没在跑：重启后端或查看日志'
                        : '绑定不改你的微信'
                  }
                >
                  {wechat?.running ? '已连接' : wechat?.bound ? '未连接' : '未绑定'}
                </span>
                <button
                  type="button"
                  className="btn btn--sm btn--primary"
                  onClick={() => setQrOpen(true)}
                  title={wechat?.bound ? '换微信号或重新授权' : '手机微信扫码连接'}
                >
                  {wechat?.bound ? '重新绑定' : '连接'}
                </button>
              </>
            }
          />
        </Rows>
      </Section>

      <Section title="注入预览">
        <details
          className="fold"
          onToggle={(e) => {
            if (e.currentTarget.open && !fragment && !fragmentLoading) void refreshFragment();
          }}
        >
          <summary className="fold__summary">当前会注入的内容</summary>
          <div className="fold__body">
            {fragmentLoading ? (
              <div className="empty-hint">
                <span className="spinner" /> 正在读取…
              </div>
            ) : fragment ? (
              fragment.empty ? (
                <div className="empty-hint">人设卡为空，当前不注入任何内容</div>
              ) : (
                <div className="persona-preview">
                  <div className="persona-preview__part">
                    <div className="persona-preview__head">
                      稳定块 · {fragment.stable_chars ?? fragment.chars} 字符
                    </div>
                    <pre className="mem-block">{fragment.stable ?? fragment.fragment}</pre>
                  </div>
                  {fragment.dynamic ? (
                    <div className="persona-preview__part">
                      <div className="persona-preview__head">
                        动态块 · {fragment.dynamic_chars ?? fragment.dynamic.length} 字符
                      </div>
                      <pre className="mem-block">{fragment.dynamic}</pre>
                    </div>
                  ) : null}
                </div>
              )
            ) : (
              <div className="empty-hint">展开后读取</div>
            )}
          </div>
        </details>
        <div className="set-foot">
          <span>注入 {fragment?.chars ?? 0} 字符</span>
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => void refreshFragment()}
            disabled={fragmentLoading}
            title="重新读取当前会注入的人设原文"
          >
            <RotateCcw size={13} /> 刷新
          </button>
        </div>
      </Section>

      {error && <div className="alert alert--error">{error}</div>}

      <div className="console__toolbar persona-toolbar">
        {dirty && <span className="badge badge--accent persona-toolbar__state">未保存</span>}
        <button
          type="button"
          className="btn btn--sm btn--ghost"
          onClick={() => void onReset()}
          disabled={saving}
          title="恢复为系统默认人设卡"
        >
          <RotateCcw size={13} /> 恢复默认
        </button>
        {dirty && (
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={onDiscard}
            title="放弃未保存的修改，回到已存版本"
          >
            <X size={13} /> 放弃修改
          </button>
        )}
        <button
          type="button"
          className="btn btn--sm btn--primary"
          onClick={() => void onSave()}
          disabled={saving || !dirty}
          title={dirty ? '保存修改（Ctrl+S）' : '没有未保存的修改'}
        >
          {saving ? <span className="spinner" /> : <Check size={13} />} 保存
        </button>
      </div>

      <WechatQrDialog open={qrOpen} onClose={() => setQrOpen(false)} onBound={refreshChannels} />
    </div>
  );
}

export default AssistantPage;
