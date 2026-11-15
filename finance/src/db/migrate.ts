/**
 * Migrazioni esplicite, solo in avanti e non distruttive.
 *
 * Regole (verificate dal codice e dai test):
 *   - versioni consecutive da 1, nome in snake_case;
 *   - nessuna istruzione distruttiva (DROP, DELETE FROM, RENAME, REPLACE): lo schema cresce solo per aggiunte;
 *   - ogni migrazione e' un batch atomico insieme alla sua riga in `schema_migrations`;
 *   - il checksum delle migrazioni gia' applicate deve coincidere: una migrazione modificata dopo l'applicazione
 *     blocca tutto (MigrationError) invece di produrre uno schema diverso da quello dichiarato;
 *   - un database con versione piu' alta del codice non viene toccato.
 * Nessuna migrazione parte da sola: si eseguono con `migrate()` (CLI: `cloud-finance migrate`).
 */
import { sha256Hex } from "../domain/hash.ts";
import type { Db } from "./types.ts";
import { stmt } from "./types.ts";

export interface Migration {
  readonly version: number;
  readonly name: string;
  readonly statements: readonly string[];
}

export class MigrationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MigrationError";
  }
}

const DESTRUCTIVE =
  /\b(DROP\s+(TABLE|INDEX|VIEW|TRIGGER|COLUMN)|DELETE\s+FROM|RENAME\s+(TO|COLUMN)|TRUNCATE|REPLACE\s+INTO|INSERT\s+OR\s+REPLACE)\b/i;

export function validateMigrations(list: readonly Migration[]): void {
  list.forEach((m, i) => {
    if (m.version !== i + 1)
      throw new MigrationError(`versione ${m.version} fuori sequenza (attesa ${i + 1})`);
    if (!/^[a-z0-9_]+$/.test(m.name)) throw new MigrationError(`nome di migrazione non valido: ${m.name}`);
    if (m.statements.length === 0) throw new MigrationError(`migrazione ${m.version} vuota`);
    for (const sql of m.statements) {
      const hit = DESTRUCTIVE.exec(sql);
      if (hit)
        throw new MigrationError(`migrazione ${m.version} contiene un'istruzione distruttiva: ${hit[0]}`);
    }
  });
}

export function migrationChecksum(m: Migration): Promise<string> {
  return sha256Hex(JSON.stringify([m.version, m.name, m.statements]));
}

interface AppliedRow {
  version: number;
  name: string;
  checksum: string;
  applied_at: string;
}

export async function appliedMigrations(db: Db): Promise<AppliedRow[]> {
  const table = await db.first<{ name: string }>(
    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'",
  );
  if (table === null) return [];
  return db.all<AppliedRow>(
    "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version",
  );
}

export interface MigrationStatus {
  current: number;
  target: number;
  pending: { version: number; name: string }[];
}

/** Controlla coerenza tra migrazioni applicate e codice. Non scrive nulla. */
export async function migrationStatus(db: Db, list: readonly Migration[]): Promise<MigrationStatus> {
  validateMigrations(list);
  const applied = await appliedMigrations(db);
  const target = list.length;
  for (const row of applied) {
    const known = list[row.version - 1];
    if (known === undefined) {
      throw new MigrationError(
        `il database e' alla versione ${row.version}, piu' recente del codice (${target}): nessuna modifica`,
      );
    }
    if (known.name !== row.name || (await migrationChecksum(known)) !== row.checksum) {
      throw new MigrationError(
        `la migrazione ${row.version} (${row.name}) applicata non coincide con quella nel codice`,
      );
    }
  }
  applied.forEach((row, i) => {
    if (row.version !== i + 1)
      throw new MigrationError(`storico delle migrazioni con buchi alla versione ${i + 1}`);
  });
  const current = applied.length;
  return {
    current,
    target,
    pending: list.slice(current).map((m) => ({ version: m.version, name: m.name })),
  };
}

export interface MigrateResult {
  from: number;
  to: number;
  applied: number[];
}

/** Applica le migrazioni mancanti, ciascuna in un batch atomico. Idempotente. */
export async function migrate(db: Db, list: readonly Migration[], now: () => string): Promise<MigrateResult> {
  const status = await migrationStatus(db, list);
  const applied: number[] = [];
  for (const m of list.slice(status.current)) {
    const checksum = await migrationChecksum(m);
    await db.batch([
      ...m.statements.map((sql) => stmt(sql)),
      stmt(
        "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
        m.version,
        m.name,
        checksum,
        now(),
      ),
    ]);
    applied.push(m.version);
  }
  return { from: status.current, to: status.current + applied.length, applied };
}
