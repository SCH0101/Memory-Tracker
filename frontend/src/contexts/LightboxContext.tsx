import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import type { CategoryGroup, ChartRecord } from "../lib/api";
import { Lightbox } from "../components/Lightbox";

export interface LightboxItem {
  image: string;          // path under /charts/ (e.g. "GS mem may 31/recrop_p010_613.jpg")
  tag?: string;
  caption?: string;
  broker?: string;
  date?: string;
  category?: string;
  subcategory?: string;
  page?: number;
  filename?: string;
  /** When present, the lightbox shows inline edit / delete controls. */
  chartRecord?: ChartRecord;
}

interface Ctx {
  open: (item: LightboxItem) => void;
  close: () => void;
}

const LightboxCtx = createContext<Ctx | null>(null);

export function useLightbox(): Ctx {
  const v = useContext(LightboxCtx);
  if (!v) throw new Error("useLightbox must be used inside <LightboxProvider>");
  return v;
}

interface ProviderProps {
  children: ReactNode;
  /** Called after inline lightbox edits/deletes, so the gallery can refetch. */
  onChartUpdated?: () => void;
}

// Normalize a chart image path for cross-source comparison: strip the
// "/charts/" static-mount prefix if present, URL-decode it (RagChart.image_url
// is URL-encoded with %20 for spaces), and flip Windows-style backslashes to
// forward slashes so the same chart matches regardless of how the path was
// stored.
function normalizeImagePath(p: string | undefined): string {
  if (!p) return "";
  let s = p.replace(/\\/g, "/");
  try {
    s = decodeURIComponent(s);
  } catch {
    /* leave as-is */
  }
  for (const prefix of ["/charts/", "charts/", "/api/charts/rag-asset/", "api/charts/rag-asset/"]) {
    if (s.startsWith(prefix)) {
      s = s.slice(prefix.length);
      break;
    }
  }
  return s.replace(/^\/+/, "").toLowerCase();
}

// Look for a ChartRecord in the gallery's cached query data whose image_path
// matches the given lightbox image. Lets the chat-thumb lightbox surface the
// Edit button when the displayed chart is actually one in the local gallery.
function findLocalChartRecord(qc: QueryClient, imagePath: string | undefined): ChartRecord | null {
  const needle = normalizeImagePath(imagePath);
  if (!needle) return null;
  const groups = qc.getQueryData<CategoryGroup[]>(["charts", "by-category"]);
  if (!groups) return null;
  for (const g of groups)
    for (const s of g.subcategories)
      for (const c of s.charts)
        if ([c.image_path, c.image_url].some((p) => normalizeImagePath(p) === needle)) return c;
  return null;
}

export function LightboxProvider({ children, onChartUpdated }: ProviderProps) {
  const qc = useQueryClient();
  const [item, setItem] = useState<LightboxItem | null>(null);

  // Resolve a ChartRecord for the lightbox item. Direct match wins (gallery
  // passes one explicitly); for chat-thumb lightboxes we fall back to the
  // gallery's cached chart list so the Edit button works when the chart is
  // locally indexed.
  const resolvedRecord = useMemo<ChartRecord | null>(() => {
    if (!item) return null;
    if (item.chartRecord) return item.chartRecord;
    return findLocalChartRecord(qc, item.image);
  }, [item, qc]);

  const open = useCallback((it: LightboxItem) => setItem(it), []);
  const close = useCallback(() => setItem(null), []);

  return (
    <LightboxCtx.Provider value={{ open, close }}>
      {children}
      {item && (
        <Lightbox
          item={item}
          onClose={close}
          chartRecord={resolvedRecord}
          onChanged={(updated) => {
            onChartUpdated?.();
            setItem((prev) =>
              prev
                ? {
                    ...prev,
                    tag: updated.tag,
                    caption: updated.caption,
                    broker: updated.broker,
                    date: updated.chart_date,
                    category: updated.category_name || undefined,
                    subcategory: updated.subcategory_name || undefined,
                    chartRecord: updated,
                  }
                : prev,
            );
          }}
          onDeleted={() => {
            onChartUpdated?.();
            setItem(null);
          }}
        />
      )}
    </LightboxCtx.Provider>
  );
}
