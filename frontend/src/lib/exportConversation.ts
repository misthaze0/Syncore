/**
 * 把一段对话导出成 Markdown
 *
 * ★ 为什么放在前端而不是加一个后端导出接口
 *
 * 需要的全部数据已经在浏览器里了 —— 界面上的消息就是完整内容
 * （`agentStore.messages` 由实时流与历史重放共同填充，两条路径同一份结构）。
 * 再加一个后端接口，等于为了「把已有数据换个格式」多维护一条链路与一套测试。
 *
 * ★ 边界：导出的是**对话本身**，不是全部可观测数据
 *
 * 引用来源与产出文件是「这段对话的结论」，跟着走有价值；
 * 而工具原始 JSON、召回排名、span 这类调试信息**刻意不导出** ——
 * 一份给用户带走的文档里塞满这些，只会让真正的内容读不下去。
 * 需要完整留痕的场景，出口是控制台的链路追踪。
 */

import type { ChatMessage } from '@/types';

/** 把标题/首句变成安全的文件名（去掉路径分隔符与常见非法字符） */
export function safeFileName(text: string, fallback = '对话记录'): string {
  const cleaned = (text || '')
    .replace(/[\\/:*?"<>|\n\r\t]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 40);
  return cleaned || fallback;
}

function timestamp(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function conversationToMarkdown(messages: ChatMessage[], title: string): string {
  const lines: string[] = [`# ${title || '对话记录'}`, '', `> 导出自 Syncore · ${timestamp()}`, ''];

  messages.forEach((message) => {
    if (message.role === 'user') {
      lines.push('## 我', '', message.content.trim(), '');
      return;
    }

    lines.push('## Syncore', '', (message.content || '').trim() || '（无内容）', '');

    const files = message.files ?? [];
    if (files.length > 0) {
      lines.push('**本次产出的文件**', '');
      files.forEach((f) => lines.push(`- \`${f.path}\``));
      lines.push('');
    }

    const sources = message.sources ?? [];
    if (sources.length > 0) {
      lines.push('**引用来源**', '');
      sources.forEach((s) => {
        const page = s.page > 0 ? ` · P${s.page}` : '';
        const score = typeof s.score === 'number' ? ` · 相关度 ${s.score.toFixed(3)}` : '';
        lines.push(`- ${s.source || '未知来源'}${page}${score}`);
      });
      lines.push('');
    }

    if (message.error) lines.push(`> ⚠️ 这一轮出错：${message.error}`, '');
  });

  return lines.join('\n');
}

/** 触发浏览器下载一个文本文件（零依赖，用 Blob + 临时 <a>） */
export function downloadTextFile(filename: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // 立刻 revoke 在部分浏览器里会打断下载，留一拍更稳
  window.setTimeout(() => URL.revokeObjectURL(url), 5000);
}
