import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";


async function loadGalleryCacheModule() {
  let source;
  try {
    source = readFileSync(new URL("../src/lib/galleryCache.ts", import.meta.url), "utf8");
  } catch {
    assert.fail("src/lib/galleryCache.ts is missing");
  }
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
}


test("normal Gallery loads use the reusable browser cache key", async () => {
  const { galleryRequest } = await loadGalleryCacheModule();

  assert.deepEqual(galleryRequest(false, 12345), {
    path: "/api/charts/by-category",
    init: undefined,
  });
});


test("manual Gallery refresh explicitly bypasses browser and server caches", async () => {
  const { galleryRequest } = await loadGalleryCacheModule();

  assert.deepEqual(galleryRequest(true, 12345), {
    path: "/api/charts/by-category?refresh=true&_=12345",
    init: { cache: "no-store" },
  });
});


test("local Gallery mutations bypass only the browser cache", async () => {
  const { galleryRequest } = await loadGalleryCacheModule();

  assert.deepEqual(galleryRequest(false, 12345, true), {
    path: "/api/charts/by-category?_=12345",
    init: { cache: "no-store" },
  });
});
