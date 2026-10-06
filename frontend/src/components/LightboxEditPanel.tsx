import { useEffect, useMemo, useState } from "react";
import { Loader2, Save, Trash2, X } from "lucide-react";
import type { ChartRecord } from "../lib/api";
import { useCategories, useDeleteChart, useUpdateChart } from "../hooks/useCharts";

interface Props {
  chart: ChartRecord;
  onClose: () => void;
  onChanged: (chart: ChartRecord) => void;
  onDeleted: () => void;
}

export function LightboxEditPanel({ chart, onClose, onChanged, onDeleted }: Props) {
  const [tag, setTag] = useState(chart.tag || "");
  const [caption, setCaption] = useState(chart.caption || "");
  const [broker, setBroker] = useState(chart.broker || "");
  const [chartDate, setChartDate] = useState(chart.chart_date || "");
  const [categoryName, setCategoryName] = useState(chart.category_name || "");
  const [subcategoryName, setSubcategoryName] = useState(chart.subcategory_name || "");

  const updateMutation = useUpdateChart();
  const deleteMutation = useDeleteChart();
  const { data: categories } = useCategories();

  useEffect(() => {
    setTag(chart.tag || "");
    setCaption(chart.caption || "");
    setBroker(chart.broker || "");
    setChartDate(chart.chart_date || "");
    setCategoryName(chart.category_name || "");
    setSubcategoryName(chart.subcategory_name || "");
  }, [chart.id]);

  const selectedCat = useMemo(
    () => categories?.find((c) => c.name === categoryName),
    [categories, categoryName],
  );
  const categoryOptions = useMemo(() => {
    const base = categories || [];
    if (!categoryName || base.some((c) => c.name === categoryName)) return base;
    return [
      ...base,
      { id: -1, name: categoryName, display_order: 999, chart_count: 0, subcategories: [] },
    ];
  }, [categories, categoryName]);
  const subcategories = useMemo(() => {
    const base = selectedCat?.subcategories || [];
    if (!subcategoryName || base.some((s) => s.name === subcategoryName)) return base;
    return [
      ...base,
      { id: -1, name: subcategoryName, display_order: 999, chart_count: 0 },
    ];
  }, [selectedCat, subcategoryName]);
  const busy = updateMutation.isPending || deleteMutation.isPending;

  const save = async () => {
    const safeTag = tag.trim() || "Untitled chart";
    const updated = await updateMutation.mutateAsync({
      id: chart.id,
      tag: safeTag,
      caption,
      broker,
      chart_date: chartDate,
      category_name: categoryName,
      subcategory_name: subcategoryName,
    });
    onChanged({
      ...chart,
      ...updated,
      tag: safeTag,
      caption,
      broker,
      chart_date: chartDate,
      category_name: categoryName || null,
      subcategory_name: subcategoryName || null,
      image_url: chart.image_url,
      source_filename: chart.source_filename,
    });
    onClose();
  };

  const remove = async () => {
    const prompt = chart.is_rag
      ? `Delete "${chart.tag || "this ES RAG chart"}" from the Memory Chart gallery? This will not delete the shared ES RAG source.`
      : `Delete "${chart.tag || "this chart"}" from the local gallery?`;
    if (!window.confirm(prompt)) return;
    await deleteMutation.mutateAsync(chart.id);
    onDeleted();
  };

  return (
    <aside
      className="w-[390px] shrink-0 overflow-y-auto border-l border-white/10 bg-background text-foreground shadow-2xl"
      onWheel={(e) => e.stopPropagation()}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="border-b px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold">Edit Chart</h3>
            <p className="text-[11px] text-muted-foreground">
              {chart.is_rag
                ? "Edits are saved as Memory Chart overrides. The shared ES RAG source stays intact."
                : "Rename, recategorize, or delete this local chart."}
            </p>
          </div>
          <button onClick={onClose} className="rounded p-1 hover:bg-muted" title="Close edit panel">
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      <div className="space-y-4 p-4 text-sm">
        <div>
          <label className="mb-1 block text-xs font-semibold text-muted-foreground">Name / Tag</label>
          <input
            value={tag}
            onChange={(e) => setTag(e.target.value)}
            className="w-full rounded-md border bg-background px-3 py-2 text-sm"
            placeholder="Chart name"
          />
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="mb-1 block text-xs font-semibold text-muted-foreground">Broker</label>
            <input
            value={broker}
            onChange={(e) => setBroker(e.target.value)}
            className="w-full rounded-md border bg-background px-3 py-2 text-sm"
          />
          </div>
          <div>
            <label className="mb-1 block text-xs font-semibold text-muted-foreground">Date</label>
            <input
              type="date"
            value={chartDate}
            onChange={(e) => setChartDate(e.target.value)}
            className="w-full rounded-md border bg-background px-3 py-2 text-sm"
          />
          </div>
        </div>

        <div>
          <label className="mb-1 block text-xs font-semibold text-muted-foreground">Category</label>
          <select
            value={categoryName}
            onChange={(e) => {
              setCategoryName(e.target.value);
              setSubcategoryName("");
            }}
            className="w-full rounded-md border bg-background px-3 py-2 text-sm"
          >
            <option value="">Select...</option>
            {categoryOptions.map((c) => (
              <option key={c.id} value={c.name}>
                {c.name}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="mb-1 block text-xs font-semibold text-muted-foreground">Subcategory</label>
          <select
            value={subcategoryName}
            onChange={(e) => setSubcategoryName(e.target.value)}
            className="w-full rounded-md border bg-background px-3 py-2 text-sm"
          >
            <option value="">Select...</option>
            {subcategories.map((s) => (
              <option key={s.id} value={s.name}>
                {s.name}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="mb-1 block text-xs font-semibold text-muted-foreground">Caption</label>
          <textarea
            value={caption}
            onChange={(e) => setCaption(e.target.value)}
            rows={5}
            className="w-full resize-y rounded-md border bg-background px-3 py-2 text-sm"
            placeholder="Describe what the chart shows..."
          />
        </div>

        <div className="rounded-md border bg-muted/30 px-3 py-2 text-[11px] text-muted-foreground">
          <div>Source: {chart.source_filename || "-"}</div>
          <div>Page: {chart.page_number || "-"}</div>
          <div>Type: {chart.chart_type || chart.region_type || "-"}</div>
        </div>

        <div className="flex items-center justify-between border-t pt-4">
          <button
            onClick={remove}
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded-md px-3 py-2 text-sm font-semibold text-destructive hover:bg-destructive/10 disabled:opacity-50"
          >
            {deleteMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
            Delete
          </button>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="rounded-md border px-3 py-2 text-sm font-semibold hover:bg-muted"
              disabled={busy}
            >
              Cancel
            </button>
            <button
              onClick={save}
              disabled={busy}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {updateMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              Save
            </button>
          </div>
        </div>
      </div>
    </aside>
  );
}
