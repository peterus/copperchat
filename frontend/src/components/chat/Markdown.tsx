import { memo } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeSanitize from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';

/**
 * Agent text may only embed the stored tool images / renders of a project. Any other URL (external:
 * data exfiltration; other /api/ GETs, e.g. the uncached agent-view render) shows a placeholder.
 * blob: never reaches here: react-markdown's urlTransform and rehype-sanitize strip it.
 */
const MARKDOWN_IMAGE = /^\/api\/projects\/[0-9a-f]{12}\/(?:images|renders)\/(?!\.+$)[\w.-]+$/;
export const isMarkdownImageUrl = (u: string) => MARKDOWN_IMAGE.test(u);

const REMARK_PLUGINS = [remarkGfm];
const REHYPE_PLUGINS = [rehypeSanitize];

/**
 * Raw HTML is never rendered (react-markdown default); rehype-sanitize is defence in depth.
 * Memoized on `text`: a streaming delta to one row must not re-parse every other row.
 */
export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="cc-markdown break-words">
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={{
          a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
          img: ({ node: _node, src, alt }) =>
            typeof src === 'string' && isMarkdownImageUrl(src) ? (
              <img src={src} alt={alt ?? ''} className="max-w-full" />
            ) : (
              <span className="text-[var(--cc-muted)]">[image: {alt || 'external'}]</span>
            ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});
