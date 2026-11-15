import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  type Migration,
  MigrationError,
  migrate,
  migrationStatus,
  validateMigrations,
} from "../src/db/migrate.ts";
import { MIGRATIONS, SCHEMA_VERSION } from "../src/db/migrations/index.ts";
import { stmt } from "../src/db/types.ts";
import { ConflictError, commitGuarded, readWriteSeq } from "../src/db/write.ts";
import {
  count,
  freshDb,
  importTx,
  integrityCheck,
  row,
  setup,
  setupWithAccounts,
  testCtx,
} from "./helpers.ts";

describe("migrazioni", () => {
  it("database nuovo: applica tutte le versioni, integrita' ok, seconda esecuzione senza effetti", async () => {
    const db = await freshDb();
    const ctx = testCtx(db);
    const first = await migrate(db, MIGRATIONS, ctx.now);
    assert.deepEqual(first, { from: 0, to: SCHEMA_VERSION, applied: [1, 2, 3, 4, 5, 6, 7, 8] });
    assert.equal(await integrityCheck(db), "ok");
    const again = await migrate(db, MIGRATIONS, ctx.now);
    assert.deepEqual(again, { from: SCHEMA_VERSION, to: SCHEMA_VERSION, applied: [] });
    assert.equal(await count(db, "schema_migrations"), SCHEMA_VERSION);
  });

  it("aggiornamento v1 -> v8 conserva i dati esistenti", async () => {
    const db = await freshDb();
    const ctx = testCtx(db);
    await migrate(db, MIGRATIONS.slice(0, 1), ctx.now);
    await db.batch([
      stmt(
        "INSERT INTO accounts (id, name, type, currency, holder_role, created_at) VALUES ('a', 'A', 'current', 'EUR', 'owner', 'x')",
      ),
    ]);
    assert.deepEqual((await migrationStatus(db, MIGRATIONS)).pending, [
      { version: 2, name: "classification_review" },
      { version: 3, name: "planning" },
      { version: 4, name: "bank_formats" },
      { version: 5, name: "obligations" },
      { version: 6, name: "account_profiles" },
      { version: 7, name: "manual_and_budgets" },
      { version: 8, name: "search" },
    ]);
    const res = await migrate(db, MIGRATIONS, ctx.now);
    assert.deepEqual(res.applied, [2, 3, 4, 5, 6, 7, 8]);
    assert.equal(await count(db, "accounts"), 1);
    assert.equal(await integrityCheck(db), "ok");
  });

  it("migrazione gia' applicata e poi modificata nel codice: blocco", async () => {
    const { db } = await setup();
    const tampered: Migration[] = MIGRATIONS.map((m) =>
      m.version === 2 ? { ...m, statements: [...m.statements, "CREATE TABLE extra (x TEXT)"] } : m,
    );
    await assert.rejects(migrationStatus(db, tampered), MigrationError);
    await assert.rejects(
      migrate(db, tampered, () => "t"),
      /non coincide/,
    );
  });

  it("database piu' recente del codice: nessuna modifica", async () => {
    const { db } = await setup();
    await assert.rejects(
      migrate(db, MIGRATIONS.slice(0, 2), () => "t"),
      /piu' recente del codice/,
    );
  });

  it("rifiuta istruzioni distruttive e versioni fuori sequenza", () => {
    const base = MIGRATIONS[0];
    assert.ok(base);
    for (const sql of [
      "DROP TABLE accounts",
      "ALTER TABLE accounts DROP COLUMN name",
      "DELETE FROM accounts",
      "ALTER TABLE accounts RENAME TO conti",
      "INSERT OR REPLACE INTO meta VALUES ('a','b')",
    ]) {
      assert.throws(
        () => validateMigrations([base, { version: 2, name: "x", statements: [sql] }]),
        /distruttiva/,
        sql,
      );
    }
    assert.throws(() => validateMigrations([{ ...base, version: 2 }]), /fuori sequenza/);
  });

  it("migrazione che fallisce a meta': rollback completo, versione invariata", async () => {
    const db = await freshDb();
    const ctx = testCtx(db);
    await migrate(db, MIGRATIONS, ctx.now);
    const broken: Migration = {
      version: 9,
      name: "broken",
      statements: ["CREATE TABLE nuova (x TEXT)", "CREATE TABLE accounts (id TEXT)"],
    };
    await assert.rejects(migrate(db, [...MIGRATIONS, broken], ctx.now));
    assert.equal((await migrationStatus(db, MIGRATIONS)).current, 8);
    assert.equal(
      await db.first("SELECT name FROM sqlite_master WHERE name = 'nuova'"),
      null,
      "la tabella creata prima dell'errore non deve esistere",
    );
  });

  it("storico immutabile: schema_migrations, import_runs, raw_records, audit_log, transazioni", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", [row("2026-01-02", "-1.00", "Prova")]);
    const { db } = env;
    const forbidden = [
      "UPDATE schema_migrations SET name = 'x'",
      "DELETE FROM schema_migrations",
      "UPDATE import_runs SET status = 'rejected'",
      "DELETE FROM import_runs",
      "UPDATE raw_records SET raw_json = '{}'",
      "DELETE FROM raw_records",
      "UPDATE audit_log SET action = 'x'",
      "DELETE FROM audit_log",
      "DELETE FROM transactions",
      "UPDATE transactions SET account_id = 'carta'",
      "UPDATE transactions SET status = 'pending', booking_date = NULL",
      "DELETE FROM transaction_observations",
    ];
    for (const sql of forbidden) {
      await assert.rejects(db.batch([stmt(sql)]), Error, sql);
    }
    assert.equal(await count(db, "raw_records"), 1);
  });

  it("vincoli: chiavi esterne, CHECK sugli importi e sulle valute", async () => {
    const { db } = await setupWithAccounts();
    await assert.rejects(
      db.batch([
        stmt(
          "INSERT INTO balance_observations (id, account_id, as_of_date, balance_minor, currency, kind, source_id, created_at) VALUES ('b', 'inesistente', '2026-01-01', 1, 'EUR', 'end_of_day', 'manual', 'x')",
        ),
      ]),
      /FOREIGN KEY/,
    );
    await assert.rejects(
      db.batch([
        stmt(
          "INSERT INTO accounts (id, name, type, currency, holder_role, created_at) VALUES ('z', 'Z', 'current', 'eur', 'owner', 'x')",
        ),
      ]),
      /CHECK/,
    );
    await assert.rejects(
      db.batch([
        stmt(
          "INSERT INTO balance_observations (id, account_id, as_of_date, balance_minor, currency, kind, source_id, created_at) VALUES ('b', 'conto', '2026-01-01', 1.5, 'EUR', 'end_of_day', 'manual', 'x')",
        ),
      ]),
      /cannot store REAL value in INTEGER column|datatype mismatch/i,
    );
  });

  it("controllo di concorrenza: un piano calcolato su dati vecchi non viene applicato", async () => {
    const { db } = await setupWithAccounts();
    const seq = await readWriteSeq(db);
    await commitGuarded(db, seq, [
      stmt("INSERT INTO institutions (id, name, created_at) VALUES ('i1', 'I1', 'x')"),
    ]);
    await assert.rejects(
      commitGuarded(db, seq, [
        stmt("INSERT INTO institutions (id, name, created_at) VALUES ('i2', 'I2', 'x')"),
      ]),
      ConflictError,
    );
    assert.equal(await count(db, "institutions"), 1);
  });
});
