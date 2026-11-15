/**
 * File SQL per `wrangler d1 migrations apply`, GENERATI da `MIGRATIONS` (unica fonte: nessun secondo sistema).
 *
 * Ogni file `000N_<nome>.sql` contiene, nell'ordine:
 *   1. (solo N >= 2) GUARDIA: righe in `write_guard` (CHECK expected = actual) che fanno fallire l'intero file se il
 *      database non e' esattamente alla versione N-1 o se una migrazione precedente registrata ha un checksum diverso
 *      da quello del codice. Per N = 1 la guardia e' la creazione stessa delle tabelle (fallisce se esistono gia');
 *   2. le istruzioni della migrazione, identiche a quelle eseguite dal runner;
 *   3. la riga in `schema_migrations` con LO STESSO checksum calcolato dal runner.
 * Wrangler aggiunge in coda la propria riga in `d1_migrations` ed esegue tutto in un solo batch (atomico, verificato
 * in locale). Cosi' un database migrato con Wrangler e' verificabile con `migrationStatus()` come uno migrato dal runner.
 *
 * Il messaggio di una guardia fallita contiene `write_guard_seq`: in questo contesto significa "versione o checksum
 * del database diversi da quelli attesi".
 */
import { type Migration, migrationChecksum, validateMigrations } from "./migrate.ts";

export const GENERATED_MARKER =
  "-- GENERATO da src/db/migrations con `npm run migrations:generate`. NON MODIFICARE A MANO.";

export function migrationFileName(m: Migration): string {
  return `${String(m.version).padStart(4, "0")}_${m.name}.sql`;
}

const sqlText = (s: string) => `'${s.replace(/'/g, "''")}'`;

export interface MigrationSqlFile {
  readonly version: number;
  readonly name: string;
  readonly fileName: string;
  readonly checksum: string;
  /** istruzioni nell'ordine in cui vanno eseguite (senza ";" finale) */
  readonly statements: readonly string[];
  /** contenuto del file (UTF-8, LF) */
  readonly content: string;
}

/** Le istruzioni devono poter stare in un file SQL senza ambiguita' per lo splitter di Wrangler. */
function assertFileSafe(m: Migration): void {
  for (const s of m.statements) {
    if (s.trimEnd().endsWith(";")) throw new Error(`migrazione ${m.version}: istruzione con ";" finale`);
    if (s.includes("--") || s.includes("/*"))
      throw new Error(`migrazione ${m.version}: commenti SQL non ammessi`);
  }
}

export async function guardStatementsFor(list: readonly Migration[], version: number): Promise<string[]> {
  if (version <= 1) return [];
  const previous = list.slice(0, version - 1);
  const out = [
    `INSERT INTO write_guard (expected_seq, actual_seq) SELECT ${version - 1}, COALESCE(MAX(version), 0) FROM schema_migrations`,
  ];
  for (const p of previous) {
    out.push(
      `INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = ${p.version} AND name = ${sqlText(p.name)} AND checksum = ${sqlText(await migrationChecksum(p))}`,
    );
  }
  out.push("DELETE FROM write_guard");
  return out;
}

export async function generateMigrationFiles(list: readonly Migration[]): Promise<MigrationSqlFile[]> {
  validateMigrations(list);
  const files: MigrationSqlFile[] = [];
  for (const m of list) {
    assertFileSafe(m);
    const checksum = await migrationChecksum(m);
    const guard = await guardStatementsFor(list, m.version);
    const record = `INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (${m.version}, ${sqlText(m.name)}, ${sqlText(checksum)}, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))`;
    const statements = [...guard, ...m.statements, record];
    const header = [
      GENERATED_MARKER,
      `-- versione: ${m.version} | nome: ${m.name} | checksum: ${checksum}`,
      m.version === 1
        ? "-- guardia: la creazione delle tabelle fallisce se il database e' gia' inizializzato"
        : `-- guardia: si applica solo a un database alla versione ${m.version - 1} con le migrazioni precedenti integre`,
    ];
    files.push({
      version: m.version,
      name: m.name,
      fileName: migrationFileName(m),
      checksum,
      statements,
      content: `${header.join("\n")}\n\n${statements.map((s) => `${s};`).join("\n\n")}\n`,
    });
  }
  return files;
}
