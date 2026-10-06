import { brokerHex, splitBrokerSeriesName } from "./brokerColors";

function hashStr(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  const normalized = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  const n = Number.parseInt(normalized, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgbToHex(r: number, g: number, b: number): string {
  const toHex = (v: number) =>
    Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0");
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}

function mixHex(a: string, b: string, t: number): string {
  const [ar, ag, ab] = hexToRgb(a);
  const [br, bg, bb] = hexToRgb(b);
  return rgbToHex(ar + (br - ar) * t, ag + (bg - ag) * t, ab + (bb - ab) * t);
}

export function seriesHex(name: string): string {
  const { broker, variant } = splitBrokerSeriesName(name);
  const base = brokerHex(broker || name);
  const s = variant.toLowerCase();
  if (!s) return base;

  if (s.startsWith("demand")) return mixHex(base, "#ffffff", 0.38);
  if (s.startsWith("supply")) return mixHex(base, "#000000", 0.26);

  if (s === "dram") return mixHex(base, "#ffffff", 0.28);
  if (s === "nand") return mixHex(base, "#000000", 0.18);
  if (s === "hbm") return mixHex(base, "#8b5cf6", 0.26);
  if (s === "total") return base;

  if (s.startsWith("contract")) return mixHex(base, "#ffffff", 0.26);
  if (s.startsWith("spot")) return mixHex(base, "#000000", 0.18);

  const variants = [
    base,
    mixHex(base, "#ffffff", 0.30),
    mixHex(base, "#000000", 0.18),
    mixHex(base, "#0ea5e9", 0.22),
    mixHex(base, "#f97316", 0.20),
    mixHex(base, "#8b5cf6", 0.22),
  ];
  return variants[hashStr(s) % variants.length];
}

export function seriesStrokeDasharray(name: string): string | undefined {
  const { variant } = splitBrokerSeriesName(name);
  const s = variant.toLowerCase();
  if (!s) return undefined;
  if (s.startsWith("supply") || s.startsWith("spot") || s.includes("others")) return "5 3";
  if (s === "nand" || s.includes("kioxia") || s.includes("ymtc")) return "3 3";
  if (s === "hbm" || s.includes("sk hynix")) return "7 3";
  if (s.includes("micron") || s.includes("wdc")) return "2 3";
  return undefined;
}

export function seriesLegendLabel(name: string): string {
  const { broker, variant } = splitBrokerSeriesName(name);
  return variant || broker || name;
}
