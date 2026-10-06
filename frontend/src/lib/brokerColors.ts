// Single source of truth for "broker → color" across the app.
// HEX is for SVG / inline styles (LineChart, ChatPanel chart thumbs).
// Use brokerHex(name) — handles common short/long aliases and falls back to a
// stable hash-based pick so an un-mapped broker still gets a consistent color.

const FIXED: Record<string, string> = {
  // Bulge-bracket & top sellside
  GS: "#2563eb",                 // blue-600
  "Goldman Sachs": "#2563eb",
  JPM: "#7c3aed",                // violet-600
  JPMorgan: "#7c3aed",
  "J.P. Morgan": "#7c3aed",
  "J.P.Morgan": "#7c3aed",
  "JP Morgan": "#7c3aed",
  "JPMorgan Chase": "#7c3aed",
  MS: "#10b981",                 // emerald-500
  "Morgan Stanley": "#10b981",
  UBS: "#dc2626",                // red-600
  BofA: "#f59e0b",               // amber-500
  "Bank of America": "#f59e0b",
  Citi: "#0ea5e9",               // sky-500
  Citigroup: "#0ea5e9",
  Barclays: "#06b6d4",           // cyan-500
  DB: "#4f46e5",                 // indigo-600
  "Deutsche Bank": "#4f46e5",
  Jefferies: "#0891b2",          // cyan-600
  HSBC: "#be123c",                // rose-700

  // Asia / regional
  CLSA: "#ea580c",               // orange-600
  Nomura: "#a855f7",             // purple-500
  Daiwa: "#16a34a",              // green-600
  Mizuho: "#d97706",             // amber-600
  CICC: "#9333ea",               // purple-600

  // Independent / boutique
  Bernstein: "#65a30d",          // lime-600
  "Arete Research": "#475569",    // slate-600
  Arete: "#475569",
  Wolfe: "#be185d",              // pink-700
  Cowen: "#7e22ce",              // purple-700
  Susquehanna: "#0369a1",        // sky-700
  Raymond: "#15803d",            // green-700

  // Industry data
  TrendForce: "#0d9488",         // teal-600
  Counterpoint: "#9f1239",       // rose-800
  Gartner: "#1e3a8a",            // blue-900
};

const ALIASES: Record<string, string> = {
  goldmansachs: "GS",
  goldman: "GS",
  jpmorgan: "JPM",
  jpmorganchase: "JPM",
  jpm: "JPM",
  morganstanley: "MS",
  bankofamerica: "BofA",
  boaml: "BofA",
  bofa: "BofA",
  deutschebank: "DB",
  db: "DB",
  areteresearch: "Arete",
};

// Deterministic fallback palette (color-blind-friendly, distinct from FIXED).
const FALLBACK = [
  "#64748b", "#84cc16", "#14b8a6", "#facc15", "#22d3ee", "#f472b6",
  "#8b5cf6", "#fb7185", "#34d399", "#a3a3a3",
];

function hashStr(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

function normalizeToken(s: string): string {
  return (s || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
}

export function cleanBrokerLabel(name: string): string {
  return (name || "")
    .replace(/\u00c3\u201a/g, "")
    .replace(/\u00c2/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

const SERIES_SPLIT_RE = /\s+(?:\u00b7|-|\u2013|\u2014)\s+/;

export function splitBrokerSeriesName(name: string): { broker: string; variant: string } {
  const raw = cleanBrokerLabel(name);
  if (!raw) return { broker: "", variant: "" };
  const parts = raw.split(SERIES_SPLIT_RE);
  if (parts.length <= 1) return { broker: raw, variant: "" };
  return {
    broker: parts[0].trim(),
    variant: parts.slice(1).join(" - ").trim(),
  };
}

export function canonicalBrokerName(name: string): string {
  const { broker } = splitBrokerSeriesName(name);
  const raw = broker || cleanBrokerLabel(name);
  return ALIASES[normalizeToken(raw)] || raw || "";
}

/**
 * Return a stable color for a broker / researcher name.
 * Series labels of the form `${broker} · ${suffix}` (used by merged Dashboard
 * panels, e.g. "GS · Contract") are matched by the broker prefix so all of a
 * broker's series share one color.
 */
export function brokerHex(name: string): string {
  const raw = cleanBrokerLabel(name);
  if (!raw) return FALLBACK[0];
  const broker = canonicalBrokerName(raw);
  if (FIXED[broker]) return FIXED[broker];
  if (FIXED[raw]) return FIXED[raw];

  // Stable fallback per (broker-prefix) name.
  return FALLBACK[hashStr(broker || raw) % FALLBACK.length];
}
