import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Save, Settings, X } from "lucide-react";
import { api } from "../lib/api";

export function PromptSettingsButton() {
  const [open, setOpen] = useState(false);
  const [industry, setIndustry] = useState("");
  const [company, setCompany] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const qc = useQueryClient();
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["promptSettings"], queryFn: api.ragSettings, enabled: open,
  });

  useEffect(() => {
    if (!data) return;
    setIndustry(data.settings.tabs.overview.industry_prompt);
    setCompany(data.settings.tabs.overview.company_prompt);
  }, [data]);

  useEffect(() => {
    if (!open) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving) setOpen(false);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [open, saving]);

  const save = async () => {
    if (!data || saving) return;
    setSaving(true);
    setMessage("");
    try {
      const settings = JSON.parse(JSON.stringify(data.settings));
      settings.tabs.overview.industry_prompt = industry;
      settings.tabs.overview.company_prompt = company;
      const result = await api.updateRagSettings(settings);
      qc.setQueryData(["promptSettings"], { ...data, settings: result.settings });
      qc.invalidateQueries({ queryKey: ["overview"] });
      setMessage("Saved. The next Overview request will use these instructions.");
    } catch (err) {
      setMessage(`Save failed: ${String(err)}`);
    } finally {
      setSaving(false);
    }
  };

  return <>
    <button type="button" aria-label="Prompt settings" title="Prompt settings"
      onClick={() => { setOpen(true); setMessage(""); refetch(); }}
      className="inline-flex h-10 w-10 items-center justify-center rounded-lg border text-muted-foreground hover:border-primary/50 hover:text-foreground">
      <Settings className="h-4 w-4" />
    </button>
    {open && createPortal(
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6"
        onMouseDown={() => !saving && setOpen(false)}>
        <div role="dialog" aria-modal="true" aria-labelledby="prompt-settings-title"
          className="flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-lg border bg-background shadow-xl"
          onMouseDown={(event) => event.stopPropagation()}>
          <div className="flex items-center justify-between border-b px-5 py-4">
            <div>
              <h2 id="prompt-settings-title" className="font-bold">Prompt settings</h2>
              <p className="mt-1 text-xs text-muted-foreground">Add instructions to the Overview question sent to the API. Leave blank to use the default.</p>
            </div>
            <button type="button" aria-label="Close settings" disabled={saving} onClick={() => setOpen(false)} className="rounded-md p-2 hover:bg-muted"><X className="h-4 w-4" /></button>
          </div>
          <div className="space-y-5 overflow-y-auto p-5">
            {isLoading ? <p className="text-sm text-muted-foreground">Loading prompts...</p>
              : error ? <p className="text-sm text-destructive">Failed to load prompts: {String(error)}</p>
              : <>
                <label className="block text-sm font-semibold">Industry Overview prompt
                  <textarea value={industry} onChange={(event) => setIndustry(event.target.value)} rows={8} maxLength={8000} disabled={saving}
                    className="mt-2 w-full resize-y rounded-md border bg-background p-3 text-sm font-normal leading-relaxed" placeholder="Enter instructions for the Arete and other provider overviews..." />
                </label>
                <label className="block text-sm font-semibold">Company Overview prompt
                  <textarea value={company} onChange={(event) => setCompany(event.target.value)} rows={8} maxLength={8000} disabled={saving}
                    className="mt-2 w-full resize-y rounded-md border bg-background p-3 text-sm font-normal leading-relaxed" placeholder="Enter instructions for the company overview..." />
                </label>
              </>}
          </div>
          <div className="flex items-center justify-between gap-4 border-t px-5 py-4">
            <p role="status" className="text-xs text-muted-foreground">{message}</p>
            <button type="button" onClick={save} disabled={saving || !data || !!error}
              className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground disabled:opacity-50">
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} Save
            </button>
          </div>
        </div>
      </div>, document.body)}
  </>;
}
