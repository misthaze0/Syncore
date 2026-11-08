/**
 * 语音输入 hook —— 点击开始持续聆听，按句切分、说一句转写一句，再点一次结束
 *
 * 状态机：idle → listening →（停止）→ idle，转写请求在后台排队，不阻塞聆听。
 * - 采集用 Web Audio 直接拿 PCM（不用 MediaRecorder：它只能录完整段再解码）
 * - `SentenceSegmenter` 按静音切句，每句立刻上传；结果按顺序追加到草稿
 * - 失败时把 error 交回给调用方（ChatInput 用 title 提示，不打断输入）
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { transcribeAudio } from '@/lib/api';
import { encodeWav, SentenceSegmenter } from '@/lib/audio';

export type VoiceState = 'idle' | 'listening';

interface UseVoiceInputResult {
  state: VoiceState;
  /** 有句子在上传/转写（聆听中也会出现，不算错误） */
  busy: boolean;
  error: string | null;
  start: () => Promise<void>;
  stop: () => void;
  clearError: () => void;
}

interface Capture {
  stream: MediaStream;
  context: AudioContext;
  source: MediaStreamAudioSourceNode;
  processor: ScriptProcessorNode;
  mute: GainNode;
  segmenter: SentenceSegmenter;
}

const FRAME_SIZE = 4096; // ≈85ms @48kHz：VAD 精度与开销的折中

export function useVoiceInput(onTranscribed: (text: string) => void): UseVoiceInputResult {
  const [state, setState] = useState<VoiceState>('idle');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const captureRef = useRef<Capture | null>(null);
  const onTranscribedRef = useRef(onTranscribed);
  onTranscribedRef.current = onTranscribed;

  // 转写按顺序串行：并发请求返回顺序不保证，文字会被拼乱
  const queueRef = useRef<Promise<void>>(Promise.resolve());
  const pendingRef = useRef(0);

  const clearError = useCallback(() => setError(null), []);

  const sendSegment = useCallback((samples: Float32Array) => {
    if (samples.length < 3200) return; // < 0.2s 不值得发一次请求
    pendingRef.current += 1;
    setBusy(true);
    queueRef.current = queueRef.current.then(async () => {
      try {
        const wav = encodeWav(samples, 16000);
        const { text, empty } = await transcribeAudio(new Blob([wav], { type: 'audio/wav' }));
        if (!empty && text.trim()) onTranscribedRef.current(text.trim());
      } catch (e) {
        setError(e instanceof Error ? e.message : '语音识别失败');
      } finally {
        pendingRef.current -= 1;
        if (pendingRef.current === 0) setBusy(false);
      }
    });
  }, []);

  const stop = useCallback(() => {
    const capture = captureRef.current;
    if (!capture) return;
    captureRef.current = null;
    capture.processor.onaudioprocess = null;
    try {
      capture.source.disconnect();
      capture.processor.disconnect();
      capture.mute.disconnect();
    } catch {
      /* 节点可能已被关闭，忽略 */
    }
    // 先把最后半句交出去，再释放麦克风
    capture.segmenter.flush();
    capture.stream.getTracks().forEach((t) => t.stop());
    void capture.context.close();
    setState('idle');
  }, []);

  const start = useCallback(async () => {
    setError(null);
    if (captureRef.current) return;

    if (!navigator.mediaDevices?.getUserMedia) {
      setError('当前浏览器不支持录音（需要 HTTPS 或 localhost）');
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      const context = new AudioContext();
      await context.resume();

      const source = context.createMediaStreamSource(stream);
      const processor = context.createScriptProcessor(FRAME_SIZE, 1, 1);
      const mute = context.createGain();
      mute.gain.value = 0; // 接到扬声器才能让 ScriptProcessor 持续跑，但不能回放麦克风
      const segmenter = new SentenceSegmenter({ onSegment: sendSegment });

      processor.onaudioprocess = (e) => {
        const input = e.inputBuffer;
        let frame = input.getChannelData(0);
        if (input.numberOfChannels > 1) {
          const mixed = new Float32Array(frame.length);
          for (let ch = 0; ch < input.numberOfChannels; ch++) {
            const data = input.getChannelData(ch);
            for (let i = 0; i < mixed.length; i++) mixed[i] += data[i];
          }
          for (let i = 0; i < mixed.length; i++) mixed[i] /= input.numberOfChannels;
          frame = mixed;
        }
        segmenter.push(frame, input.sampleRate);
      };

      source.connect(processor);
      processor.connect(mute);
      mute.connect(context.destination);

      captureRef.current = { stream, context, source, processor, mute, segmenter };
      setState('listening');
    } catch (e) {
      setError(`无法访问麦克风：${e instanceof Error ? e.message : String(e)}`);
      setState('idle');
    }
  }, [sendSegment]);

  // 组件卸载时释放麦克风（不动 setState，避免卸载后更新）
  useEffect(() => {
    return () => {
      const capture = captureRef.current;
      if (!capture) return;
      captureRef.current = null;
      capture.processor.onaudioprocess = null;
      capture.stream.getTracks().forEach((t) => t.stop());
      void capture.context.close();
    };
  }, []);

  return { state, busy, error, start, stop, clearError };
}
