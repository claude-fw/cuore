/**
 * Adattatore Cloudflare D1 per l'interfaccia `Db`.
 *
 * Nessuna dipendenza: i tipi sotto descrivono SOLO la parte del binding D1 che usiamo (prepare/bind/all/first/batch),
 * cosi' l'adattatore funziona sia con il binding reale del Worker (`env.DB`) sia con quello locale di Miniflare.
 *
 * Differenze da SQLite via Node verificate sul D1 locale (test/d1-compat.test.ts):
 *   - `batch()` e' atomico (transazione implicita): il nucleo non apre mai transazioni a mano;
 *   - chiavi esterne sempre attive; non serve (e non si puo') impostare PRAGMA come journal_mode;
 *   - `PRAGMA integrity_check` e funzioni come `sqlite_version()` non sono autorizzati; `PRAGMA quick_check` e
 *     `PRAGMA foreign_key_check` si';
 *   - in `sqlite_master` compare la tabella interna `_cf_METADATA`;
 *   - gli errori arrivano come Error con prefisso "D1_ERROR:" e il messaggio SQLite originale (vincoli e trigger).
 */
import type { Db, SqlValue, Statement } from "./types.ts";

export interface D1PreparedStatementLike {
  bind(...values: SqlValue[]): D1PreparedStatementLike;
  all<T = Record<string, unknown>>(): Promise<{ results: T[] }>;
  first<T = Record<string, unknown>>(): Promise<T | null>;
}

export interface D1DatabaseLike {
  prepare(sql: string): D1PreparedStatementLike;
  batch(statements: D1PreparedStatementLike[]): Promise<unknown[]>;
}

/**
 * Limite di D1 sui parametri per istruzione. Verificato sul D1 locale: 100 accettati, 101 -> "too many SQL variables"
 * (test/d1-compat.test.ts). L'adattatore lo controlla prima, con un messaggio chiaro.
 */
export const D1_MAX_BOUND_PARAMETERS = 100;

export class D1Db implements Db {
  readonly #d1: D1DatabaseLike;

  constructor(d1: D1DatabaseLike) {
    this.#d1 = d1;
  }

  #prepare(sql: string, params: readonly SqlValue[]): D1PreparedStatementLike {
    if (params.length > D1_MAX_BOUND_PARAMETERS) {
      throw new Error(`troppi parametri per D1 (${params.length} > ${D1_MAX_BOUND_PARAMETERS})`);
    }
    const p = this.#d1.prepare(sql);
    return params.length === 0 ? p : p.bind(...params);
  }

  async all<T>(sql: string, params: readonly SqlValue[] = []): Promise<T[]> {
    const { results } = await this.#prepare(sql, params).all<T>();
    return results;
  }

  async first<T>(sql: string, params: readonly SqlValue[] = []): Promise<T | null> {
    return (await this.#prepare(sql, params).first<T>()) ?? null;
  }

  async batch(statements: readonly Statement[]): Promise<void> {
    if (statements.length === 0) return;
    await this.#d1.batch(statements.map((s) => this.#prepare(s.sql, s.params)));
  }
}
