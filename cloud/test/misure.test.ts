/**
 * Banco di misura (src/misure.ts, wrangler.misure.jsonc) in workerd (Miniflare), senza rete, dati sintetici: chiuso per
 * difetto, separato dalla produzione, sequenza di misura completa su pochi movimenti, PITR non simulabile in locale (501).
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { before, describe, it } from "node:test";
import { parseJsonc } from "../scripts/controlla-configurazione.ts";
import { bancoLocale, misura, provaPitr } from "../scripts/misura-banco.ts";
import { AUD, bundle, EMAIL, startWorker, TEAM, type TestKey, testKey, token } from "./helpers.ts";

const leggi = (f: string) => readFileSync(new URL(`../${f}`, import.meta.url), "utf8");
const TOKEN = "token-di-prova-del-banco-0001";
const HASH = createHash("sha256").update(TOKEN).digest("hex");
const BANCO = { durableObjects: { BANCO: { className: "BancoMisure", useSQLite: true } } };
const senzaRete = () => new Response("rete non disponibile nei test", { status: 599 });

let misureScript = "";
let prodScript = "";
let key: TestKey;
before(async () => {
  misureScript = await bundle("misure");
  prodScript = await bundle("index");
  key = await testKey("chiave-team");
});

const post = (
  mf: { dispatchFetch(u: string, i?: RequestInit): Promise<Response> },
  path: string,
  auth?: string,
  body: unknown = {},
  method = "POST",
) =>
  mf.dispatchFetch(`https://cuore-misure-sintetiche.esempio.workers.dev${path}`, {
    method,
    headers: { "Content-Type": "application/json", ...(auth ? { Authorization: auth } : {}) },
    ...(method === "POST" ? { body: JSON.stringify(body) } : {}),
  });

describe("banco di misura: separato dalla produzione e chiuso per difetto", () => {
  it("la produzione non lo contiene: ne' nel pacchetto ne' in wrangler.jsonc", () => {
    for (const s of ["BancoMisure", "/misure/", "cloud_banco", "MISURE_TOKEN"])
      assert.ok(!prodScript.includes(s), s);
    assert.ok(misureScript.includes("BancoMisure"), "controllo del controllo");
    const prod = parseJsonc(leggi("wrangler.jsonc")) as Record<string, unknown>;
    assert.ok(!JSON.stringify(prod).includes("BANCO") && !JSON.stringify(prod).includes("misure"));
    // biome-ignore lint/suspicious/noExplicitAny: configurazione letta da JSONC
    const m = parseJsonc(leggi("wrangler.misure.jsonc")) as Record<string, any>;
    assert.notEqual(m.name, "cuore");
    assert.equal(m.main, "src/misure.ts");
    assert.deepEqual(
      m.durable_objects.bindings.map((b: { name: string }) => b.name),
      ["BANCO"],
      "nessun binding ai Durable Object della vetrina",
    );
    assert.equal(m.vars.MISURE_TOKEN_SHA256, "", "spento nel file: l'impronta si passa solo al deploy");
    assert.equal(m.observability.enabled, false);
    for (const k of ["triggers", "r2_buckets", "routes", "route"]) assert.equal(m[k], undefined, k);
  });

  it("senza impronta del token: 503 a tutto; token assente o sbagliato: 401; solo POST su /misure/", async () => {
    const spento = await startWorker(misureScript, {}, senzaRete, BANCO);
    for (const [p, m] of [
      ["/misure/azzera", "POST"],
      ["/misure/esegui", "POST"],
      ["/", "GET"],
    ] as const) {
      const r = await post(spento, p, `Bearer ${TOKEN}`, {}, m);
      assert.equal(r.status, 503, p);
      assert.equal(((await r.json()) as { errore: string }).errore, "banco_spento");
    }
    const vuoto = await startWorker(misureScript, { MISURE_TOKEN_SHA256: "  " }, senzaRete, BANCO);
    assert.equal((await post(vuoto, "/misure/azzera", `Bearer ${TOKEN}`)).status, 503);

    const mf = await startWorker(misureScript, { MISURE_TOKEN_SHA256: HASH }, senzaRete, BANCO);
    assert.equal((await post(mf, "/misure/azzera")).status, 401);
    assert.equal((await post(mf, "/misure/azzera", "Bearer token-sbagliato")).status, 401);
    assert.equal((await post(mf, "/misure/azzera", TOKEN)).status, 401, "senza «Bearer»");
    assert.equal((await post(mf, "/misure/azzera", `Bearer ${TOKEN}`, {}, "GET")).status, 405);
    assert.equal((await post(mf, "/altro", `Bearer ${TOKEN}`)).status, 404);
    const prima = await post(mf, "/misure/esegui", `Bearer ${TOKEN}`, { operazione: "stato" });
    assert.equal(prima.status, 409, "niente misure prima di /misure/azzera");
    const male = await post(mf, "/misure/azzera", `Bearer ${TOKEN}`, { movimenti: 999_999 });
    assert.equal(male.status, 400);
  });

  it("l'ingresso di produzione non risponde alle rotte del banco, nemmeno al titolare", async () => {
    const certs = (r: Request) =>
      r.url === `https://${TEAM}/cdn-cgi/access/certs` ? Response.json({ keys: [key.jwk] }) : senzaRete();
    const mf = await startWorker(
      prodScript,
      { ACCESS_TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD, ACCESS_ALLOWED_EMAILS: EMAIL, CUORE_STATO: "attivo" },
      certs,
    );
    const t = await token(key);
    for (const m of ["GET", "POST"]) {
      const r = await mf.dispatchFetch("https://cuore.esempio.workers.dev/misure/azzera", {
        method: m,
        headers: { "Cf-Access-Jwt-Assertion": t, Authorization: `Bearer ${TOKEN}` },
      });
      assert.ok([404, 405].includes(r.status), `${m} ${r.status}`);
      assert.ok(!(await r.text()).includes("movimenti_previsti"));
    }
  });
});

describe("banco di misura: sequenza completa su pochi movimenti sintetici (locale)", () => {
  it("carica, classifica, trasferimenti, feed, consultazioni, backup: tutto ok; PITR = 501 in Miniflare", async () => {
    const banco = await bancoLocale();
    try {
      const m = await misura(banco.chiama, { movimenti: 240, blocco: 100, ripetizioni: 1, ricaricaOgni: 7 });
      for (const r of m.righe) assert.equal(r.esito, "ok", r.operazione);
      assert.equal(m.righe.length, 22);
      assert.deepEqual(m.stato, {
        movimenti: 240,
        classificati: 240,
        da_verificare: m.stato.da_verificare,
        trasferimenti_proposti: 5,
      });
      const pitr = await provaPitr(banco.chiama);
      assert.equal(pitr.esito, "pitr_non_disponibile");
      const passi = pitr.passi as Record<string, string>;
      assert.equal(passi.segnalibro, "ok", "getCurrentBookmark esiste anche in locale");
      assert.match(passi.prepara ?? "", /^501 pitr_non_disponibile .*point-in-time recovery/);
      // il riavvio voluto (ctx.abort) e' gestito: la scrittura resta (in locale nessun ripristino)
      const w = await banco.chiama("/misure/pitr/scrivi");
      const rv = await banco.chiama("/misure/pitr/riavvia");
      assert.deepEqual([rv.status, rv.doc.riavviato], [200, true]);
      const l = await banco.chiama("/misure/pitr/leggi");
      assert.equal(l.doc.marca, w.doc.marca);
      assert.equal(l.doc.movimenti, 240, "i dati sopravvivono al riavvio");
    } finally {
      await banco.chiudi();
    }
  });
});
