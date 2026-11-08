/**
 * 语音前端工具 —— 实时 PCM 采集 → 按句切分 → 16kHz 单声道 WAV 编码
 *
 * ★ 为什么自己切句而不是录完整段再传
 *
 * 本地 SenseVoice 识别 20 秒音频只要 0.6 秒，真正的等待来自「用户说完还要
 * 再点一次停止」。按静音切句后，每说完一句就转写一句，文字随说随出；
 * 停止键只负责结束这一轮聆听。
 *
 * ★ 为什么上传 WAV 而不是 webm
 *
 * 本地后端只吃 16bit PCM WAV（云端也支持），而浏览器 MediaRecorder 产出 webm。
 * 直接采集 PCM、自己重采样成 16kHz 再拼 WAV，纯前端、零依赖、可复现。
 */

/** 把 Float32 PCM（单声道）写成 16kHz 16bit WAV 的 ArrayBuffer */
export function encodeWav(samples: Float32Array, sampleRate: number): ArrayBuffer {
  const numChannels = 1;
  const bitsPerSample = 16;
  const bytesPerSample = bitsPerSample / 8;
  const blockAlign = numChannels * bytesPerSample;
  const byteRate = sampleRate * blockAlign;
  const dataSize = samples.length * bytesPerSample;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);

  const writeString = (offset: number, str: string) => {
    for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
  };

  // RIFF 头
  writeString(0, 'RIFF');
  view.setUint32(4, 36 + dataSize, true);
  writeString(8, 'WAVE');
  // fmt 块
  writeString(12, 'fmt ');
  view.setUint32(16, 16, true); // fmt 块大小
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, numChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, byteRate, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, bitsPerSample, true);
  // data 块
  writeString(36, 'data');
  view.setUint32(40, dataSize, true);

  let offset = 44;
  for (let i = 0; i < samples.length; i++, offset += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buffer;
}

export interface SentenceSegmenterOptions {
  /** 每切出一句调用一次（16kHz 单声道浮点样本） */
  onSegment: (samples: Float32Array) => void;
  /** 从静音进入「说话中」时调用一次（唤醒后的等待超时据此取消） */
  onSpeechStart?: () => void;
  /** 连续有声多久算开口 */
  startSpeechMs?: number;
  /** 连续静音多久算这句说完 */
  endSilenceMs?: number;
  /** 太短的气音/杂音不当成一句 */
  minSegmentMs?: number;
  /** 兜底：一句话最长多少毫秒，超过就强制切 */
  maxSegmentMs?: number;
  /** 开口前保留的音频，避免吞掉第一个字 */
  prerollMs?: number;
  /** 静音判定下限（自适应噪声底再乘系数） */
  minSpeechRms?: number;
}

/**
 * 流式重采样：吃任意采样率、任意帧长的 PCM，吐出 16kHz 单声道样本。
 *
 * 小数位置跨帧保留，帧边界不会丢样本；语音输入与唤醒复用同一份状态机，
 * 避免两条链路对「16kHz」的理解出现细微差异。
 */
export class StreamingResampler {
  private readonly targetRate: number;
  private lastInput: number | null = null;
  private pendingStep = 1;

  constructor(targetRate = 16000) {
    this.targetRate = targetRate;
  }

  process(frame: Float32Array, sampleRate: number): number[] {
    if (sampleRate === this.targetRate) return Array.from(frame);

    const step = sampleRate / this.targetRate;
    const out: number[] = [];
    let prev = this.lastInput;
    let pending = this.pendingStep;
    for (let i = 0; i < frame.length; i++) {
      const cur = frame[i];
      if (prev === null) {
        prev = cur;
        continue;
      }
      pending -= 1;
      while (pending <= 0) {
        const t = 1 + pending;
        out.push(prev + (cur - prev) * t);
        pending += step;
      }
      prev = cur;
    }
    this.lastInput = prev;
    this.pendingStep = pending;
    return out;
  }

  reset(): void {
    this.lastInput = null;
    this.pendingStep = 1;
  }
}

/**
 * 实时按句切分器：吃任意采样率、任意帧长的 PCM，吐出 16kHz 的一句话。
 *
 * 判定逻辑是「自适应噪声底 + 滞回」：
 * - 静音时用指数平均跟踪环境噪声，阈值 = max(下限, 噪声 × 3)；
 * - 连续有声 startSpeechMs 才进入「说话中」，避免键盘声触发；
 * - 连续静音 endSilenceMs 才收句，逗号级的停顿不会把一句话切成两半。
 */
export class SentenceSegmenter {
  private readonly onSegment: (samples: Float32Array) => void;
  private readonly onSpeechStart?: () => void;
  private readonly targetRate = 16000;
  private readonly startSpeechMs: number;
  private readonly endSilenceMs: number;
  private readonly minSegmentMs: number;
  private readonly maxSegmentMs: number;
  private readonly prerollLimit: number;
  private readonly minSpeechRms: number;
  private readonly resampler = new StreamingResampler(this.targetRate);

  private noise = 0.003;
  private speaking = false;
  private startRunMs = 0;
  private silenceMs = 0;
  private preroll: number[] = [];
  private segment: number[] = [];

  constructor(options: SentenceSegmenterOptions) {
    this.onSegment = options.onSegment;
    this.onSpeechStart = options.onSpeechStart;
    this.startSpeechMs = options.startSpeechMs ?? 160;
    this.endSilenceMs = options.endSilenceMs ?? 650;
    this.minSegmentMs = options.minSegmentMs ?? 200;
    this.maxSegmentMs = options.maxSegmentMs ?? 20000;
    this.prerollLimit = Math.round(((options.prerollMs ?? 300) / 1000) * this.targetRate);
    this.minSpeechRms = options.minSpeechRms ?? 0.01;
  }

  push(frame: Float32Array, sampleRate: number): void {
    const samples = this.resampler.process(frame, sampleRate);
    if (samples.length === 0) return;

    const frameMs = (samples.length / this.targetRate) * 1000;
    let energy = 0;
    for (let i = 0; i < samples.length; i++) energy += samples[i] * samples[i];
    const rms = Math.sqrt(energy / samples.length);
    const threshold = Math.max(this.minSpeechRms, this.noise * 3);

    if (!this.speaking) {
      this.pushPreroll(samples);
      if (rms >= threshold) {
        this.startRunMs += frameMs;
        if (this.startRunMs >= this.startSpeechMs) {
          this.speaking = true;
          this.silenceMs = 0;
          this.segment = [...this.preroll];
          this.onSpeechStart?.();
        }
      } else {
        this.startRunMs = 0;
        // Speech onset must not raise its own threshold before startSpeechMs.
        this.noise = this.noise * 0.95 + rms * 0.05;
      }
      return;
    }

    for (let i = 0; i < samples.length; i++) this.segment.push(samples[i]);
    if (rms >= threshold) {
      this.silenceMs = 0;
    } else {
      this.silenceMs += frameMs;
    }

    const segmentMs = (this.segment.length / this.targetRate) * 1000;
    if (segmentMs >= this.maxSegmentMs) {
      this.emitSegment();
    } else if (this.silenceMs >= this.endSilenceMs) {
      this.emitSegment();
    }
  }

  /**
   * 预置「开口前」的一小段音频当第一句的预卷。
   *
   * 唤醒命中时把唤醒词前后的那截补进来，「你好助手，今天……」连读时
   * 第一个字不会被切掉。回放给检测器，避免用户已经说完后，预卷被静音覆盖。
   */
  prime(samples: ArrayLike<number>): void {
    const list = Float32Array.from(samples);
    const windowSamples = Math.round(this.targetRate * 0.03);
    for (let offset = 0; offset < list.length; offset += windowSamples) {
      this.push(list.subarray(offset, offset + windowSamples), this.targetRate);
    }
  }

  /** 结束聆听：把正在进行的一句也交出去 */
  flush(): void {
    if (this.speaking) this.emitSegment();
    this.preroll = [];
  }

  private emitSegment(): void {
    const samples = this.segment;
    this.speaking = false;
    this.startRunMs = 0;
    this.silenceMs = 0;
    this.segment = [];
    // 收尾的静音正好当下一句的预卷：用户接着说时不会漏掉第一个字
    this.preroll = samples.slice(-this.prerollLimit);
    if (this.voicedMs(samples) >= this.minSegmentMs) {
      this.onSegment(Float32Array.from(samples));
    }
  }

  /**
   * 整段里的真实有声时长（30ms 窗口，超过峰值 25% 算出声）。
   *
   * 不用「录帧时累加阈值以上的帧」：音量起伏会让一部分帧短暂低于绝对阈值，
   * 把时长算少，短句就被当成气音丢掉；相对阈值也不受环境噪声漂移影响。
   */
  private voicedMs(samples: number[]): number {
    const winMs = 30;
    const win = Math.round((winMs / 1000) * this.targetRate);
    const rmsList: number[] = [];
    for (let off = 0; off + win <= samples.length; off += win) {
      let energy = 0;
      for (let i = off; i < off + win; i++) energy += samples[i] * samples[i];
      rmsList.push(Math.sqrt(energy / win));
    }
    let peak = 0;
    for (const rms of rmsList) peak = Math.max(peak, rms);
    const threshold = Math.max(this.minSpeechRms, peak * 0.25);
    let voiced = 0;
    for (const rms of rmsList) if (rms >= threshold) voiced += winMs;
    return voiced;
  }

  private pushPreroll(samples: number[]): void {
    for (let i = 0; i < samples.length; i++) this.preroll.push(samples[i]);
    const overflow = this.preroll.length - this.prerollLimit;
    if (overflow > 0) this.preroll.splice(0, overflow);
  }
}
