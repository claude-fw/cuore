/**
 * ALLINEAMENTO del registro di Wrangler (`d1_migrations`) per un database migrato dal runner del progetto.
 *
 * E' un unico batch "tutto o niente": prima le GUARDIE (righe in `write_guard`, CHECK expected = actual), poi le
 * registrazioni. Se una sola guardia fallisce, nulla viene scritto (nemmeno la tabella `d1_migrations`): l'errore
 * mostra `write_guard_seq`. Le guardie richiedono che il database DIMOSTRI di essere esattamente alla versione del
 * codice:
 *   1. storia: `schema_migrations` contiene esattamente le versioni 1..N, ciascuna con nome e checksum del codice;
 *   2. registro: `d1_migrations` e' vuoto oppure contiene, NELL'ORDINE, i primi k file attesi (nessun nome estraneo,
 *      nessun buco, nessuno scambio di ordine); si completano solo i file mancanti in coda;
 *   3. schema: gli oggetti del progetto in `sqlite_master` sono esattamente quelli dell'istantanea, con lo stesso
 *      testo SQL (confronto ESATTO: una differenza anche solo di spazi fa rifiutare, mai accettare).
 * Non corregge nulla: in caso di incoerenza rifiuta e lascia il database com'era. Rieseguito su un database gia'
 * allineato non cambia nulla (idempotente).
 */
import { type Migration, migrationChecksum, validateMigrations } from "./migrate.ts";
import { migrationFileName } from "./migration-sql.ts";
import { NOT_PROJECT, type SchemaSnapshot } from "./schema-check.ts";

const sqlText = (s: string) => `'${s.replace(/'/g, "''")}'`;
const guard = (expected: string | number, actualSelect: string) =>
  `INSERT INTO write_guard (expected_seq, actual_seq) SELECT ${expected}, (${actualSelect})`;

/** Stessa definizione che crea Wrangler 4.147.0 (getCreateMigrationsTableQuery). */
export const D1_MIGRATIONS_TABLE = `CREATE TABLE IF NOT EXISTS "d1_migrations"(
		id         INTEGER PRIMARY KEY AUTOINCREMENT,
		name       TEXT UNIQUE,
		applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
)`;

export async function baselineStatements(
  list: readonly Migration[],
  snapshot: SchemaSnapshot,
): Promise<string[]> {
  validateMigrations(list);
  const n = list.length;
  if (snapshot.version !== n) {
    throw new Error(
      `istantanea alla versione ${snapshot.version}, codice alla ${n}: rigenerare le migrazioni`,
    );
  }
  const files = list.map(migrationFileName);
  const out = [D1_MIGRATIONS_TABLE];

  // 1. storia esatta 1..N con nomi e checksum del codice
  out.push(guard(n, "SELECT COUNT(*) FROM schema_migrations"));
  out.push(guard(n, "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"));
  for (const m of list) {
    out.push(
      guard(
        1,
        `SELECT COUNT(*) FROM schema_migrations WHERE version = ${m.version} AND name = ${sqlText(m.name)} AND checksum = ${sqlText(await migrationChecksum(m))}`,
      ),
    );
  }

  // 2. registro di Wrangler: solo nomi attesi, ciascuno nella sua posizione (=> prefisso ordinato dei file attesi)
  out.push(
    guard(0, `SELECT COUNT(*) FROM d1_migrations WHERE name NOT IN (${files.map(sqlText).join(", ")})`),
  );
  files.forEach((file, i) => {
    out.push(
      guard(
        0,
        `SELECT COUNT(*) FROM d1_migrations d WHERE d.name = ${sqlText(file)} AND (SELECT COUNT(*) FROM d1_migrations e WHERE e.id <= d.id) <> ${i + 1}`,
      ),
    );
  });

  // 3. schema esatto: stesso numero di oggetti del progetto e ciascuno identico all'istantanea
  out.push(guard(snapshot.objects.length, `SELECT COUNT(*) FROM sqlite_master WHERE ${NOT_PROJECT}`));
  for (const o of snapshot.objects) {
    const sql = o.sql === null ? "sql IS NULL" : `sql = ${sqlText(o.sql)}`;
    out.push(
      guard(
        1,
        `SELECT COUNT(*) FROM sqlite_master WHERE type = ${sqlText(o.type)} AND name = ${sqlText(o.name)} AND tbl_name = ${sqlText(o.tbl_name)} AND ${sql}`,
      ),
    );
  }
  out.push("DELETE FROM write_guard");

  // registrazione dei soli file mancanti, nell'ordine
  for (const file of files) {
    out.push(
      `INSERT INTO d1_migrations (name) SELECT ${sqlText(file)} WHERE NOT EXISTS (SELECT 1 FROM d1_migrations WHERE name = ${sqlText(file)})`,
    );
  }
  return out;
}

export async function baselineSql(list: readonly Migration[], snapshot: SchemaSnapshot): Promise<string> {
  const statements = await baselineStatements(list, snapshot);
  return `-- Allineamento di d1_migrations (generato): rifiuta qualsiasi storia, registro o schema non dimostrabili.\n${statements.map((s) => `${s};`).join("\n")}\n`;
}
