/**
 * Verifica in SOLA LETTURA di un database rispetto al codice: versioni e checksum registrati (`migrationStatus`),
 * schema reale (`sqlite_master`) confrontato con l'istantanea generata, stato del registro di Wrangler.
 * Usa solo query permesse anche su D1 (nessun PRAGMA non autorizzato). Non scrive mai.
 */
import { type Migration, MigrationError, migrationStatus } from "./migrate.ts";
import { migrationFileName } from "./migration-sql.ts";
import type { Db } from "./types.ts";

export interface SchemaObject {
  readonly type: string;
  readonly name: string;
  readonly tbl_name: string;
  /** testo SQL ESATTO come in sqlite_master (il confronto per la verifica normalizza gli spazi) */
  readonly sql: string | null;
}

export interface SchemaSnapshot {
  readonly version: number;
  readonly objects: readonly SchemaObject[];
}

/**
 * Oggetti del motore o di strumenti esterni, non dello schema del progetto: prefissi `sqlite_` (SQLite), `_cf_`
 * (D1) e la tabella `d1_migrations` di Wrangler. Confronto per prefisso esatto (in LIKE "_" sarebbe un jolly).
 */
export const NOT_PROJECT =
  "substr(name, 1, 7) <> 'sqlite_' AND substr(name, 1, 4) <> '_cf_' AND name <> 'd1_migrations'";

/**
 * Esclude gli indici DERIVATI: tabelle virtuali (FTS5, v8) e loro tabelle ombra (`<nome>_data`, `_idx`, `_config`,
 * `_docsize`; alcune sono WITHOUT ROWID). Si ricostruiscono dai dati (`rebuild`, trigger): impronte logiche e
 * backup le saltano, il ripristino le rigenera inserendo i movimenti. Condizione su `sqlite_master` (anche D1/DO).
 */
export const NOT_DERIVED = `NOT EXISTS (SELECT 1 FROM sqlite_master v WHERE v.type = 'table'
  AND v.sql LIKE 'CREATE VIRTUAL TABLE%'
  AND (sqlite_master.name = v.name OR substr(sqlite_master.name, 1, length(v.name) + 1) = v.name || '_'))`;

export function normalizeSql(sql: string | null): string | null {
  return sql === null ? null : sql.replace(/\s+/g, " ").trim();
}

export async function readSchema(db: Db): Promise<SchemaObject[]> {
  const rows = await db.all<SchemaObject>(
    `SELECT type, name, tbl_name, sql FROM sqlite_master WHERE ${NOT_PROJECT} ORDER BY type, name`,
  );
  return rows.map((r) => ({ type: r.type, name: r.name, tbl_name: r.tbl_name, sql: r.sql }));
}

export interface SchemaDiff {
  missing: string[];
  unexpected: string[];
  changed: string[];
}

export function compareSchema(
  expected: readonly SchemaObject[],
  actual: readonly SchemaObject[],
): SchemaDiff {
  const key = (o: SchemaObject) => `${o.type}:${o.name}`;
  const exp = new Map(expected.map((o) => [key(o), o]));
  const act = new Map(actual.map((o) => [key(o), o]));
  return {
    missing: [...exp.keys()].filter((k) => !act.has(k)),
    unexpected: [...act.keys()].filter((k) => !exp.has(k)),
    changed: [...exp.keys()].filter((k) => {
      const a = act.get(k);
      const e = exp.get(k);
      return (
        a !== undefined &&
        e !== undefined &&
        (normalizeSql(a.sql) !== normalizeSql(e.sql) || a.tbl_name !== e.tbl_name)
      );
    }),
  };
}

export interface DatabaseVerification {
  /** coerente: nessuna migrazione modificata, schema identico all'istantanea (se confrontabile), registro Wrangler allineato */
  ok: boolean;
  /** alla versione del codice (nessuna migrazione in attesa) */
  upToDate: boolean;
  version: number;
  target: number;
  pending: { version: number; name: string }[];
  /** errore di coerenza versioni/checksum (database piu' recente, migrazione modificata, buchi) */
  migrationError: string | null;
  /** confronto dello schema: solo se il database e' alla versione dell'istantanea */
  schema: { compared: boolean; diff: SchemaDiff | null };
  /** registro di Wrangler: assente, oppure coerente/incoerente con schema_migrations */
  wrangler: { table: boolean; names: string[]; consistent: boolean | null };
}

export async function verifyDatabase(
  db: Db,
  list: readonly Migration[],
  snapshot: SchemaSnapshot,
): Promise<DatabaseVerification> {
  let version = 0;
  let pending: { version: number; name: string }[] = [];
  let migrationError: string | null = null;
  try {
    const st = await migrationStatus(db, list);
    version = st.current;
    pending = st.pending;
  } catch (err) {
    if (!(err instanceof MigrationError)) throw err;
    migrationError = err.message;
  }

  let schema: DatabaseVerification["schema"] = { compared: false, diff: null };
  if (migrationError === null && version === snapshot.version) {
    schema = { compared: true, diff: compareSchema(snapshot.objects, await readSchema(db)) };
  }

  const hasD1Table =
    (await db.first<{ name: string }>(
      "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'd1_migrations'",
    )) !== null;
  let wrangler: DatabaseVerification["wrangler"] = { table: false, names: [], consistent: null };
  if (hasD1Table) {
    const names = (await db.all<{ name: string }>("SELECT name FROM d1_migrations ORDER BY id")).map(
      (r) => r.name,
    );
    const expected = list.slice(0, version).map(migrationFileName);
    wrangler = { table: true, names, consistent: JSON.stringify(names) === JSON.stringify(expected) };
  }

  const diff = schema.diff;
  const schemaOk =
    !schema.compared ||
    (diff !== null && diff.missing.length + diff.unexpected.length + diff.changed.length === 0);
  return {
    ok: migrationError === null && schemaOk && wrangler.consistent !== false,
    upToDate: migrationError === null && pending.length === 0,
    version,
    target: list.length,
    pending,
    migrationError,
    schema,
    wrangler,
  };
}
