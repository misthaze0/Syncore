/**
 * 展示层格式化工具
 *
 * 追踪与评估面板里有大量「毫秒 / 时间戳 / 比率」需要展示，
 * 统一收在这里，避免每个组件各写一遍 `xxx.toFixed(2)`，
 * 也避免同一个数字在不同面板里精度不一致。
 */

/** 毫秒 → 可读耗时（保留 1 位小数，< 1ms 显示更细的精度） */
export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '—';
  if (ms < 1) return `${ms.toFixed(2)}ms`;
  if (ms < 1000) return `${ms.toFixed(1)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

/** epoch ms → 本地时间字符串 */
export function formatTime(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—';
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** epoch ms → 本地日期时间字符串 */
export function formatDateTime(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—';
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${formatTime(ms)}`;
}

/** 相对时间（“3 分钟前”）。用于报告列表，比绝对时间更容易一眼判断新旧。 */
export function formatRelative(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—';
  const diff = Date.now() - ms;
  if (diff < 0) return '刚刚';
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return `${sec} 秒前`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const hour = Math.floor(min / 60);
  if (hour < 24) return `${hour} 小时前`;
  const day = Math.floor(hour / 24);
  if (day < 30) return `${day} 天前`;
  return formatDateTime(ms);
}

/**
 * ISO 时间字符串 → 相对时间。
 *
 * 记忆与索引的接口统一用 ISO 字符串（后端 `now_iso()`），
 * 而追踪那边用的是 epoch ms —— 两套来源不要互相硬转，
 * 这里单独提供一个解析入口，解析失败时原样返回而不是显示「—」，
 * 免得把一个真实的坏值伪装成「没有值」。
 */
export function formatIsoRelative(iso: string | null | undefined): string {
  if (!iso) return '—';
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  return formatRelative(ms);
}

/** 字节数 → 可读体积 */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}
