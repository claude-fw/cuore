/**
 * Migrazioni ibride: file SQL generati da MIGRATIONS (per Wrangler) e runner del progetto.
 * Qui i file sono eseguiti istruzione per istruzione in un batch (come fa Wrangler in locale); il percorso con la
 * CLI di Wrangler vera e propria e' in test/d1-migrations.test.ts.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { pendingChanges } from "../scripts/migrations.ts";
import { baselineStatements, D1_MIGRATIONS_TABLE } from "../src/db/baseline.ts";
import {
  type Migration,
  MigrationError,
  migrate,
  migrationChecksum,
  migrationStatus,
} from "../src/db/migrate.ts";
import { generateMigrationFiles, type MigrationSqlFile } from "../src/db/migration-sql.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import { SCHEMA_SNAPSHOT } from "../src/db/migrations/schema-snapshot.generated.ts";
import { compareSchema, readSchema, verifyDatabase } from "../src/db/schema-check.ts";
import { type Db, stmt } from "../src/db/types.ts";
import { count, FailingDb, freshDb } from "./helpers.ts";

const FILES = await generateMigrationFiles(MIGRATIONS);
const file = (v: number): MigrationSqlFile => {
  const f = FILES[v - 1];
  assert.ok(f);
  return f;
};
const applyFile = (db: Db, f: MigrationSqlFile) => db.batch(f.statements.map((s) => stmt(s)));
const rawSchema = (db: Db) =>
  db.all<{ type: string; name: string; sql: string | null }>(
    "SELECT type, name, sql FROM sqlite_master WHERE substr(name, 1, 7) <> 'sqlite_' AND substr(name, 1, 4) <> '_cf_' AND name <> 'd1_migrations' ORDER BY type, name",
  );
const history = (db: Db) =>
  db.all<{ version: number; name: string; checksum: string }>(
    "SELECT version, name, checksum FROM schema_migrations ORDER BY version",
  );
const tables = async (db: Db) =>
  (await db.all<{ name: string }>("SELECT name FROM sqlite_master WHERE type = 'table'")).map((t) => t.name);

describe("file SQL generati", () => {
  it("sul disco coincidono byte per byte con quelli generati dal codice; istantanea aggiornata", async () => {
    assert.deepEqual(await pendingChanges(), []);
  });

  it("nomi, intestazione con checksum del runner, guardia di versione e di checksum", async () => {
    assert.deepEqual(
      FILES.map((f) => f.fileName),
      [
        "0001_core.sql",
        "0002_classification_review.sql",
        "0003_planning.sql",
        "0004_bank_formats.sql",
        "0005_obligations.sql",
        "0006_account_profiles.sql",
        "0007_manual_and_budgets.sql",
        "0008_search.sql",
      ],
    );
    for (const f of FILES) {
      const m = MIGRATIONS[f.version - 1] as Migration;
      assert.equal(f.checksum, await migrationChecksum(m));
      assert.ok(f.content.includes(`checksum: ${f.checksum}`));
      assert.ok(f.content.startsWith("-- GENERATO"));
      const guards = f.statements.filter((s) => s.startsWith("INSERT INTO write_guard"));
      assert.equal(guards.length, f.version === 1 ? 0 : f.version, `guardie del file ${f.version}`);
      assert.ok(
        f.statements
          .at(-1)
          ?.startsWith(
            `INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (${f.version}, '${f.name}', '${f.checksum}'`,
          ),
      );
    }
  });

  it("rifiuta istruzioni che renderebbero ambiguo il file (';' finale, commenti)", async () => {
    const base = MIGRATIONS[0] as Migration;
    await assert.rejects(
      generateMigrationFiles([{ ...base, statements: [...base.statements, "SELECT 1;"] }]),
      /";" finale/,
    );
    await assert.rejects(
      generateMigrationFiles([{ ...base, statements: [...base.statements, "SELECT 1 -- x"] }]),
      /commenti/,
    );
  });
});

describe("percorso SQL e runner: stesso risultato", () => {
  it("schema IDENTICO (testo SQL esatto) e stessa storia di versioni e checksum", async () => {
    const viaSql = await freshDb();
    for (const f of FILES) await applyFile(viaSql, f);
    const viaRunner = await freshDb();
    await migrate(viaRunner, MIGRATIONS, () => "2026-10-21T00:00:00.000Z");
    assert.deepEqual(await rawSchema(viaSql), await rawSchema(viaRunner));
    assert.deepEqual(await history(viaSql), await history(viaRunner));
    const at = await viaSql.all<{ applied_at: string }>("SELECT applied_at FROM schema_migrations");
    for (const r of at) assert.match(r.applied_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
    assert.equal(await count(viaSql, "write_guard"), 0, "la guardia non lascia righe");
  });

  it("un database migrato con i file SQL e' riconosciuto dal runner (nessuna migrazione rieseguita)", async () => {
    const db = await freshDb();
    for (const f of FILES) await applyFile(db, f);
    assert.deepEqual(await migrationStatus(db, MIGRATIONS), { current: 8, target: 8, pending: [] });
    assert.deepEqual((await migrate(db, MIGRATIONS, () => "t")).applied, []);
    const v = await verifyDatabase(db, MIGRATIONS, SCHEMA_SNAPSHOT);
    assert.deepEqual([v.ok, v.upToDate, v.schema.compared, v.migrationError], [true, true, true, null]);
    assert.deepEqual(v.schema.diff, { missing: [], unexpected: [], changed: [] });
  });

  it("aggiornamento misto: v1-v2 dal runner, v3-v4 con i file SQL", async () => {
    const db = await freshDb();
    await migrate(db, MIGRATIONS.slice(0, 2), () => "t");
    await applyFile(db, file(3));
    await applyFile(db, file(4));
    assert.equal((await verifyDatabase(db, MIGRATIONS, SCHEMA_SNAPSHOT)).ok, true);
  });
});

describe("percorso SQL: errori atomici, mai stati ambigui", () => {
  it("versione saltata (file 3 su database alla versione 1): rifiutato, nulla scritto", async () => {
    const db = await freshDb();
    await applyFile(db, file(1));
    const before = await rawSchema(db);
    await assert.rejects(applyFile(db, file(3)), /write_guard_seq/);
    assert.deepEqual(await rawSchema(db), before);
    assert.deepEqual(
      (await history(db)).map((h) => h.version),
      [1],
    );
    assert.ok(!(await tables(db)).includes("savings_goals"));
  });

  it("doppia applicazione: rifiutata (file 1 per le tabelle esistenti, file N>1 per la guardia)", async () => {
    const db = await freshDb();
    await applyFile(db, file(1));
    await applyFile(db, file(2));
    await assert.rejects(applyFile(db, file(1)), /already exists/);
    await assert.rejects(applyFile(db, file(2)), /write_guard_seq/);
    assert.deepEqual(
      (await history(db)).map((h) => h.version),
      [1, 2],
    );
  });

  it("migrazione precedente registrata con checksum diverso: file successivo rifiutato; runner e verifica la segnalano", async () => {
    const db = await freshDb();
    const tampered: Migration[] = [
      {
        ...(MIGRATIONS[0] as Migration),
        statements: [...(MIGRATIONS[0] as Migration).statements, "CREATE TABLE extra (x TEXT) STRICT"],
      },
    ];
    await migrate(db, tampered, () => "t");
    await assert.rejects(applyFile(db, file(2)), /write_guard_seq/);
    await assert.rejects(migrationStatus(db, MIGRATIONS), MigrationError);
    const v = await verifyDatabase(db, MIGRATIONS, SCHEMA_SNAPSHOT);
    assert.equal(v.ok, false);
    assert.match(v.migrationError ?? "", /non coincide/);
  });

  it("database nato dal runner + file 1 di Wrangler: rifiutato senza modifiche", async () => {
    const db = await freshDb();
    await migrate(db, MIGRATIONS, () => "t");
    const before = await rawSchema(db);
    await assert.rejects(applyFile(db, file(1)), /already exists/);
    assert.deepEqual(await rawSchema(db), before);
  });
});

describe("verifica dello schema e registro di Wrangler", () => {
  it("rileva oggetti in piu', mancanti e modificati; database a versione precedente: in attesa, schema non confrontato", async () => {
    const db = await freshDb();
    await migrate(db, MIGRATIONS, () => "t");
    await db.batch([stmt("CREATE TABLE intrusa (x TEXT) STRICT")]);
    const v = await verifyDatabase(db, MIGRATIONS, SCHEMA_SNAPSHOT);
    assert.equal(v.ok, false);
    assert.deepEqual(v.schema.diff?.unexpected, ["table:intrusa"]);

    const actual = await readSchema(db);
    const expected = SCHEMA_SNAPSHOT.objects.map((o) =>
      o.name === "accounts" ? { ...o, sql: `${o.sql} -- diverso` } : o,
    );
    const diff = compareSchema(
      [...expected, { type: "table", name: "fantasma", tbl_name: "fantasma", sql: "x" }],
      actual,
    );
    assert.deepEqual(diff.missing, ["table:fantasma"]);
    assert.deepEqual(diff.changed, ["table:accounts"]);

    const old = await freshDb();
    await migrate(old, MIGRATIONS.slice(0, 2), () => "t");
    const vo = await verifyDatabase(old, MIGRATIONS, SCHEMA_SNAPSHOT);
    assert.deepEqual(
      [vo.ok, vo.upToDate, vo.schema.compared, vo.pending.map((p) => p.version)],
      [true, false, false, [3, 4, 5, 6, 7, 8]],
    );
  });

  it("registro di Wrangler incoerente con schema_migrations: segnalato", async () => {
    const db = await freshDb();
    for (const f of FILES) await applyFile(db, f);
    await db.batch([
      stmt(D1_MIGRATIONS_TABLE),
      stmt("INSERT INTO d1_migrations (name) VALUES ('0001_core.sql'), ('9999_estranea.sql')"),
    ]);
    const v = await verifyDatabase(db, MIGRATIONS, SCHEMA_SNAPSHOT);
    assert.deepEqual([v.ok, v.wrangler.consistent], [false, false]);
  });
});

const BASELINE = (await baselineStatements(MIGRATIONS, SCHEMA_SNAPSHOT)).map((s) => stmt(s));

describe("baseline: gate di sicurezza (tutto o niente)", () => {
  const registry = async (db: Db) =>
    (await db.all<{ name: string }>("SELECT name FROM d1_migrations ORDER BY id")).map((r) => r.name);
  const hasRegistry = async (db: Db) =>
    (await db.first("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'd1_migrations'")) !==
    null;
  const runnerDb = async () => {
    const db = await freshDb();
    await migrate(db, MIGRATIONS, () => "t");
    return db;
  };
  /** Il baseline deve fallire sulla guardia e lasciare il database esattamente com'era. */
  const refused = async (db: Db, message: string) => {
    const before = await rawSchema(db);
    const regBefore = (await hasRegistry(db)) ? await registry(db) : null;
    await assert.rejects(db.batch(BASELINE), /write_guard_seq|no such table/, message);
    assert.deepEqual(await rawSchema(db), before, `${message}: schema invariato`);
    assert.deepEqual(
      (await hasRegistry(db)) ? await registry(db) : null,
      regBefore,
      `${message}: registro invariato`,
    );
  };

  it("database coerente: registra tutti i file nell'ordine; rieseguito non cambia nulla; verifica ok", async () => {
    const db = await runnerDb();
    await db.batch(BASELINE);
    await db.batch(BASELINE);
    assert.deepEqual(
      await registry(db),
      FILES.map((f) => f.fileName),
    );
    assert.equal(await count(db, "write_guard"), 0);
    const v = await verifyDatabase(db, MIGRATIONS, SCHEMA_SNAPSHOT);
    assert.deepEqual([v.ok, v.upToDate, v.wrangler.consistent], [true, true, true]);
  });

  it("registro gia' parziale ma coerente (prefisso ordinato): completa solo i file mancanti", async () => {
    const db = await runnerDb();
    await db.batch([
      stmt(D1_MIGRATIONS_TABLE),
      stmt("INSERT INTO d1_migrations (name) VALUES ('0001_core.sql'), ('0002_classification_review.sql')"),
    ]);
    await db.batch(BASELINE);
    assert.deepEqual(
      await registry(db),
      FILES.map((f) => f.fileName),
    );
  });

  it("database vuoto: rifiutato, non crea nemmeno la tabella del registro", async () => {
    const db = await freshDb();
    await refused(db, "vuoto");
    assert.equal(await hasRegistry(db), false);
  });

  it("storia incompleta (database a una versione precedente): rifiutato", async () => {
    const db = await freshDb();
    await migrate(db, MIGRATIONS.slice(0, 2), () => "t");
    await refused(db, "versione 2");
    assert.equal(await hasRegistry(db), false);
  });

  it("checksum di una migrazione registrata diverso dal codice: rifiutato, nessuna registrazione parziale", async () => {
    const db = await freshDb();
    const tampered: Migration[] = MIGRATIONS.map((m) =>
      m.version === 1 ? { ...m, statements: [...m.statements, "CREATE TABLE extra (x TEXT) STRICT"] } : m,
    );
    await migrate(db, tampered, () => "t");
    await refused(db, "checksum v1");
    assert.equal(await hasRegistry(db), false);
  });

  it("riga in piu' nella storia (versione oltre il codice): rifiutato", async () => {
    const db = await runnerDb();
    await db.batch([
      stmt(
        "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (9, 'futura', 'x', 't')",
      ),
    ]);
    await refused(db, "versione 8");
    // riga estranea SOTTO la versione 1: il massimo resta corretto, la rileva solo il conteggio
    const low = await runnerDb();
    await low.batch([
      stmt(
        "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (0, 'estranea', 'x', 't')",
      ),
    ]);
    await refused(low, "versione 0");
  });

  it("schema divergente (oggetto in piu' o oggetto diverso): rifiutato", async () => {
    const extra = await runnerDb();
    await extra.batch([stmt("CREATE TABLE intrusa (x TEXT) STRICT")]);
    await refused(extra, "tabella in piu'");
    const index = await runnerDb();
    await index.batch([stmt("CREATE INDEX indice_intruso ON accounts (name)")]);
    await refused(index, "indice in piu'");
  });

  it("registro con nome estraneo, buco o ordine invertito: rifiutato, registro non toccato", async () => {
    for (const [label, values] of [
      ["nome estraneo", "('0001_core.sql'), ('9999_estranea.sql')"],
      ["buco", "('0001_core.sql'), ('0003_planning.sql')"],
      ["ordine invertito", "('0002_classification_review.sql'), ('0001_core.sql')"],
      ["solo un file successivo", "('0004_bank_formats.sql')"],
    ] as const) {
      const db = await runnerDb();
      await db.batch([stmt(D1_MIGRATIONS_TABLE), stmt(`INSERT INTO d1_migrations (name) VALUES ${values}`)]);
      await refused(db, label);
    }
  });

  it("interruzione durante l'allineamento (errore all'ultima istruzione): nessuna scrittura", async () => {
    const db = await runnerDb();
    const failing = new FailingDb(db, BASELINE.length - 1);
    await assert.rejects(failing.batch(BASELINE));
    assert.equal(await hasRegistry(db), false);
    assert.equal(await count(db, "write_guard"), 0);
    await db.batch(BASELINE);
    assert.deepEqual(
      await registry(db),
      FILES.map((f) => f.fileName),
      "dopo l'errore si puo' ripetere",
    );
  });

  it("istantanea non allineata al codice: il baseline non si genera", async () => {
    await assert.rejects(baselineStatements(MIGRATIONS, { ...SCHEMA_SNAPSHOT, version: 3 }), /rigenerare/);
  });
});
