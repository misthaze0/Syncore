import { useEffect } from 'react';
import { EditorContent, useEditor } from '@tiptap/react';
import { officeEditorExtensions } from '@/components/office/editorExtensions';

export function OfficePreview({ content }: { content: string }) {
  const editor = useEditor({ extensions: officeEditorExtensions(''), content, editable: false, editorProps: { attributes: { class: 'office-prose office-prose--preview', 'aria-label': '文档预览' } } });
  useEffect(() => { if (editor) editor.commands.setContent(content, { emitUpdate: false }); }, [content, editor]);
  return <EditorContent editor={editor} />;
}
