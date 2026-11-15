/**
 * Contratto di consultazione v1 (src/query) con dati SINTETICI: filtri, intervalli, valute, risultati vuoti, errori,
 * paginazione, sola lettura e coerenza con i riepiloghi (nessun doppio conteggio).
 * Gira sia su SQLite via Node sia su D1 locale (CLOUD_FINANCE_TEST_DB=d1).
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { migrate } from "../src/db/migrate.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import type { Db } from "../src/db/types.ts";
import { redactIdentifiers } from "../src/domain/redact.ts";
import type {
  BalancesOutput,
  DailyOutput,
  DataStatusOutput,
  ObligationsOutput,
  SummaryOutput,
  TransactionItem,
  TransactionsOutput,
} from "../src/query/consultation.ts";
import { readOnlyDb } from "../src/query/consultation.ts";
import { OPERATIONS, type QueryResponse, runQuery } from "../src/query/operations.ts";
import { createAccount } from "../src/services/accounts.ts";
import { addManualBalance } from "../src/services/balances.ts";
import { classifyTransactions, publishRuleSet } from "../src/services/classification.ts";
import { decideLink, detectLinks, listLinks } from "../src/services/links.ts";
import { createCounterparty, createObligation, recordObligationEvent } from "../src/services/obligations.ts";
import { reconcileAccount } from "../src/services/reconciliation.ts";
import { periodReport } from "../src/services/reports.ts";
import { freshDb, importTx, row, setupWithAccounts, type TestEnv, testCtx } from "./helpers.ts";

const RULES = JSON.parse(
  readFileSync(new URL("../examples/sintetici/regole-demo.json", import.meta.url), "utf8"),
) as unknown;

const txnId = async (env: TestEnv, description: string) =>
  (await env.db.first<{ id: string }>("SELECT id FROM transactions WHERE description = ?", [description]))
    ?.id ?? "";

/** Esegue un'operazione e restituisce i dati; fallisce il test se la risposta e' un errore. */
async function q<T>(db: Db, operation: string, input: unknown = {}, today?: string): Promise<T> {
  const r = await runQuery(db, operation, input, { today });
  if (!r.ok) assert.fail(`${operation}: ${r.error.code} ${r.error.message}`);
  return r.data as T;
}

async function err(
  db: Db,
  operation: string,
  input: unknown = {},
): Promise<{ code: string; message: string }> {
  const r: QueryResponse = await runQuery(db, operation, input);
  assert.equal(r.ok, false, `${operation} doveva fallire`);
  return r.ok ? { code: "", message: "" } : r.error;
}

const BONIFICO = "Bonifico IT60 X054 2811 1010 0000 0123 456 carta 4111111111111111 sorella DEMO";

/**
 * Scenario: stipendio, spese classificate, trasferimento confermato conto -> carta, prestito alla sorella collegato
 * al bonifico, pending, doppione confermato, conto USD senza saldi, saldi conto riconciliati, saldi carta in conflitto.
 */
async function scenario(): Promise<TestEnv> {
  const env = await setupWithAccounts();
  await publishRuleSet(env.ctx, RULES);
  await importTx(env, "conto", [
    row("2026-01-02", "1200.00", "Stipendio DEMO", { counterparty: "Azienda Esempio Srl" }),
    row("2026-01-03", "-45.20", "Spesa supermercato", { counterparty: "Supermercato Esempio" }),
    row("2026-01-05", "-3.50", "Caffe", { counterparty: "Bar Esempio" }),
    row("2026-01-07", "-200.00", "Trasferimento verso carta"),
    row("2026-01-10", "-50.00", BONIFICO),
    row("2026-01-15", "-25.00", "Ristorante Esempio", { status: "pending" }),
  ]);
  await importTx(
    env,
    "conto",
    [row("2026-01-05", "-3.50", "BAR ESEMPIO SRL", { counterparty: "Bar Esempio" })],
    "b.csv",
  );
  await importTx(env, "carta", [row("2026-01-07", "200.00", "Ricarica da conto", { id: "K1" })]);
  await importTx(env, "usd", [row("2026-01-08", "-10.00", "Libro USD DEMO", { currency: "USD" })]);
  await classifyTransactions(env.ctx);
  await detectLinks(env.ctx);
  for (const l of await listLinks(env.ctx, "proposed")) await decideLink(env.ctx, l.id, "confirmed");
  await createCounterparty(env.ctx, { id: "sorella-demo", name: "Sorella DEMO" });
  await createCounterparty(env.ctx, { id: "amico-demo", name: "Amico DEMO" });
  await createObligation(env.ctx, {
    counterpartyId: "sorella-demo",
    direction: "receivable",
    currency: "EUR",
    amountMinor: 5000,
    openedOn: "2026-01-10",
    dueOn: "2026-02-15",
    description: "Prestito a sorella DEMO",
    transactionId: await txnId(env, BONIFICO),
  });
  await createObligation(env.ctx, {
    counterpartyId: "amico-demo",
    direction: "payable",
    currency: "EUR",
    amountMinor: 2000,
    openedOn: "2026-01-12",
    description: "Cena pagata da amico DEMO",
  });
  const closed = await createObligation(env.ctx, {
    counterpartyId: "amico-demo",
    direction: "payable",
    currency: "USD",
    amountMinor: 700,
    openedOn: "2026-01-02",
    description: "Biglietto DEMO",
  });
  await recordObligationEvent(env.ctx, closed.id, {
    kind: "payment",
    amountMinor: 700,
    occurredOn: "2026-01-04",
  });
  // conto: 1000,00 al 1/1; +1200 -45,20 -3,50 -200 -50 = 1901,30 al 31/1 (pending e doppione esclusi)
  await addManualBalance(env.ctx, "conto", "2026-01-01", 100_000);
  await addManualBalance(env.ctx, "conto", "2026-01-31", 190_130);
  assert.equal((await reconcileAccount(env.ctx, "conto", "2026-01-01", "2026-01-31")).status, "ok");
  await addManualBalance(env.ctx, "carta", "2026-01-31", 20_000);
  await addManualBalance(env.ctx, "carta", "2026-01-31", 19_000);
  return env;
}

async function allPages(db: Db, input: Record<string, unknown>): Promise<TransactionItem[]> {
  const items: TransactionItem[] = [];
  let cursor: string | undefined;
  for (let i = 0; i < 50; i += 1) {
    const page = await q<TransactionsOutput>(db, "transactions", { ...input, ...(cursor ? { cursor } : {}) });
    items.push(...page.items);
    if (page.nextCursor === null) return items;
    cursor = page.nextCursor;
  }
  assert.fail("paginazione senza fine");
}

describe("consultazione: contratto, errori, sola lettura", () => {
  it("operazioni elencate con input descritto; operazione o campi sconosciuti, input non oggetto: errori a codice", async () => {
    const { db } = await setupWithAccounts();
    assert.deepEqual(
      OPERATIONS.map((o) => o.name),
      [
        "accounts",
        "categories",
        "planning",
        "summary",
        "daily",
        "monthly",
        "balances",
        "transactions",
        "obligations",
        "data_status",
        "overview",
        "manual",
        "budgets",
        "search",
      ],
    );
    for (const o of OPERATIONS) assert.ok(o.description.length > 20, o.name);
    assert.equal((await err(db, "delete_all")).code, "unknown_operation");
    assert.equal((await err(db, "accounts", [])).code, "invalid_input");
    assert.match(
      (await err(db, "summary", { from: "2026-01-01", to: "2026-01-31", acount: "x" })).message,
      /acount/,
    );
    const bad: [string, unknown][] = [
      ["summary", { from: "2026-01-01" }],
      ["summary", { from: "2026-02-01", to: "2026-01-01" }],
      ["summary", { from: "2026-02-30", to: "2026-03-01" }],
      ["summary", { from: "2026-01-01", to: "2026-01-31", currency: "euro" }],
      ["summary", { from: "2026-01-01", to: "2026-01-31", accountIds: "conto" }],
      [
        "summary",
        { from: "2026-01-01", to: "2026-01-31", accountIds: Array.from({ length: 21 }, (_, i) => `c${i}`) },
      ],
      ["balances", {}],
      ["daily", { from: "2026-01-01" }],
      ["daily", { from: "2026-01-01", to: "2026-06-30" }],
      ["transactions", { from: "2026-01-01", to: "2026-01-31", limit: 0 }],
      ["transactions", { from: "2026-01-01", to: "2026-01-31", limit: 201 }],
      ["transactions", { from: "2026-01-01", to: "2026-01-31", limit: 2.5 }],
      ["transactions", { from: "2026-01-01", to: "2026-01-31", kinds: ["spesa"] }],
      ["transactions", { from: "2026-01-01", to: "2026-01-31", statuses: ["booked", "annullata"] }],
      ["transactions", { from: "2026-01-01", to: "2026-01-31", categoryId: 3 }],
      ["obligations", { asOf: "2026-01-31", status: "aperti" }],
      ["data_status", { asOf: "2026-01-31", staleAfterDays: 0 }],
    ];
    for (const [op, input] of bad)
      assert.equal((await err(db, op, input)).code, "invalid_input", JSON.stringify(input));
    const nf = await err(db, "summary", {
      from: "2026-01-01",
      to: "2026-01-31",
      accountIds: ["inesistente"],
    });
    assert.deepEqual([nf.code, /inesistente/.test(nf.message)], ["not_found", true]);
  });

  it("asOf predefinito = data odierna fornita dal chiamante (mai l'orologio di sistema)", async () => {
    const env = await scenario();
    const b = await q<BalancesOutput>(env.db, "balances", {}, "2026-01-31");
    assert.equal(b.asOf, "2026-01-31");
  });

  it("database non aggiornato o vuoto: schema_mismatch, nessuna lettura dei dati", async () => {
    const db = await freshDb();
    assert.equal((await err(db, "accounts")).code, "schema_mismatch");
    await migrate(db, MIGRATIONS.slice(0, 4), testCtx(db).now);
    const e = await err(db, "accounts");
    assert.deepEqual([e.code, /versione 4/.test(e.message)], ["schema_mismatch", true]);
  });

  it("errore interno: messaggio generico, nessun dettaglio SQL esposto", async () => {
    const env = await scenario();
    let calls = 0;
    const flaky: Db = {
      all: async () => {
        throw new Error("no such column: segreto_interno");
      },
      first: async (sql, params) => {
        calls += 1;
        if (calls > 1) throw new Error("no such column: segreto_interno");
        return env.db.first(sql, params);
      },
      batch: (s) => env.db.batch(s),
    };
    const r = await runQuery(flaky, "balances", { asOf: "2026-01-31" });
    assert.equal(r.ok, false);
    assert.ok(!r.ok && r.error.code === "internal" && !/segreto/.test(r.error.message));
  });

  it("sola lettura: nessuna operazione scrive (contatore delle scritture e write_seq invariati); readOnlyDb rifiuta batch", async () => {
    const env = await scenario();
    let writes = 0;
    const spy: Db = {
      all: (sql, params) => env.db.all(sql, params),
      first: (sql, params) => env.db.first(sql, params),
      batch: async (s) => {
        writes += 1;
        return env.db.batch(s);
      },
    };
    const seq = await env.db.first<{ value: string }>("SELECT value FROM meta WHERE key = 'write_seq'");
    const inputs: Record<string, unknown> = {
      accounts: {},
      summary: { from: "2026-01-01", to: "2026-01-31" },
      daily: { from: "2026-01-01", to: "2026-01-31" },
      monthly: { from: "2026-01-01", to: "2026-01-31" },
      balances: { asOf: "2026-01-31" },
      transactions: { from: "2026-01-01", to: "2026-01-31" },
      obligations: { asOf: "2026-01-31", status: "all" },
      data_status: { asOf: "2026-01-31" },
      overview: { from: "2026-01-01", to: "2026-01-31" },
      manual: { from: "2026-01-01", to: "2026-01-31" },
      budgets: { month: "2026-01" },
      search: { q: "demo" },
    };
    for (const o of OPERATIONS) assert.equal((await runQuery(spy, o.name, inputs[o.name])).ok, true, o.name);
    assert.equal(writes, 0);
    assert.deepEqual(await env.db.first("SELECT value FROM meta WHERE key = 'write_seq'"), seq);
    await assert.rejects(readOnlyDb(env.db).batch([]), /sola lettura/);
  });
});

describe("consultazione: riepilogo e movimenti coerenti (nessun doppio conteggio)", () => {
  it("riepilogo = servizio esistente; filtro valuta; periodo vuoto con nota", async () => {
    const env = await scenario();
    const s = await q<SummaryOutput>(env.db, "summary", { from: "2026-01-01", to: "2026-01-31" });
    const report = await periodReport(env.ctx, "2026-01-01", "2026-01-31");
    assert.deepEqual(s.currencies, report.currencies);
    // la serie mensile in una lettura da' gli stessi numeri del riepilogo di ogni mese (senza le quote di budget)
    const m = await q<{ months: { month: string; currencies: Record<string, unknown>[] }[] }>(
      env.db,
      "monthly",
      {
        from: "2025-12-01",
        to: "2026-01-31",
      },
    );
    assert.deepEqual(
      m.months.map((x) => x.month),
      ["2025-12", "2026-01"],
    );
    assert.deepEqual(
      m.months[1]?.currencies,
      report.currencies.map(({ budgetSharesBp: _, ...c }) => c),
    );
    assert.deepEqual(
      s.currencies.map((c) => c.currency),
      ["EUR", "USD"],
    );
    const eur = s.currencies[0];
    assert.deepEqual(
      [eur?.income, eur?.transfersOut, eur?.transfersIn, eur?.obligationsOut, eur?.pendingCount],
      [120_000, 20_000, 20_000, 5000, 1],
    );
    assert.equal(s.excludedDuplicates, 1);
    assert.ok(s.notes.some((n) => /in attesa/.test(n)));
    const usd = await q<SummaryOutput>(env.db, "summary", {
      from: "2026-01-01",
      to: "2026-01-31",
      currency: "USD",
    });
    assert.deepEqual(
      usd.currencies.map((c) => c.currency),
      ["USD"],
    );
    const empty = await q<SummaryOutput>(env.db, "summary", { from: "2025-01-01", to: "2025-12-31" });
    assert.deepEqual(empty.currencies, []);
    assert.ok(empty.notes.some((n) => /nessun movimento/.test(n)));
  });

  it("serie giornaliera: giorni in ordine, somma dei giorni = riepilogo del periodo per valuta", async () => {
    const env = await scenario();
    const d = await q<DailyOutput>(env.db, "daily", { from: "2026-01-01", to: "2026-01-31" });
    const dates = d.days.map((x) => x.date);
    assert.deepEqual(dates, [...new Set(dates)].sort());
    assert.ok(dates.length > 1);
    const report = await periodReport(env.ctx, "2026-01-01", "2026-01-31");
    for (const c of report.currencies) {
      const tot = (k: "income" | "netSpending" | "unclassifiedOut" | "pendingCount") =>
        d.days.reduce((t, x) => t + (x.currencies.find((y) => y.currency === c.currency)?.[k] ?? 0), 0);
      assert.deepEqual(
        [tot("income"), tot("netSpending"), tot("unclassifiedOut"), tot("pendingCount")],
        [c.income, c.netSpending, c.unclassifiedOut, c.pendingCount],
        c.currency,
      );
    }
    assert.deepEqual(
      (await q<DailyOutput>(env.db, "daily", { from: "2025-01-01", to: "2025-01-31" })).days,
      [],
    );
  });

  it("somme dei movimenti di tutte le pagine = riepilogo per valuta e tipo; doppione escluso salvo richiesta", async () => {
    const env = await scenario();
    const items = await allPages(env.db, { from: "2026-01-01", to: "2026-01-31", limit: 2 });
    assert.equal(
      new Set(items.map((i) => i.id)).size,
      items.length,
      "nessun movimento ripetuto tra le pagine",
    );
    const sorted = [...items].sort((a, b) => a.date.localeCompare(b.date) || a.id.localeCompare(b.id));
    assert.deepEqual(items, sorted, "ordine stabile per data effettiva e id");
    const report = await periodReport(env.ctx, "2026-01-01", "2026-01-31");
    for (const c of report.currencies) {
      const booked = items.filter((i) => i.currency === c.currency && i.status === "booked");
      const sum = (kind: string, sign: 1 | -1) =>
        booked
          .filter((i) => i.kind === kind && Math.sign(i.amountMinor) === sign)
          .reduce((t, i) => t + Math.abs(i.amountMinor), 0);
      assert.equal(booked.length, c.bookedCount, c.currency);
      assert.equal(sum("income", 1), c.income);
      assert.equal(sum("expense", -1), c.expenses);
      assert.equal(sum("fee", -1), c.fees);
      assert.equal(sum("transfer", 1), c.transfersIn);
      assert.equal(sum("transfer", -1), c.transfersOut);
      assert.equal(sum("obligation", 1), c.obligationsIn);
      assert.equal(sum("obligation", -1), c.obligationsOut);
      assert.equal(sum("unknown", -1), c.unclassifiedOut);
      assert.equal(
        items.filter((i) => i.currency === c.currency && i.status === "pending").length,
        c.pendingCount,
      );
    }
    assert.ok(items.every((i) => !i.excluded));
    const withExcluded = await allPages(env.db, {
      from: "2026-01-01",
      to: "2026-01-31",
      includeExcluded: true,
    });
    assert.deepEqual(
      withExcluded.filter((i) => i.excluded).map((i) => i.description),
      ["BAR ESEMPIO SRL"],
    );
  });

  it("filtri per conto, stato, tipo, categoria e categoria assente; intervallo sulla data effettiva", async () => {
    const env = await scenario();
    const tx = (input: Record<string, unknown>) =>
      q<TransactionsOutput>(env.db, "transactions", { from: "2026-01-01", to: "2026-01-31", ...input });
    assert.deepEqual(
      (await tx({ accountIds: ["carta"] })).items.map((i) => [i.description, i.kind]),
      [["Ricarica da conto", "transfer"]],
    );
    assert.deepEqual(
      (await tx({ statuses: ["pending"] })).items.map((i) => [i.description, i.date, i.bookingDate]),
      [["Ristorante Esempio", "2026-01-15", null]],
    );
    const obligation = (await tx({ kinds: ["obligation"] })).items;
    assert.deepEqual(
      obligation.map((i) => [i.amountMinor, i.kind]),
      [[-5000, "obligation"]],
    );
    assert.deepEqual(
      (await tx({ categoryId: "spesa-alimentare" })).items.map((i) => i.description),
      ["Spesa supermercato"],
    );
    const noCategory = (await tx({ categoryId: null })).items;
    assert.ok(noCategory.length > 0 && noCategory.every((i) => i.categoryId === null));
    assert.deepEqual((await tx({ accountIds: ["usd"], statuses: ["pending"] })).items, []);
    const jan5 = await q<TransactionsOutput>(env.db, "transactions", {
      from: "2026-01-05",
      to: "2026-01-05",
    });
    assert.deepEqual(
      jan5.items.map((i) => i.description),
      ["Caffe"],
    );
    const empty = await q<TransactionsOutput>(env.db, "transactions", {
      from: "2027-01-01",
      to: "2027-12-31",
    });
    assert.deepEqual([empty.items, empty.nextCursor], [[], null]);
  });

  it("cursore: legato ai filtri; illeggibile o riusato con filtri diversi -> invalid_input", async () => {
    const env = await scenario();
    const first = await q<TransactionsOutput>(env.db, "transactions", {
      from: "2026-01-01",
      to: "2026-01-31",
      limit: 1,
    });
    assert.ok(first.nextCursor);
    const reused = await err(env.db, "transactions", {
      from: "2026-01-01",
      to: "2026-01-31",
      accountIds: ["conto"],
      limit: 1,
      cursor: first.nextCursor,
    });
    assert.deepEqual([reused.code, /filtri diversi/.test(reused.message)], ["invalid_input", true]);
    for (const cursor of ["%%%", "e30", btoa(JSON.stringify({ v: 2 }))]) {
      assert.equal(
        (await err(env.db, "transactions", { from: "2026-01-01", to: "2026-01-31", cursor })).code,
        "invalid_input",
      );
    }
  });

  it("dati personali: IBAN e numeri lunghi mascherati nell'uscita, intatti nel database", async () => {
    const env = await scenario();
    const [item] = (
      await q<TransactionsOutput>(env.db, "transactions", { from: "2026-01-10", to: "2026-01-10" })
    ).items;
    assert.equal(item?.description, "Bonifico [IBAN] carta […1111] sorella DEMO");
    assert.ok(await env.db.first("SELECT 1 FROM transactions WHERE description = ?", [BONIFICO]));
    assert.equal(redactIdentifiers("Caffe 3,50 del 05/01"), "Caffe 3,50 del 05/01", "testi normali intatti");
    assert.equal(redactIdentifiers("rif. 1234-5678-9012"), "rif. […9012]");
    assert.ok(!Object.keys(item ?? {}).some((k) => /raw|fingerprint|source|import|file/i.test(k)));
  });
});

describe("consultazione: saldi, crediti/debiti, stato dei dati", () => {
  it("saldi: osservato e riconciliato del giorno = verificato; vecchio, in conflitto o mancante mai presentato come corrente", async () => {
    const env = await scenario();
    const jan31 = await q<BalancesOutput>(env.db, "balances", { asOf: "2026-01-31" });
    const line = (b: BalancesOutput, id: string) => b.accounts.find((a) => a.accountId === id);
    assert.deepEqual(
      [line(jan31, "conto")?.status, line(jan31, "conto")?.balanceMinor, line(jan31, "conto")?.verifiedAsOf],
      ["observed", 190_130, true],
    );
    assert.deepEqual(
      [line(jan31, "carta")?.status, line(jan31, "carta")?.balanceMinor, line(jan31, "carta")?.verifiedAsOf],
      ["conflict", null, false],
    );
    assert.deepEqual([line(jan31, "usd")?.status, line(jan31, "usd")?.balanceMinor], ["missing", null]);
    assert.deepEqual(jan31.totals, [
      {
        currency: "EUR",
        observedSumMinor: 190_130,
        accountsIncluded: 1,
        accountsWithoutBalance: 1,
        complete: false,
        allVerifiedAsOf: false,
      },
      {
        currency: "USD",
        observedSumMinor: 0,
        accountsIncluded: 0,
        accountsWithoutBalance: 1,
        complete: false,
        allVerifiedAsOf: false,
      },
    ]);
    await importTx(env, "conto", [row("2026-02-02", "-1.00", "Caffe febbraio")], "feb.csv");
    const feb5 = line(
      await q<BalancesOutput>(env.db, "balances", { asOf: "2026-02-05", accountIds: ["conto"] }),
      "conto",
    );
    assert.deepEqual(
      [feb5?.balanceMinor, feb5?.ageDays, feb5?.bookedMovementsAfter, feb5?.verifiedAsOf],
      [190_130, 5, 1, false],
      "il saldo resta quello osservato: nessun saldo ricostruito dai movimenti",
    );
    assert.ok(feb5?.notes.some((n) => /non e' il saldo corrente/.test(n)));
    const before = line(await q<BalancesOutput>(env.db, "balances", { asOf: "2025-12-31" }), "conto");
    assert.deepEqual([before?.status, before?.reconciliation], ["missing", null]);
  });

  it("crediti/debiti: aperti per scadenza, scaduti, giorni alla scadenza, filtri, posizione per valuta", async () => {
    const env = await scenario();
    const mar = await q<ObligationsOutput>(env.db, "obligations", { asOf: "2026-03-01" });
    assert.deepEqual(
      mar.items.map((o) => [o.description, o.residualMinor, o.overdue, o.daysToDue, o.linkedTransactions]),
      [
        ["Prestito a sorella DEMO", 5000, true, -14, 1],
        ["Cena pagata da amico DEMO", 2000, false, null, 0],
      ],
    );
    assert.equal(mar.items[0]?.counterparty.name, "Sorella DEMO");
    assert.deepEqual(mar.position, [
      { currency: "EUR", receivable: 5000, payable: 2000, net: 3000, openCount: 2, overdueCount: 1 },
      { currency: "USD", receivable: 0, payable: 0, net: 0, openCount: 0, overdueCount: 0 },
    ]);
    const closed = await q<ObligationsOutput>(env.db, "obligations", {
      asOf: "2026-03-01",
      status: "closed",
    });
    assert.deepEqual(
      closed.items.map((o) => [o.description, o.status, o.paidMinor]),
      [["Biglietto DEMO", "closed", 700]],
    );
    const payable = await q<ObligationsOutput>(env.db, "obligations", {
      asOf: "2026-03-01",
      status: "all",
      direction: "payable",
      currency: "EUR",
    });
    assert.deepEqual(
      payable.items.map((o) => o.description),
      ["Cena pagata da amico DEMO"],
    );
    const before = await q<ObligationsOutput>(env.db, "obligations", { asOf: "2025-12-31", status: "all" });
    assert.deepEqual([before.items, before.position, before.truncated], [[], [], false]);
  });

  it("stato dei dati: ultimi aggiornamenti per conto e anomalie (saldi, riconciliazione, dati vecchi, import rifiutato, nessun import)", async () => {
    const env = await scenario();
    await createAccount(env.ctx, {
      id: "vuoto",
      name: "Conto vuoto DEMO",
      type: "cash",
      currency: "EUR",
      holderRole: "owner",
    });
    await reconcileAccount(env.ctx, "carta", "2026-01-07", "2026-01-31");
    await importTx(env, "usd", ["riga,non,valida"], "rotto.csv");
    const st = await q<DataStatusOutput>(env.db, "data_status", { asOf: "2026-02-05" });
    assert.equal(st.schemaVersion, MIGRATIONS.length);
    const conto = st.accounts.find((a) => a.accountId === "conto");
    assert.deepEqual(
      [conto?.lastBookedOn, conto?.lastBalanceOn, conto?.lastReconciliation?.status, conto?.pendingCount],
      ["2026-01-10", "2026-01-31", "ok", 1],
    );
    const codes = st.anomalies.map((a) => `${a.accountId}:${a.code}`).sort();
    // dati fermi: ultimo movimento (non data d'importazione) oltre la soglia rispetto alla data richiesta
    assert.deepEqual(codes, [
      "carta:balance_conflict",
      "carta:reconciliation_difference",
      "carta:stale_data",
      "conto:stale_data",
      "usd:balance_missing",
      "usd:last_import_rejected",
      "usd:stale_data",
      "vuoto:balance_missing",
      "vuoto:no_imports",
    ]);
    const fresh = await q<DataStatusOutput>(env.db, "data_status", { asOf: "2026-01-12", staleAfterDays: 7 });
    assert.ok(
      !fresh.anomalies.some((a) => a.code === "stale_data" && a.accountId === "conto"),
      "dati recenti: nessun avviso",
    );
    const staleConto = st.anomalies.find((a) => a.code === "stale_data" && a.accountId === "conto");
    assert.deepEqual(staleConto?.detail, { lastMovementOn: "2026-01-10", days: 26 });
    assert.ok((st.openReviewItems.balance_conflict ?? 0) >= 1);
    const later = await q<DataStatusOutput>(env.db, "data_status", { asOf: "2026-03-01", staleAfterDays: 7 });
    assert.ok(later.anomalies.some((a) => a.code === "stale_import" && a.accountId === "conto"));
  });
});
