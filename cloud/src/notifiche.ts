/**
 * Notifiche e promemoria ONLINE nel Durable Object Finance (rotte interne, le chiama solo il Worker dopo Access).
 *
 * 1. Operazioni del ponte del Mac (`scritture.py`) portate nel cloud, stessi ingressi e uscite:
 *    `promemoria_stato`, `promemoria_imposta`, `appunti_stato`, `appunti_scrivi` → `POST /promemoria`
 *    `{op, input, richiesta}` → `{ok: true, risultato}` | 422 `{ok: false, errore: "non_valido", messaggio}`.
 *    Scritture: `requestId` obbligatorio; stesso id e stessa richiesta = stessa risposta con `ripetuta: true`; stesso id
 *    con richiesta diversa = 409 `requestId_gia_usato` (come il ponte, ma persistente: tabella `cloud_richieste`).
 * 2. Dispositivi Web Push (uno per dispositivo, con nome): `POST /notifiche/iscrizione` `{dispositivo, nome,
 *    endpoint}` (sostituisce l'iscrizione precedente dello stesso dispositivo), `POST /notifiche/annulla`
 *    `{dispositivo}`, `POST /notifiche/imposta` `{attive}` (interruttore generale del push), `POST /notifiche/prova`
 *    (una notifica di prova a tutti i dispositivi), `GET /notifiche/stato`, `GET /notifiche/recenti` (per il service
 *    worker: le ultime notifiche delle 12 ore, al massimo 5).
 */
import type { Db } from "../../finance/src/db/types.ts";
import { aggiungi, stato as appuntiStato, NonValido } from "./appunti.ts";
import { romaIso } from "./online.ts";
import { appuntoSend, ConfigNonValida, readSettings, settingsStatus, updateSettings } from "./promemoria.ts";
import { checkEndpoint } from "./push.ts";

// biome-ignore lint/suspicious/noExplicitAny: ingressi JSON validati qui
type Rec = Record<string, any>;
export interface Reply {
  status: number;
  body: Rec;
}

export const DISPOSITIVI_TABLE = `CREATE TABLE IF NOT EXISTS cloud_dispositivi (dispositivo TEXT PRIMARY KEY,
  nome TEXT NOT NULL, endpoint TEXT NOT NULL UNIQUE, creata TEXT NOT NULL, aggiornata TEXT NOT NULL) STRICT`;
export const PROMEMORIA_OPS = ["promemoria_stato", "promemoria_imposta", "appunti_stato", "appunti_scrivi"];
const READ_OPS = ["promemoria_stato", "appunti_stato"];
const REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{7,99}$/;
const DEVICE = /^[A-Za-z0-9_-]{8,64}$/;
const isObj = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);

/** Come `fingerprint` del ponte: op e ingresso con le chiavi ordinate. */
async function fingerprint(op: string, input: unknown): Promise<string> {
  const sort = (v: unknown): unknown =>
    Array.isArray(v)
      ? v.map(sort)
      : isObj(v)
        ? Object.fromEntries(
            Object.keys(v)
              .sort()
              .map((k) => [k, sort(v[k])]),
          )
        : v;
  const d = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(JSON.stringify([op, sort(input)])),
  );
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function run(db: Db, op: string, i: Rec, rid: string | null, nowMs: number): Promise<unknown> {
  if (op === "promemoria_stato") return settingsStatus(db, nowMs);
  if (op === "promemoria_imposta") return updateSettings(db, i, nowMs);
  if (op === "appunti_scrivi") return aggiungi(db, i, rid, new Date(nowMs));
  const st = await appuntiStato(db);
  const [settings] = await readSettings(db);
  for (const a of st.appunti) {
    const t = a.stato === "aperto" && a.promemoria ? appuntoSend(settings, a.quando, nowMs) : null;
    a.invio_previsto = t === null ? null : romaIso(new Date(t));
  }
  return st;
}

/** `POST /promemoria` del DO: stesse regole di `Scritture.execute` + `_promemoria` del ponte. */
export async function promemoriaOp(db: Db, body: unknown, nowMs = Date.now()): Promise<Reply> {
  const { op, input = {}, richiesta } = (isObj(body) ? body : {}) as Rec;
  if (typeof op !== "string" || !PROMEMORIA_OPS.includes(op) || !isObj(input))
    return { status: 400, body: { ok: false, errore: "operazione_sconosciuta" } };
  const write = !READ_OPS.includes(op);
  if (write && !(typeof richiesta === "string" && REQUEST_ID.test(richiesta)))
    return { status: 400, body: { ok: false, errore: "requestId_mancante" } };
  const fp = write ? await fingerprint(op, input) : "";
  if (write) {
    const prev = await db.first<{ impronta: string; risposta: string }>(
      "SELECT impronta, risposta FROM cloud_richieste WHERE request_id = ?",
      [richiesta],
    );
    if (prev !== null)
      return prev.impronta === fp
        ? { status: 200, body: { ...JSON.parse(prev.risposta), ripetuta: true } }
        : { status: 409, body: { ok: false, errore: "requestId_gia_usato" } };
  }
  try {
    const doc = { ok: true, risultato: await run(db, op, input, write ? richiesta : null, nowMs) };
    if (write)
      await db.batch([
        {
          sql: "INSERT INTO cloud_richieste (request_id, op, impronta, risposta, creata) VALUES (?, ?, ?, ?, ?)",
          params: [richiesta, op, fp, JSON.stringify(doc), new Date(nowMs).toISOString()],
        },
      ]);
    return { status: 200, body: doc };
  } catch (e) {
    if (e instanceof NonValido || e instanceof ConfigNonValida)
      return { status: 422, body: { ok: false, errore: "non_valido", messaggio: e.message } };
    return { status: 500, body: { ok: false, errore: "errore" } };
  }
}

// ------------------------------------------------------------------------------------------------ dispositivi
export interface PushInfo {
  /** chiave pubblica VAPID (vars.VAPID_PUBLIC_KEY) se coerente con quella privata; null = push non configurato */
  chiavePubblica: string | null;
  motivo: string | null;
}

const notificheAttive = async (db: Db) =>
  (await db.first<{ valore: string }>("SELECT valore FROM cloud_impostazioni WHERE chiave = 'notifiche'"))
    ?.valore === "attive";

export async function statoNotifiche(db: Db, info: PushInfo): Promise<Rec> {
  return {
    schema: "cuore.notifiche/1",
    configurate: info.chiavePubblica !== null,
    chiave_pubblica: info.chiavePubblica,
    motivo: info.motivo,
    attive: await notificheAttive(db),
    dispositivi: await db.all(
      "SELECT dispositivo, nome, creata, aggiornata FROM cloud_dispositivi ORDER BY creata, dispositivo",
    ),
    nota: "Su iPhone e iPad le notifiche arrivano solo all'app aggiunta alla schermata Home (iOS/iPadOS 16.4 o successivo).",
  };
}

export async function recenti(db: Db, nowMs: number): Promise<Rec> {
  const from = new Date(nowMs - 12 * 3_600_000).toISOString();
  return {
    notifiche: await db.all(
      "SELECT id, creata, tipo, titolo, testo FROM cloud_notifiche WHERE creata >= ? ORDER BY creata DESC, id LIMIT 5",
      [from],
    ),
  };
}

/** Azioni sui dispositivi; `notify` registra una notifica (e il suo push in coda), `info` lo stato del push. */
export async function azioneNotifiche(
  db: Db,
  azione: string,
  body: unknown,
  info: PushInfo,
  notify: (n: { chiave: string; tipo: string; titolo: string; testo: string }) => Promise<void>,
  nowMs = Date.now(),
): Promise<Reply> {
  const i = isObj(body) ? body : {};
  const bad = (messaggio: string): Reply => ({
    status: 422,
    body: { ok: false, errore: "non_valido", messaggio },
  });
  const now = new Date(nowMs).toISOString();
  if (azione === "iscrizione") {
    if (info.chiavePubblica === null)
      return { status: 503, body: { ok: false, errore: "push_non_configurato" } };
    if (typeof i.dispositivo !== "string" || !DEVICE.test(i.dispositivo))
      return bad("dispositivo non valido");
    const nome = typeof i.nome === "string" ? i.nome.trim() : "";
    if (!nome || nome.length > 40) return bad("nome: da 1 a 40 caratteri");
    let endpoint: string;
    try {
      endpoint = checkEndpoint(i.endpoint);
    } catch (e) {
      return bad((e as Error).message);
    }
    await db.batch([
      // lo stesso indirizzo push passato a un altro dispositivo (reinstallazione): resta uno solo
      {
        sql: "DELETE FROM cloud_dispositivi WHERE endpoint = ? AND dispositivo <> ?",
        params: [endpoint, i.dispositivo],
      },
      {
        sql: `INSERT INTO cloud_dispositivi (dispositivo, nome, endpoint, creata, aggiornata) VALUES (?, ?, ?, ?, ?)
              ON CONFLICT (dispositivo) DO UPDATE SET nome = excluded.nome, endpoint = excluded.endpoint,
              aggiornata = excluded.aggiornata`,
        params: [i.dispositivo, nome, endpoint, now, now],
      },
    ]);
    return { status: 200, body: { ok: true, iscritto: true, dispositivo: i.dispositivo, nome } };
  }
  if (azione === "annulla") {
    if (typeof i.dispositivo !== "string" || !DEVICE.test(i.dispositivo))
      return bad("dispositivo non valido");
    await db.batch([{ sql: "DELETE FROM cloud_dispositivi WHERE dispositivo = ?", params: [i.dispositivo] }]);
    return { status: 200, body: { ok: true, iscritto: false, dispositivo: i.dispositivo } };
  }
  if (azione === "imposta") {
    if (typeof i.attive !== "boolean") return bad("attive: true o false");
    await db.batch([
      {
        sql: "INSERT INTO cloud_impostazioni (chiave, valore) VALUES ('notifiche', ?) ON CONFLICT (chiave) DO UPDATE SET valore = excluded.valore",
        params: [i.attive ? "attive" : "spente"],
      },
    ]);
    return { status: 200, body: { ok: true, attive: i.attive } };
  }
  if (azione === "prova") {
    if (!(await notificheAttive(db))) return { status: 409, body: { ok: false, errore: "notifiche_spente" } };
    await notify({
      chiave: `prova:${now}`,
      tipo: "prova",
      titolo: "CUORE",
      testo: "Notifica di prova: funziona.",
    });
    return { status: 200, body: { ok: true, inviata: true } };
  }
  return { status: 404, body: { ok: false, errore: "non_trovato" } };
}
