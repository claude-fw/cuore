import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

test("la versione mostrata in Impostazioni coincide con la cache del service worker", () => {
  const pagina = readFileSync(new URL("../../app/index.html", import.meta.url), "utf8");
  const sw = readFileSync(new URL("../../app/sw.js", import.meta.url), "utf8");
  const mostrata = pagina.match(/const VERSIONE_APP = "v(\d+) /)?.[1];
  const cache = sw.match(/cuore-static-v(\d+)/)?.[1];
  assert.ok(mostrata);
  assert.equal(mostrata, cache);
});
