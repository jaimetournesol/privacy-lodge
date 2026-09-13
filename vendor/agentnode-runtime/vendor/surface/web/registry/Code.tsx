import { memo, useEffect, useState } from 'react';

let shikiPromise: Promise<typeof import('shiki')> | null = null;
const loadShiki = () => (shikiPromise ??= import('shiki'));

export const CodeBlock = memo(
  ({ code, lang, bare }: { code: string; lang?: string; bare?: boolean }) => {
    const [html, setHtml] = useState<string | null>(null);
    const [copied, setCopied] = useState(false);

    useEffect(() => {
      let alive = true;
      setHtml(null);
      loadShiki()
        .then(({ codeToHtml }) =>
          codeToHtml(code, { lang: (lang ?? 'text') as never, theme: 'github-dark-default' }).catch(() =>
            codeToHtml(code, { lang: 'text', theme: 'github-dark-default' }),
          ),
        )
        .then((out) => alive && setHtml(out))
        .catch(() => alive && setHtml(null));
      return () => {
        alive = false;
      };
    }, [code, lang]);

    const copy = () => {
      void navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    };

    return (
      <div className={`codeblock ${bare ? 'codeblock-bare' : ''}`}>
        <div className="codeblock-bar">
          <span className="mono muted">{lang ?? 'text'}</span>
          <button className="icon-btn" title="Copy" onClick={copy}>
            {copied ? '✓' : '⧉'}
          </button>
        </div>
        {html ? (
          <div className="codeblock-html" dangerouslySetInnerHTML={{ __html: html }} />
        ) : (
          <pre className="codeblock-plain mono">{code}</pre>
        )}
      </div>
    );
  },
);
