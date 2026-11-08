import { memo } from 'react';
import { BookOpen, ChevronDown, Globe } from '@/components/icons';
import { nameOf } from '@/lib/artifacts';
import { knowledgeSources, safeSourceUrl, webSources } from '@/lib/messagePresentation';
import { openWebLink } from '@/stores/artifactStore';
import type { SourceDoc, ToolCallInfo } from '@/types';

export const MessageSources = memo(function MessageSources({ sources = [], tools = [] }: { sources?: SourceDoc[]; tools?: Pick<ToolCallInfo, 'toolName' | 'result' | 'status'>[] }) {
  const citations = [...sources, ...knowledgeSources(tools).filter(source => !sources.some(existing => existing.source === source.source && existing.page === source.page))];
  const urls = webSources(tools).filter(url => !citations.some(source => safeSourceUrl(source.source) === url));
  if (!citations.length && !urls.length) return null;
  return <details className="message-sources">
    <summary><BookOpen size={13}/><span>来源 {citations.length + urls.length}</span><ChevronDown size={12}/></summary>
    <div className="message-sources__list">
      {citations.map((source, index) => {
        const url = safeSourceUrl(source.source);
        const title = url ? new URL(url).hostname : nameOf(source.source || '') || '未命名资料';
        return <article key={source.source + ':' + source.page + ':' + index} className="message-source">
          <header><span className="message-source__index">{index + 1}</span>{url ? <button type="button" onClick={() => openWebLink(url)} title={source.source}>{title}</button> : <span title={source.source}>{title}</span>}{source.page > 0 && <small>第 {source.page} 页</small>}</header>
          {source.preview && <details><summary>查看引用片段</summary><p>{source.preview}</p></details>}
        </article>;
      })}
      {urls.map(url => <button className="message-source message-source--web" key={url} onClick={() => openWebLink(url)} title={url}><Globe size={14}/><span>{new URL(url).hostname}<small>{url}</small></span><span aria-hidden>↗</span></button>)}
    </div>
  </details>;
});
