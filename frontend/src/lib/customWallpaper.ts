/**
 * 自定义对话壁纸的图像处理
 *
 * ★ 为什么在浏览器里压，而不是丢给后端
 *
 * 壁纸是个人的界面偏好，不是内容：它不该进知识库、不该被索引，
 * 也不该因为后端没起来就丢掉。压成 dataURL 存 localStorage，
 * 桌面壳（file://）和 Web 两条路共用一套代码。代价是配额只有约 4MB，
 * 所以这里有一条「长边逐级缩一遍」的降质梯子，而不是只卡一个文件大小。
 *
 * ★ 为什么裁切单独算一遍
 *
 * 对话区是横的，手机照片是竖的 —— 直接铺上去 cover 会把人裁没。
 * 上传时给出「按哪个比例裁」比事后调锚点管用，所以比例清单在这里，
 * 弹窗只管画裁切框，像素活儿（canvas 取样）留在这个文件。
 */

/** 支持的入口格式：SVG 故意不收 —— 它按容器拉伸，铺进壁纸层会糊成一片 */
const ACCEPTED_MIME = ['image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/bmp', 'image/avif'];

/** 解码前的字节上限。再大的图浏览器也解得慢，而且 2560 长边根本用不到 */
export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;

/** 裁切/缩放后的长边上限：4K 屏幕铺满够用，再大只是把 localStorage 塞爆 */
const MAX_LONG_EDGE = 2560;

/**
 * 单张图的字节预算（base64 前约 1.5MB）。缩略图与壁纸层共用同一份 dataURL。
 * 图库还有一条「所有加起来」的总预算在 hooks/useWallpaper —— 这里只管这一张压到多大。
 */
const BYTE_BUDGET = 1_500_000;

/** 缩到长边 1280 还压不进预算，就认输 —— 继续缩出来的是马赛克 */
const MIN_LONG_EDGE = 1280;

const SCALE_LADDER = [1, 0.82, 0.66, 0.52, 0.4, 0.3];

export interface WallpaperRatioPreset {
  id: string;
  label: string;
  /** 宽高比；null 表示「原图，不裁」 */
  ratio: number | null;
  note?: string;
}

/**
 * 「对话区大约是横的」这一事实，由这几个比例替用户做决定。
 * 4:3 也留着：那是相机原生比例，硬转 16:9 会丢掉上下各一条。
 */
export const RATIO_PRESETS: WallpaperRatioPreset[] = [
  { id: 'auto', label: '原图', ratio: null, note: '不裁切，按图片自己的比例铺' },
  { id: '16:9', label: '16:9', ratio: 16 / 9, note: '对话区的形状' },
  { id: '21:9', label: '21:9', ratio: 21 / 9 },
  { id: '4:3', label: '4:3', ratio: 4 / 3 },
  { id: '1:1', label: '1:1', ratio: 1 },
  { id: '9:16', label: '9:16', ratio: 9 / 16 },
];

export interface DecodedImage {
  bitmap: ImageBitmap;
  width: number;
  height: number;
  /** 宽高比，横图 > 1 */
  ratio: number;
  hasAlpha: boolean;
  mime: string;
}

export class WallpaperImageError extends Error {}

/** 透明通道检测：有 alpha 就得留 png/webp，否则一律转 jpeg 省一半体积 */
function detectAlpha(bitmap: ImageBitmap): boolean {
  const w = Math.min(160, bitmap.width);
  const h = Math.min(160, bitmap.height);
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  if (!ctx) return false;
  ctx.drawImage(bitmap, 0, 0, w, h);
  const { data } = ctx.getImageData(0, 0, w, h);
  for (let i = 3; i < data.length; i += 4) {
    if (data[i] < 250) return true;
  }
  return false;
}

export async function decodeImageFile(file: File): Promise<DecodedImage> {
  if (!ACCEPTED_MIME.includes(file.type)) {
    throw new WallpaperImageError('只能选 jpg / png / webp / gif / bmp / avif 图片');
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    throw new WallpaperImageError(`图片太大（${formatBytes(file.size)}），换一张 20MB 以内的`);
  }
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    throw new WallpaperImageError('这个文件读不出图片，可能已经损坏');
  }
  if (!bitmap.width || !bitmap.height) {
    bitmap.close();
    throw new WallpaperImageError('这张图片是空的');
  }
  // gif 取到的是首帧；动图铺在背景上会永远停在第一帧，这点得让人知道
  return {
    bitmap,
    width: bitmap.width,
    height: bitmap.height,
    ratio: bitmap.width / bitmap.height,
    hasAlpha: detectAlpha(bitmap),
    mime: file.type,
  };
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** dataURL 的 base64 还原不成精确字节，按 3/4 估 —— 界面上只用来给人一个量级 */
export function estimateDataUrlBytes(dataUrl: string): number {
  return Math.round((dataUrl.length * 3) / 4);
}

function scaleLongEdge(width: number, height: number, scale: number): { w: number; h: number } {
  const long = Math.max(width, height);
  const factor = Math.min(1, (long * scale) / MAX_LONG_EDGE);
  return {
    w: Math.max(1, Math.round(width * factor)),
    h: Math.max(1, Math.round(height * factor)),
  };
}

function encodeWithBudget(
  source: CanvasImageSource,
  sx: number,
  sy: number,
  sw: number,
  sh: number,
  outW: number,
  outH: number,
  hasAlpha: boolean,
): string {
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new WallpaperImageError('这个浏览器不支持画布，用不了自定义壁纸');
  canvas.width = outW;
  canvas.height = outH;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(source, sx, sy, sw, sh, 0, 0, outW, outH);

  const formats = hasAlpha ? ['image/webp', 'image/png'] : ['image/webp', 'image/jpeg'];
  for (const format of formats) {
    for (const scale of SCALE_LADDER) {
      const target = scaleLongEdge(outW, outH, scale);
      if (target.w !== outW || target.h !== outH) {
        canvas.width = target.w;
        canvas.height = target.h;
        ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(source, sx, sy, sw, sh, 0, 0, target.w, target.h);
      }
      const quality = format === 'image/jpeg' ? 0.86 : 0.9;
      const url = canvas.toDataURL(format, quality);
      if (estimateDataUrlBytes(url) <= BYTE_BUDGET) return url;
    }
  }
  // 梯子走完还超：最后按最低像素底线压一次，能进预算就留，进不了就报错
  const floor = scaleLongEdge(outW, outH, MIN_LONG_EDGE / MAX_LONG_EDGE);
  canvas.width = floor.w;
  canvas.height = floor.h;
  ctx.drawImage(source, sx, sy, sw, sh, 0, 0, floor.w, floor.h);
  const url = canvas.toDataURL(formats[formats.length - 1], 0.72);
  if (estimateDataUrlBytes(url) <= BYTE_BUDGET) return url;
  throw new WallpaperImageError(
    `这张图压到 ${MIN_LONG_EDGE} 边还装不下（${formatBytes(estimateDataUrlBytes(url))}），换一张小一点的`,
  );
}

/** 不裁切，只按长边上限与体积预算重编码 */
export function encodeWholeImage(img: DecodedImage): string {
  const factor = Math.min(1, MAX_LONG_EDGE / Math.max(img.width, img.height));
  const w = Math.max(1, Math.round(img.width * factor));
  const h = Math.max(1, Math.round(img.height * factor));
  return encodeWithBudget(img.bitmap, 0, 0, img.width, img.height, w, h, img.hasAlpha);
}

/**
 * 按目标比例裁一块出来，输出长边不超过 MAX_LONG_EDGE。
 * rect 用源图像素表示，越界由这里兜住 —— 弹窗那侧的约束只保证「看得见」，
 * 浮点误差可能把取样框推出边缘半像素，canvas 会静默画成透明。
 */
export function encodeCrop(
  img: DecodedImage,
  rect: { sx: number; sy: number; sw: number; sh: number },
): string {
  const sw = Math.max(1, Math.min(img.width - rect.sx, rect.sw));
  const sh = Math.max(1, Math.min(img.height - rect.sy, rect.sh));
  const factor = Math.min(1, MAX_LONG_EDGE / Math.max(sw, sh));
  const w = Math.max(1, Math.round(sw * factor));
  const h = Math.max(1, Math.round(sh * factor));
  return encodeWithBudget(img.bitmap, rect.sx, rect.sy, sw, sh, w, h, img.hasAlpha);
}
