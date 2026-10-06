import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";


async function loadWorkbookModelModule() {
  let source;
  try {
    source = readFileSync(new URL("../src/lib/workbookModel.ts", import.meta.url), "utf8");
  } catch {
    assert.fail("src/lib/workbookModel.ts is missing");
  }
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
}


test("capex chart displays accounting outflows as positive magnitudes", async () => {
  const { buildCapexChartModel } = await loadWorkbookModelModule();
  const sourceSeries = [
    { name: "AMZN US Equity", values: [-34194.0064, -111332.5702] },
    { name: "GOOGL US Equity", values: [-56390.43, -194250.7379] },
  ];
  const model = buildCapexChartModel(
    [2025, 2026],
    sourceSeries,
  );

  assert.deepEqual(model.years, [2025, 2026]);
  assert.deepEqual(model.series[0].values, [34194.0064, 111332.5702]);
  assert.deepEqual(sourceSeries[0].values, [-34194.0064, -111332.5702]);
  assert.equal(model.yMin, 0);
  assert.equal(model.yMax, 200000);
});


test("workbook cells use backend percentage display verbatim", async () => {
  const { displayWorkbookCell } = await loadWorkbookModelModule();

  assert.equal(
    displayWorkbookCell({ address: "E35", value: 1.405165727, display: "140.52%", is_percentage: true }),
    "140.52%",
  );
});


test("a refresh error hides previously loaded workbook data", async () => {
  const { workbookViewState } = await loadWorkbookModelModule();

  assert.equal(workbookViewState({ isLoading: false, error: new Error("locked"), hasData: true }), "error");
  assert.equal(workbookViewState({ isLoading: true, error: null, hasData: false }), "loading");
  assert.equal(workbookViewState({ isLoading: false, error: null, hasData: true }), "ready");
});


test("Capex legend toggles companies without mutating workbook series", async () => {
  const {
    capexInteractionReducer,
    createCapexInteractionState,
    visibleCapexSeries,
  } = await loadWorkbookModelModule();
  const sourceSeries = [
    { name: "AMZN US Equity", values: [-10, -20] },
    { name: "GOOGL US Equity", values: [-30, -40] },
  ];

  let state = capexInteractionReducer(createCapexInteractionState(), { type: "toggle", seriesName: "AMZN US Equity" });
  assert.deepEqual(visibleCapexSeries(sourceSeries, state).map((series) => series.name), ["GOOGL US Equity"]);
  assert.deepEqual(sourceSeries[0].values, [-10, -20]);

  state = capexInteractionReducer(state, { type: "toggle", seriesName: "AMZN US Equity" });
  assert.deepEqual(visibleCapexSeries(sourceSeries, state).map((series) => series.name), ["AMZN US Equity", "GOOGL US Equity"]);
});


test("Capex show-all restores every hidden company", async () => {
  const { capexInteractionReducer, createCapexInteractionState } = await loadWorkbookModelModule();

  let state = capexInteractionReducer(createCapexInteractionState(), { type: "toggle", seriesName: "AMZN US Equity" });
  state = capexInteractionReducer(state, { type: "toggle", seriesName: "GOOGL US Equity" });
  state = capexInteractionReducer(state, { type: "show_all" });

  assert.deepEqual(state.hiddenSeries, []);
});


test("Capex chart keeps a positive axis when every company is hidden", async () => {
  const { buildCapexChartModel } = await loadWorkbookModelModule();

  const model = buildCapexChartModel([2025, 2026], []);

  assert.equal(model.yMin, 0);
  assert.equal(model.yMax, 1);
});


test("Capex hover highlights one company and dims the others", async () => {
  const {
    capexInteractionReducer,
    capexSeriesOpacity,
    createCapexInteractionState,
  } = await loadWorkbookModelModule();

  const state = capexInteractionReducer(createCapexInteractionState(), { type: "highlight", seriesName: "AMZN US Equity" });

  assert.equal(capexSeriesOpacity("AMZN US Equity", state), 1);
  assert.equal(capexSeriesOpacity("GOOGL US Equity", state), 0.2);
});


test("HBM table promotes workbook labels instead of Excel coordinates", async () => {
  const { buildHbmTableView } = await loadWorkbookModelModule();
  const table = {
    sheet: "DRAM Demand",
    range: "B2:I144",
    columns: ["B", "C", "D", "E", "F", "G", "H", "I"],
    rows: [
      {
        row: 2,
        cells: [
          { address: "B2", value: "DRAM Memory Demand based on GW Provisioning", display: "DRAM Memory Demand based on GW Provisioning", is_percentage: false },
          ...Array.from({ length: 7 }, (_, index) => ({ address: `${String.fromCharCode(67 + index)}2`, value: null, display: "", is_percentage: false })),
        ],
      },
      {
        row: 3,
        cells: [
          { address: "B3", value: null, display: "", is_percentage: false },
          { address: "C3", value: "Totals / Comments etc", display: "Totals / Comments etc", is_percentage: false },
          ...[2025, 2026, 2027, 2028, 2029, 2030].map((year, index) => ({ address: `${String.fromCharCode(68 + index)}3`, value: year, display: year.toLocaleString(), is_percentage: false })),
        ],
      },
      {
        row: 4,
        cells: [
          { address: "B4", value: "GW provisioning", display: "GW provisioning", is_percentage: false },
          ...Array.from({ length: 7 }, (_, index) => ({ address: `${String.fromCharCode(67 + index)}4`, value: null, display: "", is_percentage: false })),
        ],
      },
    ],
  };

  const view = buildHbmTableView(table);

  assert.equal(view.title, "DRAM Memory Demand based on GW Provisioning");
  assert.deepEqual(view.headers, ["Metric", "Totals / Comments etc", "2025", "2026", "2027", "2028", "2029", "2030"]);
  assert.equal(view.numericColumnStart, 2);
  assert.deepEqual(view.rows.map((row) => ({ key: row.key, style: row.style })), [{ key: "4", style: "section" }]);
  assert.equal(view.headers.includes("B"), false);
  assert.equal(Object.hasOwn(view.rows[0], "rowLabel"), false);
  assert.equal(Object.hasOwn(view, "sheet"), false);
  assert.equal(Object.hasOwn(view, "range"), false);
});


test("Capex table collapses its empty comments column without changing accounting signs", async () => {
  const { buildCapexTableView } = await loadWorkbookModelModule();
  const titleCells = [
    { address: "B26", value: "Datacentre Capex USD Mn", display: "Datacentre Capex USD Mn", is_percentage: false },
    ...Array.from({ length: 7 }, (_, index) => ({ address: `${String.fromCharCode(67 + index)}26`, value: null, display: "", is_percentage: false })),
  ];
  const companyCells = [
    { address: "B27", value: "AMZN US Equity", display: "AMZN US Equity", is_percentage: false },
    { address: "C27", value: null, display: "", is_percentage: false },
    { address: "D27", value: -34194.0064, display: "-34,194.01", is_percentage: false },
    ...Array.from({ length: 5 }, (_, index) => ({ address: `${String.fromCharCode(69 + index)}27`, value: null, display: "", is_percentage: false })),
  ];
  const percentageCells = [
    { address: "B35", value: "% yoy", display: "% yoy", is_percentage: false },
    { address: "C35", value: null, display: "", is_percentage: false },
    ...[null, 1.4052, 0.5709, 0.2917, -0.1669, 0.0328].map((value, index) => ({
      address: `${String.fromCharCode(68 + index)}35`,
      value,
      display: value === null ? "" : `${(value * 100).toFixed(2)}%`,
      is_percentage: value !== null,
    })),
  ];
  const capex = {
    sheet: "Hyperscaler Capex GW Triangulat",
    range: "B26:I35",
    columns: ["B", "C", "D", "E", "F", "G", "H", "I"],
    rows: [{ row: 26, cells: titleCells }, { row: 27, cells: companyCells }, { row: 35, cells: percentageCells }],
    table: {
      sheet: "Hyperscaler Capex GW Triangulat",
      range: "B26:I35",
      columns: ["B", "C", "D", "E", "F", "G", "H", "I"],
      rows: [{ row: 26, cells: titleCells }, { row: 27, cells: companyCells }, { row: 35, cells: percentageCells }],
    },
    years: [2025, 2026, 2027, 2028, 2029, 2030],
    series: [{ name: "AMZN US Equity", values: [-34194.0064, null, null, null, null, null] }],
  };

  const view = buildCapexTableView(capex);

  assert.equal(view.title, "Datacentre Capex USD Mn");
  assert.deepEqual(view.headers, ["Company", "2025", "2026", "2027", "2028", "2029", "2030"]);
  assert.equal(view.numericColumnStart, 1);
  assert.equal(view.rows[0].cells.length, 7);
  assert.equal(view.rows[0].cells[1].display, "-34,194.01");
  assert.equal(view.rows[1].cells[5].display, "-16.69%");
  assert.equal(view.rows[1].style, "summary");
  assert.equal(view.headers.includes("B"), false);
  assert.equal(Object.hasOwn(view.rows[0], "rowLabel"), false);
  assert.equal(Object.hasOwn(view, "sheet"), false);
  assert.equal(Object.hasOwn(view, "range"), false);
});
