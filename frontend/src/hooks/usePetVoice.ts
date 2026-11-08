/**
 * 桌面助手语音回合：唤醒 → 播回应 → 收一句命令 → 本地转写 → 助理问答 → 逐句朗读 → 回到等唤醒
 *
 * ★ 为什么 KWS 在本地后端而不是这里
 * 唤醒模型由 FastAPI 里的 sherpa-onnx 跑，渲染层只负责采集 16kHz PCM 并通过
 * 本机 WebSocket 推过去；音频不出 127.0.0.1。
 *
 * ★ 为什么唤醒词音频不进入转写
 * 唤醒只是唤醒，不是输入。命中时滚动缓冲直接丢弃，播完回应（用户自定义）
 * 才开始收音，从时间上把唤醒词和命令隔开，不依赖转写后剥词的字符串匹配。
 *
 * 助理历史由服务端 `assistant_main` 持久化；事件通过桌面桥接实时投影到主窗。
 * 请求由桌面助手持有，主窗切页不会打断语音，也不会占用普通对话的流状态。
 */

import { useEffect, useRef, useState } from 'react';

import {
  ASSISTANT_THREAD_ID,
  fetchWakeStatus,
  streamChat,
  transcribeAudio,
  wakeSocketUrl,
} from '@/lib/api';
import { encodeWav, SentenceSegmenter, StreamingResampler } from '@/lib/audio';
import { StreamingSpeechText } from '@/lib/speechText';
import type { AgentEvent } from '@/types';
import type { PetRoute, PetRuntime } from '@/types/desktop';
import { useTTSPlayer } from '@/hooks/useTTSPlayer';

export type PetVoiceState = 'off' | 'waking' | 'acknowledging' | 'listening' | 'thinking' | 'speaking';

export interface PetVoiceController {
  state: PetVoiceState;
  /** 语音回答正在出声（与日常播报的播放器分开） */
  speaking: boolean;
  /** 从命中唤醒到回答结束，期间助理输入框应禁用 */
  busy: boolean;
}

export interface UsePetVoiceOptions {
  /** 桌面助手显示 && 唤醒开关打开 && 未免打扰 */
  enabled: boolean;
  wakeWord: string;
  /** 成功唤醒后的回应，播完才开始听命令 */
  ackText?: string;
  /** 日常播报（提醒/后台任务）正在出声：此时不做唤醒判定 */
  noticeSpeaking: boolean;
  modelRef: React.MutableRefObject<any | null>;
  onBubble: (text: string, route?: PetRoute) => void;
}

interface VoiceTurn {
  id: string;
  published: boolean;
  chatEnded: boolean;
  listenAttempts: number;
  speechFinished: boolean;
  controller: AbortController | null;
  streamDone: boolean;
  sawMessages: boolean;
  messages: string[];
  speechText: StreamingSpeechText;
  tokens: string;
  segmentAt: number;
  transcribedAt: number;
  firstTextAt: number;
  firstAudioAt: number;
  spokenSomething: boolean;
  approvalNotified: boolean;
  deniedNotified: boolean;
  errorText: string;
  waitingTts: boolean;
  /** 唤醒回应正在播放：播完（或播放失败）再进入聆听 */
  ackPending: boolean;
  finishTimer: number | null;
}

interface MicCapture {
  stream: MediaStream;
  context: AudioContext;
  source: MediaStreamAudioSourceNode;
  processor: ScriptProcessorNode;
  mute: GainNode;
}

const DEFAULT_RUNTIME: PetRuntime = {
  permissionMode: '',
  assistantBusy: false,
  webEnabled: true,
  thinkingEnabled: false,
  voiceBusy: false,
};

const FRAME_SIZE = 4096;
const LISTEN_TIMEOUT_MS = 12000;
/** 已开口但一直没收句的上限：maxSegmentMs(20s) + 余量，防止气音后卡在聆听态 */
const LISTEN_HARD_TIMEOUT_MS = 25000;
/** 回应播完后先等一小段，让扬声器回声散掉，避免把回应收进第一句 */
const ACK_GRACE_MS = 400;
const SPEECH_TAIL_MS = 300;
const RETRY_DELAYS = [1000, 2000, 5000];
const WAKE_STATUS_RETRY_MS = 30000;
const READY_TIMEOUT_MS = 20000;
const MAX_SOCKET_BUFFER_BYTES = 16000 * 4; // 超过 1s 就重连，避免迟到的音频唤醒

const READ_ONLY_PROMPT =
  '这一步没有执行，原因和操作记录已放在助理对话里。';
const APPROVAL_PROMPT =
  '这一步需要你确认。我已打开助理对话，请在那里选择允许或拒绝。';

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** 去掉标点空白，只留用于比较的字 */
function normalizeWord(text: string): string {
  return text.replace(/[\s，。！？、,.!?…~～：；;""''「」『』（）()]/g, '');
}

/**
 * 用户可能把唤醒词当成命令再说一遍（测试、口误）。
 * 只有整句以唤醒词开头时才剥掉，避免把正常内容里的同音字误删。
 */
function stripWakeWord(text: string, wakeWord: string): string {
  const word = normalizeWord(wakeWord);
  if (!word) return text.trim();
  const normalized = normalizeWord(text);
  if (!normalized.startsWith(word)) return text.trim();
  let consumed = 0;
  let index = 0;
  while (index < text.length && consumed < word.length) {
    if (!/[\s，。！？、,.!?…~～：；;""''「」『』（）()]/.test(text[index])) consumed += 1;
    index += 1;
  }
  return text
    .slice(index)
    .replace(/^[\s，。！？、,.!?…~～：；;]+/, '')
    .trim();
}

/** 朗读与气泡都用的纯文本：代码块不念，Markdown 标记去掉 */
function textForSpeech(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, ' 代码部分请在主窗查看。')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/[#*_>]+/g, '')
    .trim();
}

export function usePetVoice(options: UsePetVoiceOptions): PetVoiceController {
  const { enabled, wakeWord, ackText = '我在', noticeSpeaking, modelRef, onBubble } = options;
  const bridge = typeof window !== 'undefined' ? window.xjmsDesktop?.pet : undefined;

  const [state, setState] = useState<PetVoiceState>('off');
  const [visible, setVisible] = useState(true);
  const [runtime, setRuntime] = useState<PetRuntime>(DEFAULT_RUNTIME);

  const runtimeRef = useRef(runtime);
  runtimeRef.current = runtime;
  const bubbleRef = useRef(onBubble);
  bubbleRef.current = onBubble;
  const wakeWordRef = useRef(wakeWord);
  wakeWordRef.current = wakeWord;
  const ackTextRef = useRef(ackText);
  ackTextRef.current = ackText;

  const turnRef = useRef<VoiceTurn | null>(null);
  const audioPendingRef = useRef(false);
  const onTtsIdleRef = useRef<() => void>(() => {});
  const onTtsErrorRef = useRef<(message: string) => void>(() => {});

  const tts = useTTSPlayer(false, modelRef, {
    onSpeak: () => {
      const turn = turnRef.current;
      if (!turn?.published || turn.firstAudioAt || !turn.firstTextAt) return;
      turn.firstAudioAt = performance.now();
      // Timings only, never record the microphone audio or conversation text.
      console.info('[voice latency]', {
        stt_ms: Math.round(turn.transcribedAt - turn.segmentAt),
        first_text_ms: Math.round(turn.firstTextAt - turn.transcribedAt),
        text_to_audio_ms: Math.round(turn.firstAudioAt - turn.firstTextAt),
        segment_to_audio_ms: Math.round(turn.firstAudioAt - turn.segmentAt),
      });
    },
    onIdle: () => {
      audioPendingRef.current = false;
      onTtsIdleRef.current();
    },
    onError: (message) => {
      audioPendingRef.current = false;
      onTtsErrorRef.current(message);
    },
  });
  const ttsRef = useRef(tts);
  ttsRef.current = tts;

  useEffect(() => {
    if (!enabled || !visible || !bridge) return;
    return tts.warm(ackText);
  }, [enabled, visible, bridge, ackText, tts.warm]);

  useEffect(() => {
    if (!bridge) return;
    let alive = true;
    let receivedLive = false;
    void bridge
      .getRuntime()
      .then((value) => {
        if (alive && !receivedLive && value) setRuntime(value);
      })
      .catch(() => {});
    const off = bridge.onRuntime((value) => {
      receivedLive = true;
      if (alive) setRuntime(value);
    });
    return () => {
      alive = false;
      off();
    };
  }, [bridge]);

  useEffect(() => {
    if (!bridge) return;
    return bridge.onVisibility((value) => setVisible(value));
  }, [bridge]);

  useEffect(() => {
    if (!enabled || !bridge || !visible || !wakeWord.trim() || runtime.assistantBusy || noticeSpeaking) {
      setState('off');
      return;
    }
    const activeBridge = bridge;

    let active = true;
    let phase: PetVoiceState = 'off';
    let socket: WebSocket | null = null;
    let socketReady = false;
    let capture: MicCapture | null = null;
    let segmenter: SentenceSegmenter | null = null;
    let connectTimer: number | null = null;
    let statusTimer: number | null = null;
    let readyTimer: number | null = null;
    let listenTimer: number | null = null;
    let retryIndex = 0;
    let statusNotified = false;
    let startingCapture = false;
    let ackGraceTimer: number | null = null;
    const kwsResampler = new StreamingResampler(16000);

    const setPhase = (next: PetVoiceState) => {
      phase = next;
      setState(next);
    };

    const bubble = (text: string, route?: PetRoute) => {
      if (active) bubbleRef.current(text, route);
    };

    const isTurn = (turn: VoiceTurn) => active && turnRef.current === turn;

    const setVoiceBusy = (value: boolean) => {
      try {
        activeBridge.setVoiceBusy(value);
      } catch {
        /* 桥接不可用时不影响本地回合 */
      }
    };

    function closeSocket() {
      const current = socket;
      socket = null;
      socketReady = false;
      if (readyTimer !== null) {
        window.clearTimeout(readyTimer);
        readyTimer = null;
      }
      if (!current) return;
      current.onopen = null;
      current.onmessage = null;
      current.onclose = null;
      current.onerror = null;
      try {
        if (current.readyState === WebSocket.OPEN) current.send(JSON.stringify({ type: 'stop' }));
        current.close();
      } catch {
        /* 已经关掉 */
      }
    }

    function stopCapture() {
      const current = capture;
      capture = null;
      segmenter = null;
      if (!current) return;
      current.processor.onaudioprocess = null;
      try {
        current.source.disconnect();
        current.processor.disconnect();
        current.mute.disconnect();
      } catch {
        /* 节点可能已被关闭 */
      }
      current.stream.getTracks().forEach((track) => track.stop());
      void current.context.close().catch(() => {});
      kwsResampler.reset();
    }

    function sendControl(type: 'pause' | 'resume') {
      try {
        if (socket && socket.readyState === WebSocket.OPEN && socketReady) {
          socket.send(JSON.stringify({ type }));
        }
      } catch {
        /* 断开后由重连兜底 */
      }
    }

    function playBeep() {
      const context = capture?.context;
      if (!context) return;
      try {
        const oscillator = context.createOscillator();
        const gain = context.createGain();
        oscillator.type = 'sine';
        oscillator.frequency.value = 880;
        gain.gain.setValueAtTime(0.0001, context.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.06, context.currentTime + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.08);
        oscillator.connect(gain);
        gain.connect(context.destination);
        oscillator.start();
        oscillator.stop(context.currentTime + 0.09);
        oscillator.onended = () => {
          try {
            oscillator.disconnect();
            gain.disconnect();
          } catch {
            /* 已断开 */
          }
        };
      } catch {
        /* 提示音不是关键路径 */
      }
    }

    function say(turn: VoiceTurn, text: string) {
      const clean = textForSpeech(text);
      if (!clean) return;
      turn.spokenSomething = true;
      audioPendingRef.current = true;
      setPhase('speaking');
      ttsRef.current.speak(clean);
    }

    function speakAvailable(turn: VoiceTurn, index: number, text: string, final = false) {
      for (const chunk of turn.speechText.take(index, text, final)) {
        bubble(chunk);
        say(turn, chunk);
      }
    }

    function finishSpeech(turn: VoiceTurn) {
      if (turn.speechFinished) return;
      turn.speechFinished = true;
      if (turn.sawMessages || turn.messages.length) {
        turn.messages.forEach((text, index) => speakAvailable(turn, index, text, true));
        return;
      }
      speakAvailable(turn, 0, turn.tokens, true);
    }

    function scheduleFinish(turn: VoiceTurn) {
      if (!isTurn(turn)) return;
      if (turn.finishTimer !== null) window.clearTimeout(turn.finishTimer);
      turn.finishTimer = window.setTimeout(() => {
        turn.finishTimer = null;
        if (!isTurn(turn)) return;
        // 上一段朗读的 onIdle 可能晚于 done；真正没声了才收回合
        if (audioPendingRef.current) {
          turn.waitingTts = true;
          setPhase('speaking');
          return;
        }
        finishTurn(turn);
      }, SPEECH_TAIL_MS);
    }

    function settleTurn(turn: VoiceTurn) {
      if (!isTurn(turn)) return;
      if (!turn.streamDone) {
        finishSpeech(turn);
        if (!turn.spokenSomething && !turn.errorText) {
          turn.errorText = 'incomplete';
          bubble('这次没有说完，回主窗看看吧。', '/assistant');
          say(turn, '这次没有说完，回主窗看看吧。');
        }
      }
      if (!turn.spokenSomething && !turn.errorText) {
        say(turn, '处理好了，具体内容我放到主窗里了。');
      }
      if (audioPendingRef.current) {
        turn.waitingTts = true;
        setPhase('speaking');
        return;
      }
      scheduleFinish(turn);
    }

    function finishTurn(turn: VoiceTurn) {
      if (turnRef.current !== turn) return;
      turnRef.current = null;
      if (listenTimer !== null) window.clearTimeout(listenTimer);
      listenTimer = null;
      if (ackGraceTimer !== null) {
        window.clearTimeout(ackGraceTimer);
        ackGraceTimer = null;
      }
      if (turn.finishTimer !== null) {
        window.clearTimeout(turn.finishTimer);
        turn.finishTimer = null;
      }
      turn.controller?.abort();
      turn.controller = null;
      if (turn.published && !turn.chatEnded) {
        turn.chatEnded = true;
        activeBridge.publishVoiceUpdate({ type: 'end', id: turn.id });
      }
      setVoiceBusy(false);
      if (!active) {
        setPhase('off');
        return;
      }
      setPhase('waking');
      resumeWaking();
    }

    function handleEvent(turn: VoiceTurn, event: AgentEvent) {
      if (!turn.firstTextAt && ((event.type === 'token' && event.content.trim()) ||
          (event.type === 'assistant_messages' && event.content.some((text) => text.trim())))) {
        turn.firstTextAt = performance.now();
      }
      activeBridge.publishVoiceUpdate({ type: 'event', id: turn.id, event });
      switch (event.type) {
        case 'token':
          turn.tokens += event.content;
          if (event.message_index !== undefined) {
            const index = event.message_index;
            turn.messages[index] = (turn.messages[index] ?? '') + event.content;
            speakAvailable(turn, index, turn.messages[index]);
          } else speakAvailable(turn, 0, turn.tokens);
          break;

        case 'assistant_messages':
          turn.sawMessages = true;
          turn.messages = [...event.content];
          turn.messages.forEach((text, index) => speakAvailable(turn, index, text, index < turn.messages.length - 1));
          break;

        case 'interrupt': {
          bubble(APPROVAL_PROMPT, '/assistant');
          if (!turn.approvalNotified) say(turn, APPROVAL_PROMPT);
          turn.approvalNotified = true;
          void activeBridge.openMain('/assistant');
          break;
        }

        case 'question': {
          const text = '有个细节需要你选择，我已放到助理对话里。';
          bubble(text, '/assistant');
          say(turn, text);
          void activeBridge.openMain('/assistant');
          break;
        }

        case 'tool_result':
          if (event.denied && !turn.approvalNotified && !turn.deniedNotified) {
            turn.deniedNotified = true;
            bubble(READ_ONLY_PROMPT, '/assistant');
            say(turn, READ_ONLY_PROMPT);
          }
          break;

        case 'error':
          if (!turn.errorText) {
            turn.errorText = event.content;
            const text = '刚才没接上，回主窗看看吧。';
            bubble(text, '/assistant');
            say(turn, text);
          }
          break;

        case 'done':
          turn.streamDone = true;
          if (!turn.sawMessages && event.answer) turn.tokens = event.answer;
          finishSpeech(turn);
          break;

        default:
          break;
      }
    }

    async function runChat(turn: VoiceTurn, message: string) {
      if (!isTurn(turn)) return;
      const controller = turn.controller ?? new AbortController();
      turn.controller = controller;
      turn.published = true;
      activeBridge.publishVoiceUpdate({ type: 'start', id: turn.id, message });
      const current = runtimeRef.current;
      try {
        for await (const event of streamChat({
          message,
          threadId: ASSISTANT_THREAD_ID,
          web: current.webEnabled,
          thinking: current.thinkingEnabled,
          permissionMode: current.permissionMode || undefined,
          signal: controller.signal,
        })) {
          if (!isTurn(turn)) return;
          handleEvent(turn, event);
        }
      } catch (error) {
        if (!isTurn(turn)) return;
        if (controller.signal.aborted) return;
        turn.errorText = messageOf(error);
        activeBridge.publishVoiceUpdate({ type: 'event', id: turn.id, event: { type: 'error', content: turn.errorText } });
        const text = '刚才没接上，回主窗看看吧。';
        bubble(text, '/assistant');
        say(turn, text);
      } finally {
        if (!turn.chatEnded) {
          turn.chatEnded = true;
          activeBridge.publishVoiceUpdate({ type: 'end', id: turn.id });
        }
        if (isTurn(turn)) settleTurn(turn);
      }
    }

    async function runCommand(turn: VoiceTurn, samples: Float32Array) {
      if (!isTurn(turn)) return;
      let text = '';
      try {
        const wav = encodeWav(samples, 16000);
        const result = await transcribeAudio(new Blob([wav], { type: 'audio/wav' }), turn.controller?.signal);
        if (!isTurn(turn)) return;
        text = (result.text ?? '').trim();
        turn.transcribedAt = performance.now();
        if (result.empty || !text) {
          retryListening(turn);
          return;
        }
      } catch (error) {
        if (!isTurn(turn)) return;
        bubble(`没听清：${messageOf(error)}`);
        finishTurn(turn);
        return;
      }

      const command = stripWakeWord(text, wakeWord);
      if (!command) {
        retryListening(turn);
        return;
      }
      if (/^(停止|停下|别说了|不用了|取消|结束对话)[。！!\s]*$/.test(command)) {
        bubble('好，需要时再叫我。');
        finishTurn(turn);
        return;
      }
      bubble(`听到：${command}`);
      await runChat(turn, command);
    }

    function beginTurn() {
      if (turnRef.current) return;
      const turn: VoiceTurn = {
        id: crypto.randomUUID(),
        published: false,
        chatEnded: false,
        listenAttempts: 0,
        speechFinished: false,
        controller: new AbortController(),
        streamDone: false,
        sawMessages: false,
        messages: [],
        speechText: new StreamingSpeechText(),
        tokens: '',
        segmentAt: 0,
        transcribedAt: 0,
        firstTextAt: 0,
        firstAudioAt: 0,
        spokenSomething: false,
        approvalNotified: false,
        deniedNotified: false,
        errorText: '',
        waitingTts: false,
        ackPending: false,
        finishTimer: null,
      };
      turnRef.current = turn;
      sendControl('pause');
      kwsResampler.reset();
      setVoiceBusy(true);
      playBeep();
      const ack = (ackTextRef.current ?? '').trim();
      if (!ack) {
        setPhase('listening');
        listenForCommand(turn);
        return;
      }
      turn.ackPending = true;
      setPhase('acknowledging');
      bubble(ack);
      audioPendingRef.current = true;
      ttsRef.current.speak(ack);
    }

    function startListeningAfterAck(turn: VoiceTurn) {
      if (!isTurn(turn)) return;
      turn.ackPending = false;
      setPhase('listening');
      listenForCommand(turn);
    }

    function retryListening(turn: VoiceTurn) {
      if (turn.listenAttempts >= 2) {
        bubble('还是没听清，需要时再叫我。');
        finishTurn(turn);
        return;
      }
      bubble('我在，请说。');
      setPhase('listening');
      listenForCommand(turn);
      void startCapture();
    }

    function listenForCommand(turn: VoiceTurn) {
      turn.listenAttempts += 1;
      listenTimer = window.setTimeout(() => {
        listenTimer = null;
        if (!active || phase !== 'listening' || turnRef.current !== turn) return;
        bubble('没听到声音，我先等着。需要时再叫我。');
        finishTurn(turn);
      }, LISTEN_TIMEOUT_MS);

      segmenter = new SentenceSegmenter({
        onSegment: (samples) => {
          if (!active || phase !== 'listening' || turnRef.current !== turn) return;
          if (listenTimer !== null) {
            window.clearTimeout(listenTimer);
            listenTimer = null;
          }
          setPhase('thinking');
          turn.segmentAt = performance.now();
          window.setTimeout(() => {
            if (!isTurn(turn)) return;
            stopCapture();
            void runCommand(turn, samples);
          }, 0);
        },
        onSpeechStart: () => {
          // 开口后换成长上限：很短的气音可能凑不出 minSegmentMs 而不收句，
          // 只取消等待超时会永远停在聆听态
          if (listenTimer !== null) {
            window.clearTimeout(listenTimer);
            listenTimer = null;
          }
          listenTimer = window.setTimeout(() => {
            listenTimer = null;
            if (!active || phase !== 'listening' || turnRef.current !== turn) return;
            bubble('没听到完整的一句，我先等着。需要时再叫我。');
            finishTurn(turn);
          }, LISTEN_HARD_TIMEOUT_MS);
        },
        startSpeechMs: 160,
        endSilenceMs: 900,
        minSegmentMs: 180,
        maxSegmentMs: 20000,
        prerollMs: 2000,
      });
    }

    function handleWake() {
      if (!active || phase !== 'waking' || turnRef.current) return;
      if (runtimeRef.current.assistantBusy) {
        bubble('上一件事还在处理，等一下再说。');
        sendControl('pause');
        window.setTimeout(() => {
          if (active && phase === 'waking') sendControl('resume');
        }, 600);
        return;
      }
      beginTurn();
    }

    async function startCapture() {
      if (!active || capture || startingCapture) return;
      if (!navigator.mediaDevices?.getUserMedia) {
        bubble('当前环境不能使用麦克风。', '/settings/pet');
        return;
      }
      startingCapture = true;
      let stream: MediaStream | null = null;
      let context: AudioContext | null = null;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
        if (!active || !['waking', 'listening'].includes(phase) || (phase === 'waking' && !socketReady)) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        context = new AudioContext();
        await context.resume();
        if (!active || !['waking', 'listening'].includes(phase)) {
          stream.getTracks().forEach((track) => track.stop());
          void context.close().catch(() => {});
          return;
        }
        const source = context.createMediaStreamSource(stream);
        const processor = context.createScriptProcessor(FRAME_SIZE, 1, 1);
        const mute = context.createGain();
        mute.gain.value = 0;
        processor.onaudioprocess = (event) => {
          if (!active) return;
          const input = event.inputBuffer;
          let frame = input.getChannelData(0);
          if (input.numberOfChannels > 1) {
            const mixed = new Float32Array(frame.length);
            for (let channel = 0; channel < input.numberOfChannels; channel += 1) {
              const data = input.getChannelData(channel);
              for (let index = 0; index < mixed.length; index += 1) mixed[index] += data[index];
            }
            for (let index = 0; index < mixed.length; index += 1) mixed[index] /= input.numberOfChannels;
            frame = mixed;
          }
          if (phase === 'waking') {
            const samples = kwsResampler.process(frame, input.sampleRate);
            if (!samples.length) return;
            if (!socket || !socketReady || socket.readyState !== WebSocket.OPEN) return;
            if (socket.bufferedAmount > MAX_SOCKET_BUFFER_BYTES) {
              closeSocket();
              stopCapture();
              scheduleReconnect();
              return;
            }
            socket.send(Float32Array.from(samples).buffer);
          } else if (phase === 'listening' && segmenter) {
            segmenter.push(frame, input.sampleRate);
          }
        };
        source.connect(processor);
        processor.connect(mute);
        mute.connect(context.destination);
        capture = { stream, context, source, processor, mute };
      } catch (error) {
        stream?.getTracks().forEach((track) => track.stop());
        void context?.close().catch(() => {});
        if (!active) return;
        bubble(`麦克风打不开：${messageOf(error)}`, '/settings/pet');
        setPhase('off');
        closeSocket();
      } finally {
        startingCapture = false;
      }
    }

    function resumeWaking() {
      if (!active) return;
      segmenter = null;
      kwsResampler.reset();
      if (socket && socketReady) {
        sendControl('resume');
        void startCapture();
        return;
      }
      connect();
    }

    function scheduleReconnect() {
      if (!active || connectTimer !== null) return;
      const delay = RETRY_DELAYS[Math.min(retryIndex, RETRY_DELAYS.length - 1)];
      retryIndex += 1;
      connectTimer = window.setTimeout(() => {
        connectTimer = null;
        if (active && phase === 'waking') connect();
      }, delay);
    }

    function statusProblem(reason: string) {
      if (statusNotified) return;
      statusNotified = true;
      bubble(`语音唤醒还没准备好：${reason}`, '/settings/pet');
    }

    function handleSocketError(data: Record<string, unknown>) {
      const code = String(data.code ?? '');
      if (code !== 'unavailable' && code !== 'disabled') return;
      statusProblem(String(data.message ?? '唤醒服务出错'));
      closeSocket();
      stopCapture();
      setPhase('off');
      if (statusTimer === null) statusTimer = window.setTimeout(boot, WAKE_STATUS_RETRY_MS);
    }

    function connect() {
      if (!active || socket) return;
      let next: WebSocket;
      try {
        next = new WebSocket(wakeSocketUrl());
      } catch {
        scheduleReconnect();
        return;
      }
      socket = next;
      socketReady = false;
      readyTimer = window.setTimeout(() => {
        readyTimer = null;
        if (active && socket === next && !socketReady) {
          try {
            next.close();
          } catch {
            /* 已关闭 */
          }
        }
      }, READY_TIMEOUT_MS);
      next.onopen = () => {
        if (!active || socket !== next) return;
        next.send(JSON.stringify({ type: 'start', wake_word: wakeWordRef.current }));
      };
      next.onmessage = (event) => {
        if (!active || socket !== next || typeof event.data !== 'string') return;
        let data: Record<string, unknown>;
        try {
          data = JSON.parse(event.data) as Record<string, unknown>;
        } catch {
          return;
        }
        if (data.type === 'ready') {
          if (readyTimer !== null) {
            window.clearTimeout(readyTimer);
            readyTimer = null;
          }
          socketReady = true;
          retryIndex = 0;
          if (phase === 'waking') void startCapture();
        } else if (data.type === 'wake') {
          handleWake();
        } else if (data.type === 'error') {
          handleSocketError(data);
        }
      };
      next.onclose = () => {
        if (readyTimer !== null) {
          window.clearTimeout(readyTimer);
          readyTimer = null;
        }
        if (socket !== next) return;
        socket = null;
        socketReady = false;
        if (!active) return;
        if (phase === 'waking') {
          stopCapture();
          scheduleReconnect();
        }
      };
      next.onerror = () => {};
    }

    async function boot() {
      if (!active) return;
      if (statusTimer !== null) {
        window.clearTimeout(statusTimer);
        statusTimer = null;
      }
      let info: Awaited<ReturnType<typeof fetchWakeStatus>>;
      try {
        info = await fetchWakeStatus();
      } catch {
        if (!active) return;
        statusProblem('本地服务还没连上');
        statusTimer = window.setTimeout(boot, WAKE_STATUS_RETRY_MS);
        return;
      }
      if (!active) return;
      if (!info.available) {
        statusProblem(info.reason || '本地模型还没就绪');
        statusTimer = window.setTimeout(boot, WAKE_STATUS_RETRY_MS);
        return;
      }
      statusNotified = false;
      setPhase('waking');
      connect();
    }

    const offCancel = activeBridge.onCancelVoice(() => {
      const turn = turnRef.current;
      if (!turn) return;
      turn.controller?.abort();
      ttsRef.current.stop();
      audioPendingRef.current = false;
      bubble('已停止，需要时再叫我。');
      finishTurn(turn);
    });

    function teardown() {
      active = false;
      offCancel();
      if (connectTimer !== null) window.clearTimeout(connectTimer);
      if (statusTimer !== null) window.clearTimeout(statusTimer);
      if (readyTimer !== null) window.clearTimeout(readyTimer);
      if (listenTimer !== null) window.clearTimeout(listenTimer);
      if (ackGraceTimer !== null) {
        window.clearTimeout(ackGraceTimer);
        ackGraceTimer = null;
      }
      connectTimer = null;
      statusTimer = null;
      readyTimer = null;
      listenTimer = null;
      const turn = turnRef.current;
      turnRef.current = null;
      if (turn) {
        if (turn.finishTimer !== null) window.clearTimeout(turn.finishTimer);
        turn.controller?.abort();
        if (turn.published && !turn.chatEnded) {
          turn.chatEnded = true;
          activeBridge.publishVoiceUpdate({ type: 'end', id: turn.id });
        }
      }
      audioPendingRef.current = false;
      onTtsIdleRef.current = () => {};
      onTtsErrorRef.current = () => {};
      setState('off');
      ttsRef.current.stop();
      setVoiceBusy(false);
      closeSocket();
      stopCapture();
    }

    onTtsIdleRef.current = () => {
      const turn = turnRef.current;
      if (!active || !turn) return;
      if (turn.waitingTts) scheduleFinish(turn);
      else if (turn.ackPending) {
        // 回应播完：等扬声器回声散掉再开始听，避免把回应收进第一句
        ackGraceTimer = window.setTimeout(() => {
          ackGraceTimer = null;
          startListeningAfterAck(turn);
        }, ACK_GRACE_MS);
      } else if (phase === 'speaking') setPhase('thinking');
    };
    onTtsErrorRef.current = () => {
      const turn = turnRef.current;
      // 回应播不出来也不能卡死语音回合：直接进入聆听
      if (active && turn?.ackPending) {
        startListeningAfterAck(turn);
        return;
      }
      bubble('语音播放暂时不可用，文字提示会继续。');
    };

    setPhase('waking');
    void boot();

    return teardown;
  }, [enabled, visible, wakeWord, noticeSpeaking, runtime.assistantBusy, bridge]);

  return {
    state,
    speaking: tts.isPlaying,
    busy: state === 'acknowledging' || state === 'listening' || state === 'thinking' || state === 'speaking',
  };
}
