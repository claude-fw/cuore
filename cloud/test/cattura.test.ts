/**
 * Cattura Apple Pay (C3, sintetico) in workerd (Miniflare): schema chiuso, idempotenza, limite di frequenza esatto
 * e sospensione, abbinamento dopo la sincronizzazione (mai conferma automatica).
 */
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { before, describe, it } from "node:test";
import { AUD, bundle, EMAIL, startWorker, TEAM, type TestKey, testKey, token } from "./helpers.ts";

const URL0 = "https://cuore.esempio.workers.dev";
const TOKEN = "cattura-di-prova-0001";
const TOKEN_HASH = Buffer.from(
  await webcrypto.subtle.digest("SHA-256", new TextEncoder().encode(TOKEN)),
).toString("hex");

let key: TestKey;
let script = "";
const outbound = (r: Request) => {
  if (r.url === `https://${TEAM}/cdn-cgi/access/certs`) return Response.json({ keys: [key.jwk] });
  return new Response("rete non disponibile nei test", { status: 599 });
};

before(async () => {
  key = await testKey("chiave-team-cattura");
  script = await bundle("index");
});

type Mf = Awaited<ReturnType<typeof startWorker>>;
// biome-ignore lint/suspicious/noExplicitAny: risposte JSON lette nei test
type Doc = Record<string, any>;

const start = () =>
  startWorker(
    script,
    {
      ACCESS_TEAM_DOMAIN: TEAM,
      ACCESS_AUD: AUD,
      ACCESS_ALLOWED_EMAILS: EMAIL,
      CUORE_STATO: "attivo",
      CUORE_SCRITTURE: "attive",
      CATTURA_TOKEN_SHA256: TOKEN_HASH,
    },
    outbound,
  );

async function cattura(
  mf: Mf,
  body: Record<string, unknown>,
  bearer: string | null = TOKEN,
): Promise<{ status: number; doc: Doc }> {
  const r = await mf.dispatchFetch(`${URL0}/cattura`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(bearer === null ? {} : { Authorization: `Bearer ${bearer}` }),
    },
    body: JSON.stringify(body),
  });
  return { status: r.status, doc: (await r.json()) as Doc };
}

const base = (id: string) => ({
  id,
  importo: "12.30",
  valuta: "EUR",
  esercente: "Bar Centrale",
  carta: "Apple Pay · Visa",
  istante: new Date().toISOString(),
});

const cron = async (mf: Mf) => {
  const w = await (
    mf as unknown as { getWorker(): Promise<{ scheduled(o: unknown): Promise<unknown> }> }
  ).getWorker();
  await w.scheduled({ scheduledTime: new Date(), cron: "0 5,11,17,21 * * *" });
};

async function esegui(mf: Mf, name: string, input: Doc = {}): Promise<Doc> {
  const r = await mf.dispatchFetch(`${URL0}/api/finance/esegui`, {
    method: "POST",
    headers: {
      "Cf-Access-Jwt-Assertion": await token(key),
      Origin: URL0,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ op: name, input, requestId: `cattura-prova-${crypto.randomUUID()}` }),
  });
  const doc = (await r.json()) as Doc;
  assert.equal(r.status, 200, `${name}: ${JSON.stringify(doc)}`);
  return doc;
}

const versione = async (mf: Mf) => (await esegui(mf, "notifiche")).versione as number;

async function op(mf: Mf, name: string, input: Doc = {}): Promise<Doc> {
  return (await esegui(mf, name, input)).risultato;
}

describe("cattura Apple Pay (C3, sintetico)", () => {
  it("token sbagliato o assente = 401; nessuna scrittura", async () => {
    const mf = await start();
    assert.equal((await cattura(mf, base("cattura-0001-prova"), "sbagliato")).status, 401);
    assert.equal((await cattura(mf, base("cattura-0002-prova"), null)).status, 401);
    assert.deepEqual(await op(mf, "catture"), []);
  });

  it("cattura visibile come provvisoria subito dopo l'invio", async () => {
    const mf = await start();
    const { status, doc } = await cattura(mf, base("cattura-0003-prova"));
    assert.equal(status, 200);
    assert.equal(doc.risultato.stato, "provvisoria");
    const list = await op(mf, "catture");
    assert.equal(list.length, 1);
    assert.equal(list[0].esercente, "Bar Centrale");
    assert.equal(list[0].importo_minor, 1230);
  });

  it("stesso id due volte = una sola cattura; stesso id con contenuto diverso = 409", async () => {
    const mf = await start();
    const corpo = base("cattura-0004-prova");
    const r1 = await cattura(mf, corpo);
    assert.equal(r1.status, 200);
    const r2 = await cattura(mf, corpo);
    assert.equal(r2.status, 200);
    assert.equal(r2.doc.ripetuta, true);
    const r3 = await cattura(mf, { ...corpo, importo: "9.99" });
    assert.equal(r3.status, 409);
    assert.equal((await op(mf, "catture")).length, 1);
  });

  it("importo vuoto: rifiutata (422) e segnalata per la revisione", async () => {
    const mf = await start();
    const prima = await versione(mf);
    const { status, doc } = await cattura(mf, { ...base("cattura-0005-prova"), importo: "" });
    assert.equal(status, 422);
    // "segnalata": la revisione e' una riga del nucleo Finance (review_items), non solo la risposta 422.
    assert.equal(await versione(mf), prima + 1, "la segnalazione deve scrivere nel nucleo Finance");
    assert.equal(doc.errore, "importo_vuoto");
    assert.equal((await op(mf, "catture")).length, 0, "nessuna cattura creata");
  });

  it("11a richiesta nello stesso minuto = 429; 3 superamenti consecutivi sospendono il token", async () => {
    const mf = await start();
    for (let i = 0; i < 10; i++) {
      const r = await cattura(mf, base(`cattura-limite-${String(i).padStart(4, "0")}-prova`));
      assert.equal(r.status, 200, `richiesta ${i}`);
    }
    const r11 = await cattura(mf, base("cattura-limite-0010-prova"));
    assert.equal(r11.status, 429);
    const r12 = await cattura(mf, base("cattura-limite-0011-prova"));
    assert.equal(r12.status, 429);
    const r13 = await cattura(mf, base("cattura-limite-0012-prova"));
    assert.equal(r13.status, 429);
    // quarto tentativo oltre il limite, col token ormai sospeso: 403, non 429
    const r14 = await cattura(mf, base("cattura-limite-0013-prova"));
    assert.equal(r14.status, 403);
    assert.equal(r14.doc.errore, "token_sospeso");
  });

  it("schema chiuso: campi non ammessi o mancanti rifiutati", async () => {
    const mf = await start();
    const r1 = await cattura(mf, { ...base("cattura-0006-prova"), extra: "no" });
    assert.equal(r1.status, 422);
    const r2 = await cattura(mf, { ...base("cattura-0007-prova"), valuta: "ZZZ" });
    assert.equal(r2.status, 422);
  });

  it("dopo la sincronizzazione: proposta di abbinamento corretta col movimento bancario sintetico", async () => {
    const mf = await start();
    // carta-demo.csv (dati sintetici): DEMO-0004, "Cinema Esempio", -9.90 EUR, 2026-01-25, pending.
    const { status } = await cattura(mf, {
      id: "cattura-0010-prova",
      importo: "9.90",
      valuta: "EUR",
      esercente: "Cinema Esempio",
      carta: "Apple Pay · Visa",
      istante: "2026-01-25T20:15:00.000Z",
    });
    assert.equal(status, 200);
    assert.equal((await op(mf, "catture"))[0].stato, "provvisoria");

    await cron(mf);

    const list = await op(mf, "catture");
    assert.equal(list[0].stato, "proposta");
    const txnId = list[0].txn_id;
    assert.ok(typeof txnId === "string" && txnId !== "");

    // mai una conferma automatica: resta "proposta" finche' non si chiama conferma_cattura
    const conferma = await op(mf, "conferma_cattura", { id: "cattura-0010-prova" });
    assert.equal(conferma.stato, "abbinata");
  });

  it("abbinamento rifiutato: la cattura torna provvisoria e non si ripropone lo stesso movimento", async () => {
    const mf = await start();
    await cattura(mf, {
      id: "cattura-0011-prova",
      importo: "9.90",
      valuta: "EUR",
      esercente: "Cinema Esempio",
      carta: "Apple Pay · Visa",
      istante: "2026-01-25T20:15:00.000Z",
    });
    await cron(mf);
    const proposta = (await op(mf, "catture"))[0];
    assert.equal(proposta.stato, "proposta");
    await op(mf, "rifiuta_cattura", { id: "cattura-0011-prova", motivo: "non era quella spesa" });
    assert.equal((await op(mf, "catture"))[0].stato, "provvisoria");
    await cron(mf);
    assert.equal(
      (await op(mf, "catture"))[0].stato,
      "provvisoria",
      "il movimento rifiutato non si ripropone",
    );
  });

  it("mai contata due volte nei riepiloghi: una cattura non tocca il riepilogo Finance", async () => {
    const mf = await start();
    const riepilogo = () =>
      op(mf, "consulta", {
        operazione: "summary",
        input: { from: "2020-01-01", to: "2030-01-01" },
        oggi: "2026-06-01",
      });
    const prima = await riepilogo();
    await cattura(mf, base("cattura-0012-prova"));
    assert.deepEqual(await riepilogo(), prima, "la cattura provvisoria resta fuori da transactions");
  });

  it("stato_cattura riporta i contatori correnti", async () => {
    const mf = await start();
    await cattura(mf, base("cattura-0009-prova"));
    const st = await op(mf, "stato_cattura");
    assert.equal(st.sospeso, false);
    assert.equal(st.al_minuto, 1);
    assert.equal(st.al_giorno, 1);
  });
});
