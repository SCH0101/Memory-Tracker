import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { LayoutGrid, FileText, Table2 } from "lucide-react";
import { useChartsByCategory } from "./hooks/useCharts";
import { UploadZone } from "./components/UploadZone";
import { PromptSettingsButton } from "./components/PromptSettingsButton";
import { PipelineProgress } from "./components/PipelineProgress";
import { MainGroupSection } from "./components/MainGroupSection";
import { FilterBar } from "./components/FilterBar";
import { ChatPanel } from "./components/ChatPanel";
import { OverviewTab } from "./components/OverviewTab";
import { ModelTab } from "./components/WorkbookModelTab";
import { DashboardTab } from "./components/DashboardTab";
import { useLightbox } from "./contexts/LightboxContext";
import type { ChartRecord } from "./lib/api";

type Tab = "gallery" | "overview" | "model" | "dashboard";

const TABS: { id: Tab; label: string; icon: React.ReactNode }[] = [
  { id: "gallery", label: "Gallery", icon: <LayoutGrid className="h-4 w-4" /> },
  { id: "overview", label: "Overview", icon: <FileText className="h-4 w-4" /> },
  { id: "model", label: "Model", icon: <Table2 className="h-4 w-4" /> },
];

function brokerKey(name?: string | null): string {
  const clean = (name || "").trim().toLowerCase();
  const aliases: Record<string, string> = {
    "arete research": "arete",
    "goldman sachs": "gs",
    goldman: "gs",
    "jp morgan": "jpm",
    "j.p. morgan": "jpm",
    jpmorgan: "jpm",
    "morgan stanley": "ms",
    bofa: "bofa",
    baml: "bofa",
    bac: "bofa",
    "bank of america": "bofa",
    boa: "bofa",
    citigroup: "citi",
    "citi research": "citi",
    barc: "barclays",
    deutsche: "db",
    "deutsche bank": "db",
    "arete": "arete",
    bern: "bernstein",
    "sanford bernstein": "bernstein",
    alliancebernstein: "bernstein",
  };
  if (aliases[clean]) return aliases[clean];
  return clean;
}

export default function App() {
  const { data: groups, isLoading, isFetching, refreshFromSource } = useChartsByCategory();
  const qc = useQueryClient();
  const lightbox = useLightbox();
  const [tab, setTab] = useState<Tab>("gallery");
  const [activeJobs, setActiveJobs] = useState<string[]>([]);
  const [filters, setFilters] = useState<{
    broker?: string;
    search?: string;
  }>({});

  // Gallery click opens the lightbox; the lightbox's Edit button opens the
  // settings modal (handled by LightboxProvider).
  const openChart = (c: ChartRecord) =>
    lightbox.open({
      image: c.image_url || c.image_path,
      tag: c.tag,
      caption: c.caption,
      broker: c.broker,
      date: c.chart_date,
      category: c.category_name || undefined,
      subcategory: c.subcategory_name || undefined,
      page: c.page_number,
      filename: c.source_filename || undefined,
      chartRecord: c,
    });

  const handleUploadComplete = (jobIds: string[]) => {
    setActiveJobs((prev) => [...prev, ...jobIds]);
  };

  const handleJobDone = (jobId: string) => {
    setActiveJobs((prev) => prev.filter((j) => j !== jobId));
    void refreshFromSource();
    // Cheap-read invalidations: dashboard datapoints, overview chart index,
    // and corpus stats all reflect the new PDF; no GPU / Qdrant write cost.
    // Heavy regenerations (Overview Claude, Model RAG pull) stay manual per
    // the embedded tier rules.
    qc.invalidateQueries({ queryKey: ["dashboard"] });
    qc.invalidateQueries({ queryKey: ["chartIndex"] });
    qc.invalidateQueries({ queryKey: ["corpusStats"] });
  };

  const filteredGroups = groups?.map((g) => ({
    ...g,
    subcategories: g.subcategories
      .map((s) => ({
        ...s,
        charts: s.charts.filter((c) => {
          if (filters.broker && brokerKey(c.broker) !== brokerKey(filters.broker)) return false;
          if (
            filters.search &&
            !c.tag.toLowerCase().includes(filters.search.toLowerCase()) &&
            !c.caption.toLowerCase().includes(filters.search.toLowerCase()) &&
            !(c.source_filename || "").toLowerCase().includes(filters.search.toLowerCase())
          )
            return false;
          return true;
        }),
      }))
      .filter((s) => s.charts.length > 0),
  })).filter((g) => g.subcategories.length > 0);

  const totalCharts =
    groups?.reduce(
      (sum, g) =>
        sum + g.subcategories.reduce((s2, sub) => s2 + sub.charts.length, 0),
      0
    ) ?? 0;

  return (
    <div className="min-h-screen bg-background">
      {/* Header */}
      <header className="sticky top-0 z-30 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
        <div className="mx-auto max-w-[1800px] px-6 py-4">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-2xl font-bold tracking-tight">
                Memory Chart
              </h1>
              <p className="text-sm text-muted-foreground">
                Semiconductor memory research &middot; {totalCharts}{" "}
                chart{totalCharts !== 1 ? "s" : ""}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <UploadZone onUploadComplete={handleUploadComplete} />
              <PromptSettingsButton />
            </div>
          </div>

          {/* Tab navigation */}
          <nav className="mt-3 flex gap-1">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                  tab === t.id
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-muted"
                }`}
              >
                {t.icon}
                {t.label}
              </button>
            ))}
          </nav>
        </div>
      </header>

      {/* Active jobs (shown on any tab so upload progress is always visible) */}
      {activeJobs.length > 0 && (
        <div className="mx-auto max-w-[1800px] px-6 pt-4 space-y-2">
          {activeJobs.map((jid) => (
            <PipelineProgress
              key={jid}
              jobId={jid}
              onDone={() => handleJobDone(jid)}
            />
          ))}
        </div>
      )}

      {tab === "gallery" && (
        <>
          <div className="mx-auto max-w-[1800px] px-6 py-4">
            <FilterBar
              filters={filters}
              onChange={setFilters}
              isRefreshing={isFetching}
              onRefresh={() => {
                qc.invalidateQueries({ queryKey: ["brokers"] });
                qc.invalidateQueries({ queryKey: ["chartIndex"] });
                qc.invalidateQueries({ queryKey: ["corpusStats"] });
                void refreshFromSource();
              }}
            />
          </div>

          <main className="mx-auto max-w-[1800px] px-6 pb-12 space-y-6">
            {isLoading && (
              <div className="text-center py-20 text-muted-foreground">
                Loading charts...
              </div>
            )}

            {!isLoading && (!filteredGroups || filteredGroups.length === 0) && (
              <div className="text-center py-20">
                <LineChartIcon className="mx-auto mb-4 h-10 w-10 text-muted-foreground" />
                <h2 className="text-xl font-semibold mb-2">No charts yet</h2>
                <p className="text-muted-foreground">
                  Upload sellside PDF reports to extract and categorize memory
                  industry charts.
                </p>
              </div>
            )}

            {Array.from(
              new Set(filteredGroups?.map((group) => group.main_group || "Memory Sector")),
            )
              .sort((left, right) => {
                if (left === "Memory Sector") return -1;
                if (right === "Memory Sector") return 1;
                return left.localeCompare(right);
              })
              .map((mainGroup) => {
              const sectionGroups = filteredGroups?.filter(
                (group) => (group.main_group || "Memory Sector") === mainGroup,
              );
              return sectionGroups?.length ? (
                <MainGroupSection
                  key={mainGroup}
                  title={mainGroup}
                  groups={sectionGroups}
                  onSelectChart={openChart}
                />
              ) : null;
              })}
          </main>
        </>
      )}

      {tab === "overview" && <OverviewTab />}
      {tab === "model" && <ModelTab />}
      {tab === "dashboard" && <DashboardTab />}

      {/* RAG chat bar: query the fact pool + paste/tag charts into it */}
      <ChatPanel />
    </div>
  );
}
