/**
 * BANCO DI MISURA (C1, rischio n. 1 di): Worker SEPARATO (`wrangler.misure.jsonc`, nome
 * `cuore-misure-sintetiche`) con un suo Durable Object SQLite `BancoMisure`. Non tocca l'ingresso di produzione
 * (index.ts non lo importa) ne' i Durable Object della vetrina: nasce per una misura e poi si cancella
 * (`wrangler delete -c wrangler.misure.jsonc`). Esporta solo il gestore e la classe (regola di workerd).
 *
 * Serve a sapere, sul piano Free, se il motore Finance INVARIATO sta nel limite di CPU di un Durable Object (10 ms o
 * 30 s?): carica a blocchi N movimenti SINTETICI generati qui (mai dati reali), poi esegue una operazione per richiesta
 * (= una invocazione: ognuna fa i conti col limite da sola): classificazione, rilevamento dei trasferimenti, feed Money
 * (le 12 letture di app/bridge/cuore_money.ts + la serie di 12 mesi), le singole consultazioni, il backup L1.
 * Prova anche il ripristino a un istante (PITR) con l'API dei Durable Object SQLite: `getCurrentBookmark`,
 * `getBookmarkForTime`, `onNextSessionRestoreBookmark` + `ctx.abort()` (Miniflare non lo implementa: risponde 501).
 *
 * Chiuso per difetto: senza `MISURE_TOKEN_SHA256` (impronta SHA-256 di un token casuale, passata solo al momento del
 * deploy con `--var`) risponde 503 a tutto; token sbagliato = 401; solo POST su `/misure/...`, al massimo 4 KiB.
 * `ms_interni` e' il tempo visto dal Durable Object: in produzione gli orologi dei Worker avanzano solo con l'I/O, quindi
 * la CPU vera si legge dal pannello (Metrics) o da `wrangler tail`; il segnale netto e' l'errore di CPU superata.
 */

import { DurableObject } from "cloudflare:workers";
import type { webcrypto } from "node:crypto";
import regole from "../../finance/examples/sintetici/regole-demo.json" with { type: "json" };
import type { Ctx } from "../../finance/src/context.ts";
import { migrate } from "../../finance/src/db/migrate.ts";
import { PENDING_MIGRATIONS } from "../../finance/src/db/migrations/007_manual_and_budgets.ts";
import { MIGRATIONS } from "../../finance/src/db/migrations/index.ts";
import { readWriteSeq } from "../../finance/src/db/write.ts";
import { importFile } from "../../finance/src/import/importer.ts";
import { runQuery, SYNTHETIC_QUERY_SCHEMA_VERSION } from "../../finance/src/query/operations.ts";
import { createAccount } from "../../finance/src/services/accounts.ts";
import { classifyTransactions, publishRuleSet } from "../../finance/src/services/classification.ts";
import { detectLinks } from "../../finance/src/services/links.ts";
import { SECURITY_HEADERS } from "./app.ts";
import { createBackup, readExportableTables, readSqliteSequence, recipientKid } from "./backup.ts";
import { checkToken } from "./cattura.ts";
import { DurableSqliteDb, type DurableSqlStorage } from "./durable-db.ts";

const MISURE_PREFIX = "/misure/";
const MAX_BODY = 4 * 1024;
const PER_GIORNO = 8;
const BANCO_TABLE =
  "CREATE TABLE IF NOT EXISTS cloud_banco (chiave TEXT PRIMARY KEY, valore TEXT NOT NULL) STRICT";

class Rifiuto extends Error {
  readonly status: number;
  readonly codice: string;
  constructor(status: number, codice: string, messaggio = codice) {
    super(messaggio);
    this.status = status;
    this.codice = codice;
  }
}

const giorno = (ms: number) => new Date(ms).toISOString().slice(0, 10);
const addDays = (d: string, n: number) => giorno(Date.parse(`${d}T00:00:00Z`) + n * 86_400_000);
const eur = (cent: number) => (cent / 100).toFixed(2);

type Riga = readonly ["conto" | "carta", number, string, string];
/** Gli 8 movimenti di ogni giorno `g`: `i` progressivo, `c` importo in centesimi, `r` la ricarica (solo nei giorni `ric`). */
const TIPI: readonly ((i: number, c: number, r: number | null, g: number) => Riga)[] = [
  (i, c) => ["conto", -c, `Spesa supermercato ${i}`, "Supermercato Esempio"],
  (_i, c) => ["carta", -(c % 400) - 80, "Caffe", "Bar Esempio"],
  (i, c) => ["carta", -c, `Libreria online ${i}`, "Libreria Esempio"],
  (i, c) => ["conto", -(c % 300) - 50, `Commissione bonifico ${i}`, "Banca Esempio"],
  (i, c, r) =>
    r === null
      ? ["conto", -c, `Pagamento POS ${i}`, `Negozio ${i % 53}`]
      : ["conto", -r, "Ricarica carta", ""],
  (i, c, r) =>
    r === null
      ? ["carta", -(c % 500) - 100, `Ristorante ${i}`, `Ristorante ${i % 31}`]
      : ["carta", r, "Ricarica da conto", ""],
  (i, c) => ["conto", -c, `Abbonamento streaming ${i}`, "Servizio Esempio"],
  (i, c, _r, g) =>
    g % 4 === 0
      ? ["conto", c * 20, `Stipendio DEMO ${i}`, "Azienda Esempio Srl"]
      : ["conto", -c, `Pagamento vario ${i}`, `Esercente ${i % 97}`],
];

/**
 * Movimenti SINTETICI deterministici (8 al giorno da `inizio`): spese che le regole d'esempio classificano, una
 * ricarica conto → carta ogni `ricaricaOgni` giorni (coppia da trasferimento; 7 = come i dati veri, ~2% dei movimenti),
 * stipendi e pagamenti senza regola (da verificare).
 */
function movimentiSintetici(
  inizio: string,
  da: number,
  quanti: number,
  ricaricaOgni: number,
): Record<"conto" | "carta", string> {
  const testa =
    "transaction_id,transaction_date,booking_date,status,amount,currency,description,counterparty,original_amount,original_currency";
  const out = { conto: [testa], carta: [testa] };
  for (let i = da; i < da + quanti; i++) {
    const g = Math.floor(i / PER_GIORNO);
    const d = addDays(inizio, g);
    const tipo = TIPI[i % PER_GIORNO] as (typeof TIPI)[number];
    const r = g % ricaricaOgni === 0 ? 2000 + ((g * 37) % 5000) : null;
    const [conto, importo, descrizione, controparte] = tipo(i, 100 + ((i * 7919) % 9000), r, g);
    out[conto].push(`BANCO-${i},${d},${d},booked,${eur(importo)},EUR,${descrizione},${controparte},,`);
  }
  return { conto: `${out.conto.join("\n")}\n`, carta: `${out.carta.join("\n")}\n` };
}

type Plan = [string, Record<string, unknown>];
/** Le consultazioni del feed Money (stesso piano di app/bridge/cuore_money.ts, non importato: `app/` e' a parte). */
function consultazioni(oggi: string): Record<string, Plan> {
  const mese = `${oggi.slice(0, 8)}01`;
  const prec = `${addDays(mese, -1).slice(0, 8)}01`;
  return {
    accounts: ["accounts", {}],
    overview: ["overview", { from: mese, to: oggi }],
    summary: ["summary", { from: mese, to: oggi }],
    summary_12_mesi: ["summary", { from: addDays(oggi, -365), to: oggi }],
    categories: ["categories", {}],
    planning: ["planning", {}],
    balances: ["balances", { asOf: oggi }],
    data_status: ["data_status", { asOf: oggi }],
    obligations: ["obligations", { asOf: oggi }],
    transactions: ["transactions", { from: addDays(oggi, -30), to: oggi, limit: 50 }],
    manual: ["manual", { from: addDays(oggi, -30), to: oggi }],
    budgets: ["budgets", { month: oggi.slice(0, 7) }],
    daily: ["daily", { from: prec, to: oggi }],
    search: ["search", { q: "supermercato", limit: 50 }],
  };
}
const FEED = [
  "accounts",
  "overview",
  "summary",
  "categories",
  "planning",
  "balances",
  "data_status",
  "obligations",
  "transactions",
  "manual",
  "budgets",
  "daily",
];

/** API PITR dei Durable Object SQLite (pagina «SQLite storage API»); i tipi di @cloudflare/workers-types non sono installati. */
interface PitrStorage {
  getCurrentBookmark(): Promise<string>;
  getBookmarkForTime(timestamp: number | Date): Promise<string>;
  onNextSessionRestoreBookmark(bookmark: string): Promise<string>;
  deleteAll(): Promise<void>;
}

export class BancoMisure extends DurableObject {
  readonly #sql: DurableSqlStorage;
  readonly #ctx: Ctx;

  constructor(state: DurableObjectState, env: unknown) {
    super(state, env);
    this.#sql = state.storage as unknown as DurableSqlStorage;
    this.#ctx = {
      db: new DurableSqliteDb(this.#sql),
      now: () => new Date().toISOString(),
      newId: () => crypto.randomUUID(),
    };
    this.#sql.sql.exec(BANCO_TABLE);
  }

  get #storage(): PitrStorage {
    return this.ctx.storage as unknown as PitrStorage;
  }

  #valore(chiave: string): string | null {
    const r = this.#sql.sql.exec("SELECT valore FROM cloud_banco WHERE chiave = ?", chiave).toArray()[0];
    return (r?.valore as string | undefined) ?? null;
  }

  #imposta(chiave: string, valore: string): void {
    this.#sql.sql.exec(
      "INSERT INTO cloud_banco (chiave, valore) VALUES (?, ?) ON CONFLICT (chiave) DO UPDATE SET valore = excluded.valore",
      chiave,
      valore,
    );
  }

  async #conta(): Promise<Record<string, number>> {
    const n = async (sql: string) => (await this.#ctx.db.first<{ n: number }>(sql))?.n ?? 0;
    return {
      movimenti: await n("SELECT COUNT(*) AS n FROM transactions"),
      classificati: await n("SELECT COUNT(DISTINCT transaction_id) AS n FROM classifications"),
      da_verificare: await n("SELECT COUNT(*) AS n FROM review_items WHERE status = 'open'"),
      trasferimenti_proposti: await n("SELECT COUNT(*) AS n FROM transaction_links WHERE type = 'transfer'"),
    };
  }

  override async fetch(request: Request): Promise<Response> {
    const path = new URL(request.url).pathname;
    let body: Record<string, unknown> = {};
    try {
      const t = await request.text();
      body = t === "" ? {} : (JSON.parse(t) as Record<string, unknown>);
    } catch {
      return Response.json({ ok: false, errore: "richiesta_non_valida" }, { status: 400 });
    }
    const t0 = performance.now();
    try {
      const esito = await this.#esegui(path, body);
      return Response.json({
        ok: true,
        ms_interni: Math.round((performance.now() - t0) * 100) / 100,
        ...esito,
      });
    } catch (e) {
      if (e instanceof Rifiuto)
        return Response.json({ ok: false, errore: e.codice, messaggio: e.message }, { status: e.status });
      throw e;
    }
  }

  async #esegui(path: string, b: Record<string, unknown>): Promise<Record<string, unknown>> {
    const ctx = this.#ctx;
    const oggi = giorno(Date.now());
    const intero = (v: unknown, min: number, max: number, nome: string) => {
      if (!Number.isInteger(v) || (v as number) < min || (v as number) > max)
        throw new Rifiuto(400, "parametro_non_valido", `${nome}: intero fra ${min} e ${max}`);
      return v as number;
    };
    const preparato = () => {
      const inizio = this.#valore("inizio");
      if (inizio === null) throw new Rifiuto(409, "banco_non_preparato", "prima /misure/azzera");
      return inizio;
    };
    switch (path) {
      case "/misure/azzera": {
        const n = intero(b.movimenti ?? 5000, 1, 20_000, "movimenti");
        const ogni = intero(b.ricarica_ogni_giorni ?? 7, 1, 365, "ricarica_ogni_giorni");
        await this.#storage.deleteAll();
        await migrate(ctx.db, [...MIGRATIONS, ...PENDING_MIGRATIONS], ctx.now);
        await ctx.db.batch([
          { sql: "INSERT OR IGNORE INTO meta (key, value) VALUES ('dataset', 'sintetico')", params: [] },
          { sql: BANCO_TABLE, params: [] },
        ]);
        for (const [id, type] of [
          ["conto", "current"],
          ["carta", "card"],
        ] as const)
          await createAccount(ctx, { id, name: `Banco ${id}`, type, currency: "EUR", holderRole: "owner" });
        await publishRuleSet(ctx, regole);
        const inizio = addDays(oggi, -Math.ceil(n / PER_GIORNO));
        this.#imposta("inizio", inizio);
        this.#imposta("ricarica_ogni", String(ogni));
        return { movimenti_previsti: n, inizio, ricarica_ogni_giorni: ogni };
      }
      case "/misure/carica": {
        const inizio = preparato();
        const da = intero(b.da, 0, 20_000, "da");
        const quanti = intero(b.quanti, 1, 2000, "quanti");
        const csv = movimentiSintetici(inizio, da, quanti, Number(this.#valore("ricarica_ogni") ?? 7));
        for (const conto of ["conto", "carta"] as const) {
          if (csv[conto].split("\n").length <= 2) continue;
          const r = await importFile(ctx, {
            accountId: conto,
            parserId: "cloud-finance-csv",
            fileName: `banco-${conto}-${da}.csv`,
            content: new TextEncoder().encode(csv[conto]),
          });
          if (r.status !== "committed") throw new Rifiuto(422, "importazione_rifiutata", r.status);
        }
        return { da, quanti, ...(await this.#conta()) };
      }
      case "/misure/esegui":
        preparato();
        return { operazione: b.operazione, esito: await this.#operazione(b, oggi) };
      case "/misure/pitr/segnalibro": {
        const out: Record<string, unknown> = { segnalibro: await this.#storage.getCurrentBookmark() };
        if (b.istante_ms !== undefined)
          out.segnalibro_istante = await pitr(() =>
            this.#storage.getBookmarkForTime(intero(b.istante_ms, 0, Date.now(), "istante_ms")),
          );
        return out;
      }
      case "/misure/pitr/scrivi": {
        const marca = crypto.randomUUID();
        this.#imposta("pitr_marca", marca);
        return { marca };
      }
      case "/misure/pitr/leggi":
        return { marca: this.#valore("pitr_marca"), ...(this.#valore("inizio") ? await this.#conta() : {}) };
      case "/misure/pitr/prepara": {
        if (typeof b.segnalibro !== "string" || b.segnalibro === "")
          throw new Rifiuto(400, "parametro_non_valido", "segnalibro mancante");
        const s = b.segnalibro;
        // il ripristino avviene al prossimo avvio dell'oggetto (/misure/pitr/riavvia); `annulla` torna indietro
        return { annulla: await pitr(() => this.#storage.onNextSessionRestoreBookmark(s)) };
      }
      case "/misure/pitr/riavvia":
        (this.ctx as unknown as { abort(reason?: string): void }).abort("riavvio per il ripristino PITR");
        return { riavviato: true };
      default:
        throw new Rifiuto(404, "non_trovato");
    }
  }

  async #operazione(b: Record<string, unknown>, oggi: string): Promise<unknown> {
    const ctx = this.#ctx;
    const q = { today: oggi, expectedSchemaVersion: SYNTHETIC_QUERY_SCHEMA_VERSION };
    const piano = consultazioni(oggi);
    switch (b.operazione) {
      case "stato":
        return this.#conta();
      case "classifica":
        return classifyTransactions(ctx);
      case "trasferimenti":
        return detectLinks(ctx);
      case "consulta": {
        const p = typeof b.nome === "string" ? piano[b.nome] : undefined;
        if (p === undefined)
          throw new Rifiuto(400, "parametro_non_valido", `nome fra ${Object.keys(piano).join(", ")}`);
        const r = await runQuery(ctx.db, p[0], p[1], q);
        return { ok: r.ok, ...(r.ok ? {} : { errore: r.error }) };
      }
      case "feed": {
        let riuscite = 0;
        let chiamate = 0;
        for (const nome of FEED) {
          const [op, input] = piano[nome] as Plan;
          chiamate++;
          if ((await runQuery(ctx.db, op, input, q)).ok) riuscite++;
        }
        for (let m = 11; m >= 0; m--) {
          const d = new Date(`${oggi.slice(0, 8)}01T00:00:00Z`);
          d.setUTCMonth(d.getUTCMonth() - m);
          const from = giorno(d.getTime());
          d.setUTCMonth(d.getUTCMonth() + 1, 0);
          const to = giorno(Math.min(d.getTime(), Date.parse(`${oggi}T00:00:00Z`)));
          chiamate++;
          if ((await runQuery(ctx.db, "summary", { from, to }, q)).ok) riuscite++;
        }
        return { chiamate, riuscite };
      }
      case "backup": {
        const chiavi = await this.#chiaviDiProva();
        const t = await readExportableTables(ctx.db);
        const file = await createBackup({
          tables: t,
          sqliteSequence: await readSqliteSequence(ctx.db),
          versioneSchema: SYNTHETIC_QUERY_SCHEMA_VERSION,
          writeSeq: await readWriteSeq(ctx.db),
          origine: "sintetico",
          recipients: chiavi.recipients,
          signingKey: chiavi.signingKey,
        });
        return { blocchi: file.blocchi.length, byte: JSON.stringify(file).length, chiavi: chiavi.nuove };
      }
      default:
        throw new Rifiuto(
          400,
          "operazione_sconosciuta",
          "stato, classifica, trasferimenti, consulta, feed, backup",
        );
    }
  }

  /** Chiavi di PROVA generate nel banco (mai segreti veri); la prima volta costano: la misura buona e' la seconda. */
  async #chiaviDiProva() {
    const salvate = this.#valore("chiavi_di_prova");
    const rsa = { name: "RSA-OAEP", hash: "SHA-256" };
    const ec = { name: "ECDSA", namedCurve: "P-256" };
    let jwk: { pub: webcrypto.JsonWebKey; firma: webcrypto.JsonWebKey };
    const nuove = salvate === null;
    if (salvate === null) {
      const kp = (await crypto.subtle.generateKey(
        { ...rsa, modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]) },
        true,
        ["encrypt", "decrypt"],
      )) as webcrypto.CryptoKeyPair;
      const f = (await crypto.subtle.generateKey(ec, true, ["sign", "verify"])) as webcrypto.CryptoKeyPair;
      jwk = {
        pub: await crypto.subtle.exportKey("jwk", kp.publicKey),
        firma: await crypto.subtle.exportKey("jwk", f.privateKey),
      };
      this.#imposta("chiavi_di_prova", JSON.stringify(jwk));
    } else jwk = JSON.parse(salvate);
    const publicKey = await crypto.subtle.importKey("jwk", jwk.pub, rsa, true, ["encrypt"]);
    return {
      nuove,
      recipients: [{ kid: await recipientKid(publicKey), publicKey }],
      signingKey: await crypto.subtle.importKey("jwk", jwk.firma, ec, false, ["sign"]),
    };
  }
}

/** PITR non disponibile (Miniflare: «does not implement point-in-time recovery»; forse il piano) = 501, mai un 500. */
async function pitr<T>(f: () => Promise<T>): Promise<T> {
  try {
    return await f();
  } catch (e) {
    throw new Rifiuto(501, "pitr_non_disponibile", e instanceof Error ? e.message : String(e));
  }
}

interface Env {
  BANCO: DurableObjectNamespace;
  /** impronta SHA-256 del token del banco; assente = banco spento (503) */
  MISURE_TOKEN_SHA256?: string;
}

const json = (status: number, data: unknown) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { ...SECURITY_HEADERS, "Content-Type": "application/json; charset=utf-8" },
  });

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const hash = env.MISURE_TOKEN_SHA256?.trim();
    if (!hash) return json(503, { ok: false, errore: "banco_spento" });
    if ((await checkToken(request.headers.get("Authorization"), hash)) !== true)
      return json(401, { ok: false, errore: "non_autorizzato" });
    const url = new URL(request.url);
    if (request.method !== "POST") return json(405, { ok: false, errore: "solo_post" });
    if (!url.pathname.startsWith(MISURE_PREFIX)) return json(404, { ok: false, errore: "non_trovato" });
    const body = await request.arrayBuffer();
    if (body.byteLength > MAX_BODY) return json(413, { ok: false, errore: "richiesta_troppo_grande" });
    try {
      const r = await env.BANCO.get(env.BANCO.idFromName("banco")).fetch(
        new Request(`https://banco.interno${url.pathname}`, { method: "POST", body }),
      );
      return new Response(await r.text(), {
        status: r.status,
        headers: { ...SECURITY_HEADERS, "Content-Type": "application/json; charset=utf-8" },
      });
    } catch (e) {
      const messaggio = (e instanceof Error ? e.message : String(e)).slice(0, 300);
      // il riavvio voluto (ctx.abort) arriva qui come errore: e' l'esito atteso
      if (url.pathname === "/misure/pitr/riavvia") return json(200, { ok: true, riavviato: true, messaggio });
      return json(502, { ok: false, errore: "durable_object", messaggio });
    }
  },
};
