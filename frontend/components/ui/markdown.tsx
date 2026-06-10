import { cn } from "@/lib/utils"
import type { EntityMatch } from "@/lib/entity-lookup"
import { marked } from "marked"
import { createContext, memo, useContext, useId, useMemo } from "react"
import ReactMarkdown, { Components } from "react-markdown"
import remarkBreaks from "remark-breaks"
import remarkGfm from "remark-gfm"
import { CodeBlock, CodeBlockCode } from "./code-block"

/**
 * Wires bolded entity names in agent markdown to the evidence graph + detail
 * panel. Provided once near the app root (EntityResolverApp); the strong
 * renderer below consults it. Null context (or no match) = plain bold, so the
 * markdown component stays drop-in everywhere else.
 */
export type EntityInteraction = {
  /** Normalized known-entity lookup; null when the text isn't a known entity. */
  lookup: (name: string) => EntityMatch | null
  /** Click: highlight/center the node and open the right-hand detail panel. */
  onEntityClick: (match: EntityMatch) => void
}

export const EntityInteractionContext = createContext<EntityInteraction | null>(
  null
)

/** Flatten a strong element's children to plain text (entity names are plain). */
function childrenToText(children: React.ReactNode): string {
  if (typeof children === "string") return children
  if (Array.isArray(children)) {
    return children.every((c) => typeof c === "string") ? children.join("") : ""
  }
  return ""
}

export type MarkdownProps = {
  children: string
  id?: string
  className?: string
  components?: Partial<Components>
}

function parseMarkdownIntoBlocks(markdown: string): string[] {
  const tokens = marked.lexer(markdown)
  return tokens.map((token) => token.raw)
}

function extractLanguage(className?: string): string {
  if (!className) return "plaintext"
  const match = className.match(/language-(\w+)/)
  return match ? match[1] : "plaintext"
}

// Styled element renderers on the lmcanvas token set (light default). Sizes are
// em-relative so headings stay proportional whether they appear in a full
// narrative (text-sm) or a compact claim row (text-xs). Body text intentionally
// inherits color/size from the parent wrapper so each call site keeps control;
// only headings, emphasis, links, and code get explicit accents.
const INITIAL_COMPONENTS: Partial<Components> = {
  h1: function H1({ node, className, children, ...props }) {
    void node
    return (
      <h1
        className={cn(
          "mt-4 mb-2 text-[1.3em] font-semibold leading-tight text-foreground first:mt-0",
          className
        )}
        {...props}
      >
        {children}
      </h1>
    )
  },
  h2: function H2({ node, className, children, ...props }) {
    void node
    return (
      <h2
        className={cn(
          "mt-4 mb-2 text-[1.18em] font-semibold leading-tight text-foreground first:mt-0",
          className
        )}
        {...props}
      >
        {children}
      </h2>
    )
  },
  h3: function H3({ node, className, children, ...props }) {
    void node
    return (
      <h3
        className={cn(
          "mt-3 mb-1.5 text-[1.08em] font-semibold leading-snug text-foreground first:mt-0",
          className
        )}
        {...props}
      >
        {children}
      </h3>
    )
  },
  h4: function H4({ node, className, children, ...props }) {
    void node
    return (
      <h4
        className={cn(
          "mt-3 mb-1 text-[1em] font-semibold text-foreground first:mt-0",
          className
        )}
        {...props}
      >
        {children}
      </h4>
    )
  },
  h5: function H5({ node, className, children, ...props }) {
    void node
    return (
      <h5
        className={cn(
          "mt-2.5 mb-1 text-[0.95em] font-semibold uppercase tracking-wide text-foreground/80 first:mt-0",
          className
        )}
        {...props}
      >
        {children}
      </h5>
    )
  },
  h6: function H6({ node, className, children, ...props }) {
    void node
    return (
      <h6
        className={cn(
          "mt-2.5 mb-1 text-[0.9em] font-semibold uppercase tracking-wide text-muted-foreground first:mt-0",
          className
        )}
        {...props}
      >
        {children}
      </h6>
    )
  },
  p: function Paragraph({ node, className, children, ...props }) {
    void node
    return (
      <p
        className={cn("my-2 leading-relaxed first:mt-0 last:mb-0", className)}
        {...props}
      >
        {children}
      </p>
    )
  },
  ul: function UnorderedList({ node, className, children, ...props }) {
    void node
    return (
      <ul
        className={cn(
          "my-2 list-disc space-y-1 pl-5 marker:text-muted-foreground first:mt-0 last:mb-0",
          className
        )}
        {...props}
      >
        {children}
      </ul>
    )
  },
  ol: function OrderedList({ node, className, children, ...props }) {
    void node
    return (
      <ol
        className={cn(
          "my-2 list-decimal space-y-1 pl-5 marker:text-muted-foreground first:mt-0 last:mb-0",
          className
        )}
        {...props}
      >
        {children}
      </ol>
    )
  },
  li: function ListItem({ node, className, children, ...props }) {
    void node
    return (
      <li className={cn("leading-relaxed [&>ul]:mt-1 [&>ol]:mt-1", className)} {...props}>
        {children}
      </li>
    )
  },
  strong: function Strong({ node, className, children, ...props }) {
    void node
    const interaction = useContext(EntityInteractionContext)
    const text = childrenToText(children)
    const match = interaction && text ? interaction.lookup(text) : null
    if (interaction && match) {
      return (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation()
            interaction.onEntityClick(match)
          }}
          title={`Show ${match.name} on the graph and open its details`}
          className={cn(
            "cursor-pointer font-semibold text-foreground underline decoration-foreground/35 decoration-dotted underline-offset-2 transition-colors hover:decoration-foreground hover:decoration-solid",
            className
          )}
        >
          {children}
        </button>
      )
    }
    return (
      <strong className={cn("font-semibold text-foreground", className)} {...props}>
        {children}
      </strong>
    )
  },
  em: function Emphasis({ node, className, children, ...props }) {
    void node
    return (
      <em className={cn("italic", className)} {...props}>
        {children}
      </em>
    )
  },
  a: function Anchor({ node, className, children, ...props }) {
    void node
    return (
      <a
        className={cn(
          "font-medium text-accent-brand underline underline-offset-2 transition-opacity hover:opacity-80",
          className
        )}
        target="_blank"
        rel="noopener noreferrer"
        {...props}
      >
        {children}
      </a>
    )
  },
  blockquote: function Blockquote({ node, className, children, ...props }) {
    void node
    return (
      <blockquote
        className={cn(
          "my-2 border-l-2 border-border pl-3 italic text-muted-foreground first:mt-0 last:mb-0",
          className
        )}
        {...props}
      >
        {children}
      </blockquote>
    )
  },
  hr: function HorizontalRule({ node, className, ...props }) {
    void node
    return <hr className={cn("my-3 border-border", className)} {...props} />
  },
  table: function Table({ node, className, children, ...props }) {
    void node
    return (
      <div className="my-2 max-w-full overflow-x-auto">
        <table
          className={cn(
            "w-full border-collapse text-left text-[0.9em]",
            className
          )}
          {...props}
        >
          {children}
        </table>
      </div>
    )
  },
  th: function TableHeader({ node, className, children, ...props }) {
    void node
    return (
      <th
        className={cn(
          "border border-border bg-muted px-2 py-1 font-semibold text-foreground",
          className
        )}
        {...props}
      >
        {children}
      </th>
    )
  },
  td: function TableCell({ node, className, children, ...props }) {
    void node
    return (
      <td className={cn("border border-border px-2 py-1 align-top", className)} {...props}>
        {children}
      </td>
    )
  },
  code: function CodeComponent({ className, children, ...props }) {
    const isInline =
      !props.node?.position?.start.line ||
      props.node?.position?.start.line === props.node?.position?.end.line

    if (isInline) {
      const { node: _node, ...rest } = props
      void _node
      return (
        <code
          className={cn(
            "rounded bg-muted px-1 py-0.5 font-mono text-[0.85em] text-foreground",
            className
          )}
          {...rest}
        >
          {children}
        </code>
      )
    }

    const language = extractLanguage(className)

    return (
      <CodeBlock className={className}>
        <CodeBlockCode code={children as string} language={language} />
      </CodeBlock>
    )
  },
  pre: function PreComponent({ children }) {
    return <>{children}</>
  },
}

const MemoizedMarkdownBlock = memo(
  function MarkdownBlock({
    content,
    components = INITIAL_COMPONENTS,
  }: {
    content: string
    components?: Partial<Components>
  }) {
    return (
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        components={components}
      >
        {content}
      </ReactMarkdown>
    )
  },
  function propsAreEqual(prevProps, nextProps) {
    return prevProps.content === nextProps.content
  }
)

MemoizedMarkdownBlock.displayName = "MemoizedMarkdownBlock"

function MarkdownComponent({
  children,
  id,
  className,
  components = INITIAL_COMPONENTS,
}: MarkdownProps) {
  const generatedId = useId()
  const blockId = id ?? generatedId
  const blocks = useMemo(() => parseMarkdownIntoBlocks(children), [children])

  return (
    <div className={className}>
      {blocks.map((block, index) => (
        <MemoizedMarkdownBlock
          key={`${blockId}-block-${index}`}
          content={block}
          components={components}
        />
      ))}
    </div>
  )
}

const Markdown = memo(MarkdownComponent)
Markdown.displayName = "Markdown"

export { Markdown }
