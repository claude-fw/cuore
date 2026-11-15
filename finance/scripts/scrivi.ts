/**
 * Processo di SCRITTURA del ponte reale di CUORE (D2-reale): un'operazione del titolare sul database indicato.
 *
 *   node scripts/scrivi.ts esegui --db <percorso assoluto del database>    richiesta JSON sull'ingresso standard
 *
 * Nessun percorso predefinito, nessuna variabile d'ambiente: senza `--db` esce con 64. Stesse operazioni e stessa busta
 * di `prova.ts` (`operazioni.ts`). Rifiuti di sicurezza: 2. Errori dei servizi: 1 (`non_valido` | `non_trovato` |
 * `errore`).
 * - Database: file normale (niente collegamenti simbolici, un solo collegamento fisico), dell'utente, non scrivibile da
 *   altri, fuori da iCloud e dal repository; schema alla versione del codice (nessuna migrazione qui).
 * - Letture (`READ_ONLY_OPS`): su un'istantanea in memoria (`readImmutable`), nessun file toccato.
 * - Scritture: prima della PRIMA scrittura di ogni giorno (Europe/Rome) un backup in `<db>/../../backup/`
 *   (`<nome>-prima-di-scrittura-<istante>.sqlite3`, schema della rotazione), copia coerente con `VACUUM INTO`, permessi
 *   0600, `integrity_check` ok e impronta del contenuto uguale a quella dell'originale prima e dopo la copia. Il nome
 *   definitivo si da' solo a un backup verificato; altrimenti la copia resta come `.non-verificato` e la scrittura e'
 *   rifiutata (2).
 */
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  realpathSync,
  renameSync,
} from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { type Ctx, createCtx } from "../src/context.ts";
import { SCHEMA_VERSION } from "../src/db/migrations/index.ts";
import { filesState, ImmutableSqliteDb, NodeSqliteDb, readImmutable } from "../src/db/node-sqlite.ts";
import type { Db } from "../src/db/types.ts";
import { parseBackupName } from "../src/domain/backup-retention.ts";
import { logicalChecksum, snapshot } from "./import-plan.ts";
import { type Input, OPS, out, READ_ONLY_OPS, serviceError } from "./operazioni.ts";

const EXIT_USAGE = 64;
const EXIT_REFUSED = 2;
const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const ZONE = "Europe/Rome";

export class Rifiuto extends Error {}

const inside = (child: string, parent: string) => {
  const r = relative(parent, child);
  return r === "" || (!r.startsWith("..") && !isAbsolute(r));
};

/** Percorso reale del database, dopo i controlli di sicurezza; altrimenti `Rifiuto`. */
export function checkDb(path: unknown): string {
  if (typeof path !== "string" || !isAbsolute(path) || !path.endsWith(".sqlite3"))
    throw new Rifiuto("--db: percorso assoluto di un file .sqlite3");
  if (!existsSync(path)) throw new Rifiuto("database inesistente");
  const st = lstatSync(path);
  if (st.isSymbolicLink() || !st.isFile()) throw new Rifiuto("il database deve essere un file normale");
  if (st.nlink !== 1) throw new Rifiuto("database con piu' collegamenti fisici");
  if (typeof process.getuid === "function" && st.uid !== process.getuid())
    throw new Rifiuto("database di un altro utente");
  if ((st.mode & 0o022) !== 0) throw new Rifiuto("database scrivibile da altri utenti");
  const real = realpathSync.native(path);
  const lib = join(homedir(), "Library");
  for (const f of [join(lib, "Mobile Documents"), join(lib, "CloudStorage"), REPO_ROOT])
    if (inside(real, f)) throw new Rifiuto("database in iCloud o nel repository: rifiutato");
  return real;
}

const dayIn = (iso: string) => new Intl.DateTimeFormat("en-CA", { timeZone: ZONE }).format(new Date(iso));

async function verified(path: string): Promise<{ integrity: string; checksum: string }> {
  const db = new ImmutableSqliteDb(path);
  try {
    const r = await db.first<{ integrity_check: string }>("PRAGMA integrity_check");
    return { integrity: r?.integrity_check ?? "", checksum: await logicalChecksum(db) };
  } finally {
    db.close();
  }
}

async function checksumOf(path: string): Promise<string> {
  const db = new NodeSqliteDb(path, { readOnly: true });
  try {
    return await logicalChecksum(db);
  } finally {
    db.close();
  }
}

/** Impronta da un'istantanea in memoria (`readImmutable`): nessun file creato accanto al database. */
async function immutableChecksumOf(path: string): Promise<string> {
  return readImmutable(path, (db) => logicalChecksum(db));
}

/** `VACUUM INTO` da una connessione `immutable=1`: nessun `-wal`/`-shm` creato; scartata se il database cambia. */
async function immutableSnapshot(from: string, to: string): Promise<void> {
  const before = JSON.stringify(filesState(from));
  const db = new ImmutableSqliteDb(from);
  try {
    await db.all(`VACUUM INTO '${to.replaceAll("'", "''")}'`);
  } finally {
    db.close();
  }
  if (JSON.stringify(filesState(from)) !== before) throw new Error("database cambiato durante la copia");
}

export interface BackupOptions {
  now?: () => Date;
  /** copia coerente (predefinita `VACUUM INTO`); sostituibile nei test */
  copy?: (from: string, to: string) => Promise<void>;
}

/** Backup verificato del giorno: `presente` se ce n'e' gia' uno di oggi, `creato` dopo la verifica; altrimenti `Rifiuto`. */
export function dailyBackup(
  db: string,
  options: BackupOptions = {},
): Promise<{ stato: "presente" | "creato"; file: string }> {
  return verifiedBackup(db, "scrittura", options);
}

/**
 * Copia verificata del database in `<db>/../../backup/<nome>-prima-di-<tipo>-<istante>.sqlite3` (schema della
 * rotazione), al massimo una per tipo e per giorno (Europe/Rome): `presente` se c'e' gia', `creato` dopo la verifica
 * (`integrity_check` ok, impronta del contenuto uguale a quella dell'originale prima e dopo la copia, permessi 0600).
 * Altrimenti `Rifiuto` e la copia resta come `.non-verificato`. L'originale e' solo letto.
 * `settimanale` (database magari non aperto da nessuno): letture solo da istantanee `immutable`, cosi' accanto al
 * database non compare nessun `-wal`/`-shm`; con un `-wal` non vuoto o un `-journal` (scrittura in corso) nessun backup.
 */
export async function verifiedBackup(
  db: string,
  kind: "scrittura" | "settimanale",
  options: BackupOptions = {},
): Promise<{ stato: "presente" | "creato"; file: string }> {
  const now = (options.now ?? (() => new Date()))();
  const today = dayIn(now.toISOString());
  const dir = join(dirname(dirname(db)), "backup");
  const name = basename(db, ".sqlite3");
  const what = kind === "scrittura" ? "nessuna scrittura" : "nessun backup";
  const pure = kind === "settimanale";
  const sum = pure ? immutableChecksumOf : checksumOf;
  if (pure) {
    const st = filesState(db);
    if ((st.wal !== null && st.wal.size !== "0") || st.journal !== null)
      throw new Rifiuto(`database in uso (-wal non vuoto o -journal presente): ${what}`);
  }
  try {
    if (existsSync(dir)) {
      const st = lstatSync(dir);
      if (st.isSymbolicLink() || !st.isDirectory()) throw new Rifiuto("cartella dei backup non valida");
      const done = readdirSync(dir).find((n) => {
        const b = parseBackupName(n);
        return b !== null && b.database === name && b.kind === kind && dayIn(b.at) === today;
      });
      if (done !== undefined) return { stato: "presente", file: done };
    } else mkdirSync(dir, { mode: 0o700 });
  } catch (err) {
    if (err instanceof Rifiuto) throw err;
    throw new Rifiuto(`cartella dei backup non accessibile: ${what}`);
  }
  const file = `${name}-prima-di-${kind}-${now.toISOString().replace(/[:.]/g, "-")}.sqlite3`;
  const final = join(dir, file);
  const work = `${final}.in-verifica`;
  const previousUmask = process.umask(0o077);
  try {
    const before = await sum(db);
    await (options.copy ?? (pure ? immutableSnapshot : snapshot))(db, work);
    chmodSync(work, 0o600);
    const copy = await verified(work);
    const after = await sum(db);
    const problem =
      copy.integrity !== "ok"
        ? "integrity_check non ok"
        : copy.checksum !== before || after !== before
          ? "impronta del backup diversa dal database"
          : null;
    if (problem !== null) {
      renameSync(work, `${final}.non-verificato`);
      throw new Rifiuto(`backup non verificato (${problem}): ${what}`);
    }
    renameSync(work, final);
    return { stato: "creato", file };
  } catch (err) {
    if (err instanceof Rifiuto) throw err;
    try {
      if (existsSync(work)) renameSync(work, `${final}.non-verificato`);
    } catch {
      // copia non verificata lasciata con il nome provvisorio, che la rotazione ignora
    }
    throw new Rifiuto(`backup non riuscito: ${what} (${err instanceof Error ? err.name : "errore"})`);
  } finally {
    process.umask(previousUmask);
  }
}

async function schemaOf(db: string): Promise<number> {
  const ro = new ImmutableSqliteDb(db);
  try {
    return (await ro.first<{ v: number | null }>("SELECT MAX(version) AS v FROM schema_migrations"))?.v ?? 0;
  } catch {
    return 0;
  } finally {
    ro.close();
  }
}

const refuse = (): never => {
  throw new Error("sola lettura");
};
const readCtx = (db: Db): Ctx => ({ db, now: refuse, newId: refuse });

export async function main(argv: readonly string[], options: BackupOptions = {}): Promise<number> {
  const [command, ...rest] = argv;
  const at = rest.indexOf("--db");
  const path = at >= 0 ? rest[at + 1] : undefined;
  if (command !== "esegui" || path === undefined || path.startsWith("--")) {
    process.stderr.write("uso: scrivi.ts esegui --db <database>   (nessun percorso predefinito)\n");
    return EXIT_USAGE;
  }
  let request: { op?: string; input?: Input };
  try {
    request = JSON.parse(readFileSync(0, "utf8"));
  } catch {
    return out({ ok: false, errore: "richiesta_non_valida" }, EXIT_USAGE);
  }
  const name = request.op ?? "";
  const op = OPS[name];
  if (op === undefined) return out({ ok: false, errore: "operazione_sconosciuta" }, EXIT_USAGE);
  const input = request.input ?? {};
  try {
    const db = checkDb(path);
    const version = await schemaOf(db);
    if (version !== SCHEMA_VERSION)
      throw new Rifiuto(
        `database alla versione ${version}, attesa ${SCHEMA_VERSION}: nessuna migrazione da qui`,
      );
    if (READ_ONLY_OPS.has(name))
      return out({ ok: true, risultato: await readImmutable(db, (snap) => op(readCtx(snap), input, {})) });
    const backup = await dailyBackup(db, options);
    const conn = new NodeSqliteDb(db);
    try {
      return out({ ok: true, risultato: await op(createCtx(conn), input, {}), backup: backup.stato });
    } finally {
      conn.close();
    }
  } catch (err) {
    if (err instanceof Rifiuto)
      return out({ ok: false, errore: "rifiutato", motivo: err.message }, EXIT_REFUSED);
    return serviceError(err);
  }
}

if (import.meta.main) process.exitCode = await main(process.argv.slice(2));
