/** Incremental speech cuts are independent of chat bubble boundaries. */
export class StreamingSpeechText {
  private entries = new Map<number, { sent: number; chunks: number; text: string }>();

  take(index: number, text: string, final = false): string[] {
    const entry = this.entries.get(index) ?? { sent: 0, chunks: 0, text: '' };
    this.entries.set(index, entry);
    const spoken = entry.text.slice(0, entry.sent);
    if (entry.sent && !text.startsWith(spoken)) {
      // Final chat bubbles may remove punctuation; that is not a new utterance.
      if (text.replace(/[。！？!?…\s]+$/, '') === spoken.replace(/[。！？!?…\s]+$/, '')) {
        entry.sent = text.length;
      } else {
        // A corrected snapshot is a new phrase, never read a sliced-off final word.
        entry.sent = 0;
        entry.chunks = 0;
      }
    }
    entry.text = text;
    // Tagged reasoning waits for the backend's authoritative clean final snapshot.
    if (/<\/?think(?:ing)?(?:>|$)/i.test(text)) return [];
    const result: string[] = [];
    let end = text.length;
    // Never speak a half-open code block, inline code or Markdown link.
    const fence = text.match(/```/g)?.length ?? 0;
    if (fence % 2) end = Math.min(end, text.lastIndexOf('```'));
    const prefix = text.slice(0, end);
    const ticks = prefix.replace(/```[\s\S]*?```/g, '').match(/`/g)?.length ?? 0;
    if (ticks % 2) end = Math.min(end, prefix.lastIndexOf('`'));
    const linkStart = text.lastIndexOf('[');
    if (linkStart >= entry.sent && !text.slice(linkStart).includes(')')) end = Math.min(end, linkStart);
    const url = /https?:\/\/\S*$/.exec(text.slice(0, end));
    if (url && !final) end = Math.min(end, url.index);

    while (entry.sent < end) {
      const rest = text.slice(entry.sent, end);
      const first = entry.chunks === 0;
      const min = first ? 8 : 18;
      const max = first ? 32 : 72;
      let cut = 0;
      let inFence = false;
      let inline = false;
      let link = 0;
      for (let i = 0; i < rest.length; i++) {
        if (rest.startsWith('```', i)) { inFence = !inFence; i += 2; continue; }
        if (inFence) continue;
        if (rest[i] === '`') { inline = !inline; continue; }
        if (inline) continue;
        if (rest[i] === '[') link += 1;
        if (rest[i] === ')' && link) link -= 1;
        if (link) continue;
        if (/[。！？!?…\n]/.test(rest[i]) || (i + 1 >= min && /[，,；;：:]/.test(rest[i]))) { cut = i + 1; break; }
        if (i + 1 >= max && (/[\u4e00-\u9fff\s]/.test(rest[i]) || i + 1 >= 120)) { cut = i + 1; break; }
      }
      if (!cut && final) cut = rest.length;
      if (!cut) break;
      const chunk = rest.slice(0, cut).trim();
      entry.sent += cut;
      if (chunk) { entry.chunks += 1; result.push(chunk); }
    }
    if (final) entry.sent = text.length;
    return result;
  }
}

/** Bound synthesis work without slicing ordinary Chinese clauses. */
export function speechSegments(text: string): string[] {
  const parts: string[] = [];
  let rest = text.trim();
  while (rest.length > 90) {
    const prefix = rest.slice(0, 90);
    const boundary = Math.max(prefix.lastIndexOf('。'), prefix.lastIndexOf('，'), prefix.lastIndexOf(' '));
    const cut = boundary >= 30 ? boundary + 1 : 90;
    parts.push(rest.slice(0, cut));
    rest = rest.slice(cut).trimStart();
  }
  if (rest) parts.push(rest);
  return parts;
}
