/**
 * Crediti e debiti (schema v5) con dati SINTETICI: dominio puro, servizi, riepiloghi, collegamenti bancari.
 * Gira sia su SQLite via Node sia su D1 locale (CLOUD_FINANCE_TEST_DB=d1).
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { migrate } from "../src/db/migrate.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import { stmt } from "../src/db/types.ts";
import {
  expectedTransactionSign,
  obligationState,
  obligationsPosition,
  residualAfter,
} from "../src/domain/obligations.ts";
import { createAccount } from "../src/services/accounts.ts";
import { addManualBalance } from "../src/services/balances.ts";
import { decideLink, detectLinks, listLinks } from "../src/services/links.ts";
import {
  createCounterparty,
  createObligation,
  linkTransactionToObligations,
  listObligations,
  obligationsPositionAt,
  recordObligationEvent,
} from "../src/services/obligations.ts";
import { reconcileAccount } from "../src/services/reconciliation.ts";
import { periodReport } from "../src/services/reports.ts";
import {
  count,
  FailingDb,
  freshDb,
  importTx,
  integrityCheck,
  row,
  setupWithAccounts,
  type TestEnv,
  testCtx,
} from "./helpers.ts";

const txnId = async (env: TestEnv, description: string) =>
  (await env.db.first<{ id: string }>("SELECT id FROM transactions WHERE description = ?", [description]))
    ?.id ?? "";
const eur = async (env: TestEnv) =>
  (await periodReport(env.ctx, "2026-01-01", "2026-12-31")).currencies.find((c) => c.currency === "EUR");
const residualOf = async (env: TestEnv, id: string, asOf = "2026-12-31") =>
  (await listObligations(env.ctx, asOf)).find((o) => o.id === id)?.state;

async function withPeople(): Promise<TestEnv> {
  const env = await setupWithAccounts();
  await createCounterparty(env.ctx, { id: "sorella-demo", name: "Sorella DEMO" });
  await createCounterparty(env.ctx, { id: "amico-demo", name: "Amico DEMO" });
  return env;
}

describe("crediti e debiti: dominio puro", () => {
  const ev = (
    kind: "open" | "increase" | "payment" | "forgive" | "adjust",
    amountMinor: number,
    occurredOn = "2026-01-10",
  ) => ({
    kind,
    amountMinor,
    occurredOn,
  });

  it("residuo = apertura + aumenti - pagamenti - condoni +/- rettifiche; chiuso a zero; scadenza superata", () => {
    const s = obligationState(
      [ev("open", 5000), ev("increase", 1000), ev("payment", 2000), ev("forgive", 500), ev("adjust", -500)],
      "2026-02-01",
      "2026-03-01",
    );
    assert.deepEqual(
      [s.initial, s.increased, s.paid, s.forgiven, s.adjusted, s.residual, s.status, s.overdue],
      [5000, 1000, 2000, 500, -500, 3000, "open", true],
    );
    const closed = obligationState([ev("open", 5000), ev("payment", 5000)], "2026-02-01", "2026-03-01");
    assert.deepEqual(
      [closed.residual, closed.status, closed.overdue],
      [0, "closed", false],
      "chiuso non e' mai scaduto",
    );
    assert.throws(() => obligationState([ev("payment", 10)], null, "2026-03-01"), /apertura/);
    assert.throws(() => obligationState([ev("open", 10), ev("open", 10)], null, "2026-03-01"), /apertura/);
    assert.throws(() => obligationState([ev("open", 10), ev("payment", 20)], null, "2026-03-01"), /negativo/);
  });

  it("nessun pagamento in eccesso; segni attesi dei movimenti; posizione per valuta senza sommare valute", () => {
    assert.equal(residualAfter(3000, "payment", 3000), 0);
    assert.equal(residualAfter(3000, "payment", 3001), null);
    assert.equal(residualAfter(3000, "adjust", -3001), null);
    assert.equal(residualAfter(3000, "increase", 1), 3001);
    assert.equal(expectedTransactionSign("receivable", "open"), -1, "credito: ho dato denaro");
    assert.equal(expectedTransactionSign("receivable", "payment"), 1, "credito: mi restituiscono");
    assert.equal(expectedTransactionSign("payable", "open"), 1, "debito: ho ricevuto denaro");
    assert.equal(expectedTransactionSign("payable", "payment"), -1, "debito: restituisco");
    const st = (residual: number) => ({
      ...obligationState(
        [ev("open", residual || 1), ...(residual ? [] : [ev("payment", 1)])],
        null,
        "2026-03-01",
      ),
    });
    assert.deepEqual(
      obligationsPosition([
        { direction: "receivable", currency: "EUR", state: st(5000) },
        { direction: "payable", currency: "EUR", state: st(2000) },
        { direction: "receivable", currency: "USD", state: st(700) },
        { direction: "payable", currency: "EUR", state: st(0) },
      ]),
      [
        { currency: "EUR", receivable: 5000, payable: 2000, net: 3000, openCount: 2, overdueCount: 0 },
        { currency: "USD", receivable: 700, payable: 0, net: 700, openCount: 1, overdueCount: 0 },
      ],
    );
  });
});

describe("crediti e debiti senza movimenti bancari", () => {
  it("creare un credito NON crea entrate, spese o transazioni; pagamenti parziali fino alla chiusura", async () => {
    const env = await withPeople();
    const o = await createObligation(env.ctx, {
      counterpartyId: "sorella-demo",
      direction: "payable",
      currency: "EUR",
      amountMinor: 5000,
      openedOn: "2026-01-05",
      dueOn: "2026-03-31",
      description: "Prestito ricevuto DEMO",
    });
    assert.equal(await count(env.db, "transactions"), 0);
    assert.equal(
      (await periodReport(env.ctx, "2026-01-01", "2026-12-31")).currencies.length,
      0,
      "nessuna cassa",
    );
    await recordObligationEvent(env.ctx, o.id, {
      kind: "payment",
      amountMinor: 2000,
      occurredOn: "2026-02-01",
      note: "contanti",
    });
    assert.deepEqual(
      [(await residualOf(env, o.id))?.residual, (await residualOf(env, o.id))?.status],
      [3000, "open"],
    );
    await assert.rejects(
      recordObligationEvent(env.ctx, o.id, { kind: "payment", amountMinor: 3001, occurredOn: "2026-02-10" }),
      /supera il residuo/,
    );
    await recordObligationEvent(env.ctx, o.id, {
      kind: "payment",
      amountMinor: 3000,
      occurredOn: "2026-02-10",
    });
    const s = await residualOf(env, o.id);
    assert.deepEqual([s?.residual, s?.status, s?.paid], [0, "closed", 5000]);
    // posizione alla data: prima del secondo pagamento il residuo era 3000 e la scadenza non ancora passata
    const feb5 = await residualOf(env, o.id, "2026-02-05");
    assert.deepEqual([feb5?.residual, feb5?.overdue], [3000, false]);
    const apr = (await obligationsPositionAt(env.ctx, "2026-04-01"))[0];
    assert.deepEqual([apr?.payable, apr?.openCount, apr?.overdueCount], [0, 0, 0]);
    assert.ok((await count(env.db, "audit_log", "entity = 'obligation'")) >= 3);
  });

  it("condono, aumento, rettifica motivata; ordine cronologico; validazioni", async () => {
    const env = await withPeople();
    const o = await createObligation(env.ctx, {
      counterpartyId: "amico-demo",
      direction: "receivable",
      currency: "EUR",
      amountMinor: 1000,
      openedOn: "2026-01-05",
      description: "Pizza anticipata DEMO",
    });
    await recordObligationEvent(env.ctx, o.id, {
      kind: "increase",
      amountMinor: 500,
      occurredOn: "2026-01-06",
    });
    await assert.rejects(
      recordObligationEvent(env.ctx, o.id, { kind: "payment", amountMinor: 100, occurredOn: "2026-01-05" }),
      /precedente all'ultimo evento/,
    );
    await assert.rejects(
      recordObligationEvent(env.ctx, o.id, { kind: "adjust", amountMinor: -100, occurredOn: "2026-01-07" }),
      /motivazione/,
    );
    await recordObligationEvent(env.ctx, o.id, {
      kind: "adjust",
      amountMinor: -100,
      occurredOn: "2026-01-07",
      note: "importo ricordato male",
    });
    await recordObligationEvent(env.ctx, o.id, {
      kind: "forgive",
      amountMinor: 1400,
      occurredOn: "2026-01-08",
      note: "regalo",
    });
    assert.equal((await residualOf(env, o.id))?.status, "closed");
    await assert.rejects(
      recordObligationEvent(env.ctx, o.id, { kind: "forgive", amountMinor: 1, occurredOn: "2026-01-09" }),
      /supera il residuo/,
    );
    await assert.rejects(
      createObligation(env.ctx, {
        counterpartyId: "nessuno",
        direction: "receivable",
        currency: "EUR",
        amountMinor: 1,
        openedOn: "2026-01-01",
        description: "x",
      }),
      /controparte non trovato/,
    );
    await assert.rejects(
      createObligation(env.ctx, {
        counterpartyId: "amico-demo",
        direction: "receivable",
        currency: "EUR",
        amountMinor: 1,
        openedOn: "2026-02-01",
        dueOn: "2026-01-01",
        description: "x",
      }),
      /scadenza precedente/,
    );
    await assert.rejects(
      createObligation(env.ctx, {
        counterpartyId: "amico-demo",
        direction: "receivable",
        currency: "EUR",
        amountMinor: 0,
        openedOn: "2026-02-01",
        description: "x",
      }),
      /positivo/,
    );
  });

  it("doppioni: apertura o evento identici rifiutati salvo conferma esplicita", async () => {
    const env = await withPeople();
    const input = {
      counterpartyId: "amico-demo",
      direction: "receivable" as const,
      currency: "EUR",
      amountMinor: 1000,
      openedOn: "2026-01-05",
      description: "Biglietto DEMO",
    };
    const o = await createObligation(env.ctx, input);
    await assert.rejects(createObligation(env.ctx, input), /possibile doppione/);
    await createObligation(env.ctx, { ...input, confirmDuplicate: true });
    await recordObligationEvent(env.ctx, o.id, {
      kind: "payment",
      amountMinor: 200,
      occurredOn: "2026-01-06",
    });
    await assert.rejects(
      recordObligationEvent(env.ctx, o.id, { kind: "payment", amountMinor: 200, occurredOn: "2026-01-06" }),
      /possibile doppione/,
    );
    await recordObligationEvent(env.ctx, o.id, {
      kind: "payment",
      amountMinor: 200,
      occurredOn: "2026-01-06",
      confirmDuplicate: true,
    });
    assert.equal((await residualOf(env, o.id))?.residual, 600);
    assert.equal(await count(env.db, "obligations"), 2);
  });

  it("storico immutabile: obbligazioni ed eventi non si modificano ne' si cancellano", async () => {
    const env = await withPeople();
    await createObligation(env.ctx, {
      counterpartyId: "amico-demo",
      direction: "receivable",
      currency: "EUR",
      amountMinor: 100,
      openedOn: "2026-01-05",
      description: "x",
    });
    for (const sql of [
      "UPDATE obligation_events SET amount_minor = 1",
      "DELETE FROM obligation_events",
      "UPDATE obligations SET currency = 'USD'",
      "DELETE FROM obligations",
      "DELETE FROM counterparties",
    ]) {
      await assert.rejects(env.db.batch([stmt(sql)]), /immutabil/, sql);
    }
  });
});

describe("crediti e debiti con movimenti bancari (collegamento verificato, mai automatico)", () => {
  const LINES = [
    row("2026-01-10", "-50.00", "Bonifico a sorella DEMO"),
    row("2026-02-10", "30.00", "Restituzione sorella DEMO"),
    row("2026-02-15", "75.00", "Restituzione amico DEMO"),
    row("2026-02-20", "-60.00", "Bonifico misto DEMO"),
    row("2026-03-01", "100.00", "Prestito da amico DEMO"),
    row("2026-03-05", "-40.00", "Rata ad amico DEMO"),
  ];

  it("prestito dato e restituzione parziale: mai entrate ne' spese; riepilogo senza doppi conteggi; riconciliazione invariata", async () => {
    const env = await withPeople();
    await importTx(env, "conto", LINES.slice(0, 2));
    const before = await eur(env);
    assert.deepEqual(
      [before?.unclassifiedOut, before?.unclassifiedIn],
      [5000, 3000],
      "prima del collegamento: da classificare",
    );
    const o = await createObligation(env.ctx, {
      counterpartyId: "sorella-demo",
      direction: "receivable",
      currency: "EUR",
      amountMinor: 5000,
      openedOn: "2026-01-10",
      description: "Prestito a sorella DEMO",
      transactionId: await txnId(env, "Bonifico a sorella DEMO"),
    });
    await linkTransactionToObligations(env.ctx, await txnId(env, "Restituzione sorella DEMO"), [
      { obligationId: o.id, kind: "payment", amountMinor: 3000 },
    ]);
    const after = await eur(env);
    assert.deepEqual(
      [
        after?.income,
        after?.expenses,
        after?.net,
        after?.obligationsOut,
        after?.obligationsIn,
        after?.unclassifiedCount,
      ],
      [0, 0, 0, 5000, 3000, 0],
    );
    assert.equal((await residualOf(env, o.id))?.residual, 2000);
    // la cassa reale non cambia: la riconciliazione conta comunque i movimenti bancari
    await addManualBalance(env.ctx, "conto", "2026-01-01", 10000);
    await addManualBalance(env.ctx, "conto", "2026-02-28", 8000);
    assert.equal((await reconcileAccount(env.ctx, "conto", "2026-01-01", "2026-02-28")).status, "ok");
  });

  it("un movimento ripaga due crediti; quote non pari al movimento, segno o valuta sbagliati, pagamento in eccesso: rifiuto senza scritture", async () => {
    const env = await withPeople();
    await importTx(env, "conto", LINES.slice(0, 4));
    await importTx(env, "usd", [row("2026-02-20", "-10.00", "Spesa USD DEMO", { currency: "USD" })]);
    const a = await createObligation(env.ctx, {
      counterpartyId: "sorella-demo",
      direction: "receivable",
      currency: "EUR",
      amountMinor: 5000,
      openedOn: "2026-01-01",
      description: "A",
    });
    const b = await createObligation(env.ctx, {
      counterpartyId: "amico-demo",
      direction: "receivable",
      currency: "EUR",
      amountMinor: 2500,
      openedOn: "2026-01-01",
      description: "B",
    });
    const settantacinque = await txnId(env, "Restituzione amico DEMO");
    const eventsBefore = await count(env.db, "obligation_events");
    await assert.rejects(
      linkTransactionToObligations(env.ctx, settantacinque, [
        { obligationId: a.id, kind: "payment", amountMinor: 5000 },
      ]),
      /nessun collegamento parziale/,
    );
    await assert.rejects(
      linkTransactionToObligations(env.ctx, settantacinque, [
        { obligationId: b.id, kind: "payment", amountMinor: 7500 },
      ]),
      /supera il residuo/,
    );
    await assert.rejects(
      linkTransactionToObligations(env.ctx, await txnId(env, "Bonifico misto DEMO"), [
        { obligationId: a.id, kind: "payment", amountMinor: 6000 },
      ]),
      /segno del movimento incoerente/,
    );
    await assert.rejects(
      linkTransactionToObligations(env.ctx, await txnId(env, "Spesa USD DEMO"), [
        { obligationId: a.id, kind: "increase", amountMinor: 1000 },
      ]),
      /valuta diversa/,
    );
    await assert.rejects(
      linkTransactionToObligations(env.ctx, settantacinque, [
        { obligationId: a.id, kind: "payment", amountMinor: 4000 },
        { obligationId: a.id, kind: "payment", amountMinor: 4000 },
      ]),
      /piu' volte/,
    );
    assert.equal(await count(env.db, "obligation_events"), eventsBefore, "nessuna scrittura dopo i rifiuti");
    await linkTransactionToObligations(env.ctx, settantacinque, [
      { obligationId: a.id, kind: "payment", amountMinor: 5000 },
      { obligationId: b.id, kind: "payment", amountMinor: 2500 },
    ]);
    assert.deepEqual(
      [(await residualOf(env, a.id))?.status, (await residualOf(env, b.id))?.status],
      ["closed", "closed"],
    );
    await assert.rejects(
      linkTransactionToObligations(env.ctx, settantacinque, [
        { obligationId: a.id, kind: "increase", amountMinor: 7500 },
      ]),
      /gia' collegato/,
      "un movimento si collega una sola volta",
    );
  });

  it("debito ricevuto via banca e rata: posizione netta crediti - debiti", async () => {
    const env = await withPeople();
    await importTx(env, "conto", LINES.slice(4));
    const d = await createObligation(env.ctx, {
      counterpartyId: "amico-demo",
      direction: "payable",
      currency: "EUR",
      amountMinor: 10000,
      openedOn: "2026-03-01",
      description: "Prestito da amico DEMO",
      transactionId: await txnId(env, "Prestito da amico DEMO"),
    });
    await linkTransactionToObligations(env.ctx, await txnId(env, "Rata ad amico DEMO"), [
      { obligationId: d.id, kind: "payment", amountMinor: 4000 },
    ]);
    await createObligation(env.ctx, {
      counterpartyId: "sorella-demo",
      direction: "receivable",
      currency: "EUR",
      amountMinor: 1500,
      openedOn: "2026-03-02",
      description: "Cena DEMO",
    });
    assert.deepEqual((await obligationsPositionAt(env.ctx, "2026-03-31"))[0], {
      currency: "EUR",
      receivable: 1500,
      payable: 6000,
      net: -4500,
      openCount: 2,
      overdueCount: 0,
    });
    const r = await eur(env);
    assert.deepEqual([r?.income, r?.expenses, r?.obligationsIn, r?.obligationsOut], [0, 0, 10000, 4000]);
    await assert.rejects(
      createObligation(env.ctx, {
        counterpartyId: "amico-demo",
        direction: "payable",
        currency: "EUR",
        amountMinor: 4000,
        openedOn: "2026-03-05",
        description: "x",
        transactionId: await txnId(env, "Rata ad amico DEMO"),
      }),
      /gia' collegato/,
    );
  });

  it("movimenti non collegabili: pending, trasferimento proposto; un movimento collegato non diventa trasferimento", async () => {
    const env = await withPeople();
    await importTx(env, "conto", [
      row("2026-01-10", "-25.00", "Pending DEMO", { status: "pending" }),
      row("2026-01-12", "-20.00", "Uscita DEMO"),
    ]);
    await importTx(env, "carta", [row("2026-01-12", "20.00", "Entrata DEMO", { id: "K1" })]);
    const o = await createObligation(env.ctx, {
      counterpartyId: "amico-demo",
      direction: "receivable",
      currency: "EUR",
      amountMinor: 9999,
      openedOn: "2026-01-01",
      description: "x",
    });
    await assert.rejects(
      linkTransactionToObligations(env.ctx, await txnId(env, "Pending DEMO"), [
        { obligationId: o.id, kind: "increase", amountMinor: 2500 },
      ]),
      /contabilizzati/,
    );
    assert.equal((await detectLinks(env.ctx)).transfersProposed, 1);
    await assert.rejects(
      linkTransactionToObligations(env.ctx, await txnId(env, "Uscita DEMO"), [
        { obligationId: o.id, kind: "increase", amountMinor: 2000 },
      ]),
      /transfer proposto/,
    );
    // collegato prima che il trasferimento venga confermato: la conferma e' rifiutata
    const env2 = await withPeople();
    await importTx(env2, "conto", [row("2026-01-12", "-20.00", "Uscita DEMO")]);
    await importTx(env2, "carta", [row("2026-01-12", "20.00", "Entrata DEMO", { id: "K1" })]);
    const o2 = await createObligation(env2.ctx, {
      counterpartyId: "amico-demo",
      direction: "receivable",
      currency: "EUR",
      amountMinor: 2000,
      openedOn: "2026-01-12",
      description: "x",
      transactionId: await txnId(env2, "Uscita DEMO"),
    });
    assert.ok(o2.id);
    assert.equal((await detectLinks(env2.ctx)).transfersProposed, 0, "mai riproposto come trasferimento");
    await env2.db.batch([
      stmt(
        "INSERT INTO transaction_links (id, type, from_txn_id, to_txn_id, status, origin, created_at) VALUES ('forzato', 'transfer', ?, ?, 'proposed', 'user', 't')",
        await txnId(env2, "Uscita DEMO"),
        await txnId(env2, "Entrata DEMO"),
      ),
    ]);
    const [forced] = await listLinks(env2.ctx, "proposed");
    assert.ok(forced);
    await assert.rejects(decideLink(env2.ctx, forced.id, "confirmed"), /gia' collegato a crediti\/debiti/);
  });

  it("errore di scrittura durante il collegamento: nessun evento scritto (rollback)", async () => {
    const env = await withPeople();
    await importTx(env, "conto", LINES.slice(2, 3));
    const a = await createObligation(env.ctx, {
      counterpartyId: "sorella-demo",
      direction: "receivable",
      currency: "EUR",
      amountMinor: 5000,
      openedOn: "2026-01-01",
      description: "A",
    });
    const b = await createObligation(env.ctx, {
      counterpartyId: "amico-demo",
      direction: "receivable",
      currency: "EUR",
      amountMinor: 2500,
      openedOn: "2026-01-01",
      description: "B",
    });
    const before = await count(env.db, "obligation_events");
    const ctx = { ...env.ctx, db: new FailingDb(env.db, 5) };
    await assert.rejects(
      linkTransactionToObligations(ctx, await txnId(env, "Restituzione amico DEMO"), [
        { obligationId: a.id, kind: "payment", amountMinor: 5000 },
        { obligationId: b.id, kind: "payment", amountMinor: 2500 },
      ]),
    );
    assert.equal(await count(env.db, "obligation_events"), before);
  });
});

describe("compatibilita' con i dati delle versioni precedenti", () => {
  it("database v4 con movimenti: aggiornamento a v5 senza perdite, poi credito sul movimento esistente", async () => {
    const db = await freshDb();
    const t: TestEnv = { db, ctx: testCtx(db) };
    await migrate(db, MIGRATIONS.slice(0, 4), t.ctx.now);
    await createAccount(t.ctx, {
      id: "conto",
      name: "Conto DEMO",
      type: "current",
      currency: "EUR",
      holderRole: "owner",
    });
    await importTx(t, "conto", [
      row("2026-01-10", "-50.00", "Bonifico a sorella DEMO"),
      row("2026-01-11", "-5.00", "Caffe DEMO"),
    ]);
    assert.deepEqual(await migrate(db, MIGRATIONS, t.ctx.now), { from: 4, to: 8, applied: [5, 6, 7, 8] });
    assert.equal(await count(db, "transactions"), 2);
    const up = await eur(t);
    assert.deepEqual(
      [up?.unclassifiedOut, up?.obligationsOut, up?.expenses],
      [5500, 0, 0],
      "movimenti v4 intatti",
    );
    await createCounterparty(t.ctx, { id: "sorella-demo", name: "Sorella DEMO" });
    await createObligation(t.ctx, {
      counterpartyId: "sorella-demo",
      direction: "receivable",
      currency: "EUR",
      amountMinor: 5000,
      openedOn: "2026-01-10",
      description: "Prestito DEMO",
      transactionId: await txnId(t, "Bonifico a sorella DEMO"),
    });
    const after = await eur(t);
    assert.deepEqual([after?.obligationsOut, after?.unclassifiedOut], [5000, 500]);
    assert.equal(await integrityCheck(db), "ok");
  });
});
