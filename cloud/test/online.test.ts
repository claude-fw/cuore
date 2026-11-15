/**
 * CUORE online con i dati veri (`CUORE_FINANCE_ORIGINE` = "mac"), SOLO DATI SINTETICI: ingresso di produzione in
 * workerd (Miniflare), database sintetico SENZA il marcatore `sintetico` (come il reale) migrato con M3, feed School
 * sintetico inviato col token del Mac; poi la pagina e le API del ponte (`/api/money`, `/api/search`, `/api/scritture`,
 * `POST /api/scritture/esegui`) come le usa `app/index.html`. Ricerca confrontata con `app/bridge/cuore_search.py`
 * sugli stessi feed. Nessuna rete: chiavi del team servite da `outboundService`.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { before, describe, it } from "node:test";
import { readImmutable } from "../../finance/src/db/node-sqlite.ts";
import { createSyntheticSession } from "../../finance/src/synthetic/generate.ts";
import { SYNTHETIC_DB } from "../../finance/src/synthetic/guard.ts";
import { inviaMigrazione, preparaMigrazione } from "../scripts/migra-finance.ts";
import { cerca, romaIso } from "../src/online.ts";
import { AUD, bundle, EMAIL, startWorker, TEAM, type TestKey, testKey, token } from "./helpers.ts";

// biome-ignore lint/suspicious/noExplicitAny: documenti JSON letti nei test
type Doc = Record<string, any>;
type Mf = Awaited<ReturnType<typeof startWorker>>;

const URL0 = "https://cuore.esempio.workers.dev";
const MAC = "0123456789abcdef0123456789abcdef.access";
const MIGRA = "abcdef0123456789abcdef0123456789.access";
const SCHOOL = JSON.parse(
  readFileSync(new URL("./fixtures/school-feed-sintetico.json", import.meta.url), "utf8"),
);
const CONFIG = {
  ACCESS_TEAM_DOMAIN: TEAM,
  ACCESS_AUD: AUD,
  ACCESS_ALLOWED_EMAILS: EMAIL,
  CUORE_STATO: "attivo",
  CUORE_FINANCE_ORIGINE: "mac",
  CUORE_SCRITTURE: "attive",
  ACCESS_SERVIZIO_SCHOOL: MAC,
  ACCESS_SERVIZIO_MIGRAZIONE: MIGRA,
};

let key: TestKey;
let script = "";
let dir = "";
const certs = (r: Request) =>
  r.url === `https://${TEAM}/cdn-cgi/access/certs`
    ? Response.json({ keys: [key.jwk] })
    : new Response("rete non disponibile nei test", { status: 599 });
const service = (cn: string) => token(key, { email: null, sub: "", common_name: cn });

before(async () => {
  key = await testKey("chiave-team-online");
  script = await bundle("index");
  dir = mkdtempSync(join(tmpdir(), "cuore-online-test-"));
});

/** Database sintetico SENZA il marcatore `sintetico`: per il motore e' come il database reale. */
async function comeIlReale(): Promise<string> {
  const s = await createSyntheticSession(join(dir, `s-${Math.random().toString(36).slice(2)}`));
  const path = join(s.realDir, SYNTHETIC_DB);
  const db = new DatabaseSync(path);
  db.exec("DELETE FROM meta WHERE key = 'dataset'");
  db.close();
  return path;
}

async function migrato(extra: Record<string, string> = {}): Promise<Mf> {
  const mf = await startWorker(script, { ...CONFIG, ...extra }, certs);
  const piano = await readImmutable(await comeIlReale(), (db) => preparaMigrazione(db));
  const jwt = await service(MIGRA);
  const e = await inviaMigrazione(piano, {
    base: URL0,
    fetch: (u, i) =>
      mf.dispatchFetch(u, {
        ...i,
        headers: { ...(i?.headers as Record<string, string>), "Cf-Access-Jwt-Assertion": jwt },
      }),
    clientId: MIGRA,
    clientSecret: "segreto-fittizio-solo-test-0001",
  });
  assert.equal(e.ok, true, JSON.stringify(e));
  const s = await mf.dispatchFetch(`${URL0}/api/school`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Cf-Access-Jwt-Assertion": await service(MAC) },
    body: JSON.stringify({ ...SCHOOL, generato: romaIso(new Date()) }),
  });
  assert.equal(s.status, 200, await s.text());
  return mf;
}

async function get(mf: Mf, path: string, auth = true): Promise<{ status: number; doc: Doc; text: string }> {
  const r = await mf.dispatchFetch(
    `${URL0}${path}`,
    auth ? { headers: { "Cf-Access-Jwt-Assertion": await token(key) } } : {},
  );
  const text = await r.text();
  return { status: r.status, doc: text.startsWith("{") ? (JSON.parse(text) as Doc) : {}, text };
}

async function esegui(
  mf: Mf,
  body: Doc,
  headers: Record<string, string> = {},
): Promise<{ status: number; doc: Doc }> {
  const r = await mf.dispatchFetch(`${URL0}/api/scritture/esegui`, {
    method: "POST",
    headers: {
      "Cf-Access-Jwt-Assertion": await token(key),
      Origin: URL0,
      "Content-Type": "application/json",
      ...headers,
    },
    body: JSON.stringify(body),
  });
  const text = await r.text();
  return { status: r.status, doc: text.startsWith("{") ? (JSON.parse(text) as Doc) : {} };
}

describe("CUORE online con i dati veri (solo dati sintetici)", () => {
  let mf: Mf;
  let conto = "";
  before(async () => {
    mf = await migrato();
  });

  it("prima della migrazione: pagina online, feed Money dichiarato in attesa, scritture Finance rifiutate", async () => {
    const vuoto = await startWorker(script, CONFIG, certs);
    const p = await get(vuoto, "/");
    assert.equal(p.status, 200);
    assert.match(
      p.text,
      /<meta name="cuore-online" content="reali"><meta name="cuore-scritture" content="reali">/,
    );
    const m = await get(vuoto, "/api/money");
    assert.equal(m.doc.esito, "non_disponibile");
    assert.equal(m.doc.errori[0].codice, "in_attesa_dei_dati_del_mac");
    const w = await esegui(vuoto, { op: "stato_budget", input: {} });
    assert.equal(w.status, 409);
    assert.equal(w.doc.errore, "in_attesa_dei_dati_del_mac");
  });

  it("la pagina dice «online · dati reali», non la vetrina; Access obbligatorio ovunque", async () => {
    const p = await get(mf, "/");
    assert.match(p.text, /<meta name="cuore-online" content="reali">/);
    assert.doesNotMatch(p.text, /<meta name="cuore-vetrina" content="1">/);
    for (const path of ["/", "/api/money", "/api/school", "/api/search?q=conto", "/api/scritture"])
      assert.equal((await get(mf, path, false)).status, 403, path);
    const altro = await mf.dispatchFetch(`${URL0}/api/money`, {
      headers: { "Cf-Access-Jwt-Assertion": await token(key, { email: "estraneo@prova.invalid" }) },
    });
    assert.equal(altro.status, 403);
    const r = await mf.dispatchFetch(`${URL0}/api/scritture/esegui`, {
      method: "POST",
      headers: { Origin: URL0, "Content-Type": "application/json" },
      body: JSON.stringify({ op: "stato_budget", input: {} }),
    });
    assert.equal(r.status, 403, "scrittura senza Access");
    assert.equal(
      (await esegui(mf, { op: "stato_budget", input: {} }, { Origin: "https://altro.invalid" })).status,
      403,
    );
  });

  it("feed Money completo dal Durable Object, nel contratto della pagina", async () => {
    const m = await get(mf, "/api/money");
    assert.equal(m.status, 200);
    const f = m.doc;
    assert.equal(f.schema, "cuore.money.feed/1");
    assert.equal(f.origine, "cloud");
    assert.equal(f.sola_lettura_verificata, true);
    assert.equal(f.esito, "completo", JSON.stringify(f.errori));
    const attese = ["accounts", "overview", "summary", "categories", "planning", "balances", "data_status"];
    attese.push("obligations", "transactions", "trend", "manual", "budgets", "daily");
    assert.deepEqual(Object.keys(f.operazioni).sort(), attese.sort());
    assert.deepEqual(f.scritture, { database: "dati reali", banche: false, attive: true });
    assert.match(f.generato, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$/);
    assert.ok(["ok", "dati_vecchi", "nessun_dato"].includes(f.aggiornamento.stato));
    const accounts = f.operazioni.accounts.data as Doc[];
    assert.ok(accounts.length >= 2);
    for (const a of accounts) assert.equal("profile" in a, false, "nessun profilo di titolarita'");
    conto = accounts.find((a) => !a.closed)?.id;
    const s = await get(mf, "/api/vetrina/stato");
    assert.equal(s.doc.dati, "dal_mac");
    assert.equal(s.doc.finance.consultazione_ok, true, "anche la ricerca v8 risponde sui dati non sintetici");
  });

  it("scritture Finance dal telefono: requestId idempotente, correzione, annullamento, categoria, budget, contanti", async () => {
    const spesa = {
      accountId: conto,
      transactionDate: romaIso(new Date()).slice(0, 10),
      amountMinor: -450,
      currency: "EUR",
      description: "Pizza al taglio online",
    };
    const req = { op: "aggiungi_manuale", requestId: "online-spesa-0001", input: spesa };
    const a = await esegui(mf, req);
    assert.equal(a.status, 200, JSON.stringify(a.doc));
    assert.equal(a.doc.risultato.status, "created");
    const b = await esegui(mf, req);
    assert.equal(b.doc.ripetuta, true, "stessa richiesta = nessun doppione");
    assert.equal(b.doc.risultato.id, a.doc.risultato.id);
    assert.equal((await esegui(mf, { ...req, input: { ...spesa, amountMinor: -1 } })).status, 409);
    assert.equal(
      (await esegui(mf, { op: "aggiungi_manuale", input: spesa })).doc.errore,
      "requestId_mancante",
    );
    const id = a.doc.risultato.id as string;
    const fix = await esegui(mf, {
      op: "correggi_manuale",
      requestId: "online-corr-0001",
      input: { id, modifiche: { amountMinor: -500 }, motivo: "importo sbagliato" },
    });
    assert.equal(fix.status, 200, JSON.stringify(fix.doc));
    const cat = await esegui(mf, {
      op: "classifica",
      requestId: "online-cat-0001",
      input: { id, tipo: "expense", categoria: null, nota: null },
    });
    assert.equal(cat.status, 200, JSON.stringify(cat.doc));
    const mese = romaIso(new Date()).slice(0, 7);
    const budget = await esegui(mf, {
      op: "imposta_budget",
      requestId: "online-budget-0001",
      input: { scope: "total", target: null, validFrom: mese, currency: "EUR", amountMinor: 30000 },
    });
    assert.equal(budget.status, 200, JSON.stringify(budget.doc));
    const cash = { id: "contanti", name: "Contanti", type: "cash", currency: "EUR" };
    assert.equal(
      (await esegui(mf, { op: "crea_conto", requestId: "online-conto-0001", input: cash })).status,
      200,
    );
    const consulta = await esegui(mf, {
      op: "consulta",
      input: { operazione: "budgets", input: { month: mese } },
    });
    assert.equal(consulta.doc.risultato.ok, true);
    const m = (await get(mf, "/api/money")).doc;
    const manuale = (m.operazioni.manual.data.items as Doc[]).find((x) => x.id === id);
    assert.equal(manuale?.amountMinor, -500);
    assert.ok((m.operazioni.accounts.data as Doc[]).some((x) => x.id === "contanti"));
    const via = await esegui(mf, {
      op: "annulla_manuale",
      requestId: "online-annulla-0001",
      input: { id, motivo: "prova" },
    });
    assert.equal(via.status, 200, JSON.stringify(via.doc));
    const dopo = (await get(mf, "/api/money")).doc;
    assert.equal((dopo.operazioni.manual.data.items as Doc[]).find((x) => x.id === id)?.voided, true);
  });

  it("School: «Sincronizza tutto» dal telefono lascia una richiesta, il Mac la legge, il feed successivo la chiude", async () => {
    const mac = async () => {
      const r = await mf.dispatchFetch(`${URL0}/api/school/richiesta`, {
        headers: { "Cf-Access-Jwt-Assertion": await service(MAC) },
      });
      return { status: r.status, doc: (await r.json()) as Doc };
    };
    assert.equal((await mac()).doc.risultato.richiesta, null);
    const r = await esegui(mf, { op: "richiedi_school", input: {} });
    assert.equal(r.status, 200, JSON.stringify(r.doc));
    assert.equal(typeof r.doc.risultato.richiesta, "string");
    assert.equal((await mac()).doc.risultato.richiesta, r.doc.risultato.richiesta);
    const titolare = await get(mf, "/api/school/richiesta");
    assert.equal(titolare.status, 403, "la richiesta la legge solo il Mac");
    const s = await mf.dispatchFetch(`${URL0}/api/school`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Cf-Access-Jwt-Assertion": await service(MAC) },
      body: JSON.stringify({ ...SCHOOL, generato: romaIso(new Date(Date.now() + 1000)) }),
    });
    assert.equal(s.status, 200, await s.text());
    assert.equal((await mac()).doc.risultato.richiesta, null, "giro fatto dopo la richiesta: chiusa");
  });

  it("School: feed del Mac, note/impegni/compiti fatti nel cloud (idempotenti); promemoria e appunti anche online", async () => {
    const s = await get(mf, "/api/school");
    assert.equal(s.status, 200);
    assert.equal(s.doc.schema, "cuore.school.feed/1");
    const nota = { tipo: "nota", testo: "Ripassare i logaritmi", materia: "matematica" };
    const a = await esegui(mf, { op: "school_scrivi", requestId: "online-nota-0001", input: nota });
    assert.equal(a.status, 200, JSON.stringify(a.doc));
    const b = await esegui(mf, { op: "school_scrivi", requestId: "online-nota-0001", input: nota });
    assert.equal(b.doc.risultato.id, a.doc.risultato.id, "stessa richiesta = stesso evento");
    assert.equal(
      (
        await esegui(mf, {
          op: "school_scrivi",
          requestId: "online-nota-0001",
          input: { ...nota, testo: "altro" },
        })
      ).status,
      422,
    );
    assert.equal((await esegui(mf, { op: "school_scrivi", input: nota })).doc.errore, "requestId_mancante");
    await esegui(mf, {
      op: "school_scrivi",
      requestId: "online-imp-0001",
      input: { tipo: "impegno", testo: "Ripetizioni", data: "2026-11-20" },
    });
    await esegui(mf, {
      op: "school_scrivi",
      requestId: "online-fatto-0001",
      input: { tipo: "compito", uid: "cv:1", fatto: true },
    });
    assert.equal(
      (
        await esegui(mf, {
          op: "school_scrivi",
          requestId: "online-bad-0001",
          input: { tipo: "impegno", testo: "x", data: "2026-02-30" },
        })
      ).status,
      422,
    );
    await esegui(mf, {
      op: "school_scrivi",
      requestId: "online-togli-0001",
      input: { tipo: "annulla", id: a.doc.risultato.id, motivo: "fatto" },
    });
    const st = (await esegui(mf, { op: "school_stato", input: {} })).doc.risultato;
    assert.equal(st.schema, "cuore.school.personale/1");
    assert.equal(st.note.length, 0, "nota annullata");
    assert.equal(st.impegni.length, 1);
    assert.ok(st.fatti["cv:1"]);
    assert.equal(st.eventi, 4);
    // promemoria e appunti ora anche online (notifiche.test.ts): stessi formati del ponte
    assert.equal((await esegui(mf, { op: "promemoria_stato", input: {} })).doc.risultato.file, "assente");
    assert.equal((await esegui(mf, { op: "appunti_scrivi", input: {} })).doc.errore, "requestId_mancante");
    assert.equal(
      (await esegui(mf, { op: "rileva_trasferimenti", input: {} })).doc.errore,
      "operazione_sconosciuta",
    );
    assert.equal((await get(mf, "/api/events")).status, 503);
    assert.equal((await get(mf, "/api/promemoria")).doc.schema, "cuore.promemoria.impostazioni/1");
    const cap = (await get(mf, "/api/scritture")).doc;
    assert.equal(cap.attive, true);
    assert.ok(cap.operazioni.includes("school_scrivi") && cap.operazioni.includes("crea_conto"));
  });

  it("ricerca: School e Money letti; domanda troppo corta rifiutata", async () => {
    const r = await get(mf, "/api/search?q=matematica");
    assert.equal(r.status, 200);
    assert.equal(r.doc.schema, "cuore.search/1");
    assert.equal(r.doc.fonti.school.stato, "letta");
    assert.equal(r.doc.fonti.money.stato, "letta");
    assert.equal(r.doc.fonti.classroom.stato, "non_collegata");
    assert.equal((await get(mf, "/api/search?q=a")).status, 400);
    assert.equal((await get(mf, "/api/search?q=conto&limite=x")).status, 400);
  });

  it("scritture spente (CUORE_SCRITTURE assente): pagina in sola lettura, POST rifiutato", async () => {
    const ro = await migrato({ CUORE_SCRITTURE: "" });
    const p = await get(ro, "/");
    assert.match(p.text, /<meta name="cuore-online" content="reali">/);
    assert.doesNotMatch(p.text, /<meta name="cuore-scritture"/);
    assert.equal((await esegui(ro, { op: "stato_budget", input: {} })).doc.errore, "scritture_spente");
    assert.equal((await get(ro, "/api/money")).doc.scritture.attive, undefined);
  });

  it("senza CUORE_FINANCE_ORIGINE resta la vetrina: nessuna API dei dati veri", async () => {
    const v = await startWorker(script, { ...CONFIG, CUORE_FINANCE_ORIGINE: "" }, certs);
    const page = (await get(v, "/")).text;
    assert.match(page, /<meta name="cuore-vetrina" content="1">/);
    assert.doesNotMatch(page, /<meta name="cuore-online"/);
    for (const path of ["/api/money", "/api/search?q=conto", "/api/scritture", "/api/events"])
      assert.equal((await get(v, path)).status, 404, path);
    assert.equal((await esegui(v, { op: "stato_budget", input: {} })).status, 405);
    assert.equal((await get(v, "/api/vetrina/stato")).doc.dati, "sintetici");
  });

  it("stessa ricerca del ponte del Mac (cuore_search.py) sugli stessi feed", async (t) => {
    const py = spawnSync("python3", ["-c", "print(1)"]);
    if (py.status !== 0) return t.skip("python3 non disponibile");
    const school = (await get(mf, "/api/school")).doc;
    const money = (await get(mf, "/api/money")).doc;
    const fs = join(dir, "feed-scuola.json");
    const fm = join(dir, "feed-money.json");
    writeFileSync(fs, JSON.stringify(school));
    writeFileSync(fm, JSON.stringify(money));
    const bridge = new URL("../../app/bridge", import.meta.url).pathname;
    for (const q of ["matematica", "conto", "ripasso verifica", "Caffè", "zz"]) {
      const r = spawnSync(
        "python3",
        [
          "-B",
          "-c",
          "import json,sys; sys.path.insert(0, sys.argv[1]); import cuore_search as S; " +
            "s, m = (json.load(open(p)) for p in sys.argv[2:4]); " +
            "print(json.dumps(S.search(sys.argv[4], school_fn=lambda: s, money_fn=lambda: m), ensure_ascii=False))",
          bridge,
          fs,
          fm,
          q,
        ],
        { encoding: "utf8" },
      );
      assert.equal(r.status, 0, r.stderr);
      const atteso = JSON.parse(r.stdout) as Doc;
      const ottenuto = cerca(q, 50, school, money);
      assert.deepEqual(ottenuto, atteso, q);
    }
  });
});
