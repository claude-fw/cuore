/**
 * Percorso Wrangler reale: `wrangler d1 migrations apply --local` con i file generati in migrations/ (copiati in una
 * cartella temporanea). Lo stato locale di D1 viene poi letto IN SOLA LETTURA con node:sqlite e verificato con lo
 * stesso codice del runner. Telemetria disattivata, nessuna credenziale, nessuna risorsa remota.
 * Eseguito con CLOUD_FINANCE_TEST_DB=d1.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";
import { WranglerCliDb } from "../scripts/wrangler-db.ts";
import { baselineSql } from "../src/db/baseline.ts";
import { migrate } from "../src/db/migrate.ts";
import { generateMigrationFiles } from "../src/db/migration-sql.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import { SCHEMA_SNAPSHOT } from "../src/db/migrations/schema-snapshot.generated.ts";
import { NodeSqliteDb } from "../src/db/node-sqlite.ts";
import { verifyDatabase } from "../src/db/schema-check.ts";
import { BACKEND } from "./db-backend.ts";

const SKIP = BACKEND === "d1" ? false : "solo con CLOUD_FINANCE_TEST_DB=d1 (usa la CLI di Wrangler)";
const ROOT = new URL("..", import.meta.url).pathname;
const WRANGLER = join(ROOT, "node_modules/wrangler/bin/wrangler.js");
const REPO_MIGRATIONS = join(ROOT, "migrations");
const work = mkdtempSync(join(tmpdir(), "cloud-finance-d1-migrations-"));
after(() => rmSync(work, { recursive: true, force: true }));

/** Progetto Wrangler temporaneo con una propria cartella di migrazioni (sottoinsieme dei file del repository). */
function project(name: string, files: readonly string[]): string {
  const dir = join(work, name);
  mkdirSync(join(dir, "migrations"), { recursive: true });
  for (const f of files) copyFileSync(join(REPO_MIGRATIONS, f), join(dir, "migrations", f));
  writeFileSync(
    join(dir, "wrangler.jsonc"),
    JSON.stringify({
      name: "cloud-finance-local-test",
      compatibility_date: "2026-09-01",
      d1_databases: [
        {
          binding: "DB",
          database_name: "cloud-finance-local",
          database_id: "00000000-0000-0000-0000-000000000000",
          migrations_dir: "migrations",
        },
      ],
    }),
  );
  return dir;
}

function wrangler(dir: string, ...args: string[]): { code: number; out: string } {
  const r = spawnSync(
    process.execPath,
    [
      WRANGLER,
      "d1",
      ...args,
      "cloud-finance-local",
      "--local",
      "--persist-to",
      join(dir, "state"),
      "--config",
      join(dir, "wrangler.jsonc"),
    ],
    {
      encoding: "utf8",
      cwd: dir,
      env: { PATH: process.env.PATH ?? "", HOME: dir, WRANGLER_SEND_METRICS: "false", CI: "1" },
      timeout: 120_000,
    },
  );
  return { code: r.status ?? -1, out: `${r.stdout}\n${r.stderr}` };
}

/** Apre in sola lettura il file SQLite in cui D1 locale ha salvato il database. */
function openLocalD1(dir: string): NodeSqliteDb {
  const base = join(dir, "state", "v3", "d1", "miniflare-D1DatabaseObject");
  const files = readdirSync(base).filter((f) => f.endsWith(".sqlite") && f !== "metadata.sqlite");
  assert.equal(files.length, 1, `file D1 locali: ${files.join(", ")}`);
  return new NodeSqliteDb(join(base, files[0] as string), { readOnly: true });
}

const ALL = readdirSync(REPO_MIGRATIONS)
  .filter((f) => f.endsWith(".sql"))
  .sort();

describe("wrangler d1 migrations apply (D1 locale)", { skip: SKIP }, () => {
  it("applica i file generati; il risultato coincide con il runner (schema esatto, versioni, checksum); seconda esecuzione senza effetti", async () => {
    const dir = project("normale", ALL);
    const applied = wrangler(dir, "migrations", "apply");
    assert.equal(applied.code, 0, applied.out);
    const again = wrangler(dir, "migrations", "apply");
    assert.equal(again.code, 0, again.out);
    assert.match(again.out, /No migrations to apply/);

    const d1 = openLocalD1(dir);
    const runner = new NodeSqliteDb(":memory:");
    await migrate(runner, MIGRATIONS, () => "t");
    try {
      const v = await verifyDatabase(d1, MIGRATIONS, SCHEMA_SNAPSHOT);
      assert.deepEqual(
        [v.ok, v.upToDate, v.schema.compared, v.wrangler.consistent],
        [true, true, true, true],
        JSON.stringify(v),
      );
      const schemaSql =
        "SELECT type, name, sql FROM sqlite_master WHERE substr(name, 1, 7) <> 'sqlite_' AND substr(name, 1, 4) <> '_cf_' AND name <> 'd1_migrations' ORDER BY type, name";
      assert.deepEqual(
        await d1.all(schemaSql),
        await runner.all(schemaSql),
        "testo SQL identico, non solo normalizzato",
      );
      const hist = "SELECT version, name, checksum FROM schema_migrations ORDER BY version";
      assert.deepEqual(await d1.all(hist), await runner.all(hist));
      assert.deepEqual(
        (await d1.all<{ name: string }>("SELECT name FROM d1_migrations ORDER BY id")).map((r) => r.name),
        ALL,
      );
    } finally {
      d1.close();
      runner.close();
    }
  });

  it("file mancante nella sequenza: Wrangler applica il successivo, la guardia lo rifiuta in modo atomico", async () => {
    const dir = project("buco", ["0001_core.sql", "0003_planning.sql"]);
    const r = wrangler(dir, "migrations", "apply");
    assert.notEqual(r.code, 0);
    assert.match(r.out, /write_guard_seq/);
    const d1 = openLocalD1(dir);
    try {
      assert.deepEqual(
        (await d1.all<{ version: number }>("SELECT version FROM schema_migrations")).map((x) => x.version),
        [1],
      );
      assert.deepEqual(
        (await d1.all<{ name: string }>("SELECT name FROM d1_migrations")).map((x) => x.name),
        ["0001_core.sql"],
      );
      assert.equal(await d1.first("SELECT name FROM sqlite_master WHERE name = 'savings_goals'"), null);
      assert.equal(
        (await verifyDatabase(d1, MIGRATIONS, SCHEMA_SNAPSHOT)).ok,
        true,
        "stato coerente alla versione 1",
      );
    } finally {
      d1.close();
    }
  });

  it("database gia' migrato fuori da Wrangler: apply rifiutato senza modifiche; dopo l'allineamento nulla da applicare", async () => {
    const dir = project("allineamento", ALL);
    // database migrato senza registro di Wrangler (stesso risultato del runner, dimostrato in migration-sql.test.ts)
    const files = await generateMigrationFiles(MIGRATIONS);
    writeFileSync(join(dir, "tutto.sql"), files.map((f) => f.content).join("\n"));
    assert.equal(wrangler(dir, "execute", "--file", join(dir, "tutto.sql")).code, 0);

    const refused = wrangler(dir, "migrations", "apply");
    assert.notEqual(refused.code, 0);
    assert.match(refused.out, /already exists/);
    let d1 = openLocalD1(dir);
    try {
      const v = await verifyDatabase(d1, MIGRATIONS, SCHEMA_SNAPSHOT);
      assert.deepEqual(
        [v.ok, v.upToDate, v.wrangler.table, v.wrangler.names],
        [false, true, true, []],
        "registro vuoto",
      );
    } finally {
      d1.close();
    }

    writeFileSync(join(dir, "baseline.sql"), await baselineSql(MIGRATIONS, SCHEMA_SNAPSHOT));
    assert.equal(wrangler(dir, "execute", "--file", join(dir, "baseline.sql")).code, 0);
    assert.equal(wrangler(dir, "execute", "--file", join(dir, "baseline.sql")).code, 0, "idempotente");
    const nothing = wrangler(dir, "migrations", "apply");
    assert.equal(nothing.code, 0, nothing.out);
    assert.match(nothing.out, /No migrations to apply/);
    d1 = openLocalD1(dir);
    try {
      const v = await verifyDatabase(d1, MIGRATIONS, SCHEMA_SNAPSHOT);
      assert.deepEqual([v.ok, v.upToDate, v.wrangler.consistent], [true, true, true]);
    } finally {
      d1.close();
    }
  });

  it("limite di Wrangler: un file gia' applicato e poi modificato NON viene rilevato da Wrangler; lo rileva il controllo dei file", async () => {
    const dir = project("modificato", ALL);
    assert.equal(wrangler(dir, "migrations", "apply").code, 0);
    const target = join(dir, "migrations", "0004_bank_formats.sql");
    writeFileSync(target, `${readFileSync(target, "utf8")}\nCREATE TABLE aggiunta_a_mano (x TEXT) STRICT;\n`);
    const r = wrangler(dir, "migrations", "apply");
    assert.equal(r.code, 0);
    assert.match(r.out, /No migrations to apply/, "Wrangler guarda solo i nomi dei file");
    const generated = (await generateMigrationFiles(MIGRATIONS)).find(
      (f) => f.fileName === "0004_bank_formats.sql",
    );
    assert.notEqual(
      readFileSync(target, "utf8"),
      generated?.content,
      "il confronto con il generato lo rileva",
    );
  });
});

describe("gate di sicurezza con la CLI di Wrangler (D1 locale)", { skip: SKIP }, () => {
  /** Database migrato fuori da Wrangler (stessa storia del runner) in un progetto temporaneo. */
  const migratedOutside = async (name: string) => {
    const dir = project(name, ALL);
    const files = await generateMigrationFiles(MIGRATIONS);
    writeFileSync(join(dir, "tutto.sql"), files.map((f) => f.content).join("\n"));
    assert.equal(wrangler(dir, "execute", "--file", join(dir, "tutto.sql")).code, 0);
    writeFileSync(join(dir, "baseline.sql"), await baselineSql(MIGRATIONS, SCHEMA_SNAPSHOT));
    return dir;
  };
  const objects = (dir: string) => {
    const d1 = openLocalD1(dir);
    try {
      return d1.all("SELECT type, name, sql FROM sqlite_master ORDER BY type, name");
    } finally {
      d1.close();
    }
  };

  it("baseline su schema divergente: rifiutato da Wrangler, nessuna scrittura (nemmeno la tabella del registro)", async () => {
    const dir = await migratedOutside("baseline-divergente");
    assert.equal(wrangler(dir, "execute", "--command", "CREATE TABLE intrusa (x TEXT) STRICT").code, 0);
    const before = await objects(dir);
    const r = wrangler(dir, "execute", "--file", join(dir, "baseline.sql"));
    assert.notEqual(r.code, 0);
    assert.match(r.out, /write_guard_seq/);
    assert.deepEqual(await objects(dir), before);
    assert.ok(!(before as { name: string }[]).some((o) => o.name === "d1_migrations"));
  });

  it("baseline interrotto da un errore nello stesso file: Wrangler annulla tutto (batch atomico)", async () => {
    const dir = await migratedOutside("baseline-interrotto");
    const before = await objects(dir);
    writeFileSync(
      join(dir, "interrotto.sql"),
      `${readFileSync(join(dir, "baseline.sql"), "utf8")}\nINSERT INTO accounts (id) VALUES (NULL);\n`,
    );
    const r = wrangler(dir, "execute", "--file", join(dir, "interrotto.sql"));
    assert.notEqual(r.code, 0);
    assert.deepEqual(await objects(dir), before, "nessuna riga del registro, nessuna tabella creata");
  });

  it("registro di Wrangler avanti rispetto alla storia: apply rifiutato dalla guardia, verifica incoerente, baseline rifiutato", async () => {
    const dir = project("registro-avanti", ALL.slice(0, 2));
    assert.equal(wrangler(dir, "migrations", "apply").code, 0);
    // qualcuno registra a mano 0003 senza applicarlo
    assert.equal(
      wrangler(dir, "execute", "--command", "INSERT INTO d1_migrations (name) VALUES ('0003_planning.sql')")
        .code,
      0,
    );
    for (const f of ALL.slice(2)) copyFileSync(join(REPO_MIGRATIONS, f), join(dir, "migrations", f));
    const r = wrangler(dir, "migrations", "apply");
    assert.notEqual(r.code, 0, "Wrangler salta 0003 (gia' 'registrato') e prova 0004");
    assert.match(r.out, /write_guard_seq/);
    const d1 = openLocalD1(dir);
    try {
      assert.deepEqual(
        (await d1.all<{ version: number }>("SELECT version FROM schema_migrations")).map((x) => x.version),
        [1, 2],
      );
      const v = await verifyDatabase(d1, MIGRATIONS, SCHEMA_SNAPSHOT);
      assert.deepEqual([v.ok, v.wrangler.consistent], [false, false]);
    } finally {
      d1.close();
    }
    writeFileSync(join(dir, "baseline.sql"), await baselineSql(MIGRATIONS, SCHEMA_SNAPSHOT));
    assert.notEqual(
      wrangler(dir, "execute", "--file", join(dir, "baseline.sql")).code,
      0,
      "storia incompleta",
    );
  });

  it("verifica in sola lettura passando da Wrangler (--local): stesso esito della lettura diretta; rifiuta scritture", async () => {
    const dir = project("verifica-cli", ALL);
    assert.equal(wrangler(dir, "migrations", "apply").code, 0);
    const target = {
      database: "cloud-finance-local",
      config: join(dir, "wrangler.jsonc"),
      mode: { kind: "local" as const, persistTo: join(dir, "state") },
      env: { HOME: dir },
    };
    const viaCli = new WranglerCliDb(target);
    const cliResult = await verifyDatabase(viaCli, MIGRATIONS, SCHEMA_SNAPSHOT);
    const d1 = openLocalD1(dir);
    try {
      assert.deepEqual(cliResult, await verifyDatabase(d1, MIGRATIONS, SCHEMA_SNAPSHOT));
    } finally {
      d1.close();
    }
    assert.deepEqual([cliResult.ok, cliResult.upToDate, cliResult.schema.compared], [true, true, true]);
    await assert.rejects(viaCli.batch([]), /sola lettura/);
    await assert.rejects(viaCli.all("DELETE FROM d1_migrations"), /solo query SELECT/);
    await assert.rejects(viaCli.all("SELECT 1; DELETE FROM d1_migrations"), /senza ';'/);
    assert.equal(
      (await verifyDatabase(viaCli, MIGRATIONS, SCHEMA_SNAPSHOT)).wrangler.names.length,
      MIGRATIONS.length,
      "registro intatto",
    );

    assert.equal(
      wrangler(dir, "execute", "--command", "CREATE INDEX indice_intruso ON accounts (name)").code,
      0,
    );
    const drift = await verifyDatabase(viaCli, MIGRATIONS, SCHEMA_SNAPSHOT);
    assert.equal(drift.ok, false);
    assert.deepEqual(drift.schema.diff?.unexpected, ["index:indice_intruso"]);
  });
});

describe("L3 — sequenza incompleta con wrangler d1 migrations apply (D1 locale)", { skip: SKIP }, () => {
  const PROJECT_SQL =
    "SELECT type, name, sql FROM sqlite_master WHERE substr(name, 1, 7) <> 'sqlite_' AND substr(name, 1, 4) <> '_cf_' AND name <> 'd1_migrations' ORDER BY type, name";
  /** Oggetti del progetto in un database alla versione `v` creato dal runner (riferimento). */
  const runnerObjects = async (v: number) => {
    const ref = new NodeSqliteDb(":memory:");
    try {
      await migrate(ref, MIGRATIONS.slice(0, v), () => "t");
      return await ref.all(PROJECT_SQL);
    } finally {
      ref.close();
    }
  };
  const read = async (dir: string) => {
    const d1 = openLocalD1(dir);
    try {
      return {
        objects: await d1.all(PROJECT_SQL),
        versions: (
          await d1.all<{ version: number }>("SELECT version FROM schema_migrations ORDER BY version")
        ).map((x) => x.version),
        registry: (await d1.all<{ name: string }>("SELECT name FROM d1_migrations ORDER BY id")).map(
          (x) => x.name,
        ),
        verify: await verifyDatabase(d1, MIGRATIONS, SCHEMA_SNAPSHOT),
      };
    } finally {
      d1.close();
    }
  };

  it("buco nella sequenza: rifiuto senza modifiche parziali (schema IDENTICO alla versione 1), poi completamento sullo stesso database", async () => {
    const dir = project("l3-buco", ["0001_core.sql", "0003_planning.sql"]);
    const refused = wrangler(dir, "migrations", "apply");
    assert.notEqual(refused.code, 0, refused.out);
    assert.match(refused.out, /write_guard_seq/);
    const after1 = await read(dir);
    assert.deepEqual(
      after1.objects,
      await runnerObjects(1),
      "nessun oggetto della migrazione 3, nemmeno parziale",
    );
    assert.deepEqual(after1.versions, [1]);
    assert.deepEqual(after1.registry, ["0001_core.sql"], "0003 non registrato da Wrangler");
    assert.deepEqual([after1.verify.ok, after1.verify.upToDate], [true, false]);

    // completamento: si aggiungono i file mancanti nella stessa cartella e si riapplica
    for (const f of ALL.filter((x) => x !== "0001_core.sql" && x !== "0003_planning.sql")) {
      copyFileSync(join(REPO_MIGRATIONS, f), join(dir, "migrations", f));
    }
    const completed = wrangler(dir, "migrations", "apply");
    assert.equal(completed.code, 0, completed.out);
    const after2 = await read(dir);
    assert.deepEqual(
      after2.objects,
      await runnerObjects(MIGRATIONS.length),
      "schema finale identico al runner",
    );
    assert.deepEqual(
      after2.versions,
      MIGRATIONS.map((m) => m.version),
    );
    assert.deepEqual(after2.registry, ALL);
    assert.deepEqual(
      [after2.verify.ok, after2.verify.upToDate, after2.verify.wrangler.consistent],
      [true, true, true],
    );
  });

  it("primo file mancante: rifiuto; unico residuo la tabella d1_migrations VUOTA creata da Wrangler prima di applicare", async () => {
    const dir = project("l3-senza-primo", ALL.slice(1));
    const refused = wrangler(dir, "migrations", "apply");
    assert.notEqual(refused.code, 0, refused.out);
    assert.match(refused.out, /no such table/);
    const d1 = openLocalD1(dir);
    try {
      const all = (
        await d1.all<{ name: string }>(
          "SELECT name FROM sqlite_master WHERE substr(name, 1, 4) <> '_cf_' ORDER BY name",
        )
      ).map((x) => x.name);
      assert.deepEqual(
        all.filter((n) => !n.startsWith("sqlite_")),
        ["d1_migrations"],
      );
      assert.equal((await d1.all("SELECT * FROM d1_migrations")).length, 0);
      const v = await verifyDatabase(d1, MIGRATIONS, SCHEMA_SNAPSHOT);
      assert.deepEqual(
        [v.version, v.ok, v.upToDate, v.wrangler.table, v.wrangler.names],
        [0, true, false, true, []],
      );
    } finally {
      d1.close();
    }
  });
});
