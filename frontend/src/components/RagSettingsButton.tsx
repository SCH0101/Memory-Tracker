import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createPortal } from "react-dom";
import { Loader2, Save, Settings, X } from "lucide-react";
import { api, type RagSettings, type RagOverviewSectionSetting } from "../lib/api";

type SettingsTab = "overview" | "dashboard" | "model" | "ask_claude";
type PromptTab = "dashboard" | "model" | "ask_claude";

const TAB_LABELS: Record<SettingsTab, string> = {
  overview: "Overview",
  dashboard: "Dashboard",
  model: "Model",
  ask_claude: "Ask Memory Research",
};

const SOURCE_LABELS: Record<string, string> = {
  broker_research: "Broker research",
  expert_call: "Expert calls",
  company_presentation: "Company presentations",
  industry_report: "Industry reports",
  earnings_transcript: "Earnings transcripts",
  tables: "Tables",
  user_input: "User input",
};

const DEFAULT_SOURCE_KEYS = Object.keys(SOURCE_LABELS);

function cloneSettings(settings: RagSettings): RagSettings {
  return JSON.parse(JSON.stringify(settings));
}

function sectionDraft(draft: RagSettings, key: string) {
  if (!draft.tabs.overview.sections[key]) {
    draft.tabs.overview.sections[key] = {
      query: "",
      prompt: "",
      sources: [...(draft.tabs.overview.sources || DEFAULT_SOURCE_KEYS)],
    };
  } else if (!draft.tabs.overview.sections[key].sources) {
    draft.tabs.overview.sections[key].sources = [...(draft.tabs.overview.sources || DEFAULT_SOURCE_KEYS)];
  }
  return draft.tabs.overview.sections[key];
}

function SourceSelector({
  label,
  value,
  options = DEFAULT_SOURCE_KEYS,
  onChange,
}: {
  label: string;
  value?: string[];
  options?: string[];
  onChange: (value: string[]) => void;
}) {
  const selected = new Set(value || []);
  const toggle = (key: string) => {
    const next = new Set(selected);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    onChange(options.filter((opt) => next.has(opt)));
  };
  return (
    <div className="space-y-1">
      <div className="text-xs font-semibold">{label}</div>
      <div className="flex flex-wrap gap-1.5">
        {options.map((key) => {
          const active = selected.has(key);
          return (
            <button
              key={key}
              type="button"
              onClick={() => toggle(key)}
              className={`rounded border px-2 py-1 text-[11px] font-medium transition-colors ${
                active
                  ? "border-primary bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground"
              }`}
            >
              {SOURCE_LABELS[key] || key}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function IntegerTextInput({
  value,
  min,
  emptyValue,
  onChange,
}: {
  value: number;
  min: number;
  emptyValue: number;
  onChange: (value: number) => void;
}) {
  const [text, setText] = useState(String(value ?? emptyValue));

  useEffect(() => {
    setText(String(value ?? emptyValue));
  }, [value, emptyValue]);

  const commit = (raw: string) => {
    const digits = raw.replace(/\D/g, "");
    setText(digits);
    if (digits === "") {
      onChange(emptyValue);
      return;
    }
    onChange(Math.max(min, Number(digits)));
  };

  return (
    <input
      type="text"
      inputMode="numeric"
      pattern="[0-9]*"
      value={text}
      onChange={(e) => commit(e.target.value)}
      onBlur={() => {
        setText(String(text === "" ? emptyValue : Math.max(min, Number(text))));
      }}
      className="mt-1 w-full rounded-md border bg-background px-2 py-1.5 text-sm"
    />
  );
}

function PromptEditor({
  label,
  value,
  original,
  rows = 12,
  onChange,
  onReset,
}: {
  label: string;
  value: string;
  original?: string;
  rows?: number;
  onChange: (value: string) => void;
  onReset: () => void;
}) {
  const displayValue = value || original || "";
  const hasOverride = value.trim().length > 0;
  if (!displayValue) return null;
  return (
    <label className="block text-xs font-semibold">
      <span className="flex items-center justify-between gap-3">
        <span>{label}</span>
        {hasOverride ? (
          <button
            type="button"
            onClick={onReset}
            className="rounded border px-2 py-1 text-[11px] font-medium text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            Use default
          </button>
        ) : null}
      </span>
      <textarea
        value={displayValue}
        onChange={(e) => onChange(e.target.value)}
        rows={rows}
        spellCheck={false}
        className="mt-1 w-full resize-y rounded-md border bg-background px-3 py-2 font-mono text-[11px] leading-relaxed"
      />
      <span className="mt-1 block text-[11px] font-normal text-muted-foreground">
        {hasOverride ? "Saved as the active prompt after you press Save." : "Default prompt. Edit this box to save a modified prompt."}
      </span>
    </label>
  );
}

export function RagSettingsButton() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<SettingsTab>("overview");
  const [draft, setDraft] = useState<RagSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");

  const { data, isLoading, refetch } = useQuery({
    queryKey: ["ragSettings"],
    queryFn: api.ragSettings,
    enabled: open,
    staleTime: 30_000,
  });

  useEffect(() => {
    if (data?.settings) setDraft(cloneSettings(data.settings));
  }, [data?.settings]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving) setOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, saving]);

  const save = async () => {
    if (!draft || saving) return;
    setSaving(true);
    setMessage("");
    try {
      const res = await api.updateRagSettings(draft);
      setDraft(cloneSettings(res.settings));
      qc.invalidateQueries({ queryKey: ["overview"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      qc.invalidateQueries({ queryKey: ["model"] });
      qc.invalidateQueries({ queryKey: ["ragSettings"] });
      setMessage("Saved");
    } catch (err) {
      setMessage(`Save failed: ${String(err)}`);
    } finally {
      setSaving(false);
    }
  };

  const updateDraft = (fn: (next: RagSettings) => void) => {
    setDraft((current) => {
      if (!current) return current;
      const next = cloneSettings(current);
      fn(next);
      return next;
    });
  };

  const sections = data?.overview_sections || [];
  const promptTemplates = data?.prompt_templates;
  const sourceOptions = data?.source_options?.length ? data.source_options : DEFAULT_SOURCE_KEYS;
  const promptTab: PromptTab | null = activeTab === "overview" ? null : activeTab;
  const tabPromptTemplate = promptTab ? promptTemplates?.[promptTab].prompt : "";

  return (
    <>
      <button
        type="button"
        onClick={() => {
          setOpen(true);
          setMessage("");
          refetch();
        }}
        className="inline-flex h-10 w-10 items-center justify-center rounded-lg border text-muted-foreground hover:border-primary/50 hover:text-foreground"
        title="RAG retrieval and prompt settings"
      >
        <Settings className="h-4 w-4" />
      </button>

      {open && createPortal(
        <div
          className="fixed inset-0 z-50 bg-black/40"
          onMouseDown={() => !saving && setOpen(false)}
        >
          <div
            className="fixed bottom-8 left-1/2 top-8 flex min-h-0 -translate-x-1/2 flex-col overflow-hidden rounded-lg border bg-background shadow-xl"
            style={{
              width: "min(1120px, calc(100vw - 48px))",
            }}
            onMouseDown={(e) => e.stopPropagation()}
          >
            <div className="flex shrink-0 items-center justify-between border-b bg-background px-4 py-3">
              <div>
                <h2 className="text-sm font-bold">RAG Settings</h2>
                <p className="text-xs text-muted-foreground">
                  Control retrieval count, rerank count, and editable prompts.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                disabled={saving}
                title="Close settings"
                aria-label="Close settings"
                className="rounded-md border p-1.5 hover:bg-muted disabled:opacity-50"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            {isLoading || !draft ? (
              <div className="flex min-h-0 flex-1 items-center justify-center text-sm text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Loading settings...
              </div>
            ) : (
              <div className="grid min-h-0 flex-1 grid-cols-[220px_1fr] overflow-hidden">
                <aside className="min-h-0 overflow-y-auto border-r p-3">
                  <div className="mb-4 rounded-md border bg-muted/20 p-3">
                    <label className="mb-2 block text-xs font-semibold">
                      Chunks Get
                      <IntegerTextInput
                        value={draft.retrieval.chunks_get}
                        min={1}
                        emptyValue={1}
                        onChange={(value) => updateDraft((next) => {
                          next.retrieval.chunks_get = value;
                        })}
                      />
                    </label>
                    <label className="block text-xs font-semibold">
                      Rerank Keep
                      <IntegerTextInput
                        value={draft.retrieval.rerank_keep}
                        min={0}
                        emptyValue={0}
                        onChange={(value) => updateDraft((next) => {
                          next.retrieval.rerank_keep = value;
                        })}
                      />
                    </label>
                    <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
                      Applies to Overview, Dashboard refresh, Model RAG pull, and Ask Memory Research retrieval. Set Rerank Keep to 0 to tell ES RAG to skip reranking.
                    </p>
                  </div>
                  <div className="mb-4 rounded-md border bg-muted/20 p-3">
                    <SourceSelector
                      label="Default Sources"
                      options={sourceOptions}
                      value={draft.retrieval.sources}
                      onChange={(sources) => updateDraft((next) => {
                        next.retrieval.sources = sources;
                      })}
                    />
                    <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
                      Global fallback. Each tab and Overview section can use its own source selection.
                    </p>
                  </div>

                  <div className="space-y-1">
                    {(Object.keys(TAB_LABELS) as SettingsTab[]).map((tab) => (
                      <button
                        key={tab}
                        type="button"
                        onClick={() => setActiveTab(tab)}
                        className={`w-full rounded-md px-3 py-2 text-left text-sm ${
                          activeTab === tab ? "bg-primary text-primary-foreground" : "hover:bg-muted"
                        }`}
                      >
                        {TAB_LABELS[tab]}
                      </button>
                    ))}
                  </div>
                </aside>

                <main className="min-h-0 overflow-y-auto p-4">
                  {activeTab === "overview" ? (
                    <div className="space-y-4">
                      <SourceSelector
                        label="Overview Sources"
                        options={sourceOptions}
                        value={draft.tabs.overview.sources}
                        onChange={(sources) => updateDraft((next) => {
                          next.tabs.overview.sources = sources;
                        })}
                      />
                      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                        <div className="space-y-2">
                          <h3 className="text-sm font-bold">Industry Overview</h3>
                          <PromptEditor
                            label="Industry Prompt"
                            value={draft.tabs.overview.industry_prompt}
                            original={promptTemplates?.overview.industry_prompt}
                            rows={12}
                            onChange={(value) => updateDraft((next) => {
                              next.tabs.overview.industry_prompt = value;
                            })}
                            onReset={() => updateDraft((next) => {
                              next.tabs.overview.industry_prompt = "";
                            })}
                          />
                        </div>
                        <div className="space-y-2">
                          <h3 className="text-sm font-bold">Company Overview</h3>
                          <PromptEditor
                            label="Company Prompt"
                            value={draft.tabs.overview.company_prompt}
                            original={promptTemplates?.overview.company_prompt}
                            rows={12}
                            onChange={(value) => updateDraft((next) => {
                              next.tabs.overview.company_prompt = value;
                            })}
                            onReset={() => updateDraft((next) => {
                              next.tabs.overview.company_prompt = "";
                            })}
                          />
                        </div>
                      </div>

                      <div className="space-y-3">
                        <h3 className="text-xs font-bold uppercase tracking-wide text-muted-foreground">
                          Overview Sections
                        </h3>
                        {sections.map((section: RagOverviewSectionSetting) => {
                          const s = draft.tabs.overview.sections[section.key] || {
                            query: "",
                            prompt: "",
                            sources: section.sources || draft.tabs.overview.sources || DEFAULT_SOURCE_KEYS,
                          };
                          return (
                            <div key={section.key} className="rounded-md border p-3">
                              <div className="mb-2 flex items-center justify-between gap-3">
                                <h4 className="text-sm font-bold">{section.label}</h4>
                                <button
                                  type="button"
                                  onClick={() => updateDraft((next) => {
                                    next.tabs.overview.sections[section.key] = {
                                      query: "",
                                      prompt: "",
                                      sources: [...(next.tabs.overview.sources || DEFAULT_SOURCE_KEYS)],
                                    };
                                  })}
                                  className="rounded border px-2 py-1 text-[11px] hover:bg-muted"
                                >
                                  Clear
                                </button>
                              </div>
                              <label className="mb-2 block text-xs font-semibold">
                                Retrieval Query Override
                                <textarea
                                  value={s.query || ""}
                                  onChange={(e) => updateDraft((next) => {
                                    sectionDraft(next, section.key).query = e.target.value;
                                  })}
                                  rows={2}
                                  className="mt-1 w-full resize-y rounded-md border bg-background px-3 py-2 text-xs"
                                  placeholder={section.default_query}
                                />
                              </label>
                              <SourceSelector
                                label="Section Sources"
                                options={sourceOptions}
                                value={s.sources || section.sources || draft.tabs.overview.sources}
                                onChange={(sources) => updateDraft((next) => {
                                  sectionDraft(next, section.key).sources = sources;
                                })}
                              />
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  ) : (
                    <div className="space-y-3">
                      <h3 className="text-sm font-bold">{TAB_LABELS[activeTab]}</h3>
                      {promptTab ? (
                        <>
                          <SourceSelector
                            label={`${TAB_LABELS[promptTab]} Sources`}
                            options={sourceOptions}
                            value={draft.tabs[promptTab].sources}
                            onChange={(sources) => updateDraft((next) => {
                              next.tabs[promptTab].sources = sources;
                            })}
                          />
                          <PromptEditor
                            label={`${TAB_LABELS[promptTab]} Prompt`}
                            value={draft.tabs[promptTab].prompt}
                            original={tabPromptTemplate}
                            rows={16}
                            onChange={(value) => updateDraft((next) => {
                              next.tabs[promptTab].prompt = value;
                            })}
                            onReset={() => updateDraft((next) => {
                              next.tabs[promptTab].prompt = "";
                            })}
                          />
                        </>
                      ) : null}
                    </div>
                  )}
                </main>
              </div>
            )}

            <div className="flex shrink-0 items-center justify-between border-t bg-background px-4 py-3">
              <div className="text-xs text-muted-foreground">{message}</div>
              <button
                type="button"
                onClick={save}
                disabled={saving || !draft}
                className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                Save
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}
