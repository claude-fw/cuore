/**
 * ESECUTORE DI LAVORI IDEMPOTENTE (B3), indipendente dal runtime (Node, Durable Object, consumatore di una coda).
 *
 * Un lavoro e' identificato da una CHIAVE univoca scelta dal chiamante (es. `banca:conto:2026-03-01T07`): la stessa
 * chiave non viene mai eseguita due volte con successo. Stati:
 *
 *   queued ─► running ─► succeeded                 (risultato memorizzato: le chiamate successive lo restituiscono)
 *                 │  └─► retry_wait ─► running ... (errore temporaneo: attesa crescente, fino a `maxAttempts`)
 *                 └────► failed                    (errore permanente o tentativi esauriti: si riapre solo a mano)
 *
 * - Doppia elaborazione: chi esegue prende un LEASE (proprietario + scadenza) con un aggiornamento condizionato sulla
 *   versione del record (`replace` = compare-and-set): un secondo esecutore trova `busy`.
 * - Interruzioni: se il processo muore, il lease scade e il primo esecutore successivo riprende il lavoro (il tentativo
 *   interrotto conta). Se l'esecutore originale torna e prova a chiudere, il suo aggiornamento fallisce (`lease_lost`):
 *   l'esito resta quello di chi ha ripreso. Per questo gli effetti del gestore devono essere idempotenti a loro volta
 *   (nel motore Finance lo sono: id, impronte, `write_seq`).
 * - Il lease deve superare la durata massima del gestore (nessun rinnovo automatico in questa versione).
 * - Errori: `PermanentError` -> failed subito; qualsiasi altro errore e' trattato come temporaneo.
 * - Attese deterministiche (nessun valore casuale), per test ripetibili; il chiamante decide quando ripassare.
 */

export type JobState = "queued" | "running" | "retry_wait" | "succeeded" | "failed";

export interface JobEvent {
  at: number;
  event: string;
  detail?: string;
}

export interface JobRecord {
  key: string;
  type: string;
  state: JobState;
  attempts: number;
  maxAttempts: number;
  leaseOwner: string | null;
  leaseUntil: number | null;
  nextAttemptAt: number | null;
  result: unknown;
  lastError: string | null;
  history: JobEvent[];
  version: number;
  createdAt: number;
  updatedAt: number;
}

/** Deposito degli stati. `create` e `replace` devono essere atomici rispetto agli altri esecutori. */
export interface JobStore {
  get(key: string): Promise<JobRecord | null>;
  /** false se la chiave esiste gia' */
  create(record: JobRecord): Promise<boolean>;
  /** compare-and-set: scrive solo se la versione attuale e' `expectedVersion` (la nuova deve essere +1) */
  replace(key: string, expectedVersion: number, next: JobRecord): Promise<boolean>;
}

export class PermanentError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PermanentError";
  }
}

export interface ExecutorOptions {
  store: JobStore;
  /** istante corrente in millisecondi */
  now: () => number;
  /** identita' di questo esecutore (processo, istanza, messaggio) */
  owner: string;
  leaseMs?: number;
  maxAttempts?: number;
  /** attesa prima del tentativo successivo, dato il numero di tentativi gia' fatti */
  backoffMs?: (attempts: number) => number;
}

export type RunOutcome<T> =
  | { status: "succeeded"; result: T; cached: boolean; attempts: number }
  | { status: "busy"; leaseUntil: number }
  | { status: "waiting"; nextAttemptAt: number }
  | { status: "retry_scheduled"; nextAttemptAt: number; error: string; attempts: number }
  | { status: "failed"; error: string; attempts: number }
  | { status: "lease_lost" };

export const DEFAULT_LEASE_MS = 10 * 60_000;
export const DEFAULT_MAX_ATTEMPTS = 4;
const HISTORY_LIMIT = 50;

/** 30 s, 2 min, 8 min, 32 min, ... fino a 6 ore. */
export function defaultBackoffMs(attempts: number): number {
  return Math.min(30_000 * 4 ** Math.max(0, attempts - 1), 6 * 3_600_000);
}

function withEvent(rec: JobRecord, at: number, event: string, detail?: string): JobEvent[] {
  const h = [...rec.history, detail === undefined ? { at, event } : { at, event, detail }];
  return h.slice(-HISTORY_LIMIT);
}

function message(err: unknown): string {
  const text = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
  return text.slice(0, 300);
}

export async function runJob<T>(
  opts: ExecutorOptions,
  key: string,
  type: string,
  handler: (run: { key: string; attempt: number }) => Promise<T>,
): Promise<RunOutcome<T>> {
  if (key.trim() === "" || key.length > 200) throw new Error("chiave del lavoro non valida");
  const leaseMs = opts.leaseMs ?? DEFAULT_LEASE_MS;
  const maxAttempts = opts.maxAttempts ?? DEFAULT_MAX_ATTEMPTS;
  const backoff = opts.backoffMs ?? defaultBackoffMs;
  if (!(leaseMs > 0) || !Number.isInteger(maxAttempts) || maxAttempts < 1)
    throw new Error("opzioni non valide");

  for (let round = 0; round < 5; round++) {
    const now = opts.now();
    let rec = await opts.store.get(key);
    if (rec === null) {
      const fresh: JobRecord = {
        key,
        type,
        state: "queued",
        attempts: 0,
        maxAttempts,
        leaseOwner: null,
        leaseUntil: null,
        nextAttemptAt: null,
        result: null,
        lastError: null,
        history: [{ at: now, event: "creato", detail: opts.owner }],
        version: 1,
        createdAt: now,
        updatedAt: now,
      };
      if (!(await opts.store.create(fresh))) continue;
      rec = fresh;
    }
    if (rec.type !== type) throw new Error(`chiave ${key} gia' usata per un lavoro di tipo ${rec.type}`);
    if (rec.state === "succeeded") {
      return { status: "succeeded", result: rec.result as T, cached: true, attempts: rec.attempts };
    }
    if (rec.state === "failed")
      return { status: "failed", error: rec.lastError ?? "", attempts: rec.attempts };
    if (rec.state === "running" && (rec.leaseUntil ?? 0) > now) {
      return { status: "busy", leaseUntil: rec.leaseUntil as number };
    }
    if (rec.state === "retry_wait" && (rec.nextAttemptAt ?? 0) > now) {
      return { status: "waiting", nextAttemptAt: rec.nextAttemptAt as number };
    }
    let base = rec;
    if (rec.state === "running") {
      // lease scaduto: il tentativo precedente si e' interrotto
      const interrupted = `interrotto (proprietario ${rec.leaseOwner ?? "?"})`;
      if (rec.attempts >= rec.maxAttempts) {
        const dead: JobRecord = {
          ...rec,
          state: "failed",
          leaseOwner: null,
          leaseUntil: null,
          lastError: `${interrupted}; tentativi esauriti`,
          history: withEvent(rec, now, "fallito", `${interrupted}; tentativi esauriti`),
          version: rec.version + 1,
          updatedAt: now,
        };
        if (await opts.store.replace(key, rec.version, dead)) {
          return { status: "failed", error: dead.lastError as string, attempts: dead.attempts };
        }
        continue;
      }
      base = { ...rec, lastError: interrupted, history: withEvent(rec, now, "ripresa", interrupted) };
    }
    const claimed: JobRecord = {
      ...base,
      state: "running",
      attempts: base.attempts + 1,
      leaseOwner: opts.owner,
      leaseUntil: now + leaseMs,
      nextAttemptAt: null,
      history: withEvent(base, now, "avviato", `tentativo ${base.attempts + 1} (${opts.owner})`),
      version: rec.version + 1,
      updatedAt: now,
    };
    if (!(await opts.store.replace(key, rec.version, claimed))) continue;

    let next: JobRecord;
    let outcome: RunOutcome<T>;
    try {
      const result = await handler({ key, attempt: claimed.attempts });
      const end = opts.now();
      next = {
        ...claimed,
        state: "succeeded",
        result: result as unknown,
        lastError: null,
        leaseOwner: null,
        leaseUntil: null,
        history: withEvent(claimed, end, "riuscito"),
        version: claimed.version + 1,
        updatedAt: end,
      };
      outcome = { status: "succeeded", result, cached: false, attempts: claimed.attempts };
    } catch (err) {
      const end = opts.now();
      const error = message(err);
      const permanent = err instanceof PermanentError;
      if (permanent || claimed.attempts >= claimed.maxAttempts) {
        const why = permanent ? error : `${error}; tentativi esauriti`;
        next = {
          ...claimed,
          state: "failed",
          lastError: why,
          leaseOwner: null,
          leaseUntil: null,
          history: withEvent(claimed, end, "fallito", why),
          version: claimed.version + 1,
          updatedAt: end,
        };
        outcome = { status: "failed", error: why, attempts: claimed.attempts };
      } else {
        const at = end + backoff(claimed.attempts);
        next = {
          ...claimed,
          state: "retry_wait",
          lastError: error,
          nextAttemptAt: at,
          leaseOwner: null,
          leaseUntil: null,
          history: withEvent(claimed, end, "errore temporaneo", error),
          version: claimed.version + 1,
          updatedAt: end,
        };
        outcome = { status: "retry_scheduled", nextAttemptAt: at, error, attempts: claimed.attempts };
      }
    }
    if (!(await opts.store.replace(key, claimed.version, next))) return { status: "lease_lost" };
    return outcome;
  }
  throw new Error(`lavoro ${key}: troppe modifiche concorrenti`);
}

/** Riapre a mano un lavoro fallito (decisione esplicita, con motivo): torna in coda con i tentativi azzerati. */
export async function reopenFailedJob(
  store: JobStore,
  key: string,
  now: number,
  reason: string,
): Promise<boolean> {
  if (reason.trim() === "") throw new Error("motivo obbligatorio");
  const rec = await store.get(key);
  if (rec === null || rec.state !== "failed") return false;
  return store.replace(key, rec.version, {
    ...rec,
    state: "queued",
    attempts: 0,
    nextAttemptAt: null,
    history: withEvent(rec, now, "riaperto", reason.trim()),
    version: rec.version + 1,
    updatedAt: now,
  });
}

/** Deposito in memoria (test, prove locali). Copie profonde: nessun riferimento condiviso con il chiamante. */
export class MemoryJobStore implements JobStore {
  private readonly records = new Map<string, JobRecord>();

  async get(key: string): Promise<JobRecord | null> {
    const r = this.records.get(key);
    return r === undefined ? null : structuredClone(r);
  }

  async create(record: JobRecord): Promise<boolean> {
    if (this.records.has(record.key)) return false;
    this.records.set(record.key, structuredClone(record));
    return true;
  }

  async replace(key: string, expectedVersion: number, next: JobRecord): Promise<boolean> {
    const cur = this.records.get(key);
    if (cur === undefined || cur.version !== expectedVersion || next.version !== expectedVersion + 1)
      return false;
    this.records.set(key, structuredClone(next));
    return true;
  }
}
