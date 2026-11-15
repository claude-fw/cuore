/**
 * Adattatore `Db` del motore Finance sopra lo SQLite di un Durable Object (`ctx.storage.sql`, sincrono).
 * `batch` e' atomico con `transactionSync` (sql.exec non accetta BEGIN/COMMIT: documentazione «SQLite storage API»).
 * Provato nella fase C0 (): stessi esiti di SQLite via Node e di D1.
 */
import type { Db, SqlValue, Statement } from "../../finance/src/db/types.ts";

export interface SqlCursor {
  toArray(): Record<string, unknown>[];
  /** righe lette da SQLite per questa istruzione (contano nella quota giornaliera del piano Free) */
  readonly rowsRead?: number;
}

export interface DurableSqlStorage {
  sql: { exec(query: string, ...bindings: SqlValue[]): SqlCursor };
  transactionSync<T>(fn: () => T): T;
}

export class DurableSqliteDb implements Db {
  readonly #storage: DurableSqlStorage;
  /** righe lette in totale da questa istanza: la differenza prima/dopo una richiesta dice quanto costa */
  rowsRead = 0;

  constructor(storage: DurableSqlStorage) {
    this.#storage = storage;
  }

  #run(sql: string, params: readonly SqlValue[] = []): Record<string, unknown>[] {
    const cur = this.#storage.sql.exec(sql, ...params);
    const rows = cur.toArray();
    this.rowsRead += cur.rowsRead ?? 0;
    return rows;
  }

  async all<T>(sql: string, params?: readonly SqlValue[]): Promise<T[]> {
    return this.#run(sql, params) as T[];
  }

  async first<T>(sql: string, params?: readonly SqlValue[]): Promise<T | null> {
    return (this.#run(sql, params)[0] ?? null) as T | null;
  }

  async batch(statements: readonly Statement[]): Promise<void> {
    if (statements.length === 0) return;
    this.#storage.transactionSync(() => {
      for (const s of statements) this.#run(s.sql, s.params);
    });
  }
}
