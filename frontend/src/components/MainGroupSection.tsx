import { useState } from "react";
import type { CategoryGroup, ChartRecord } from "../lib/api";
import { CategorySection } from "./CategorySection";

interface Props {
  title: string;
  groups: CategoryGroup[];
  onSelectChart: (chart: ChartRecord) => void;
}

export function MainGroupSection({ title, groups, onSelectChart }: Props) {
  const [open, setOpen] = useState(false);
  const totalCharts = groups.reduce(
    (groupTotal, group) =>
      groupTotal +
      group.subcategories.reduce(
        (subcategoryTotal, subcategory) => subcategoryTotal + subcategory.charts.length,
        0,
      ),
    0,
  );

  return (
    <section className="rounded-2xl border-2 bg-card shadow-sm">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-center justify-between rounded-2xl px-6 py-5 text-left transition-colors hover:bg-muted/50"
      >
        <div className="flex items-center gap-3">
          <svg
            className={`h-5 w-5 text-muted-foreground transition-transform ${
              open ? "rotate-90" : ""
            }`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
          </svg>
          <h2 className="text-xl font-bold">{title}</h2>
          <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-semibold text-slate-700">
            {totalCharts}
          </span>
        </div>
      </button>

      {open && (
        <div className="space-y-4 border-t bg-muted/10 px-5 py-5">
          {groups.map((group) => (
            <CategorySection
              key={`${group.main_group}-${group.category}`}
              group={group}
              onSelectChart={onSelectChart}
            />
          ))}
        </div>
      )}
    </section>
  );
}
