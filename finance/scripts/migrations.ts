#!/usr/bin/env node
/**
 * Strumenti per le migrazioni (solo locale, nessun accesso remoto):
 *   generate                 rigenera migrations/*.sql e l'istantanea dello schema da `MIGRATIONS`
 *   check                    verifica che file e istantanea coincidano con il codice (usato da `npm run check`)
 *   baseline                 stampa l'SQL di allineamento di `d1_migrations` (con guardie: tutto o niente)
 *   verify --db <file>       verifica in sola lettura un file SQLite (anche il file locale di D1) rispetto al codice
 *   verify-wrangler --database <nome> --config <file> (--local --persist-to <dir> | --remote --account-id <id>)
 *                            stessa verifica in sola lettura passando dalla CLI di Wrangler (bersaglio esplicito;
 *                            per --remote l'account deve coincidere con account_id della configurazione: vedi
 *                            scripts/wrangler-db.ts)
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { parseArgs } from "node:util";
import { baselineSql } from "../src/db/baseline.ts";
import { migrate } from "../src/db/migrate.ts";
import { generateMigrationFiles } from "../src/db/migration-sql.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import { NodeSqliteDb } from "../src/db/node-sqlite.ts";
import { readSchema, type SchemaSnapshot, verifyDatabase } from "../src/db/schema-check.ts";
import { WranglerCliDb } from "./wrangler-db.ts";

const ROOT = new URL("..", import.meta.url).pathname;
export const MIGRATIONS_DIR = join(ROOT, "migrations");
export const SNAPSHOT_FILE = join(ROOT, "src/db/migrations/schema-snapshot.generated.ts");

export async function buildSnapshot(): Promise<SchemaSnapshot> {
  const db = new NodeSqliteDb(":memory:");
  try {
    await migrate(db, MIGRATIONS, () => "1970-01-01T00:00:00.000Z");
    return { version: MIGRATIONS.length, objects: await readSchema(db) };
  } finally {
    db.close();
  }
}

export function snapshotModule(snapshot: SchemaSnapshot): string {
  return [
    "// GENERATO da `npm run migrations:generate` (scripts/migrations.ts). NON MODIFICARE A MANO.",
    "// Schema atteso dopo tutte le migrazioni, letto da sqlite_master su SQLite con spazi normalizzati.",
    'import type { SchemaSnapshot } from "../schema-check.ts";',
    "",
    `export const SCHEMA_SNAPSHOT: SchemaSnapshot = ${JSON.stringify(snapshot, null, 2)};`,
    "",
  ].join("\n");
}

/** Differenze tra il disco e cio' che il codice genererebbe (vuoto = allineati). */
export async function pendingChanges(): Promise<string[]> {
  const problems: string[] = [];
  const files = await generateMigrationFiles(MIGRATIONS);
  const expected = new Set(files.map((f) => f.fileName));
  const onDisk = existsSync(MIGRATIONS_DIR)
    ? readdirSync(MIGRATIONS_DIR).filter((f) => f.endsWith(".sql"))
    : [];
  for (const f of files) {
    const path = join(MIGRATIONS_DIR, f.fileName);
    if (!existsSync(path)) problems.push(`manca ${f.fileName}`);
    else if (readFileSync(path, "utf8") !== f.content)
      problems.push(`${f.fileName} diverso da quello generato`);
  }
  for (const f of onDisk) if (!expected.has(f)) problems.push(`file SQL non generato dal codice: ${f}`);
  const snap = snapshotModule(await buildSnapshot());
  if (!existsSync(SNAPSHOT_FILE) || readFileSync(SNAPSHOT_FILE, "utf8") !== snap) {
    problems.push("istantanea dello schema non aggiornata");
  }
  return problems;
}

async function main(argv: string[]): Promise<number> {
  const { values, positionals } = parseArgs({
    args: argv,
    options: {
      db: { type: "string" },
      database: { type: "string" },
      config: { type: "string" },
      local: { type: "boolean" },
      remote: { type: "boolean" },
      "persist-to": { type: "string" },
      "account-id": { type: "string" },
    },
    allowPositionals: true,
  });
  switch (positionals[0]) {
    case "generate": {
      mkdirSync(MIGRATIONS_DIR, { recursive: true });
      const existing = readdirSync(MIGRATIONS_DIR).filter((f) => f.endsWith(".sql"));
      const files = await generateMigrationFiles(MIGRATIONS);
      const unknown = existing.filter((f) => !files.some((g) => g.fileName === f));
      if (unknown.length > 0) {
        // nessuna cancellazione automatica: un file estraneo va valutato a mano
        process.stderr.write(
          `file SQL non generati dal codice (non toccati, da verificare): ${unknown.join(", ")}\n`,
        );
        return 1;
      }
      for (const f of files) writeFileSync(join(MIGRATIONS_DIR, f.fileName), f.content);
      writeFileSync(SNAPSHOT_FILE, snapshotModule(await buildSnapshot()));
      process.stdout.write(
        `generati ${files.length} file SQL e l'istantanea dello schema (v${MIGRATIONS.length})\n`,
      );
      return 0;
    }
    case "check": {
      const problems = await pendingChanges();
      if (problems.length > 0) {
        process.stderr.write(
          `migrazioni non allineate al codice:\n- ${problems.join("\n- ")}\nEseguire: npm run migrations:generate\n`,
        );
        return 1;
      }
      process.stdout.write(`migrazioni allineate: ${MIGRATIONS.length} file SQL e istantanea dello schema\n`);
      return 0;
    }
    case "baseline": {
      const { SCHEMA_SNAPSHOT } = await import("../src/db/migrations/schema-snapshot.generated.ts");
      process.stdout.write(await baselineSql(MIGRATIONS, SCHEMA_SNAPSHOT));
      return 0;
    }
    case "verify-wrangler": {
      const { database, config } = values;
      if (database === undefined || config === undefined) throw new Error("indicare --database e --config");
      if (values.local === values.remote) throw new Error("indicare esattamente uno tra --local e --remote");
      const persistTo = values["persist-to"];
      if (values.local && persistTo === undefined) throw new Error("--local richiede --persist-to");
      const accountId = values["account-id"];
      if (values.local && accountId !== undefined) throw new Error("--account-id vale solo con --remote");
      if (values.remote && accountId === undefined) throw new Error("--remote richiede --account-id");
      // il costruttore controlla account e configurazione PRIMA di avviare Wrangler
      const db = new WranglerCliDb({
        database,
        config,
        mode: values.local
          ? { kind: "local", persistTo: persistTo as string }
          : { kind: "remote", accountId: accountId as string },
      });
      const { SCHEMA_SNAPSHOT } = await import("../src/db/migrations/schema-snapshot.generated.ts");
      const result = await verifyDatabase(db, MIGRATIONS, SCHEMA_SNAPSHOT);
      process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
      return result.ok ? 0 : 1;
    }
    case "verify": {
      if (values.db === undefined) throw new Error("indicare --db <file>");
      const db = new NodeSqliteDb(values.db, { readOnly: true });
      try {
        const { SCHEMA_SNAPSHOT } = await import("../src/db/migrations/schema-snapshot.generated.ts");
        const result = await verifyDatabase(db, MIGRATIONS, SCHEMA_SNAPSHOT);
        process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return result.ok ? 0 : 1;
      } finally {
        db.close();
      }
    }
    default:
      process.stderr.write(
        "uso: migrations.ts generate | check | baseline | verify --db <file> | verify-wrangler ...\n",
      );
      return 64;
  }
}

if (import.meta.main) {
  main(process.argv.slice(2)).then(
    (code) => {
      process.exitCode = code;
    },
    (err: unknown) => {
      process.stderr.write(`errore: ${err instanceof Error ? err.message : String(err)}\n`);
      process.exitCode = 2;
    },
  );
}
