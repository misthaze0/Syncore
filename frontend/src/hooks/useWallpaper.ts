/**
 * 对话区壁纸 —— 只有用户上传的图，可以是多张
 *
 * ★ 为什么浓度是连续值、且直接就是不透明度
 *
 * 滑块说 60% 就是 60%，中间不藏乘数 —— 一旦按主题偷偷封顶，控件上那个数就
 * 开始说谎：用户拧到底发现「怎么还是淡的」，比没有滑块更糟。
 * 浅色主题下浓度高了确实会读不动，那是用户在试自己的边界，不该由代码代答。
 *
 * ★ 为什么存在 localStorage 而不是后端
 *
 * 用户自己传的图没有「构建期」这一步，只能编成 dataURL 存进 localStorage ——
 * 桌面壳（file://）与 Web 两条路都吃这一套，不需要后端、不需要新接口、
 * 重启后端也不丢。
 *
 * ★ 为什么每张图自带铺法与锚点
 *
 * 一张是竖屏手机照、一张是 4K 横图、还有一张是纹理小图 —— 「这一张该怎么铺」
 * 属于这一张。切换壁纸时沿用上一张的设置，只会让人以为「没换干净」。
 *
 * ★ 为什么除了单图预算还要有一条总预算
 *
 * 一个源的 localStorage 只有几 MB，而一张图压完是 0.2–1.5MB（base64 后更长）。
 * 单图的体积闸在 lib/customWallpaper，这里再加一条图库级的闸：超了直接拒收并
 * 把原因说清楚，而不是等浏览器抛 QuotaExceededError 后界面显示一张其实没存住的图。
 */

import { create } from 'zustand';

const SELECTED_KEY = 'xjms.wallpaper';
const STRENGTH_KEY = 'xjms.wallpaperStrength';
const IDS_KEY = 'xjms.wallpaperIds';
const IMAGE_KEY_PREFIX = 'xjms.wallpaperImage.';
const META_KEY_PREFIX = 'xjms.wallpaperMeta.';

/** 改版前全应用只有一张自定义图，旧键得搬进图库 */
const LEGACY_IMAGE_KEY = 'xjms.wallpaperCustomImage';
const LEGACY_FIT_KEY = 'xjms.wallpaperCustomFit';
const LEGACY_ANCHOR_KEY = 'xjms.wallpaperCustomAnchor';

/** 上一版内置壁纸的 id：它们不再是合法选项，选中了就退回「无」 */
const RETIRED_IDS = ['cathedral', 'roses', 'custom'];

/** 图库上限：再多也不是「挑一张」而是翻相册了 */
export const MAX_WALLPAPER_ITEMS = 12;

/** 所有 dataURL 的字符总和上限。留了余量给同源的其它数据 */
const TOTAL_CHAR_BUDGET = 3_600_000;

/** 用户上传的图没有「已知构图」，铺法得让他自己挑 */
export type CustomFit = 'cover' | 'contain' | 'repeat';

export const CUSTOM_FIT_OPTIONS: Array<{ value: CustomFit; label: string; hint: string }> = [
  { value: 'cover', label: '铺满', hint: '撑满对话区，多出来的部分裁掉' },
  { value: 'contain', label: '完整', hint: '整张图都看得见，四周留白' },
  { value: 'repeat', label: '平铺', hint: '原尺寸重复铺满，适合纹理小图' },
];

const H_ALIGN = ['left', 'center', 'right'] as const;
const V_ALIGN = ['top', 'center', 'bottom'] as const;
export type HAlign = (typeof H_ALIGN)[number];
export type VAlign = (typeof V_ALIGN)[number];

export interface WallpaperAnchor {
  h: HAlign;
  v: VAlign;
}

export interface WallpaperItem {
  id: string;
  /** 压过的 dataURL，缩略图与壁纸层共用这一份 */
  src: string;
  fit: CustomFit;
  anchor: WallpaperAnchor;
  /** 编码后的字节数（估），只在界面上给个量级 */
  bytes: number;
  /** 卡片上的名字，取原文件名去掉扩展名 */
  name: string;
}

/** 'none' 或者图库里某张图的 id */
export type WallpaperChoice = string;

export interface AddWallpaperInput {
  src: string;
  fit: CustomFit;
  bytes: number;
  name: string;
}

export type AddWallpaperResult =
  | { ok: true; item: WallpaperItem }
  | { ok: false; reason: 'quota' | 'full' };

/** 0–100。默认 25：暗色下发淡、浅色下刚好，两头都不冒犯 */
export const STRENGTH_DEFAULT = 25;

const DEFAULT_ANCHOR: WallpaperAnchor = { h: 'center', v: 'center' };

function clampStrength(n: number): number {
  if (!Number.isFinite(n)) return STRENGTH_DEFAULT;
  return Math.min(100, Math.max(0, Math.round(n)));
}

function isFit(v: unknown): v is CustomFit {
  return v === 'cover' || v === 'contain' || v === 'repeat';
}

function isAnchor(v: unknown): v is WallpaperAnchor {
  if (!v || typeof v !== 'object') return false;
  const { h, v: vv } = v as Record<string, unknown>;
  return H_ALIGN.includes(h as HAlign) && V_ALIGN.includes(vv as VAlign);
}

function lsGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** 普通偏好写失败就退化成「本次会话内有效」；图库的写要单独判，见 addWallpaper */
function lsSet(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* 隐私模式下写不进去 —— 不该因此报错 */
  }
}

function lsRemove(key: string): void {
  try {
    window.localStorage.removeItem(key);
  } catch {
    /* 同上 */
  }
}

function newId(): string {
  return `w${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
}

function readIds(): string[] {
  const raw = lsGet(IDS_KEY);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

function readItem(id: string): WallpaperItem | null {
  const src = lsGet(IMAGE_KEY_PREFIX + id);
  if (!src) return null;
  let meta: Partial<Omit<WallpaperItem, 'id' | 'src'>> = {};
  const raw = lsGet(META_KEY_PREFIX + id);
  if (raw) {
    try {
      meta = JSON.parse(raw);
    } catch {
      meta = {};
    }
  }
  return {
    id,
    src,
    fit: isFit(meta.fit) ? meta.fit : 'cover',
    anchor: isAnchor(meta.anchor) ? meta.anchor : DEFAULT_ANCHOR,
    bytes: typeof meta.bytes === 'number' ? meta.bytes : Math.round((src.length * 3) / 4),
    name: typeof meta.name === 'string' && meta.name ? meta.name : '壁纸',
  };
}

function writeItem(item: WallpaperItem): void {
  lsSet(IMAGE_KEY_PREFIX + item.id, item.src);
  lsSet(
    META_KEY_PREFIX + item.id,
    JSON.stringify({ fit: item.fit, anchor: item.anchor, bytes: item.bytes, name: item.name }),
  );
}

function eraseItem(id: string): void {
  lsRemove(IMAGE_KEY_PREFIX + id);
  lsRemove(META_KEY_PREFIX + id);
}

function loadItems(): WallpaperItem[] {
  const items: WallpaperItem[] = [];
  for (const id of readIds()) {
    const item = readItem(id);
    // 图没了但 id 还列着（被别的清库动作删了一半）：顺手把这一条剔掉
    if (item) items.push(item);
    else eraseItem(id);
  }
  return items;
}

/** 旧版只有一张图：搬进图库，顺便把「选中自定义」翻译成「选中这张新条目」 */
function migrateLegacy(items: WallpaperItem[], selected: string | null): { items: WallpaperItem[]; selected: string } {
  if (typeof window === 'undefined') return { items, selected: 'none' };
  const legacySrc = lsGet(LEGACY_IMAGE_KEY);
  const legacyFit = isFit(lsGet(LEGACY_FIT_KEY)) ? (lsGet(LEGACY_FIT_KEY) as CustomFit) : 'cover';
  const legacyAnchor = (lsGet(LEGACY_ANCHOR_KEY) ?? '').split(' ');
  const h = H_ALIGN.includes(legacyAnchor[0] as HAlign) ? (legacyAnchor[0] as HAlign) : 'center';
  const v = V_ALIGN.includes(legacyAnchor[1] as VAlign) ? (legacyAnchor[1] as VAlign) : 'center';

  if (legacySrc) {
    const item: WallpaperItem = {
      id: newId(),
      src: legacySrc,
      fit: legacyFit,
      anchor: { h, v },
      bytes: Math.round((legacySrc.length * 3) / 4),
      name: '我的壁纸',
    };
    writeItem(item);
    lsSet(IDS_KEY, JSON.stringify([item.id]));
    lsRemove(LEGACY_IMAGE_KEY);
    lsRemove(LEGACY_FIT_KEY);
    lsRemove(LEGACY_ANCHOR_KEY);
    return {
      items: [...items, item],
      selected: selected === 'custom' ? item.id : 'none',
    };
  }
  // 没图但选中值停在旧内置图上：退回「无」
  if (selected && RETIRED_IDS.includes(selected)) return { items, selected: 'none' };
  return { items, selected: selected ?? 'none' };
}

/** 平铺固定从左上角起，其余铺法由锚点决定保留哪一块 */
function positionOf(item: WallpaperItem): string {
  return item.fit === 'repeat' ? '0 0' : `${item.anchor.h} ${item.anchor.v}`;
}

function apply(choice: WallpaperChoice, strength: number, items: WallpaperItem[]): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  root.style.setProperty('--chat-wallpaper-strength', String(strength / 100));
  const item = items.find((w) => w.id === choice);
  if (!item) {
    root.removeAttribute('data-wallpaper');
    root.style.removeProperty('--chat-wallpaper');
    root.style.removeProperty('--chat-wallpaper-position');
    root.style.removeProperty('--chat-wallpaper-size');
    root.style.removeProperty('--chat-wallpaper-repeat');
    return;
  }
  root.setAttribute('data-wallpaper', 'custom');
  root.style.setProperty('--chat-wallpaper', `url("${item.src}")`);
  root.style.setProperty('--chat-wallpaper-position', positionOf(item));
  root.style.setProperty('--chat-wallpaper-size', item.fit === 'repeat' ? 'auto' : item.fit);
  root.style.setProperty('--chat-wallpaper-repeat', item.fit === 'repeat' ? 'repeat' : 'no-repeat');
}

function totalChars(items: WallpaperItem[]): number {
  return items.reduce((sum, item) => sum + item.src.length, 0);
}

interface WallpaperStore {
  items: WallpaperItem[];
  /** 'none' 或 items 里某张的 id */
  wallpaper: WallpaperChoice;
  /** 壁纸不透明度，0–100 */
  strength: number;
  setWallpaper: (wallpaper: WallpaperChoice) => void;
  setStrength: (strength: number) => void;
  /** 存进图库并启用；空间不够或图库满时返回 ok:false，调用方得把这句话还给用户 */
  addWallpaper: (input: AddWallpaperInput) => AddWallpaperResult;
  removeWallpaper: (id: string) => void;
  setItemFit: (id: string, fit: CustomFit) => void;
  setItemAnchor: (id: string, anchor: WallpaperAnchor) => void;
}

function init(): { items: WallpaperItem[]; wallpaper: WallpaperChoice; strength: number } {
  if (typeof window === 'undefined') return { items: [], wallpaper: 'none', strength: STRENGTH_DEFAULT };
  const loaded = loadItems();
  const saved = lsGet(SELECTED_KEY);
  if (saved && loaded.some((item) => item.id === saved)) {
    return { items: loaded, wallpaper: saved, strength: strengthFromStorage() };
  }
  const migrated = migrateLegacy(loaded, saved);
  const raw = lsGet(STRENGTH_KEY);
  return {
    items: migrated.items,
    wallpaper: migrated.selected,
    strength: raw === null ? STRENGTH_DEFAULT : clampStrength(Number(raw)),
  };
}

function strengthFromStorage(): number {
  const raw = lsGet(STRENGTH_KEY);
  return raw === null ? STRENGTH_DEFAULT : clampStrength(Number(raw));
}

export const useWallpaperStore = create<WallpaperStore>((set, get) => {
  const boot = init();
  apply(boot.wallpaper, boot.strength, boot.items);
  lsSet(SELECTED_KEY, boot.wallpaper);

  return {
    items: boot.items,
    wallpaper: boot.wallpaper,
    strength: boot.strength,
    setWallpaper: (wallpaper) => {
      if (wallpaper !== 'none' && !get().items.some((item) => item.id === wallpaper)) return;
      lsSet(SELECTED_KEY, wallpaper);
      apply(wallpaper, get().strength, get().items);
      set({ wallpaper });
    },
    setStrength: (strength) => {
      const next = clampStrength(strength);
      lsSet(STRENGTH_KEY, String(next));
      apply(get().wallpaper, next, get().items);
      set({ strength: next });
    },
    addWallpaper: (input) => {
      const { items } = get();
      if (items.length >= MAX_WALLPAPER_ITEMS) return { ok: false, reason: 'full' };
      if (totalChars(items) + input.src.length > TOTAL_CHAR_BUDGET) return { ok: false, reason: 'quota' };

      const item: WallpaperItem = {
        id: newId(),
        src: input.src,
        fit: isFit(input.fit) ? input.fit : 'cover',
        anchor: DEFAULT_ANCHOR,
        bytes: input.bytes,
        name: input.name || '壁纸',
      };
      const prevIds = lsGet(IDS_KEY) ?? '[]';
      const nextIds = [...items.map((w) => w.id), item.id];
      try {
        window.localStorage.setItem(IMAGE_KEY_PREFIX + item.id, item.src);
        writeItem(item);
        window.localStorage.setItem(IDS_KEY, JSON.stringify(nextIds));
      } catch {
        // 配额真的满了：把写了一半的那张收回去，界面上不能留下一张存不住的图
        eraseItem(item.id);
        lsSet(IDS_KEY, prevIds);
        return { ok: false, reason: 'quota' };
      }
      const nextItems = [...items, item];
      lsSet(SELECTED_KEY, item.id);
      apply(item.id, get().strength, nextItems);
      set({ items: nextItems, wallpaper: item.id });
      return { ok: true, item };
    },
    removeWallpaper: (id) => {
      const { items, wallpaper } = get();
      const nextItems = items.filter((item) => item.id !== id);
      eraseItem(id);
      lsSet(IDS_KEY, JSON.stringify(nextItems.map((item) => item.id)));
      // 铺法与锚点跟着这张一起没了：下一张的比例大概完全不同
      const next = wallpaper === id ? 'none' : wallpaper;
      lsSet(SELECTED_KEY, next);
      apply(next, get().strength, nextItems);
      set({ items: nextItems, wallpaper: next });
    },
    setItemFit: (id, fit) => {
      const nextItems = get().items.map((item) => (item.id === id ? { ...item, fit } : item));
      const target = nextItems.find((item) => item.id === id);
      if (target) writeItem(target);
      apply(get().wallpaper, get().strength, nextItems);
      set({ items: nextItems });
    },
    setItemAnchor: (id, anchor) => {
      const nextItems = get().items.map((item) => (item.id === id ? { ...item, anchor } : item));
      const target = nextItems.find((item) => item.id === id);
      if (target) writeItem(target);
      apply(get().wallpaper, get().strength, nextItems);
      set({ items: nextItems });
    },
  };
});

export function useWallpaper(): {
  wallpaper: WallpaperChoice;
  strength: number;
  setWallpaper: (w: WallpaperChoice) => void;
  setStrength: (n: number) => void;
} {
  return {
    wallpaper: useWallpaperStore((s) => s.wallpaper),
    strength: useWallpaperStore((s) => s.strength),
    setWallpaper: useWallpaperStore((s) => s.setWallpaper),
    setStrength: useWallpaperStore((s) => s.setStrength),
  };
}
