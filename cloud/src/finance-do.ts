/**
 * Durable Object «Finance» della VETRINA: il motore Finance invariato su SQLite del Durable Object, caricato SOLO con
 * gli esempi sintetici del repository (`finance/examples/sintetici`) e marcato `meta.dataset = 'sintetico'`, con lo
 * schema della copia sintetica (attive + in attesa, come il ponte di prova). Nessuna importazione reale, nessuna rete
 * salvo i push (C3). Rotte: `GET /stato` (conteggi e prova della consultazione), `GET /money` (feed Money della pagina
 * online, online.ts), `POST /esegui` (API C2,
 * finance-api.ts, con le operazioni delle automazioni) e `POST /cron` (turno del Cron, automazioni.ts); `alarm()` riprende
 * la coda. Con il bucket R2 `BACKUP` collegato il Cron scrive anche la copia giornaliera L1 (backup-cloud.ts, ultime 7).
 *
 * Con `CUORE_FINANCE_ORIGINE` = "mac" (M3, solo provato su dati sintetici: migrazione.ts) NON carica gli esempi: nasce
 * con il solo schema e aspetta i dati del Mac su `POST /migrazione`; finche' la migrazione non e' completata le rotte
 * che scrivono nel motore (`/esegui`, `/cattura`, `/cron`) rispondono 409, cosi' il database resta vuoto fino all'arrivo.
 */

import { DurableObject } from "cloudflare:workers";
import cartaCsv from "../../finance/examples/sintetici/carta-demo.csv";
import contoCsv from "../../finance/examples/sintetici/conto-demo.csv";
import regole from "../../finance/examples/sintetici/regole-demo.json" with { type: "json" };
import saldiCsv from "../../finance/examples/sintetici/saldi-conto-demo.csv";
import type { Ctx } from "../../finance/src/context.ts";
import { migrate } from "../../finance/src/db/migrate.ts";
import { PENDING_MIGRATIONS } from "../../finance/src/db/migrations/007_manual_and_budgets.ts";
import { MIGRATIONS } from "../../finance/src/db/migrations/index.ts";
import { readWriteSeq } from "../../finance/src/db/write.ts";
import { importFile } from "../../finance/src/import/importer.ts";
import { OPERATIONS, runQuery, SYNTHETIC_QUERY_SCHEMA_VERSION } from "../../finance/src/query/operations.ts";
import { createAccount } from "../../finance/src/services/accounts.ts";
import { classifyTransactions, publishRuleSet } from "../../finance/src/services/classification.ts";
import { detectLinks } from "../../finance/src/services/links.ts";
import { stato as appuntiStato } from "./appunti.ts";
import {
  AUTOMATION_READS,
  AUTOMATION_TABLES,
  AUTOMATION_WRITES,
  type Automazioni,
  aggiornaBancaAMano,
  aggiornaBancaSeServe,
  notify,
  plan,
  runDue,
} from "./automazioni.ts";
import {
  type BackupFile,
  type BackupKeys,
  createBackup,
  parseBackupKeys,
  readExportableTables,
  readSqliteSequence,
} from "./backup.ts";
import { type BackupBucket, salvaBackup } from "./backup-cloud.ts";
import { leggiBanca, sincronizzaBanca } from "./banca.ts";
import { CATTURA_READS, CATTURA_TABLES, CATTURA_WRITES, registraCattura } from "./cattura.ts";
import { DurableSqliteDb, type DurableSqlStorage } from "./durable-db.ts";
import { execute, REQUESTS_TABLE } from "./finance-api.ts";
import { gestisciMigrazione, MIGRAZIONE_TABLES, migrazioneCompletata, origineMac } from "./migrazione.ts";
import { azioneNotifiche, type PushInfo, promemoriaOp, recenti, statoNotifiche } from "./notifiche.ts";
import { moneyFeed } from "./online.ts";
import { parseVapid, pushInfo } from "./push.ts";

export const STATUS_SCHEMA = "cuore.vetrina.stato/1";

const utf8 = (s: string) => new TextEncoder().encode(s);
const today = () => new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Rome" }).format(new Date());

const CLOUD_TABLES = [REQUESTS_TABLE, ...AUTOMATION_TABLES, ...CATTURA_TABLES, ...MIGRAZIONE_TABLES];

/** Origine `mac` (M3): solo schema del motore e tabelle del cloud, nessun dato; i dati arrivano dal Mac. */
async function prepareForMac(ctx: Ctx): Promise<void> {
  await migrate(ctx.db, [...MIGRATIONS, ...PENDING_MIGRATIONS], ctx.now);
  await ctx.db.batch(CLOUD_TABLES.map((sql) => ({ sql, params: [] })));
}

/** Carica i dati sintetici una sola volta (database vuoto); idempotente. */
async function seedSynthetic(ctx: Ctx): Promise<void> {
  await migrate(ctx.db, [...MIGRATIONS, ...PENDING_MIGRATIONS], ctx.now);
  await ctx.db.batch([
    { sql: "INSERT OR IGNORE INTO meta (key, value) VALUES ('dataset', 'sintetico')", params: [] },
    ...CLOUD_TABLES.map((sql) => ({ sql, params: [] })),
  ]);
  const n = await ctx.db.first<{ n: number }>("SELECT COUNT(*) AS n FROM accounts");
  if ((n?.n ?? 0) > 0) return;
  for (const [id, name, type] of [
    ["conto-demo", "Conto DEMO", "current"],
    ["carta-demo", "Carta DEMO", "card"],
    ["contanti-demo", "Contanti DEMO", "cash"],
  ] as const)
    await createAccount(ctx, { id, name, type, currency: "EUR", holderRole: "owner" });
  await publishRuleSet(ctx, regole);
  for (const [accountId, parserId, fileName, text] of [
    ["conto-demo", "cloud-finance-csv", "conto-demo.csv", contoCsv],
    ["carta-demo", "cloud-finance-csv", "carta-demo.csv", cartaCsv],
    ["conto-demo", "cloud-finance-balances-csv", "saldi-conto-demo.csv", saldiCsv],
  ] as const) {
    const r = await importFile(ctx, { accountId, parserId, fileName, content: utf8(text) });
    if (r.status !== "committed") throw new Error(`esempio sintetico ${fileName} non importato: ${r.status}`);
  }
  await classifyTransactions(ctx);
  await detectLinks(ctx);
}

export class FinanceVetrina extends DurableObject {
  readonly #db: DurableSqliteDb;
  readonly #sql: DurableSqlStorage;
  /** origine `mac`: aspetta (o ha ricevuto) i dati del Mac invece degli esempi sintetici */
  readonly #mac: boolean;
  readonly #ctx: Ctx;
  readonly #auto: Automazioni;
  readonly #backupKeys: Promise<BackupKeys | null>;
  readonly #push: PushInfo;

  constructor(state: DurableObjectState, env: unknown) {
    super(state, env);
    this.#sql = state.storage as unknown as DurableSqlStorage;
    this.#db = new DurableSqliteDb(this.#sql);
    this.#ctx = { db: this.#db, now: () => new Date().toISOString(), newId: () => crypto.randomUUID() };
    const e = env as {
      VAPID_PRIVATE_JWK?: string;
      VAPID_SUBJECT?: string;
      VAPID_PUBLIC_KEY?: string;
      SCHOOL?: DurableObjectNamespace;
      BACKUP_SIGNING_PRIVATE_JWK?: string;
      BACKUP_MAC_PUBLIC_JWK?: string;
      BACKUP_RECOVERY_PUBLIC_JWK?: string;
      CUORE_FINANCE_ORIGINE?: string;
      BACKUP?: BackupBucket;
      BANCA_CHIAVE?: string;
      BANCA_COLLEGAMENTI?: string;
    };
    this.#mac = origineMac(e.CUORE_FINANCE_ORIGINE);
    const bucket = e.BACKUP;
    const vapid = parseVapid(e.VAPID_PRIVATE_JWK, e.VAPID_SUBJECT);
    this.#push = pushInfo(vapid, e.VAPID_PUBLIC_KEY);
    const school = e.SCHOOL;
    const fromSchool = async (req: Request) => {
      if (school === undefined) return null;
      try {
        const r = await school.get(school.idFromName("school")).fetch(req);
        return r.ok ? ((await r.json()) as Record<string, unknown>) : null;
      } catch {
        return null;
      }
    };
    let banca: ReturnType<typeof leggiBanca> = null;
    try {
      banca = leggiBanca(e);
    } catch (err) {
      console.error(`banca: collegamento ignorato (${(err as Error).message})`);
    }
    this.#auto = {
      ctx: this.#ctx,
      ...(banca === null
        ? {}
        : {
            banca: async (giorno: string) =>
              this.#inAttesa()
                ? { saltato: "in_attesa", avvisi: [] }
                : sincronizzaBanca(this.#ctx, banca, giorno),
          }),
      vapid,
      fetch: (u, i) => fetch(u, i),
      // promemoria online (promemoria.ts): fonti in sola lettura, tutte nel cloud
      fonti: {
        school: () => fromSchool(new Request("https://school.interno/feed")),
        personale: async () =>
          (
            (await fromSchool(
              new Request("https://school.interno/personale", {
                method: "POST",
                body: JSON.stringify({ op: "school_stato", input: {} }),
              }),
            )) as { risultato?: Record<string, unknown> } | null
          )?.risultato ?? null,
        money: async () => (this.#inAttesa() ? null : this.#moneyFeed()),
        appunti: async () => (await appuntiStato(this.#db)).appunti,
      },
      // L1: copia giornaliera nel bucket (solo se collegato); senza chiavi il lavoro riesce ma non scrive nulla
      ...(bucket === undefined
        ? {}
        : {
            backup: async (giorno: string) => {
              const file = await this.#backup();
              return file === null ? { saltato: "chiavi_mancanti" } : salvaBackup(bucket, giorno, file);
            },
          }),
    };
    this.#backupKeys = parseBackupKeys(e);
    void state.blockConcurrencyWhile(() => (this.#mac ? prepareForMac : seedSynthetic)(this.#ctx));
  }

  /** Riprende la coda all'ora indicata (mai nel passato: almeno fra un secondo). */
  async #wake(next: number | null): Promise<void> {
    const storage = this.ctx.storage as { setAlarm(ms: number): Promise<void> };
    if (next !== null) await storage.setAlarm(Math.max(next, Date.now() + 1000));
  }

  override async alarm(): Promise<void> {
    await this.#wake(await runDue(this.#auto));
  }

  /**
   * Feed Money tenuto pronto: si ricalcola solo se il motore ha scritto qualcosa (write_seq) o e' cambiato il giorno.
   * Senza questa copia ogni apertura dell'app rileggeva migliaia di righe (quota giornaliera del piano Free).
   */
  #feedPronto: { chiave: string; feed: Record<string, unknown> } | null = null;
  async #moneyFeed(): Promise<Record<string, unknown>> {
    const giorno = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Rome" }).format(new Date());
    const chiave = `${await readWriteSeq(this.#db)}|${giorno}`;
    if (this.#feedPronto?.chiave === chiave) return this.#feedPronto.feed;
    const feed = await moneyFeed(this.#db, new Date(), false);
    this.#feedPronto = feed.esito === "non_disponibile" ? null : { chiave, feed };
    return feed;
  }

  /** Origine `mac` senza migrazione completata: niente scritture nel motore (resterebbe non vuoto). */
  #inAttesa(): boolean {
    return this.#mac && !migrazioneCompletata(this.#sql);
  }

  async #dataset(): Promise<string | null> {
    return (
      (await this.#db.first<{ value: string }>("SELECT value FROM meta WHERE key = 'dataset'"))?.value ?? null
    );
  }

  override async fetch(request: Request): Promise<Response> {
    const prima = this.#db.rowsRead;
    const r = await this.#fetch(request);
    // quota del piano Free: righe lette per richiesta (solo il percorso e un numero, nessun dato)
    console.log(
      JSON.stringify({ righe_lette: this.#db.rowsRead - prima, percorso: new URL(request.url).pathname }),
    );
    return r;
  }

  async #fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/migrazione") {
      let body: unknown;
      try {
        body = await request.json();
      } catch {
        return Response.json({ ok: false, errore: "richiesta_non_valida" }, { status: 400 });
      }
      const r = await gestisciMigrazione(this.#sql, this.#db, body, {
        attesa: this.#mac,
        versioneMotore: SYNTHETIC_QUERY_SCHEMA_VERSION,
        now: this.#ctx.now,
      });
      return Response.json(r.body, { status: r.status });
    }
    if (request.method === "POST" && url.pathname === "/promemoria") {
      const r = await promemoriaOp(this.#db, await request.json().catch(() => null));
      return Response.json(r.body, { status: r.status });
    }
    if (url.pathname === "/notifiche/stato" && request.method === "GET")
      return Response.json(await statoNotifiche(this.#db, this.#push));
    if (url.pathname === "/notifiche/recenti" && request.method === "GET")
      return Response.json(await recenti(this.#db, Date.now()));
    if (url.pathname.startsWith("/notifiche/") && request.method === "POST") {
      const r = await azioneNotifiche(
        this.#db,
        url.pathname.slice("/notifiche/".length),
        await request.json().catch(() => null),
        this.#push,
        (n) => notify(this.#auto, n),
      );
      // la prova parte subito (stesso turno), non al prossimo allarme
      await this.#wake(await runDue(this.#auto));
      return Response.json(r.body, { status: r.status });
    }
    if (request.method === "POST" && url.pathname === "/cron" && this.#inAttesa()) {
      // senza i dati del Mac solo i promemoria (School, appunti; Finance resta «non disponibile»), mai il motore
      const { ora } = (await request.json()) as { ora?: number };
      const next = await plan(
        this.#auto,
        Number.isFinite(ora) ? (ora as number) : Date.now(),
        "solo-promemoria",
      );
      await this.#wake(next);
      return Response.json({ ok: true, prossimo: next, saltato: "in_attesa_dei_dati_del_mac" });
    }
    if (
      request.method === "POST" &&
      (url.pathname === "/esegui" || url.pathname === "/cattura") &&
      this.#inAttesa()
    )
      return Response.json({ ok: false, errore: "in_attesa_dei_dati_del_mac" }, { status: 409 });
    if (request.method === "POST" && url.pathname === "/esegui") {
      let req: unknown;
      try {
        req = await request.json();
      } catch {
        return Response.json({ ok: false, errore: "richiesta_non_valida" }, { status: 400 });
      }
      const r = await execute(this.#ctx, req, {
        reads: { ...AUTOMATION_READS, ...CATTURA_READS },
        writes: {
          ...AUTOMATION_WRITES,
          ...CATTURA_WRITES,
          aggiorna_banca: () => aggiornaBancaAMano(this.#auto),
          aggiorna_banca_se_serve: () => aggiornaBancaSeServe(this.#auto),
        },
      });
      await this.#wake(await this.#queued());
      return Response.json(r.body, { status: r.status });
    }
    if (request.method === "POST" && url.pathname === "/cattura") {
      let body: unknown;
      try {
        body = await request.json();
      } catch {
        return Response.json({ ok: false, errore: "richiesta_non_valida" }, { status: 400 });
      }
      const r = await registraCattura(this.#ctx, body);
      return Response.json(r.body, { status: r.status });
    }
    if (request.method === "POST" && url.pathname === "/backup/esporta") {
      const file = await this.#backup();
      return file === null
        ? Response.json({ ok: false, errore: "chiavi_di_backup_non_configurate" }, { status: 503 })
        : Response.json(file);
    }
    if (request.method === "POST" && url.pathname === "/cron") {
      const { ora, cron } = (await request.json()) as { ora?: number; cron?: string };
      const next = await plan(
        this.#auto,
        Number.isFinite(ora) ? (ora as number) : Date.now(),
        typeof cron === "string" ? cron : undefined,
      );
      await this.#wake(next);
      return Response.json({ ok: true, prossimo: next });
    }
    if (request.method === "GET" && url.pathname === "/money")
      return Response.json(
        this.#inAttesa() ? await moneyFeed(this.#db, new Date(), true) : await this.#moneyFeed(),
      );
    if (request.method !== "GET" || url.pathname !== "/stato")
      return Response.json({ errore: "non_trovato" }, { status: 404 });
    const db = this.#db;
    const count = async (sql: string) => (await db.first<{ n: number }>(sql))?.n ?? 0;
    const day = today();
    // `daily` accetta al massimo 93 giorni: per lei basta il periodo recente
    const recent = new Date(Date.parse(`${day}T00:00:00Z`) - 60 * 86_400_000).toISOString().slice(0, 10);
    const ops = await Promise.all(
      OPERATIONS.map((op) => {
        const input: Record<string, unknown> = {};
        for (const [k, spec] of Object.entries(op.input))
          if (spec.required && spec.type === "date")
            input[k] = k === "from" ? (op.name === "daily" ? recent : "2026-01-01") : day;
          else if (spec.required && spec.type === "string") input[k] = "cuore"; // `search` (v8): prova della ricerca
        return runQuery(db, op.name, input, {
          today: day,
          expectedSchemaVersion: SYNTHETIC_QUERY_SCHEMA_VERSION,
        });
      }),
    );
    const quick = await db.first<{ quick_check: string }>("PRAGMA quick_check");
    const fk = await db.all("PRAGMA foreign_key_check");
    const okOps = ops.filter((r) => r.ok).length;
    const dataset = await this.#dataset();
    return Response.json({
      schema: STATUS_SCHEMA,
      dati:
        dataset === "sintetico" ? "sintetici" : this.#inAttesa() ? "in_attesa_dei_dati_del_mac" : "dal_mac",
      generato: new Date().toISOString(),
      finance: {
        motore: "Cloud Finance · Durable Object SQLite",
        versione_schema: SYNTHETIC_QUERY_SCHEMA_VERSION,
        conti: await count("SELECT COUNT(*) AS n FROM accounts"),
        movimenti: await count("SELECT COUNT(*) AS n FROM transactions"),
        classificati: await count("SELECT COUNT(DISTINCT transaction_id) AS n FROM classifications"),
        da_verificare: await count("SELECT COUNT(*) AS n FROM review_items WHERE status = 'open'"),
        trasferimenti_proposti: await count(
          "SELECT COUNT(*) AS n FROM transaction_links WHERE type = 'transfer'",
        ),
        operazioni_riuscite: `${okOps} su ${ops.length}`,
        consultazione_ok: okOps === ops.length,
        integrita: quick?.quick_check === "ok" && fk.length === 0 ? "ok" : "problemi",
      },
    });
  }

  /** Backup L1 dell'intero motore (cifrato e firmato, backup.ts); `null` = chiavi non configurate. */
  async #backup(): Promise<BackupFile | null> {
    const keys = await this.#backupKeys;
    if (keys === null) return null;
    const db = this.#db;
    const [tables, sqliteSequence, writeSeq] = await Promise.all([
      readExportableTables(db),
      readSqliteSequence(db),
      readWriteSeq(db),
    ]);
    return createBackup({
      tables,
      sqliteSequence,
      versioneSchema: SYNTHETIC_QUERY_SCHEMA_VERSION,
      writeSeq,
      origine: (await this.#dataset()) === "sintetico" ? "sintetico" : "durable-object",
      recipients: keys.recipients,
      signingKey: keys.signingKey,
    });
  }

  async #queued(): Promise<number | null> {
    return (
      (await this.#db.first<{ dopo: number | null }>("SELECT MIN(dopo) AS dopo FROM cloud_coda"))?.dopo ??
      null
    );
  }
}
