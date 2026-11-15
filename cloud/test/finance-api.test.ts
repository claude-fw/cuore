/**
 * API di scrittura Finance (C2) in workerd (Miniflare), sul Durable Object sintetico v7, senza rete: interruttore,
 * controlli d'ingresso, idempotenza per `requestId`, conflitti di versione, esiti del motore.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { before, describe, it } from "node:test";
import { gzipSync } from "node:zlib";
import { AUD, bundle, EMAIL, startWorker, TEAM, type TestKey, testKey, token } from "./helpers.ts";

const URL0 = "https://cuore.esempio.workers.dev";
const CONFIG = {
  ACCESS_TEAM_DOMAIN: TEAM,
  ACCESS_AUD: AUD,
  ACCESS_ALLOWED_EMAILS: EMAIL,
  CUORE_STATO: "attivo",
  CUORE_SCRITTURE: "attive",
};

let key: TestKey;
let script = "";
const certs = (r: Request) =>
  r.url === `https://${TEAM}/cdn-cgi/access/certs`
    ? Response.json({ keys: [key.jwk] })
    : new Response("rete non disponibile nei test", { status: 599 });

before(async () => {
  key = await testKey("chiave-team");
  script = await bundle("index");
});

type Mf = Awaited<ReturnType<typeof startWorker>>;
// biome-ignore lint/suspicious/noExplicitAny: risposte JSON lette nei test
type Doc = Record<string, any>;

async function post(mf: Mf, body: unknown, headers: Record<string, string> = {}) {
  const r = await mf.dispatchFetch(`${URL0}/api/finance/esegui`, {
    method: "POST",
    headers: {
      "Cf-Access-Jwt-Assertion": await token(key),
      Origin: URL0,
      "Content-Type": "application/json",
      ...headers,
    },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
  return { status: r.status, doc: (await r.json()) as Doc };
}

const spesa = (extra: Doc = {}) => ({
  accountId: "contanti-demo",
  transactionDate: "2026-11-02",
  amountMinor: -450,
  currency: "EUR",
  description: "Pizza al taglio",
  ...extra,
});

describe("API di scrittura Finance (C2, sintetico)", () => {
  it("chiusa per difetto; Origin, tipo, dimensione e richiesta controllati prima del Durable Object", async () => {
    const off = await startWorker(script, { ...CONFIG, CUORE_SCRITTURE: "" }, certs);
    assert.equal((await post(off, { op: "stato_budget" })).doc.errore, "scritture_spente");
    const mf = await startWorker(script, CONFIG, certs);
    const cases: [number, string, unknown, Record<string, string>][] = [
      [403, "origine_non_ammessa", { op: "consulta" }, { Origin: "https://altro.invalid" }],
      [415, "tipo_non_ammesso", { op: "consulta" }, { "Content-Type": "text/plain" }],
      [413, "richiesta_troppo_grande", { x: "a".repeat(70_000) }, {}],
      [400, "richiesta_non_valida", "{non json", {}],
      [400, "operazione_sconosciuta", { op: "cancella_tutto" }, {}],
      [400, "requestId_mancante", { op: "aggiungi_manuale", input: spesa() }, {}],
      [400, "versione_non_valida", { op: "stato_budget", versione: "x" }, {}],
    ];
    for (const [status, errore, body, headers] of cases) {
      const r = await post(mf, body, headers);
      assert.equal(r.status, status, errore);
      assert.equal(r.doc.errore, errore);
    }
    const noAuth = await mf.dispatchFetch(`${URL0}/api/finance/esegui`, { method: "POST", body: "{}" });
    assert.equal(noAuth.status, 403);
  });

  it("spesa in contanti: creata una volta, ripetuta con lo stesso requestId, id riusato = 409, storico", async () => {
    const mf = await startWorker(script, CONFIG, certs);
    const req = { op: "aggiungi_manuale", requestId: "spesa-0001-prova", input: spesa() };
    const a = await post(mf, req);
    assert.equal(a.status, 200);
    assert.equal(a.doc.risultato.status, "created");
    const b = await post(mf, req);
    assert.equal(b.status, 200);
    assert.equal(b.doc.ripetuta, true);
    assert.equal(b.doc.risultato.id, a.doc.risultato.id);
    assert.equal(b.doc.versione, a.doc.versione, "la ripetizione non scrive");
    const c = await post(mf, { ...req, input: spesa({ amountMinor: -500 }) });
    assert.equal(c.status, 409);
    assert.equal(c.doc.errore, "richiesta_riusata");
    // possibile doppione: niente registrato, la conferma riusa lo stesso requestId
    const d1 = { op: "aggiungi_manuale", requestId: "spesa-0002-prova", input: spesa() };
    const d = await post(mf, d1);
    assert.equal(d.doc.risultato.status, "possible_duplicate");
    const e = await post(mf, { ...d1, input: spesa({ confirmDuplicate: true }) });
    assert.equal(e.doc.risultato.status, "created");
    const h = await post(mf, { op: "storico_manuale", input: { id: a.doc.risultato.id } });
    assert.equal(h.status, 200);
    assert.equal(h.doc.risultato.length, 1);
  });

  it("correzione e annullamento con versione: dati cambiati = 409 senza scrivere; esiti 422 e 404", async () => {
    const mf = await startWorker(script, CONFIG, certs);
    const a = await post(mf, { op: "aggiungi_manuale", requestId: "spesa-0003-prova", input: spesa() });
    const id = a.doc.risultato.id as string;
    const v = a.doc.versione as number;
    const fix = (requestId: string, versione: number) => ({
      op: "correggi_manuale",
      requestId,
      versione,
      input: { id, modifiche: { amountMinor: -480 }, motivo: "scontrino" },
    });
    const ok = await post(mf, fix("corr-0001-prova", v));
    assert.equal(ok.status, 200);
    assert.equal(ok.doc.versione, v + 1);
    const stale = await post(mf, fix("corr-0002-prova", v));
    assert.equal(stale.status, 409);
    assert.equal(stale.doc.errore, "conflitto");
    assert.equal(stale.doc.versione, v + 1);
    const bad = await post(mf, {
      op: "annulla_manuale",
      requestId: "annu-0001-prova",
      input: { id, motivo: "" },
    });
    assert.equal(bad.status, 422);
    const gone = await post(mf, {
      op: "annulla_manuale",
      requestId: "annu-0002-prova",
      input: { id: "inesistente", motivo: "prova" },
    });
    assert.equal(gone.status, 404);
    const voided = await post(mf, {
      op: "annulla_manuale",
      requestId: "annu-0003-prova",
      input: { id, motivo: "doppia" },
    });
    assert.equal(voided.status, 200);
    const h = await post(mf, { op: "storico_manuale", input: { id } });
    assert.deepEqual(
      h.doc.risultato.map((e: Doc) => e.reason),
      [null, "scontrino", "doppia"],
    );
  });

  it("budget totale del mese e consultazione sullo schema v7", async () => {
    const mf = await startWorker(script, CONFIG, certs);
    const set = await post(mf, {
      op: "imposta_budget",
      requestId: "budg-0001-prova",
      input: { scope: "total", target: null, validFrom: "2026-11", currency: "EUR", amountMinor: 30000 },
    });
    assert.equal(set.status, 200, JSON.stringify(set.doc));
    const st = await post(mf, { op: "stato_budget", input: { mese: "2026-11", valuta: "EUR" } });
    assert.equal(st.status, 200);
    assert.ok(JSON.stringify(st.doc.risultato).includes("30000"));
    const q = await post(mf, {
      op: "consulta",
      input: { operazione: "accounts", oggi: "2026-11-02" },
    });
    assert.equal(q.status, 200, JSON.stringify(q.doc));
  });

  it("Contanti: saldo indicato a mano più i movimenti segnati dopo (annullati esclusi)", async () => {
    const mf = await startWorker(script, CONFIG, certs);
    const ok = async (body: Doc) => {
      const r = await post(mf, body);
      assert.equal(r.status, 200, JSON.stringify(r.doc));
      return r.doc.risultato;
    };
    await ok({
      op: "crea_conto",
      requestId: "conto-cash-01",
      input: { id: "contanti", name: "Contanti DEMO", type: "cash", currency: "EUR" },
    });
    const saldo = { op: "saldo_manuale", input: { conto: "contanti", data: "2026-11-10", importo: 8000 } };
    await ok({ ...saldo, requestId: "saldo-cash-01" });
    assert.equal(
      (await ok({ ...saldo, requestId: "saldo-cash-02" })).created,
      false,
      "stesso saldo: nessun doppione",
    );
    const spesa = (rid: string, amountMinor: number, d: string) =>
      ok({
        op: "aggiungi_manuale",
        requestId: rid,
        input: {
          requestId: rid,
          accountId: "contanti",
          transactionDate: d,
          amountMinor,
          currency: "EUR",
          description: `Spesa DEMO ${rid}`,
        },
      });
    await spesa("spesa-cash-01", -1250, "2026-11-11");
    const annullata = await spesa("spesa-cash-02", -500, "2026-11-12");
    await ok({
      op: "annulla_manuale",
      requestId: "annulla-cash-1",
      input: { id: annullata.id, motivo: "segnata per errore" },
    });
    const q = await ok({ op: "consulta", input: { operazione: "balances", oggi: "2026-11-13" } });
    const c = q.data.accounts.find((x: Doc) => x.accountId === "contanti");
    assert.equal(c.balanceMinor, 6750, "80,00 − 12,50; la spesa annullata non conta");
  });

  it("Da comprare: aggiungi_acquisto e segna_acquisto con requestId (ripetuta = nessun doppione)", async () => {
    const mf = await startWorker(script, CONFIG, certs);
    const req = {
      op: "aggiungi_acquisto",
      input: { title: "Cassa DEMO", currency: "EUR", estimatedMinor: 8500 },
      requestId: "acq-0000001",
    };
    const a = await post(mf, req);
    assert.equal(a.status, 200, JSON.stringify(a.doc));
    const again = await post(mf, req);
    assert.equal(again.doc.ripetuta, true);
    assert.equal(again.doc.risultato.id, a.doc.risultato.id);
    assert.equal(
      (
        await post(mf, {
          op: "segna_acquisto",
          input: { id: a.doc.risultato.id, stato: "purchased" },
          requestId: "acq-0000002",
        })
      ).status,
      422,
    );
    const c = await post(mf, {
      op: "segna_acquisto",
      input: { id: a.doc.risultato.id, stato: "cancelled" },
      requestId: "acq-0000003",
    });
    assert.equal(c.status, 200, JSON.stringify(c.doc));
    const q = await post(mf, { op: "consulta", input: { operazione: "planning", oggi: "2026-11-02" } });
    const mine = q.doc.risultato.data.purchases.filter((p: Doc) => p.title === "Cassa DEMO");
    assert.deepEqual(
      mine.map((p: Doc) => p.status),
      ["cancelled"],
    );
  });

  it("Movimenti da estratto e registro crediti: si importano, si ricaricano senza doppioni, il registro si applica una volta", async () => {
    const mf = await startWorker(script, CONFIG, certs);
    const csv = [
      "transaction_id,transaction_date,booking_date,status,amount,currency,description,counterparty,original_amount,original_currency",
      "demo-prestito-1,2026-11-03,2026-11-03,booked,-50.00,EUR,Prestito DEMO,Amico DEMO,,",
      "demo-spesa-1,2026-11-04,2026-11-04,booked,-3.75,EUR,Spesa DEMO,Negozio DEMO,,",
    ].join("\n");
    const gz = (t: string) => gzipSync(Buffer.from(t)).toString("base64");
    const imp = (id: string) =>
      post(mf, {
        op: "importa_movimenti",
        input: { conto: "conto-demo", gzip: gz(csv), nome: "demo.csv" },
        requestId: id,
      });
    const a = await imp("imp-00000001");
    assert.equal(a.status, 200, JSON.stringify(a.doc));
    assert.equal(a.doc.risultato.conteggi.created, 2);
    const b = await imp("imp-00000002");
    assert.equal(b.status, 200, JSON.stringify(b.doc));
    assert.notEqual(b.doc.risultato.conteggi.created, 2);
    const registro = {
      schema: "cuore-registro/1",
      controparti: [{ id: "amico-demo", nome: "Amico DEMO" }],
      voci: [
        {
          controparte: "amico-demo",
          direzione: "receivable",
          importo: 5000,
          aperto: "2026-11-03",
          descrizione: "Prestito DEMO",
          movimento: { conto: "conto-demo", data: "2026-11-03", importo: -5000 },
          eventi: [{ tipo: "increase", importo: 2000, data: "2026-11-05", nota: "in contanti" }],
        },
      ],
    };
    const r1 = await post(mf, {
      op: "registro_crediti",
      input: { gzip: gz(JSON.stringify(registro)) },
      requestId: "reg-00000001",
    });
    assert.equal(r1.status, 200, JSON.stringify(r1.doc));
    assert.deepEqual(r1.doc.risultato, { aperte: 1, saltate: 0, eventi: 1 });
    const r2 = await post(mf, {
      op: "registro_crediti",
      input: { gzip: gz(JSON.stringify(registro)) },
      requestId: "reg-00000002",
    });
    assert.equal(r2.doc.risultato.saltate, 1);
    assert.equal(r2.doc.risultato.eventi, 0, "ricaricato: nessun evento doppio");
    registro.voci[0]?.eventi.push({ tipo: "payment", importo: 3000, data: "2026-11-06", nota: "restituiti" });
    const r3 = await post(mf, {
      op: "registro_crediti",
      input: { gzip: gz(JSON.stringify(registro)) },
      requestId: "reg-00000003",
    });
    assert.deepEqual(
      r3.doc.risultato,
      { aperte: 0, saltate: 1, eventi: 1 },
      "voce esistente: solo l'evento nuovo",
    );
    const q = await post(mf, { op: "consulta", input: { operazione: "obligations", oggi: "2026-11-10" } });
    assert.equal(q.status, 200, JSON.stringify(q.doc));
    assert.equal(q.doc.risultato.data.position[0].receivable, 4000);
    const t = await post(mf, {
      op: "consulta",
      input: {
        operazione: "transactions",
        input: { from: "2026-11-01", to: "2026-11-10" },
        oggi: "2026-11-10",
      },
    });
    assert.equal(t.status, 200, JSON.stringify(t.doc));
    const prestito = t.doc.risultato.data.items.find((x: Doc) => x.amountMinor === -5000);
    assert.equal(prestito.kind, "obligation");
    assert.equal(prestito.why, "Prestito DEMO", "il perché del movimento è il credito a cui è collegato");
  });

  it("Regole di classificazione: file compresso → nuova versione (o la stessa) e riclassificazione", async () => {
    const mf = await startWorker(script, CONFIG, certs);
    const file = readFileSync(new URL("../../finance/examples/sintetici/regole-demo.json", import.meta.url));
    const gzip = gzipSync(file).toString("base64");
    const r = await post(mf, { op: "pubblica_regole", input: { gzip }, requestId: "regole-0000001" });
    assert.equal(r.status, 200, JSON.stringify(r.doc));
    assert.ok(Number.isInteger(r.doc.risultato.version));
    assert.ok(r.doc.risultato.classificazione.examined > 0);
    const bad = await post(mf, {
      op: "pubblica_regole",
      input: { gzip: "non-gzip" },
      requestId: "regole-0000002",
    });
    assert.equal(bad.status, 422, JSON.stringify(bad.doc));
    assert.match(bad.doc.messaggio ?? bad.doc.motivo ?? JSON.stringify(bad.doc), /gzip/);
  });

  it("Automazioni nel cloud: stato_automazioni si legge dalla pagina (sola lettura)", async () => {
    const mf = await startWorker(script, CONFIG, certs);
    const r = await post(mf, { op: "stato_automazioni", input: {} });
    assert.equal(r.status, 200, JSON.stringify(r.doc));
    assert.ok(Array.isArray(r.doc.risultato.lavori));
    assert.ok(Array.isArray(r.doc.risultato.coda));
    assert.match(r.doc.risultato.notifiche, /^(attive|spente)$/);
  });
});
