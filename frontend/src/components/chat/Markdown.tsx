import ReactMarkdown from 'react-markdown';
import rehypeSanitize from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';

/** Same-origin API files and local blobs only: an external image URL in agent output could leak data. */
export const isSafeImageUrl = (u: string) => u.startsWith('/api/') || u.startsWith('blob:');

/** Raw HTML is never rendered (react-markdown default); rehype-sanitize is defence in depth. */
export function Markdown({ text }: { text: string }) {
  return (
    <div className="cc-markdown break-words">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        components={{
          a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
          img: ({ node: _node, src, alt }) =>
            typeof src === 'string' && isSafeImageUrl(src) ? (
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
}
