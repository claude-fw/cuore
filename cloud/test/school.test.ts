/**
 * Feed School nel cloud (E3), solo dati sintetici: validazione a schema chiuso (Node) e Durable Object School in
 * workerd (Miniflare) dietro il token di servizio del Mac. Il feed di prova (`fixtures/school-feed-sintetico.json`) e'
 * l'uscita del mittente Python (`school_cloud_push.minimize`) sul Registry sintetico dei test del motore School: il cloud
 * deve accettare esattamente cio' che il Mac produce. Nessuna rete: chiavi del team servite da `outboundService`.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { before, describe, it } from "node:test";
import { parseServiceIds, verifyServiceJwt } from "../src/access.ts";
import { checkSchoolFeed, deliveryText, SCHOOL_MAX_BODY, SCHOOL_SOGLIA_ORE } from "../src/school.ts";
import { AUD, bundle, EMAIL, nowS, startWorker, TEAM, type TestKey, testKey, token } from "./helpers.ts";

// biome-ignore lint/suspicious/noExplicitAny: documenti JSON letti nei test
type Doc = Record<string, any>;
const FIXTURE = readFileSync(new URL("./fixtures/school-feed-sintetico.json", import.meta.url), "utf8");
const feed = (over: Doc = {}): Doc => ({ ...(JSON.parse(FIXTURE) as Doc), ...over });
const iso = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "+00:00");
const MAC = "0123456789abcdef0123456789abcdef.access";
const ALTRO = "fedcba9876543210fedcba9876543210.access";
const URL0 = "https://cuore.esempio.workers.dev";
const CONFIG = {
  ACCESS_TEAM_DOMAIN: TEAM,
  ACCESS_AUD: AUD,
  ACCESS_ALLOWED_EMAILS: EMAIL,
  CUORE_STATO: "attivo",
  ACCESS_SERVIZIO_SCHOOL: MAC,
};

let key: TestKey;
let prodScript = "";
let localScript = "";
const outbound = (r: Request) =>
  r.url === `https://${TEAM}/cdn-cgi/access/certs`
    ? Response.json({ keys: [key.jwk] })
    : new Response("rete non disponibile nei test", { status: 599 });

before(async () => {
  key = await testKey("chiave-team-school");
  prodScript = await bundle("index");
  localScript = await bundle("locale");
});

/** Token di Access di un token di servizio: niente email, `common_name` = Client ID (forma documentata da Access). */
const serviceToken = (cn: string, extra: Doc = {}) =>
  token(key, { email: null, sub: "", common_name: cn, ...extra });

type Mf = Awaited<ReturnType<typeof startWorker>>;
async function push(
  mf: Mf,
  body: unknown,
  headers: Record<string, string> | null = null,
): Promise<{ status: number; doc: Doc }> {
  const h = headers ?? { "Cf-Access-Jwt-Assertion": await serviceToken(MAC) };
  const r = await mf.dispatchFetch(`${URL0}/api/school`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...h },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
  const text = await r.text();
  return { status: r.status, doc: text.startsWith("{") ? (JSON.parse(text) as Doc) : { testo: text } };
}
async function read(mf: Mf, headers?: Record<string, string>): Promise<{ status: number; doc: Doc }> {
  const h = headers ?? { "Cf-Access-Jwt-Assertion": await token(key) };
  const r = await mf.dispatchFetch(`${URL0}/api/school`, { headers: h });
  return { status: r.status, doc: (await r.json()) as Doc };
}

describe("validazione del feed School (schema chiuso)", () => {
  it("accetta il feed sintetico prodotto dal mittente del Mac", () => {
    const c = checkSchoolFeed(feed());
    assert.equal(c.ok, true, JSON.stringify(c));
  });

  it("rifiuta schema, versione, campi in piu', vocabolari e credenziali; mai i valori nei problemi", () => {
    const code = (f: unknown) => {
      const c = checkSchoolFeed(f);
      return c.ok ? "ok" : c.codice;
    };
    assert.equal(code([]), "schema_non_valido");
    assert.equal(code(feed({ schema: "cuore.money.feed/1" })), "schema_non_valido");
    assert.equal(code(feed({ schema: "cuore.school.feed/2" })), "versione_non_supportata");
    assert.equal(code(feed({ fonte: "reale" })), "non_conforme", "campo tolto dal Mac");
    assert.equal(code(feed({ esito: "ottimo" })), "non_conforme");
    assert.equal(code(feed({ generato: "2026-10-04T12:00:00" })), "non_conforme", "istante senza fuso");
    assert.equal(code(feed({ scritture: { registry: true } })), "non_conforme");
    assert.equal(
      code(feed({ errori: [{ ambito: "voti", codice: "errore_motore", messaggio: "x" }] })),
      "non_conforme",
    );
    const f = feed();
    f.errori = [
      {
        ambito: "voti",
        codice: "errore_motore",
        messaggio: "errore del motore nel calcolo della pagina",
        dettaglio: "KeyError",
      },
    ];
    f.esito = "parziale";
    assert.equal(code(f), "non_conforme", "dettaglio tolto dal Mac");
    const page = feed();
    page.pagine.voti.regole_agente = "testo";
    assert.equal(code(page), "non_conforme");
    const uid = feed();
    uid.pagine.voti.blocchi[0].provenienza.uid = ["cvv:1"];
    assert.equal(code(uid), "non_conforme", "elenchi di uid della provenienza tolti dal Mac");
    const extra = feed();
    extra.pagine.storico = { ...extra.pagine.voti, pagina: "storico" };
    assert.equal(code(extra), "non_conforme", "pagina non mostrata dall'interfaccia");
    const stato = feed();
    stato.pagine.home.blocchi[0].stato = "inventato";
    assert.equal(code(stato), "non_conforme");
    const segreto = feed();
    segreto.pagine.home.blocchi[0].dati = { client_secret: "SEGRETO-DI-PROVA" };
    const c = checkSchoolFeed(segreto);
    assert.equal(c.ok ? "ok" : c.codice, "campo_sensibile");
    assert.ok(!JSON.stringify(c).includes("SEGRETO-DI-PROVA"));
  });

  it("consegna: recente senza rileggere il JSON; oltre la soglia «vecchio» e dati_vecchi", () => {
    const f = feed();
    const gen = Date.parse(f.generato);
    const row = {
      generato: f.generato,
      generato_ms: gen,
      ricevuto: iso(gen),
      byte: 1,
      impronta: "x",
      feed: FIXTURE.trim(),
    };
    const fresh = JSON.parse(deliveryText(row, gen + 60_000));
    assert.equal(fresh.consegna.stato, "recente");
    assert.equal(fresh.consegna.eta_minuti, 1);
    assert.equal(fresh.freschezza.stato, f.freschezza.stato);
    assert.deepEqual({ ...fresh, consegna: undefined }, { ...f, consegna: undefined });
    const old = JSON.parse(deliveryText(row, gen + (SCHOOL_SOGLIA_ORE * 60 + 1) * 60_000));
    assert.equal(old.consegna.stato, "vecchio");
    assert.equal(old.freschezza.stato, "dati_vecchi");
    assert.deepEqual(old.pagine, f.pagine);
  });

  it("token di servizio: solo il Client ID ammesso, mai un token del titolare; elenco chiuso per difetto", async () => {
    const keys = async (kid: string) => (kid === key.kid ? key.jwk : null);
    const cfg = { teamDomain: TEAM, aud: AUD, allowedEmails: [EMAIL] };
    const v = (t: string) => verifyServiceJwt(t, cfg, keys, nowS(), [MAC]);
    assert.deepEqual(await v(await serviceToken(MAC)), { ok: true, clientId: MAC });
    assert.deepEqual(await v(await serviceToken(ALTRO)), { ok: false, reason: "servizio_non_ammesso" });
    assert.deepEqual(await v(await token(key)), { ok: false, reason: "non_di_servizio" });
    assert.deepEqual(await v(await serviceToken(MAC, { aud: ["altro"] })), { ok: false, reason: "audience" });
    assert.deepEqual(await v(await serviceToken(MAC, { exp: nowS() - 3600 })), {
      ok: false,
      reason: "scaduto",
    });
    assert.deepEqual(parseServiceIds(undefined), []);
    assert.deepEqual(parseServiceIds(""), []);
    assert.deepEqual(parseServiceIds("non-un-id"), []);
    assert.deepEqual(parseServiceIds(`${MAC},${ALTRO},${MAC}`), [], "al massimo 2 (rotazione)");
    assert.deepEqual(parseServiceIds(` ${MAC} , ${ALTRO}`), [MAC, ALTRO]);
  });
});

describe("Durable Object School (ingresso di produzione)", () => {
  it("accetta un feed valido dal Mac e lo serve al titolare con la consegna", async () => {
    const mf = await startWorker(prodScript, CONFIG, outbound);
    assert.equal((await read(mf)).status, 404, "nulla prima del primo invio");
    const f = feed({ generato: iso(Date.now() - 60_000) });
    const r = await push(mf, f);
    assert.equal(r.status, 200, JSON.stringify(r.doc));
    assert.equal(r.doc.ok, true);
    assert.equal(r.doc.generato, f.generato);
    const g = await read(mf);
    assert.equal(g.status, 200);
    assert.equal(g.doc.schema, "cuore.school.feed/1");
    assert.equal(g.doc.consegna.stato, "recente");
    assert.equal(g.doc.consegna.generato, f.generato);
    assert.deepEqual(g.doc.pagine, f.pagine);
    // Finance resta separato e invariato
    const v = await mf.dispatchFetch(`${URL0}/api/vetrina/stato`, {
      headers: { "Cf-Access-Jwt-Assertion": await token(key) },
    });
    assert.equal(v.status, 200);
  });

  it("rifiuta chi non e' il Mac: senza token, titolare, altro token di servizio, rotta senza Client ID", async () => {
    const mf = await startWorker(prodScript, CONFIG, outbound);
    const f = feed({ generato: iso(Date.now()) });
    const none = await push(mf, f, {});
    assert.deepEqual([none.status, none.doc.motivo], [403, "token_assente"]);
    const owner = await push(mf, f, { "Cf-Access-Jwt-Assertion": await token(key) });
    assert.deepEqual([owner.status, owner.doc.motivo], [403, "non_di_servizio"]);
    const other = await push(mf, f, { "Cf-Access-Jwt-Assertion": await serviceToken(ALTRO) });
    assert.deepEqual([other.status, other.doc.motivo], [403, "servizio_non_ammesso"]);
    const forged = await push(mf, f, {
      "Cf-Access-Client-Id": MAC,
      "Cf-Access-Client-Secret": "x".repeat(40),
    });
    assert.equal(
      forged.status,
      403,
      "le intestazioni del token non bastano: serve il token firmato da Access",
    );
    // il token di servizio non apre le pagine del titolare
    const asMac = await read(mf, { "Cf-Access-Jwt-Assertion": await serviceToken(MAC) });
    assert.deepEqual([asMac.status, asMac.doc.motivo], [403, "email_assente"]);
    assert.equal((await read(mf)).status, 404, "nessun feed salvato dai tentativi rifiutati");
    const { ACCESS_SERVIZIO_SCHOOL: _, ...closed } = CONFIG;
    const off = await startWorker(prodScript, closed, outbound);
    const r = await push(off, f);
    assert.deepEqual([r.status, r.doc.errore], [503, "invio_school_spento"]);
    const spento = await startWorker(prodScript, { ...CONFIG, CUORE_STATO: "spento" }, outbound);
    assert.equal((await push(spento, f)).status, 503);
  });

  it("rifiuta schema, versione, campi, tipo, JSON e dimensione errati senza toccare l'ultimo feed", async () => {
    const mf = await startWorker(prodScript, CONFIG, outbound);
    const now = Date.now();
    const good = feed({ generato: iso(now - 120_000) });
    assert.equal((await push(mf, good)).status, 200);
    const cases: [unknown, number, string][] = [
      [feed({ generato: iso(now), schema: "altro/1" }), 422, "schema_non_valido"],
      [feed({ generato: iso(now), schema: "cuore.school.feed/2" }), 422, "versione_non_supportata"],
      [feed({ generato: iso(now), motore: "x" }), 422, "non_conforme"],
      [feed({ generato: iso(now), pagine: { voti: { schema: "x" } } }), 422, "non_conforme"],
      [feed({ generato: iso(now + 3600_000) }), 422, "generato_nel_futuro"],
      ["{non json", 400, "richiesta_non_valida"],
    ];
    for (const [body, status, errore] of cases) {
      const r = await push(mf, body);
      assert.deepEqual([r.status, r.doc.errore], [status, errore], errore);
    }
    const typed = await mf.dispatchFetch(`${URL0}/api/school`, {
      method: "POST",
      headers: { "Content-Type": "text/plain", "Cf-Access-Jwt-Assertion": await serviceToken(MAC) },
      body: JSON.stringify(good),
    });
    assert.equal(typed.status, 415);
    const big = feed({ generato: iso(now) });
    big.pagine.home.blocchi[0].dati = { testo: "x".repeat(SCHOOL_MAX_BODY) };
    // A pezzi, senza Content-Length: il Worker legge tutto e rifiuta (con la lunghezza dichiarata risponde 413 prima
    // della fine dell'invio e il client di prova a volte vede la connessione chiusa).
    const bytes = new TextEncoder().encode(JSON.stringify(big));
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        for (let i = 0; i < bytes.length; i += 65536) c.enqueue(bytes.subarray(i, i + 65536));
        c.close();
      },
    });
    const bigRes = await mf.dispatchFetch(`${URL0}/api/school`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Cf-Access-Jwt-Assertion": await serviceToken(MAC) },
      body: stream,
      duplex: "half",
    } as Parameters<Mf["dispatchFetch"]>[1]);
    const r = { status: bigRes.status, doc: (await bigRes.json()) as Doc };
    assert.deepEqual([r.status, r.doc.errore], [413, "richiesta_troppo_grande"]);
    const g = await read(mf);
    assert.equal(g.doc.generato, good.generato, "resta l'ultimo feed valido");
  });

  it("solo l'ultima istantanea: un invio in ritardo non sostituisce uno piu' nuovo; ripetere e' idempotente", async () => {
    const mf = await startWorker(prodScript, CONFIG, outbound);
    const now = Date.now();
    const a = feed({ generato: iso(now - 7200_000) });
    const b = feed({ generato: iso(now - 3600_000), esito: "parziale" });
    b.errori = [
      { ambito: "voti", codice: "errore_motore", messaggio: "errore del motore nel calcolo della pagina" },
    ];
    delete b.pagine.voti;
    assert.equal((await push(mf, a)).status, 200);
    assert.equal((await push(mf, b)).status, 200);
    let g = await read(mf);
    assert.equal(g.doc.generato, b.generato);
    assert.equal(g.doc.esito, "parziale");
    assert.equal(g.doc.pagine.voti, undefined);
    const late = await push(mf, a);
    assert.deepEqual([late.status, late.doc.errore], [409, "feed_meno_recente"]);
    const again = await push(mf, b);
    assert.deepEqual([again.status, again.doc.ripetuto], [200, true]);
    const reused = await push(mf, { ...b, esito: "completo", errori: [], pagine: a.pagine });
    assert.deepEqual([reused.status, reused.doc.errore], [409, "generato_riusato"]);
    g = await read(mf);
    assert.equal(g.doc.generato, b.generato);
  });

  it("freschezza: un feed di ieri e' «vecchio» e dichiarato dati_vecchi", async () => {
    const mf = await startWorker(prodScript, CONFIG, outbound);
    const gen = Date.now() - (SCHOOL_SOGLIA_ORE + 7) * 3600_000;
    assert.equal((await push(mf, feed({ generato: iso(gen) }))).status, 200);
    const g = await read(mf);
    assert.equal(g.doc.consegna.stato, "vecchio");
    assert.equal(g.doc.consegna.soglia_ore, SCHOOL_SOGLIA_ORE);
    assert.ok(g.doc.consegna.eta_minuti >= (SCHOOL_SOGLIA_ORE + 7) * 60 - 1);
    assert.equal(g.doc.freschezza.stato, "dati_vecchi");
  });
});

describe("Durable Object School (ingresso locale, token di servizio simulato)", () => {
  const LOCAL = "http://127.0.0.1:8787";
  const SERVICE = {
    "CF-Access-Client-Id": "macschoolsimulato0001.access",
    "CF-Access-Client-Secret": "segreto-simulato-mac-school-solo-locale-0001",
  };
  it("il Mac simulato invia; segreto sbagliato = 403; il titolare simulato legge", async () => {
    const mf = await startWorker(
      localScript,
      { CUORE_STATO: "attivo", ACCESS_SERVIZIO_SCHOOL: "macschoolsimulato0001.access" },
      outbound,
    );
    const f = feed({ generato: iso(Date.now()) });
    const send = (h: Record<string, string>) =>
      mf.dispatchFetch(`${LOCAL}/api/school`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...h },
        body: JSON.stringify(f),
      });
    assert.equal(
      (await send({ ...SERVICE, "CF-Access-Client-Secret": "sbagliato-sbagliato-0001" })).status,
      403,
    );
    assert.equal((await send({})).status, 403);
    const ok = await send(SERVICE);
    assert.equal(ok.status, 200, await ok.clone().text());
    const login = await mf.dispatchFetch(`${LOCAL}/__locale/accedi?come=titolare`, { redirect: "manual" });
    const cookie = (login.headers.get("Set-Cookie") ?? "").split(";")[0] ?? "";
    const g = await mf.dispatchFetch(`${LOCAL}/api/school`, { headers: { Cookie: cookie } });
    assert.equal(g.status, 200);
    assert.equal(((await g.json()) as Doc).generato, f.generato);
    const asMac = await mf.dispatchFetch(`${LOCAL}/api/school`, { headers: SERVICE });
    assert.equal(asMac.status, 403, "il token del Mac non legge");
  });
});
