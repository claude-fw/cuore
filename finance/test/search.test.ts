/**
 * Ricerca FTS5 sui movimenti (blocco A6, migrazione v8 IN ATTESA) con dati SINTETICI, su Node e su D1 locale.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { logicalChecksum } from "../scripts/import-plan.ts";
import { migrate, validateMigrations } from "../src/db/migrate.ts";
import { PENDING_MIGRATIONS } from "../src/db/migrations/007_manual_and_budgets.ts";
import { MIGRATIONS, SCHEMA_VERSION } from "../src/db/migrations/index.ts";
import { NodeSqliteDb } from "../src/db/node-sqlite.ts";
import { NOT_DERIVED } from "../src/db/schema-check.ts";
import { type Db, type SqlValue, stmt } from "../src/db/types.ts";
import {
  OPERATIONS,
  type QueryResponse,
  runQuery,
  SYNTHETIC_QUERY_SCHEMA_VERSION,
} from "../src/query/operations.ts";
import { ftsExpression, type SearchOutput, searchTerms } from "../src/query/search.ts";
import { createAccount } from "../src/services/accounts.ts";
import { publishRuleSet, setClassification } from "../src/services/classification.ts";
import {
  addManualTransaction,
  correctManualTransaction,
  voidManualTransaction,
} from "../src/services/manual.ts";
import { count, freshDb, importTx, integrityCheck, row, type TestEnv, testCtx } from "./helpers.ts";

const ALL = [...MIGRATIONS, ...PENDING_MIGRATIONS];
const V7 = MIGRATIONS.filter((m) => m.version <= 7);
const SYNTH = { expectedSchemaVersion: SYNTHETIC_QUERY_SCHEMA_VERSION };

const RULES = {
  schema: "cloud-finance-rules/1",
  note: "SINTETICHE",
  categories: [
    { id: "cibo", name: "Cibo", parent: null, budget_group: "needs" },
    { id: "svago", name: "Svago", parent: null, budget_group: "wants" },
  ],
  rules: [
    {
      id: "mai",
      priority: 10,
      description: "regola che non scatta",
      when: { all: [{ field: "description", op: "starts_with", value: "zzz-mai" }] },
      assign: { kind: "expense", category: "svago", confidence: "high" },
    },
  ],
};

/** Database v7 (come il reale prima dell'attivazione) con conti, movimenti importati e manuali: nessun indice. */
async function v7(): Promise<TestEnv & { ids: Record<string, string> }> {
  const db = await freshDb();
  const ctx = testCtx(db);
  const env = { db, ctx };
  await migrate(db, V7, ctx.now);
  await db.batch([stmt("INSERT INTO meta (key, value) VALUES ('dataset', 'sintetico')")]);
  for (const [id, type] of [
    ["conto", "current"],
    ["carta", "card"],
  ] as const)
    await createAccount(ctx, { id, name: `${id} DEMO`, type, currency: "EUR", holderRole: "owner" });
  await publishRuleSet(ctx, RULES);
  await importTx(env, "conto", [
    row("2026-03-01", "-3.20", "CAFFÈ CENTRALE Milano", { id: "b1", counterparty: "Bar Àlba" }),
    row("2026-03-05", "-45.90", "SUPERMERCATO ESEMPIO", { id: "b2" }),
    row("2026-03-06", "-12.00", "BONIFICO A IT60X0542811101000000123456 affitto", { id: "b3" }),
    row("2026-03-07", "-9.99", "Carta 1234 5678 9012 3456 abbonamento", { id: "b4" }),
    row("2026-03-09", "-4.50", "Caffetteria del Porto", { id: "b5" }),
  ]);
  await importTx(env, "carta", [row("2026-03-02", "-7.00", "caffe al volo", { id: "c1" })], "carta.csv");
  const bank = async (src: string) =>
    (
      (await db.first<{ id: string }>("SELECT id FROM transactions WHERE source_txn_id = ?", [src])) as {
        id: string;
      }
    ).id;
  const manual = async (requestId: string, date: string, description: string, note?: string) =>
    (
      (await addManualTransaction(ctx, {
        requestId,
        accountId: "conto",
        transactionDate: date,
        amountMinor: -1500,
        currency: "EUR",
        description,
        note: note ?? null,
        confirmDuplicate: true,
      })) as { id: string }
    ).id;
  const ids: Record<string, string> = {
    b1: await bank("b1"),
    b2: await bank("b2"),
    b3: await bank("b3"),
    b4: await bank("b4"),
    b5: await bank("b5"),
    c1: await bank("c1"),
    pizza: await manual("m1", "2026-03-10", "Pizzeria da Gino", "cena di compleanno con Àndrea"),
    annullato: await manual("m2", "2026-03-11", "Pizzeria annullata"),
    doppione: await manual("m3", "2026-03-05", "Supermercato a mano"),
  };
  await voidManualTransaction(ctx, ids.annullato as string, "inserito per errore");
  await db.batch([
    stmt(
      `INSERT INTO transaction_links (id, type, from_txn_id, to_txn_id, status, origin, created_at, decided_at)
       VALUES ('l1', 'possible_duplicate', ?, ?, 'confirmed', 'user', ?, ?)`,
      ids.b2 as string,
      ids.doppione as string,
      ctx.now(),
      ctx.now(),
    ),
  ]);
  await setClassification(ctx, ids.b1 as string, "expense", "cibo", "sintetico");
  return { ...env, ids };
}

async function v8(): Promise<TestEnv & { ids: Record<string, string> }> {
  const e = await v7();
  await migrate(e.db, ALL, e.ctx.now);
  return e;
}

async function search(db: Db, input: Record<string, unknown>): Promise<QueryResponse> {
  return runQuery(db, "search", input, SYNTH);
}

async function found(db: Db, input: Record<string, unknown>): Promise<string[]> {
  const r = await search(db, input);
  assert.equal(r.ok, true, JSON.stringify(r));
  return (r as { data: SearchOutput }).data.items.map((i) => i.id);
}

async function ftsIntegrity(db: Db): Promise<void> {
  // 'integrity-check' con rank = 1 confronta l'indice con la vista sorgente: errore se non coincidono
  await db.batch([
    stmt("INSERT INTO transactions_search (transactions_search, rank) VALUES ('integrity-check', 1)"),
  ]);
}

describe("migrazione v8 (ricerca) attiva", () => {
  it("attiva dal 05/11: schema v8, nessuna migrazione in attesa, `search` fra le operazioni", () => {
    assert.equal(SCHEMA_VERSION, 8);
    assert.deepEqual(PENDING_MIGRATIONS, []);
    assert.equal(SYNTHETIC_QUERY_SCHEMA_VERSION, 8);
    validateMigrations(ALL);
    assert.equal(
      OPERATIONS.some((o) => o.name === "search"),
      true,
    );
  });

  it("v7 -> v8 senza perdita di righe; l'indice copre i movimenti esistenti (anche le note dei manuali)", async () => {
    const e = await v7();
    const tables = async () =>
      (
        await e.db.all<{ name: string }>(
          "SELECT name FROM sqlite_master WHERE type = 'table' AND substr(name, 1, 7) <> 'sqlite_' AND substr(name, 1, 4) <> '_cf_' ORDER BY name",
        )
      ).map((r) => r.name);
    const before = await tables();
    const counts = async (names: readonly string[]) =>
      Promise.all(names.map(async (n) => [n, await count(e.db, `"${n}"`)] as const));
    const rowsBefore = await counts(before);
    const res = await migrate(e.db, ALL, e.ctx.now);
    assert.deepEqual(res.applied, [8]);
    const rowsAfter = await counts(before);
    assert.deepEqual(
      rowsAfter.filter(([n]) => n !== "schema_migrations"),
      rowsBefore.filter(([n]) => n !== "schema_migrations"),
    );
    assert.equal(await count(e.db, "schema_migrations"), 8);
    const added = (await tables()).filter((n) => !before.includes(n));
    assert.ok(added.includes("transactions_search"), added.join(","));
    assert.ok(
      added.every((n) => n.startsWith("transactions_search")),
      added.join(","),
    );
    await ftsIntegrity(e.db);
    assert.equal(await integrityCheck(e.db), "ok");
    assert.deepEqual(await found(e.db, { q: "compleanno" }), [e.ids.pizza], "nota del manuale");
    assert.deepEqual(await found(e.db, { q: "pizzeria" }), [e.ids.pizza], "annullato escluso");
  });
});

describe("ricerca", () => {
  it("maiuscole, accenti e prefissi ignorati; tutte le parole devono comparire; dal piu' recente", async () => {
    const e = await v8();
    const caffe = [e.ids.b5, e.ids.c1, e.ids.b1]; // 09/03, 02/03, 01/03
    assert.deepEqual(await found(e.db, { q: "caffe" }), caffe);
    assert.deepEqual(await found(e.db, { q: "CAFFÈ" }), caffe);
    assert.deepEqual(await found(e.db, { q: "Caf" }), caffe);
    assert.deepEqual(await found(e.db, { q: "caffe alba" }), [e.ids.b1], "controparte, accento");
    assert.deepEqual(await found(e.db, { q: "caffè milano" }), [e.ids.b1]);
    assert.deepEqual(await found(e.db, { q: "andrea" }), [e.ids.pizza], "nota con accento");
    assert.deepEqual(await found(e.db, { q: "caffe nessuna-parola" }), []);
    const r = (await search(e.db, { q: "Caffè, al!" })) as { ok: true; data: SearchOutput };
    assert.deepEqual(r.data.terms, ["caffe", "al"]);
    assert.deepEqual(
      r.data.items.map((i) => i.id),
      [e.ids.c1, e.ids.b1],
      "al* = «al» volo, Àlba; Caffetteria del Porto non ha parole che iniziano per «al»",
    );
    const item = r.data.items.find((i) => i.id === e.ids.b1);
    assert.deepEqual(
      { ...item, id: undefined },
      {
        id: undefined,
        accountId: "conto",
        date: "2026-03-01",
        transactionDate: "2026-03-01",
        bookingDate: "2026-03-01",
        status: "booked",
        amountMinor: -320,
        currency: "EUR",
        description: "CAFFÈ CENTRALE Milano",
        counterparty: "Bar Àlba",
        note: null,
        kind: "expense",
        categoryId: "cibo",
        classification: "confirmed",
        why: null,
      },
    );
  });

  it("filtri facoltativi: periodo, conto, categoria; limite e troncamento", async () => {
    const e = await v8();
    assert.deepEqual(await found(e.db, { q: "caffe", from: "2026-03-02", to: "2026-03-08" }), [e.ids.c1]);
    assert.deepEqual(await found(e.db, { q: "caffe", accountIds: ["carta"] }), [e.ids.c1]);
    assert.deepEqual(await found(e.db, { q: "caffe", categoryId: "cibo" }), [e.ids.b1]);
    assert.deepEqual(await found(e.db, { q: "caffe", categoryId: null }), [e.ids.b5, e.ids.c1]);
    const r = (await search(e.db, { q: "caffe", limit: 2 })) as { ok: true; data: SearchOutput };
    assert.deepEqual(
      r.data.items.map((i) => i.id),
      [e.ids.b5, e.ids.c1],
    );
    assert.equal(r.data.truncated, true);
    assert.equal(((await search(e.db, { q: "caffe" })) as { data: SearchOutput }).data.truncated, false);
    const nf = await search(e.db, { q: "caffe", accountIds: ["inesistente"] });
    assert.equal(nf.ok === false && nf.error.code, "not_found");
    for (const bad of [
      { q: "caffe", from: "2026-03-09", to: "2026-03-01" },
      { q: "caffe", from: "2026-02-30" },
      { q: "caffe", limit: 0 },
      { q: "caffe", limit: 51 },
      { q: "caffe", categoryId: 3 },
      { q: "caffe", accountIds: "conto" },
      { q: "caffe", altro: 1 },
    ]) {
      const res = await search(e.db, bad);
      assert.equal(res.ok === false && res.error.code, "invalid_input", JSON.stringify(bad));
    }
  });

  it("esclusi doppioni confermati e manuali annullati; IBAN e numeri lunghi non ricercabili e mascherati", async () => {
    const e = await v8();
    assert.deepEqual(await found(e.db, { q: "supermercato" }), [e.ids.b2], "doppione manuale escluso");
    assert.deepEqual(await found(e.db, { q: "annullata" }), [], "manuale annullato escluso");
    // le cifre mascherate non sono un oracolo: niente risultati, anche se l'indice le contiene
    for (const q of ["IT60X0542811101000000123456", "it60", "0542811101", "5678", "9012 3456"])
      assert.deepEqual(await found(e.db, { q }), [], q);
    const r = (await search(e.db, { q: "affitto" })) as { ok: true; data: SearchOutput };
    assert.equal(r.data.items[0]?.description, "BONIFICO A [IBAN] affitto");
    const c = (await search(e.db, { q: "abbonamento" })) as { ok: true; data: SearchOutput };
    assert.equal(c.data.items[0]?.description, "Carta […3456] abbonamento");
    assert.deepEqual(await found(e.db, { q: "3456" }), [e.ids.b4], "le 4 cifre mostrate restano cercabili");
  });

  it("i trigger tengono l'indice allineato: inserimenti, correzioni, note, aggiornamenti dell'importazione", async () => {
    const e = await v8();
    await importTx(e, "conto", [row("2026-03-20", "-2.00", "Gelateria Nuova", { id: "b9" })], "nuovo.csv");
    assert.equal((await found(e.db, { q: "gelateria" })).length, 1, "importazione successiva indicizzata");
    const m = (await addManualTransaction(e.ctx, {
      requestId: "m9",
      accountId: "carta",
      transactionDate: "2026-03-21",
      amountMinor: -800,
      currency: "EUR",
      description: "Libreria",
      counterparty: "Edicola Sòle",
      note: "regalo per Marta",
      confirmDuplicate: true,
    })) as { id: string };
    assert.deepEqual(await found(e.db, { q: "libreria marta sole" }), [m.id], "manuale con nota");
    await correctManualTransaction(e.ctx, m.id, { description: "Cartoleria" }, "nome sbagliato");
    assert.deepEqual(await found(e.db, { q: "libreria" }), [], "vecchia descrizione tolta");
    assert.deepEqual(await found(e.db, { q: "cartoleria marta" }), [m.id], "nuova descrizione con nota");
    await correctManualTransaction(e.ctx, m.id, { amountMinor: -900 }, "importo"); // nessun cambio di testo
    // aggiornamento diretto dei campi descrittivi (come fa l'importazione): consentito dai trigger di v1
    await e.db.batch([
      stmt("UPDATE transactions SET counterparty = 'Forno Rossi' WHERE id = ?", e.ids.b5 as string),
    ]);
    assert.deepEqual(await found(e.db, { q: "forno" }), [e.ids.b5]);
    await ftsIntegrity(e.db);
    assert.equal(await integrityCheck(e.db), "ok");
    // controllo del controllo: un indice non allineato viene rilevato
    await e.db.batch([
      stmt("INSERT INTO transactions_search (rowid, description) VALUES (999999, 'fantasma')"),
    ]);
    await assert.rejects(ftsIntegrity(e.db));
    // le tabelle restano append-only: nessuna cancellazione passa (ne' dall'indice ne' dai movimenti)
    await assert.rejects(e.db.batch([stmt("DELETE FROM transactions WHERE id = ?", m.id)]));
  });

  it("input pericoloso o sintassi FTS5: errore pulito o semplice testo, mai SQL eseguito ne' errore interno", async () => {
    const e = await v8();
    const n = await count(e.db, "transactions");
    for (const q of [
      '"',
      "*",
      "-",
      "()",
      "NEAR(",
      'caffe" OR "pizzeria',
      "caffe OR pizzeria",
      "description:caffe",
      "caffe*",
      "^caffe",
      "'); DROP TABLE transactions; --",
      "caffe AND NOT alba",
      "a",
    ]) {
      const r = await search(e.db, { q });
      assert.ok(r.ok || r.error.code === "invalid_input", `${q}: ${JSON.stringify(r)}`);
    }
    assert.equal(await count(e.db, "transactions"), n);
    // le parole chiave di FTS5 sono testo: "caffe OR pizzeria" chiede entrambe le parole (nessun risultato)
    assert.deepEqual(await found(e.db, { q: "caffe OR pizzeria" }), []);
    assert.deepEqual(await found(e.db, { q: "description:caffe" }), [], "nessun filtro di colonna");
    assert.deepEqual(searchTerms('caffe" OR "pizzeria*'), ["caffe", "or", "pizzeria"]);
    assert.equal(ftsExpression(searchTerms("Caffè (alba)")), '"caffe"* "alba"*');
    for (const bad of [
      {},
      { q: "" },
      { q: "   " },
      { q: 7 },
      { q: "x".repeat(101) },
      { q: "a b c d e f g h i" },
    ])
      assert.equal(
        ((await search(e.db, bad)) as { ok: boolean; error?: { code: string } }).error?.code,
        "invalid_input",
        JSON.stringify(bad),
      );
    assert.equal(
      ((await search(e.db, { q: "aa bb cc dd ee ff gg hh ii" })) as { error?: { code: string } }).error?.code,
      "invalid_input",
      "al massimo 8 parole",
    );
  });

  it("database v7 (reale non ancora migrato): ogni consultazione chiusa finche' non passa a v8", async () => {
    const e = await v7();
    const r = await runQuery(e.db, "search", { q: "caffe" });
    assert.equal(!r.ok && r.error.code, "schema_mismatch");
    const t = await runQuery(e.db, "transactions", { from: "2026-03-01", to: "2026-03-31" });
    assert.equal(!t.ok && t.error.code, "schema_mismatch", "nessuna lettura a meta' su uno schema vecchio");
    // dopo la migrazione 7 -> 8 il percorso reale (senza versione attesa) cerca
    await migrate(e.db, MIGRATIONS, e.ctx.now);
    const real = await runQuery(e.db, "search", { q: "caffe" });
    assert.equal(real.ok, true, JSON.stringify(real));
  });
});

const PROJECT = "type = 'table' AND substr(name, 1, 7) <> 'sqlite_' AND substr(name, 1, 4) <> '_cf_'";

describe("indice derivato: impronte e backup", () => {
  it("NOT_DERIVED esclude solo l'indice e le sue tabelle ombra; logicalChecksum funziona su v8", async () => {
    const e = await v8();
    const names = async (where: string) =>
      (
        await e.db.all<{ name: string }>(
          `SELECT name FROM sqlite_master WHERE ${PROJECT} AND ${where} ORDER BY name`,
        )
      ).map((r) => r.name);
    const all = await names("1 = 1");
    const kept = await names(NOT_DERIVED);
    assert.deepEqual(
      all.filter((n) => !kept.includes(n)),
      [
        "transactions_search",
        "transactions_search_config",
        "transactions_search_data",
        "transactions_search_docsize",
        "transactions_search_idx",
      ],
    );
    assert.ok(kept.includes("transactions") && kept.includes("audit_log"));
    if (e.db instanceof NodeSqliteDb) assert.match(await logicalChecksum(e.db), /^[0-9a-f]{64}$/);
  });

  it("ripristino come L1 (tabelle senza indice, ordine alfabetico): l'indice si rigenera dai trigger", async () => {
    const e = await v8();
    const tables = (
      await e.db.all<{ name: string }>(
        `SELECT name FROM sqlite_master WHERE ${PROJECT} AND name <> 'schema_migrations' AND ${NOT_DERIVED} ORDER BY name`,
      )
    ).map((r) => r.name);
    const copy = new NodeSqliteDb(":memory:", { foreignKeys: false });
    await migrate(copy, ALL, e.ctx.now);
    for (const t of tables) {
      const rows = await e.db.all<Record<string, SqlValue>>(`SELECT * FROM "${t}" ORDER BY rowid`);
      const cols = rows.length > 0 ? Object.keys(rows[0] as object) : [];
      await copy.batch([
        stmt(`DELETE FROM "${t}"`),
        ...rows.map((r) =>
          stmt(
            `INSERT INTO "${t}" (${cols.map((c) => `"${c}"`).join(", ")}) VALUES (${cols.map(() => "?").join(", ")})`,
            ...cols.map((c) => r[c] ?? null),
          ),
        ),
      ]);
    }
    await ftsIntegrity(copy);
    for (const q of ["caffe", "compleanno", "pizzeria", "affitto"])
      assert.deepEqual(await found(copy, { q }), await found(e.db, { q }), q);
    copy.close();
  });
});
