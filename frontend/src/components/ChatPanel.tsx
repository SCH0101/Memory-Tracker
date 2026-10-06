import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  MessageSquare,
  X,
  Send,
  Loader2,
  ImagePlus,
  Check,
  Maximize2,
  Trash2,
  StickyNote,
  FileText,
} from "lucide-react";
import {
  api,
  sourcePdfUrl,
  type ChatTurn,
  type ChatSource,
  type RagChart,
  type ChartRecord,
} from "../lib/api";
import { MarkdownBlock } from "./MarkdownBlock";
import { useLightbox } from "../contexts/LightboxContext";
import { refreshGalleryAfterLocalChange } from "../hooks/useCharts";

function todayISO(): string {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

interface ChatMsg {
  role: "user" | "assistant";
  content: string;
  sources?: ChatSource[];
  charts?: RagChart[];
  pending?: boolean;
  system?: boolean; // info line (e.g. "added to pool")
}

interface Staged {
  file: File;
  previewUrl: string;
}

const QUICK_PROMPTS = [
  "What changed in HBM demand and supply?",
  "Compare DRAM pricing signals across brokers.",
  "Show the best charts for AI server memory.",
];

const CHAT_SESSION_KEY = "memoryAskClaudeSessionId";

function makeChatSessionId(): string {
  const cryptoObj = typeof crypto !== "undefined" ? crypto : undefined;
  if (cryptoObj?.randomUUID) return cryptoObj.randomUUID();
  return `memory-chat-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function getChatSessionId(): string {
  try {
    const existing = localStorage.getItem(CHAT_SESSION_KEY);
    if (existing) return existing;
    const next = makeChatSessionId();
    localStorage.setItem(CHAT_SESSION_KEY, next);
    return next;
  } catch {
    return makeChatSessionId();
  }
}

function resetChatSessionId(): string {
  const next = makeChatSessionId();
  try {
    localStorage.setItem(CHAT_SESSION_KEY, next);
  } catch { /* localStorage unavailable */ }
  return next;
}

function fmtDateInt(di: number | null): string {
  if (!di) return "";
  const s = String(di);
  return s.length === 8 ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : s;
}

function sourceFileLabel(filename?: string) {
  return (filename || "").split(/[\\/]/).filter(Boolean).pop() || "";
}

function ragAssetPath(imageUrl?: string): string {
  const prefix = "/api/charts/rag-asset/";
  if (!imageUrl?.startsWith(prefix)) return imageUrl || "";
  const rel = imageUrl.slice(prefix.length);
  try {
    return decodeURIComponent(rel);
  } catch {
    return rel;
  }
}

function chartToRecord(chart: RagChart): ChartRecord | undefined {
  if (!chart.id) return undefined;
  const isRag = chart.id < 0 || chart.image_url.startsWith("/api/charts/rag-asset/");
  const title = chart.tag || chart.caption || "(chart)";
  return {
    id: chart.id,
    source_pdf_id: 0,
    subcategory_id: null,
    image_path: chart.image_path || ragAssetPath(chart.image_url),
    image_url: chart.image_url,
    page_number: chart.page ?? 0,
    region_type: isRag ? "rag_chart" : "pasted_chart",
    tag: title,
    caption: chart.caption || title,
    chart_type: isRag ? "rag_chart" : "pasted_chart",
    broker: chart.broker || "",
    chart_date: chart.date || "",
    chart_date_int: chart.date_int || null,
    quality_status: "approved",
    category_name: chart.category || null,
    subcategory_name: chart.subcategory || null,
    source_filename: chart.source_filename || chart.filename || null,
    is_rag: isRag,
  };
}

function ChatAnswer({ text, sources }: { text: string; sources?: ChatSource[] }) {
  return <MarkdownBlock text={text} sources={sources} />;
}

function withSourceMapForHistory(m: ChatMsg): ChatTurn {
  let content = m.content;
  if (m.role === "assistant" && m.sources?.length) {
    const sourceMap = m.sources
      .map((s, i) => {
        const sid = s.id || `S${i + 1}`;
        const file = sourceFileLabel(s.filename) || "source";
        const page = s.page ? ` P.${s.page}` : "";
        const broker = s.broker ? ` ${s.broker}` : "";
        const date = s.date ? ` ${s.date}` : "";
        return `[${sid}]${broker}${date} ${file}${page}`.trim();
      })
      .join("\n");
    content += `\n\nSOURCE MAP FOR THIS PRIOR ANSWER:\n${sourceMap}`;
  }
  return { role: m.role, content };
}

function ChartThumb({ chart }: { chart: RagChart }) {
  const lightbox = useLightbox();
  const sourceHref = sourcePdfUrl(chart.source_filename || chart.filename, chart.page);
  const record = chartToRecord(chart);
  const open = () =>
    lightbox.open({
      // Lightbox accepts absolute paths (e.g. /api/charts/rag-asset/...) as-is.
      image: chart.image_url,
      tag: chart.tag || chart.caption || "(chart)",
      caption: chart.caption,
      broker: chart.broker,
      date: chart.date,
      page: chart.page ?? undefined,
      filename: chart.source_filename || chart.filename || undefined,
      chartRecord: record,
    });
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={open}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
      }}
      className="group block w-full text-left rounded-md border bg-card overflow-hidden hover:border-primary/40 transition-colors"
      title={chart.caption}
    >
      <div className="h-28 bg-white overflow-hidden">
        <img
          src={chart.image_url}
          alt={chart.caption || "chart"}
          loading="lazy"
          className="w-full h-full object-contain"
        />
      </div>
      <div className="p-1.5">
        <p className="text-[11px] leading-tight line-clamp-2">{chart.caption || "(chart)"}</p>
        <div className="mt-1 flex items-center gap-1.5 text-[10px] text-muted-foreground">
          {chart.broker && <span className="font-medium">{chart.broker}</span>}
          {chart.date && <span>{chart.date}</span>}
          <span className="ml-auto inline-flex items-center gap-1">
            {sourceHref && (
              <a
                href={sourceHref}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="inline-flex items-center gap-0.5 rounded border px-1 py-0.5 text-[10px] text-muted-foreground hover:border-primary/40 hover:text-primary"
                title={chart.page ? `Open source PDF at page ${chart.page}` : "Open source PDF"}
              >
                <FileText className="h-3 w-3" />
                PDF
              </a>
            )}
            <Maximize2 className="h-3 w-3 opacity-0 group-hover:opacity-60" />
          </span>
        </div>
      </div>
    </div>
  );
}

export function ChatPanel() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<"chat" | "add">("chat");
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [staged, setStaged] = useState<Staged | null>(null);
  const [hint, setHint] = useState("");
  const [pasting, setPasting] = useState(false);
  const sessionIdRef = useRef<string>(getChatSessionId());
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // Drag-to-resize: user can drag the LEFT edge of the panel to make it
  // wider (panel is anchored to the right). Width persists to localStorage
  // so the next session restores it. Clamped to [MIN_WIDTH, 85% of viewport].
  const MIN_WIDTH = 380;
  const DEFAULT_WIDTH = 460;
  const MAX_WIDTH_PCT = 0.85;
  const STORAGE_KEY = "askPanelWidth";
  const [panelWidth, setPanelWidth] = useState<number>(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      const n = stored ? parseInt(stored, 10) : NaN;
      if (Number.isFinite(n)) {
        const maxW = typeof window !== "undefined"
          ? window.innerWidth * MAX_WIDTH_PCT : DEFAULT_WIDTH;
        return Math.max(MIN_WIDTH, Math.min(n, maxW));
      }
    } catch { /* localStorage unavailable */ }
    return DEFAULT_WIDTH;
  });
  const widthRef = useRef(panelWidth);
  useEffect(() => { widthRef.current = panelWidth; }, [panelWidth]);
  const draggingRef = useRef(false);
  const onResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    draggingRef.current = true;
    document.body.style.cursor = "ew-resize";
    document.body.style.userSelect = "none";
  }, []);
  useEffect(() => {
    if (!open) return;
    const onMove = (e: MouseEvent) => {
      if (!draggingRef.current) return;
      const maxW = window.innerWidth * MAX_WIDTH_PCT;
      const next = Math.max(MIN_WIDTH, Math.min(window.innerWidth - e.clientX, maxW));
      setPanelWidth(next);
    };
    const onUp = () => {
      if (draggingRef.current) {
        draggingRef.current = false;
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        try { localStorage.setItem(STORAGE_KEY, String(Math.round(widthRef.current))); } catch { /* ignore */ }
      }
    };
    const onResize = () => {
      const maxW = window.innerWidth * MAX_WIDTH_PCT;
      setPanelWidth((w) => Math.min(w, maxW));
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      window.removeEventListener("resize", onResize);
    };
  }, [open]);
  const resetWidth = useCallback(() => {
    setPanelWidth(DEFAULT_WIDTH);
    try { localStorage.setItem(STORAGE_KEY, String(DEFAULT_WIDTH)); } catch { /* ignore */ }
  }, []);
  // Text-note staging: parallel to the chart paste flow above. When non-null,
  // the panel renders the "Add note" form instead of (or alongside) the chart
  // staging area. Submits to /api/paste-text → doc_type='user_input' chunk in
  // the shared fact pool, retrievable on next chat query.
  // (noteOpen state removed — the Add sub-tab now hosts the note form
  //  permanently; no toggle needed.)
  const [noteText, setNoteText] = useState("");
  const [noteTicker, setNoteTicker] = useState("");
  const [noteBroker, setNoteBroker] = useState("");
  const [noteDate, setNoteDate] = useState(() => todayISO());
  const [postingNote, setPostingNote] = useState(false);

  const resizeInput = useCallback(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    const next = Math.min(Math.max(el.scrollHeight, 42), 132);
    el.style.height = `${next}px`;
  }, []);

  const usePrompt = useCallback(
    (prompt: string) => {
      setInput(prompt);
      setActiveTab("chat");
      requestAnimationFrame(() => {
        inputRef.current?.focus();
        resizeInput();
      });
    },
    [resizeInput]
  );

  useEffect(() => {
    resizeInput();
  }, [input, resizeInput]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, staged]);

  const stageImage = useCallback((file: File) => {
    setStaged({ file, previewUrl: URL.createObjectURL(file) });
  }, []);

  const onPaste = useCallback(
    (e: React.ClipboardEvent) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      for (const it of items) {
        if (it.type.startsWith("image/")) {
          const f = it.getAsFile();
          if (f) {
            e.preventDefault();
            stageImage(f);
            setActiveTab("add");  // hop to the Add tab so user can tag + submit
            return;
          }
        }
      }
    },
    [stageImage]
  );

  const send = useCallback(async () => {
    const q = input.trim();
    if (!q || busy) return;
    setInput("");
    const history: ChatMsg[] = [...messages.filter((m) => !m.system), { role: "user", content: q }];
    setMessages((prev) => [...prev, { role: "user", content: q }, { role: "assistant", content: "", pending: true }]);
    setBusy(true);
    try {
      const turns: ChatTurn[] = history.map(withSourceMapForHistory);
      const res = await api.chat(turns, sessionIdRef.current);
      setMessages((prev) => {
        const next = [...prev];
        const idx = next.findIndex((m) => m.pending);
        const msg: ChatMsg = res.error
          ? { role: "assistant", content: `⚠️ ${res.error}` }
          : { role: "assistant", content: res.reply || "(no answer)", sources: res.sources, charts: res.charts };
        if (idx >= 0) next[idx] = msg;
        else next.push(msg);
        return next;
      });
    } catch (err) {
      setMessages((prev) => {
        const next = [...prev];
        const idx = next.findIndex((m) => m.pending);
        const msg: ChatMsg = { role: "assistant", content: `⚠️ ${String(err)}` };
        if (idx >= 0) next[idx] = msg;
        else next.push(msg);
        return next;
      });
    } finally {
      setBusy(false);
    }
  }, [input, busy, messages]);

  const addPastedChart = useCallback(async () => {
    if (!staged || pasting) return;
    setPasting(true);
    try {
      const res = await api.pasteChart(staged.file, { hint: hint.trim() || undefined });
      const chart: RagChart | undefined = res.rag_image_url
        ? {
            id: res.id,
            tag: res.tag,
            caption: res.caption || res.tag,
            image_url: res.rag_image_url,
            image_path: res.image_path,
            broker: res.broker,
            date: res.date,
            date_int: 0,
            filename: "",
            source_filename: "",
            page: null,
            chart_file: "",
            score: 1,
            category: res.category,
            subcategory: res.subcategory,
          }
        : undefined;
      // Refresh the gallery so the freshly-pasted (or deduped) chart shows up
      // immediately — the new row was inserted into SQLite by /api/paste-chart,
      // but the gallery's react-query cache is otherwise stale until refetch.
      await refreshGalleryAfterLocalChange(qc).catch((error) => {
        console.warn("Gallery refresh after paste failed", error);
      });
      qc.invalidateQueries({ queryKey: ["categories"] });
      qc.invalidateQueries({ queryKey: ["chartIndex"] });
      const msg = res.deduped
        ? `↺ Already in your gallery as "${res.tag}" — no duplicate row was created (same image was pasted before).`
        : res.rag_indexed
          ? `✓ Tagged "${res.tag}"${res.broker ? ` (${res.broker})` : ""} → ${res.category} / ${res.subcategory}. Added to the gallery and the research pool (RAG).`
          : `✓ Tagged "${res.tag}" and saved to the gallery. (Not indexed into the RAG — Qdrant may be offline.)`;
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          system: true,
          content: msg,
          charts: chart ? [chart] : undefined,
        },
      ]);
      URL.revokeObjectURL(staged.previewUrl);
      setStaged(null);
      setHint("");
      setActiveTab("chat");  // jump back so the user sees the confirmation
    } catch (err) {
      setMessages((prev) => [...prev, { role: "assistant", system: true, content: `⚠️ Paste failed: ${String(err)}` }]);
    } finally {
      setPasting(false);
    }
  }, [staged, hint, pasting, qc]);

  const discardStaged = useCallback(() => {
    if (staged) URL.revokeObjectURL(staged.previewUrl);
    setStaged(null);
    setHint("");
  }, [staged]);

  // Submit the staged note → /api/paste-text → user_input chunk in the
  // shared fact pool. Triggers corpus-stats invalidation so the staleness
  // banners on Overview / Model pick up the new chunk on their next poll.
  const submitNote = useCallback(async () => {
    const t = noteText.trim();
    if (!t || postingNote) return;
    setPostingNote(true);
    try {
      const res = await api.pasteText(t, {
        ticker: noteTicker.trim() || undefined,
        broker: noteBroker.trim() || undefined,
        date: noteDate.trim() || undefined,
      });
      qc.invalidateQueries({ queryKey: ["corpusStats"] });
      const lines: string[] = [];
      if (res.ok) {
        lines.push(`✓ Note added to the fact pool as a user_input chunk`);
        const tags = [
          res.broker ? `broker=${res.broker}` : "",
          (res.ticker && res.ticker !== "USER") ? `ticker=${res.ticker}` : "",
          res.date ? `date=${res.date}` : "",
        ].filter(Boolean).join(" · ");
        if (tags) lines.push(`Tagged: ${tags}`);
        if (res.kpi_tags?.length) lines.push(`KPI: ${res.kpi_tags.join(", ")}`);
        if (res.title) lines.push(`Title: ${res.title}`);
        lines.push(`Mention "from my notes" in your next question to focus chat on user_input chunks.`);
      } else {
        lines.push(`⚠️ Note NOT indexed: ${res.reason || "unknown error"}`);
      }
      setMessages((prev) => [
        ...prev,
        { role: "assistant", system: true, content: lines.join("\n") },
      ]);
      setNoteText("");
      setNoteTicker("");
      setNoteBroker("");
      setNoteDate(todayISO());
      setActiveTab("chat");  // jump back so the user sees the confirmation
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", system: true, content: `⚠️ Note paste failed: ${String(err)}` },
      ]);
    } finally {
      setPostingNote(false);
    }
  }, [noteText, noteTicker, noteBroker, noteDate, postingNote, qc]);

  const clearAll = useCallback(() => {
    setMessages([]);
    setInput("");
    sessionIdRef.current = resetChatSessionId();
    if (staged) URL.revokeObjectURL(staged.previewUrl);
    setStaged(null);
    setHint("");
    setActiveTab("chat");
    setNoteText("");
    setNoteTicker("");
    setNoteBroker("");
    setNoteDate(todayISO());
  }, [staged]);

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="fixed bottom-5 right-5 z-40 flex items-center gap-2 rounded-full bg-primary px-4 py-3 text-primary-foreground shadow-lg hover:bg-primary/90 transition-colors"
        title="Ask the memory research assistant"
      >
        <MessageSquare className="h-5 w-5" />
        <span className="text-sm font-semibold">Ask</span>
      </button>
    );
  }

  return (
    <div
      className="fixed inset-y-0 right-0 z-50 flex flex-col border-l bg-background shadow-2xl"
      style={{ width: `${panelWidth}px` }}
    >
      {/* Drag-to-resize handle on the LEFT edge — drag leftward to widen */}
      <div
        onMouseDown={onResizeStart}
        onDoubleClick={resetWidth}
        title="Drag to resize · double-click to reset"
        className="group absolute inset-y-0 -left-1 z-10 w-2 cursor-ew-resize"
      >
        <div className="absolute inset-y-0 left-1/2 -translate-x-1/2 w-px bg-border group-hover:w-[2px] group-hover:bg-primary/50 transition-all" />
      </div>
      {/* Header */}
      <div className="border-b">
        <div className="flex items-center justify-between px-4 pt-3">
          <div className="flex items-center gap-2">
            <MessageSquare className="h-5 w-5 text-primary" />
            <div>
              <h2 className="text-sm font-bold leading-none">Ask — Memory Research</h2>
              <p className="text-[11px] text-muted-foreground mt-0.5">
                Grounded in the shared fact pool
              </p>
            </div>
          </div>
          <div className="flex items-center gap-0.5">
            <button
              onClick={clearAll}
              disabled={messages.length === 0 && !input && !staged && !noteText}
              className="rounded p-1 hover:bg-muted disabled:opacity-30 disabled:cursor-not-allowed"
              title="Clear conversation"
            >
              <Trash2 className="h-4 w-4" />
            </button>
            <button onClick={() => setOpen(false)} className="rounded p-1 hover:bg-muted" title="Close">
              <X className="h-5 w-5" />
            </button>
          </div>
        </div>
        {/* Sub-tabs: Chat (conversation) | Add (paste chart / text → fact pool) */}
        <div className="mt-2 flex">
          <button
            onClick={() => setActiveTab("chat")}
            className={`flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-semibold border-b-2 transition-colors ${
              activeTab === "chat"
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <MessageSquare className="h-3.5 w-3.5" />
            Chat
          </button>
          <button
            onClick={() => setActiveTab("add")}
            className={`flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-semibold border-b-2 transition-colors ${
              activeTab === "add"
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <ImagePlus className="h-3.5 w-3.5" />
            Add to pool
            {(staged || noteText.trim()) && (
              <span className="ml-0.5 inline-block h-1.5 w-1.5 rounded-full bg-primary" />
            )}
          </button>
        </div>
      </div>

      {/* Chat tab: messages + input row */}
      {activeTab === "chat" && (
      <>
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4 space-y-5">
        {messages.length === 0 && (
          <div className="mx-auto max-w-sm py-10 text-center text-sm text-muted-foreground">
            <MessageSquare className="h-8 w-8 mx-auto mb-3 opacity-40" />
            <p className="font-semibold text-foreground">Ask Memory Research</p>
            <p className="mt-1 text-xs leading-relaxed">
              Grounded in PDFs, pasted notes, and indexed charts.
            </p>
            <div className="mt-4 grid gap-2 text-left">
              {QUICK_PROMPTS.map((prompt) => (
                <button
                  key={prompt}
                  type="button"
                  onClick={() => usePrompt(prompt)}
                  className="rounded-md border bg-card px-3 py-2 text-xs leading-snug text-foreground hover:border-primary/40 hover:bg-muted/40 transition-colors"
                >
                  {prompt}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) =>
          m.system ? (
            <div key={i} className="rounded-md border border-primary/20 bg-primary/5 px-3 py-2 text-xs">
              <div className="flex items-start gap-1.5">
                <Check className="h-3.5 w-3.5 text-primary mt-0.5 shrink-0" />
                <span>{m.content}</span>
              </div>
              {m.charts && m.charts.length > 0 && (
                <div className="mt-2 grid grid-cols-2 gap-2">
                  {m.charts.map((c, j) => (
                    <ChartThumb key={j} chart={c} />
                  ))}
                </div>
              )}
            </div>
          ) : (
            <div key={i} className={m.role === "user" ? "flex justify-end" : "flex justify-start"}>
              <div
                className={
                  m.role === "user"
                    ? "max-w-[82%] rounded-lg bg-primary px-3 py-2 text-sm leading-6 text-primary-foreground"
                    : "max-w-[94%] rounded-lg border bg-card px-3.5 py-3 text-sm leading-7 shadow-sm"
                }
              >
                {m.pending ? (
                  <div className="flex items-center gap-2 text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    <span className="text-xs">Thinking...</span>
                  </div>
                ) : m.role === "assistant" ? (
                  <ChatAnswer text={m.content} sources={m.sources} />
                ) : (
                  <div className="whitespace-pre-wrap leading-6">{m.content}</div>
                )}
                {m.charts && m.charts.length > 0 && (
                  <div className="mt-2.5 grid grid-cols-2 gap-2">
                    {m.charts.map((c, j) => (
                      <ChartThumb key={j} chart={c} />
                    ))}
                  </div>
                )}
                {false && (m.sources || []).length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {(m.sources || []).map((s, j) => {
                      const head = [s.broker, s.date].filter(Boolean).join(" · ");
                      const fname = s.filename || "";
                      return (
                        <span
                          key={j}
                          title={fname || head}
                          className="inline-flex items-center gap-1 rounded bg-background/60 px-1.5 py-0.5 text-[10px] text-muted-foreground max-w-full"
                        >
                          {head && <span className="font-medium">{head}</span>}
                          {head && fname && <span className="opacity-40">·</span>}
                          {fname && <span className="truncate max-w-[260px]">{fname}</span>}
                          {!head && !fname && <span>source</span>}
                        </span>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          )
        )}
      </div>

      {/* Input row — Chat tab only */}
      <div className="border-t px-3 py-3">
        <div className="flex items-end gap-2 rounded-lg border bg-card p-2 shadow-sm">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => {
              setInput(e.target.value);
              resizeInput();
            }}
            onInput={resizeInput}
            onPaste={onPaste}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            rows={1}
            placeholder="Ask about DRAM / NAND / HBM, or paste a chart..."
            className="max-h-32 min-h-[42px] flex-1 resize-none bg-transparent px-2 py-2 text-sm leading-6 focus:outline-none"
          />
          <button
            onClick={send}
            disabled={busy || !input.trim()}
            className="mb-0.5 rounded-md bg-primary p-2.5 text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
            title="Send"
          >
            {busy ? <Loader2 className="h-5 w-5 animate-spin" /> : <Send className="h-5 w-5" />}
          </button>
        </div>
      </div>
      </>
      )}

      {/* Add tab: paste-chart section + paste-text section, both always shown */}
      {activeTab === "add" && (
        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-5">
          <p className="text-[11px] text-muted-foreground">
            Add content directly into the shared fact pool. Both go through Qdrant — charts are saved as <code className="rounded bg-muted px-1">media_type=chart</code>, text notes as <code className="rounded bg-muted px-1">doc_type=user_input</code>. They surface in chat retrieval immediately.
          </p>

          {/* ── Paste a chart ───────────────────────────────────────── */}
          <section className="rounded-md border bg-card">
            <header className="flex items-center gap-1.5 border-b px-3 py-2 text-xs font-semibold">
              <ImagePlus className="h-3.5 w-3.5 text-primary" />
              Paste a chart
            </header>
            <div className="p-3">
              {!staged ? (
                <label className="flex flex-col items-center justify-center gap-2 rounded border-2 border-dashed border-muted-foreground/30 px-3 py-6 text-center cursor-pointer hover:border-primary/50 hover:bg-muted/30 transition-colors"
                       onPaste={(e) => {
                         const items = e.clipboardData?.items;
                         if (!items) return;
                         for (const it of items) {
                           if (it.type.startsWith("image/")) {
                             const f = it.getAsFile();
                             if (f) { e.preventDefault(); stageImage(f); return; }
                           }
                         }
                       }}>
                  <ImagePlus className="h-5 w-5 text-muted-foreground" />
                  <div className="text-xs text-muted-foreground">
                    Click here then <strong>Ctrl/Cmd+V</strong> to paste a chart image
                    <br />
                    or use the file picker
                  </div>
                  <input
                    type="file"
                    accept="image/*"
                    className="hidden"
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) stageImage(f);
                    }}
                  />
                  <span className="rounded border px-2 py-0.5 text-[10px] text-muted-foreground">
                    Choose file…
                  </span>
                </label>
              ) : (
                <div className="space-y-2">
                  <div className="flex gap-3">
                    <img src={staged.previewUrl} alt="staged" className="h-24 w-32 rounded border bg-white object-contain" />
                    <div className="flex-1 space-y-2">
                      <p className="text-[11px] font-medium">Tag &amp; add to the research pool</p>
                      <input
                        value={hint}
                        onChange={(e) => setHint(e.target.value)}
                        placeholder="Optional hint (e.g. 'HBM TAM, UBS') — leave blank to auto-tag"
                        className="w-full rounded border bg-background px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-primary/30"
                      />
                      <div className="flex gap-2">
                        <button
                          onClick={addPastedChart}
                          disabled={pasting}
                          className="flex items-center gap-1.5 rounded bg-primary px-3 py-1.5 text-xs font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                        >
                          {pasting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ImagePlus className="h-3.5 w-3.5" />}
                          {pasting ? "Tagging…" : "Add chart"}
                        </button>
                        <button onClick={discardStaged} className="rounded border px-3 py-1.5 text-xs hover:bg-muted">
                          Discard
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </section>

          {/* ── Paste a note (text → user_input chunk) ──────────────── */}
          <section className="rounded-md border bg-card">
            <header className="flex items-center gap-1.5 border-b px-3 py-2 text-xs font-semibold">
              <FileText className="h-3.5 w-3.5 text-primary" />
              Paste a note
            </header>
            <div className="p-3 space-y-2">
              <textarea
                value={noteText}
                onChange={(e) => setNoteText(e.target.value)}
                rows={6}
                placeholder="Paste an analyst quote, a number, an internal observation…"
                className="w-full resize-y rounded border bg-background px-2 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-primary/30"
              />
              <div className="grid grid-cols-3 gap-1.5 text-[11px]">
                <input
                  value={noteTicker}
                  onChange={(e) => setNoteTicker(e.target.value)}
                  placeholder="Ticker"
                  title="Ticker (MU, 005930, …)"
                  className="rounded border bg-background px-1.5 py-1 focus:outline-none focus:ring-2 focus:ring-primary/30"
                />
                <input
                  value={noteBroker}
                  onChange={(e) => setNoteBroker(e.target.value)}
                  placeholder="Broker"
                  title="Broker (GS, JPM, …)"
                  className="rounded border bg-background px-1.5 py-1 focus:outline-none focus:ring-2 focus:ring-primary/30"
                />
                <input
                  type="date"
                  value={noteDate}
                  onChange={(e) => setNoteDate(e.target.value)}
                  className="rounded border bg-background px-1.5 py-1 focus:outline-none focus:ring-2 focus:ring-primary/30"
                />
              </div>
              <p className="text-[10px] text-muted-foreground">
                All optional — empty fields are auto-tagged from the note's text by Haiku.
              </p>
              <div className="flex gap-2">
                <button
                  onClick={submitNote}
                  disabled={postingNote || !noteText.trim()}
                  className="flex items-center gap-1.5 rounded bg-primary px-3 py-1.5 text-xs font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                >
                  {postingNote ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <StickyNote className="h-3.5 w-3.5" />}
                  {postingNote ? "Adding…" : "Add note"}
                </button>
                <button
                  onClick={() => { setNoteText(""); setNoteTicker(""); setNoteBroker(""); setNoteDate(todayISO()); }}
                  disabled={postingNote || (!noteText && !noteTicker && !noteBroker)}
                  className="rounded border px-3 py-1.5 text-xs hover:bg-muted disabled:opacity-50"
                >
                  Clear
                </button>
              </div>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
