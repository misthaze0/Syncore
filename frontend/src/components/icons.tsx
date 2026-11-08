/**
 * Syncore —— 自定义图标系统
 *
 * 设计规范：
 *   - 24×24 网格，2px 描边（可调），圆角线帽 / 圆角连接
 *   - 全部 currentColor 取色，深浅主题零改动
 *   - API 兼容原 lucide-react：size / className / strokeWidth
 *   - 品牌图标 Wine 单独打磨，酒杯轮廓 + 酒液位
 *
 * 图标分 9 组：导航箭头 / 操作 / 状态反馈 / 文件内容 / AI 助手 /
 * 通信 / 界面 / 杂项 / 实心。共 96 枚，覆盖当前全部用量 + 未来扩展。
 */

import type { CSSProperties, FC, ReactNode } from 'react';

// ── 类型 ─────────────────────────────────────────────

export interface IconProps {
  size?: number | string;
  className?: string;
  strokeWidth?: number;
  fill?: string;
  style?: CSSProperties;
}

/** 与原 lucide-react 的 LucideIcon 用法完全兼容 */
export type IconType = FC<IconProps>;

// ── 工厂 ─────────────────────────────────────────────

function makeIcon(children: ReactNode): IconType {
  const Icon: IconType = ({
    size = 24,
    className,
    strokeWidth = 2,
    fill = 'none',
    style,
  }: IconProps) => (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill={fill}
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      style={style}
      aria-hidden="true"
    >
      {children}
    </svg>
  );
  return Icon;
}

/** 填充型图标（如停止钮、实心标记）—— 用 fill 而非 stroke */
function makeFillIcon(path: string): IconType {
  const Icon: IconType = ({
    size = 24,
    className,
    style,
  }: IconProps) => (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="currentColor"
      className={className}
      style={style}
      aria-hidden="true"
    >
      <path d={path} />
    </svg>
  );
  return Icon;
}

// ══════════════════════════════════════════════════════
//  1. 导航 & 箭头
// ══════════════════════════════════════════════════════

export const ArrowDown = makeIcon(<path d="M12 5v14M19 12l-7 7-7-7" />);
export const ArrowLeft = makeIcon(<path d="M19 12H5M12 19l-7-7 7-7" />);
export const ArrowRight = makeIcon(<path d="M5 12h14M12 5l7 7-7 7" />);
export const ArrowUp = makeIcon(<path d="M12 19V5M19 12l-7-7-7 7" />);

export const ChevronDown = makeIcon(<path d="M6 9l6 6 6-6" />);
export const ChevronRight = makeIcon(<path d="M9 18l6-6-6-6" />);

// ══════════════════════════════════════════════════════
//  2. 操作
// ══════════════════════════════════════════════════════

export const Check = makeIcon(<path d="M20 6L9 17l-5-5" />);
export const X = makeIcon(<path d="M18 6L6 18M6 6l12 12" />);
export const Plus = makeIcon(<path d="M12 5v14M5 12h14" />);

export const Copy = makeIcon(
  <>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15V5a2 2 0 0 1 2-2h10" />
  </>,
);

export const Pencil = makeIcon(
  <>
    <path d="M17 3a2.85 2.85 0 0 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
  </>,
);

export const Trash = makeIcon(
  <>
    <path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
    <path d="M10 11v6M14 11v6" />
  </>,
);

export const Download = makeIcon(
  <>
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <path d="M7 10l5 5 5-5M12 15V3" />
  </>,
);

export const Upload = makeIcon(
  <>
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <path d="M17 8l-5-5-5 5M12 3v12" />
  </>,
);

export const RefreshCw = makeIcon(
  <>
    <path d="M21 12a9 9 0 0 1-9 9c-2.5 0-4.8-1-6.5-2.8L3 21v-5h5" />
    <path d="M3 12a9 9 0 0 1 9-9c2.5 0 4.8 1 6.5 2.8L21 3v5h-5" />
  </>,
);

export const RotateCcw = makeIcon(
  <>
    <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
    <path d="M3 3v5h5" />
  </>,
);

export const Send = makeIcon(
  <>
    <path d="M22 2L11 13" />
    <path d="M22 2l-7 20-4-9-9-4 20-7z" />
  </>,
);

export const ExternalLink = makeIcon(
  <>
    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
    <path d="M15 3h6v6M10 14L21 3" />
  </>,
);

// ══════════════════════════════════════════════════════
//  3. 状态 & 反馈
// ══════════════════════════════════════════════════════

export const CircleCheck = makeIcon(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M9 12l2 2 4-4" />
  </>,
);

export const CircleX = makeIcon(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M9 9l6 6M15 9l-6 6" />
  </>,
);

export const CircleSlash = makeIcon(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M5.6 5.6l12.8 12.8" />
  </>,
);

export const Hourglass = makeIcon(
  <>
    <path d="M6 2h12M6 22h12" />
    <path d="M6 2v4c0 3 3 4 6 6 3 2 6 3 6 6v4" opacity="0" />
    <path d="M6 2c0 3 3 4 6 6 3-2 6-3 6-6M6 22c0-3 3-4 6-6 3 2 6 3 6 6" />
  </>,
);

export const Info = makeIcon(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5M12 8h.01" />
  </>,
);

export const TriangleAlert = makeIcon(
  <>
    <path d="M12 3l9 16H3l9-16z" />
    <path d="M12 10v4M12 17h.01" />
  </>,
);

export const ShieldAlert = makeIcon(
  <>
    <path d="M12 3l8 3v5c0 5-3.5 8-8 10-4.5-2-8-5-8-10V6l8-3z" />
    <path d="M12 8v4M12 16h.01" />
  </>,
);

export const ShieldCheck = makeIcon(
  <>
    <path d="M12 3l8 3v5c0 5-3.5 8-8 10-4.5-2-8-5-8-10V6l8-3z" />
    <path d="M9 12l2 2 4-4" />
  </>,
);

/** 旋转加载圈——配合 .spin 动画使用 */
export const Loader = makeIcon(<path d="M21 12a9 9 0 1 1-6.2-8.56" />);

// ══════════════════════════════════════════════════════
//  4. 文件 & 内容
// ══════════════════════════════════════════════════════

export const FileText = makeIcon(
  <>
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6z" />
    <path d="M14 2v6h6M8 13h8M8 17h8M8 9h2" />
  </>,
);

export const FileX = makeIcon(
  <>
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6z" />
    <path d="M14 2v6h6M10.5 13l3 3M13.5 13l-3 3" />
  </>,
);

export const Folder = makeIcon(
  <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />,
);

export const FolderOpen = makeIcon(
  <>
    <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v1H3V7z" />
    <path d="M3 10h18l-2 8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-8z" />
  </>,
);

export const UploadCloud = makeIcon(
  <>
    <path d="M16 16l-4-4-4 4" />
    <path d="M12 12v9" />
    <path d="M20 16.5A4.5 4.5 0 0 0 17 8.3 6 6 0 0 0 5.3 10 4 4 0 0 0 6 18" />
    <path d="M8 16l4 4 4-4" opacity="0" />
  </>,
);

export const Paperclip = makeIcon(
  <path d="M21 10.5L12 19.5a5 5 0 0 1-7-7l9-9a3.5 3.5 0 0 1 5 5l-9 9a2 2 0 0 1-3-3l8-8" />,
);

export const Image = makeIcon(
  <>
    <rect x="3" y="3" width="18" height="18" rx="2" />
    <circle cx="8.5" cy="8.5" r="1.5" />
    <path d="M21 15l-5-5L5 21" />
  </>,
);

// ── 会话置顶 / 字号：侧栏与顶栏的小工具图标 ──

export const Pin = makeIcon(
  <>
    <path d="M12 17v5" />
    <path d="M9 10.8V4h6v6.8l2 3.2H7l2-3.2z" />
  </>,
);

export const Type = makeIcon(
  <>
    <path d="M4 7V5h16v2" />
    <path d="M12 5v14" />
    <path d="M9 19h6" />
  </>,
);

// ── 交付物图标（「本次产出 N 个文件」按扩展名挑图标） ──

export const FileSpreadsheet = makeIcon(
  <>
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6z" />
    <path d="M14 2v6h6M8 13h8M8 17h8M11 13v8" />
  </>,
);

export const Presentation = makeIcon(
  <>
    <path d="M3 3h18M4 3v11a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V3" />
    <path d="M12 15v3M9 21l3-3 3 3" />
    <path d="M8 11l3-3 2 2 3-4" />
  </>,
);

export const Package = makeIcon(
  <>
    <path d="M21 8l-9-5-9 5v8l9 5 9-5V8z" />
    <path d="M3 8l9 5 9-5M12 13v8" />
  </>,
);

// ══════════════════════════════════════════════════════
//  5. AI & 助手
// ══════════════════════════════════════════════════════

export const Brain = makeIcon(
  <>
    <path d="M12 5a3 3 0 0 0-6 0 3 3 0 0 0-3 3 3 3 0 0 0 1 5 3 3 0 0 0 5 4 3 3 0 0 0 3-1V5z" />
    <path d="M12 5a3 3 0 0 1 6 0 3 3 0 0 1 3 3 3 3 0 0 1-1 5 3 3 0 0 1-5 4 3 3 0 0 1-3-1V5z" />
  </>,
);

export const BookOpen = makeIcon(
  <>
    <path d="M12 6.5C10.5 5 8 4 4 4v14c4 0 6.5 1 8 2.5 1.5-1.5 4-2.5 8-2.5V4c-4 0-6.5 1-8 2.5z" />
    <path d="M12 6.5v14" />
  </>,
);

export const History = makeIcon(
  <>
    <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
    <path d="M3 3v5h5" />
    <path d="M12 7v5l3 3" />
  </>,
);

export const Zap = makeIcon(<path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z" />);

export const Sparkles = makeIcon(
  <>
    <path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z" />
    <path d="M19 15l.7 2L22 17.7 20 18.4 19.3 20.7 18.6 18.4 16.6 17.7 18.6 17 19 15z" />
    <path d="M5 4l.5 1.5L7 6l-1.5.5L5 8l-.5-1.5L3 6l1.5-.5L5 4z" />
  </>,
);

export const Network = makeIcon(
  <>
    <rect x="9" y="2" width="6" height="6" rx="1" />
    <rect x="2" y="16" width="6" height="6" rx="1" />
    <rect x="16" y="16" width="6" height="6" rx="1" />
    <path d="M12 8v4M12 12H5v4M12 12h7v4" />
  </>,
);

export const Database = makeIcon(
  <>
    <ellipse cx="12" cy="5" rx="9" ry="3" />
    <path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5" />
    <path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3" />
  </>,
);

export const Terminal = makeIcon(
  <>
    <rect x="2" y="3" width="20" height="18" rx="2" />
    <path d="M6 8l4 4-4 4M12 16h6" />
  </>,
);

export const Stethoscope = makeIcon(
  <>
    <path d="M4 2v6a4 4 0 0 0 8 0V2" />
    <path d="M6 2v2M10 2v2" />
    <path d="M8 12v2a4 4 0 0 0 8 0v-1a3 3 0 0 1 6 0v1" />
    <circle cx="20" cy="10" r="1" fill="currentColor" stroke="none" />
  </>,
);

export const Wrench = makeIcon(
  <path d="M14.5 6.5a4 4 0 0 0-5.4 5.4L3 18l3 3 6.1-6.1a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-2.4 2.6-2.6z" />,
);

/** 品牌图标——酒杯 + 酒液位 */
export const Wine = makeIcon(
  <>
    {/* 酒杯轮廓：杯口 → 右壁 → 弧底 → 左壁 → 回杯口 */}
    <path d="M5 3h14l-1.5 8a5 5 0 0 1-7 0L5 3z" />
    {/* 酒液位线——比杯口略低，用虚化表达液面 */}
    <path d="M7.3 6.5h9.4" opacity="0.35" />
    {/* 杯柱 */}
    <path d="M12 14v8" />
    {/* 杯座 */}
    <path d="M8 22h8" />
  </>,
);

/** 桌面助手——猫脸，两只耳朵 + 胡须点 */
export const Cat = makeIcon(
  <>
    <path d="M12 5c.67 0 1.35.09 2 .26 1.78-2 5.03-2.84 6.42-2.26 1.4.58-.42 7-.42 7 .57 1.07 1 2.24 1 3.44C21 17.9 16.97 21 12 21s-9-3-9-7.56c0-1.25.5-2.4 1-3.44 0 0-1.89-6.42-.5-7 1.39-.58 4.72.23 6.5 2.23A9.04 9.04 0 0 1 12 5Z" />
    <path d="M8 14v.5" />
    <path d="M16 14v.5" />
    <path d="M11.25 16.25h1.5L12 17l-.75-.75Z" />
  </>,
);

// ══════════════════════════════════════════════════════
//  6. 通信
// ══════════════════════════════════════════════════════

export const MessageSquare = makeIcon(
  <path d="M21 15a2 2 0 0 1-2 2H8l-4 4V5a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2v10z" />,
);

export const QrCode = makeIcon(
  <>
    <rect width="5" height="5" x="3" y="3" rx="1" />
    <rect width="5" height="5" x="16" y="3" rx="1" />
    <rect width="5" height="5" x="3" y="16" rx="1" />
    <path d="M21 16h-3a2 2 0 0 0-2 2v3" />
    <path d="M21 21v.01" />
    <path d="M12 21v-3a2 2 0 0 0-2-2H3" />
    <path d="M12 7h3" />
    <path d="M12 12h.01" />
    <path d="M16 12h.01" />
    <path d="M16 7h.01" />
    <path d="M7 12h.01" />
    <path d="M7 7h.01" />
  </>,
);

export const Bell = makeIcon(
  <>
    <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
    <path d="M13.7 21a2 2 0 0 1-3.4 0" />
  </>,
);

export const Mic = makeIcon(
  <>
    <rect x="9" y="2" width="6" height="11" rx="3" />
    <path d="M5 10a7 7 0 0 0 14 0" />
    <path d="M12 17v5" />
  </>,
);

export const Volume2 = makeIcon(
  <>
    <path d="M11 4L6 8H3v8h3l5 4V4z" />
    <path d="M16 8a4 4 0 0 1 0 8M18.5 5.5a8 8 0 0 1 0 13" />
  </>,
);

// ══════════════════════════════════════════════════════
//  7. 界面
// ══════════════════════════════════════════════════════

export const Settings = makeIcon(
  <>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
  </>,
);

export const Search = makeIcon(
  <>
    <circle cx="11" cy="11" r="7" />
    <path d="M21 21l-4.5-4.5" />
  </>,
);

export const Filter = makeIcon(
  <path d="M3 4h18l-7 8v6l-4 2v-8L3 4z" />,
);

export const Home = makeIcon(
  <>
    <path d="M3 12l9-8 9 8" />
    <path d="M5 10v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V10" />
    <path d="M9 21v-6h6v6" />
  </>,
);

export const Sun = makeIcon(
  <>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </>,
);

export const Moon = makeIcon(<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />);

/** 外观设置页 —— 调色盘 */
export const Palette = makeIcon(
  <>
    <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.93 0 1.65-.75 1.65-1.68 0-.44-.18-.83-.44-1.12-.29-.29-.44-.66-.44-1.13 0-.87.71-1.57 1.58-1.57H16c3.5 0 6-2.5 6-6 0-4.5-4.5-8.5-10-8.5z" />
    <circle cx="7.5" cy="11.5" r="0.9" fill="currentColor" stroke="none" />
    <circle cx="10.5" cy="7.5" r="0.9" fill="currentColor" stroke="none" />
    <circle cx="15" cy="7.5" r="0.9" fill="currentColor" stroke="none" />
    <circle cx="17.5" cy="10.5" r="0.9" fill="currentColor" stroke="none" />
  </>,
);

/** 快捷键设置页 —— 键盘 */
export const Keyboard = makeIcon(
  <>
    <rect x="2" y="4" width="20" height="16" rx="2" />
    <path d="M6 8h.01M10 8h.01M14 8h.01M18 8h.01M8 12h.01M12 12h.01M16 12h.01M7 16h10" />
  </>,
);

export const Eye = makeIcon(
  <>
    <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z" />
    <circle cx="12" cy="12" r="3" />
  </>,
);

export const EyeOff = makeIcon(
  <>
    <path d="M9.9 4.2A10 10 0 0 1 12 4c6.5 0 10 7 10 7a18 18 0 0 1-2.2 3.2M6.1 6.1A18 18 0 0 0 2 11s3.5 7 10 7a10 10 0 0 0 4.9-1.3" />
    <path d="M14.1 14.1a3 3 0 0 1-4.2-4.2M2 2l20 20" />
  </>,
);

export const MoreHorizontal = makeIcon(
  <>
    <circle cx="5" cy="12" r="1" fill="currentColor" stroke="none" />
    <circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" />
    <circle cx="19" cy="12" r="1" fill="currentColor" stroke="none" />
  </>,
);

// ══════════════════════════════════════════════════════
//  8. 杂项
// ══════════════════════════════════════════════════════

export const PanelLeft = makeIcon(
  <>
    <rect x="3" y="3" width="18" height="18" rx="2" />
    <path d="M9 3v18" />
  </>,
);

export const Users = makeIcon(
  <>
    <circle cx="9" cy="8" r="3.5" />
    <path d="M3 21c0-3.5 3-5.5 6-5.5s6 2 6 5.5" />
    <path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 21c0-2.5-1-4.3-2.5-5.3" />
  </>,
);

export const Key = makeIcon(
  <>
    <circle cx="7.5" cy="15.5" r="4.5" />
    <path d="M10.5 12.5L21 2M16 7l3 3M14 9l2 2" />
  </>,
);

export const Clock = makeIcon(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </>,
);

export const Calendar = makeIcon(
  <>
    <rect x="3" y="4" width="18" height="18" rx="2" />
    <path d="M3 10h18M8 2v4M16 2v4" />
  </>,
);

export const Star = makeIcon(
  <path d="M12 2l3 7h7l-5.5 4.5L18 21l-6-4-6 4 1.5-7.5L2 9h7l3-7z" />,
);

export const Heart = makeIcon(
  <path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.6l-1-1a5.5 5.5 0 0 0-7.8 7.8l1 1L12 21l7.8-7.6 1-1a5.5 5.5 0 0 0 0-7.8z" />,
);

export const Globe = makeIcon(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M3 12h18M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18z" />
  </>,
);

export const Layers = makeIcon(
  <>
    <path d="M12 2l9 5-9 5-9-5 9-5z" />
    <path d="M3 12l9 5 9-5M3 17l9 5 9-5" />
  </>,
);

export const Code = makeIcon(
  <>
    <path d="M16 18l4-6-4-6M8 6L4 12l4 6M14 4l-4 16" />
  </>,
);

export const Server = makeIcon(
  <>
    <rect x="3" y="3" width="18" height="7" rx="2" />
    <rect x="3" y="14" width="18" height="7" rx="2" />
    <path d="M7 6.5h.01M7 17.5h.01" />
  </>,
);

export const Flag = makeIcon(
  <>
    <path d="M4 22V4M4 4h13l-2 4 2 4H4" />
  </>,
);

/* ── 设置中心重写（2026-10）新增 ── */
export const Bot = makeIcon(
  <>
    <path d="M12 8V4H8" />
    <rect width="16" height="12" x="4" y="8" rx="2" />
    <path d="M2 14h2M20 14h2M15 13v2M9 13v2" />
  </>,
);
export const FlaskConical = makeIcon(
  <>
    <path d="M10 2v7.53L4.19 19.94A1 1 0 0 0 5.08 21.4h13.84a1 1 0 0 0 .89-1.46L14 9.53V2" />
    <path d="M8.5 2h7M7 16h10" />
  </>,
);
export const Monitor = makeIcon(
  <>
    <rect width="20" height="14" x="2" y="3" rx="2" />
    <path d="M8 21h8M12 17v4" />
  </>,
);
export const Puzzle = makeIcon(
  <path d="M19.4 14a1.9 1.9 0 0 0 0-1.2 2 2 0 0 0-1.2-1.3H16V7.5A1.5 1.5 0 0 0 14.5 6H12V3.8a2 2 0 0 0-1.3-1.2 1.9 1.9 0 0 0-2.4 1.9V6H5.5A1.5 1.5 0 0 0 4 7.5V10h1.7a2 2 0 0 1 1.9 1.4 1.9 1.9 0 0 1-1.1 2.4l-.8.2H4v4.5A1.5 1.5 0 0 0 5.5 20H10v-1.8a2 2 0 0 1 1.4-1.9 1.9 1.9 0 0 1 2.4 1.1l.2.8V20h2.5a1.5 1.5 0 0 0 1.5-1.5V16h2.2a2 2 0 0 0 1.2-2z" />,
);
export const Wand2 = makeIcon(
  <>
    <path d="M21 4L8.5 16.5M9 17l-2 2M3 8l2-2M12 3l1 2M3 15l2 1M17 3l-1 2M19 9l2-1" />
    <path d="m14 10 1.5 1.5L8 19l-1.5-1.5z" />
  </>,
);
export const BarChart = makeIcon(
  <>
    <path d="M3 3v18h18" />
    <rect x="7" y="10" width="3" height="7" rx="0.5" />
    <rect x="12" y="6" width="3" height="11" rx="0.5" />
    <rect x="17" y="13" width="3" height="4" rx="0.5" />
  </>,
);

// ══════════════════════════════════════════════════════
//  9. 实心 / 填充型（状态钮、停止钮等需要实心标记的场景）
// ══════════════════════════════════════════════════════

/** 停止钮——实心方块（在深色圆钮上需要高对比度） */
export const Square = makeFillIcon('M5 5h14a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z');

/** 实心圆点——用于列表标记 / 状态指示 */
export const Dot = makeFillIcon('M12 4a8 8 0 1 1 0 16 8 8 0 0 1 0-16z');

// ══════════════════════════════════════════════════════
//  别名——与原 lucide-react 命名兼容，替换时零改名
// ══════════════════════════════════════════════════════

export { BarChart as BarChart3 };
export { Loader as Loader2 };
export { Trash as Trash2 };

// ══════════════════════════════════════════════════════
//  10. 后补图标（引用回复 / 会话分支 / 语言切换）
// ══════════════════════════════════════════════════════

/** 引用回复——双引号形态（取自 lucide quote 的路径） */
export const Quote = makeIcon(
  <>
    <path d="M16 3a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2 1 1 0 0 1 1 1v1a2 2 0 0 1-2 2 1 1 0 0 0-1 1v2a1 1 0 0 0 1 1 6 6 0 0 0 6-6V5a2 2 0 0 0-2-2z" />
    <path d="M5 3a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2 1 1 0 0 1 1 1v1a2 2 0 0 1-2 2 1 1 0 0 0-1 1v2a1 1 0 0 0 1 1 6 6 0 0 0 6-6V5a2 2 0 0 0-2-2z" />
  </>,
);

/** 会话分支——三节点分叉（取自 lucide git-fork 的路径） */
export const GitFork = makeIcon(
  <>
    <circle cx="12" cy="18" r="3" />
    <circle cx="6" cy="6" r="3" />
    <circle cx="18" cy="6" r="3" />
    <path d="M18 9v2c0 .6-.4 1-1 1H7c-.6 0-1-.4-1-1V9" />
    <path d="M12 12v3" />
  </>,
);

/** 任务清单——多行待办（侧栏任务入口） */
export const ListTodo = makeIcon(
  <>
    <rect x="3" y="5" width="6" height="6" rx="1" />
    <path d="m3 17 2 2 4-4" />
    <path d="M13 6h8" />
    <path d="M13 12h8" />
    <path d="M13 18h8" />
  </>,
);

/** 语言切换——「文A」双字形（取自 lucide languages 的路径） */
export const Languages = makeIcon(
  <>
    <path d="m5 8 6 6" />
    <path d="m4 14 6-6 2-3" />
    <path d="M2 5h12" />
    <path d="M7 2h1" />
    <path d="m22 22-5-10-5 10" />
    <path d="M14 18h6" />
  </>,
);

/** 右侧栏开关——矩形 + 右列高亮（PanelLeft 的镜像，取自 lucide panel-right 的路径） */
export const PanelRight = makeIcon(
  <>
    <rect x="3" y="3" width="18" height="18" rx="2" />
    <path d="M15 3v18" />
  </>,
);

/** 秒表——定时任务侧栏入口（lucide timer 的路径） */
export const Timer = makeIcon(
  <>
    <path d="M10 2h4" />
    <path d="M12 14l3-3" />
    <circle cx="12" cy="14" r="8" />
  </>,
);

/** 播放三角——定时任务的「立即运行一次」（lucide play 的路径） */
export const Play = makeIcon(<path d="M6 3 20 12 6 21Z" />);

/** 兼容原 lucide-react 的 LucideIcon 类型 */
export type LucideIcon = IconType;
