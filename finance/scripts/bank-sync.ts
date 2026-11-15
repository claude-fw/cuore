#!/usr/bin/env node
/**
 * Collegamento bancario (Enable Banking, AIS in sola lettura): procedura del titolare e COLLAUDO SU COPIA.
 *
 *   node scripts/bank-sync.ts stato     --collegamenti <file.json> [--stato <file.json>]
 *   node scripts/bank-sync.ts banche    --collegamenti <file.json> --paese IT --conferma-rete
 *   node scripts/bank-sync.ts consenso  --collegamenti <file.json> --banca "<nome>" --paese IT [--giorni 180] --conferma-rete
 *   node scripts/bank-sync.ts sessione  --collegamenti <file.json> --codice <code> --conferma-rete
 *   node scripts/bank-sync.ts prova-su-copia --db <finance.sqlite3> --collegamenti <file.json> [--oggi YYYY-MM-DD]
 *        [--stato <file.json>] (--fonte-finta <file.json> | --conferma-rete) [--conserva]
 *   node scripts/bank-sync.ts sincronizza --db <finance.sqlite3> --collegamenti <file.json> --stato <file.json>
 *        --applica (--fonte-finta <file.json> | --conferma-rete) [--oggi YYYY-MM-DD] [--riprova]
 *
 * Regole:
 *   - nessuna chiamata di rete senza `--conferma-rete` (decisione esplicita del titolare a ogni comando);
 *   - nessun comando scrive nel database indicato: `prova-su-copia` copia i BYTE del file (rifiuta -wal non vuoto o
 *     -journal: scrittura in corso), sincronizza una copia in una cartella temporanea privata (0700), confronta con una
 *     seconda copia intatta (solo aggiunte ammesse) e verifica che l'originale sia identico prima e dopo (byte e stato
 *     del file). La copia viene eliminata salvo `--conserva`;
 *   - uscita: solo conti, date, esiti, categorie d'errore e conteggi; mai importi, descrizioni, IBAN completi o chiavi;
 *   - `sincronizza` aggiorna il database REALE solo con `--applica`, con le garanzie di `import-plan.ts --apply`:
 *     controlli in sola lettura, backup indipendente verificato, lavoro su una copia accanto al database, controlli
 *     (integrita', append-only, nessuna modifica concorrente), sostituzione atomica; ai problemi l'originale resta identico;
 *   - limiti della banca imposti qui, non dal chiamante: un conto si legge solo con consenso valido, non prima del tempo
 *     indicato dopo un errore, mai dopo un errore che richiede il titolare (salvo `--riprova`) e non piu' di una volta
 *     ogni 6 ore (stato obbligatorio per `sincronizza` e per `prova-su-copia` con la rete);
 *   - stato dei tentativi: letto in modo stretto (mancante senza `--primo-avvio`, malformato, non valido o illeggibile =
 *     BLOCCATO prima di qualsiasi chiamata), protetto da `<stato>.lock` (una sola esecuzione) e scritto in modo atomico
 *     (`<stato>.in-corso` esclusivo, poi rename). I conti da leggere vengono registrati `in_corso` PRIMA di contattare
 *     la banca: se la scrittura fallisce non parte nulla; se il programma si interrompe dopo, il tentativo conta;
 *   - i collaudi con `--fonte-finta` scrivono solo stati marcati `collaudo` e non toccano mai lo stato delle letture reali;
 *   - la chiave privata si legge dal file indicato nei collegamenti e non viene mai stampata ne' copiata.
 */
import { createHash } from "node:crypto";
import {
  chmodSync,
  closeSync,
  copyFileSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  mkdtempSync,
  openSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
  writeSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { parseArgs } from "node:util";
import { createCtx } from "../src/context.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import { NodeSqliteDb } from "../src/db/node-sqlite.ts";
import {
  type AttemptDecision,
  attemptDecision,
  type BankConnection,
  buildStatus,
  CONNECTIONS_SCHEMA,
  type ConnectionsConfig,
  consentState,
  markAttempts,
  parseConnections,
  parseStatus,
  privatePathProblems,
  type SyncStatus,
} from "../src/sync/connections.ts";
import { EnableBankingSource, type Transport } from "../src/sync/enable-banking.ts";
import { type SyncReport, syncAccounts } from "../src/sync/runner.ts";
import type { AccountDataSource, FetchStrategy, TransactionsPage } from "../src/sync/source.ts";
import { appendOnlyProblems, checksumOf, existingTargetProblems, snapshot, stamp } from "./import-plan.ts";

export interface Deps {
  out: (o: unknown) => void;
  now: () => Date;
  /** trasporto HTTP (test); predefinito: fetch */
  transport?: Transport;
  /** scrittura del file temporaneo dello stato (test: guasti e interruzioni); predefinita: writeSync */
  writeStatus?: (fd: number, text: string) => void;
}

const REPO = resolve(dirname(new URL(import.meta.url).pathname), "..", "..");

/** Fonte da file JSON (collaudo senza rete): { "<accountRef>": { pages: [{transactions, continuationKey}], balances } }. */
class FixtureSource implements AccountDataSource {
  readonly provider = "enable-banking";
  readonly data: Record<string, { pages: TransactionsPage[]; balances: unknown[] }>;
  constructor(data: Record<string, { pages: TransactionsPage[]; balances: unknown[] }>) {
    this.data = data;
  }
  async transactions(
    ref: string,
    _f: string,
    _t: string,
    key: string | null,
    _s?: FetchStrategy,
  ): Promise<TransactionsPage> {
    const pages = this.data[ref]?.pages ?? [{ transactions: [], continuationKey: null }];
    return pages[key === null ? 0 : Number(key)] ?? { transactions: [], continuationKey: null };
  }
  async balances(ref: string): Promise<readonly unknown[]> {
    return this.data[ref]?.balances ?? [];
  }
}

function fileState(p: string): string | null {
  if (!existsSync(p)) return null;
  const s = statSync(p, { bigint: true });
  return `${s.size}:${s.mtimeNs}:${s.ctimeNs}:${s.ino}`;
}

const sha = (p: string) => createHash("sha256").update(readFileSync(p)).digest("hex");

/** Copia dei byte senza aprire SQLite sull'originale; rifiuta scritture in corso o un file cambiato durante la copia. */
export function copyBytes(src: string, dest: string): string[] {
  if (!existsSync(src)) return ["database inesistente"];
  if ((fileState(`${src}-wal`) ?? "0:").split(":")[0] !== "0")
    return ["-wal non vuoto: scrittura in corso o non consolidata"];
  if (existsSync(`${src}-journal`)) return ["-journal presente: scrittura in corso o interrotta"];
  const before = fileState(src);
  copyFileSync(src, dest);
  chmodSync(dest, 0o600);
  if (fileState(src) !== before) return ["database cambiato durante la copia"];
  return [];
}

function loadConfig(path: string | undefined): { config: ConnectionsConfig | null; problems: string[] } {
  if (!path) return { config: null, problems: ["--collegamenti obbligatorio"] };
  const p = privatePathProblems(path, REPO, "collegamenti");
  if (p.length > 0) return { config: null, problems: p };
  if (!existsSync(path)) return { config: null, problems: ["collegamenti: file inesistente"] };
  return parseConnections(readFileSync(path, "utf8"), REPO);
}

function provider(config: ConnectionsConfig, deps: Deps): EnableBankingSource {
  return new EnableBankingSource({
    appId: config.appId,
    privateKeyPem: readFileSync(config.keyFile, "utf8"),
    ...(deps.transport ? { transport: deps.transport } : {}),
  });
}

/** Solo il codice di sistema (EACCES, ENOSPC...): mai percorsi, messaggi completi o contenuto. */
const errCode = (e: unknown): string =>
  e && typeof e === "object" && "code" in e && typeof e.code === "string" && /^E[A-Z0-9]+$/.test(e.code)
    ? e.code
    : "errore";

/**
 * Stato dei tentativi in lettura stretta. `missing`: «vietato» (letture reali), «primo-avvio» (deve mancare: primo
 * collegamento), «ammesso» (collaudo con fonte finta o semplice consultazione). Un temporaneo `.in-corso` rimasto da
 * una scrittura interrotta blocca: il titolare verifica e lo rimuove.
 */
function loadStatus(
  path: string,
  now: Date,
  missing: "vietato" | "primo-avvio" | "ammesso",
): { status: SyncStatus | null; problems: string[] } {
  const p = privatePathProblems(path, REPO, "stato");
  if (p.length > 0) return { status: null, problems: p };
  if (existsSync(`${path}.in-corso`))
    return {
      status: null,
      problems: [
        "stato: file temporaneo .in-corso presente (scrittura interrotta): verificarlo e rimuoverlo",
      ],
    };
  let text: string;
  try {
    text = readFileSync(path, "utf8");
  } catch (e) {
    if (errCode(e) !== "ENOENT") return { status: null, problems: [`stato: non leggibile (${errCode(e)})`] };
    return missing === "vietato"
      ? {
          status: null,
          problems: ["stato: file inesistente (solo per il primo collegamento: --primo-avvio)"],
        }
      : { status: null, problems: [] };
  }
  if (missing === "primo-avvio")
    return {
      status: null,
      problems: ["--primo-avvio: lo stato esiste gia' e i suoi tentativi restano validi"],
    };
  return parseStatus(text, now);
}

/** Una sola esecuzione per file di stato; un `.lock` rimasto da un'interruzione blocca finche' il titolare lo rimuove. */
function acquireLock(path: string): { release: () => void } | string {
  const lock = `${path}.lock`;
  try {
    const fd = openSync(lock, "wx", 0o600);
    writeSync(fd, `${process.pid}\n`);
    closeSync(fd);
  } catch (e) {
    return errCode(e) === "EEXIST"
      ? "stato: .lock presente (altra esecuzione in corso o interrotta): verificare che nessuna sia attiva, poi rimuoverlo"
      : `stato: cartella non scrivibile (${errCode(e)})`;
  }
  return { release: () => rmSync(lock, { force: true }) };
}

/** Scrittura atomica: temporaneo esclusivo 0600, fsync, rename. Un errore non lascia mai uno stato parziale al suo posto. */
function writePrivate(path: string, obj: unknown, write: Deps["writeStatus"]): string | null {
  const tmp = `${path}.in-corso`;
  let fd: number | null = null;
  try {
    fd = openSync(tmp, "wx", 0o600);
    (write ?? ((f, t) => writeSync(f, t)))(fd, `${JSON.stringify(obj, null, 1)}\n`);
    fsyncSync(fd);
    closeSync(fd);
    fd = null;
    renameSync(tmp, path);
  } catch (e) {
    if (fd !== null) closeSync(fd);
    if (errCode(e) !== "EEXIST") rmSync(tmp, { force: true }); // il temporaneo di un altro non si tocca
    return `stato non scrivibile (${errCode(e)})`;
  }
  try {
    const dfd = openSync(dirname(path), "r");
    fsyncSync(dfd);
    closeSync(dfd);
  } catch {
    // fsync della cartella non disponibile: il rename resta atomico
  }
  return null;
}

/** Stato di collaudo (fonte finta) e stato delle letture reali non si mescolano mai. */
function modeProblems(status: SyncStatus | null, fixture: boolean): string[] {
  if (status === null) return [];
  if (fixture && status.collaudo !== true)
    return ["stato di letture reali: per i collaudi con --fonte-finta usare un file di stato separato"];
  if (!fixture && status.collaudo === true)
    return [
      "stato di collaudo (fonte finta): non vale per i limiti della banca; usare lo stato delle letture reali",
    ];
  return [];
}

const asTest = (s: SyncStatus, fixture: boolean): SyncStatus => (fixture ? { ...s, collaudo: true } : s);

/** Giorno di calendario a Roma (le banche datano i movimenti in ora locale). */
export const romeDay = (d: Date) => new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Rome" }).format(d);

/** Conti da leggere adesso e conti saltati con il motivo (regole in `attemptDecision`). */
function dueConnections(
  config: ConnectionsConfig,
  status: SyncStatus | null,
  now: Date,
  retry: boolean,
): { due: BankConnection[]; skipped: { conto: string; motivo: string; non_prima_di: string | null }[] } {
  const due: BankConnection[] = [];
  const skipped: { conto: string; motivo: string; non_prima_di: string | null }[] = [];
  for (const c of config.connections) {
    const d: AttemptDecision = attemptDecision(
      c,
      status?.connessioni.find((x) => x.conto === c.accountId) ?? null,
      now,
      retry,
    );
    if (d.due) due.push(c);
    else skipped.push({ conto: c.accountId, motivo: d.reason, non_prima_di: d.notBefore });
  }
  return { due, skipped };
}

const summary = (r: SyncReport) => ({
  conto: r.accountId,
  esito: r.status,
  errore: r.error?.kind ?? null,
  riprova_dopo_ore: r.retryAfterHours,
  finestra: r.window,
  buco: r.gap,
  strategia: r.strategy,
  pagine: r.pages,
  letti: r.fetched,
  saldi_letti: r.balancesRead,
  importazione: r.import && {
    stato: r.import.status,
    nuovi: r.import.counts.created,
    aggiornati: r.import.counts.updated,
    invariati: r.import.counts.unchanged,
    esclusi: r.import.counts.excluded,
    saldi: r.import.counts.balancesCreated,
    problemi: r.import.issues,
  },
  collegamenti_proposti: r.links,
});

export async function main(argv: string[], deps: Deps): Promise<number> {
  const { values: v, positionals } = parseArgs({
    args: argv,
    allowPositionals: true,
    options: {
      collegamenti: { type: "string" },
      stato: { type: "string" },
      paese: { type: "string" },
      banca: { type: "string" },
      giorni: { type: "string" },
      codice: { type: "string" },
      db: { type: "string" },
      oggi: { type: "string" },
      "fonte-finta": { type: "string" },
      "conferma-rete": { type: "boolean" },
      conserva: { type: "boolean" },
      applica: { type: "boolean" },
      riprova: { type: "boolean" },
      "primo-avvio": { type: "boolean" },
    },
  });
  const cmd = positionals[0];
  const { config, problems } = loadConfig(v.collegamenti);
  if (config === null) {
    deps.out({ esito: "BLOCCATO", problemi: problems, schema_atteso: CONNECTIONS_SCHEMA });
    return 64;
  }
  const net = (what: string) => {
    if (v["conferma-rete"]) return true;
    deps.out({
      esito: "BLOCCATO",
      problemi: [`${what}: richiede --conferma-rete (chiamata reale al fornitore)`],
    });
    return false;
  };

  if (cmd === "stato") {
    const loaded =
      typeof v.stato === "string"
        ? loadStatus(v.stato, deps.now(), "ammesso")
        : { status: null, problems: [] };
    const st = loaded.status;
    deps.out({
      collegamenti: config.connections.map((c) => ({
        conto: c.accountId,
        banca: c.aspsp,
        consenso: consentState(c.validUntil, deps.now()),
        ultimo_tentativo: st?.connessioni.find((x) => x.conto === c.accountId)?.ultimo_tentativo ?? null,
      })),
      ...(loaded.problems.length > 0 ? { esito: "STATO NON VALIDO", problemi: loaded.problems } : {}),
    });
    return loaded.problems.length > 0 ? 1 : 0;
  }
  if (cmd === "banche") {
    if (!v.paese || !net("elenco delle banche")) return 64;
    const all = await provider(config, deps).aspsps(v.paese);
    deps.out({
      paese: v.paese,
      banche: all.length,
      rilevanti: all.filter((a) => /revolut|paschi|\bmps\b/i.test(a.name)),
    });
    return 0;
  }
  if (cmd === "consenso") {
    if (!v.banca || !v.paese || !net("avvio del consenso")) return 64;
    const days = Number(v.giorni ?? "180");
    if (!Number.isInteger(days) || days < 1 || days > 180) {
      deps.out({ esito: "BLOCCATO", problemi: ["--giorni: intero da 1 a 180"] });
      return 64;
    }
    const until = new Date(deps.now().getTime() + days * 86_400_000).toISOString();
    const r = await provider(config, deps).startAuthorization({
      aspspName: v.banca,
      country: v.paese,
      redirectUrl: config.redirectUrl,
      validUntil: until,
      state: createHash("sha256").update(`${deps.now().toISOString()}${v.banca}`).digest("hex").slice(0, 16),
    });
    deps.out({
      apri_nel_browser: r.url,
      scadenza_richiesta: until,
      poi: "sessione --codice <code dal redirect>",
    });
    return 0;
  }
  if (cmd === "sessione") {
    if (!v.codice || !net("creazione della sessione")) return 64;
    const s = await provider(config, deps).createSession(v.codice);
    deps.out({
      sessione: "creata",
      scade: s.validUntil,
      conti: s.accounts.map((a) => ({
        uid: a.uid,
        valuta: a.currency,
        iban_finale: a.ibanLast4,
        nome: a.name,
      })),
      poi: "copiare uid, sessionId e validUntil nel file dei collegamenti, associando ogni uid a un conto Finance",
      sessionId: s.sessionId,
    });
    return 0;
  }
  if (cmd === "prova-su-copia") return proofOnCopy(v, config, deps);
  if (cmd === "sincronizza") return syncReal(v, config, deps);
  deps.out({
    esito: "BLOCCATO",
    problemi: ["comando: stato | banche | consenso | sessione | prova-su-copia | sincronizza"],
  });
  return 64;
}

async function proofOnCopy(
  v: Record<string, string | boolean | undefined>,
  config: ConnectionsConfig,
  deps: Deps,
): Promise<number> {
  const db = typeof v.db === "string" ? resolve(v.db) : null;
  const today = typeof v.oggi === "string" ? v.oggi : romeDay(deps.now());
  if (db === null || !/^\d{4}-\d\d-\d\d$/.test(today)) {
    deps.out({
      esito: "BLOCCATO",
      problemi: ["--db obbligatorio (percorso del database da copiare); --oggi YYYY-MM-DD"],
    });
    return 64;
  }
  const fixture = typeof v["fonte-finta"] === "string" ? v["fonte-finta"] : null;
  if (fixture === null && !v["conferma-rete"]) {
    deps.out({
      esito: "BLOCCATO",
      problemi: ["senza --fonte-finta serve --conferma-rete (lettura reale dalla banca)"],
    });
    return 64;
  }
  const statusPath = typeof v.stato === "string" ? v.stato : null;
  if (fixture === null && statusPath === null) {
    deps.out({
      esito: "BLOCCATO",
      problemi: [
        "con --conferma-rete serve --stato (le letture del collaudo contano per i limiti della banca)",
      ],
    });
    return 64;
  }
  if (statusPath === null) return proofLocked(v, config, deps, db, today, fixture, null, null);
  const sp = privatePathProblems(statusPath, REPO, "stato");
  if (sp.length > 0) {
    deps.out({ esito: "BLOCCATO", database: "non toccato", problemi: sp });
    return 1;
  }
  const lock = acquireLock(statusPath);
  if (typeof lock === "string") {
    deps.out({ esito: "BLOCCATO", database: "non toccato", problemi: [lock] });
    return 1;
  }
  try {
    const loaded = loadStatus(
      statusPath,
      deps.now(),
      fixture !== null ? "ammesso" : v["primo-avvio"] ? "primo-avvio" : "vietato",
    );
    const problems = [...loaded.problems, ...modeProblems(loaded.status, fixture !== null)];
    if (problems.length > 0) {
      deps.out({ esito: "BLOCCATO", database: "non toccato", problemi: problems });
      return 1;
    }
    return await proofLocked(v, config, deps, db, today, fixture, statusPath, loaded.status);
  } finally {
    lock.release();
  }
}

async function proofLocked(
  v: Record<string, string | boolean | undefined>,
  config: ConnectionsConfig,
  deps: Deps,
  db: string,
  today: string,
  fixture: string | null,
  statusPath: string | null,
  previous: SyncStatus | null,
): Promise<number> {
  const source: AccountDataSource =
    fixture !== null ? new FixtureSource(JSON.parse(readFileSync(fixture, "utf8"))) : provider(config, deps);
  // letture reali: stessi limiti della sincronizzazione (le letture del collaudo contano per la banca)
  const plan =
    fixture === null
      ? dueConnections(config, previous, deps.now(), v.riprova === true)
      : { due: config.connections, skipped: [] };
  const shaStart = existsSync(db) ? sha(db) : null;
  const stateStart = fileState(db);
  const dir = mkdtempSync(join(tmpdir(), "cuore-banca-prova-"));
  chmodSync(dir, 0o700);
  try {
    const base = join(dir, "originale.sqlite3");
    const work = join(dir, "copia.sqlite3");
    const p = copyBytes(db, base);
    if (p.length === 0) copyFileSync(base, work);
    if (p.length > 0) {
      deps.out({ esito: "BLOCCATO prima di copiare", database: "non toccato", problemi: p });
      return 1;
    }
    const conn = new NodeSqliteDb(work);
    let reports: SyncReport[];
    let marked: SyncStatus | null = null;
    try {
      const v2 = await conn.first<{ v: number | null }>("SELECT MAX(version) AS v FROM schema_migrations");
      const accounts = new Set((await conn.all<{ id: string }>("SELECT id FROM accounts")).map((a) => a.id));
      const missing = config.connections.filter((c) => !accounts.has(c.accountId)).map((c) => c.accountId);
      if (v2?.v !== MIGRATIONS.length || missing.length > 0) {
        deps.out({
          esito: "BLOCCATO",
          problemi: [
            ...(v2?.v !== MIGRATIONS.length
              ? [`schema alla versione ${v2?.v}, attesa ${MIGRATIONS.length}`]
              : []),
            ...missing.map((m) => `conto Finance inesistente: ${m}`),
          ],
        });
        return 1;
      }
      // letture reali: il tentativo si registra prima di contattare la banca; se non si puo' scrivere, non parte nulla
      if (fixture === null && statusPath !== null && plan.due.length > 0) {
        marked = markAttempts(
          config,
          previous,
          plan.due.map((c) => c.accountId),
          deps.now(),
          previous?.database ?? "copia",
        );
        const w = writePrivate(statusPath, marked, deps.writeStatus);
        if (w !== null) {
          deps.out({
            esito: "BLOCCATO prima di leggere",
            database_originale: "non toccato",
            problemi: [w, "nessun contatto con la banca"],
          });
          return 1;
        }
      }
      reports = await syncAccounts(
        createCtx(conn),
        plan.due.map((c) => ({ accountId: c.accountId, accountRef: c.accountRef, source, today })),
      );
    } finally {
      conn.close();
    }
    const ao = await appendOnlyProblems(base, work);
    const untouched = shaStart === (existsSync(db) ? sha(db) : null) && stateStart === fileState(db);
    const failures = [
      ...ao.problems,
      ...(untouched ? [] : ["DATABASE ORIGINALE CAMBIATO durante il collaudo"]),
      ...reports
        .filter((r) => r.status === "interrotta" || r.status === "rifiutata")
        .map((r) => `${r.accountId}: ${r.status}`),
    ];
    if (statusPath !== null) {
      const st = buildStatus(config, reports, deps.now(), previous?.database ?? "copia", marked ?? previous);
      const w = writePrivate(statusPath, asTest(st, fixture !== null), deps.writeStatus);
      if (w !== null)
        failures.push(
          `${w} dopo le letture: resta il tentativo «in_corso», che vale per l'intervallo minimo`,
        );
    }
    deps.out({
      esito: failures.length === 0 ? "COLLAUDO SU COPIA RIUSCITO" : "COLLAUDO SU COPIA CON PROBLEMI",
      database_originale: untouched ? "invariato (byte e stato del file)" : "CAMBIATO",
      fonte: fixture !== null ? "finta (file)" : "Enable Banking (rete)",
      conti: reports.map(summary),
      saltati: plan.skipped,
      delta_copia: ao.delta,
      problemi: failures,
      copia: v.conserva ? work : "eliminata",
    });
    return failures.length === 0 ? 0 : 1;
  } finally {
    if (!v.conserva) rmSync(dir, { recursive: true, force: true });
  }
}

/**
 * Sincronizzazione del database REALE (solo con `--applica`). Stessa sequenza di `import-plan.ts --apply`: l'originale
 * non viene mai aperto in scrittura; si lavora su `<db>.in-corso` (stessa cartella) e lo si sostituisce atomicamente solo
 * se tutti i controlli passano. Le letture avvenute restano nello stato anche quando l'aggiornamento viene bloccato.
 */
async function syncReal(
  v: Record<string, string | boolean | undefined>,
  config: ConnectionsConfig,
  deps: Deps,
): Promise<number> {
  const target = typeof v.db === "string" ? v.db : null;
  const statusPath = typeof v.stato === "string" ? v.stato : null;
  const today = typeof v.oggi === "string" ? v.oggi : romeDay(deps.now());
  const fixture = typeof v["fonte-finta"] === "string" ? v["fonte-finta"] : null;
  const usage = [
    ...(target === null ? ["--db obbligatorio (database da aggiornare)"] : []),
    ...(statusPath === null
      ? ["--stato obbligatorio (registra i tentativi e applica i limiti della banca)"]
      : []),
    ...(/^\d{4}-\d\d-\d\d$/.test(today) ? [] : ["--oggi YYYY-MM-DD"]),
    ...(v.applica ? [] : ["sincronizza: richiede --applica (scrittura nel database indicato)"]),
    ...(fixture === null && !v["conferma-rete"]
      ? ["senza --fonte-finta serve --conferma-rete (lettura reale dalla banca)"]
      : []),
  ];
  if (target === null || statusPath === null || usage.length > 0) {
    deps.out({ esito: "BLOCCATO", database: "non toccato", problemi: usage });
    return 64;
  }
  const blocked = (problemi: string[]) => {
    deps.out({ esito: "BLOCCATO prima di leggere", database: "invariato", problemi });
    return 1;
  };
  const sp = privatePathProblems(statusPath, REPO, "stato");
  if (sp.length > 0) return blocked(sp);
  const tp = await existingTargetProblems(target, REPO);
  if (tp.length > 0) return blocked(tp);
  const ro = new NodeSqliteDb(target, { readOnly: true });
  let missing: string[];
  try {
    const accounts = new Set((await ro.all<{ id: string }>("SELECT id FROM accounts")).map((a) => a.id));
    missing = config.connections.filter((c) => !accounts.has(c.accountId)).map((c) => c.accountId);
  } finally {
    ro.close();
  }
  if (missing.length > 0) return blocked(missing.map((m) => `conto Finance inesistente: ${m}`));

  const lock = acquireLock(statusPath);
  if (typeof lock === "string") return blocked([lock]);
  try {
    const loaded = loadStatus(statusPath, deps.now(), v["primo-avvio"] ? "primo-avvio" : "vietato");
    const problems = [...loaded.problems, ...modeProblems(loaded.status, fixture !== null)];
    if (problems.length > 0) return blocked(problems);
    return await syncLocked(v, config, deps, target, statusPath, today, fixture, loaded.status, blocked);
  } finally {
    lock.release();
  }
}

async function syncLocked(
  v: Record<string, string | boolean | undefined>,
  config: ConnectionsConfig,
  deps: Deps,
  target: string,
  statusPath: string,
  today: string,
  fixture: string | null,
  previous: SyncStatus | null,
  blocked: (problemi: string[]) => number,
): Promise<number> {
  const test = fixture !== null;
  const plan = dueConnections(config, previous, deps.now(), v.riprova === true);
  if (plan.due.length === 0) {
    // nessuna lettura: lo stato si riscrive solo per ricalcolare i consensi, i tentativi precedenti restano
    const st = buildStatus(config, [], deps.now(), previous?.database ?? "reale", previous);
    const w = writePrivate(statusPath, asTest(st, test), deps.writeStatus);
    deps.out({
      esito:
        w === null ? "NESSUN CONTO DA SINCRONIZZARE" : "NESSUN CONTO DA SINCRONIZZARE, STATO NON AGGIORNATO",
      database: "invariato",
      saltati: plan.skipped,
      ...(w === null ? {} : { problemi: [w] }),
    });
    return w === null ? 0 : 1;
  }
  const source: AccountDataSource =
    fixture !== null ? new FixtureSource(JSON.parse(readFileSync(fixture, "utf8"))) : provider(config, deps);
  const checksumStart = await checksumOf(target);
  const previousUmask = process.umask(0o077);
  const work = `${target}.in-corso`;
  try {
    // 1. backup indipendente, fuori dalla cartella del database, verificato
    const base = dirname(dirname(resolve(target)));
    const backupDir = join(base, "backup");
    mkdirSync(backupDir, { recursive: true, mode: 0o700 });
    const backup = join(backupDir, `${basename(target, ".sqlite3")}-prima-di-sync-${stamp()}.sqlite3`);
    await snapshot(target, backup);
    if ((await checksumOf(backup)) !== checksumStart) return blocked(["backup diverso dal database"]);
    // 2. letture e importazioni su una copia di lavoro; il tentativo si registra PRIMA di contattare la banca
    await snapshot(target, work);
    const marked = asTest(
      markAttempts(
        config,
        previous,
        plan.due.map((c) => c.accountId),
        deps.now(),
        "reale",
      ),
      test,
    );
    const w0 = writePrivate(statusPath, marked, deps.writeStatus);
    if (w0 !== null) {
      rmSync(work, { force: true });
      return blocked([w0, "nessun contatto con la banca"]);
    }
    const conn = new NodeSqliteDb(work);
    let reports: SyncReport[];
    const failures: string[] = [];
    const statusAfter = (applied: boolean) => {
      const st = buildStatus(config, reports, deps.now(), "reale", marked, applied);
      const w = writePrivate(statusPath, asTest(st, test), deps.writeStatus);
      return w === null
        ? []
        : [`${w} dopo le letture: resta il tentativo «in_corso», che vale per l'intervallo minimo`];
    };
    try {
      reports = await syncAccounts(
        createCtx(conn),
        plan.due.map((c) => ({ accountId: c.accountId, accountRef: c.accountRef, source, today })),
      );
      await conn.all("PRAGMA journal_mode = DELETE");
      const integrity = await conn.first<{ integrity_check: string }>("PRAGMA integrity_check");
      if (integrity?.integrity_check !== "ok") failures.push("integrity_check non ok");
    } finally {
      conn.close();
    }
    // 3. controlli: nessun WAL, solo aggiunte, nessuna modifica concorrente dell'originale
    if (existsSync(`${work}-wal`)) failures.push("file WAL rimasto dopo la chiusura");
    const ao = await appendOnlyProblems(target, work);
    failures.push(...ao.problems);
    if ((await checksumOf(target)) !== checksumStart)
      failures.push("il database e' cambiato durante la sincronizzazione");
    const notSynced = reports.filter((r) => r.status === "interrotta" || r.status === "rifiutata");
    if (failures.length > 0) {
      const failed = `${target}.fallito-${stamp()}`;
      if (existsSync(work)) renameSync(work, failed);
      deps.out({
        esito: "BLOCCATO",
        database: "invariato",
        backup,
        fileConservatoPerAnalisi: failed,
        problemi: [...failures, ...statusAfter(false)],
        conti: reports.map(summary),
      });
      return 1;
    }
    // 4. sostituzione atomica solo se qualcosa e' cambiato (letture tutte interrotte = nessuna scrittura)
    const checksumAfter = await checksumOf(work);
    const changed = checksumAfter !== checksumStart;
    if (changed) renameSync(work, target);
    else rmSync(work);
    const statusProblems = statusAfter(true);
    const final = {
      esito:
        notSynced.length > 0
          ? "AGGIORNATO IN PARTE (conti non sincronizzati)"
          : statusProblems.length > 0
            ? "AGGIORNATO, STATO DEI TENTATIVI NON AGGIORNATO"
            : ao.delta.transactions === 0 && ao.delta.balance_observations === 0
              ? "AGGIORNATO (nessun dato nuovo)"
              : "AGGIORNATO",
      database: changed ? "aggiornato (sostituzione atomica)" : "invariato",
      backupPrima: backup,
      improntaPrima: checksumStart,
      improntaDopo: checksumAfter,
      fonte: fixture !== null ? "finta (file)" : "Enable Banking (rete)",
      conti: reports.map(summary),
      saltati: plan.skipped,
      delta: ao.delta,
      ...(statusProblems.length > 0 ? { problemi: statusProblems } : {}),
    };
    const reportDir = join(base, "report");
    mkdirSync(reportDir, { recursive: true, mode: 0o700 });
    const reportPath = join(reportDir, `banca-${stamp()}.json`);
    writeFileSync(reportPath, `${JSON.stringify(final, null, 2)}\n`, { mode: 0o600 });
    deps.out({ ...final, rapporto: reportPath });
    return notSynced.length > 0 || statusProblems.length > 0 ? 1 : 0;
  } finally {
    process.umask(previousUmask);
  }
}

if (import.meta.main) {
  main(process.argv.slice(2), {
    out: (o) => process.stdout.write(`${JSON.stringify(o, null, 1)}\n`),
    now: () => new Date(),
  }).then(
    (code) => {
      process.exitCode = code;
    },
    (err: unknown) => {
      // solo il tipo e la categoria: mai il messaggio completo, che potrebbe contenere dati
      const kind =
        err && typeof err === "object" && "kind" in err ? String((err as { kind: unknown }).kind) : null;
      const code = errCode(err);
      process.stderr.write(
        `errore: ${err instanceof Error ? err.name : "sconosciuto"}${kind ? ` (${kind})` : ""}${code !== "errore" ? ` [${code}]` : ""}\n`,
      );
      process.exitCode = 2;
    },
  );
}
