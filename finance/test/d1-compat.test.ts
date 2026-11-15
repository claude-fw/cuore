/**
 * Compatibilita' con Cloudflare D1 LOCALE (Miniflare/workerd). Eseguito solo con CLOUD_FINANCE_TEST_DB=d1.
 * Dimostra che il motore in uso e' davvero D1 e documenta, con prove, le differenze rispetto a SQLite via Node.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { D1Db } from "../src/db/d1.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import { stmt } from "../src/db/types.ts";
import { ConflictError, commitGuarded, readWriteSeq } from "../src/db/write.ts";
import { rawD1 } from "./db-backend.ts";
import { BACKEND, freshDb, integrityCheck, setupWithAccounts } from "./helpers.ts";

const SKIP = BACKEND === "d1" ? false : "solo con CLOUD_FINANCE_TEST_DB=d1";

describe("D1 locale: prove di compatibilita'", { skip: SKIP }, () => {
  // _cf_METADATA non si controlla piu': miniflare 5.20261113 non la crea (era un dettaglio interno del simulatore).
  it("il database dei test e' davvero D1 (PRAGMA ristretti)", async () => {
    const db = await freshDb();
    assert.ok(db instanceof D1Db);
    await assert.rejects(db.first("PRAGMA integrity_check"), /not authorized/);
    await assert.rejects(db.first("PRAGMA journal_mode"), /not authorized/);
    await assert.rejects(db.first("SELECT sqlite_version()"), /not authorized/);
    assert.deepEqual(
      { ...(await db.first<Record<string, unknown>>("PRAGMA foreign_keys")) },
      { foreign_keys: 1 },
    );
  });

  it("schema completo: tutte le tabelle STRICT, trigger, indici parziali e vista presenti; integrita' ok", async () => {
    const { db } = await setupWithAccounts();
    const tables = await db.all<{ name: string; strict: number }>(
      "SELECT name, strict FROM pragma_table_list WHERE schema = 'main' AND type = 'table' AND name NOT LIKE 'sqlite_%' AND name <> '_cf_METADATA'",
    );
    assert.ok(tables.length >= 20, `tabelle: ${tables.length}`);
    assert.deepEqual(
      tables.filter((t) => t.strict !== 1).map((t) => t.name),
      [],
      "tabelle non STRICT",
    );
    const objects = await db.all<{ type: string; name: string; sql: string | null }>(
      "SELECT type, name, sql FROM sqlite_master WHERE type IN ('trigger', 'index', 'view')",
    );
    const triggers = objects.filter((o) => o.type === "trigger").map((o) => o.name);
    for (const t of [
      "raw_records_no_update",
      "excluded_rows_no_delete",
      "transactions_identity_fixed",
      "transaction_links_decide_once",
      "review_items_close_once",
      "obligation_events_no_update",
      "counterparties_no_delete",
    ]) {
      assert.ok(triggers.includes(t), t);
    }
    const partial = objects
      .filter((o) => o.type === "index" && /WHERE/i.test(o.sql ?? ""))
      .map((o) => o.name)
      .sort();
    assert.deepEqual(partial, [
      "obligation_events_by_transaction",
      "obligation_events_one_open",
      "review_items_one_open",
      "transactions_by_fingerprint",
      "transactions_by_source_id",
    ]);
    assert.ok(objects.some((o) => o.type === "view" && o.name === "current_classifications"));
    const cols = await db.all<{ name: string }>("SELECT name FROM pragma_table_info('transactions')");
    assert.ok(
      cols.some((c) => c.name === "source_type"),
      "colonna aggiunta dalla migrazione 4",
    );
    assert.equal((await db.all("SELECT version FROM schema_migrations")).length, MIGRATIONS.length);
    assert.equal(await integrityCheck(db), "ok");
  });

  it("errori dei vincoli e dei trigger arrivano con il messaggio SQLite (serve al riconoscimento dei conflitti)", async () => {
    const { db } = await setupWithAccounts();
    await assert.rejects(db.batch([stmt("DELETE FROM audit_log")]), /D1_ERROR: audit immutabile/);
    await assert.rejects(
      db.batch([
        stmt(
          "INSERT INTO accounts (id, name, type, currency, holder_role, created_at) VALUES ('x', 'X', 'boh', 'EUR', 'owner', 't')",
        ),
      ]),
      /CHECK constraint failed/,
    );
    // STRICT su D1 come su SQLite: un REAL non intero in una colonna INTEGER e' rifiutato (un numero in una colonna
    // TEXT invece viene convertito in testo senza perdita: comportamento standard di SQLite, non una differenza di D1)
    await assert.rejects(
      db.batch([
        stmt(
          "INSERT INTO balance_observations (id, account_id, as_of_date, balance_minor, currency, kind, source_id, created_at) VALUES ('r', 'conto', '2026-01-01', ?, 'EUR', 'end_of_day', 'manual', 't')",
          1.5,
        ),
      ]),
      /cannot store REAL value in INTEGER column/,
    );
    const seq = await readWriteSeq(db);
    await commitGuarded(db, seq, [
      stmt("INSERT INTO institutions (id, name, created_at) VALUES ('i1', 'I1', 't')"),
    ]);
    await assert.rejects(
      commitGuarded(db, seq, [
        stmt("INSERT INTO institutions (id, name, created_at) VALUES ('i2', 'I2', 't')"),
      ]),
      ConflictError,
    );
  });

  it("batch atomico: un errore all'ultima istruzione annulla tutte le precedenti", async () => {
    const { db } = await setupWithAccounts();
    await assert.rejects(
      db.batch([
        stmt("INSERT INTO institutions (id, name, created_at) VALUES ('a', 'A', 't')"),
        stmt("INSERT INTO institutions (id, name, created_at) VALUES ('b', 'B', 't')"),
        stmt("INSERT INTO institutions (id, name, created_at) VALUES ('a', 'duplicato', 't')"),
      ]),
      /UNIQUE constraint failed/,
    );
    assert.deepEqual(
      { ...(await db.first<Record<string, unknown>>("SELECT COUNT(*) AS n FROM institutions")) },
      { n: 0 },
    );
  });

  it("interi: numeri JavaScript interi restano INTEGER (anche negativi e grandi entro 2^53)", async () => {
    const { db } = await setupWithAccounts();
    const big = Number.MAX_SAFE_INTEGER;
    await db.batch([
      stmt(
        "INSERT INTO balance_observations (id, account_id, as_of_date, balance_minor, currency, kind, source_id, created_at) VALUES ('b1', 'conto', '2026-01-01', ?, 'EUR', 'end_of_day', 'manual', 't')",
        big,
      ),
      stmt(
        "INSERT INTO balance_observations (id, account_id, as_of_date, balance_minor, currency, kind, source_id, created_at) VALUES ('b2', 'conto', '2026-01-02', ?, 'EUR', 'end_of_day', 'manual', 't')",
        -1230,
      ),
    ]);
    const rows = await db.all<{ v: number; t: string }>(
      "SELECT balance_minor AS v, typeof(balance_minor) AS t FROM balance_observations ORDER BY id",
    );
    assert.deepEqual(
      rows.map((r) => [r.v, r.t]),
      [
        [big, "integer"],
        [-1230, "integer"],
      ],
    );
  });

  it("limite dei parametri: D1 stesso rifiuta oltre 100; l'adattatore lo anticipa con un errore chiaro", async () => {
    // lista IN piatta: misura il numero di parametri, non la profondita' dell'espressione (anch'essa limitata a 100)
    const sql = (n: number) => `SELECT 7 IN (${Array.from({ length: n }, () => "?").join(",")}) AS hit`;
    const vals = (n: number) => Array.from({ length: n }, (_, i) => i);
    const raw = await rawD1();
    assert.deepEqual(
      {
        ...(await raw
          .prepare(sql(100))
          .bind(...vals(100))
          .first<Record<string, unknown>>()),
      },
      { hit: 1 },
    );
    await assert.rejects(
      raw
        .prepare(sql(101))
        .bind(...vals(101))
        .first(),
      /too many SQL variables/,
    );
    const db = await freshDb();
    assert.equal((await db.first<{ hit: number }>(sql(100), vals(100)))?.hit, 1);
    await assert.rejects(db.first(sql(101), vals(101)), /troppi parametri per D1/);
  });
});
