/**
 * File renderer registry. Office structures come from the bounded read-only API;
 * passive media uses the conversation-scoped inline endpoint.
 */

/** 画布里的渲染方式 */
export type ArtifactKind = 'html' | 'markdown' | 'csv' | 'svg' | 'code' | 'text' | 'spreadsheet' | 'image' | 'pdf' | 'document' | 'audio' | 'video' | 'archive';

/** 扩展名 → 渲染方式。**不在这里的一律不可预览**（走下载） */
const SUFFIX_KIND: Record<string, ArtifactKind> = {
  // 能直接当页面渲染的结构
  html: 'html',
  htm: 'html',
  svg: 'svg',
  // 排版
  md: 'markdown',
  markdown: 'markdown',
  // 表格
  csv: 'csv',
  tsv: 'csv',
  xlsx: 'spreadsheet',
  xlsm: 'spreadsheet',
  png: 'image', jpg: 'image', jpeg: 'image', gif: 'image', webp: 'image', bmp: 'image', avif: 'image', ico: 'image',
  pdf: 'pdf', docx: 'document', pptx: 'document',
  mp3: 'audio', wav: 'audio', ogg: 'audio', m4a: 'audio', flac: 'audio',
  mp4: 'video', webm: 'video', ogv: 'video', zip: 'archive',
  // 代码 / 结构化文本
  json: 'code',
  jsonl: 'code',
  xml: 'code',
  yaml: 'code',
  yml: 'code',
  toml: 'code',
  ini: 'code',
  css: 'code',
  js: 'code',
  mjs: 'code',
  cjs: 'code',
  jsx: 'code',
  ts: 'code',
  tsx: 'code',
  py: 'code',
  sql: 'code',
  sh: 'code',
  bash: 'code',
  java: 'code',
  go: 'code',
  rs: 'code',
  rb: 'code',
  php: 'code',
  c: 'code',
  h: 'code',
  cpp: 'code',
  hpp: 'code',
  // 纯文本
  txt: 'text',
  log: 'text',
};

/** 渲染方式 → 界面上的中文标签 */
export const ARTIFACT_KIND_LABEL: Record<ArtifactKind, string> = {
  html: '网页',
  markdown: 'Markdown',
  csv: '表格',
  svg: '矢量图',
  code: '代码',
  text: '文本',
  spreadsheet: 'Excel', image: '图片', pdf: 'PDF', document: '文档',
  audio: '音频', video: '视频', archive: '压缩包',
};

/** 取扩展名（小写、不含点）。没有扩展名时返回空串 */
export function suffixOf(path: string): string {
  const name = path.split(/[\\/]/).pop() ?? '';
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
}

/** 文件名（去掉目录） */
export function nameOf(path: string): string {
  return path.split(/[\\/]/).pop() ?? path;
}

/**
 * 这个文件能不能进画布。不能则返回 `null`（前端渲染成「只能下载」）。
 */
export function artifactKind(path: string): ArtifactKind | null {
  return SUFFIX_KIND[suffixOf(path)] ?? null;
}

export function isPreviewable(path: string): boolean {
  return artifactKind(path) !== null;
}

/** 画布标题栏用的语言/格式标识（代码视图的高亮提示） */
export function languageOf(path: string): string {
  return suffixOf(path) || 'text';
}

/**
 * 极简 CSV 解析（零依赖）。
 *
 * ★ 为什么支持引号却不上完整 RFC 4180
 *
 * 模型生成的 CSV 里最常出现的就是「字段本身带逗号」，那正是引号包裹的场景；
 * 而转义引号（`""`）在 LLM 产出里几乎不会出现。这里处理引号包裹与 `""` 转义，
 * 其余（跨行字段等）不做 —— 画布是给人扫一眼的，不是数据管道；
 * 真要精确解析，用户下载后用 Excel 打开才是正路。
 */
export function parseCsv(text: string, delimiter = ','): string[][] {
  text = text.replace(/^\uFEFF/, '');
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = '';
  let quoted = false;

  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          cell += '"';
          i += 1;
        } else {
          quoted = false;
        }
      } else {
        cell += ch;
      }
      continue;
    }
    if (ch === '"') {
      quoted = true;
    } else if (ch === delimiter) {
      row.push(cell);
      cell = '';
    } else if (ch === '\n') {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = '';
    } else if (ch !== '\r') {
      cell += ch;
    }
  }
  row.push(cell);
  rows.push(row);

  // 丢掉结尾的空行（文件末尾的换行不该多渲染一行）
  return rows.filter((r) => r.length > 1 || (r[0] ?? '').trim() !== '');
}
