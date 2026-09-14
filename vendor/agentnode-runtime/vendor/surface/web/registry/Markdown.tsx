import { memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { CodeBlock } from './Code';

export const Md = memo(({ text }: { text: string }) => (
  <ReactMarkdown
    remarkPlugins={[remarkGfm]}
    components={{
      code({ className, children, ...rest }) {
        const lang = /language-(\w+)/.exec(className ?? '')?.[1];
        const value = String(children).replace(/\n$/, '');
        const inline = !className && !value.includes('\n');
        if (inline) {
          return (
            <code className="inline-code" {...rest}>
              {children}
            </code>
          );
        }
        return <CodeBlock code={value} lang={lang} bare />;
      },
      a({ children, ...rest }) {
        return (
          <a {...rest} target="_blank" rel="noreferrer">
            {children}
          </a>
        );
      },
    }}
  >
    {text}
  </ReactMarkdown>
));
