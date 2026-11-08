import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { EditorContent, useEditor } from '@tiptap/react';
import type { Editor } from '@tiptap/react';
import { BubbleMenu } from '@tiptap/react/menus';
import { DOMSerializer } from '@tiptap/pm/model';
import { AlignCenter, AlignJustify, AlignLeft, AlignRight, ArrowDown, ArrowUp, Bold, CheckSquare, ChevronDown, Code2, Highlighter, Italic, Link2, List, ListOrdered, Minus, Pilcrow, Plus, Quote, Redo2, Replace, Search, Sparkles, Strikethrough, Table2, Trash2, Underline, Undo2, X } from 'lucide-react';

import { officeEditorExtensions } from '@/components/office/editorExtensions';
import { useOfficeStore } from '@/stores/officeStore';
import { toast } from '@/stores/toastStore';
import type { OfficeDocument, OfficeSelection } from '@/types/office';

interface Props {
  document: OfficeDocument;
  onReady: (editor: Editor | null) => void;
  onSelection: (selection: OfficeSelection | null) => void;
  onAsk: (prompt: string) => void;
  outlineOpen: boolean;
}

function Tool({ label, active, disabled, onClick, children }: { label: string; active?: boolean; disabled?: boolean; onClick: () => void; children: ReactNode }) {
  return <button type="button" title={label} aria-label={label} aria-pressed={active} disabled={disabled} className={`office-tool${active ? ' is-active' : ''}`} onMouseDown={event => event.preventDefault()} onClick={onClick}>{children}</button>;
}

function matches(editor: Editor, text: string): Array<{ from: number; to: number }> {
  if (!text) return [];
  const found: Array<{ from: number; to: number }> = [];
  editor.state.doc.descendants((node, pos) => {
    if (!node.isTextblock) return;
    const value = node.textBetween(0, node.content.size, '\n', '\ufffc').toLocaleLowerCase();
    const query = text.toLocaleLowerCase();
    let index = value.indexOf(query);
    while (index !== -1) {
      found.push({ from: pos + 1 + index, to: pos + 1 + index + text.length });
      index = value.indexOf(query, index + Math.max(1, text.length));
    }
    return false;
  });
  return found;
}

const SLASH_ITEMS = [
  { id: 'paragraph', name: '正文', hint: '开始一段普通文字', icon: Pilcrow },
  { id: 'h1', name: '一级标题', hint: '文档的主要章节', icon: Pilcrow },
  { id: 'h2', name: '二级标题', hint: '让内容层次分明', icon: Pilcrow },
  { id: 'bullet', name: '无序列表', hint: '列出并列的想法', icon: List },
  { id: 'task', name: '待办清单', hint: '把想法变成行动', icon: CheckSquare },
  { id: 'table', name: '表格', hint: '整理信息与数据', icon: Table2 },
  { id: 'quote', name: '引用', hint: '突出一段重要的话', icon: Quote },
  { id: 'divider', name: '分隔线', hint: '划分内容区块', icon: Minus },
  { id: 'ai', name: 'AI 起草', hint: '一起跨过空白页', icon: Sparkles },
];
const BUBBLE_OPTIONS = { placement: 'top' as const };

export function OfficeEditor({ document, onReady, onSelection, onAsk, outlineOpen }: Props) {
  const update = useOfficeStore(state => state.update);
  const [source, setSource] = useState<string | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [replacement, setReplacement] = useState('');
  const [matchIndex, setMatchIndex] = useState(0);
  const [zoom, setZoom] = useState(100);
  const [slash, setSlash] = useState<{ text: string; from: number; x: number; y: number; index: number } | null>(null);
  const [linkOpen, setLinkOpen] = useState(false);
  const [link, setLink] = useState('');
  const slashRef = useRef(slash);
  slashRef.current = slash;
  const runSlashRef = useRef<(id: string) => void>(() => {});
  const sourceInputRef = useRef(false);
  const extensions = useMemo(() => officeEditorExtensions(), []);

  const editor = useEditor({
    extensions, content: document.content,
    shouldRerenderOnTransaction: true,
    editorProps: {
      attributes: { class: 'office-prose', 'aria-label': '文档正文', spellcheck: 'false' },
      handleKeyDown: (_view, event) => {
        if (event.isComposing) return false;
        const current = slashRef.current;
        if (!current) return false;
        const items = SLASH_ITEMS.filter(item => `${item.name}${item.id}`.toLowerCase().includes(current.text.toLowerCase()));
        if (event.key === 'Escape') { setSlash(null); return true; }
        if (!items.length) return false;
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          const step = event.key === 'ArrowDown' ? 1 : -1;
          setSlash({ ...current, index: (current.index + step + items.length) % items.length });
          return true;
        }
        if (event.key === 'Enter') { runSlashRef.current(items[current.index % items.length].id); return true; }
        return false;
      },
    },
    onUpdate: ({ editor: current }) => {
      update({ content: current.getHTML() });
      if (source !== null && !sourceInputRef.current) setSource(current.getMarkdown());
      const { $from, empty } = current.state.selection;
      const text = $from.parent.textBetween(0, $from.parentOffset, '\n');
      const match = /^\/([^/\n]{0,16})$/.exec(text);
      if (match && empty && $from.parent.type.name === 'paragraph') {
        const coords = current.view.coordsAtPos($from.pos);
        setSlash({ text: match[1], from: $from.pos - text.length, x: Math.min(coords.left, window.innerWidth - 290), y: Math.min(coords.bottom + 8, window.innerHeight - 390), index: 0 });
      } else setSlash(null);
    },
    onSelectionUpdate: ({ editor: current }) => {
      const { from, to, empty } = current.state.selection;
      if (empty) { onSelection(null); return; }
      const fragment = DOMSerializer.fromSchema(current.schema).serializeFragment(current.state.selection.content().content);
      const container = window.document.createElement('div');
      container.append(fragment);
      // A selection ending at a block boundary may include an empty open-end heading.
      container.querySelectorAll('p,h1,h2,h3').forEach(node => {
        if (!node.textContent && !node.children.length) node.remove();
      });
      onSelection({ from, to, text: current.state.doc.textBetween(from, to, '\n'), html: container.innerHTML });
    },
  });

  useEffect(() => { onReady(editor); return () => onReady(null); }, [editor, onReady]);
  useEffect(() => {
    if (!editor) return;
    if (editor.getHTML() !== document.content) {
      editor.commands.setContent(document.content, { emitUpdate: false });
      if (source !== null) setSource(editor.getMarkdown());
    }
  }, [document.content, editor, source]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (window.document.querySelector('[aria-modal="true"]')) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
        event.preventDefault(); setSearchOpen(true);
      }
      if (event.key === 'Escape') { setSearchOpen(false); setLinkOpen(false); }
    };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, []);

  const runSlash = useCallback((id: string) => {
    if (!editor || !slashRef.current) return;
    editor.chain().focus().deleteRange({ from: slashRef.current.from, to: editor.state.selection.from }).run();
    setSlash(null);
    const chain = editor.chain().focus();
    if (id === 'h1' || id === 'h2') chain.setHeading({ level: id === 'h1' ? 1 : 2 }).run();
    else if (id === 'paragraph') chain.setParagraph().run();
    else if (id === 'bullet') chain.toggleBulletList().run();
    else if (id === 'task') chain.toggleTaskList().run();
    else if (id === 'table') chain.insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run();
    else if (id === 'quote') chain.toggleBlockquote().run();
    else if (id === 'divider') chain.setHorizontalRule().run();
    else if (id === 'ai') onAsk('');
  }, [editor, onAsk]);
  runSlashRef.current = runSlash;

  if (!editor) return <div className="office-editor-loading"><span className="spinner" />正在准备编辑器…</div>;
  const found = matches(editor, query);
  const navigateMatch = (index: number) => {
    if (!found.length) return;
    const next = (index + found.length) % found.length;
    setMatchIndex(next);
    editor.chain().focus().setTextSelection(found[next]).scrollIntoView().run();
  };
  const replace = (all: boolean) => {
    if (!found.length) return;
    const targets = all ? found : [found[Math.min(matchIndex, found.length - 1)]];
    const transaction = editor.state.tr;
    [...targets].reverse().forEach(target => transaction.insertText(replacement, target.from, target.to));
    editor.view.dispatch(transaction);
    setMatchIndex(0);
    toast(all ? `已替换 ${targets.length} 处` : '已替换当前匹配', 'success');
  };
  const headings: Array<{ text: string; level: number; pos: number }> = [];
  editor.state.doc.descendants((node, pos) => { if (node.type.name === 'heading') headings.push({ text: node.textContent || '未命名标题', level: node.attrs.level as number, pos }); });
  const characterCount = editor.getText().replace(/\s/g, '').length;
  const wordCount = editor.getText().trim().split(/\s+/).filter(Boolean).length;
  const slashItems = slash ? SLASH_ITEMS.filter(item => `${item.name}${item.id}`.toLowerCase().includes(slash.text.toLowerCase())) : [];

  return (
    <div className="office-editor">
      <div className="office-formatbar" role="toolbar" aria-label="文档格式">
        <div className="office-toolgroup"><Tool label="撤销 (Ctrl Z)" disabled={!editor.can().undo()} onClick={() => editor.chain().focus().undo().run()}><Undo2 size={16} /></Tool><Tool label="重做 (Ctrl Shift Z)" disabled={!editor.can().redo()} onClick={() => editor.chain().focus().redo().run()}><Redo2 size={16} /></Tool></div>
        <div className="office-toolgroup office-blockselect"><select aria-label="段落样式" value={editor.isActive('heading', { level: 1 }) ? 'h1' : editor.isActive('heading', { level: 2 }) ? 'h2' : editor.isActive('heading', { level: 3 }) ? 'h3' : 'p'} onChange={event => event.target.value === 'p' ? editor.chain().focus().setParagraph().run() : editor.chain().focus().setHeading({ level: Number(event.target.value.slice(1)) as 1 | 2 | 3 }).run()}><option value="p">正文</option><option value="h1">标题 1</option><option value="h2">标题 2</option><option value="h3">标题 3</option></select><ChevronDown size={12} /></div>
        <div className="office-toolgroup"><Tool label="加粗 (Ctrl B)" active={editor.isActive('bold')} onClick={() => editor.chain().focus().toggleBold().run()}><Bold size={16} /></Tool><Tool label="斜体 (Ctrl I)" active={editor.isActive('italic')} onClick={() => editor.chain().focus().toggleItalic().run()}><Italic size={16} /></Tool><Tool label="下划线 (Ctrl U)" active={editor.isActive('underline')} onClick={() => editor.chain().focus().toggleUnderline().run()}><Underline size={16} /></Tool><Tool label="删除线" active={editor.isActive('strike')} onClick={() => editor.chain().focus().toggleStrike().run()}><Strikethrough size={16} /></Tool><Tool label="高亮" active={editor.isActive('highlight')} onClick={() => editor.chain().focus().toggleHighlight().run()}><Highlighter size={16} /></Tool></div>
        <div className="office-toolgroup"><Tool label="无序列表" active={editor.isActive('bulletList')} onClick={() => editor.chain().focus().toggleBulletList().run()}><List size={16} /></Tool><Tool label="有序列表" active={editor.isActive('orderedList')} onClick={() => editor.chain().focus().toggleOrderedList().run()}><ListOrdered size={16} /></Tool><Tool label="待办清单" active={editor.isActive('taskList')} onClick={() => editor.chain().focus().toggleTaskList().run()}><CheckSquare size={16} /></Tool><Tool label="引用" active={editor.isActive('blockquote')} onClick={() => editor.chain().focus().toggleBlockquote().run()}><Quote size={16} /></Tool></div>
        <div className="office-toolgroup office-align-tools"><Tool label="左对齐" active={editor.isActive({ textAlign: 'left' })} onClick={() => editor.chain().focus().setTextAlign('left').run()}><AlignLeft size={16} /></Tool><Tool label="居中" active={editor.isActive({ textAlign: 'center' })} onClick={() => editor.chain().focus().setTextAlign('center').run()}><AlignCenter size={16} /></Tool><Tool label="右对齐" active={editor.isActive({ textAlign: 'right' })} onClick={() => editor.chain().focus().setTextAlign('right').run()}><AlignRight size={16} /></Tool><Tool label="两端对齐" active={editor.isActive({ textAlign: 'justify' })} onClick={() => editor.chain().focus().setTextAlign('justify').run()}><AlignJustify size={16} /></Tool></div>
        <div className="office-toolgroup"><Tool label="插入表格" onClick={() => editor.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run()}><Table2 size={16} /></Tool><Tool label="插入链接" active={editor.isActive('link')} onClick={() => { setLink(editor.getAttributes('link').href as string || ''); setLinkOpen(value => !value); }}><Link2 size={16} /></Tool><Tool label="代码块" active={editor.isActive('codeBlock')} onClick={() => editor.chain().focus().toggleCodeBlock().run()}><Code2 size={16} /></Tool><Tool label="查找与替换 (Ctrl F)" active={searchOpen} onClick={() => setSearchOpen(value => !value)}><Search size={16} /></Tool></div>
        <button className={`office-source-toggle${source !== null ? ' is-active' : ''}`} onClick={() => setSource(value => value === null ? editor.getMarkdown() : null)} title="切换 Markdown 编辑">MD</button>
      </div>

      {editor.isActive('table') && <div className="office-tablebar"><span><Table2 size={13} />表格</span><button onMouseDown={event => event.preventDefault()} onClick={() => editor.chain().focus().addRowAfter().run()}><Plus size={13} />添加行</button><button onMouseDown={event => event.preventDefault()} onClick={() => editor.chain().focus().addColumnAfter().run()}><Plus size={13} />添加列</button><button onMouseDown={event => event.preventDefault()} onClick={() => editor.chain().focus().deleteRow().run()}>删除行</button><button onMouseDown={event => event.preventDefault()} onClick={() => editor.chain().focus().deleteColumn().run()}>删除列</button><button onMouseDown={event => event.preventDefault()} onClick={() => editor.chain().focus().mergeOrSplit().run()}>合并 / 拆分</button><button onMouseDown={event => event.preventDefault()} onClick={() => editor.chain().focus().deleteTable().run()}><Trash2 size={13} />删除表格</button></div>}

      {linkOpen && <form className="office-linkbar" onSubmit={event => { event.preventDefault(); if (!link) editor.chain().focus().extendMarkRange('link').unsetLink().run(); else if (/^(https?:\/\/|mailto:|#)/i.test(link)) editor.chain().focus().extendMarkRange('link').setLink({ href: link }).run(); else { toast('请输入以 https://、http:// 或 mailto: 开头的链接', 'warning'); return; } setLinkOpen(false); }}><Link2 size={15} /><input autoFocus aria-label="链接地址" placeholder="https://example.com" value={link} onChange={event => setLink(event.target.value)} /><button type="submit" className="office-text-button">应用链接</button><button type="button" className="office-tool" aria-label="关闭链接编辑" onClick={() => setLinkOpen(false)}><X size={14} /></button></form>}

      {searchOpen && <div className="office-findbar"><div><Search size={14} /><input autoFocus aria-label="查找文本" placeholder="查找文档内容" value={query} onChange={event => { setQuery(event.target.value); setMatchIndex(0); }} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); navigateMatch(event.shiftKey ? matchIndex - 1 : matchIndex + 1); } }} /><span>{found.length ? `${Math.min(matchIndex + 1, found.length)} / ${found.length}` : query ? '无匹配' : '0 / 0'}</span><Tool label="上一个匹配" onClick={() => navigateMatch(matchIndex - 1)} disabled={!found.length}><ArrowUp size={14} /></Tool><Tool label="下一个匹配" onClick={() => navigateMatch(matchIndex + 1)} disabled={!found.length}><ArrowDown size={14} /></Tool></div><div><Replace size={14} /><input aria-label="替换为" placeholder="替换为…" value={replacement} onChange={event => setReplacement(event.target.value)} /><button disabled={!found.length} onClick={() => replace(false)}>替换</button><button disabled={!found.length} onClick={() => replace(true)}>全部替换</button><Tool label="关闭查找" onClick={() => setSearchOpen(false)}><X size={14} /></Tool></div></div>}

      <div className="office-writing-area">
        {outlineOpen && <aside className="office-outline" aria-label="文档大纲"><span className="office-section-label">文档大纲</span>{headings.length ? headings.map((heading, index) => <button key={`${heading.pos}-${index}`} style={{ paddingLeft: `${12 + (heading.level - 1) * 12}px` }} onClick={() => editor.chain().focus().setTextSelection(heading.pos + 1).scrollIntoView().run()} title={heading.text}>{heading.text}</button>) : <p>暂无标题</p>}</aside>}
        <div className="office-canvas" style={{ '--office-zoom': zoom / 100 } as CSSProperties}>
          <article className={`office-paper${document.kind === 'spreadsheet' ? ' office-paper--wide' : ''}`}>
            <input className="office-document-title" aria-label="文档标题" maxLength={200} placeholder="未命名文档" value={document.title} onChange={event => update({ title: event.target.value })} />
            <div className="office-paper-divider" />
            <div className={source !== null ? 'office-rich-hidden' : ''}><EditorContent editor={editor} /></div>
            {source !== null && <textarea className="office-markdown-source" aria-label="Markdown 源码" value={source} onChange={event => { setSource(event.target.value); sourceInputRef.current = true; try { editor.commands.setContent(event.target.value, { contentType: 'markdown' }); } finally { sourceInputRef.current = false; } }} spellCheck={false} />}
            {editor.isEmpty && source === null && <div className="office-empty-hints"><button onClick={() => onAsk('请帮我为当前文档拟一个清晰的大纲，并写一段开场。')}><Sparkles size={15} />生成大纲</button></div>}
          </article>
        </div>
      </div>

      {source === null && <BubbleMenu editor={editor} options={BUBBLE_OPTIONS}><div className="office-bubble"><button onClick={() => onAsk('请润色选中的文字，使表达自然流畅、简洁专业，保留原意。')}><Sparkles size={14} />AI 润色</button><span /><Tool label="加粗选中文字" active={editor.isActive('bold')} onClick={() => editor.chain().focus().toggleBold().run()}><Bold size={14} /></Tool><Tool label="高亮选中文字" active={editor.isActive('highlight')} onClick={() => editor.chain().focus().toggleHighlight().run()}><Highlighter size={14} /></Tool><button onClick={() => onAsk('请用更简洁的语言改写选中的内容，保留关键信息。')}>精简</button><button onClick={() => onAsk('请将选中的内容翻译成自然、专业的英文。')}>翻译</button></div></BubbleMenu>}

      {slash && <div className="office-slash" role="listbox" aria-label="插入内容" style={{ left: Math.max(8, slash.x), top: Math.max(50, slash.y) }}><span className="office-section-label">插入内容</span>{slashItems.length ? slashItems.map((item, index) => <button role="option" aria-selected={index === slash.index} key={item.id} className={index === slash.index ? 'is-active' : ''} onMouseDown={event => event.preventDefault()} onClick={() => runSlash(item.id)}><item.icon size={18} /><span><strong>{item.name}</strong></span></button>) : <p>没有找到匹配的内容</p>}</div>}

      <footer className="office-editor-status"><span><span className="office-live-dot" />{characterCount.toLocaleString()} 字符<span className="office-status-separator">·</span>约 {Math.max(1, Math.ceil(Math.max(characterCount / 400, wordCount / 200)))} 分钟阅读</span><div><select aria-label="文档缩放" value={zoom} onChange={event => setZoom(Number(event.target.value))}>{[80, 90, 100, 110, 125].map(value => <option key={value} value={value}>{value}%</option>)}</select></div></footer>
    </div>
  );
}
