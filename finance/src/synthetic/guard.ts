/**
 * AMBIENTE SINTETICO DI CUORE FINANCE (D3a, decisione C-10): isolamento verificabile dai dati reali.
 *
 * Una SESSIONE DI PROVA e' una cartella creata da zero (`createSession`) sotto la cartella temporanea dell'utente, con:
 *   - `finance-sintetico.sqlite3`: database creato in quella cartella e marcato `meta.dataset = 'sintetico'` e
 *     `meta.sessione = <id>`;
 *   - `sessione.json`: identita' e stato (`aperta` | `chiusa`).
 *
 * Ogni apertura in scrittura e ogni migrazione passano da `openSyntheticForWrite`, che e' FAIL-CLOSED:
 *   1. percorso esplicito e assoluto (nessun percorso predefinito, nessuna variabile d'ambiente `CUORE_*`);
 *   2. `realpath` della cartella SOTTO la cartella temporanea dell'utente (lista ammessa) e FUORI dalle cartelle reali
 *      (`~/Library/Application Support/Cloud` e `/CUORE`, `~/Library/Mobile Documents`, `~/Library/CloudStorage`,
 *      repository): ogni dubbio (percorso inesistente, `realpath` fallito) e' un rifiuto;
 *   3. nessun collegamento simbolico (cartella, database, file di sessione) e database con UN solo collegamento fisico
 *      (un hard link al database reale avrebbe un percorso «buono»);
 *   4. cartella dell'utente corrente con permessi 0700, file 0600;
 *   5. marcatore e id di sessione letti in SOLA LETTURA prima di aprire in scrittura, e riletti sulla connessione in
 *      scrittura prima di qualsiasi istruzione (anche prima di `migrate`).
 * Il database reale non ha il marcatore: anche un percorso sfuggito ai controlli 1–4 viene rifiutato al passo 5.
 */
import { randomUUID } from "node:crypto";
import { existsSync, lstatSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, isAbsolute, join, relative } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import type { Ctx } from "../context.ts";
import { type Migration, migrate } from "../db/migrate.ts";
import { PENDING_MIGRATIONS } from "../db/migrations/007_manual_and_budgets.ts";
import { MIGRATIONS, SCHEMA_VERSION } from "../db/migrations/index.ts";
import { NodeSqliteDb } from "../db/node-sqlite.ts";
import { stmt } from "../db/types.ts";

export const SYNTHETIC_DB = "finance-sintetico.sqlite3";
export const SESSION_FILE = "sessione.json";
export const SESSION_SCHEMA = "cuore.prova.sessione/1";
export const MARKER = "sintetico";
/** Versione dello schema della copia sintetica: le migrazioni attive piu' quelle in attesa (v7). */
export const SYNTHETIC_SCHEMA_VERSION = SCHEMA_VERSION + PENDING_MIGRATIONS.length;
export const SYNTHETIC_MIGRATIONS = [...MIGRATIONS, ...PENDING_MIGRATIONS];

export class SyntheticGuardError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SyntheticGuardError";
  }
}

export interface GuardRoots {
  /** l'unica radice ammessa (realpath) */
  allowedRoot: string;
  /** radici vietate (anche se fossero sotto quella ammessa) */
  forbidden: readonly string[];
}

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

export function defaultRoots(): GuardRoots {
  const home = homedir();
  return {
    allowedRoot: tmpdir(),
    forbidden: [
      join(home, "Library", "Application Support", "Cloud"),
      join(home, "Library", "Application Support", "CUORE"),
      join(home, "Library", "Mobile Documents"),
      join(home, "Library", "CloudStorage"),
      REPO_ROOT,
    ],
  };
}

function fail(message: string): never {
  throw new SyntheticGuardError(message);
}

function inside(child: string, parent: string): boolean {
  const r = relative(parent, child);
  return r === "" || (!r.startsWith("..") && !isAbsolute(r));
}

/**
 * `realpathSync.native` (realpath(3) del sistema): stesso risultato, ma sotto il modello dei permessi di Node chiede
 * la lettura solo del percorso dato, non di ogni cartella antenata come la versione in JavaScript (verificato il 01/11:
 * quella rifiuta la radice temporanea anche con `--allow-fs-read`).
 */
function real(path: string): string {
  try {
    return realpathSync.native(path);
  } catch {
    return fail(`percorso non verificabile: ${path}`);
  }
}

/** Come `real`, ma per le radici vietate che potrebbero non esistere: si confronta anche il percorso dato. */
function realOrSelf(path: string): string[] {
  try {
    return [path, realpathSync.native(path)];
  } catch {
    return [path];
  }
}

function assertPrivate(path: string, kind: "dir" | "file"): void {
  const st = lstatSync(path);
  if (st.isSymbolicLink()) fail(`collegamento simbolico non ammesso: ${path}`);
  if (kind === "dir" ? !st.isDirectory() : !st.isFile()) fail(`tipo di file inatteso: ${path}`);
  if (typeof process.getuid === "function" && st.uid !== process.getuid())
    fail(`proprietario diverso: ${path}`);
  if ((st.mode & 0o077) !== 0) fail(`permessi troppo aperti (${(st.mode & 0o777).toString(8)}): ${path}`);
  if (kind === "file" && st.nlink !== 1) fail(`file con piu' collegamenti fisici: ${path}`);
}

function assertPlace(realPath: string, roots: GuardRoots, allowRootItself: boolean): void {
  for (const f of roots.forbidden) {
    for (const candidate of realOrSelf(f)) {
      if (inside(realPath, candidate))
        fail(`percorso vietato (dati reali, iCloud o repository): ${realPath}`);
    }
  }
  const allowed = real(roots.allowedRoot);
  if (!inside(realPath, allowed) || (!allowRootItself && realPath === allowed)) {
    fail(`percorso fuori dalla cartella temporanea dell'utente: ${realPath}`);
  }
}

/** Cartella di una sessione esistente: restituisce il suo realpath o rifiuta. */
export function checkDataDir(dir: unknown, roots: GuardRoots = defaultRoots()): string {
  if (typeof dir !== "string" || dir.trim() === "") fail("cartella dati obbligatoria (--dati <percorso>)");
  if (!isAbsolute(dir)) fail(`percorso assoluto obbligatorio: ${dir}`);
  if (!existsSync(dir)) fail(`cartella inesistente: ${dir}`);
  if (lstatSync(dir).isSymbolicLink()) fail(`collegamento simbolico non ammesso: ${dir}`);
  const r = real(dir);
  assertPlace(r, roots, false);
  assertPrivate(r, "dir");
  return r;
}

export interface SessionInfo {
  schema: string;
  id: string;
  creata: string;
  stato: "aperta" | "chiusa";
  database: string;
  versioneSchema: number;
}

export function readSession(realDir: string): SessionInfo {
  const p = join(realDir, SESSION_FILE);
  if (!existsSync(p)) fail("file di sessione assente");
  assertPrivate(p, "file");
  let s: SessionInfo;
  try {
    s = JSON.parse(readFileSync(p, "utf8")) as SessionInfo;
  } catch {
    return fail("file di sessione illeggibile");
  }
  if (s.schema !== SESSION_SCHEMA || typeof s.id !== "string" || s.database !== SYNTHETIC_DB) {
    fail("file di sessione non valido");
  }
  if (s.stato !== "aperta" && s.stato !== "chiusa") fail("stato della sessione non valido");
  return s;
}

function dbFile(realDir: string): string {
  const p = join(realDir, SYNTHETIC_DB);
  if (!existsSync(p)) fail("database sintetico assente");
  assertPrivate(p, "file");
  if (dirname(real(p)) !== realDir) fail("database fuori dalla cartella della sessione");
  return p;
}

async function markerOf(db: { first<T>(sql: string, p?: readonly unknown[]): Promise<T | null> }) {
  try {
    const rows = await (db as NodeSqliteDb).all<{ key: string; value: string }>(
      "SELECT key, value FROM meta WHERE key IN ('dataset', 'sessione')",
    );
    return Object.fromEntries(rows.map((r) => [r.key, r.value])) as { dataset?: string; sessione?: string };
  } catch {
    return {};
  }
}

function assertMarker(m: { dataset?: string; sessione?: string }, session: SessionInfo): void {
  if (m.dataset !== MARKER)
    fail("marcatore sintetico assente o errato: il database non e' una copia di prova");
  if (m.sessione !== session.id) fail("il database appartiene a un'altra sessione");
}

/**
 * Cartella, sessione e file del database verificati SENZA aprire il database (nessun -wal/-shm creato). Per le sole
 * letture del ponte di prova (`prova_money.ts`): il marcatore lo ricontrolla `runQuery` sull'istantanea immutabile
 * (lo schema sintetico e' ammesso solo su un database marcato). Le scritture passano sempre da `openSyntheticForWrite`.
 */
export function locateSynthetic(dir: unknown, roots: GuardRoots = defaultRoots()) {
  const realDir = checkDataDir(dir, roots);
  const session = readSession(realDir);
  return { realDir, session, path: dbFile(realDir) };
}

/** Verifica completa in SOLA LETTURA (nessuna scrittura di dati; SQLite puo' creare -wal/-shm vuoti nella sessione). */
export async function verifySynthetic(dir: unknown, roots: GuardRoots = defaultRoots()) {
  const { realDir, session, path } = locateSynthetic(dir, roots);
  const ro = new DatabaseSync(path, { readOnly: true });
  try {
    const rows = ro.prepare("SELECT key, value FROM meta WHERE key IN ('dataset', 'sessione')").all() as {
      key: string;
      value: string;
    }[];
    assertMarker(Object.fromEntries(rows.map((r) => [r.key, r.value])), session);
    const v = ro.prepare("SELECT MAX(version) AS v FROM schema_migrations").get() as { v: number | null };
    return { realDir, path, session, schemaVersion: v.v ?? 0 };
  } catch (err) {
    if (err instanceof SyntheticGuardError) throw err;
    return fail("marcatore non verificabile");
  } finally {
    ro.close();
  }
}

export interface OpenSynthetic {
  db: NodeSqliteDb;
  ctx: Ctx;
  session: SessionInfo;
  realDir: string;
}

/** Unico modo di aprire in scrittura (e di migrare) una copia sintetica. */
export async function openSyntheticForWrite(
  dir: unknown,
  options: { roots?: GuardRoots; now?: () => string; newId?: () => string; migrate?: boolean } = {},
): Promise<OpenSynthetic> {
  const checked = await verifySynthetic(dir, options.roots);
  if (checked.session.stato !== "aperta") fail("sessione chiusa: nessuna scrittura");
  const db = new NodeSqliteDb(checked.path);
  try {
    assertMarker(await markerOf(db), checked.session); // stessa verifica sulla connessione in scrittura
  } catch (err) {
    db.close();
    throw err;
  }
  const ctx: Ctx = {
    db,
    now: options.now ?? (() => new Date().toISOString()),
    newId: options.newId ?? (() => randomUUID()),
  };
  if (options.migrate === true) await migrate(db, SYNTHETIC_MIGRATIONS, ctx.now);
  return { db, ctx, session: checked.session, realDir: checked.realDir };
}

/**
 * Crea una sessione NUOVA: la cartella non deve esistere (nessuna sovrascrittura) e il suo genitore deve stare nella
 * cartella temporanea dell'utente. Il database nasce vuoto in quella cartella appena creata, quindi le migrazioni della
 * creazione non possono toccare un file esistente; il marcatore si scrive subito dopo, prima di qualsiasi dato.
 */
export async function createEmptySession(
  dir: unknown,
  options: { roots?: GuardRoots; now?: () => string; migrations?: readonly Migration[] } = {},
): Promise<{ realDir: string; session: SessionInfo; db: NodeSqliteDb }> {
  const migrations = options.migrations ?? SYNTHETIC_MIGRATIONS;
  const roots = options.roots ?? defaultRoots();
  if (typeof dir !== "string" || dir.trim() === "") fail("cartella dati obbligatoria (--dati <percorso>)");
  if (!isAbsolute(dir)) fail(`percorso assoluto obbligatorio: ${dir}`);
  if (existsSync(dir) || isDanglingLink(dir))
    fail(`la cartella esiste gia': nessuna sovrascrittura (${dir})`);
  const parent = real(dirname(dir));
  assertPlace(parent, roots, true);
  mkdirSync(dir, { mode: 0o700 });
  const realDir = real(dir);
  assertPlace(realDir, roots, false);
  assertPrivate(realDir, "dir");
  const now = options.now ?? (() => new Date().toISOString());
  const session: SessionInfo = {
    schema: SESSION_SCHEMA,
    id: randomUUID(),
    creata: new Date().toISOString(),
    stato: "aperta",
    database: SYNTHETIC_DB,
    versioneSchema: migrations.length,
  };
  const path = join(realDir, SYNTHETIC_DB);
  if (existsSync(path)) fail("database gia' presente nella cartella nuova");
  writeFileSync(path, "", { mode: 0o600, flag: "wx" });
  const db = new NodeSqliteDb(path);
  await migrate(db, migrations, now);
  await db.batch([
    stmt("INSERT INTO meta (key, value) VALUES ('dataset', ?)", MARKER),
    stmt("INSERT INTO meta (key, value) VALUES ('sessione', ?)", session.id),
  ]);
  writeFileSync(join(realDir, SESSION_FILE), `${JSON.stringify(session, null, 1)}\n`, {
    mode: 0o600,
    flag: "wx",
  });
  return { realDir, session, db };
}

function isDanglingLink(path: string): boolean {
  try {
    return lstatSync(path).isSymbolicLink();
  } catch {
    return false;
  }
}

/** Fine della sessione: lo stato diventa `chiusa` (scritture rifiutate). Nessun file viene cancellato. */
export async function closeSession(dir: unknown, roots: GuardRoots = defaultRoots()): Promise<SessionInfo> {
  const checked = await verifySynthetic(dir, roots);
  const closed: SessionInfo = { ...checked.session, stato: "chiusa" };
  writeFileSync(join(checked.realDir, SESSION_FILE), `${JSON.stringify(closed, null, 1)}\n`, { mode: 0o600 });
  return closed;
}
