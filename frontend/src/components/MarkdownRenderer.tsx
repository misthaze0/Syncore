/**
 * Markdown 渲染器 —— 支持 GFM 表格 / 代码块 / 列表等
 *
 * 代码块额外提供：
 * - 语言标签（从 className 中解析 language-xxx；没标语言的块交给
 *   rehype-highlight 的 detect 猜一个 —— 模型经常忘了写围栏语言）
 * - 语法高亮（rehype-highlight / highlight.js，token 配色见 styles/hljs.css，
 *   明暗两套跟着 data-theme 走）
 * - 复制按钮（零依赖，调用 navigator.clipboard）
 */

import { Children, isValidElement, memo, useCallback, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeHighlight from 'rehype-highlight';
import remarkGfm from 'remark-gfm';

import { openWebLink } from '@/stores/artifactStore';
import { toast } from '@/stores/toastStore';

interface MarkdownRendererProps {
  content: string;
}

const PLUGINS = [remarkGfm];

const REHYPE_PLUGINS = [[rehypeHighlight, { detect: true }] as const];

function detectLanguage(className?: string): string | null {
  if (!className) return null;
  const match = /language-([\w+-]+)/.exec(className);
  return match?.[1] ?? null;
}

/**
 * 从 React 子树里提取纯文本。
 *
 * 高亮之后 code 的 children 不再是字符串，而是一层层 `<span class="hljs-*">` ——
 * 复制按钮要的是用户看到的原文，必须递归取文本节点（否则高亮一开复制就变空串）。
 */
function nodeText(node: React.ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join('');
  if (typeof node === 'object' && 'props' in node) {
    return nodeText((node as React.ReactElement<{ children?: React.ReactNode }>).props.children);
  }
  return '';
}

function CodeBlock({ children, className }: { children: React.ReactNode; className?: string }) {
  const [copied, setCopied] = useState(false);
  const language = detectLanguage(className);

  const handleCopy = useCallback(() => {
    const raw = nodeText(children);
    void navigator.clipboard.writeText(raw)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 2000);
      })
      .catch(() => toast('复制失败，请选择代码后手动复制', 'error'));
  }, [children]);

  return (
    <div className="md-codeblock">
      <div className="md-codeblock__head">
        {language && <span className="md-codeblock__lang">{language}</span>}
        <button type="button" className="md-codeblock__copy" onClick={handleCopy}>
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre className={className}>
        <code className={className}>{children}</code>
      </pre>
    </div>
  );
}

const COMPONENTS = {
  a: ({ href, onClick, ...rest }: React.AnchorHTMLAttributes<HTMLAnchorElement>) => {
    // 正文里的外部链接（模型常把搜索来源写成 markdown 链接）：桌面端点击改在右侧栏
    // 浏览器页签打开，Web 端保持新标签页。锚点(#)/mailto 等非 http 链接不拦截。
    const isExternal = !!href && /^https?:\/\//i.test(href);
    return (
      <a
        {...rest}
        href={href}
        target="_blank"
        rel="noreferrer noopener"
        onClick={(e) => {
          onClick?.(e);
          if (isExternal && !e.defaultPrevented) {
            e.preventDefault();
            openWebLink(href as string);
          }
        }}
      />
    );
  },
  pre: ({ children }: { children?: React.ReactNode }) => {
    // 自定义 code 渲染器会让子元素的 type 成为组件函数，不能按字符串判断。
    const code = Children.toArray(children).find((child) =>
      isValidElement<{ className?: string; children?: React.ReactNode }>(child),
    );
    if (isValidElement<{ className?: string; children?: React.ReactNode }>(code)) {
      const codeProps = code.props;
      return <CodeBlock className={codeProps.className}>{codeProps.children}</CodeBlock>;
    }
    return <pre>{children}</pre>;
  },
  code: ({ children, className }: { children?: React.ReactNode; className?: string }) => {
    // 行内代码走默认样式；代码块已在 pre 中处理
    return <code className={className}>{children}</code>;
  },
  table: ({ children }: { children?: React.ReactNode }) => (
    <div className="md-table-scroll" role="region" aria-label="表格" tabIndex={0}>
      <table>{children}</table>
    </div>
  ),
} as const;

export const MarkdownRenderer = memo(function MarkdownRenderer({
  content,
}: MarkdownRendererProps) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={PLUGINS}
        rehypePlugins={REHYPE_PLUGINS as never}
        components={COMPONENTS}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});
