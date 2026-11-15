/**
 * Interfaccia minima verso il database, pensata per due implementazioni:
 *   - oggi: SQLite locale tramite `node:sqlite` (src/db/node-sqlite.ts);
 *   - domani: Cloudflare D1 (asincrono, senza transazioni interattive, con `batch()` atomico).
 *
 * Per questo il nucleo segue sempre lo schema "leggi -> pianifica (funzioni pure) -> scrivi in un unico batch atomico".
 * Nessun modulo del nucleo apre transazioni a mano.
 */

export type SqlValue = string | number | null;

export interface Statement {
  readonly sql: string;
  readonly params: readonly SqlValue[];
}

export function stmt(sql: string, ...params: SqlValue[]): Statement {
  return { sql, params };
}

export interface Db {
  /** Tutte le righe della query. */
  all<T>(sql: string, params?: readonly SqlValue[]): Promise<T[]>;
  /** Prima riga o null. */
  first<T>(sql: string, params?: readonly SqlValue[]): Promise<T | null>;
  /** Esegue le istruzioni in modo ATOMICO: tutte o nessuna (come D1 `batch`). */
  batch(statements: readonly Statement[]): Promise<void>;
}
