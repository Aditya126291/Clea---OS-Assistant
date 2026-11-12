import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface MarkdownContentProps {
  content: string;
}

export function MarkdownContent({ content }: MarkdownContentProps) {
  return (
    <div className="clea-markdown space-y-2.5 text-sm leading-relaxed text-[var(--text)]">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p className="whitespace-pre-wrap break-words">{children}</p>,
          ul: ({ children }) => <ul className="list-disc space-y-1 pl-5">{children}</ul>,
          ol: ({ children }) => <ol className="list-decimal space-y-1 pl-5">{children}</ol>,
          h1: ({ children }) => <h1 className="text-base font-semibold">{children}</h1>,
          h2: ({ children }) => <h2 className="text-sm font-semibold">{children}</h2>,
          h3: ({ children }) => <h3 className="text-sm font-medium">{children}</h3>,
          code: ({ className, children, ...props }) => (
            <code
              className={
                className
                  ? `font-mono text-xs ${className}`
                  : "rounded bg-[var(--bg-subtle)] px-1.5 py-0.5 font-mono text-[12px]"
              }
              {...props}
            >
              {children}
            </code>
          ),
          pre: ({ children }) => (
            <pre className="my-2 overflow-x-auto rounded-lg border border-[var(--border)] bg-[var(--bg-subtle)] p-3 font-mono text-xs [&_code]:bg-transparent [&_code]:p-0">
              {children}
            </pre>
          ),
          table: ({ children }) => (
            <div className="my-2 overflow-x-auto">
              <table className="w-full border-collapse text-left text-xs">{children}</table>
            </div>
          ),
          th: ({ children }) => (
            <th className="border border-[var(--border)] bg-[var(--bg-subtle)] px-2.5 py-1.5 font-medium">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="border border-[var(--border)] px-2.5 py-1.5">{children}</td>
          ),
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-[var(--border-strong)] pl-3 text-[var(--text-muted)]">
              {children}
            </blockquote>
          ),
          a: ({ href, children }) => (
            <a
              href={href}
              target="_blank"
              rel="noreferrer noopener"
              className="underline decoration-[var(--border-strong)] underline-offset-2 hover:text-[var(--text-muted)]"
            >
              {children}
            </a>
          ),
        }}
      >
        {content}
      </Markdown>
    </div>
  );
}
