import { useRef, useState, type RefObject } from 'react';
import { Check, FileText, RefreshCw, UploadCloud, X } from '@/components/icons';
import { formatBytes } from '@/lib/format';
import { KNOWLEDGE_EXTENSIONS, useKnowledgeStore } from '@/stores/knowledgeStore';
import type { IndexStatus } from '@/types';

const LABELS = { queued: '等待中', uploading: '入库中', indexed: '已完成', warning: '部分就绪', error: '失败' };
export function KnowledgeUploadPanel({ inputRef, status }: { inputRef: RefObject<HTMLInputElement>; status: IndexStatus | null }) {
  const { uploads, uploading, enqueue, retry, dismiss, clearFinished } = useKnowledgeStore();
  const [dragging, setDragging] = useState(false);
  const depth = useRef(0);
  const extensions = status?.supported_extensions ?? KNOWLEDGE_EXTENSIONS;
  const maxSize = status?.max_file_size_mb ?? 50;
  const add = (files: FileList | File[]) => enqueue(files, maxSize, extensions);
  return <section className="kb-upload-panel" aria-label="添加知识文档">
    <input ref={inputRef} type="file" multiple accept={extensions.join(',')} className="sr-only" aria-label="选择知识库文档" onChange={event => {
      if (event.target.files?.length) add(event.target.files);
      event.target.value = '';
    }} />
    <button type="button" className={`kb-upload ${dragging ? 'kb-upload--over' : ''}`} onClick={() => inputRef.current?.click()}
      onDragEnter={event => { event.preventDefault(); depth.current += 1; setDragging(true); }}
      onDragOver={event => { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; }}
      onDragLeave={event => { event.preventDefault(); depth.current -= 1; if (depth.current <= 0) { depth.current = 0; setDragging(false); } }}
      onDrop={event => { event.preventDefault(); depth.current = 0; setDragging(false); if (event.dataTransfer.files.length) add(event.dataTransfer.files); }}>
      <span className="kb-upload__icon"><UploadCloud size={24} /></span>
      <span className="kb-upload__copy"><strong>{dragging ? '松开即可添加文档' : '把资料拖到这里，或点击上传'}</strong><span>PDF、Word、PPT、Excel、Markdown 与文本文件 · 每份最多 {maxSize} MB</span></span>
      <span className="kb-upload__tag">{uploading ? '可继续添加' : '支持批量上传'}</span>
    </button>
    {uploads.length > 0 && <details className="kb-upload-queue" open>
      <summary><span>{uploading ? <span className="spinner" /> : <Check size={14} />}本次上传 <b>{uploads.length}</b> 份<span className="kb-caption">{uploads.filter(item => item.state === 'indexed' || item.state === 'warning').length} 份完成{uploads.some(item => item.state === 'error') ? ` · ${uploads.filter(item => item.state === 'error').length} 份失败` : ''}</span></span></summary>
      <div className="kb-upload-queue__list" aria-live="polite">{uploads.map(item => <div className="kb-upload-item" key={item.id}>
        <FileText size={16} /><div className="kb-upload-item__copy"><strong title={item.name}>{item.name}</strong><span className={item.state === 'error' ? 'kb-error-text' : ''}>{formatBytes(item.size)} · {item.message}</span></div>
        <span className={`kb-status kb-status--${item.state === 'error' ? 'error' : item.state === 'warning' ? 'warning' : item.state === 'indexed' ? 'indexed' : 'pending'}`}><i />{LABELS[item.state]}</span>
        {item.state === 'error' && item.file && <button type="button" className="btn btn--sm btn--ghost" onClick={() => retry(item.id)} aria-label={`重试上传 ${item.name}`}><RefreshCw size={13} /></button>}
        {item.state !== 'uploading' && <button type="button" className="btn btn--sm btn--ghost" onClick={() => dismiss(item.id)} aria-label={`${item.state === 'queued' ? '取消上传' : '移除上传记录'} ${item.name}`}><X size={13} /></button>}
      </div>)}</div>
      {uploads.some(item => item.state === 'indexed' || item.state === 'warning') && <button type="button" className="kb-text-button kb-upload-queue__clear" onClick={clearFinished}>清除已完成记录</button>}
    </details>}
  </section>;
}
