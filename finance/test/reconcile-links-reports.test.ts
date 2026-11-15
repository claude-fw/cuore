import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { stmt } from "../src/db/types.ts";
import { addManualBalance } from "../src/services/balances.ts";
import { classifyTransactions, publishRuleSet } from "../src/services/classification.ts";
import { decideLink, detectLinks, listLinks } from "../src/services/links.ts";
import {
  addFxRate,
  allocateToGoal,
  createGoal,
  createPlannedPurchase,
  listGoalsWithProgress,
  setPurchaseStatus,
} from "../src/services/planning.ts";
import { reconcileAccount, reconcileObservedPeriods } from "../src/services/reconciliation.ts";
import { liquidity, periodReport } from "../src/services/reports.ts";
import { listReviewItems } from "../src/services/review.ts";
import { count, importBalances, importTx, row, setupWithAccounts, type TestEnv } from "./helpers.ts";

const example = (name: string) => new URL(`../examples/sintetici/${name}`, import.meta.url);
const DEMO_RULES = JSON.parse(readFileSync(example("regole-demo.json"), "utf8")) as unknown;

const idOf = async (env: TestEnv, description: string) =>
  (await env.db.first<{ id: string }>("SELECT id FROM transactions WHERE description = ?", [description]))
    ?.id ?? "";

describe("riconciliazione", () => {
  const LINES = [
    row("2026-01-02", "1200.00", "Stipendio"),
    row("2026-01-03", "-45.20", "Spesa", { booking: "2026-01-04" }),
    row("2026-01-15", "-25.00", "Pending esclusa", { status: "pending" }),
  ];

  it("saldi coerenti con i movimenti: ok; le pending non contano", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", LINES);
    await importBalances(env, "conto", ["2026-01-01,500.00,EUR", "2026-01-31,1654.80,EUR"]);
    const r = await reconcileAccount(env.ctx, "conto", "2026-01-01", "2026-01-31");
    assert.equal(r.status, "ok");
    assert.equal(r.difference, 0);
    assert.equal(r.pendingInPeriod, 1);
    assert.equal(await count(env.db, "reconciliations"), 1);
  });

  it("differenza: registrata e messa in verifica; corretta la causa, la verifica si chiude", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", LINES);
    await importBalances(env, "conto", ["2026-01-01,500.00,EUR", "2026-01-31,1650.00,EUR"]);
    const r = await reconcileAccount(env.ctx, "conto", "2026-01-01", "2026-01-31");
    assert.equal(r.status, "difference");
    assert.equal(r.difference, 480); // manca un movimento di -4.80
    assert.ok((await listReviewItems(env.ctx)).some((i) => i.kind === "reconciliation_difference"));
    await importTx(env, "conto", [row("2026-01-20", "-4.80", "Movimento mancante")]);
    const again = await reconcileAccount(env.ctx, "conto", "2026-01-01", "2026-01-31");
    assert.equal(again.status, "ok");
    assert.ok(!(await listReviewItems(env.ctx)).some((i) => i.kind === "reconciliation_difference"));
    assert.equal(await count(env.db, "reconciliations"), 2, "lo storico delle riconciliazioni resta");
  });

  it("dati insufficienti e saldi in conflitto per la stessa data", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", LINES);
    assert.equal(
      (await reconcileAccount(env.ctx, "conto", "2026-01-01", "2026-01-31")).status,
      "insufficient_data",
    );
    await importBalances(env, "conto", ["2026-01-01,500.00,EUR", "2026-01-31,1654.80,EUR"]);
    const manual = await addManualBalance(env.ctx, "conto", "2026-01-31", 165000);
    assert.equal(manual.created, true);
    assert.ok((await listReviewItems(env.ctx)).some((i) => i.kind === "balance_conflict"));
    assert.equal((await reconcileAccount(env.ctx, "conto", "2026-01-01", "2026-01-31")).status, "conflict");
    // reinserire lo stesso saldo manuale non duplica
    assert.equal((await addManualBalance(env.ctx, "conto", "2026-01-31", 165000)).created, false);
  });

  it("riconciliazione di tutti i periodi osservati", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", LINES);
    await importBalances(env, "conto", [
      "2026-01-01,500.00,EUR",
      "2026-01-02,1700.00,EUR",
      "2026-01-31,1654.80,EUR",
    ]);
    const all = await reconcileObservedPeriods(env.ctx, "conto");
    assert.deepEqual(
      all.map((r) => [r.fromDate, r.toDate, r.status]),
      [
        ["2026-01-01", "2026-01-02", "ok"],
        ["2026-01-02", "2026-01-31", "ok"],
      ],
    );
  });
});

describe("trasferimenti, rimborsi e riepilogo di periodo (scenario sintetico completo)", () => {
  const load = async () => {
    const env = await setupWithAccounts();
    const read = (n: string) => readFileSync(example(n));
    const { importFile } = await import("../src/import/importer.ts");
    await importFile(env.ctx, {
      accountId: "conto",
      parserId: "cloud-finance-csv",
      fileName: "c",
      content: read("conto-demo.csv"),
    });
    await importFile(env.ctx, {
      accountId: "carta",
      parserId: "cloud-finance-csv",
      fileName: "k",
      content: read("carta-demo.csv"),
    });
    await importFile(env.ctx, {
      accountId: "conto",
      parserId: "cloud-finance-balances-csv",
      fileName: "s",
      content: read("saldi-conto-demo.csv"),
    });
    await publishRuleSet(env.ctx, DEMO_RULES);
    await classifyTransactions(env.ctx);
    return env;
  };

  it("propone il trasferimento tra conti propri e il rimborso, solo come proposte", async () => {
    const env = await load();
    const d = await detectLinks(env.ctx);
    assert.deepEqual(d, { transfersProposed: 1, refundsProposed: 1, ambiguous: 0 });
    const proposed = await listLinks(env.ctx, "proposed");
    assert.deepEqual(proposed.map((l) => l.type).sort(), ["refund", "transfer"]);
    // rilevare di nuovo non duplica
    assert.deepEqual(await detectLinks(env.ctx), { transfersProposed: 0, refundsProposed: 0, ambiguous: 0 });
  });

  it("non abbina valute diverse ne' importi diversi; un rifiuto non viene riproposto", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", [
      row("2026-01-07", "-100.00", "Verso conto USD"),
      row("2026-01-07", "-50.00", "Uscita"),
    ]);
    await importTx(env, "usd", [row("2026-01-07", "100.00", "Da conto EUR", { currency: "USD" })]);
    await importTx(env, "carta", [row("2026-01-08", "50.00", "Entrata", { id: "K1" })]);
    const d = await detectLinks(env.ctx);
    assert.equal(d.transfersProposed, 1, "solo la coppia in EUR");
    const [link] = await listLinks(env.ctx, "proposed");
    assert.ok(link);
    await decideLink(env.ctx, link.id, "rejected");
    assert.equal((await detectLinks(env.ctx)).transfersProposed, 0);
    await assert.rejects(decideLink(env.ctx, link.id, "confirmed"), /gia' deciso/);
    await assert.rejects(
      env.db.batch([stmt("UPDATE transaction_links SET status = 'confirmed'")]),
      /una sola volta/,
    );
  });

  it("trasferimenti ambigui: nessuna proposta, solo verifica", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", [
      row("2026-01-07", "-20.00", "Uscita A"),
      row("2026-01-07", "-20.00", "Uscita B"),
    ]);
    await importTx(env, "carta", [row("2026-01-07", "20.00", "Entrata", { id: "K1" })]);
    const d = await detectLinks(env.ctx);
    assert.equal(d.transfersProposed, 0);
    assert.equal(d.ambiguous, 2);
  });

  it("dopo le conferme: trasferimenti esclusi, rimborso a riduzione della spesa, pending a parte", async () => {
    const env = await load();
    await detectLinks(env.ctx);
    for (const l of await listLinks(env.ctx, "proposed")) await decideLink(env.ctx, l.id, "confirmed");
    const report = await periodReport(env.ctx, "2026-01-01", "2026-01-31");
    const [eur] = report.currencies;
    assert.ok(eur);
    assert.equal(report.currencies.length, 1);
    assert.equal(eur.income, 120000);
    assert.equal(eur.transfersIn, 20000);
    assert.equal(eur.transfersOut, 20000);
    assert.equal(eur.refunds, 2765);
    assert.equal(eur.fees, 100);
    // spese: supermercato 45.20 + abbonamento 12.99 + libreria 27.65; i due caffe' sono proposte a bassa
    // confidenza: restano "da classificare", non entrano tra le spese
    assert.equal(eur.expenses, 4520 + 1299 + 2765);
    assert.equal(eur.spendingByCategory.libri, 2765);
    assert.equal(eur.spendingByCategory["bar-ristoranti"], undefined);
    assert.equal(eur.unclassifiedCount, 2);
    assert.equal(eur.unclassifiedOut, 700);
    assert.equal(eur.pendingCount, 2);
    assert.equal(eur.pendingSum, -2500 - 990);
    const refund = await env.db.first<{ kind: string; category_id: string }>(
      "SELECT kind, category_id FROM current_classifications WHERE transaction_id = ?",
      [await idOf(env, "Rimborso Libreria online DEMO")],
    );
    assert.deepEqual({ ...refund }, { kind: "refund", category_id: "libri" });
    const rec = await reconcileAccount(env.ctx, "conto", "2026-01-01", "2026-01-31");
    assert.equal(rec.status, "ok");
  });

  it("doppione confermato: escluso da riepilogo e riconciliazione, mai cancellato", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", [row("2026-01-05", "-3.50", "Caffe")]);
    await importTx(env, "conto", [row("2026-01-05", "-3.50", "CAFFE BAR ESEMPIO")]);
    const [dup] = await listLinks(env.ctx, "proposed");
    assert.ok(dup);
    await decideLink(env.ctx, dup.id, "confirmed");
    await importBalances(env, "conto", ["2026-01-01,10.00,EUR", "2026-01-31,6.50,EUR"]);
    assert.equal((await reconcileAccount(env.ctx, "conto", "2026-01-01", "2026-01-31")).status, "ok");
    const report = await periodReport(env.ctx, "2026-01-01", "2026-01-31");
    assert.equal(report.excludedDuplicates, 1);
    assert.equal(report.currencies[0]?.unclassifiedOut, 350);
    assert.equal(await count(env.db, "transactions"), 2);
  });
});

describe("pianificazione e liquidita'", () => {
  it("obiettivi con accantonamenti append-only", async () => {
    const env = await setupWithAccounts();
    const g = await createGoal(env.ctx, { name: "Fondo DEMO", targetMinor: 50000, currency: "EUR" });
    await allocateToGoal(env.ctx, g.id, 20000, "2026-01-10");
    await allocateToGoal(env.ctx, g.id, -5000, "2026-01-12", "correzione");
    const [withProgress] = await listGoalsWithProgress(env.ctx);
    assert.deepEqual(withProgress?.progress, {
      allocated: 15000,
      remaining: 35000,
      progressBp: 3000,
      reached: false,
    });
    await assert.rejects(env.db.batch([stmt("DELETE FROM goal_allocations")]), /immutabili/);
    await assert.rejects(allocateToGoal(env.ctx, g.id, 0, "2026-01-12"), /nullo/);
  });

  it("acquisto pianificato: diventa acquistato solo con una transazione reale coerente", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", [
      row("2026-01-09", "-29.90", "Negozio DEMO"),
      row("2026-01-09", "10.00", "Entrata"),
    ]);
    const p = await createPlannedPurchase(env.ctx, {
      title: "Oggetto DEMO",
      currency: "EUR",
      estimatedMinor: 3000,
      priority: 2,
    });
    assert.equal(p.notion_block_id, null, "colonna storica: nessun riferimento Notion scritto");
    await assert.rejects(setPurchaseStatus(env.ctx, p.id, "purchased"), /transazione reale/);
    await assert.rejects(setPurchaseStatus(env.ctx, p.id, "purchased", await idOf(env, "Entrata")), /uscita/);
    await setPurchaseStatus(env.ctx, p.id, "planned");
    await setPurchaseStatus(env.ctx, p.id, "purchased", await idOf(env, "Negozio DEMO"));
    await assert.rejects(setPurchaseStatus(env.ctx, p.id, "idea"), /non ammesso/);
    await assert.rejects(env.db.batch([stmt("DELETE FROM planned_purchases")]), /nessuna cancellazione/);
  });

  it("liquidita': ultimo saldo osservato, conversione marcata come stima, conflitti visibili", async () => {
    const env = await setupWithAccounts();
    await addManualBalance(env.ctx, "conto", "2026-01-31", 100000);
    await addManualBalance(env.ctx, "usd", "2026-01-30", 10000);
    await addFxRate(env.ctx, "2026-01-30", "USD", "EUR", "0.9234", "inserimento manuale DEMO");
    const lines = await liquidity(env.ctx, "2026-01-31", "EUR");
    const usd = lines.find((l) => l.accountId === "usd");
    assert.deepEqual(usd?.converted, {
      currency: "EUR",
      amountMinor: 9234,
      rate: "0.9234",
      rateDate: "2026-01-30",
      estimated: true,
    });
    assert.equal(lines.find((l) => l.accountId === "carta")?.certainty, "missing");
    await addManualBalance(env.ctx, "conto", "2026-01-31", 99000);
    assert.equal(
      (await liquidity(env.ctx, "2026-01-31")).find((l) => l.accountId === "conto")?.certainty,
      "conflict",
    );
  });
});
