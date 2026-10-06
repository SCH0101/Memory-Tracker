// Minimal Markdown renderer — no external dep. Covers what the chat reply
// actually produces: GFM tables, bullets / numbered lists, headings,
// **bold**, *italic*, `inline code`, paragraphs. Anything else falls through
// as plain text. Designed for the narrow Ask panel — tables get horizontal
// scroll, code/text scale down to fit ~440px.
import type { ReactNode } from "react";
import { sourcePdfUrl } from "../lib/api";

type Block =
  | { type: "table"; header: string[]; rows: string[][] }
  | { type: "list"; ordered: boolean; items: string[] }
  | { type: "heading"; level: number; text: string }
  | { type: "blockquote"; text: string }
  | { type: "code"; text: string; language?: string }
  | { type: "paragraph"; text: string }
  | { type: "hr" }
  | { type: "blank" };

interface CitationSource {
  id?: string;
  provider?: string;
  broker?: string;
  date?: string;
  filename?: string;
  page?: number | null;
  content_type?: string;
  href?: string;
}

type SourceMap = Record<string, CitationSource>;

function basename(path?: string | null): string {
  return (path || "").split(/[\\/]/).filter(Boolean).pop() || "";
}

function providerLabel(source?: CitationSource): string {
  const clean = (source?.provider || source?.broker || "").trim();
  if (clean && clean.toUpperCase() !== "MEMORY") return clean;
  const file = basename(source?.filename);
  const m = file.match(/\[([^\]]{1,20})\]/);
  if (m?.[1]?.trim()) return m[1].trim();
  if (/expert[\s_-]*call/i.test(file)) return "Expert Call";
  return "Source";
}

function sourceTooltip(source?: CitationSource): string {
  if (!source) return "";
  const file = basename(source.filename);
  const readableFile = /^(?:doc:)?sha256:/i.test(file) ? "" : file;
  return [readableFile, source.date || "", source.page ? `p.${source.page}` : ""]
    .filter(Boolean)
    .join(" · ") || "Open cited source";
}

function sourceHref(source?: CitationSource): string {
  if (source?.href) return source.href;
  const filename = (source?.filename || "").trim();
  if (!filename) return "";
  if (/^(?:doc:|sha256:)/i.test(filename)) {
    const id = encodeURIComponent(filename);
    return `/api/research-documents/${id}/pages/${source?.page && source.page > 0 ? source.page : 1}`;
  }
  return sourcePdfUrl(filename, source?.page);
}

function renderSourceBox(source: CitationSource, key: number, explicitHref?: string): ReactNode {
  const href = explicitHref || sourceHref(source);
  const label = providerLabel(source);
  const box = (
    <span className={`mc-cite-badge ${href ? "" : "mc-cite-badge--unavailable"}`} title={sourceTooltip(source)}>
      {label}
    </span>
  );
  return href ? (
    <a key={key} href={href} target="_blank" rel="noreferrer" className="mc-cite-link" aria-label={`Open ${label} source: ${sourceTooltip(source)}`}>
      {box}
    </a>
  ) : <span key={key}>{box}</span>;
}

function cleanMarkdownHref(href: string): string {
  const clean = href.trim();
  if (clean.startsWith("<") && clean.endsWith(">")) return clean.slice(1, -1);
  return clean;
}

function isSafeHref(href: string): boolean {
  return /^(https?:|mailto:|\/)/i.test(href);
}

function splitTableRow(line: string): string[] {
  // " | a | b | " → ["a", "b"]
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

function parse(text: string): Block[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  const isListItem = (l: string) => /^\s{0,3}(?:[-*+]\s+|\d+[.)]\s+)/.test(l);
  const isOrderedListItem = (l: string) => /^\s{0,3}\d+[.)]\s+/.test(l);
  const stripListMarker = (l: string) => l.replace(/^\s{0,3}(?:[-*+]|\d+[.)])\s+/, "");
  const isHeading = (l: string) => /^\s{0,3}#{1,6}\s+/.test(l);
  const isTableRow = (l: string) => /^\s*\|.*\|\s*$/.test(l);
  const isTableSep = (l: string) => /^\s*\|?\s*:?-{2,}:?(\s*\|\s*:?-{2,}:?)+\s*\|?\s*$/.test(l);
  const isHr = (l: string) => /^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(l);
  const isFence = (l: string) => /^\s{0,3}(?:```|~~~)/.test(l);
  const isBlockquote = (l: string) => /^\s{0,3}>\s?/.test(l);
  const isSetextHeading = (l: string) => /^\s{0,3}(?:=+|-+)\s*$/.test(l);

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    // Fenced code blocks, usually formulas or snippets from the ES answer.
    const fence = line.match(/^\s{0,3}(```+|~~~+)\s*([A-Za-z0-9_-]*)\s*$/);
    if (fence) {
      const marker = fence[1][0];
      const minLength = fence[1].length;
      const closeRe = new RegExp(`^\\s{0,3}${marker}{${minLength},}\\s*$`);
      const language = fence[2] || undefined;
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !closeRe.test(lines[i])) {
        codeLines.push(lines[i]);
        i++;
      }
      if (i < lines.length) i++;
      blocks.push({ type: "code", text: codeLines.join("\n"), language });
      continue;
    }

    // Setext headings:
    // Heading text
    // ------------
    if (line.trim() && i + 1 < lines.length && isSetextHeading(lines[i + 1])) {
      blocks.push({ type: "heading", level: lines[i + 1].trim().startsWith("=") ? 1 : 2, text: line.trim() });
      i += 2;
      continue;
    }

    // Standalone inline-code formulas are easier to audit as formula blocks.
    const standaloneCode = line.trim().match(/^(`{1,3})([^`].*?)\1$/);
    if (standaloneCode) {
      blocks.push({ type: "code", text: standaloneCode[2].trim() });
      i++;
      continue;
    }

    // Blockquotes. We keep citation/link inline rendering intact inside them.
    if (isBlockquote(line)) {
      const quote: string[] = [];
      while (i < lines.length && isBlockquote(lines[i])) {
        quote.push(lines[i].replace(/^\s{0,3}>\s?/, ""));
        i++;
      }
      blocks.push({ type: "blockquote", text: quote.join("\n").trim() });
      continue;
    }

    // Tables: header row + separator + body rows.
    if (isTableRow(line) && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const header = splitTableRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && isTableRow(lines[i])) {
        rows.push(splitTableRow(lines[i]));
        i++;
      }
      blocks.push({ type: "table", header, rows });
      continue;
    }

    // Headings
    const hm = line.match(/^\s{0,3}(#{1,6})\s+(.+)$/);
    if (hm) {
      blocks.push({ type: "heading", level: hm[1].length, text: hm[2].trim() });
      i++;
      continue;
    }

    // Horizontal rule
    if (isHr(line)) {
      blocks.push({ type: "hr" });
      i++;
      continue;
    }

    // Lists (one block per contiguous run of same-type items)
    if (isListItem(line)) {
      const ordered = isOrderedListItem(line);
      const items: string[] = [];
      while (i < lines.length && isListItem(lines[i]) && isOrderedListItem(lines[i]) === ordered) {
        const itemLines = [stripListMarker(lines[i])];
        i++;
        while (
          i < lines.length &&
          lines[i].trim() !== "" &&
          !isListItem(lines[i]) &&
          !isHeading(lines[i]) &&
          !isHr(lines[i]) &&
          !isBlockquote(lines[i]) &&
          !isFence(lines[i]) &&
          !(isTableRow(lines[i]) && i + 1 < lines.length && isTableSep(lines[i + 1]))
        ) {
          itemLines.push(lines[i].replace(/^\s{2,}/, "").trimEnd());
          i++;
        }
        items.push(itemLines.join("\n").trim());
        if (i + 1 < lines.length && lines[i]?.trim() === "" && isListItem(lines[i + 1])) i++;
      }
      blocks.push({ type: "list", ordered, items });
      continue;
    }

    // Blank lines separate paragraphs.
    if (line.trim() === "") {
      blocks.push({ type: "blank" });
      i++;
      continue;
    }

    // Paragraph (collect until blank / structural line)
    const para: string[] = [line];
    i++;
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !isHeading(lines[i]) &&
      !isHr(lines[i]) &&
      !isListItem(lines[i]) &&
      !isBlockquote(lines[i]) &&
      !isFence(lines[i]) &&
      !(isTableRow(lines[i]) && i + 1 < lines.length && isTableSep(lines[i + 1]))
    ) {
      para.push(lines[i]);
      i++;
    }
    blocks.push({ type: "paragraph", text: para.join("\n") });
  }
  return blocks;
}

// Inline: **bold**, `code`, *italic*, _italic_. Order matters — bold first so
// the `*` inside `**` doesn't get eaten by italic.
function inline(text: string, sourceMap?: SourceMap): ReactNode[] {
  text = text.replace(/(`{2,3})([^`\n]+?)\1/g, "`$2`");
  const re =
    /(!?\[([^\]]*)\]\((<[^>]+>|[^)\s]+)(?:\s+"([^"]*)")?\))|(\[src:((?:S|URL)\d+)\])|(\[(S\d+)\])|(\*\*([^*]+)\*\*)|(~~([^~]+)~~)|(`([^`]+)`)|(\*([^*\s][^*]*?)\*)|(_([^_\s][^_]*?)_)|((?:https?:\/\/|mailto:)[^\s<]+)/g;
  const out: ReactNode[] = [];
  let last = 0;
  let key = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1]) {
      const label = m[2] || "source";
      const href = cleanMarkdownHref(m[3] || "");
      const title = m[4] || label;
      const isImage = m[1].startsWith("!");
      const urlSource = href.match(/\/api\/research-documents\/([^/]+)\/pages\/(\d+)/i);
      let sourceId = "";
      try { sourceId = urlSource ? decodeURIComponent(urlSource[1]) : ""; } catch { sourceId = ""; }
      const markedSource = label.match(/^(\d{8})\s*\[([^\]]+)\]\s*(.*)$/);
      const filename = markedSource?.[3]?.trim() || sourceId;
      const indexedSource = sourceMap?.[sourceId];
      const source = urlSource && filename ? {
        ...(indexedSource || {}),
        id: `url-${key}`,
        broker: indexedSource?.broker || markedSource?.[2] || "",
        date: indexedSource?.date || markedSource?.[1] || "",
        filename: indexedSource?.filename || filename,
        page: Number(urlSource[2]),
        href,
      } : undefined;
      if (source) {
        out.push(renderSourceBox(source, key++, source.href));
        last = m.index + m[0].length;
        continue;
      }
      if (!isSafeHref(href)) {
        out.push(label);
        last = m.index + m[0].length;
        continue;
      }
      if (isImage) {
        out.push(
          <img
            key={key++}
            src={href}
            alt={label}
            title={title}
            className="my-2 max-h-64 max-w-full rounded-md border object-contain"
          />
        );
        last = m.index + m[0].length;
        continue;
      }
      const sourceLike = /\/api\/(?:sharepoint\/pdf|charts\/source-pdf|charts\/rag-asset)/.test(href);
      out.push(
        <a
          key={key++}
          href={href}
          target="_blank"
          rel="noreferrer"
          className={sourceLike ? "mc-cite-link" : "text-primary underline underline-offset-2"}
          title={title}
        >
          {sourceLike ? (
            <span className="mc-cite-chip" tabIndex={0} data-cite={title}>
              {label}
            </span>
          ) : (
            label
          )}
        </a>
      );
    } else if (m[5]) {
      const src = sourceMap?.[m[6]];
      if (src) out.push(renderSourceBox(src, key++));
      else out.push(m[5]);
    } else if (m[7]) {
      const src = sourceMap?.[m[8]];
      if (!src) {
        out.push(m[7]);
      } else {
        const href = sourceHref(src);
        out.push(renderSourceBox(src, key++));
        last = m.index + m[0].length;
        continue;
      }
    } else if (m[9]) out.push(<strong key={key++}>{inline(m[10], sourceMap)}</strong>);
    else if (m[11]) out.push(<del key={key++}>{inline(m[12], sourceMap)}</del>);
    else if (m[13])
      out.push(
        <code key={key++} className="rounded bg-muted/70 px-1.5 py-0.5 text-[11px] font-mono">
          {m[14]}
        </code>
      );
    else if (m[15]) out.push(<em key={key++}>{inline(m[16], sourceMap)}</em>);
    else if (m[17]) out.push(<em key={key++}>{inline(m[18], sourceMap)}</em>);
    else if (m[19]) {
      const href = cleanMarkdownHref(m[19]);
      if (isSafeHref(href)) {
        out.push(
          <a key={key++} href={href} target="_blank" rel="noreferrer" className="text-primary underline underline-offset-2">
            {href}
          </a>
        );
      } else {
        out.push(href);
      }
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function MarkdownBlock({
  text,
  sources,
}: {
  text: string;
  sources?: Record<string, CitationSource> | CitationSource[];
}) {
  const sourceMap: SourceMap = {};
  if (Array.isArray(sources)) {
    sources.forEach((s, i) => { sourceMap[s.id || `S${i + 1}`] = s; });
  } else if (sources) {
    Object.assign(sourceMap, sources);
  }
  // ES citations can have nested brackets in their labels, including
  // [date [provider] title](document-page-url). Normalize the complete link
  // before the inline parser sees its inner brackets.
  let urlIndex = 1;
  const normalizedText = text.replace(
    /\[([^\n]{0,250}?)\]\((https?:\/\/[^\s)]+)\)/g,
    (whole: string, label: string, rawHref: string) => {
      let href: URL;
      try { href = new URL(rawHref); } catch { return whole; }
      const match = href.pathname.match(/\/api\/research-documents\/([^/]+)\/pages\/(\d+)/i);
      if (!match) return whole;
      let documentId: string;
      try { documentId = decodeURIComponent(match[1]); } catch { return whole; }
      const id = `URL${urlIndex++}`;
      const datedLabel = label.match(/^(\d{8})\s+\[([^\]]+)\]\s*(.*)$/);
      const bracketedProvider = datedLabel?.[2]?.trim() || label.match(/\[([^\]]{1,60})\]/)?.[1]?.trim() || "";
      // Some ES document titles encode the apostrophe in "Jason's Chips"
      // as a closing bracket: "[Jason]s Chips".
      const provider = bracketedProvider === "Jason" && /^s Chips\b/i.test(datedLabel?.[3] || "")
        ? "Jason's Chips"
        : bracketedProvider;
      const date = datedLabel?.[1]
        ? `${datedLabel[1].slice(0, 4)}-${datedLabel[1].slice(4, 6)}-${datedLabel[1].slice(6, 8)}`
        : "";
      sourceMap[id] = {
        ...(sourceMap[documentId] || {}),
        id,
        broker: sourceMap[documentId]?.broker || provider,
        date: sourceMap[documentId]?.date || date,
        filename: sourceMap[documentId]?.filename || datedLabel?.[3]?.trim() || label.trim(),
        page: Number(match[2]),
        href: rawHref,
      };
      return `[src:${id}]`;
    },
  ).replace(/([^\n])\n+\s*(?=\[src:(?:S|URL)\d+\])/g, "$1 ");
  const parsedBlocks = parse(normalizedText);
  // Generated summaries sometimes put every citation on its own paragraph.
  // Fold citation-only paragraphs into the preceding sentence/bullet so the
  // boxes stay inline with the claim they support.
  const blocks: Block[] = [];
  for (const block of parsedBlocks) {
    const citationOnly = block.type === "paragraph" && /^\s*(?:\[src:(?:S|URL)\d+\]\s*)+$/.test(block.text);
    if (citationOnly && blocks.length) {
      const previous = blocks[blocks.length - 1];
      if (previous.type === "paragraph") previous.text += ` ${block.text.trim()}`;
      else if (previous.type === "list" && previous.items.length) previous.items[previous.items.length - 1] += ` ${block.text.trim()}`;
      else blocks.push(block);
    } else {
      blocks.push(block);
    }
  }
  return (
    <div className="space-y-3 leading-7">
      {blocks.map((b, i) => {
        switch (b.type) {
          case "blank":
            return null;
          case "hr":
            return <hr key={i} className="my-4 border-border/80" />;
          case "heading": {
            const cls =
              b.level <= 2
                ? "text-sm font-bold mt-3 first:mt-0"
                : b.level === 3
                ? "text-[13px] font-semibold mt-2 first:mt-0"
                : "text-xs font-semibold uppercase text-muted-foreground mt-2 first:mt-0";
            return (
              <div key={i} className={cls}>
                {inline(b.text, sourceMap)}
              </div>
            );
          }
          case "blockquote":
            return (
              <blockquote key={i} className="border-l-2 border-primary/30 pl-3 text-muted-foreground">
                <p className="whitespace-pre-wrap leading-7">{inline(b.text, sourceMap)}</p>
              </blockquote>
            );
          case "code":
            return (
              <pre
                key={i}
                className="my-3 overflow-x-auto rounded-md border bg-muted/30 px-3 py-2 text-xs leading-relaxed"
              >
                <code className="whitespace-pre font-mono">{b.text}</code>
              </pre>
            );
          case "list": {
            const ListTag = b.ordered ? "ol" : "ul";
            return (
              <ListTag
                key={i}
                className={`pl-5 space-y-1.5 ${b.ordered ? "list-decimal" : "list-disc"} marker:text-muted-foreground`}
              >
                {b.items.map((it, j) => (
                  <li key={j} className="pl-0.5 leading-7">
                    {inline(it, sourceMap)}
                  </li>
                ))}
              </ListTag>
            );
          }
          case "table":
            return (
              <div key={i} className="my-3 -mx-1 overflow-x-auto rounded-md border bg-background/40">
                <table className="w-full border-collapse text-[11px] leading-normal">
                  <thead>
                    <tr className="border-b bg-muted/40">
                      {b.header.map((h, j) => (
                        <th key={j} className="px-2.5 py-1.5 text-left font-semibold whitespace-nowrap">
                          {inline(h, sourceMap)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {b.rows.map((r, j) => (
                      <tr key={j} className="border-b last:border-0 align-top">
                        {b.header.map((_, k) => (
                          <td key={k} className="px-2.5 py-1.5 whitespace-nowrap tabular-nums">
                            {inline(r[k] ?? "", sourceMap)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          case "paragraph":
            return (
              <p key={i} className="whitespace-pre-wrap leading-7">
                {inline(b.text, sourceMap)}
              </p>
            );
        }
      })}
    </div>
  );
}
