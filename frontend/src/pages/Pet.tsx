import { useCallback, useEffect, useRef, useState } from 'react';
import { CalendarDays, Check, Coffee, EyeOff, Heart, ListTodo, MessageCircle, Moon, MoreHorizontal, Pause, Play, RotateCcw, Settings2, Sparkles, Timer, Volume2, VolumeX, X } from 'lucide-react';
import type { PetActivity, PetRoute, PetState } from '@/types/desktop';
import { DEFAULT_PET_STATE, formatPetTime, PET_ART } from '@/lib/pet';
import { PET_ACTION_CHOICES, PET_GESTURES, petInteractionBlocked, type PetGesture, type PetGestureName } from '@/lib/petAnimation';
import { useTTSPlayer } from '@/hooks/useTTSPlayer';
import { usePetVoice } from '@/hooks/usePetVoice';
import { useLive2DModel, type PetMouthModel, type PetRigControls } from '@/hooks/useLive2DModel';

type Mood = PetActivity['mood'] | 'happy' | 'sleeping' | 'focused' | 'speaking' | 'listening';
const MOOD_LABELS: Record<Mood, string> = { idle: '待命中', thinking: '正在处理', approval: '等待确认', error: '需要查看主窗', done: '任务已完成', happy: '互动中', sleeping: '空闲', focused: '专注中', speaking: '正在播报', listening: '正在聆听' };
const TOUCH_REACTIONS = {
  head: ['收到。需要我处理什么？', '我在待命，随时可以开始。'],
  body: ['我在。有什么任务要交给我？', '先处理眼前这件事，其余可以稍后安排。'],
  dress: ['收到你的互动。需要打开哪项工作？', '准备好了，随时可以开始。'],
};
const STORAGE_KEY = 'xjms.pet.preview';

export default function Pet() {
  const bridge = window.xjmsDesktop?.pet;
  const [state, setState] = useState<PetState>(DEFAULT_PET_STATE);
  const stateRef = useRef(state);
  stateRef.current = state;
  const [ready, setReady] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [panelMode, setPanelMode] = useState<'companion' | 'gestures'>('companion');
  const [dragging, setDragging] = useState(false);
  const [artError, setArtError] = useState(false);
  const [activity, setActivity] = useState<PetActivity>({ mood: 'idle', text: '', replyId: '' });
  const [bubble, setBubble] = useState<{ text: string; route?: PetRoute } | null>(null);
  const [happy, setHappy] = useState(false);
  const [now, setNow] = useState(Date.now);
  const [heart, setHeart] = useState(0);
  const [gesture, setGesture] = useState<PetGesture | null>(null);
  const lastInteraction = useRef(Date.now());
  const bubbleTimer = useRef<number>();
  const happyTimer = useRef<number>();
  const gestureTimer = useRef<number>();
  const greeted = useRef(false);
  const blockedRef = useRef(false);
  const lastTouch = useRef(-Infinity);
  const touchCount = useRef(0);
  const nextReminder = useRef(0);
  const lastReply = useRef('');
  const imageRef = useRef<HTMLImageElement>(null);
  const mask = useRef<ImageData | null>(null);
  const ignored = useRef<boolean | null>(null);
  const drag = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const suppressClick = useRef(false);
  const moveFrame = useRef<number>();
  const menuRef = useRef<HTMLDivElement>(null);
  const modelRef = useRef<PetMouthModel | null>(null);
  const rigControls = useRef<PetRigControls>({ mood: 'idle', reducedMotion: false, dragging: false, gesture: null });
  const rig = useLive2DModel(modelRef, rigControls);

  const showBubble = useCallback((text: string, duration = 5500, route?: PetRoute) => {
    clearTimeout(bubbleTimer.current);
    setBubble({ text, route });
    bubbleTimer.current = window.setTimeout(() => setBubble(null), duration);
  }, []);

  const tts = useTTSPlayer(state.muteTts || state.quietMode, modelRef, {
    onError: () => showBubble('语音暂时不可用，文字提示会继续显示。'),
  });
  const speechRef = useRef(tts);
  speechRef.current = tts;

  const voice = usePetVoice({
    enabled: ready && state.wakeEnabled && !state.quietMode,
    wakeWord: state.wakeWord,
    ackText: state.wakeAck,
    noticeSpeaking: tts.isPlaying,
    modelRef,
    onBubble: (text, route) => showBubble(text, 9000, route),
  });
  // 「等唤醒」不是回合：只有命中唤醒后的听/想/说才压住日常提示
  const voiceBusyRef = useRef(voice.busy);
  voiceBusyRef.current = voice.busy;

  const playGesture = useCallback((name: PetGestureName) => {
    if (stateRef.current.quietMode) return;
    clearTimeout(gestureTimer.current);
    setGesture({ name, startedAt: performance.now() });
    gestureTimer.current = window.setTimeout(() => setGesture(null), PET_GESTURES[name].duration);
  }, []);

  useEffect(() => {
    if (!ready || rig.status !== 'ready' || greeted.current) return;
    greeted.current = true;
    if (!blockedRef.current && !stateRef.current.focusEndsAt && !stateRef.current.focusRemainingMs) playGesture('greet');
  }, [ready, rig.status, playGesture]);

  const patch = useCallback((delta: Partial<PetState>) => {
    const next = { ...stateRef.current, ...delta };
    stateRef.current = next;
    setState(next);
    if (bridge) bridge.setState(delta);
    else try { localStorage.setItem(STORAGE_KEY, JSON.stringify(next)); } catch { /* Preview may have no storage. */ }
  }, [bridge]);

  useEffect(() => {
    let cancelled = false;
    const off = bridge?.onStateChanged((value) => { stateRef.current = { ...DEFAULT_PET_STATE, ...value }; setState(stateRef.current); });
    const load = async () => {
      try {
        const value = bridge ? await bridge.getState() : JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
        if (!cancelled) { stateRef.current = { ...DEFAULT_PET_STATE, ...value }; setState(stateRef.current); }
      } catch { /* Keep a usable default companion. */ }
      if (!cancelled) setReady(true);
    };
    void load();
    return () => { cancelled = true; off?.(); };
  }, [bridge]);

  useEffect(() => {
    if (!ready) return;
    nextReminder.current = Date.now() + state.reminderMinutes * 60000;
  }, [ready, state.reminderMinutes, state.remindersEnabled, state.quietMode]);

  useEffect(() => {
    if (!ready) return;
    if (!stateRef.current.quietMode) showBubble('我是 Syncore 桌面助手。单击互动，双击打开工作台。', 6000);
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [ready, showBubble]);

  useEffect(() => {
    let cancelled = false;
    let receivedLive = false;
    const update = (value: PetActivity, live: boolean) => {
      if (cancelled || !value) return;
      setActivity(value);
      if (!live) { lastReply.current = value.replyId; return; }
      lastInteraction.current = Date.now();
      // 语音回合有自己的气泡与朗读；日常「处理中/完成」不抢它的画面
      if (voiceBusyRef.current && (value.mood === 'thinking' || value.mood === 'done')) {
        if (value.replyId) lastReply.current = value.replyId;
        return;
      }
      const current = stateRef.current;
      if (value.mood === 'approval') showBubble(value.text, 15000, value.route ?? '/chat');
      else if (value.mood === 'error') showBubble(value.text, 9000, value.route ?? '/chat');
      else if (!current.quietMode && value.mood === 'thinking') showBubble(value.text, 4000);
      else if (!current.quietMode && value.mood === 'done' && (!value.replyId || value.replyId !== lastReply.current)) {
        showBubble(value.text || '任务已完成，可回主窗查看结果。', 12000, value.route ?? '/chat');
        playGesture('cheer');
        speechRef.current.speak(value.text);
      }
      if (value.replyId) lastReply.current = value.replyId;
    };
    const off = bridge?.onActivity((value) => { receivedLive = true; update(value, true); });
    void bridge?.getActivity().then((value) => { if (value && !receivedLive) update(value, false); }).catch(() => {});
    return () => { cancelled = true; off?.(); };
  }, [bridge, showBubble, playGesture]);

  useEffect(() => {
    if (!ready) return;
    if (state.focusEndsAt !== null && state.focusEndsAt <= now) {
      patch({ focusEndsAt: null, focusRemainingMs: null });
      if (!voiceBusyRef.current) {
        const text = '专注时段结束。起身活动五分钟，再继续。';
        showBubble(text, 12000);
        if (!blockedRef.current) playGesture('cheer');
        speechRef.current.speak(text);
      }
      nextReminder.current = now + state.reminderMinutes * 60000;
    }
    if (state.remindersEnabled && nextReminder.current > 0 && now >= nextReminder.current) {
      nextReminder.current = now + state.reminderMinutes * 60000;
      if (!voiceBusyRef.current && !state.quietMode && state.focusEndsAt === null && !state.focusRemainingMs && activity.mood !== 'thinking' && activity.mood !== 'approval') {
        const text = '已连续工作一段时间。起身活动，看看远处，让眼睛休息一下。';
        showBubble(text, 10000);
        if (!blockedRef.current) playGesture('rest');
        speechRef.current.speak(text);
      }
    }
  }, [now, ready, state, patch, showBubble, playGesture, activity.mood]);

  const setThrough = useCallback((value: boolean) => {
    if (ignored.current === value) return;
    ignored.current = value;
    bridge?.setClickThrough(value);
  }, [bridge]);

  const hitCharacter = useCallback((x: number, y: number) => {
    if (rig.status === 'ready') return rig.hitTest(x, y);
    const image = imageRef.current;
    if (!image || artError) return false;
    const bounds = image.getBoundingClientRect();
    if (x < bounds.left || y < bounds.top || x >= bounds.right || y >= bounds.bottom) return false;
    const pixels = mask.current;
    if (!pixels) return true;
    const px = Math.min(pixels.width - 1, Math.floor((x - bounds.left) / bounds.width * pixels.width));
    const py = Math.min(pixels.height - 1, Math.floor((y - bounds.top) / bounds.height * pixels.height));
    return pixels.data[(py * pixels.width + px) * 4 + 3] > 24;
  }, [artError, rig.status, rig.hitTest]);

  const loadMask = useCallback(() => {
    const image = imageRef.current;
    if (!image) return;
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(image.naturalWidth / 4));
    canvas.height = Math.max(1, Math.round(image.naturalHeight / 4));
    const context = canvas.getContext('2d', { willReadFrequently: true });
    try {
      context?.drawImage(image, 0, 0, canvas.width, canvas.height);
      mask.current = context?.getImageData(0, 0, canvas.width, canvas.height) ?? null;
    } catch { mask.current = null; }
  }, []);

  useEffect(() => {
    const move = (event: MouseEvent) => {
      if (drag.current || panelOpen) { setThrough(false); return; }
      const target = document.elementFromPoint(event.clientX, event.clientY);
      setThrough(!target?.closest('[data-pet-interactive]') && !hitCharacter(event.clientX, event.clientY));
    };
    const leave = () => { if (!drag.current && !panelOpen) setThrough(true); };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseleave', leave);
    setThrough(!panelOpen);
    return () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseleave', leave); };
  }, [panelOpen, hitCharacter, setThrough]);

  useEffect(() => {
    bridge?.setPanelOpen(panelOpen);
    if (panelOpen) menuRef.current?.querySelector<HTMLButtonElement>('button')?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPanelOpen(false);
      if (panelOpen && event.key === 'Tab') {
        const controls = [...(menuRef.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])];
        const index = controls.indexOf(document.activeElement as HTMLButtonElement);
        event.preventDefault();
        controls[(index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length]?.focus();
      }
    };
    window.addEventListener('keydown', key);
    const blur = () => setPanelOpen(false);
    window.addEventListener('blur', blur);
    return () => { window.removeEventListener('keydown', key); window.removeEventListener('blur', blur); };
  }, [panelOpen, bridge]);

  const endDrag = useCallback(() => {
    if (!drag.current) return;
    const moved = drag.current.moved;
    suppressClick.current = moved;
    drag.current = null;
    if (moveFrame.current) cancelAnimationFrame(moveFrame.current);
    moveFrame.current = undefined;
    bridge?.dragEnd();
    setDragging(false);
    if (moved && !blockedRef.current) playGesture('settle');
  }, [bridge, playGesture]);

  useEffect(() => {
    window.addEventListener('blur', endDrag);
    return () => {
      window.removeEventListener('blur', endDrag);
      clearTimeout(bubbleTimer.current);
      clearTimeout(happyTimer.current);
      clearTimeout(gestureTimer.current);
      if (moveFrame.current) cancelAnimationFrame(moveFrame.current);
      if (drag.current) bridge?.dragEnd();
    };
  }, [bridge, endDrag]);

  const openMain = useCallback((route: PetRoute = '/assistant') => {
    setPanelOpen(false);
    if (bridge) void bridge.openMain(route).catch(() => showBubble('主窗暂时打不开，请从托盘重试。'));
    else showBubble('这是桌面助手预览，桌面版中可直接打开对应页面。');
  }, [bridge, showBubble]);

  const react = useCallback((region: keyof typeof TOUCH_REACTIONS = 'head') => {
    if (blockedRef.current || stateRef.current.quietMode || performance.now() - lastTouch.current < 700) return;
    lastTouch.current = performance.now();
    lastInteraction.current = Date.now();
    setHeart((value) => value + 1);
    setHappy(true);
    clearTimeout(happyTimer.current);
    happyTimer.current = window.setTimeout(() => setHappy(false), 2600);
    playGesture(region === 'head' ? 'affection' : region === 'dress' ? 'curious' : 'greet');
    const lines = TOUCH_REACTIONS[region];
    showBubble(lines[touchCount.current++ % lines.length]);
  }, [showBubble, playGesture]);

  const performAction = (choice: typeof PET_ACTION_CHOICES[number]) => {
    if (blockedRef.current || state.quietMode) return;
    setPanelOpen(false);
    clearTimeout(happyTimer.current);
    setHappy(false);
    lastInteraction.current = Date.now();
    playGesture(choice.name);
    showBubble(choice.text);
  };

  const startFocus = () => {
    const duration = state.focusRemainingMs ?? state.focusMinutes * 60000;
    patch({ focusEndsAt: Date.now() + duration, focusRemainingMs: null });
    lastInteraction.current = Date.now();
    if (!blockedRef.current) playGesture('greet');
    showBubble(`接下来 ${Math.ceil(duration / 60000)} 分钟进入专注模式。`);
  };
  const pauseFocus = () => patch({ focusEndsAt: null, focusRemainingMs: Math.max(1, (state.focusEndsAt ?? now) - Date.now()) });
  const remaining = state.focusEndsAt !== null ? Math.max(0, state.focusEndsAt - now) : state.focusRemainingMs;
  const focusActive = remaining !== null;
  const sleeping = !focusActive && !happy && now - lastInteraction.current >= state.idleTimeoutMs;
  const mood: Mood = voice.state === 'speaking' || voice.speaking ? 'speaking' : voice.state === 'listening' ? 'listening' : voice.state === 'thinking' ? 'thinking' : tts.isPlaying ? 'speaking' : activity.mood === 'approval' || activity.mood === 'thinking' || activity.mood === 'error' ? activity.mood : happy ? 'happy' : focusActive ? 'focused' : sleeping || state.quietMode ? 'sleeping' : 'idle';
  const interactionBlocked = petInteractionBlocked(mood);
  blockedRef.current = interactionBlocked;
  const activeGesture = !interactionBlocked && !dragging && !state.quietMode ? gesture : null;
  const statusLabel = activeGesture ? PET_GESTURES[activeGesture.name].label : MOOD_LABELS[mood];
  rigControls.current = { mood, reducedMotion: state.reducedMotion, dragging, gesture: activeGesture };

  useEffect(() => {
    if (!interactionBlocked && !dragging && !state.quietMode) return;
    clearTimeout(gestureTimer.current);
    clearTimeout(happyTimer.current);
    setGesture(null);
    setHappy(false);
  }, [interactionBlocked, dragging, state.quietMode]);

  return (
    <main className={`pet-stage pet-stage--${mood}${dragging ? ' is-dragging' : ''}${state.reducedMotion ? ' reduce-motion' : ''}`} data-gesture={activeGesture?.name ?? 'none'} onContextMenu={(event) => { event.preventDefault(); if (!drag.current) { setPanelMode('companion'); setPanelOpen((value) => !value); } }}>
      {bubble && !panelOpen && (
        <div className="pet-bubble" data-pet-interactive="true" role="status" aria-live="polite">
          <span className="pet-bubble__eyebrow">SYNCORE <span>·</span> 提示</span>
          <button className="pet-bubble__close" aria-label="收起提示" onClick={() => setBubble(null)}><X size={12} /></button>
          {bubble.route ? <button className="pet-bubble__text" onClick={() => openMain(bubble.route)}>{bubble.text}<span className="pet-bubble__link">回主窗查看 ↗</span></button> : <p>{bubble.text}</p>}
        </div>
      )}

      <button className="pet-character" aria-label="Syncore，单击互动，双击打开工作台，按住拖动" title="单击互动 · 双击打开工作台 · 右键更多" draggable={false}
        onPointerDown={(event) => {
          if (event.button !== 0 || (!artError && !hitCharacter(event.clientX, event.clientY))) return;
          event.preventDefault();
          setPanelOpen(false);
          lastInteraction.current = Date.now();
          suppressClick.current = false;
          drag.current = { x: event.screenX, y: event.screenY, moved: false };
          event.currentTarget.setPointerCapture(event.pointerId);
          setThrough(false);
          bridge?.dragStart();
        }}
        onPointerMove={(event) => {
          const current = drag.current;
          if (!current || Math.hypot(event.screenX - current.x, event.screenY - current.y) < 5) return;
          current.moved = true;
          setDragging(true);
          if (!moveFrame.current) moveFrame.current = requestAnimationFrame(() => { moveFrame.current = undefined; bridge?.dragMove(); });
        }}
        onPointerUp={endDrag} onPointerCancel={() => { if (drag.current) drag.current.moved = true; endDrag(); }}
        onLostPointerCapture={endDrag}
        onClick={(event) => {
          if (suppressClick.current && event.detail > 0) { suppressClick.current = false; return; }
          if (event.detail > 0 && !artError && !hitCharacter(event.clientX, event.clientY)) return;
          if (event.detail >= 2) { openMain(); return; }
          const bounds = event.currentTarget.getBoundingClientRect();
          const y = (event.clientY - bounds.top) / bounds.height;
          react(event.detail === 0 || y < .24 ? 'head' : y > .58 ? 'dress' : 'body');
        }}
      >
        {artError ? <span className="pet-art-fallback">S<span>Syncore</span></span> : <img ref={imageRef} className={`pet-art${rig.status === 'ready' ? ' pet-art--covered' : ''}`} src={PET_ART} alt="Syncore 桌面助手形象" draggable={false} onLoad={loadMask} onError={() => setArtError(true)} />}
        <canvas ref={rig.canvasRef} className={`pet-rig${rig.status === 'ready' ? ' is-ready' : ''}`} data-model-status={rig.status} aria-label="Syncore 动态形象" title={rig.status === 'error' ? '动态形象暂不可用，已显示静态形象' : undefined} />
      </button>
      {happy && <span key={heart} className="pet-heart" aria-hidden="true">♥</span>}
      {sleeping && !panelOpen && <span className="pet-sleep" aria-hidden="true">z z Z</span>}

      <button className="pet-status" data-pet-interactive="true" onClick={() => { if (activity.mood === 'approval' || activity.mood === 'error') openMain(activity.route ?? '/chat'); else { setPanelMode('companion'); setPanelOpen((value) => !value); } }} aria-label={`${statusLabel}${focusActive ? `，倒计时 ${formatPetTime(remaining)}` : ''}`}>
        <span className="pet-status__dot" />
        {focusActive ? <><Timer size={11} /> {formatPetTime(remaining)} {state.focusRemainingMs ? '已暂停' : ''}</> : statusLabel}
      </button>

      <nav className="pet-dock" data-pet-interactive="true" aria-label="Syncore 快捷操作">
        {voice.busy && <button aria-label="停止语音对话" title="停止语音对话" onClick={() => bridge?.cancelVoice()}><Pause size={16} /></button>}
        <button aria-label="打开工作台" title="打开主工作区" onClick={() => openMain()}><MessageCircle size={16} /></button>
        <span className="pet-dock__line" />
        <button aria-label={focusActive ? '查看专注计时' : '开始专注'} title="专注计时" className={focusActive ? 'is-on' : ''} onClick={() => { if (focusActive) { setPanelMode('companion'); setPanelOpen(true); } else startFocus(); }}><Timer size={16} /></button>
        <button aria-label="互动一下" title="互动一下" onClick={() => react()}><Heart size={16} /></button>
        <button aria-label="打开快捷动作" title="快捷动作" aria-expanded={panelOpen && panelMode === 'gestures'} onClick={() => { setPanelMode('gestures'); setPanelOpen(!panelOpen || panelMode !== 'gestures'); }}><Sparkles size={16} /></button>
        <button aria-label="打开快捷面板" title="更多功能，也可以右键打开" aria-expanded={panelOpen && panelMode === 'companion'} onClick={() => { setPanelMode('companion'); setPanelOpen(!panelOpen || panelMode !== 'companion'); }}><MoreHorizontal size={18} /></button>
      </nav>

      {panelOpen && <>
        <button className="pet-panel-scrim" aria-label="关闭快捷面板" onClick={() => setPanelOpen(false)} />
        <div ref={menuRef} className="pet-menu" data-pet-interactive="true" role="dialog" aria-modal="true" aria-label={panelMode === 'gestures' ? 'Syncore 快捷动作' : 'Syncore 快捷面板'}>
          <header className="pet-menu__header"><div><span className="pet-menu__brand">SYNCORE</span><h1>{panelMode === 'gestures' ? '快捷动作' : 'Syncore 快捷面板'}</h1></div><div className="pet-menu__tools"><button className="pet-icon-button" aria-label={panelMode === 'gestures' ? '切换快捷面板' : '切换动作面板'} title={panelMode === 'gestures' ? '快捷面板' : '动作面板'} onClick={() => setPanelMode(panelMode === 'gestures' ? 'companion' : 'gestures')}>{panelMode === 'gestures' ? <Timer size={15} /> : <Sparkles size={15} />}</button><button className="pet-icon-button" aria-label="关闭面板" onClick={() => setPanelOpen(false)}><X size={16} /></button></div></header>
          {panelMode === 'gestures' ? <>
            <p className="pet-actions__intro">选择一个动作，快速查看助手状态。</p>
            <div className="pet-actions">{PET_ACTION_CHOICES.map((choice, index) => <button key={choice.name} aria-label={choice.title} disabled={interactionBlocked || state.quietMode} onClick={() => performAction(choice)}><span className="pet-actions__number">0{index + 1}</span><span>{choice.title}<small>{choice.detail}</small></span><Play size={12} /></button>)}</div>
            <p className="pet-actions__hint" role="status">{state.quietMode ? '免打扰中，关闭后可继续操作。' : interactionBlocked ? '正在处理任务，请稍后再试。' : state.reducedMotion ? '已减少动态效果，保留文字与静态表情。' : '动作结束后返回待命状态。'}</p>
          </> : <>
          <div className="pet-focus">
            <div className="pet-focus__head"><span><Timer size={13} /> 专注计时</span><span>{state.focusRemainingMs ? '已暂停' : state.focusEndsAt ? '进行中' : '到点提醒，不打断当前工作。'}</span></div>
            <div className="pet-focus__time">{formatPetTime(remaining ?? state.focusMinutes * 60000)}<span>MIN / SEC</span></div>
            {!focusActive && <div className="pet-focus__presets">{[15, 25, 45].map((minutes) => <button key={minutes} className={state.focusMinutes === minutes ? 'is-selected' : ''} aria-pressed={state.focusMinutes === minutes} onClick={() => patch({ focusMinutes: minutes })}>{minutes} 分钟</button>)}</div>}
            <div className="pet-focus__actions"><button className="pet-primary" onClick={state.focusEndsAt ? pauseFocus : startFocus}>{state.focusEndsAt ? <Pause size={13} /> : <Play size={13} />}{state.focusEndsAt ? '暂停' : state.focusRemainingMs ? '继续专注' : '开始专注'}</button>{focusActive && <button className="pet-secondary" aria-label="结束本次专注" onClick={() => { patch({ focusEndsAt: null, focusRemainingMs: null }); showBubble('计时已结束。随时可以开始新的一段。'); }}><RotateCcw size={13} /> 结束</button>}</div>
          </div>
          <div className="pet-shortcuts"><button onClick={() => openMain('/assistant')}><MessageCircle size={17} /><span>打开工作台</span></button><button onClick={() => openMain('/tasks')}><ListTodo size={17} /><span>查看任务</span></button><button onClick={() => openMain('/schedules')}><CalendarDays size={17} /><span>定时安排</span></button></div>
          <div className="pet-options">
            <button aria-pressed={state.remindersEnabled} onClick={() => patch({ remindersEnabled: !state.remindersEnabled })}><Coffee size={14} /><span>休息提醒<small>每 {state.reminderMinutes} 分钟</small></span><span className={`pet-switch ${state.remindersEnabled ? 'is-on' : ''}`}>{state.remindersEnabled && <Check size={9} />}</span></button>
            <button aria-pressed={state.quietMode} onClick={() => { patch({ quietMode: !state.quietMode }); if (!state.quietMode) { setBubble(null); tts.stop(); } }}><Moon size={14} /><span>免打扰<small>暂停日常提示与语音</small></span><span className={`pet-switch ${state.quietMode ? 'is-on' : ''}`}>{state.quietMode && <Check size={9} />}</span></button>
          </div>
          </>}
          <footer className="pet-menu__footer"><button aria-label={state.muteTts ? '开启语音播报' : '关闭语音播报'} title={state.muteTts ? '开启语音' : '关闭语音'} onClick={() => patch({ muteTts: !state.muteTts })}>{state.muteTts ? <VolumeX size={15} /> : <Volume2 size={15} />}</button><button aria-label="打开桌面助手设置" title="外观与设置" onClick={() => openMain('/settings/pet')}><Settings2 size={15} /></button><span /><button onClick={() => { tts.stop(); if (bridge) bridge.hide(); else { setPanelOpen(false); showBubble('桌面版中可以隐藏，并从托盘唤回。'); } }}><EyeOff size={14} /> 隐藏</button></footer>
        </div>
      </>}
    </main>
  );
}
