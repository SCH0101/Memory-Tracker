import { Fragment, useCallback, useMemo, useState, type Dispatch, type SetStateAction } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  ExternalLink,
  Loader2,
  Newspaper,
  Plus,
  RefreshCw,
  Sparkles,
  Trash2,
} from "lucide-react";
import { api, type ModelCell, type ModelResponse, type ModelRow, type RackField, type RackRow } from "../lib/api";
import { useCorpusBaseline } from "../hooks/useCorpusBaseline";

const TAB_LABELS: Record<string, string> = {
  hbm: "HBM",
  dram: "DRAM",
  nand: "NAND",
  capex: "Capex",
  return_metrics: "Return Metrics",
};
const MODEL_TAB_ORDER = ["hbm", "nand", "capex", "return_metrics"];
const RACK_VENDOR_ORDER = ["NVDA", "AMD", "ASIC"];
const RACK_SPEC_PERIOD = "__spec__";
const INCREMENTAL_POWER_GROUP = "Incremental Provisioned Power";
const DEPLOYMENT_ASSUMPTION_GROUP = "Deployment Assumption - per 1GW";
const DEPLOYMENT_COMPUTE_KEYS = new Set([
  "gw_used",
  "compute_load_share",
  "compute_power_kw_per_gw",
  "server_dram_share_total_dram_market",
  "rdimm_dram_demand",
  "gp_server_dram_demand",
  "gddr_rack_attach_share",
  "hbm_backend_yield_impact",
  "hbm_logic_die_capacity_uplift",
  "hbm_combined_uplift",
  "ex_hbm_gb_per_die",
  "ex_hbm_die_per_wafer",
  "ex_hbm_wafer_yield",
  "ex_hbm_good_die_per_wafer",
]);
const FORMULA_VALUE_LOCKED_KEYS = new Set(["racks_per_gw"]);
type FormulaNames = { keyToAlias: Record<string, string>; aliasToKey: Record<string, string> };
type ScenarioMetric = {
  key: string;
  label: string;
  unit: string;
  source: "deployment" | "rack";
};

const AGGREGATE_FORMULA_LABELS: Record<string, string> = {
  rack_total_gpus_mn: "Base total GPU packages",
  rack_foundry_kwspm_required: "Base foundry capacity required",
  rack_dram_kwspm_required: "Base total DRAM kWSPM demand",
  rack_hbm_demand: "Base HBM demand",
  rack_system_dram_demand: "Base DDR5 / CPU memory demand",
  rack_gddr_dram_demand: "Base GDDR DRAM demand",
  rack_data_center_dram_demand: "Base data-center DRAM demand",
  rack_total_server_dram_demand: "Base total server DRAM demand",
  rack_total_dram_demand: "Base total DRAM demand",
  rack_implied_pc_mobile_dram_demand: "Base implied PC, mobile and other DRAM demand",
  rack_total_dram_demand_yoy: "Base total DRAM demand YoY",
  rack_hbm_share_total_dram_demand: "Base HBM / total DRAM demand",
  rack_ex_hbm_dram_demand: "Base ex-HBM DRAM demand",
  rack_hbm_core_kwspm: "Base HBM core kWSPM",
  rack_hbm_effective_kwspm: "Base HBM effective kWSPM",
  rack_ex_hbm_kwspm: "Base ex-HBM DRAM kWSPM",
  rack_hbm_share_dram_capacity: "HBM / total DRAM kWSPM",
};

function rackFieldPeriod(field: RackField, period: string): string {
  return field.period_mode === "single" ? RACK_SPEC_PERIOD : period;
}

function variableNameFromLabel(label: string): string {
  const clean = label
    .trim()
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/_+/g, "_");
  if (!clean) return "";
  return /^[0-9]/.test(clean) ? `_${clean}` : clean;
}

function buildFormulaNames(modelRows: ModelRow[], rackFields: RackField[]): FormulaNames {
  const keyToAlias: Record<string, string> = {};
  const aliasToKey: Record<string, string> = {};
  const add = (key: string, label: string) => {
    const alias = variableNameFromLabel(label);
    if (!key || !alias) return;
    keyToAlias[key] = alias;
    if (!aliasToKey[alias] || aliasToKey[alias] === key) aliasToKey[alias] = key;
  };
  for (const row of modelRows) add(row.key, row.label);
  for (const field of rackFields) add(field.key, field.label);
  for (const group of SCENARIO_OUTPUT_GROUPS) {
    for (const metric of group.metrics) add(metric.key, metric.label);
  }
  for (const [key, label] of Object.entries(AGGREGATE_FORMULA_LABELS)) add(key, label);
  return { keyToAlias, aliasToKey };
}

function displayFormula(expr: string, names?: FormulaNames): string {
  if (!names) return expr;
  return expr.replace(/\b[A-Za-z_][A-Za-z0-9_]*\b/g, (token) => names.keyToAlias[token] || token);
}

function evalExpr(expr: string, scope: Record<string, number | null>, names?: FormulaNames): number | null {
  const cleanExpr = normalizeFormula(expr);
  if (!cleanExpr) return null;
  const ids = Array.from(new Set(cleanExpr.match(/[a-zA-Z_][a-zA-Z0-9_]*/g) || []));
  const args: number[] = [];
  for (const id of ids) {
    const internalKey = names?.aliasToKey[id];
    const v = scope[id] ?? (internalKey ? scope[internalKey] : undefined);
    if (v === null || v === undefined || Number.isNaN(v)) return null;
    args.push(v);
  }
  try {
    const fn = new Function(...ids, `return (${cleanExpr});`);
    const r = fn(...args);
    return Number.isFinite(r) ? (r as number) : null;
  } catch {
    return null;
  }
}

function normalizeFormula(expr?: string | null): string {
  const text = (expr || "").trim();
  return text.startsWith("=") ? text.slice(1).trim() : text;
}

function formulaDisplay(expr?: string | null, names?: FormulaNames): string {
  const clean = normalizeFormula(expr);
  return clean ? `=${displayFormula(clean, names)}` : "";
}

function canonicalFormula(expr?: string | null, names?: FormulaNames): string {
  const clean = normalizeFormula(expr);
  if (!clean) return "";
  return clean
    .replace(/\b[A-Za-z_][A-Za-z0-9_]*\b/g, (token) => names?.aliasToKey[token] || token)
    .replace(/\s+/g, "");
}

function formulaMatchesDefault(expr?: string | null, fallback?: string | null, names?: FormulaNames): boolean {
  const formula = canonicalFormula(expr, names);
  const defaultFormula = canonicalFormula(fallback, names);
  return Boolean(formula && defaultFormula && formula === defaultFormula);
}

function cellFormula(cell: ModelCell | undefined, fallback?: string | null, session?: string | null): string {
  if (session !== undefined) {
    const sessionFormula = normalizeFormula(session);
    if (sessionFormula) return sessionFormula;
  }
  if (cell?.source === "formula" && cell.formula) return normalizeFormula(cell.formula);
  return normalizeFormula(fallback || "");
}

function fmt(value: number | null, unit: string): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "";
  if (unit === "%") {
    const pct = Math.abs(value) <= 1 ? value * 100 : value;
    const aPct = Math.abs(pct);
    if (aPct >= 100) return pct.toFixed(0);
    if (aPct >= 10) return pct.toFixed(1).replace(/\.0$/, "");
    return pct.toFixed(2).replace(/\.00$/, "");
  }
  const a = Math.abs(value);
  if (unit === "x") return value.toFixed(2);
  if (a >= 1000) return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (a >= 100) return value.toFixed(0);
  if (a >= 10) return value.toFixed(1);
  if (a >= 1) return value.toFixed(2);
  return value.toFixed(3);
}

function inputBorderClass(overrideActive: boolean, requiresInput = false, formulaOverride = false) {
  if (overrideActive) return "border-red-400 bg-red-50/70 ring-1 ring-red-200 focus:ring-red-300";
  if (formulaOverride) return "border-amber-400 bg-amber-50/70 ring-1 ring-amber-200 focus:ring-amber-300";
  return requiresInput
    ? "border-slate-300 bg-slate-50 shadow-[inset_0_0_0_1px_rgba(148,163,184,0.28)] focus:ring-primary/40"
    : "border-transparent bg-transparent hover:border-border hover:bg-background focus:border-border focus:bg-background focus:ring-primary/40";
}

function inputTextClass(isFormula: boolean, valueOverride: boolean, displayText = "") {
  if (displayText.trim().startsWith("=")) return "text-foreground";
  return isFormula && !valueOverride ? "text-foreground" : "text-blue-700";
}

const SCENARIO_OUTPUT_GROUPS = [
  {
    name: "Rack result rows",
    metrics: [
      { key: "gpu_packages_per_gw", label: "Total GPU packages in 1 GW", unit: "packages/yr", source: "rack" },
      { key: "hbm_frontend_wafers_per_gw", label: "HBM front-end wafers", unit: "wafers/yr", source: "rack" },
      { key: "cpu_memory_wafers_per_gw", label: "DDR5 front-end wafers", unit: "wafers/yr", source: "rack" },
      { key: "dram_wafers_per_gw", label: "Total DRAM memory wafers (HBM + DDR5)", unit: "wafers/yr", source: "rack" },
      { key: "hbm_demand", label: "HBM DRAM demand", unit: "bn Gb", source: "rack" },
      { key: "system_dram_demand", label: "DDR5 / CPU memory demand", unit: "bn Gb", source: "rack" },
      { key: "gddr_dram_demand", label: "GDDR DRAM demand", unit: "bn Gb", source: "rack" },
      { key: "total_server_dram_demand", label: "Total server DRAM demand", unit: "bn Gb", source: "rack" },
      { key: "total_dram_demand", label: "Total DRAM demand", unit: "bn Gb", source: "rack" },
      { key: "hbm_share_total_dram_demand", label: "HBM / total DRAM demand", unit: "%", source: "rack" },
    ],
  },
] satisfies Array<{ name: string; metrics: ScenarioMetric[] }>;

const SCENARIO_BASE_METRICS = [
  "total_gpu_packages_mn",
  "foundry_kwspm_required",
  "dram_kwspm_required",
  "hbm_demand",
  "system_dram_demand",
  "gddr_dram_demand",
  "data_center_dram_demand",
  "total_server_dram_demand",
  "total_dram_demand",
  "implied_pc_mobile_dram_demand",
  "total_dram_demand_yoy",
  "hbm_share_total_dram_demand",
  "ex_hbm_dram_demand",
  "hbm_core_kwspm",
  "hbm_effective_kwspm",
  "ex_hbm_kwspm",
  "hbm_share_dram_capacity",
];

function isFiniteNumber(value: number | null | undefined): value is number {
  return value !== null && value !== undefined && Number.isFinite(value);
}

export function ModelTab() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery<ModelResponse>({
    queryKey: ["model", "hbm"],
    queryFn: api.getModel,
    staleTime: 300_000,
  });

  const [activeTab, setActiveTab] = useState<string>("hbm");
  const [overrides, setOverrides] = useState<Record<string, number | null>>({});
  const [rackOverrides, setRackOverrides] = useState<Record<string, number | null>>({});
  const [formulaOverrides, setFormulaOverrides] = useState<Record<string, string | null>>({});
  const [rackFormulaOverrides, setRackFormulaOverrides] = useState<Record<string, string | null>>({});
  const [focus, setFocus] = useState<{ k: string; text: string } | null>(null);
  const [rackFocus, setRackFocus] = useState<{ k: string; text: string } | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const { newChunks, acknowledge } = useCorpusBaseline("model");

  const periods = data?.periods ?? [];
  const rackFields = data?.rack_fields ?? [];
  const rackSections = useMemo(() => {
    const sections = data?.rack_sections ?? [];
    return [...sections].sort((a, b) => {
      const ai = RACK_VENDOR_ORDER.indexOf(a.vendor);
      const bi = RACK_VENDOR_ORDER.indexOf(b.vendor);
      return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
    });
  }, [data]);

  const tabsWithRows = useMemo(() => {
    if (!data) return [];
    const base = data.tabs.length ? data.tabs : ["hbm", "nand"];
    const visible = new Set([...base, "capex", "return_metrics"]);
    return MODEL_TAB_ORDER.filter((t) => visible.has(t));
  }, [data]);
  const formulaNames = useMemo(
    () => buildFormulaNames(data?.rows ?? [], rackFields),
    [data?.rows, rackFields],
  );

  const cellKey = (key: string, period: string) => `${key}::${period}`;
  const rackCellKey = (rowId: number, fieldKey: string, period: string) => `${rowId}::${fieldKey}::${period}`;

  const inputValue = useCallback(
    (row: ModelRow, period: string): number | null => {
      const ck = cellKey(row.key, period);
      if (ck in overrides) return overrides[ck];
      return row.cells[period]?.value ?? null;
    },
    [overrides],
  );

  const deploymentComputed = useMemo(() => {
    const out: Record<string, Record<string, number | null>> = {};
    if (!data) return out;
    const rows = data.rows.filter((r) => DEPLOYMENT_COMPUTE_KEYS.has(r.key));
    for (const row of rows) {
      out[row.key] = {};
      for (const p of periods) out[row.key][p] = row.type === "input" ? inputValue(row, p) : null;
    }
    for (const row of rows) {
      if (row.type !== "formula" || !row.expr) continue;
      for (const p of periods) {
        const ck = cellKey(row.key, p);
        const session = ck in overrides ? overrides[ck] : undefined;
        if (session !== undefined && session !== null) {
          out[row.key][p] = session;
          continue;
        }
        if (
          session === undefined &&
          row.cells[p]?.source === "user" &&
          row.cells[p]?.value !== null &&
          row.cells[p]?.value !== undefined
        ) {
          out[row.key][p] = row.cells[p].value;
          continue;
        }
        const scope: Record<string, number | null> = {};
        for (const [key, byPeriod] of Object.entries(out)) scope[key] = byPeriod[p] ?? null;
        const formulaSession = ck in formulaOverrides ? formulaOverrides[ck] : undefined;
        if (formulaSession !== undefined) {
          out[row.key][p] = formulaSession ? evalExpr(formulaSession, scope, formulaNames) : evalExpr(row.expr, scope, formulaNames);
          continue;
        }
        if (row.cells[p]?.source === "formula" && row.cells[p]?.formula) {
          out[row.key][p] = evalExpr(row.cells[p].formula || "", scope, formulaNames);
          continue;
        }
        out[row.key][p] = evalExpr(row.expr, scope, formulaNames);
      }
    }
    return out;
  }, [data, periods, inputValue, overrides, formulaOverrides, formulaNames]);

  const rackComputed = useMemo(() => {
    const out: Record<number, Record<string, Record<string, number | null>>> = {};
    for (const section of rackSections) {
      for (const row of section.rows) {
        out[row.id] = {};
        for (const field of rackFields) {
          out[row.id][field.key] = {};
          for (const p of periods) {
            if (field.type !== "input") {
              out[row.id][field.key][p] = null;
              continue;
            }
            const storedPeriod = rackFieldPeriod(field, p);
            const ck = rackCellKey(row.id, field.key, storedPeriod);
            out[row.id][field.key][p] = ck in rackOverrides
              ? rackOverrides[ck]
              : row.cells[field.key]?.[storedPeriod]?.value ?? null;
          }
        }
      }
    }

    for (const section of rackSections) {
      for (const row of section.rows) {
        for (const field of rackFields) {
          if (field.type !== "formula" || !field.expr) continue;
          for (const p of periods) {
            const storedPeriod = rackFieldPeriod(field, p);
            const ck = rackCellKey(row.id, field.key, storedPeriod);
            const session = ck in rackOverrides ? rackOverrides[ck] : undefined;
            if (session !== undefined && session !== null) {
              out[row.id][field.key][p] = session;
              continue;
            }
            const persisted = row.cells[field.key]?.[storedPeriod];
            if (
              session === undefined &&
              persisted?.source === "user" &&
              persisted.value !== null &&
              persisted.value !== undefined
            ) {
              out[row.id][field.key][p] = persisted.value;
              continue;
            }
            const scope: Record<string, number | null> = {};
            for (const [key, byPeriod] of Object.entries(deploymentComputed)) scope[key] = byPeriod[p] ?? null;
            for (const f of rackFields) scope[f.key] = out[row.id][f.key]?.[p] ?? null;
            const periodIndex = periods.indexOf(p);
            const priorPeriod = periodIndex > 0 ? periods[periodIndex - 1] : null;
            scope.prior_year_total_dram_demand = priorPeriod
              ? out[row.id].total_dram_demand?.[priorPeriod] ?? null
              : null;
            const formulaSession = ck in rackFormulaOverrides ? rackFormulaOverrides[ck] : undefined;
            if (formulaSession !== undefined) {
              out[row.id][field.key][p] = formulaSession ? evalExpr(formulaSession, scope, formulaNames) : evalExpr(field.expr, scope, formulaNames);
              continue;
            }
            if (persisted?.source === "formula" && persisted.formula) {
              out[row.id][field.key][p] = evalExpr(persisted.formula || "", scope, formulaNames);
              continue;
            }
            out[row.id][field.key][p] = evalExpr(field.expr, scope, formulaNames);
          }
        }
      }
    }
    return out;
  }, [deploymentComputed, rackFields, rackOverrides, rackFormulaOverrides, rackSections, periods, formulaNames]);

  const rackAggregates = useMemo(() => {
    const totals: Record<string, Record<string, number | null>> = {};
    const ensure = (key: string) => {
      if (!totals[key]) totals[key] = {};
      return totals[key];
    };
    const scenarioValue = (rowId: number, metric: string, period: string) => rackComputed[rowId]?.[metric]?.[period] ?? null;

    for (const p of periods) {
      const scenarios = rackSections.flatMap((section) =>
        section.rows.map((row) => ({
          row,
          totalDram: scenarioValue(row.id, "total_dram_demand", p),
        })),
      ).filter((scenario) => isFiniteNumber(scenario.totalDram))
        .sort((a, b) => (a.totalDram as number) - (b.totalDram as number));

      const bear = scenarios[0] ?? null;
      const bull = scenarios[scenarios.length - 1] ?? null;
      const lowerMid = scenarios.length ? scenarios[Math.floor((scenarios.length - 1) / 2)] : null;
      const upperMid = scenarios.length ? scenarios[Math.ceil((scenarios.length - 1) / 2)] : null;

      ensure("scenario_bear_total_dram_demand")[p] = bear ? bear.totalDram : null;
      ensure("scenario_bull_total_dram_demand")[p] = bull ? bull.totalDram : null;

      for (const metric of SCENARIO_BASE_METRICS) {
        const lower = lowerMid ? scenarioValue(lowerMid.row.id, metric, p) : null;
        const upper = upperMid ? scenarioValue(upperMid.row.id, metric, p) : null;
        const base = isFiniteNumber(lower) && isFiniteNumber(upper) ? (lower + upper) / 2 : isFiniteNumber(lower) ? lower : null;
        ensure(`scenario_base_${metric}`)[p] = base;
      }

      ensure("rack_total_gpus_mn")[p] = totals.scenario_base_total_gpu_packages_mn?.[p] ?? null;
      ensure("rack_foundry_kwspm_required")[p] = totals.scenario_base_foundry_kwspm_required?.[p] ?? null;
      ensure("rack_dram_kwspm_required")[p] = totals.scenario_base_dram_kwspm_required?.[p] ?? null;
      ensure("rack_hbm_demand")[p] = totals.scenario_base_hbm_demand?.[p] ?? null;
      ensure("rack_system_dram_demand")[p] = totals.scenario_base_system_dram_demand?.[p] ?? null;
      ensure("rack_gddr_dram_demand")[p] = totals.scenario_base_gddr_dram_demand?.[p] ?? null;
      ensure("rack_data_center_dram_demand")[p] = totals.scenario_base_data_center_dram_demand?.[p] ?? null;
      ensure("rack_total_server_dram_demand")[p] = totals.scenario_base_total_server_dram_demand?.[p] ?? null;
      ensure("rack_total_dram_demand")[p] = totals.scenario_base_total_dram_demand?.[p] ?? null;
      ensure("rack_implied_pc_mobile_dram_demand")[p] = totals.scenario_base_implied_pc_mobile_dram_demand?.[p] ?? null;
      ensure("rack_total_dram_demand_yoy")[p] = totals.scenario_base_total_dram_demand_yoy?.[p] ?? null;
      ensure("rack_hbm_share_total_dram_demand")[p] = totals.scenario_base_hbm_share_total_dram_demand?.[p] ?? null;
      ensure("rack_ex_hbm_dram_demand")[p] = totals.scenario_base_ex_hbm_dram_demand?.[p] ?? null;
      ensure("rack_hbm_core_kwspm")[p] = totals.scenario_base_hbm_core_kwspm?.[p] ?? null;
      ensure("rack_hbm_effective_kwspm")[p] = totals.scenario_base_hbm_effective_kwspm?.[p] ?? null;
      ensure("rack_ex_hbm_kwspm")[p] = totals.scenario_base_ex_hbm_kwspm?.[p] ?? null;
      ensure("rack_hbm_share_dram_capacity")[p] = totals.scenario_base_hbm_share_dram_capacity?.[p] ?? null;
    }
    return totals;
  }, [periods, rackComputed, rackSections]);

  const computed = useMemo(() => {
    const out: Record<string, Record<string, number | null>> = {};
    if (!data) return out;
    for (const r of data.rows) {
      out[r.key] = {};
      for (const p of periods) out[r.key][p] = r.type === "input" ? inputValue(r, p) : null;
    }
    for (const r of data.rows) {
      if (r.type !== "formula" || !r.expr) continue;
      for (const p of periods) {
        const ck = cellKey(r.key, p);
        const session = ck in overrides ? overrides[ck] : undefined;
        if (session !== undefined && session !== null) {
          out[r.key][p] = session;
          continue;
        }
        if (
          session === undefined &&
          r.cells[p]?.source === "user" &&
          r.cells[p]?.value !== null &&
          r.cells[p]?.value !== undefined
        ) {
          out[r.key][p] = r.cells[p].value;
          continue;
        }
        const scope: Record<string, number | null> = {};
        for (const k of Object.keys(out)) scope[k] = out[k][p];
        for (const [k, byPeriod] of Object.entries(rackAggregates)) scope[k] = byPeriod[p] ?? null;
        const formulaSession = ck in formulaOverrides ? formulaOverrides[ck] : undefined;
        if (formulaSession !== undefined) {
          out[r.key][p] = formulaSession ? evalExpr(formulaSession, scope, formulaNames) : evalExpr(r.expr, scope, formulaNames);
          continue;
        }
        if (r.cells[p]?.source === "formula" && r.cells[p]?.formula) {
          out[r.key][p] = evalExpr(r.cells[p].formula || "", scope, formulaNames);
          continue;
        }
        out[r.key][p] = evalExpr(r.expr, scope, formulaNames);
      }
    }
    return out;
  }, [data, periods, inputValue, overrides, formulaOverrides, rackAggregates, formulaNames]);

  const commit = useCallback(async (row: ModelRow, period: string, raw: string) => {
    const ck = cellKey(row.key, period);
    const trimmed = raw.trim();
    if (trimmed.startsWith("=") && row.type === "formula") {
      const formula = normalizeFormula(trimmed);
      setOverrides((o) => ({ ...o, [ck]: null }));
      const formulaOverride = formula && !formulaMatchesDefault(formula, row.expr, formulaNames);
      setFormulaOverrides((o) => ({ ...o, [ck]: formulaOverride ? formula : null }));
      setFocus(null);
      try {
        await api.setModelCell(row.key, period, null, formulaOverride ? formula : "");
      } catch {
        /* keep local override even if persist fails */
      }
      return;
    }
    const val = trimmed === "" ? null : Number(trimmed);
    if (val !== null && Number.isNaN(val)) {
      setFocus(null);
      return;
    }
    setOverrides((o) => ({ ...o, [ck]: val }));
    if (row.type === "formula") setFormulaOverrides((o) => ({ ...o, [ck]: null }));
    setFocus(null);
    try {
      await api.setModelCell(row.key, period, val);
    } catch {
      /* keep local override even if persist fails */
    }
  }, [formulaNames]);

  const commitRackCell = useCallback(async (row: RackRow, field: RackField, period: string, raw: string) => {
    const ck = rackCellKey(row.id, field.key, period);
    const trimmed = raw.trim();
    if (trimmed.startsWith("=") && field.type === "formula") {
      const formula = normalizeFormula(trimmed);
      setRackOverrides((o) => ({ ...o, [ck]: null }));
      const formulaOverride = formula && !formulaMatchesDefault(formula, field.expr, formulaNames);
      setRackFormulaOverrides((o) => ({ ...o, [ck]: formulaOverride ? formula : null }));
      setRackFocus(null);
      try {
        await api.setRackCell(row.id, field.key, period, null, formulaOverride ? formula : "");
      } catch {
        /* keep local override even if persist fails */
      }
      return;
    }
    if (FORMULA_VALUE_LOCKED_KEYS.has(field.key) && trimmed !== "") {
      setRackFocus(null);
      return;
    }
    const val = trimmed === "" ? null : Number(trimmed);
    if (val !== null && Number.isNaN(val)) {
      setRackFocus(null);
      return;
    }
    setRackOverrides((o) => ({ ...o, [ck]: val }));
    if (field.type === "formula") setRackFormulaOverrides((o) => ({ ...o, [ck]: null }));
    setRackFocus(null);
    try {
      await api.setRackCell(row.id, field.key, period, val);
    } catch {
      /* keep local override even if persist fails */
    }
  }, [formulaNames]);

  const refreshModelData = useCallback(async () => {
    const fresh = await api.getModel();
    qc.setQueryData(["model", "hbm"], fresh);
  }, [qc]);

  const addRack = useCallback(async (vendor: string) => {
    await api.createRackRow(vendor);
    await refreshModelData();
  }, [refreshModelData]);

  const deleteRack = useCallback(async (rowId: number) => {
    await api.deleteRackRow(rowId);
    setRackOverrides((current) => {
      const next: Record<string, number | null> = {};
      for (const [k, v] of Object.entries(current)) {
        if (!k.startsWith(`${rowId}::`)) next[k] = v;
      }
      return next;
    });
    setRackFormulaOverrides((current) => {
      const next: Record<string, string | null> = {};
      for (const [k, v] of Object.entries(current)) {
        if (!k.startsWith(`${rowId}::`)) next[k] = v;
      }
      return next;
    });
    await refreshModelData();
  }, [refreshModelData]);

  const updateRackName = useCallback(async (row: RackRow, name: string) => {
    if ((row.name || "") === name.trim()) return;
    await api.updateRackRow(row.id, { name: name.trim() });
  }, []);

  const refreshRag = useCallback(async () => {
    setRefreshing(true);
    try {
      await api.refreshModel();
      await refreshModelData();
      setOverrides({});
      setFormulaOverrides({});
      setRackFormulaOverrides({});
      acknowledge();
    } finally {
      setRefreshing(false);
    }
  }, [refreshModelData, acknowledge]);

  const incrementalPowerRows = useMemo(() => {
    if (!data || activeTab !== "hbm") return [];
    return data.rows.filter((r) => r.group === INCREMENTAL_POWER_GROUP && (r.tabs || ["hbm"]).includes("hbm"));
  }, [data, activeTab]);

  const deploymentAssumptionRows = useMemo(() => {
    if (!data || activeTab !== "hbm") return [];
    return data.rows.filter((r) => r.group === DEPLOYMENT_ASSUMPTION_GROUP && (r.tabs || ["hbm"]).includes("hbm"));
  }, [data, activeTab]);

  const grouped = useMemo(() => {
    if (!data) return [];
    const rowsForTab = data.rows.filter((r) => (
      (r.tabs || ["hbm"]).includes(activeTab) &&
      !(activeTab === "hbm" && (r.group === INCREMENTAL_POWER_GROUP || r.group === DEPLOYMENT_ASSUMPTION_GROUP))
    ));
    const groups: { name: string; rows: ModelRow[] }[] = [];
    for (const r of rowsForTab) {
      let g = groups.find((x) => x.name === r.group);
      if (!g) {
        g = { name: r.group, rows: [] };
        groups.push(g);
      }
      g.rows.push(r);
    }
    return groups;
  }, [data, activeTab]);

  const renderModelValueCell = (row: ModelRow, period: string) => {
    const ck = cellKey(row.key, period);
    const cell = row.cells[period];
    const isFocused = focus?.k === ck;
    const valueOverride = row.type === "formula" && (
      (ck in overrides && overrides[ck] !== null) ||
      (!(ck in overrides) && cell?.source === "user" && cell?.value !== null && cell?.value !== undefined)
    );
    const formulaSession = ck in formulaOverrides ? formulaOverrides[ck] : undefined;
    const customFormula = row.type === "formula" ? cellFormula(cell, row.expr, formulaSession) : "";
    const formulaOverride = row.type === "formula" && (
      (formulaSession !== undefined && formulaSession !== null && formulaSession !== "" && !formulaMatchesDefault(formulaSession, row.expr, formulaNames)) ||
      (formulaSession === undefined && cell?.source === "formula" && !!cell?.formula && !formulaMatchesDefault(cell.formula, row.expr, formulaNames))
    );
    const display = isFocused ? focus.text : fmt(computed[row.key]?.[period] ?? null, row.unit);
    const focusText = row.type === "formula" && !valueOverride ? formulaDisplay(customFormula, formulaNames) : display;
    const src = ck in overrides
      ? overrides[ck] === null ? "default" : "user"
      : formulaOverride ? "formula" : row.type === "input" ? cell?.source : "computed";
    const dot = src === "user" ? "bg-blue-500" : src === "formula" ? "bg-amber-500" : src === "rag" ? "bg-emerald-500" : "bg-transparent";
    return (
      <div className="relative inline-flex items-center">
        <span
          className={`mr-1 h-1.5 w-1.5 rounded-full ${dot}`}
          title={src === "rag" ? `From RAG: ${cell?.quote || ""}` : src === "user" ? "Manually edited" : "Computed/default"}
        />
        <input
          type="text"
          inputMode="decimal"
          value={display}
          title={row.type === "formula" ? formulaDisplay(customFormula, formulaNames) : undefined}
          onFocus={() => setFocus({ k: ck, text: focusText })}
          onClick={() => {
            if (!isFocused) setFocus({ k: ck, text: focusText });
          }}
          onChange={(e) => {
            const text = e.target.value;
            setFocus({ k: ck, text });
            const t = text.trim();
            if (row.type === "formula" && t.startsWith("=")) {
              const formula = normalizeFormula(t);
              const formulaOverride = formula && !formulaMatchesDefault(formula, row.expr, formulaNames);
              setOverrides((o) => ({ ...o, [ck]: null }));
              setFormulaOverrides((o) => ({ ...o, [ck]: formulaOverride ? formula : null }));
              return;
            }
            const v = t === "" ? null : Number(t);
            if (!(v !== null && Number.isNaN(v))) {
              setOverrides((o) => ({ ...o, [ck]: v }));
              if (row.type === "formula") setFormulaOverrides((o) => ({ ...o, [ck]: null }));
            }
          }}
          onBlur={(e) => commit(row, period, e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
          className={`w-24 rounded border px-1.5 py-0.5 text-center text-xs focus:outline-none focus:ring-1 ${inputTextClass(row.type === "formula", valueOverride, display)} ${inputBorderClass(valueOverride, row.type === "input", formulaOverride)}`}
        />
      </div>
    );
  };

  return (
    <div className="mx-auto max-w-[1380px] px-6 py-6">
      <div className="mb-4 flex items-center justify-between gap-4">
        <div>
          <h2 className="text-lg font-bold">SK Hynix HBM Rack Demand Model</h2>
          <p className="text-sm text-muted-foreground">
            Editable rack editions translate provisioned power into rack count, server DRAM demand, total DRAM demand, and HBM share.
          </p>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <button
            onClick={refreshRag}
            disabled={refreshing}
            className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            title="Re-pull static RAG-backed market inputs. Dynamic rack assumptions are manual."
          >
            {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
            {refreshing ? "Pulling..." : "Refresh from RAG"}
          </button>
        </div>
      </div>

      {newChunks > 0 && !refreshing && (
        <div className="mb-4 flex items-center justify-between rounded-md border border-amber-300/60 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          <span className="flex items-center gap-1.5">
            <Sparkles className="h-3.5 w-3.5" />
            <strong>{newChunks.toLocaleString()}</strong> new chunk{newChunks === 1 ? "" : "s"} in the fact pool since the last RAG pull.
          </span>
          <button
            onClick={refreshRag}
            className="rounded border border-amber-400 bg-white px-2 py-0.5 text-[11px] font-semibold text-amber-900 hover:bg-amber-100"
          >
            Pull from RAG
          </button>
        </div>
      )}

      {isLoading && (
        <div className="py-20 text-center text-muted-foreground">
          <Loader2 className="mx-auto mb-3 h-6 w-6 animate-spin" /> Loading model...
        </div>
      )}
      {error && (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          Failed to load model: {String(error)}
        </div>
      )}

      {data && (
        <>
          <NewsTable />

          <div className="my-6 border-t" />

          <div className="mb-3 inline-flex rounded-md border p-0.5">
            {tabsWithRows.map((t) => (
              <button
                key={t}
                onClick={() => setActiveTab(t)}
                className={`rounded px-4 py-1.5 text-sm font-medium transition-colors ${
                  activeTab === t ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"
                }`}
              >
                {TAB_LABELS[t] || t}
              </button>
            ))}
          </div>

          {incrementalPowerRows.length > 0 && (
            <div className="mb-4 overflow-x-auto rounded-lg border">
              <table className="w-full min-w-[1040px] table-fixed border-collapse text-sm">
                <colgroup>
                  <col className="w-[280px]" />
                  <col className="w-20" />
                  {periods.map((p) => (
                    <col key={p} className="w-28" />
                  ))}
                </colgroup>
                <thead>
                  <tr className="border-b bg-muted/50">
                    <th className="sticky left-0 z-10 bg-muted/50 px-3 py-2 text-left font-semibold">Incremental Provisioned Power</th>
                    <th className="px-2 py-2 text-right font-normal text-muted-foreground">Unit</th>
                    {periods.map((p) => (
                      <th key={p} className="px-3 py-2 text-right font-semibold">{p}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {incrementalPowerRows.map((row) => (
                    <tr key={row.key} className={`border-b last:border-0 ${row.highlight ? "bg-primary/5 font-semibold" : ""}`}>
                      <td className="sticky left-0 z-10 bg-background px-3 py-1.5">
                        <span className={row.highlight ? "font-semibold" : ""}>{row.label}</span>
                        {row.type === "formula" && (
                          <span className="ml-1.5 text-[9px] uppercase text-muted-foreground" title={formulaDisplay(row.expr, formulaNames)}>f(x)</span>
                        )}
                      </td>
                      <td className="px-2 py-1.5 text-right text-[11px] text-muted-foreground">{row.unit}</td>
                      {periods.map((p) => (
                        <td key={p} className="px-1 py-1 text-right">
                          {renderModelValueCell(row, p)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {deploymentAssumptionRows.length > 0 && (
            <div className="mb-6 overflow-x-auto rounded-lg border">
              <table className="w-full min-w-[1040px] table-fixed border-collapse text-sm">
                <colgroup>
                  <col className="w-[280px]" />
                  <col className="w-20" />
                  {periods.map((p) => (
                    <col key={p} className="w-28" />
                  ))}
                </colgroup>
                <thead>
                  <tr className="border-b bg-muted/50">
                    <th className="sticky left-0 z-10 bg-muted/50 px-3 py-2 text-left font-semibold">Deployment Assumption - per 1GW</th>
                    <th className="px-2 py-2 text-right font-normal text-muted-foreground">Unit</th>
                    {periods.map((p) => (
                      <th key={p} className="px-3 py-2 text-right font-semibold">{p}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {deploymentAssumptionRows.map((row) => (
                    <tr key={row.key} className={`border-b last:border-0 ${row.highlight ? "bg-primary/5 font-semibold" : ""}`}>
                      <td className="sticky left-0 z-10 bg-background px-3 py-1.5">
                        <span className={row.highlight ? "font-semibold" : ""}>{row.label}</span>
                        {row.type === "formula" && (
                          <span className="ml-1.5 text-[9px] uppercase text-muted-foreground" title={formulaDisplay(row.expr, formulaNames)}>f(x)</span>
                        )}
                      </td>
                      <td className="px-2 py-1.5 text-right text-[11px] text-muted-foreground">{row.unit}</td>
                      {periods.map((p) => (
                        <td key={p} className="px-1 py-1 text-right">
                          {renderModelValueCell(row, p)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {activeTab === "hbm" && rackFields.length > 0 && (
            <>
              <RackEditor
                periods={periods}
                rackFields={rackFields}
                formulaNames={formulaNames}
                rackSections={rackSections}
                rackComputed={rackComputed}
                rackOverrides={rackOverrides}
                rackFormulaOverrides={rackFormulaOverrides}
                rackFocus={rackFocus}
                setRackFocus={setRackFocus}
                setRackOverrides={setRackOverrides}
                setRackFormulaOverrides={setRackFormulaOverrides}
                addRack={addRack}
                deleteRack={deleteRack}
                updateRackName={updateRackName}
                commitRackCell={commitRackCell}
              />
              <ScenarioOutputs
                periods={periods}
                modelRows={data.rows}
                rackFields={rackFields}
                formulaNames={formulaNames}
                rackSections={rackSections}
                rackComputed={rackComputed}
                deploymentComputed={deploymentComputed}
                scenarioSummary={rackAggregates}
                overrides={overrides}
                rackOverrides={rackOverrides}
                formulaOverrides={formulaOverrides}
                rackFormulaOverrides={rackFormulaOverrides}
                setOverrides={setOverrides}
                setRackOverrides={setRackOverrides}
                setFormulaOverrides={setFormulaOverrides}
                setRackFormulaOverrides={setRackFormulaOverrides}
              />
            </>
          )}

          {grouped.length === 0 ? (
            <div className="rounded-lg border bg-muted/20 px-4 py-10 text-center">
              <p className="text-sm font-semibold">{TAB_LABELS[activeTab] || activeTab} model logic is not defined yet.</p>
              <p className="mt-1 text-xs text-muted-foreground">The tab is wired and reserved; rows will appear here once this model logic is added.</p>
            </div>
          ) : (
            <>
              <div className="overflow-x-auto rounded-lg border">
                <table className="w-full border-collapse text-sm">
                  <thead>
                    <tr className="border-b bg-muted/50">
                      <th className="sticky left-0 z-10 bg-muted/50 px-3 py-2 text-left font-semibold">Metric</th>
                      <th className="px-2 py-2 text-right font-normal text-muted-foreground">Unit</th>
                      {periods.map((p) => (
                        <th key={p} className="px-3 py-2 text-right font-semibold">{p}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {grouped.map((g) => (
                      <Fragment key={g.name || "__group"}>
                        {g.name && (
                          <tr className="bg-muted/30">
                            <td colSpan={periods.length + 2} className="px-3 py-1.5 text-xs font-bold uppercase tracking-wide text-muted-foreground">
                              {g.name}
                            </td>
                          </tr>
                        )}
                        {g.rows.map((row) => (
                          <tr key={row.key} className={`border-b last:border-0 ${row.highlight ? "bg-primary/5 font-semibold" : ""}`}>
                            <td className="sticky left-0 z-10 bg-background px-3 py-1.5">
                              <span className={row.highlight ? "font-semibold" : ""}>{row.label}</span>
                              {row.type === "formula" && (
                                <span className="ml-1.5 text-[9px] uppercase text-muted-foreground" title={formulaDisplay(row.expr, formulaNames)}>f(x)</span>
                              )}
                            </td>
                            <td className="px-2 py-1.5 text-right text-[11px] text-muted-foreground">{row.unit}</td>
                            {periods.map((p) => (
                              <td key={p} className="px-1 py-1 text-right">
                                {renderModelValueCell(row, p)}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="mt-2 flex items-center gap-3 text-[11px] text-muted-foreground">
                <span className="inline-flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-emerald-500" /> from RAG</span>
                <span className="inline-flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-blue-500" /> your edit</span>
                <span>Red outline = formula override</span>
              </p>
            </>
          )}
        </>
      )}
    </div>
  );
}

function ScenarioOutputs({
  periods,
  modelRows,
  rackFields,
  formulaNames,
  rackSections,
  rackComputed,
  deploymentComputed,
  scenarioSummary,
  overrides,
  rackOverrides,
  formulaOverrides,
  rackFormulaOverrides,
  setOverrides,
  setRackOverrides,
  setFormulaOverrides,
  setRackFormulaOverrides,
}: {
  periods: string[];
  modelRows: ModelRow[];
  rackFields: RackField[];
  formulaNames: FormulaNames;
  rackSections: ModelResponse["rack_sections"];
  rackComputed: Record<number, Record<string, Record<string, number | null>>>;
  deploymentComputed: Record<string, Record<string, number | null>>;
  scenarioSummary: Record<string, Record<string, number | null>>;
  overrides: Record<string, number | null>;
  rackOverrides: Record<string, number | null>;
  formulaOverrides: Record<string, string | null>;
  rackFormulaOverrides: Record<string, string | null>;
  setOverrides: Dispatch<SetStateAction<Record<string, number | null>>>;
  setRackOverrides: Dispatch<SetStateAction<Record<string, number | null>>>;
  setFormulaOverrides: Dispatch<SetStateAction<Record<string, string | null>>>;
  setRackFormulaOverrides: Dispatch<SetStateAction<Record<string, string | null>>>;
}) {
  const [outputFocus, setOutputFocus] = useState<{ k: string; text: string } | null>(null);
  const modelRowByKey = useMemo(() => Object.fromEntries(modelRows.map((row) => [row.key, row])), [modelRows]);
  const rackFieldByKey = useMemo(() => Object.fromEntries(rackFields.map((field) => [field.key, field])), [rackFields]);

  const scenarios = useMemo(() => {
    const rows: { id: number; label: string; row: RackRow }[] = [];
    for (const section of rackSections ?? []) {
      section.rows.forEach((row, index) => {
        const rawName = (row.name || "").trim();
        const displayName = section.vendor === "NVDA" && /GB300/i.test(rawName)
          ? "GB300"
          : rawName || `Rack ${index + 1}`;
        rows.push({
          id: row.id,
          label: `${section.vendor} - ${displayName}`,
          row,
        });
      });
    }
    return rows;
  }, [rackSections]);

  if (scenarios.length === 0) return null;

  const rangeRows = [
    { key: "scenario_bear_total_dram_demand", label: "Bear", unit: "bn Gb" },
    { key: "scenario_base_total_dram_demand", label: "Base (median)", unit: "bn Gb" },
    { key: "scenario_bull_total_dram_demand", label: "Bull", unit: "bn Gb" },
  ];

  const outputValue = (
    scenarioId: number,
    metric: ScenarioMetric,
    period: string,
  ) => {
    if (metric.source === "deployment") return deploymentComputed[metric.key]?.[period] ?? null;
    return rackComputed[scenarioId]?.[metric.key]?.[period] ?? null;
  };

  const commitOutputCell = async (
    scenario: { id: number; row: RackRow },
    metric: ScenarioMetric,
    period: string,
    raw: string,
  ) => {
    const trimmed = raw.trim();
    if (trimmed.startsWith("=")) {
      const formula = normalizeFormula(trimmed);
      if (metric.source === "deployment") {
        const modelRow = modelRowByKey[metric.key];
        if (!modelRow || modelRow.type !== "formula") return;
        const ck = `${modelRow.key}::${period}`;
        const formulaOverride = formula && !formulaMatchesDefault(formula, modelRow.expr, formulaNames);
        setOverrides((current) => ({ ...current, [ck]: null }));
        setFormulaOverrides((current) => ({ ...current, [ck]: formulaOverride ? formula : null }));
        setOutputFocus(null);
        try {
          await api.setModelCell(modelRow.key, period, null, formulaOverride ? formula : "");
        } catch {
          /* keep local override even if persist fails */
        }
        return;
      }
      const field = rackFieldByKey[metric.key];
      if (!field || field.type !== "formula") return;
      const storagePeriod = rackFieldPeriod(field, period);
      const ck = `${scenario.id}::${field.key}::${storagePeriod}`;
      const formulaOverride = formula && !formulaMatchesDefault(formula, field.expr, formulaNames);
      setRackOverrides((current) => ({ ...current, [ck]: null }));
      setRackFormulaOverrides((current) => ({ ...current, [ck]: formulaOverride ? formula : null }));
      setOutputFocus(null);
      try {
        await api.setRackCell(scenario.id, field.key, storagePeriod, null, formulaOverride ? formula : "");
      } catch {
        /* keep local override even if persist fails */
      }
      return;
    }
    if (metric.source === "rack" && FORMULA_VALUE_LOCKED_KEYS.has(metric.key) && trimmed !== "") {
      setOutputFocus(null);
      return;
    }
    const val = trimmed === "" ? null : Number(trimmed);
    if (val !== null && Number.isNaN(val)) {
      setOutputFocus(null);
      return;
    }
    setOutputFocus(null);
    if (metric.source === "deployment") {
      const modelRow = modelRowByKey[metric.key];
      if (!modelRow) return;
      const ck = `${modelRow.key}::${period}`;
      setOverrides((current) => ({ ...current, [ck]: val }));
      if (modelRow.type === "formula") setFormulaOverrides((current) => ({ ...current, [ck]: null }));
      try {
        await api.setModelCell(modelRow.key, period, val);
      } catch {
        /* keep local override even if persist fails */
      }
      return;
    }
    const field = rackFieldByKey[metric.key];
    if (!field) return;
    const storagePeriod = rackFieldPeriod(field, period);
    const ck = `${scenario.id}::${field.key}::${storagePeriod}`;
    setRackOverrides((current) => ({ ...current, [ck]: val }));
    if (field.type === "formula") setRackFormulaOverrides((current) => ({ ...current, [ck]: null }));
    try {
      await api.setRackCell(scenario.id, field.key, storagePeriod, val);
    } catch {
      /* keep local override even if persist fails */
    }
  };

  const renderOutputCell = (
    scenario: { id: number; row: RackRow },
    metric: ScenarioMetric,
    period: string,
  ) => {
    const modelRow = metric.source === "deployment" ? modelRowByKey[metric.key] : undefined;
    const field = metric.source === "rack" ? rackFieldByKey[metric.key] : undefined;
    const formulaKey = metric.source === "deployment"
      ? `${metric.key}::${period}`
      : `${scenario.id}::${metric.key}::${field ? rackFieldPeriod(field, period) : period}`;
    const cell = metric.source === "deployment"
      ? modelRow?.cells[period]
      : field ? scenario.row.cells[metric.key]?.[rackFieldPeriod(field, period)] : undefined;
    const formulaSession = metric.source === "deployment"
      ? (formulaKey in formulaOverrides ? formulaOverrides[formulaKey] : undefined)
      : (formulaKey in rackFormulaOverrides ? rackFormulaOverrides[formulaKey] : undefined);
    const formula = metric.source === "deployment"
      ? cellFormula(cell, modelRow?.expr, formulaSession)
      : cellFormula(cell, field?.expr, formulaSession);
    const isFormula = Boolean(formula);
    const value = outputValue(scenario.id, metric, period);
    const key = metric.source === "deployment" ? `model::${formulaKey}` : formulaKey;
    const isFocused = outputFocus?.k === key;
    const display = isFocused ? outputFocus.text : fmt(value, metric.unit);

    let valueOverride = false;
    let formulaOverride = false;
    if (metric.source === "deployment") {
      const ck = `${metric.key}::${period}`;
      valueOverride = Boolean(modelRow?.type === "formula" && (
        (ck in overrides && overrides[ck] !== null) ||
        (!(ck in overrides) && cell?.source === "user" && cell.value !== null && cell.value !== undefined)
      ));
      formulaOverride = Boolean(modelRow?.type === "formula" && (
        (ck in formulaOverrides && formulaOverrides[ck] !== null && formulaOverrides[ck] !== "" && !formulaMatchesDefault(formulaOverrides[ck], modelRow.expr, formulaNames)) ||
        (!(ck in formulaOverrides) && cell?.source === "formula" && !!cell.formula && !formulaMatchesDefault(cell.formula, modelRow.expr, formulaNames))
      ));
    } else {
      if (!field) return null;
      const storagePeriod = rackFieldPeriod(field, period);
      const ck = `${scenario.id}::${metric.key}::${storagePeriod}`;
      valueOverride = Boolean(field?.type === "formula" && (
        (ck in rackOverrides && rackOverrides[ck] !== null) ||
        (!(ck in rackOverrides) && cell?.source === "user" && cell.value !== null && cell.value !== undefined)
      ));
      formulaOverride = Boolean(field?.type === "formula" && (
        (ck in rackFormulaOverrides && rackFormulaOverrides[ck] !== null && rackFormulaOverrides[ck] !== "" && !formulaMatchesDefault(rackFormulaOverrides[ck], field.expr, formulaNames)) ||
        (!(ck in rackFormulaOverrides) && cell?.source === "formula" && !!cell.formula && !formulaMatchesDefault(cell.formula, field.expr, formulaNames))
      ));
    }
    const focusText = isFormula && !valueOverride ? formulaDisplay(formula, formulaNames) : display;

    return (
      <input
        type="text"
        inputMode="decimal"
        value={display}
        title={isFormula ? formulaDisplay(formula, formulaNames) : ""}
        onFocus={() => setOutputFocus({ k: key, text: focusText })}
        onClick={() => {
          if (!isFocused) setOutputFocus({ k: key, text: focusText });
        }}
        onChange={(e) => {
          const text = e.target.value;
          setOutputFocus({ k: key, text });
          const t = text.trim();
          if (isFormula && t.startsWith("=")) {
            if (metric.source === "deployment") {
              const ck = `${metric.key}::${period}`;
              const formula = normalizeFormula(t);
              const formulaOverride = formula && !formulaMatchesDefault(formula, modelRow?.expr, formulaNames);
              setOverrides((current) => ({ ...current, [ck]: null }));
              setFormulaOverrides((current) => ({ ...current, [ck]: formulaOverride ? formula : null }));
            } else {
              const field = rackFieldByKey[metric.key];
              if (!field) return;
              const storagePeriod = rackFieldPeriod(field, period);
              const ck = `${scenario.id}::${metric.key}::${storagePeriod}`;
              const formula = normalizeFormula(t);
              const formulaOverride = formula && !formulaMatchesDefault(formula, field.expr, formulaNames);
              setRackOverrides((current) => ({ ...current, [ck]: null }));
              setRackFormulaOverrides((current) => ({ ...current, [ck]: formulaOverride ? formula : null }));
            }
            return;
          }
          const v = t === "" ? null : Number(t);
          if (!(v !== null && Number.isNaN(v))) {
            if (metric.source === "deployment") {
              const ck = `${metric.key}::${period}`;
              setOverrides((current) => ({ ...current, [ck]: v }));
              if (modelRow?.type === "formula") setFormulaOverrides((current) => ({ ...current, [ck]: null }));
            } else {
              if (FORMULA_VALUE_LOCKED_KEYS.has(metric.key) && t !== "" && !t.startsWith("=")) return;
              const field = rackFieldByKey[metric.key];
              if (!field) return;
              const storagePeriod = rackFieldPeriod(field, period);
              const ck = `${scenario.id}::${metric.key}::${storagePeriod}`;
              setRackOverrides((current) => ({ ...current, [ck]: v }));
              if (field.type === "formula") setRackFormulaOverrides((current) => ({ ...current, [ck]: null }));
            }
          }
        }}
        onBlur={(e) => commitOutputCell(scenario, metric, period, e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        }}
        className={`w-24 rounded border px-1.5 py-0.5 text-center text-xs focus:outline-none focus:ring-1 ${inputTextClass(isFormula, valueOverride, display)} ${inputBorderClass(valueOverride, false, formulaOverride)}`}
      />
    );
  };

  return (
    <div className="mb-6 space-y-4">
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b bg-muted/50">
              <th className="sticky left-0 z-10 bg-muted/50 px-3 py-2 text-left font-semibold">Scenario Range</th>
              <th className="px-2 py-2 text-right font-normal text-muted-foreground">Unit</th>
              {periods.map((p) => (
                <th key={p} className="px-3 py-2 text-right font-semibold">{p}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rangeRows.map((row) => (
              <tr key={row.key} className="border-b last:border-0">
                <td className="sticky left-0 z-10 bg-background px-3 py-1.5 font-semibold">{row.label}</td>
                <td className="px-2 py-1.5 text-right text-[11px] text-muted-foreground">{row.unit}</td>
                {periods.map((p) => (
                  <td key={p} className="px-3 py-1.5 text-right text-xs">
                    {fmt(scenarioSummary[row.key]?.[p] ?? null, row.unit)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b bg-muted/50">
              <th className="sticky left-0 z-10 bg-muted/50 px-3 py-2 text-left font-semibold">100% Rack Scenario Outputs</th>
              <th className="px-3 py-2 text-left font-semibold">Metric</th>
              <th className="px-2 py-2 text-right font-normal text-muted-foreground">Unit</th>
              {periods.map((p) => (
                <th key={p} className="px-3 py-2 text-right font-semibold">{p}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {scenarios.map((scenario) => (
              <Fragment key={scenario.id}>
                {SCENARIO_OUTPUT_GROUPS.map((group, groupIndex) => (
                  <Fragment key={`${scenario.id}-${group.name}`}>
                    <tr className="border-b bg-muted/10">
                      <td className="sticky left-0 z-10 bg-muted/10 px-3 py-1.5 text-xs font-bold">
                        {groupIndex === 0 ? scenario.label : ""}
                      </td>
                      <td colSpan={periods.length + 2} className="px-3 py-1.5 text-[11px] font-bold text-muted-foreground">
                        {group.name}
                      </td>
                    </tr>
                    {group.metrics.map((metric, metricIndex) => {
                      const isResultRow = metricIndex === group.metrics.length - 1;
                      const metricFormula = rackFieldByKey[metric.key]?.expr;
                      return (
                      <tr key={`${scenario.id}-${metric.key}`} className={`border-b last:border-0 ${isResultRow ? "bg-primary/5 font-semibold" : ""}`}>
                        <td className="sticky left-0 z-10 bg-background px-3 py-1.5" />
                        <td className="px-3 py-1.5">
                          {metric.label}
                          {metricFormula && (
                            <span
                              className="ml-1.5 text-[9px] uppercase text-muted-foreground"
                              title={formulaDisplay(metricFormula, formulaNames)}
                            >
                              f(x)
                            </span>
                          )}
                        </td>
                        <td className="px-2 py-1.5 text-right text-[11px] text-muted-foreground">{metric.unit}</td>
                        {periods.map((p) => (
                          <td key={p} className="px-3 py-1.5 text-right text-xs">
                            {renderOutputCell(scenario, metric, p)}
                          </td>
                        ))}
                      </tr>
                      );
                    })}
                  </Fragment>
                ))}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RackEditor({
  periods,
  rackFields,
  formulaNames,
  rackSections,
  rackComputed,
  rackOverrides,
  rackFormulaOverrides,
  rackFocus,
  setRackFocus,
  setRackOverrides,
  setRackFormulaOverrides,
  addRack,
  deleteRack,
  updateRackName,
  commitRackCell,
}: {
  periods: string[];
  rackFields: RackField[];
  formulaNames: FormulaNames;
  rackSections: ModelResponse["rack_sections"];
  rackComputed: Record<number, Record<string, Record<string, number | null>>>;
  rackOverrides: Record<string, number | null>;
  rackFormulaOverrides: Record<string, string | null>;
  rackFocus: { k: string; text: string } | null;
  setRackFocus: (v: { k: string; text: string } | null) => void;
  setRackOverrides: Dispatch<SetStateAction<Record<string, number | null>>>;
  setRackFormulaOverrides: Dispatch<SetStateAction<Record<string, string | null>>>;
  addRack: (vendor: string) => Promise<void>;
  deleteRack: (rowId: number) => Promise<void>;
  updateRackName: (row: RackRow, name: string) => Promise<void>;
  commitRackCell: (row: RackRow, field: RackField, period: string, raw: string) => Promise<void>;
}) {
  const fieldGroups = useMemo(() => {
    const groups: { name: string; fields: RackField[] }[] = [];
    for (const field of rackFields.filter((f) => f.display !== false && f.period_mode === "single")) {
      let group = groups.find((g) => g.name === field.group);
      if (!group) {
        group = { name: field.group, fields: [] };
        groups.push(group);
      }
      group.fields.push(field);
    }
    return groups;
  }, [rackFields]);
  const [expandedVendors, setExpandedVendors] = useState<Record<string, boolean>>({});

  const rackCellKey = (rowId: number, fieldKey: string, period: string) => `${rowId}::${fieldKey}::${period}`;

  const toggleVendor = (vendor: string) => {
    setExpandedVendors((current) => ({ ...current, [vendor]: !current[vendor] }));
  };

  const addRackAndExpand = async (vendor: string) => {
    await addRack(vendor);
    setExpandedVendors((current) => ({ ...current, [vendor]: true }));
  };

  const renderRackValueCell = (row: RackRow, field: RackField, storagePeriod: string, calcPeriod: string) => {
    const ck = rackCellKey(row.id, field.key, storagePeriod);
    const cell = row.cells[field.key]?.[storagePeriod];
    const isFocused = rackFocus?.k === ck;
    const valueOverride = field.type === "formula" && (
      (ck in rackOverrides && rackOverrides[ck] !== null) ||
      (!(ck in rackOverrides) && cell?.source === "user" && cell.value !== null && cell.value !== undefined)
    );
    const formulaSession = ck in rackFormulaOverrides ? rackFormulaOverrides[ck] : undefined;
    const customFormula = field.type === "formula" ? cellFormula(cell, field.expr, formulaSession) : "";
    const formulaOverride = field.type === "formula" && (
      (formulaSession !== undefined && formulaSession !== null && formulaSession !== "" && !formulaMatchesDefault(formulaSession, field.expr, formulaNames)) ||
      (formulaSession === undefined && cell?.source === "formula" && !!cell?.formula && !formulaMatchesDefault(cell.formula, field.expr, formulaNames))
    );
    const display = isFocused ? rackFocus.text : fmt(rackComputed[row.id]?.[field.key]?.[calcPeriod] ?? null, field.unit);
    const focusText = field.type === "formula" && !valueOverride ? formulaDisplay(customFormula, formulaNames) : display;
    const src = ck in rackOverrides
      ? rackOverrides[ck] === null ? "default" : "user"
      : formulaOverride ? "formula" : field.type === "input" ? cell?.source : "computed";
    const dot = src === "user" ? "bg-blue-500" : src === "formula" ? "bg-amber-500" : "bg-transparent";
    return (
      <div className="relative inline-flex items-center">
        <span
          className={`mr-1 h-1.5 w-1.5 rounded-full ${dot}`}
          title={src === "user" ? "Manually edited" : field.type === "formula" ? "Computed" : "Empty"}
        />
        <input
          type="text"
          inputMode="decimal"
          value={display}
          title={field.type === "formula" ? formulaDisplay(customFormula, formulaNames) : undefined}
          onFocus={() => setRackFocus({ k: ck, text: focusText })}
          onClick={() => {
            if (!isFocused) setRackFocus({ k: ck, text: focusText });
          }}
          onChange={(e) => {
            const text = e.target.value;
            setRackFocus({ k: ck, text });
            const t = text.trim();
            if (field.type === "formula" && t.startsWith("=")) {
              const formula = normalizeFormula(t);
              const formulaOverride = formula && !formulaMatchesDefault(formula, field.expr, formulaNames);
              setRackOverrides((o) => ({ ...o, [ck]: null }));
              setRackFormulaOverrides((o) => ({ ...o, [ck]: formulaOverride ? formula : null }));
              return;
            }
            const v = t === "" ? null : Number(t);
            if (!(v !== null && Number.isNaN(v))) {
              setRackOverrides((o) => ({ ...o, [ck]: v }));
              if (field.type === "formula") setRackFormulaOverrides((o) => ({ ...o, [ck]: null }));
            }
          }}
          onBlur={(e) => commitRackCell(row, field, storagePeriod, e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
          className={`w-24 rounded border px-1.5 py-0.5 text-center text-xs focus:outline-none focus:ring-1 ${inputTextClass(field.type === "formula", valueOverride, display)} ${inputBorderClass(valueOverride, field.type === "input", formulaOverride)}`}
        />
      </div>
    );
  };

  return (
    <div className="mb-6 space-y-4">
      {rackSections?.map((section) => (
        <section key={section.vendor} className="rounded-lg border bg-background">
          <div className="flex items-center justify-between border-b bg-muted/30 px-3 py-2">
            <button
              type="button"
              onClick={() => toggleVendor(section.vendor)}
              className="inline-flex min-w-0 items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-muted"
              aria-expanded={!!expandedVendors[section.vendor]}
            >
              {expandedVendors[section.vendor] ? (
                <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
              ) : (
                <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
              )}
              <h3 className="text-sm font-bold">{section.label}</h3>
              <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] font-semibold text-muted-foreground">
                {section.rows.length}
              </span>
            </button>
            <button
              onClick={() => addRackAndExpand(section.vendor)}
              className="inline-flex items-center gap-1.5 rounded-md border bg-background px-2.5 py-1 text-xs font-semibold hover:bg-muted"
            >
              <Plus className="h-3.5 w-3.5" /> Add rack
            </button>
          </div>
          {expandedVendors[section.vendor] && (
          <div className="space-y-4 p-3">
            {section.rows.length === 0 && (
              <div className="rounded-md border border-dashed px-3 py-6 text-center text-xs text-muted-foreground">
                No rack editions yet.
              </div>
            )}
            {section.rows.map((row, index) => (
              <div key={row.id} className="overflow-hidden rounded-md border">
                <div className="flex items-center justify-between gap-3 border-b bg-muted/20 px-3 py-2">
                  <input
                    type="text"
                    defaultValue={row.name || ""}
                    placeholder={`${section.vendor} rack edition ${index + 1}`}
                    onBlur={(e) => updateRackName(row, e.target.value)}
                    className="min-w-0 flex-1 rounded border bg-background px-2 py-1 text-sm font-semibold focus:outline-none focus:ring-1 focus:ring-primary/40"
                  />
                  <button
                    onClick={() => deleteRack(row.id)}
                    className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-semibold text-destructive hover:bg-destructive/5"
                  >
                    <Trash2 className="h-3.5 w-3.5" /> Delete
                  </button>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full border-collapse text-sm">
                    <thead>
                      <tr className="border-b bg-muted/40">
                        <th className="sticky left-0 z-10 bg-muted/40 px-3 py-2 text-left font-semibold">Assumption</th>
                        <th className="px-2 py-2 text-center text-xs font-normal text-muted-foreground">Unit</th>
                        <th className="px-3 py-2 text-center text-xs font-semibold">Spec</th>
                      </tr>
                    </thead>
                    <tbody>
                      {fieldGroups.map((group, groupIndex) => (
                        <Fragment key={`${row.id}-${group.name}`}>
                          {groupIndex > 0 && (
                            <tr aria-hidden="true">
                              <td colSpan={3} className="h-3 bg-background p-0" />
                            </tr>
                          )}
                          <tr className="bg-muted/20">
                            <td colSpan={3} className="px-3 py-1.5 text-[11px] font-bold uppercase tracking-wide text-muted-foreground">
                              {group.name}
                            </td>
                          </tr>
                          {group.fields.map((field) => (
                            <tr key={`${row.id}-${field.key}`} className={`border-b last:border-0 ${field.highlight ? "bg-primary/5 font-semibold" : ""}`}>
                              <td className="sticky left-0 z-10 bg-background px-3 py-1.5">
                                  {field.label}
                                  {field.type === "formula" && (
                                    <span className="ml-1.5 text-[9px] uppercase text-muted-foreground" title={formulaDisplay(field.expr, formulaNames)}>f(x)</span>
                                  )}
                              </td>
                              <td className="px-2 py-1.5 text-center text-[11px] text-muted-foreground">{field.unit}</td>
                              <td className="px-1 py-1 text-center">
                                {renderRackValueCell(row, field, RACK_SPEC_PERIOD, periods[0] ?? RACK_SPEC_PERIOD)}
                              </td>
                            </tr>
                          ))}
                        </Fragment>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
          </div>
          )}
        </section>
      ))}
    </div>
  );
}

function NewsTable() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["modelNews"], queryFn: api.getModelNews, staleTime: 300_000 });
  const [refreshing, setRefreshing] = useState(false);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const fresh = await api.refreshModelNews();
      qc.setQueryData(["modelNews"], fresh);
    } finally {
      setRefreshing(false);
    }
  }, [qc]);

  const rows = useMemo(() => {
    const dateRank = (value?: string) => {
      const match = String(value || "").match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
      if (!match) return 0;
      return Number(match[1]) * 10000 + Number(match[2]) * 100 + Number(match[3]);
    };
    return [...(data?.rows ?? [])].sort((a, b) => dateRank(b.date) - dateRank(a.date));
  }, [data?.rows]);

  return (
    <div className="mt-8">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Newspaper className="h-4 w-4 text-primary" />
          <h3 className="text-sm font-bold">SK Hynix - Latest News & Guidance</h3>
          <span className="text-[11px] text-muted-foreground">(web search - Tavily)</span>
        </div>
        <div className="flex items-center gap-3">
          {data?.last_updated && <span className="text-[11px] text-muted-foreground">Updated {data.last_updated}</span>}
          <button
            onClick={refresh}
            disabled={refreshing}
            className="flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
          >
            {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            Refresh news
          </button>
        </div>
      </div>

      {data?.message && rows.length === 0 && (
        <div className="rounded-md border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">{data.message}</div>
      )}

      {(isLoading || refreshing) && (
        <div className="py-8 text-center text-muted-foreground">
          <Loader2 className="mx-auto mb-2 h-5 w-5 animate-spin" /> Pulling latest news...
        </div>
      )}

      {rows.length > 0 && (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b bg-muted/50 text-left">
                <th className="px-3 py-2 font-semibold">Topic</th>
                <th className="px-3 py-2 font-semibold">News / Guidance</th>
                <th className="px-3 py-2 font-semibold">Key Data</th>
                <th className="px-3 py-2 font-semibold">Source</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className="border-b last:border-0 align-top">
                  <td className="whitespace-nowrap px-3 py-2 text-xs font-medium">{r.topic}</td>
                  <td className="px-3 py-2 text-xs leading-relaxed">{r.news_guidance}</td>
                  <td className="px-3 py-2 text-xs tabular-nums">{r.key_data}</td>
                  <td className="px-3 py-2 text-xs">
                    {r.url ? (
                      <a href={r.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">
                        {r.source || "link"} <ExternalLink className="h-3 w-3" />
                      </a>
                    ) : (
                      r.source
                    )}
                    {r.date && <span className="ml-1 text-muted-foreground">- {r.date}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
