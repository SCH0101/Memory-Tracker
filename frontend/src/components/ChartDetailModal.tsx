import { useState } from "react";
import { useUpdateChart, useDeleteChart, useCategories } from "../hooks/useCharts";
import type { ChartRecord } from "../lib/api";

interface Props {
  chart: ChartRecord;
  onClose: () => void;
  onUpdate: () => void;
}

export function ChartDetailModal({ chart, onClose, onUpdate }: Props) {
  const [tag, setTag] = useState(chart.tag);
  const [caption, setCaption] = useState(chart.caption || "");
  const [broker, setBroker] = useState(chart.broker);
  const [chartDate, setChartDate] = useState(chart.chart_date);
  const [categoryName, setCategoryName] = useState(chart.category_name || "");
  const [subcategoryName, setSubcategoryName] = useState(
    chart.subcategory_name || ""
  );

  const updateMutation = useUpdateChart();
  const deleteMutation = useDeleteChart();
  const { data: categories } = useCategories();

  const selectedCat = categories?.find((c) => c.name === categoryName);
  const subcategories = selectedCat?.subcategories || [];

  const handleSave = async () => {
    await updateMutation.mutateAsync({
      id: chart.id,
      tag,
      caption,
      broker,
      chart_date: chartDate,
      category_name: categoryName,
      subcategory_name: subcategoryName,
    });
    onUpdate();
    onClose();
  };

  const handleDelete = async () => {
    await deleteMutation.mutateAsync(chart.id);
    onUpdate();
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/50"
        onClick={onClose}
      />

      {/* Modal */}
      <div className="relative z-10 w-full max-w-4xl max-h-[90vh] overflow-y-auto bg-card rounded-xl shadow-2xl border mx-4">
        {/* Close button */}
        <button
          onClick={onClose}
          className="absolute right-4 top-4 z-10 rounded-full p-1.5 hover:bg-muted transition-colors"
        >
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>

        {/* Chart image */}
        <div className="bg-muted flex items-center justify-center p-4 min-h-[300px]">
          <img
            src={`/charts/${chart.image_path}`}
            alt={chart.tag}
            className="max-w-full max-h-[50vh] object-contain"
          />
        </div>

        {/* Metadata form */}
        <div className="p-6 space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium mb-1">Tag</label>
              <input
                value={tag}
                onChange={(e) => setTag(e.target.value)}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Broker</label>
              <input
                value={broker}
                onChange={(e) => setBroker(e.target.value)}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Date</label>
              <input
                type="date"
                value={chartDate}
                onChange={(e) => setChartDate(e.target.value)}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">
                Category
              </label>
              <select
                value={categoryName}
                onChange={(e) => {
                  setCategoryName(e.target.value);
                  setSubcategoryName("");
                }}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm"
              >
                <option value="">Select...</option>
                {categories?.map((c) => (
                  <option key={c.id} value={c.name}>
                    {c.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="col-span-2">
              <label className="block text-sm font-medium mb-1">
                Subcategory
              </label>
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
          </div>

          {/* Caption */}
          <div>
            <label className="block text-sm font-medium mb-1">Caption</label>
            <textarea
              value={caption}
              onChange={(e) => setCaption(e.target.value)}
              rows={3}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm resize-y"
              placeholder="Describe what the chart shows…"
            />
          </div>

          {/* Source info */}
          <div className="flex items-center gap-4 text-xs text-muted-foreground">
            <span>Source: {chart.source_filename || "—"}</span>
            <span>Page: {chart.page_number}</span>
            <span>Type: {chart.chart_type || chart.region_type}</span>
          </div>

          {/* Actions */}
          <div className="flex items-center justify-between pt-2 border-t">
            <button
              onClick={handleDelete}
              disabled={deleteMutation.isPending}
              className="rounded-md px-4 py-2 text-sm font-medium text-destructive hover:bg-destructive/10 transition-colors"
            >
              {deleteMutation.isPending ? "Deleting..." : "Delete"}
            </button>
            <div className="flex gap-2">
              <button
                onClick={onClose}
                className="rounded-md border px-4 py-2 text-sm font-medium hover:bg-muted transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleSave}
                disabled={updateMutation.isPending}
                className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
              >
                {updateMutation.isPending ? "Saving..." : "Save"}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
