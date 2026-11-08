import { useState } from 'react';
import type { IndexDocument } from '@/types';

export function DocumentStatus({ doc }: { doc: IndexDocument }) {
  const state = doc.error ? 'error' : doc.warnings?.length ? 'warning' : !doc.chunks ? 'empty' : 'indexed';
  const label = { error: '需要处理', warning: '部分就绪', empty: '无文本', indexed: '已入库' }[state];
  return <span className={`kb-status kb-status--${state}`} title={doc.error || doc.warnings?.join('；') || label}><i />{label}</span>;
}

export function ChunkText({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const long = text.length > 480;
  return <>
    <p className="kb-chunk__text">{long && !expanded ? `${text.slice(0, 480)}…` : text}</p>
    {long && <button type="button" className="kb-text-button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '收起内容' : '展开完整内容'}</button>}
  </>;
}

export function KnowledgeEmpty({ title }: { title: string }) {
  return <div className="kb-empty"><strong>{title}</strong></div>;
}
