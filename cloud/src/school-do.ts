/**
 * Durable Object «School» (blocco E3): SOLO l'ultimo feed School inviato dal Mac, in un database SQLite suo, separato
 * da Finance (regola «nessun database comune»). Nessuno storico: una riga, sostituita a ogni
 * invio piu' recente. Nessuna credenziale ClasseViva o Classroom, nessuna chiamata alle fonti: School resta sul Mac.
 *
 * Rotte interne (le chiama solo il Worker, app.ts, dopo l'identita'):
 * - `POST /feed`: corpo JSON gia' limitato dal Worker; validazione a schema chiuso (school.ts); un feed con `generato`
 *   piu' vecchio dell'ultimo conservato e' rifiutato (409: un invio in ritardo non sostituisce uno piu' nuovo); lo
 *   stesso feed ripetuto e' idempotente (200, `ripetuto`).
 * - `GET /feed`: l'ultimo feed con `consegna` (eta' e stato rispetto alla soglia); 404 se non e' mai arrivato nulla.
 * - `POST /richiesta` (titolare, «Sincronizza tutto») e `GET /richiesta` (Mac, token di servizio): una sola richiesta di
 *   giro School in attesa; la chiude il primo feed generato dopo la richiesta.
 * - `POST /personale` (dati veri online): note, impegni, compiti fatti e obiettivi del titolare scritti dalla pagina
 *   (school-personale.ts), in una tabella a parte che il feed del Mac non tocca.
 */
import { DurableObject } from "cloudflare:workers";
import { sha256Hex } from "../../finance/src/domain/hash.ts";
import type { DurableSqlStorage } from "./durable-db.ts";
import { checkSchoolFeed, deliveryText, FUTURE_SKEW_MS, SCHOOL_MAX_BODY, type StoredFeed } from "./school.ts";
import { PERSONALE_TABLE, personale } from "./school-personale.ts";

const TABLE = `CREATE TABLE IF NOT EXISTS school_feed (
  id INTEGER PRIMARY KEY CHECK (id = 1), generato TEXT NOT NULL, generato_ms INTEGER NOT NULL,
  ricevuto TEXT NOT NULL, byte INTEGER NOT NULL, impronta TEXT NOT NULL, esito TEXT NOT NULL, feed TEXT NOT NULL
) STRICT`;
const RICHIESTA =
  "CREATE TABLE IF NOT EXISTS school_richiesta (id INTEGER PRIMARY KEY CHECK (id = 1), quando TEXT NOT NULL, quando_ms INTEGER NOT NULL) STRICT";

const json = (status: number, body: unknown) => Response.json(body, { status });

export class SchoolFeed extends DurableObject {
  readonly #sql: DurableSqlStorage["sql"];
  readonly #storage: DurableSqlStorage;

  constructor(state: DurableObjectState, env: unknown) {
    super(state, env);
    this.#sql = (state.storage as unknown as DurableSqlStorage).sql;
    this.#sql.exec(TABLE);
    this.#sql.exec(PERSONALE_TABLE);
    this.#sql.exec(RICHIESTA);
    this.#storage = state.storage as unknown as DurableSqlStorage;
  }

  #last(): StoredFeed | null {
    const row = this.#sql
      .exec("SELECT generato, generato_ms, ricevuto, byte, impronta, feed FROM school_feed WHERE id = 1")
      .toArray()[0];
    return (row as unknown as StoredFeed | undefined) ?? null;
  }

  override async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/personale" && request.method === "POST") {
      let body: unknown;
      try {
        body = await request.json();
      } catch {
        return json(400, { ok: false, errore: "richiesta_non_valida" });
      }
      const r = personale(this.#storage, body);
      return json(r.status, r.body);
    }
    if (url.pathname === "/richiesta") {
      if (request.method === "POST") {
        const now = Date.now();
        this.#sql.exec(
          "INSERT INTO school_richiesta (id, quando, quando_ms) VALUES (1, ?, ?) ON CONFLICT (id) DO UPDATE SET quando = excluded.quando, quando_ms = excluded.quando_ms",
          new Date(now).toISOString(),
          now,
        );
      }
      const r = this.#sql.exec("SELECT quando FROM school_richiesta WHERE id = 1").toArray()[0];
      return json(200, { ok: true, risultato: { richiesta: (r?.quando as string | undefined) ?? null } });
    }
    if (url.pathname !== "/feed") return json(404, { errore: "non_trovato" });
    if (request.method === "GET") {
      const last = this.#last();
      if (last === null)
        return json(404, { errore: "nessun_feed", messaggio: "nessun feed School ricevuto dal Mac" });
      return new Response(deliveryText(last, Date.now()), {
        headers: { "Content-Type": "application/json; charset=utf-8" },
      });
    }
    if (request.method !== "POST") return json(405, { ok: false, errore: "metodo_non_ammesso" });

    const bytes = new Uint8Array(await request.arrayBuffer());
    if (bytes.byteLength > SCHOOL_MAX_BODY)
      return json(413, { ok: false, errore: "richiesta_troppo_grande" });
    let text: string;
    let doc: unknown;
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(bytes).trim();
      doc = JSON.parse(text);
    } catch {
      return json(400, { ok: false, errore: "richiesta_non_valida" });
    }
    const check = checkSchoolFeed(doc);
    if (!check.ok) return json(422, { ok: false, errore: check.codice, problemi: check.problemi });
    const now = Date.now();
    if (check.generatoMs > now + FUTURE_SKEW_MS)
      return json(422, { ok: false, errore: "generato_nel_futuro" });

    const impronta = await sha256Hex(text);
    const generato = (doc as { generato: string }).generato;
    const last = this.#last();
    if (last !== null) {
      if (check.generatoMs < last.generato_ms)
        return json(409, { ok: false, errore: "feed_meno_recente", ultimo_generato: last.generato });
      if (check.generatoMs === last.generato_ms)
        return last.impronta === impronta
          ? json(200, { ok: true, ripetuto: true, generato: last.generato, ricevuto: last.ricevuto })
          : json(409, { ok: false, errore: "generato_riusato" });
    }
    const ricevuto = new Date(now).toISOString();
    this.#sql.exec(
      `INSERT INTO school_feed (id, generato, generato_ms, ricevuto, byte, impronta, esito, feed)
       VALUES (1, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT (id) DO UPDATE SET generato = excluded.generato, generato_ms = excluded.generato_ms,
         ricevuto = excluded.ricevuto, byte = excluded.byte, impronta = excluded.impronta, esito = excluded.esito,
         feed = excluded.feed`,
      generato,
      check.generatoMs,
      ricevuto,
      bytes.byteLength,
      impronta,
      check.esito,
      text,
    );
    // il giro che ha prodotto questo feed e' partito dopo la richiesta: richiesta soddisfatta
    this.#sql.exec("DELETE FROM school_richiesta WHERE quando_ms <= ?", check.generatoMs);
    return json(200, { ok: true, generato, ricevuto, byte: bytes.byteLength, esito: check.esito });
  }
}
