/**
 * 自定义壁纸上传弹窗
 *
 * ★ 它解决的是「比例不对」，不是「好不好看」
 *
 * 对话区是横的一条（约 16:9），而手机里翻出来的照片多半是竖的。
 * 直接塞进去只有两种下场：cover 裁掉大半、contain 留两边空白 ——
 * 都不该由代码替用户猜。所以这里给一排比例让他挑，挑完立刻看得见
 * 「会保留多少」，而不是靠一个百分比数字。
 *
 * 选「原图」时不裁，只压体积，铺法交给下面两档 —— 竖图默认「完整」，
 * 否则 cover 会当场把人像上下各切一条。
 *
 * ★ 舞台即最终画面
 * 舞台的尺寸就是裁切框，看到的像素就是导出的像素：拖动 = 挪裁切框，
 * 滚轮/滑块 = 缩放。不做「整图 + 一个蒙版方框」那种预览，因为它看不出裁完长什么样。
 *
 * ★ 一次选多张时，同一个弹窗被复用
 * 面板按队列把 file 一张张喂进来，所以这里的「取消」是跳过这一张，不是放弃整批；
 * 只有排在最后的那张才说「加入并设为背景」。
 */

import { useEffect, useRef, useState, type CSSProperties } from 'react';

import { X } from '@/components/icons';
import type { CustomFit } from '@/hooks/useWallpaper';
import {
  RATIO_PRESETS,
  decodeImageFile,
  encodeCrop,
  encodeWholeImage,
  estimateDataUrlBytes,
  formatBytes,
  type DecodedImage,
} from '@/lib/customWallpaper';

/** 舞台高度：够看清构图，又不逼着弹窗加高 */
const STAGE_H = 216;
/** 竖比例下的最低舞台高度：再矮就看不清构图了 */
const MIN_STAGE_H = 150;
const MIN_ZOOM = 1;
const MAX_ZOOM = 4;

const FIT_OPTIONS: Array<{ value: CustomFit; label: string; hint: string }> = [
  { value: 'cover', label: '铺满', hint: '撑满对话区，多出来的部分裁掉' },
  { value: 'contain', label: '完整', hint: '整张图都看得见，四周留白' },
];

export interface CustomWallpaperResult {
  dataUrl: string;
  bytes: number;
  fit: CustomFit;
  /** 出图后的宽高比，用来判断「要不要提一句留白」 */
  ratio: number;
  cropped: boolean;
}

/**
 * 对话区是「视口去掉侧栏、顶栏、输入卡片」后剩下的那一块。
 * 这里按同样的算法估一遍，而不是去量 ChatPage 的真实盒子 ——
 * 设置弹窗挡在外面时那张图层量不到，且消息数一变高度就抖。
 * 只要别差太远，16:9 就够准了。
 */
function chatBoxRatio(): number {
  const w = Math.max(640, window.innerWidth - 280);
  const h = Math.max(320, window.innerHeight * 0.62);
  return w / h;
}

function ratioLabel(ratio: number): string {
  return `约 ${ratio.toFixed(2)}:1`;
}

interface CustomWallpaperDialogProps {
  file: File;
  /** 这次多选里还剩几张排在后面；0 表示就这一张 */
  remaining: number;
  /** null = 用户没选成（取消 / Esc / 点遮罩）—— 多张时是「跳过这一张」，不是中止整批 */
  onResolve: (result: CustomWallpaperResult | null) => void;
}

export function CustomWallpaperDialog({ file, remaining, onResolve }: CustomWallpaperDialogProps) {
  const [img, setImg] = useState<DecodedImage | null>(null);
  const [previewUrl, setPreviewUrl] = useState('');
  const [error, setError] = useState('');
  const [ratioId, setRatioId] = useState('16:9');
  const [fit, setFit] = useState<CustomFit>('cover');
  const [zoom, setZoom] = useState(MIN_ZOOM);
  const [offset, setOffset] = useState({ x: 0.5, y: 0.5 });
  /** 可用宽度 = 弹窗正文的内宽；舞台尺寸由它推导，不去量自己（量自己是异步的，会慢一帧） */
  const [availW, setAvailW] = useState(0);
  const [busy, setBusy] = useState(false);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const drag = useRef<{ px: number; py: number; ox: number; oy: number } | null>(null);

  // 解码放在弹窗里做：失败就有地方把原因写在原地，而不是弹一条抓不住的 toast
  useEffect(() => {
    let alive = true;
    let decoded: DecodedImage | null = null;
    // 多选时同一个弹窗会换着 file 跑一遍：上一张的失败原因、取景框和「处理中」都得清掉，
    // 否则 busy 一直挂着，第二张的按钮永远点不动。
    setError('');
    setBusy(false);
    setZoom(MIN_ZOOM);
    setOffset({ x: 0.5, y: 0.5 });
    const url = URL.createObjectURL(file);
    setPreviewUrl(url);
    decodeImageFile(file)
      .then((result) => {
        if (!alive) {
          result.bitmap.close();
          return;
        }
        decoded = result;
        setImg(result);
        // 竖图默认「原图 + 完整」：默认 cover 会当场把人像上下各切一条
        setRatioId(result.ratio < 1 ? 'auto' : '16:9');
        setFit(result.ratio < 1 ? 'contain' : 'cover');
      })
      .catch((e: unknown) => {
        if (alive) {
          setError(e instanceof Error ? e.message : '这张图片读不出来');
        }
      });
    return () => {
      alive = false;
      URL.revokeObjectURL(url);
      decoded?.bitmap.close();
    };
  }, [file]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onResolve(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onResolve]);

  useEffect(() => {
    const node = bodyRef.current;
    if (!node || typeof ResizeObserver === 'undefined') return;
    const measure = () => setAvailW(Math.round(node.clientWidth));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(node);
    return () => ro.disconnect();
  }, []);

  const preset = RATIO_PRESETS.find((p) => p.id === ratioId) ?? RATIO_PRESETS[0];
  const targetRatio = img ? (preset.ratio ?? img.ratio) : 16 / 9;
  const cropping = preset.ratio !== null;
  const boxRatio = chatBoxRatio();
  // 高度定死最直观，但竖比例不能让它顶穿弹窗：超出可用宽度就整体降高度
  const room = availW || STAGE_H * targetRatio;
  const stageH = Math.round(Math.max(MIN_STAGE_H, Math.min(STAGE_H, room / targetRatio)));
  const stageW = Math.round(stageH * targetRatio);
  const stageStyle = { width: stageW, height: stageH } as CSSProperties;

  // cover 语义：先保证画布铺满舞台，再乘用户的缩放
  const baseScale = img ? Math.max((stageW - 2) / img.width, stageH / img.height) : 1;
  const scale = baseScale * zoom;
  const scaledW = img ? img.width * scale : 0;
  const scaledH = img ? img.height * scale : 0;
  const spanX = Math.max(0, scaledW - (stageW - 2));
  const spanY = Math.max(0, scaledH - stageH);
  // offset 0 = 画布左/上沿对齐舞台，1 = 右/下沿对齐：预览与裁切框共用这一个定义
  const bgX = -spanX * offset.x;
  const bgY = -spanY * offset.y;

  const crop = (() => {
    if (!img) return null;
    const sw = Math.min(img.width, (stageW - 2) / scale);
    const sh = Math.min(img.height, stageH / scale);
    const sx = Math.min(Math.max(0, -bgX / scale), Math.max(0, img.width - sw));
    const sy = Math.min(Math.max(0, -bgY / scale), Math.max(0, img.height - sh));
    return { rect: { sx, sy, sw, sh }, kept: (sw * sh) / (img.width * img.height) };
  })();

  const apply = () => {
    if (!img || busy) return;
    setBusy(true);
    try {
      const dataUrl =
        cropping && crop ? encodeCrop(img, crop.rect) : encodeWholeImage(img);
      onResolve({
        dataUrl,
        bytes: estimateDataUrlBytes(dataUrl),
        fit,
        ratio: cropping ? targetRatio : img.ratio,
        cropped: cropping,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : '这张图片没能处理');
      setBusy(false);
    }
  };

  const cancel = () => onResolve(null);

  return (
    <div className="modal-mask" onMouseDown={(e) => e.target === e.currentTarget && cancel()}>
      <div className="modal wpupload" role="dialog" aria-modal="true" aria-label="自定义背景">
        <div className="modal__head">
          <div className="modal__title">用这张图做背景</div>
          <div className="modal__subtitle">
            {img
              ? `${file.name} · 原图 ${img.width}×${img.height}（${formatBytes(file.size)}）· 对话区${ratioLabel(boxRatio)}`
              : error || '正在读这张图…'}
            {remaining > 0 && ` · 后面还有 ${remaining} 张`}
          </div>
          <button type="button" className="wpupload__close" onClick={cancel} title="取消 (Esc)">
            <X size={15} />
          </button>
        </div>

        <div ref={bodyRef} className="modal__body wpupload__body">
          {error ? (
            <p className="wpupload__error">{error}</p>
          ) : (
            <>
              <div className="wpupload__label">出图比例</div>
              <div className="wpupload__ratios" role="radiogroup" aria-label="出图比例">
                {RATIO_PRESETS.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    role="radio"
                    aria-checked={p.id === ratioId}
                    className={`wpupload__ratio${p.id === ratioId ? ' is-on' : ''}`}
                    disabled={!img}
                    title={p.note}
                    onClick={() => {
                      setRatioId(p.id);
                      setZoom(MIN_ZOOM);
                      setOffset({ x: 0.5, y: 0.5 });
                      // 裁出来的图就是按目标比例切的，配「完整」只剩留白 —— 铺法跟着走
                      if (p.ratio !== null) setFit('cover');
                    }}
                  >
                    <span
                      className="wpupload__ratio-shape"
                      style={{ aspectRatio: String(p.ratio ?? img?.ratio ?? 16 / 9) }}
                    />
                    <span className="wpupload__ratio-label">{p.label}</span>
                  </button>
                ))}
              </div>

              <div className="wpupload__stage-wrap">
                <div
                  className={`wpupload__stage${img ? ' is-live' : ''}`}
                  style={stageStyle}
                  title={img ? '拖动调整位置，滚轮缩放' : undefined}
                  onPointerDown={(e) => {
                    if (!img) return;
                    // 捕获只是让拖出舞台后还能继续跟手，失败不该打断拖动本身
                    try {
                      e.currentTarget.setPointerCapture(e.pointerId);
                    } catch {
                      /* 合成事件 / 旧浏览器没有这个指针 */
                    }
                    drag.current = { px: e.clientX, py: e.clientY, ox: offset.x, oy: offset.y };
                  }}
                  onPointerMove={(e) => {
                    const d = drag.current;
                    if (!d || !img) return;
                    setOffset({
                      x: Math.min(1, Math.max(0, d.ox + (e.clientX - d.px) / Math.max(1, spanX))),
                      y: Math.min(1, Math.max(0, d.oy + (e.clientY - d.py) / Math.max(1, spanY))),
                    });
                  }}
                  onPointerUp={(e) => {
                    if (drag.current) {
                      drag.current = null;
                      try {
                        e.currentTarget.releasePointerCapture(e.pointerId);
                      } catch {
                        /* 同上 */
                      }
                    }
                  }}
                  onPointerCancel={() => {
                    drag.current = null;
                  }}
                  onWheel={(e) => {
                    if (!img) return;
                    const next = zoom * (e.deltaY > 0 ? 0.92 : 1.08);
                    setZoom(Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Number(next.toFixed(2)))));
                  }}
                >
                  {img && (
                    <img
                      className="wpupload__img"
                      src={previewUrl}
                      alt=""
                      draggable={false}
                      style={{
                        width: scaledW,
                        height: scaledH,
                        left: bgX,
                        top: bgY,
                      }}
                    />
                  )}
                </div>
                <div className="wpupload__stage-foot">
                  <span className="wpupload__stage-tip">
                    {img ? (cropping ? `保留约 ${Math.round((crop?.kept ?? 0) * 100)}%` : '不裁切') : '载入中…'}
                  </span>
                  <label className="wpupload__zoom">
                    <span>缩放</span>
                    <input
                      type="range"
                      className="wp-range"
                      min={MIN_ZOOM}
                      max={MAX_ZOOM}
                      step={0.05}
                      value={zoom}
                      disabled={!img}
                      aria-label="缩放"
                      onChange={(e) => setZoom(Number(e.target.value))}
                    />
                    <span className="wp-range__value">{zoom.toFixed(2)}×</span>
                  </label>
                </div>
              </div>

              <div className="wpupload__row">
                <div className="wpupload__label">对话区填充方式</div>
                <div className="seg" role="radiogroup" aria-label="填充方式">
                  {FIT_OPTIONS.map((o) => (
                    <button
                      key={o.value}
                      type="button"
                      className={`seg__btn${fit === o.value ? ' seg__btn--on' : ''}`}
                      aria-pressed={fit === o.value}
                      title={o.hint}
                      onClick={() => setFit(o.value)}
                    >
                      {o.label}
                    </button>
                  ))}
                </div>
              </div>

              {fit === 'contain' && targetRatio < boxRatio - 0.1 && (
                <div className="wpupload__row">
                  <span
                    className="badge badge--accent"
                    title="竖图配「完整」左右会留白；想铺满就换「铺满」，或把出图比例换成更宽的"
                  >
                    左右会留白
                  </span>
                </div>
              )}
              {fit === 'contain' && targetRatio > boxRatio + 0.1 && (
                <div className="wpupload__row">
                  <span
                    className="badge badge--accent"
                    title="横图配「完整」上下会留白；想铺满就换「铺满」，或把出图比例换成 16:9"
                  >
                    上下会留白
                  </span>
                </div>
              )}
            </>
          )}
        </div>

        <div className="modal__foot">
          <button type="button" className="btn" onClick={cancel} disabled={busy}>
            {remaining > 0 ? '跳过这张' : error ? '换一张' : '取消'}
          </button>
          {!error && (
            <button type="button" className="btn btn--primary" onClick={apply} disabled={!img || busy}>
              {busy ? '处理中…' : remaining > 0 ? '加入并继续' : '加入并设为背景'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
