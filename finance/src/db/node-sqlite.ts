/**
 * Adattatore SQLite locale basato su `node:sqlite` (incluso in Node >= 24: nessuna dipendenza nativa da compilare).
 * Unico modulo del nucleo che dipende da Node: il resto del codice parla solo con l'interfaccia `Db`.
 */
import { existsSync, readFileSync, statSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { pathToFileURL } from "node:url";
import type { Db, SqlValue, Statement } from "./types.ts";

export class NodeSqliteDb implements Db {
  readonly #db: DatabaseSync;

  /**
   * `readOnly`: SQLite rifiuta le scritture e nessun PRAGMA modifica il file principale, MA su un database in modalita'
   * WAL l'apertura crea comunque `-wal` e `-shm` accanto al file (verificato, test/readonly-sqlite.test.ts). Per una
   * lettura che non tocchi nulla sul disco, coerente anche durante un'importazione, usare `readImmutable`.
   * `foreignKeys: false` (solo per il ripristino di un backup, L1): le righe si inseriscono senza vincoli nell'ordine
   * del manifesto; si riapre con `foreignKeys` predefinito (`true`) per il controllo `foreign_key_check` finale.
   */
  constructor(path: string, options: { readOnly?: boolean; foreignKeys?: boolean } = {}) {
    this.#db = new DatabaseSync(path, { readOnly: options.readOnly ?? false });
    // `node:sqlite` apre con `foreign_keys` attive per difetto (a differenza di SQLite nudo): va spenta esplicitamente.
    this.#db.exec(`PRAGMA foreign_keys = ${(options.foreignKeys ?? true) ? "ON" : "OFF"}`);
    if (options.readOnly) return;
    this.#db.exec("PRAGMA busy_timeout = 5000");
    if (path !== ":memory:") this.#db.exec("PRAGMA journal_mode = WAL");
  }

  async all<T>(sql: string, params: readonly SqlValue[] = []): Promise<T[]> {
    return this.#db.prepare(sql).all(...params) as T[];
  }

  async first<T>(sql: string, params: readonly SqlValue[] = []): Promise<T | null> {
    const row = this.#db.prepare(sql).get(...params);
    return (row ?? null) as T | null;
  }

  async batch(statements: readonly Statement[]): Promise<void> {
    if (statements.length === 0) return;
    this.#db.exec("BEGIN IMMEDIATE");
    try {
      for (const s of statements) this.#db.prepare(s.sql).run(...s.params);
      this.#db.exec("COMMIT");
    } catch (err) {
      this.#db.exec("ROLLBACK");
      throw err;
    }
  }

  /** Controllo di integrita' del file (solo per diagnostica e test). */
  integrityCheck(): string {
    const row = this.#db.prepare("PRAGMA integrity_check").get() as { integrity_check: string } | undefined;
    return row?.integrity_check ?? "nessun risultato";
  }

  close(): void {
    this.#db.close();
  }
}

/**
 * Stato del database e dei file accanto (dimensione, modifica e cambio di stato al nanosecondo, inode); `null` =
 * assente. Nessun contenuto letto. Ogni `write()` sul file cambia `mtime`/`ctime`: e' il rivelatore delle scritture
 * concorrenti.
 */
export type FileState = { size: string; mtimeNs: string; ctimeNs: string; ino: string } | null;
export type FilesState = Record<"db" | "wal" | "shm" | "journal", FileState>;

export function filesState(path: string): FilesState {
  const one = (p: string): FileState => {
    if (!existsSync(p)) return null;
    const s = statSync(p, { bigint: true });
    return {
      size: String(s.size),
      mtimeNs: String(s.mtimeNs),
      ctimeNs: String(s.ctimeNs),
      ino: String(s.ino),
    };
  };
  return { db: one(path), wal: one(`${path}-wal`), shm: one(`${path}-shm`), journal: one(`${path}-journal`) };
}

/** Il database non si puo' leggere in modo coerente adesso (assente, scrittura in corso, cambiato durante la copia). */
export class SnapshotUnavailableError extends Error {
  readonly reason:
    | "missing"
    | "too_large"
    | "wal_not_empty"
    | "journal_present"
    | "changed_during_read"
    | "invalid_snapshot";

  constructor(reason: SnapshotUnavailableError["reason"], message: string) {
    super(message);
    this.name = "SnapshotUnavailableError";
    this.reason = reason;
  }
}

/**
 * Database aperto come URI `immutable=1` + `readOnly`: SQLite non scrive, non prende lock e non crea `-wal`/`-shm`.
 * `batch` e' sempre un errore. NON protegge da uno scrittore concorrente (legge il file vivo): per le letture usare
 * `readImmutable`.
 */
export class ImmutableSqliteDb implements Db {
  readonly #db: DatabaseSync;

  constructor(path: string) {
    const url = pathToFileURL(path);
    url.searchParams.set("immutable", "1");
    this.#db = new DatabaseSync(url, { readOnly: true });
  }

  async all<T>(sql: string, params: readonly SqlValue[] = []): Promise<T[]> {
    return this.#db.prepare(sql).all(...params) as T[];
  }

  async first<T>(sql: string, params: readonly SqlValue[] = []): Promise<T | null> {
    return (this.#db.prepare(sql).get(...params) ?? null) as T | null;
  }

  async batch(_statements: readonly Statement[]): Promise<void> {
    throw new Error("database aperto in sola lettura immutabile");
  }

  close(): void {
    this.#db.close();
  }
}

/** Copia in memoria di un'istantanea dei byte del database: nessun file aperto, `query_only`, `batch` rifiutato. */
class SnapshotSqliteDb implements Db {
  readonly #db: DatabaseSync;

  constructor(bytes: Buffer) {
    this.#db = new DatabaseSync(":memory:");
    this.#db.exec("PRAGMA query_only = ON");
    if (bytes.length === 0) return; // file di 0 byte = database SQLite vuoto
    const copy = Buffer.from(bytes);
    copy[18] = 1; // versioni di scrittura/lettura "legacy": una copia in memoria non usa il WAL (con 2 non si apre)
    copy[19] = 1;
    this.#db.deserialize(new Uint8Array(copy.buffer, copy.byteOffset, copy.byteLength));
    this.#db.exec("PRAGMA query_only = ON");
  }

  quickCheck(): string {
    return (
      (this.#db.prepare("PRAGMA quick_check").get() as { quick_check: string } | undefined)?.quick_check ?? ""
    );
  }

  async all<T>(sql: string, params: readonly SqlValue[] = []): Promise<T[]> {
    return this.#db.prepare(sql).all(...params) as T[];
  }

  async first<T>(sql: string, params: readonly SqlValue[] = []): Promise<T | null> {
    return (this.#db.prepare(sql).get(...params) ?? null) as T | null;
  }

  async batch(_statements: readonly Statement[]): Promise<void> {
    throw new Error("istantanea in sola lettura: scrittura rifiutata");
  }

  close(): void {
    this.#db.close();
  }
}

const SQLITE_MAGIC = "SQLite format 3\0";

export interface SnapshotOptions {
  /** Tentativi quando il file cambia durante la copia (predefinito 3). */
  attempts?: number;
  /** Dimensione massima del database copiato in memoria (predefinito 512 MiB). */
  maxBytes?: number;
  /** Solo per i test: lettura dei byte (predefinita `readFileSync`). */
  readBytes?: (path: string) => Buffer;
}

const pause = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Una copia coerente dei byte, o il motivo per cui ora non si puo' avere. */
function takeSnapshot(path: string, max: number, readBytes: (path: string) => Buffer): Buffer {
  const before = filesState(path);
  if (before.db === null) throw new SnapshotUnavailableError("missing", "database inesistente");
  if (BigInt(before.db.size) > BigInt(max))
    throw new SnapshotUnavailableError("too_large", "database troppo grande per una copia in memoria");
  if (before.wal !== null && before.wal.size !== "0")
    throw new SnapshotUnavailableError(
      "wal_not_empty",
      "scrittura in corso o non consolidata (-wal non vuoto)",
    );
  if (before.journal !== null)
    throw new SnapshotUnavailableError(
      "journal_present",
      "scrittura in corso o interrotta (-journal presente)",
    );
  const bytes = readBytes(path);
  if (JSON.stringify(filesState(path)) !== JSON.stringify(before))
    throw new SnapshotUnavailableError(
      "changed_during_read",
      "database cambiato durante la copia: copia scartata",
    );
  if (
    String(bytes.length) !== before.db.size ||
    (bytes.length > 0 && bytes.subarray(0, 16).toString("latin1") !== SQLITE_MAGIC)
  )
    throw new SnapshotUnavailableError("invalid_snapshot", "copia del database non valida");
  return bytes;
}

/**
 * Lettura coerente e realmente in sola lettura, anche con uno scrittore concorrente (importazione in corso):
 *   1. rifiuta `-wal` non vuoto e `-journal` presente: in WAL il file principale cambia solo nei checkpoint, che
 *      avvengono con il `-wal` pieno; in modalita' rollback solo con il `-journal` presente;
 *   2. copia i byte del file in memoria con UNA lettura e confronta lo stato dei file (dimensione, mtime/ctime al
 *      nanosecondo, inode) prima e dopo: ogni scrittura nel frattempo li cambia, e la copia viene scartata;
 *   3. apre la copia in memoria (`deserialize`), ne controlla l'integrita' (`quick_check`) ed esegue `fn` solo su di
 *      essa: le query non toccano piu' il file vivo, nessun lock, nessun file creato, lo scrittore non viene mai fermato.
 * Se il file cambia durante la copia riprova (fino a `attempts` volte); scrittura in corso = errore immediato.
 * Limite residuo: uno scrittore che modifichi il file senza aggiornare mtime/ctime (non SQLite) non sarebbe visto;
 * `quick_check` rileva comunque le copie strutturalmente rotte.
 */
export async function readImmutable<T>(
  path: string,
  fn: (db: Db) => Promise<T>,
  options: SnapshotOptions = {},
): Promise<T> {
  const attempts = Math.max(1, options.attempts ?? 3);
  const max = options.maxBytes ?? 512 * 1024 * 1024;
  const readBytes = options.readBytes ?? ((p: string) => readFileSync(p));
  for (let i = 1; ; i++) {
    let db: SnapshotSqliteDb;
    try {
      db = new SnapshotSqliteDb(takeSnapshot(path, max, readBytes));
      if (db.quickCheck() !== "ok") {
        db.close();
        throw new SnapshotUnavailableError("invalid_snapshot", "copia del database non integra");
      }
    } catch (err) {
      const retry =
        err instanceof SnapshotUnavailableError
          ? err.reason === "changed_during_read" || err.reason === "invalid_snapshot"
          : true;
      if (retry && i < attempts) {
        await pause(50 * i);
        continue;
      }
      if (err instanceof SnapshotUnavailableError) throw err;
      throw new SnapshotUnavailableError("invalid_snapshot", "copia del database non apribile");
    }
    try {
      return await fn(db);
    } finally {
      db.close();
    }
  }
}
