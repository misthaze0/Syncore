import StarterKit from '@tiptap/starter-kit';
import Placeholder from '@tiptap/extension-placeholder';
import { TableKit } from '@tiptap/extension-table';
import TextAlign from '@tiptap/extension-text-align';
import TaskList from '@tiptap/extension-task-list';
import TaskItem from '@tiptap/extension-task-item';
import Highlight from '@tiptap/extension-highlight';
import { Markdown } from '@tiptap/markdown';

export function officeEditorExtensions(placeholder = '在这里写下你的想法，输入 / 插入内容…') {
  return [
    StarterKit.configure({ heading: { levels: [1, 2, 3] }, link: { openOnClick: false, autolink: true } }),
    Placeholder.configure({ placeholder }),
    TableKit.configure({ table: { resizable: true } }),
    TextAlign.configure({ types: ['heading', 'paragraph'] }),
    TaskList, TaskItem.configure({ nested: true }), Highlight, Markdown,
  ];
}
