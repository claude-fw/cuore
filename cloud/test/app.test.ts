/**
 * Porta d'ingresso (src/app.ts) chiamata direttamente in Node, per i rami difficili da provocare in workerd:
 * Durable Object che fallisce, che risponde con un errore, ordine dei controlli.
 */
import assert from "node:assert/strict";
import { before, describe, it } from "node:test";
import { type AppDeps, handle } from "../src/app.ts";
import { AUD, EMAIL, TEAM, type TestKey, testKey, token } from "./helpers.ts";

let key: TestKey;
before(async () => {
  key = await testKey("chiave-app");
});

const deps = (over: Partial<AppDeps> = {}): AppDeps => ({
  config: { teamDomain: TEAM, aud: AUD, allowedEmails: [EMAIL] },
  keys: async (kid) => (kid === key.kid ? key.jwk : null),
  nowMs: Date.now,
  page: "<html><head></head><body></body></html>",
  finance: () => ({ fetch: async () => Response.json({ ok: true }) }),
  state: "attivo",
  ...over,
});

const req = async (path: string, method = "GET") =>
  new Request(`https://cuore.esempio.workers.dev${path}`, {
    method,
    headers: { "Cf-Access-Jwt-Assertion": await token(key) },
  });

describe("porta d'ingresso", () => {
  it("Durable Object che lancia: 502 con un codice fisso, senza messaggio ne' traccia dell'errore", async () => {
    const r = await handle(
      await req("/api/vetrina/stato"),
      deps({
        finance: () => ({
          fetch: async () => {
            throw new Error("dettaglio interno segreto");
          },
        }),
      }),
    );
    assert.equal(r.status, 502);
    const body = await r.text();
    assert.deepEqual(JSON.parse(body), { errore: "durable_object_non_disponibile" });
    assert.ok(!body.includes("segreto"));
    assert.equal(r.headers.get("Cache-Control"), "no-store");
  });

  it("errore del Durable Object: stato inoltrato, sempre come JSON con le intestazioni di sicurezza", async () => {
    const r = await handle(
      await req("/api/vetrina/stato"),
      deps({ finance: () => ({ fetch: async () => new Response("<b>x</b>", { status: 500 }) }) }),
    );
    assert.equal(r.status, 500);
    assert.match(r.headers.get("Content-Type") ?? "", /^application\/json/);
    assert.equal(r.headers.get("X-Content-Type-Options"), "nosniff");
  });

  it("l'interruttore viene prima di tutto: spento + configurazione mancante = 503 dell'interruttore", async () => {
    const r = await handle(await req("/"), deps({ state: "spento", config: null, keys: null }));
    assert.equal(r.status, 503);
    assert.match(await r.text(), /interruttore/);
  });

  it("il Durable Object non viene mai chiamato senza identita' valida", async () => {
    let calls = 0;
    const d = deps({
      finance: () => {
        calls++;
        return { fetch: async () => Response.json({}) };
      },
    });
    const r = await handle(new Request("https://cuore.esempio.workers.dev/api/vetrina/stato"), d);
    assert.equal(r.status, 403);
    assert.equal(calls, 0);
  });
});
