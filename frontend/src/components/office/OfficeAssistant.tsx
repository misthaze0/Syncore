import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react';
import type { Editor } from '@tiptap/react';
import { ArrowDownToLine, ArrowUp, Check, CheckCheck, ChevronDown, Copy, FileText, Maximize2, Minimize2, PanelRightClose, PencilLine, RefreshCw, Settings2, Sparkles, Square, Undo2, WandSparkles, X } from 'lucide-react';

import brandMark from '@/assets/brand/app-icon.png';
import { MarkdownRenderer } from '@/components/MarkdownRenderer';
import { OfficePreview } from '@/components/office/OfficePreview';
import { assistOfficeDocument } from '@/lib/officeApi';
import { useAgentStore } from '@/stores/agentStore';
import { useOfficeStore } from '@/stores/officeStore';
import { toast } from '@/stores/toastStore';
import type { OfficeAIResult, OfficeDocument, OfficeMessage, OfficeSelection } from '@/types/office';

interface Proposal {
  result: OfficeAIResult;
  baseContent: string;
  baseTitle: string;
  selection: OfficeSelection | null;
  messageId: string;
  prompt: string;
}

export interface OfficeAssistantHandle { ask: (prompt: string) => void }
interface Props {
  document: OfficeDocument;
  editor: Editor | null;
  selection: OfficeSelection | null;
  onClearSelection: () => void;
  onClose: () => void;
  onSettings: () => void;
}

const ACTIONS = [
  { label: '润色文字', icon: WandSparkles, prompt: '请润色文字，使语言自然流畅、简洁专业，保留原意和所有关键信息。' },
  { label: '继续写作', icon: PencilLine, prompt: '请沿着当前内容继续写作，补充自然衔接的下一部分，保留已有内容。' },
  { label: '提炼摘要', icon: Minimize2, prompt: '请在文档开头添加一段简明摘要，概括核心内容与结论，保留其余正文。' },
  { label: '丰富内容', icon: Maximize2, prompt: '请适当扩充内容，补充有帮助的解释、细节或行动建议，不编造事实与数据。' },
];

function readAutoApply(): boolean { try { return localStorage.getItem('xjms.office.autoApply') !== 'false'; } catch { return true; } }

export const OfficeAssistant = forwardRef<OfficeAssistantHandle, Props>(function OfficeAssistant({ document, editor, selection, onClearSelection, onClose, onSettings }, ref) {
  const [draft, setDraft] = useState('');
  const [mode, setMode] = useState<'edit' | 'chat'>('edit');
  const [autoApply, setAutoApply] = useState(readAutoApply);
  const [status, setStatus] = useState('');
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [preview, setPreview] = useState(false);
  const [applying, setApplying] = useState(false);
  const [undo, setUndo] = useState<{ before: string; title: string; after: string; afterTitle: string; messageId: string } | null>(null);
  const controller = useOfficeStore(state => state.assistantAbort);
  const models = useAgentStore(state => state.availableModels);
  const switchModel = useAgentStore(state => state.switchModel);
  const modelName = useAgentStore(state => state.serviceInfo?.model);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const documentId = document.id;

  const patchMessage = useCallback((messageId: string, patch: Partial<OfficeMessage>) => {
    const state = useOfficeStore.getState();
    if (state.document?.id !== documentId) return;
    state.update({ messages: state.document.messages.map(item => item.id === messageId ? { ...item, ...patch } : item) });
  }, [documentId]);

  useEffect(() => {
    if (followRef.current && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [document.messages, status, proposal]);
  useEffect(() => () => { useOfficeStore.getState().stopAssistant(); }, [documentId]);

  const apply = useCallback(async (candidate: Proposal) => {
    if (!editor || editor.isDestroyed || useOfficeStore.getState().document?.id !== documentId) return;
    if (editor.getHTML() !== candidate.baseContent) {
      setProposal(candidate);
      toast('文档在生成期间有了新修改，请基于当前内容重新生成建议', 'warning');
      return;
    }
    setProposal(candidate);
    setApplying(true);
    try {
      if (!await useOfficeStore.getState().save()) { toast('请先解决保存问题，再应用修改', 'warning'); return; }
      if (editor.isDestroyed || useOfficeStore.getState().document?.id !== documentId) return;
      if (editor.getHTML() !== candidate.baseContent) {
        toast('检测到新的编辑，请基于最新内容重新生成建议', 'warning');
        return;
      }
      if (candidate.result.scope === 'selection' && candidate.selection) {
        editor.chain().focus().insertContentAt({ from: candidate.selection.from, to: candidate.selection.to }, candidate.result.html).run();
      } else editor.commands.setContent(candidate.result.html);
      const after = editor.getHTML();
      if (candidate.result.title && candidate.result.scope === 'document' && useOfficeStore.getState().document?.title === candidate.baseTitle) useOfficeStore.getState().update({ title: candidate.result.title });
      setUndo({ before: candidate.baseContent, title: candidate.baseTitle, after, afterTitle: useOfficeStore.getState().document?.title || candidate.baseTitle, messageId: candidate.messageId });
      patchMessage(candidate.messageId, { status: 'applied', content: candidate.result.reply, change: candidate.result.scope === 'selection' ? '已修改选中内容' : '已更新当前文档' });
      setProposal(null);
      setPreview(false);
      onClearSelection();
      await useOfficeStore.getState().save('ai');
    } catch (error) { toast(error instanceof Error ? error.message : '应用失败，原文已保留', 'error'); }
    finally { setApplying(false); }
  }, [editor, documentId, onClearSelection, patchMessage]);

  const send = useCallback(async (text: string, requestedMode = mode) => {
    const current = useOfficeStore.getState();
    if (!text.trim() || current.assistantAbort || applying || !editor || editor.isDestroyed || current.document?.id !== documentId) return;
    if (current.document.trashed) return;
    if (current.conflict) { toast('请先保留本地副本或重新加载，再调用助手', 'warning'); return; }
    const snapshot = { ...current.document, content: editor.getHTML() };
    const selected = selection;
    const messageId = crypto.randomUUID();
    const createdAt = new Date().toISOString();
    const abort = new AbortController();
    useOfficeStore.setState({ assistantAbort: abort });
    current.update({ messages: [...snapshot.messages.slice(-98), { id: crypto.randomUUID(), role: 'user', content: text.trim(), created_at: createdAt, status: 'complete', change: selected ? `选中 ${selected.text.replace(/\s/g, '').length} 字符` : '' }, { id: messageId, role: 'assistant', content: '', created_at: createdAt, status: 'pending' }] });
    setDraft('');
    setProposal(null);
    setPreview(false);
    setStatus('正在准备…');
    followRef.current = true;
    let answer = '';
    let result: OfficeAIResult | null = null;
    let completed = false;
    let lastPaint = 0;
    try {
      for await (const event of assistOfficeDocument(snapshot, text.trim(), requestedMode, selected, abort.signal)) {
        if (abort.signal.aborted) break;
        if (event.type === 'status') setStatus(event.text);
        if (event.type === 'token') {
          answer += event.text;
          if (Date.now() - lastPaint > 50) { patchMessage(messageId, { content: answer }); lastPaint = Date.now(); }
        }
        if (event.type === 'result') result = event;
        if (event.type === 'done') completed = true;
        if (event.type === 'error') throw new Error(event.message);
      }
      if (abort.signal.aborted) return;
      if (!completed) throw new Error('连接已中断，文档尚未修改，请重试');
      if (result) {
        const candidate: Proposal = { result, baseContent: snapshot.content, baseTitle: snapshot.title, selection: selected, messageId, prompt: text };
        patchMessage(messageId, { content: result.reply, status: 'complete', change: '修改建议已生成' });
        if (autoApply && editor.getHTML() === snapshot.content) await apply(candidate);
        else { setProposal(candidate); if (autoApply) toast('检测到新的编辑，修改建议已保留在右侧', 'info'); }
      } else patchMessage(messageId, { content: answer, status: 'complete' });
    } catch (error) {
      if (!abort.signal.aborted) {
        const message = error instanceof Error ? error.message : '生成失败，请稍后重试';
        patchMessage(messageId, { content: message, status: 'error' });
      }
    } finally {
      if (useOfficeStore.getState().assistantAbort === abort) { useOfficeStore.setState({ assistantAbort: null }); setStatus(''); }
      void useOfficeStore.getState().save();
    }
  }, [mode, applying, editor, documentId, selection, autoApply, apply, patchMessage]);

  useImperativeHandle(ref, () => ({ ask: (prompt) => {
    setMode('edit');
    if (prompt) void send(prompt, 'edit');
    else { inputRef.current?.focus(); }
  } }), [send]);

  const undoChange = async () => {
    if (!undo || !editor) return;
    if (editor.getHTML() !== undo.after) { toast('文档已有后续编辑，请通过版本历史恢复需要的内容', 'info'); return; }
    editor.commands.setContent(undo.before);
    if (useOfficeStore.getState().document?.title === undo.afterTitle) useOfficeStore.getState().update({ title: undo.title });
    patchMessage(undo.messageId, { status: 'complete', change: '这次修改已撤销' });
    setUndo(null);
    await useOfficeStore.getState().save('restore');
    toast('已撤销这次 AI 修改', 'success');
  };
  const insertAnswer = async (content: string) => {
    if (!editor || !await useOfficeStore.getState().save()) return;
    editor.chain().focus().insertContentAt(editor.state.doc.content.size, content, { contentType: 'markdown' }).run();
    await useOfficeStore.getState().save('ai');
    toast('已添加到文档末尾', 'success');
  };

  return (
    <>
      <header className="office-ai-header"><div className="office-ai-identity"><img src={brandMark} alt="" /><div><strong>Syncore 助手 <span className="office-ai-badge">AI</span></strong><span><i />让文档从想法走向交付</span></div></div><button className="office-tool" onClick={onClose} title="收起助手" aria-label="收起助手"><PanelRightClose size={17} /></button></header>
      <div className="office-ai-context"><FileText size={13} /><span title={document.title}>{document.title || '未命名文档'}</span><span className="office-ai-context-label">当前文档</span></div>
      <div className="office-ai-messages" ref={scrollRef} onScroll={() => { const node = scrollRef.current; if (node) followRef.current = node.scrollHeight - node.clientHeight - node.scrollTop < 100; }} aria-label="文档助手对话" aria-busy={Boolean(controller)}>
        {!document.messages.length && <div className="office-ai-welcome"><div className="office-ai-orbit"><Sparkles size={25} /></div><h3>文档助手</h3><div className="office-ai-actions">{ACTIONS.map(action => <button key={action.label} onClick={() => void send(action.prompt, 'edit')} disabled={Boolean(controller)}><action.icon size={16} /><span>{action.label}</span><span>↗</span></button>)}</div></div>}
        {document.messages.map(message => <div key={message.id} className={`office-ai-message office-ai-message--${message.role}${message.status === 'error' ? ' is-error' : ''}`}>
          <div className="office-ai-message-label">{message.role === 'assistant' ? <><Sparkles size={12} />Syncore</> : '你'}{message.role === 'user' && message.change && <span>{message.change}</span>}</div>
          {message.content ? <MarkdownRenderer content={message.content} /> : <div className="office-ai-thinking"><span /><span /><span /></div>}
          {message.status === 'pending' && status && <div className="office-ai-progress" role="status">{status}</div>}
          {message.status === 'applied' && <div className="office-change-applied"><CheckCheck size={14} /><span>{message.change || '已更新文档'}</span>{undo?.messageId === message.id && <button onClick={() => void undoChange()} disabled={applying || Boolean(controller)}><Undo2 size={12} />撤销</button>}</div>}
          {message.status === 'cancelled' && <div className="office-ai-message-note">已停止生成</div>}
          {message.status === 'complete' && message.change === '这次修改已撤销' && <div className="office-ai-message-note">{message.change}</div>}
          {message.role === 'assistant' && message.content && message.status !== 'pending' && message.status !== 'error' && <div className="office-ai-message-tools"><button title="复制回答" aria-label="复制回答" onClick={() => { void navigator.clipboard.writeText(message.content).then(() => toast('已复制', 'success')).catch(() => toast('复制失败，请手动选择文字复制', 'error')); }}><Copy size={12} /></button>{message.status === 'complete' && !message.change && <button title="添加到文档末尾" onClick={() => void insertAnswer(message.content)} disabled={Boolean(controller)}><ArrowDownToLine size={12} />添加到文档</button>}</div>}
          {proposal?.messageId === message.id && <div className="office-change-card"><div><PencilLine size={14} /><strong>{proposal.result.scope === 'selection' ? '选中内容的修改建议' : '文档修改建议'}</strong><button title="放弃建议" aria-label="放弃建议" onClick={() => { patchMessage(message.id, { change: '建议未应用' }); setProposal(null); }}><X size={13} /></button></div><button className="office-change-preview-toggle" onClick={() => setPreview(value => !value)}>{preview ? '收起预览' : '查看修改后的内容'}<ChevronDown size={13} /></button>{preview && <div className="office-change-preview"><OfficePreview content={proposal.result.html} /></div>}{editor && editor.getHTML() !== proposal.baseContent ? <div className="office-change-stale"><span>文档有了新修改，需要基于最新内容生成。</span><button className="office-primary" disabled={Boolean(controller)} onClick={() => void send(proposal.prompt, 'edit')}><RefreshCw size={13} />重新生成</button></div> : <button className="office-primary" disabled={applying || Boolean(controller)} onClick={() => void apply(proposal)}><Check size={14} />{applying ? '正在应用…' : '应用到文档'}</button>}</div>}
          {message.status === 'error' && <button className="office-ai-retry" onClick={() => { const index = document.messages.findIndex(item => item.id === message.id); const previous = document.messages.slice(0, index).reverse().find(item => item.role === 'user'); if (previous) void send(previous.content); }} disabled={Boolean(controller)}><RefreshCw size={12} />重试</button>}
        </div>)}
      </div>
      <div className="office-ai-compose-area">
        {selection?.text && <div className="office-selection-chip"><QuoteMark /><span>已选 {selection.text.replace(/\s/g, '').length} 字符<strong>{selection.text.slice(0, 52)}</strong></span><button title="取消选区上下文" aria-label="取消选区上下文" onClick={onClearSelection}><X size={13} /></button></div>}
        <div className="office-ai-mode"><div role="group" aria-label="助手工作方式"><button className={mode === 'edit' ? 'is-active' : ''} aria-pressed={mode === 'edit'} onClick={() => setMode('edit')}><PencilLine size={12} />编辑文档</button><button className={mode === 'chat' ? 'is-active' : ''} aria-pressed={mode === 'chat'} onClick={() => setMode('chat')}>仅对话</button></div>{mode === 'edit' && <label title="关闭后可先预览修改，再应用到文档"><input type="checkbox" checked={autoApply} onChange={event => { setAutoApply(event.target.checked); try { localStorage.setItem('xjms.office.autoApply', String(event.target.checked)); } catch { /* Session only. */ } }} />自动应用</label>}</div>
        <form className="office-ai-composer" onSubmit={event => { event.preventDefault(); void send(draft); }}><textarea ref={inputRef} aria-label="给 Syncore 的指令" maxLength={12000} value={draft} onChange={event => { setDraft(event.target.value); event.target.style.height = 'auto'; event.target.style.height = `${Math.min(160, event.target.scrollHeight)}px`; }} placeholder={mode === 'chat' ? '聊聊思路，或问问这份文档…' : selection?.text ? '想怎样修改选中的文字？' : '描述你的想法，Syncore 会直接编辑文档…'} rows={3} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(draft); } }} /><div className="office-ai-composer-foot"><div className="office-ai-model">{models.length > 1 ? <select aria-label="助手使用的模型" value={models.find(model => model.default)?.id || modelName || ''} onChange={event => { const model = models.find(item => item.id === event.target.value); if (model) void switchModel(model); }}>{models.map(model => <option key={`${model.profile_id || ''}-${model.id}`} value={model.id}>{model.label || model.id}</option>)}</select> : <span>{modelName?.split('/').at(-1) || '默认模型'}</span>}<button type="button" title="模型设置" aria-label="模型设置" onClick={onSettings}><Settings2 size={13} /></button></div>{controller ? <button type="button" className="office-ai-send is-stop" title="停止生成" aria-label="停止生成" onClick={() => useOfficeStore.getState().stopAssistant()}><Square size={13} fill="currentColor" /></button> : <button type="submit" className="office-ai-send" disabled={!draft.trim() || !editor || applying} title="发送 (Enter)" aria-label="发送给 Syncore"><ArrowUp size={17} /></button>}</div></form>

      </div>
    </>
  );
});

function QuoteMark() { return <span className="office-selection-quote">“</span>; }
