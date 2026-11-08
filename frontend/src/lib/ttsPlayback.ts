import { speechSegments } from './speechText';

type ResponseResult = { response: Response; error?: never } | { response?: never; error: unknown };
interface SpeechJob {
  text: string;
  controller?: AbortController;
  response?: Promise<ResponseResult>;
}
export interface SpeechPlayerOptions {
  request: (text: string, signal: AbortSignal) => Promise<Response>;
  createContext?: () => AudioContext;
  onBusy?: (busy: boolean) => void;
  onSpeak?: (text: string) => void;
  onIdle?: () => void;
  onError?: (message: string) => void;
  onLevel?: (level: number) => void;
}

/** HTTP chunks may split a 16-bit sample between two reads. */
export class Pcm16Decoder {
  private tail: number | null = null;
  decode(input: Uint8Array): Float32Array<ArrayBuffer> {
    let bytes = input;
    if (this.tail !== null) {
      bytes = new Uint8Array(input.length + 1);
      bytes[0] = this.tail;
      bytes.set(input, 1);
    }
    const count = Math.floor(bytes.length / 2);
    const view = new DataView(bytes.buffer, bytes.byteOffset, count * 2);
    const samples = new Float32Array(count);
    for (let i = 0; i < count; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
    this.tail = bytes.length % 2 ? bytes[bytes.length - 1] : null;
    return samples;
  }
  finish() { if (this.tail !== null) throw new Error('语音片段不完整，请再试一次。'); }
}

/** Two synthesis requests at most: current speech and one sentence of lookahead. */
export class StreamingSpeechPlayer {
  private queue: SpeechJob[] = [];
  private current: SpeechJob | null = null;
  private controllers = new Set<AbortController>();
  private sources = new Map<AudioBufferSourceNode, () => void>();
  private context: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private running = false;
  private generation = 0;
  private nextStart = 0;
  private frame: number | null = null;

  constructor(private options: SpeechPlayerOptions) {}

  speak(text: string) {
    this.queue.push(...speechSegments(text).map((part) => ({ text: part })));
    if (!this.queue.length) return;
    this.prefetch();
    if (this.running) return;
    this.running = true;
    this.options.onBusy?.(true);
    void this.run(this.generation);
  }

  /** Warm only the short acknowledgement; backend cache follows model settings. */
  warm(text: string): () => void {
    if (!text.trim() || text.length > 50) return () => {};
    const controller = new AbortController();
    this.controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), 20000);
    void this.options.request(text, controller.signal).then(async (response) => {
      if (!response.ok) return;
      await response.arrayBuffer();
    }).catch(() => {}).finally(() => { clearTimeout(timeout); this.controllers.delete(controller); });
    return () => controller.abort();
  }

  stop() {
    this.generation += 1;
    this.queue = [];
    this.current = null;
    for (const controller of this.controllers) controller.abort();
    this.controllers.clear();
    this.stopSources();
    this.running = false;
    this.options.onBusy?.(false);
    this.stopMeter();
  }

  close() {
    this.stop();
    this.analyser?.disconnect();
    this.analyser = null;
    void this.context?.close().catch(() => {});
    this.context = null;
  }

  private prefetch() {
    let slots = this.current ? 1 : 2;
    for (const job of this.queue) {
      if (slots-- <= 0) break;
      if (job.response) continue;
      const controller = new AbortController();
      job.controller = controller;
      this.controllers.add(controller);
      const timeout = setTimeout(() => controller.abort(), 20000);
      job.response = this.options.request(job.text, controller.signal)
        .then((response): ResponseResult => ({ response }), (error): ResponseResult => ({ error }))
        .finally(() => clearTimeout(timeout));
    }
  }

  private async run(generation: number) {
    try {
      while (this.queue.length && generation === this.generation) {
        const job = this.queue.shift()!;
        this.current = job;
        this.prefetch();
        const result = await job.response!;
        if (generation !== this.generation) return;
        if (result.error || !result.response) throw result.error;
        const response = result.response;
        if (!response.ok) throw new Error(`语音暂时不可用（${response.status}）`);
        this.context ??= this.options.createContext?.() ?? new AudioContext();
        const ctx = this.context;
        if (ctx.state === 'suspended') await ctx.resume();
        if (generation !== this.generation) return;
        this.analyser ??= ctx.createAnalyser();
        this.analyser.fftSize = 256;
        this.analyser.disconnect();
        this.analyser.connect(ctx.destination);
        const endings: Promise<void>[] = [];
        let started = false;
        const play = (buffer: AudioBuffer) => {
          if (generation !== this.generation) return;
          const node = ctx.createBufferSource();
          node.buffer = buffer;
          node.connect(this.analyser!);
          const at = Math.max(ctx.currentTime + 0.06, this.nextStart);
          this.nextStart = at + buffer.duration;
          endings.push(new Promise<void>((resolve) => {
            this.sources.set(node, resolve);
            node.onended = () => { this.sources.delete(node); node.disconnect(); resolve(); };
            node.start(at);
          }));
          if (!started) { started = true; this.options.onSpeak?.(job.text); this.startMeter(); }
        };
        if ((response.headers.get('content-type') ?? '').includes('audio/pcm') && response.body) {
          const decoder = new Pcm16Decoder();
          const reader = response.body.getReader();
          try {
            while (generation === this.generation) {
              const timeout = setTimeout(() => job.controller?.abort(), 20000);
              let result: ReadableStreamReadResult<Uint8Array>;
              try { result = await reader.read(); } finally { clearTimeout(timeout); }
              if (result.done) break;
              const samples = decoder.decode(result.value);
              if (!samples.length) continue;
              const buffer = ctx.createBuffer(1, samples.length, 24000);
              buffer.copyToChannel(samples, 0);
              play(buffer);
            }
            decoder.finish();
          } finally {
            await reader.cancel().catch(() => {});
            reader.releaseLock();
          }
        } else {
          const timeout = setTimeout(() => job.controller?.abort(), 20000);
          let bytes: ArrayBuffer;
          try { bytes = await response.arrayBuffer(); } finally { clearTimeout(timeout); }
          if (generation !== this.generation) return;
          play(await ctx.decodeAudioData(bytes));
        }
        if (generation !== this.generation) return;
        if (!started) throw new Error('语音服务没有返回可播放的音频。');
        await Promise.all(endings);
        if (job.controller) this.controllers.delete(job.controller);
        if (this.current === job) this.current = null;
      }
    } catch (error) {
      if (generation === this.generation) {
        this.queue = [];
        for (const controller of this.controllers) controller.abort();
        this.controllers.clear();
        this.stopSources();
        this.options.onError?.(error instanceof Error ? error.message : '语音暂时不可用');
      }
    } finally {
      if (generation === this.generation) {
        this.current = null;
        this.running = false;
        this.stopMeter();
        this.options.onBusy?.(false);
        this.options.onIdle?.();
      }
    }
  }

  private stopSources() {
    for (const [source, resolve] of this.sources) {
      source.onended = null;
      try { source.stop(); } catch { /* Already ended. */ }
      source.disconnect();
      resolve();
    }
    this.sources.clear();
    this.nextStart = 0;
  }
  private startMeter() {
    if (this.frame !== null || !this.options.onLevel) return;
    const samples = new Uint8Array(this.analyser!.frequencyBinCount);
    const tick = () => {
      this.analyser?.getByteTimeDomainData(samples);
      const sum = samples.reduce((total, value) => total + ((value - 128) / 128) ** 2, 0);
      this.options.onLevel?.(Math.min(1, Math.sqrt(sum / samples.length) * 3.5));
      this.frame = requestAnimationFrame(tick);
    };
    tick();
  }
  private stopMeter() {
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    this.frame = null;
    this.options.onLevel?.(0);
  }
}
