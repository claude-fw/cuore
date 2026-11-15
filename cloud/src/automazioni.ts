/**
 * Automazioni del Durable Object (C3, dati sintetici): Cron, coda e notifiche.
 * - Cron (`scheduled` del Worker, 4 volte al giorno in wrangler.locale.jsonc) → `pianifica`: un lavoro per tipo con
 *   chiave `tipo:AAAA-MM-GGTHH` (ora di Roma: stessa ora = stesso lavoro, mai due volte), poi esegue i lavori dovuti.
 * - Coda: `cloud_coda` (cosa e da quando) + esecutore B3 (`runJob`) sul deposito persistente `SqlJobStore` (stato,
 *   tentativi, lease, esito). Le riprove con attesa crescente le fa ripartire l'allarme del Durable Object: niente
 *   Queues, nessun costo in piu'.
 * - Lavori: `abbinamenti` (manuali ↔ banca), `abbinamenti_cattura` (catture Apple Pay ↔ banca, C3, mai conferma
 *   automatica), `budget` (mese corrente: budget superati → notifica), `push` (un invio per notifica), `backup` (L1:
 *   una copia cifrata al giorno nel bucket R2, ultime 7; solo se il bucket e' collegato: backup-cloud.ts).
 * - Notifiche: registrate sempre, una volta per chiave; il push parte solo con le notifiche ATTIVE (spente per difetto,
 *   decisione del 02/11), almeno un'iscrizione e una chiave VAPID (push.ts).
 * Il deposito e' un vero compare-and-set: nel Durable Object SQL e' sincrono e un evento non ne interrompe un altro.
 * Le operazioni dell'API (impostazioni, iscrizioni, riapertura) sono idempotenti per natura.
 */
import { type Ctx, NotFoundError, ValidationError } from "../../finance/src/context.ts";
import { type Db, stmt } from "../../finance/src/db/types.ts";
import { type JobRecord, type JobStore, reopenFailedJob, runJob } from "../../finance/src/jobs/executor.ts";
import { budgetStatus } from "../../finance/src/services/budgets.ts";
import { detectManualMatches } from "../../finance/src/services/manual-matching.ts";
import { APPUNTI_TABLE } from "./appunti.ts";
import { detectCatturaMatches } from "./cattura.ts";
import { DISPOSITIVI_TABLE } from "./notifiche.ts";
import { type Fonti, MOMENTI, momentOf, PROMEMORIA_TABLES, romeAt, runReminders } from "./promemoria.ts";
import { checkEndpoint, type VapidKey, vapidHeaders } from "./push.ts";

/** Cron storico delle automazioni (abbinamenti, budget, backup); gli altri Cron servono solo ai promemoria. */
export const BASE_CRON = "0 5,11,17,21 * * *";
/** Giri della banca alle 11 e alle 20 ora italiana (UTC: un orario per l'ora solare e uno per la legale). */
export const BANCA_CRON = "0 9,10,18,19 * * *";

export const AUTOMATION_TABLES = [
  "CREATE TABLE IF NOT EXISTS cloud_lavori (key TEXT PRIMARY KEY, version INTEGER NOT NULL, record TEXT NOT NULL) STRICT",
  "CREATE TABLE IF NOT EXISTS cloud_coda (key TEXT PRIMARY KEY, tipo TEXT NOT NULL, dopo INTEGER NOT NULL) STRICT",
  `CREATE TABLE IF NOT EXISTS cloud_notifiche (id TEXT PRIMARY KEY, chiave TEXT NOT NULL UNIQUE, creata TEXT NOT NULL,
    tipo TEXT NOT NULL, titolo TEXT NOT NULL, testo TEXT NOT NULL) STRICT`,
  "CREATE TABLE IF NOT EXISTS cloud_push (endpoint TEXT PRIMARY KEY, creata TEXT NOT NULL) STRICT",
  "CREATE TABLE IF NOT EXISTS cloud_impostazioni (chiave TEXT PRIMARY KEY, valore TEXT NOT NULL) STRICT",
  DISPOSITIVI_TABLE,
  APPUNTI_TABLE,
  ...PROMEMORIA_TABLES,
];

export class SqlJobStore implements JobStore {
  readonly #db: Db;

  constructor(db: Db) {
    this.#db = db;
  }

  async get(key: string): Promise<JobRecord | null> {
    const r = await this.#db.first<{ record: string }>("SELECT record FROM cloud_lavori WHERE key = ?", [
      key,
    ]);
    return r === null ? null : (JSON.parse(r.record) as JobRecord);
  }

  async create(rec: JobRecord): Promise<boolean> {
    if ((await this.get(rec.key)) !== null) return false;
    await this.#db.batch([
      stmt(
        "INSERT INTO cloud_lavori (key, version, record) VALUES (?, ?, ?)",
        rec.key,
        rec.version,
        JSON.stringify(rec),
      ),
    ]);
    return true;
  }

  async replace(key: string, expectedVersion: number, next: JobRecord): Promise<boolean> {
    const cur = await this.get(key);
    if (cur === null || cur.version !== expectedVersion || next.version !== expectedVersion + 1) return false;
    await this.#db.batch([
      stmt(
        "UPDATE cloud_lavori SET version = ?, record = ? WHERE key = ?",
        next.version,
        JSON.stringify(next),
        key,
      ),
    ]);
    return true;
  }
}

export interface Automazioni {
  ctx: Ctx;
  vapid: VapidKey | null;
  fetch: (url: string, init: RequestInit) => Promise<Response>;
  /** L1: scrive la copia del giorno (AAAA-MM-GG, ora di Roma); assente = nessun bucket, nessun lavoro `backup` */
  backup?: (giorno: string) => Promise<unknown>;
  /** promemoria online: fonti (feed School dal DO School, feed Money, archivio personale, appunti); assente = nessuno */
  fonti?: Fonti;
  /** C-05: movimenti dalla banca (banca.ts); assente = segreti BANCA_* non impostati, nessun lavoro `banca` */
  banca?: (giorno: string) => Promise<{ avvisi: string[] } & Record<string, unknown>>;
}

const notificationsOn = async (db: Db) =>
  (await db.first<{ valore: string }>("SELECT valore FROM cloud_impostazioni WHERE chiave = 'notifiche'"))
    ?.valore === "attive";

const eur = (minor: number) => `${(Math.abs(minor) / 100).toFixed(2).replace(".", ",")} €`;

export async function notify(
  a: Automazioni,
  n: { chiave: string; tipo: string; titolo: string; testo: string },
) {
  if ((await a.ctx.db.first("SELECT 1 AS x FROM cloud_notifiche WHERE chiave = ?", [n.chiave])) !== null)
    return;
  const id = a.ctx.newId();
  await a.ctx.db.batch([
    stmt(
      "INSERT INTO cloud_notifiche (id, chiave, creata, tipo, titolo, testo) VALUES (?, ?, ?, ?, ?, ?)",
      id,
      n.chiave,
      a.ctx.now(),
      n.tipo,
      n.titolo,
      n.testo,
    ),
    stmt("INSERT OR IGNORE INTO cloud_coda (key, tipo, dopo) VALUES (?, 'push', 0)", `push:${id}`),
  ]);
}

const HANDLERS: Record<string, (a: Automazioni, key: string) => Promise<unknown>> = {
  abbinamenti: (a) => detectManualMatches(a.ctx),
  abbinamenti_cattura: (a) => detectCatturaMatches(a.ctx),
  budget: async (a, key) => {
    const month = key.slice("budget:".length, "budget:".length + 7);
    const over = (await budgetStatus(a.ctx, month, "EUR")).lines.filter((l) => l.overBudget);
    for (const l of over)
      await notify(a, {
        chiave: `budget:${month}:${l.scope}:${l.target ?? "-"}`,
        tipo: "budget",
        titolo: "Budget superato",
        testo: `${l.target ?? "Totale"} ${month}: ${eur(l.spentMinor)} su ${eur(l.budgetMinor)}`,
      });
    return { mese: month, superati: over.length };
  },
  push: async (a) => {
    if (!(await notificationsOn(a.ctx.db))) return { inviati: 0, motivo: "notifiche_spente" };
    if (a.vapid === null) return { inviati: 0, motivo: "vapid_mancante" };
    let sent = 0;
    // iscrizioni per dispositivo (online) e quelle senza nome della vetrina C3 (`iscrivi_push`)
    const targets = await a.ctx.db.all<{ endpoint: string }>(
      "SELECT endpoint FROM cloud_dispositivi UNION SELECT endpoint FROM cloud_push",
    );
    for (const { endpoint } of targets) {
      const headers = await vapidHeaders(a.vapid, endpoint, Math.floor(Date.now() / 1000));
      const r = await a.fetch(endpoint, { method: "POST", headers });
      if (r.status === 404 || r.status === 410)
        await a.ctx.db.batch([
          stmt("DELETE FROM cloud_push WHERE endpoint = ?", endpoint),
          stmt("DELETE FROM cloud_dispositivi WHERE endpoint = ?", endpoint),
        ]);
      else if (!r.ok) throw new Error(`servizio push: ${r.status}`);
      else sent++;
    }
    return { inviati: sent };
  },
  // promemoria:AAAA-MM-GG:momento (un turno per momento e giorno, ora italiana)
  promemoria: async (a, key) => {
    const [, day, moment] = key.split(":");
    if (a.fonti === undefined) return { saltato: "fonti_assenti" };
    return runReminders(a.ctx.db, a.fonti, moment as string, Date.now(), async (notes) => {
      for (const n of notes)
        await notify(a, {
          chiave: `promemoria:${day}:${moment}:${n.gruppo}`,
          tipo: "promemoria",
          titolo: n.titolo,
          testo: n.testo,
        });
    });
  },
  backup: async (a, key) =>
    a.backup ? a.backup(key.slice("backup:".length)) : { saltato: "bucket_assente" },
  // banca:AAAA-MM-GG:turno (mattina e sera: la banca ammette 4 letture al giorno senza il titolare)
  banca: async (a, key) => {
    if (!a.banca) return { saltato: "banca_non_collegata" };
    const giorno = key.split(":")[1] as string;
    const r = await a.banca(giorno);
    for (const testo of r.avvisi)
      await notify(a, {
        chiave: `banca:${giorno}:${testo}`,
        tipo: "banca",
        titolo: "Collegamento con la banca",
        testo,
      });
    return r;
  },
};

function romeHour(ms: number): string {
  const p = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: "Europe/Rome",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(new Date(ms))
      .map((x) => [x.type, x.value]),
  );
  return `${p.year}-${p.month}-${p.day}T${p.hour}`;
}

/** Esegue i lavori dovuti (anche quelli aggiunti nel frattempo, es. i push); restituisce quando ripassare, o null. */
export async function runDue(a: Automazioni): Promise<number | null> {
  const opts = { store: new SqlJobStore(a.ctx.db), now: Date.now, owner: "durable-object" };
  for (let round = 0; round < 3; round++) {
    const due = await a.ctx.db.all<{ key: string; tipo: string }>(
      "SELECT key, tipo FROM cloud_coda WHERE dopo <= ? ORDER BY dopo, key",
      [Date.now()],
    );
    if (due.length === 0) break;
    for (const j of due) {
      const handler = HANDLERS[j.tipo];
      const out = handler && (await runJob(opts, j.key, j.tipo, () => handler(a, j.key)));
      const later =
        out?.status === "retry_scheduled" || out?.status === "waiting"
          ? out.nextAttemptAt
          : out?.status === "busy"
            ? out.leaseUntil
            : null;
      await a.ctx.db.batch([
        later === null
          ? stmt("DELETE FROM cloud_coda WHERE key = ?", j.key)
          : stmt("UPDATE cloud_coda SET dopo = ? WHERE key = ?", later, j.key),
      ]);
    }
  }
  return (
    (await a.ctx.db.first<{ dopo: number | null }>("SELECT MIN(dopo) AS dopo FROM cloud_coda"))?.dopo ?? null
  );
}

/**
 * «Aggiorna dalla banca» dall'app: al massimo 2 volte al giorno oltre ai due giri automatici (la banca ammette circa
 * 4 letture al giorno senza il titolare). Stesso lavoro `banca` dei giri automatici, registrato in Automazioni.
 */
export async function aggiornaBancaAMano(a: Automazioni, ms = Date.now()) {
  if (!a.banca) throw new ValidationError("la banca non è collegata");
  const day = romeHour(ms).slice(0, 10);
  const fatti = await a.ctx.db.first<{ n: number }>(
    "SELECT COUNT(*) AS n FROM cloud_lavori WHERE key LIKE ?",
    [`banca:${day}:a-mano-%`],
  );
  const n = (fatti?.n ?? 0) + 1;
  if (n > 2)
    throw new ValidationError(
      "oggi hai già aggiornato a mano 2 volte: la banca permette poche letture al giorno; il prossimo giro automatico arriva da solo",
    );
  const key = `banca:${day}:a-mano-${n}`;
  await a.ctx.db.batch([
    stmt("INSERT OR IGNORE INTO cloud_coda (key, tipo, dopo) VALUES (?, 'banca', 0)", key),
  ]);
  await runDue(a);
  const row = await a.ctx.db.first<{ record: string }>("SELECT record FROM cloud_lavori WHERE key = ?", [
    key,
  ]);
  const j = row ? (JSON.parse(row.record) as JobRecord) : null;
  return {
    chiave: key,
    stato: j?.state ?? "in_coda",
    esito: j?.result ?? null,
    errore: j?.lastError ?? null,
  };
}

/**
 * All'apertura dell'app (telefono, iPad o Mac): legge la banca solo se l'ultima lettura riuscita ha piu' di `ore` ore,
 * usando le stesse letture «a mano» (2 al giorno). Mai un errore per un salto: l'app mostra solo i dati che ha.
 */
export async function aggiornaBancaSeServe(a: Automazioni, ms = Date.now(), ore = 3) {
  if (!a.banca) return { saltato: "banca_non_collegata" };
  const ieri = romeHour(ms - 86_400_000).slice(0, 10);
  const lavori = await a.ctx.db.all<{ record: string }>(
    "SELECT record FROM cloud_lavori WHERE key >= ? AND key < 'banca;'",
    [`banca:${ieri}`],
  );
  const ultimo = Math.max(
    0,
    ...lavori
      .map((r) => JSON.parse(r.record) as JobRecord)
      .filter((j) => j.state === "succeeded")
      .map((j) => j.updatedAt),
  );
  if (ms - ultimo < ore * 3_600_000) return { saltato: "recente", ultimo: new Date(ultimo).toISOString() };
  try {
    return await aggiornaBancaAMano(a, ms);
  } catch (e) {
    if (e instanceof ValidationError) return { saltato: "limite", messaggio: e.message };
    throw e;
  }
}

/**
 * Momento dei promemoria di un turno del Cron: 07:15, 15:00 o 19:00 ora italiana, con al massimo un'ora di ritardo
 * (i Cron sono in UTC: due orari per momento, uno per l'ora solare e uno per l'ora legale; quello sbagliato non cade
 * nella finestra). null = nessun momento.
 */
export function promemoriaMoment(ms: number): string | null {
  const m = momentOf(ms);
  if (m === null) return null;
  const at = romeAt(romeHour(ms).slice(0, 10), MOMENTI[m] as string);
  return ms >= at && ms - at < 3_600_000 ? m : null;
}

/** Turno del Cron: mette in coda i lavori di quell'ora (idempotente) ed esegue i dovuti. `cron`: l'espressione che
 * ha fatto partire il turno; i lavori storici solo con quella storica (o senza indicazione, come prima). */
export async function plan(a: Automazioni, scheduledMs: number, cron?: string): Promise<number | null> {
  const hour = romeHour(scheduledMs);
  const jobs: [string, string][] =
    cron === undefined || cron === BASE_CRON
      ? ["abbinamenti", "abbinamenti_cattura", "budget"].map((t) => [`${t}:${hour}`, t])
      : [];
  // backup: uno al giorno (la chiave e' il giorno: i turni successivi trovano il lavoro gia' riuscito)
  if (a.backup !== undefined && jobs.length) jobs.push([`backup:${hour.slice(0, 10)}`, "backup"]);
  const h = hour.slice(11);
  if (a.banca !== undefined && cron === BANCA_CRON && (h === "11" || h === "20"))
    jobs.push([`banca:${hour.slice(0, 10)}:${h === "11" ? "mattina" : "sera"}`, "banca"]);
  const moment = a.fonti === undefined ? null : promemoriaMoment(scheduledMs);
  if (moment !== null) jobs.push([`promemoria:${hour.slice(0, 10)}:${moment}`, "promemoria"]);
  await a.ctx.db.batch(
    jobs.map(([k, t]) => stmt("INSERT OR IGNORE INTO cloud_coda (key, tipo, dopo) VALUES (?, ?, 0)", k, t)),
  );
  return runDue(a);
}

// biome-ignore lint/suspicious/noExplicitAny: ingresso JSON validato qui
type Op = (ctx: Ctx, i: Record<string, any>) => Promise<unknown>;

export const AUTOMATION_READS: Record<string, Op> = {
  notifiche: (ctx) =>
    ctx.db.all(
      "SELECT id, creata, tipo, titolo, testo FROM cloud_notifiche ORDER BY creata DESC, id LIMIT 50",
    ),
  stato_automazioni: async (ctx) => ({
    notifiche: (await notificationsOn(ctx.db)) ? "attive" : "spente",
    iscrizioni_push: (await ctx.db.first<{ n: number }>("SELECT COUNT(*) AS n FROM cloud_push"))?.n ?? 0,
    coda: await ctx.db.all("SELECT key AS chiave, tipo, dopo FROM cloud_coda ORDER BY dopo, key"),
    lavori: (
      await ctx.db.all<{ record: string }>("SELECT record FROM cloud_lavori ORDER BY key DESC LIMIT 50")
    ).map((r) => {
      const j = JSON.parse(r.record) as JobRecord;
      return {
        chiave: j.key,
        stato: j.state,
        tentativi: j.attempts,
        errore: j.lastError,
        esito: j.result,
        quando: new Date(j.updatedAt).toISOString(),
      };
    }),
  }),
};

export const AUTOMATION_WRITES: Record<string, Op> = {
  imposta_notifiche: async (ctx, i) => {
    if (typeof i.attive !== "boolean") throw new ValidationError("attive: true o false");
    const valore = i.attive ? "attive" : "spente";
    await ctx.db.batch([
      stmt(
        "INSERT INTO cloud_impostazioni (chiave, valore) VALUES ('notifiche', ?) ON CONFLICT (chiave) DO UPDATE SET valore = excluded.valore",
        valore,
      ),
    ]);
    return { notifiche: valore };
  },
  iscrivi_push: async (ctx, i) => {
    const endpoint = checkEndpoint(i.endpoint);
    await ctx.db.batch([
      stmt("INSERT OR IGNORE INTO cloud_push (endpoint, creata) VALUES (?, ?)", endpoint, ctx.now()),
    ]);
    return { iscritto: true };
  },
  disiscrivi_push: async (ctx, i) => {
    await ctx.db.batch([stmt("DELETE FROM cloud_push WHERE endpoint = ?", String(i.endpoint))]);
    return { iscritto: false };
  },
  riapri_lavoro: async (ctx, i) => {
    const key = String(i.chiave ?? "");
    if (typeof i.motivo !== "string" || i.motivo.trim() === "")
      throw new ValidationError("motivo obbligatorio");
    if (!(await reopenFailedJob(new SqlJobStore(ctx.db), key, Date.now(), i.motivo)))
      throw new NotFoundError("lavoro fallito", key);
    await ctx.db.batch([
      stmt(
        "INSERT OR IGNORE INTO cloud_coda (key, tipo, dopo) VALUES (?, ?, 0)",
        key,
        key.split(":")[0] ?? "",
      ),
    ]);
    return { riaperto: key };
  },
};
