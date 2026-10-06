import type {
  WorkbookCapexSeries,
  WorkbookCapexTable,
  WorkbookCell,
  WorkbookRow,
  WorkbookTable,
} from "./api";


export interface CapexChartModel {
  years: Array<number | string | null>;
  series: WorkbookCapexSeries[];
  yMin: number;
  yMax: number;
}


export interface CapexInteractionState {
  hiddenSeries: string[];
  highlightedSeries: string | null;
}


export type CapexInteractionAction =
  | { type: "toggle"; seriesName: string }
  | { type: "show_all" }
  | { type: "highlight"; seriesName: string | null };


export function createCapexInteractionState(): CapexInteractionState {
  return { hiddenSeries: [], highlightedSeries: null };
}


export function capexInteractionReducer(
  state: CapexInteractionState,
  action: CapexInteractionAction,
): CapexInteractionState {
  if (action.type === "show_all") return { ...state, hiddenSeries: [] };
  if (action.type === "highlight") return { ...state, highlightedSeries: action.seriesName };
  const hiddenSeries = state.hiddenSeries.includes(action.seriesName)
    ? state.hiddenSeries.filter((name) => name !== action.seriesName)
    : [...state.hiddenSeries, action.seriesName];
  return { ...state, hiddenSeries };
}


export function visibleCapexSeries(
  series: WorkbookCapexSeries[],
  state: CapexInteractionState,
): WorkbookCapexSeries[] {
  return series.filter((item) => !state.hiddenSeries.includes(item.name));
}


export function capexSeriesOpacity(seriesName: string, state: CapexInteractionState): number {
  return state.highlightedSeries === null || state.highlightedSeries === seriesName ? 1 : 0.2;
}


export interface WorkbookTableViewRow {
  key: string;
  cells: WorkbookCell[];
  style: "normal" | "section" | "summary";
}


export interface WorkbookTableView {
  title: string;
  headers: string[];
  numericColumnStart: number;
  rows: WorkbookTableViewRow[];
}


function niceCeiling(value: number): number {
  if (value <= 0) return 0;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  return Math.ceil(value / magnitude) * magnitude;
}


export function buildCapexChartModel(
  years: Array<number | string | null>,
  series: WorkbookCapexSeries[],
): CapexChartModel {
  const chartSeries = series.map((item) => ({
    ...item,
    values: item.values.map((value) => value === null ? null : Math.abs(value)),
  }));
  const values = chartSeries.flatMap((item) => item.values).filter((value): value is number => Number.isFinite(value));
  if (values.length === 0) return { years, series: chartSeries, yMin: 0, yMax: 1 };
  const minimum = Math.min(0, ...values);
  const maximum = Math.max(0, ...values);
  return {
    years,
    series: chartSeries,
    yMin: minimum < 0 ? -niceCeiling(Math.abs(minimum)) : 0,
    yMax: maximum > 0 ? niceCeiling(maximum) : 0,
  };
}


export function displayWorkbookCell(cell: WorkbookCell): string {
  return cell.display;
}


function tableRows(rows: WorkbookRow[]): WorkbookTableViewRow[] {
  return rows.map((row) => {
    const populated = row.cells.filter((cell) => cell.display !== "").length;
    const label = row.cells[0]?.display.toLowerCase() ?? "";
    const style = populated === 1 && row.cells[0]?.display
      ? "section"
      : label.includes("hyperscaler capex") || label === "% yoy"
        ? "summary"
        : "normal";
    return { key: String(row.row), cells: row.cells, style };
  });
}


function yearHeader(cell: WorkbookCell): string {
  return typeof cell.value === "number" ? String(cell.value) : cell.display;
}


export function buildHbmTableView(table: WorkbookTable): WorkbookTableView {
  const titleRow = table.rows[0];
  const headerRow = table.rows[1];
  const headers = headerRow?.cells.map((cell, index) => {
    if (index === 0) return cell.display || "Metric";
    if (index === 1) return cell.display || "Comments";
    return yearHeader(cell);
  }) ?? [];
  return {
    title: titleRow?.cells[0]?.display || table.sheet,
    headers,
    numericColumnStart: 2,
    rows: tableRows(table.rows.slice(2)),
  };
}


export function buildCapexTableView(capex: WorkbookCapexTable): WorkbookTableView {
  const titleRow = capex.table.rows[0];
  const bodyRows = capex.table.rows.slice(1);
  const hasComments = bodyRows.some((row) => row.cells[1]?.display !== "");
  const visibleRows = hasComments
    ? bodyRows
    : bodyRows.map((row) => ({ ...row, cells: row.cells.filter((_, index) => index !== 1) }));
  return {
    title: titleRow?.cells[0]?.display || "Datacentre Capex",
    headers: ["Company", ...(hasComments ? ["Comments"] : []), ...capex.years.map((year) => year === null ? "" : String(year))],
    numericColumnStart: hasComments ? 2 : 1,
    rows: tableRows(visibleRows),
  };
}


export function workbookViewState({
  isLoading,
  error,
  hasData,
}: {
  isLoading: boolean;
  error: unknown;
  hasData: boolean;
}): "loading" | "error" | "ready" | "empty" {
  if (error) return "error";
  if (isLoading) return "loading";
  if (hasData) return "ready";
  return "empty";
}
