import { useCallback, useEffect, useRef, useState } from "react";
import { X, ZoomIn, ZoomOut, RotateCcw, Pencil, FileText } from "lucide-react";
import type { LightboxItem } from "../contexts/LightboxContext";
import { sourcePdfUrl, type ChartRecord } from "../lib/api";
import { LightboxEditPanel } from "./LightboxEditPanel";

interface Props {
  item: LightboxItem;
  onClose: () => void;
  /** Optional — when omitted, the Edit button is hidden (view-only mode). */
  chartRecord?: ChartRecord | null;
  onChanged?: (chart: ChartRecord) => void;
  onDeleted?: () => void;
}

const MIN_ZOOM = 1;
const MAX_ZOOM = 6;
const STEP = 1.25;

export function Lightbox({ item, onClose, chartRecord, onChanged, onDeleted }: Props) {
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [editing, setEditing] = useState(false);
  const dragRef = useRef<{ x: number; y: number; px: number; py: number } | null>(null);
  const editable = Boolean(chartRecord && chartRecord.id !== 0);

  // Esc / +/- / 0 keyboard shortcuts.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "+" || e.key === "=") setZoom((z) => Math.min(MAX_ZOOM, z * STEP));
      else if (e.key === "-" || e.key === "_") setZoom((z) => Math.max(MIN_ZOOM, z / STEP));
      else if (e.key === "0") {
        setZoom(1);
        setPan({ x: 0, y: 0 });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Reset pan whenever zoom returns to 1.
  useEffect(() => {
    if (zoom === 1) setPan({ x: 0, y: 0 });
  }, [zoom]);

  useEffect(() => {
    setEditing(false);
  }, [item.image, chartRecord?.id]);

  const onWheel = useCallback((e: React.WheelEvent) => {
    // Wheel zoom (deltaY < 0 = zoom in).
    e.preventDefault();
    const factor = e.deltaY < 0 ? STEP : 1 / STEP;
    setZoom((z) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z * factor)));
  }, []);

  const onMouseDown = (e: React.MouseEvent) => {
    if (zoom <= 1) return;
    dragRef.current = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y };
  };
  const onMouseMove = (e: React.MouseEvent) => {
    const d = dragRef.current;
    if (!d) return;
    setPan({ x: d.px + (e.clientX - d.x), y: d.py + (e.clientY - d.y) });
  };
  const endDrag = () => {
    dragRef.current = null;
  };

  const onDoubleClick = () => {
    if (zoom === 1) setZoom(2.5);
    else {
      setZoom(1);
      setPan({ x: 0, y: 0 });
    }
  };

  const reset = () => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  };

  const title = chartRecord?.tag || item.tag || item.caption || "Chart";
  const subtitle = [chartRecord?.broker || item.broker, chartRecord?.chart_date || item.date].filter(Boolean).join(" · ");
  const cat = [
    chartRecord?.category_name || item.category,
    chartRecord?.subcategory_name || item.subcategory,
  ].filter(Boolean).join(" › ");
  const src = item.image ? (item.image.startsWith("http") || item.image.startsWith("/") ? item.image : `/charts/${item.image}`) : "";
  const sourceFilename = chartRecord?.source_filename || item.filename;
  const sourcePage = chartRecord?.page_number || item.page;
  const pdfHref = sourcePdfUrl(sourceFilename, sourcePage);

  return (
    <div
      className="fixed inset-0 z-[60] flex flex-col bg-black/85 backdrop-blur-sm"
      onWheel={onWheel}
      onMouseMove={onMouseMove}
      onMouseUp={endDrag}
      onMouseLeave={endDrag}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-4 border-b border-white/10 px-5 py-3 text-white">
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-sm font-semibold leading-tight">{title}</h2>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-white/70">
            {subtitle && <span>{subtitle}</span>}
            {cat && <span>{cat}</span>}
            {item.filename && (
              <span className="truncate">
                {item.filename}
                {item.page ? ` · p.${item.page}` : ""}
              </span>
            )}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <button
            onClick={() => setZoom((z) => Math.max(MIN_ZOOM, z / STEP))}
            className="rounded p-1.5 text-white/80 hover:bg-white/10 disabled:opacity-30"
            disabled={zoom <= MIN_ZOOM}
            title="Zoom out (-)"
          >
            <ZoomOut className="h-5 w-5" />
          </button>
          <span className="w-12 text-center text-xs tabular-nums text-white/70">{Math.round(zoom * 100)}%</span>
          <button
            onClick={() => setZoom((z) => Math.min(MAX_ZOOM, z * STEP))}
            className="rounded p-1.5 text-white/80 hover:bg-white/10 disabled:opacity-30"
            disabled={zoom >= MAX_ZOOM}
            title="Zoom in (+)"
          >
            <ZoomIn className="h-5 w-5" />
          </button>
          <button onClick={reset} className="rounded p-1.5 text-white/80 hover:bg-white/10" title="Reset (0)">
            <RotateCcw className="h-5 w-5" />
          </button>
          {pdfHref && (
            <a
              href={pdfHref}
              target="_blank"
              rel="noopener noreferrer"
              className="ml-1 rounded p-1.5 text-white/80 hover:bg-white/10"
              title={sourcePage ? `Open source PDF at page ${sourcePage}` : "Open source PDF"}
            >
              <FileText className="h-5 w-5" />
            </a>
          )}
          {editable && (
            <button
              onClick={() => setEditing((v) => !v)}
              className="ml-1 flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-semibold text-primary-foreground hover:bg-primary/90"
              title="Edit, rename, or delete"
            >
              <Pencil className="h-3.5 w-3.5" />
              Edit
            </button>
          )}
          <button onClick={onClose} className="ml-1 rounded p-1.5 text-white/80 hover:bg-white/10" title="Close (Esc)">
            <X className="h-5 w-5" />
          </button>
        </div>
      </div>

      {/* Image stage — click outside the image closes; drag to pan when zoomed */}
      <div
        className="relative flex min-h-0 flex-1 overflow-hidden"
        onClick={(e) => {
          const target = e.target as HTMLElement;
          if (target.closest("[data-lightbox-content='true']")) return;
          onClose();
        }}
      >
        <div className="relative flex min-w-0 flex-1 items-center justify-center overflow-hidden">
          {src ? (
            <img
              src={src}
              alt={title}
              data-lightbox-content="true"
              draggable={false}
              onMouseDown={onMouseDown}
              onDoubleClick={onDoubleClick}
              style={{
                transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
                cursor: zoom > 1 ? (dragRef.current ? "grabbing" : "grab") : "zoom-in",
                transition: dragRef.current ? "none" : "transform 0.15s ease-out",
              }}
              className="max-h-[88vh] max-w-[92vw] select-none object-contain"
            />
          ) : (
            <div className="text-white/60">Image unavailable</div>
          )}
        </div>

        {editing && chartRecord && onChanged && onDeleted && (
          <div data-lightbox-content="true">
            <LightboxEditPanel
              chart={chartRecord}
              onClose={() => setEditing(false)}
              onChanged={onChanged}
              onDeleted={onDeleted}
            />
          </div>
        )}
      </div>

      {/* Hint footer */}
      <div className="border-t border-white/10 px-5 py-2 text-center text-[10px] text-white/40">
        Scroll or use +/- to zoom · drag to pan · double-click to toggle zoom · Esc to close
      </div>
    </div>
  );
}
