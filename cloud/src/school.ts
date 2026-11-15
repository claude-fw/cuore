/**
 * Feed School nel cloud (blocco E3, opzione A / S1: «School resta sul Mac e invia solo dati selezionati»).
 * Il Mac invia l'ultimo `cuore.school.feed/1` gia' minimizzato (scuola/mcp/classeviva/school_cloud_push.py); qui lo
 * si valida a schema CHIUSO con gli stessi campi ammessi (`school-feed-campi.json`, unica fonte per Mac e cloud):
 * qualunque campo in piu', una versione diversa, uno stato o un messaggio fuori vocabolario = rifiuto. In piu' nessuna
 * chiave che sembri una credenziale, a qualunque profondita'. Il contenuto dei blocchi (`dati`, `serie`) non si
 * interpreta: e' cio' che l'interfaccia mostra (contratti del motore School).
 *
 * Funzioni pure (nessun runtime Cloudflare): usate dal Durable Object School (school-do.ts) e provate anche in Node.
 */
import CAMPI from "./school-feed-campi.json" with { type: "json" };

export const SCHOOL_FEED_SCHEMA = CAMPI.schema;
export const SCHOOL_MAX_BODY = CAMPI.max_byte;
/** Oltre questa eta' (dal `generato` del Mac) il feed e' «vecchio»: tre giri al giorno, il piu' lungo intervallo e' la notte. */
export const SCHOOL_SOGLIA_ORE = CAMPI.soglia_ore;
/** Tolleranza sull'orologio del Mac: un `generato` piu' avanti di cosi' e' rifiutato. */
export const FUTURE_SKEW_MS = 5 * 60 * 1000;

const MAX_PROBLEMS = 10;
const MAX_BLOCKS = 300;
const MAX_ERRORS = 50;
const set = (a: readonly string[]) => new Set(a);
const K = {
  feed: set(CAMPI.campi.feed),
  required: CAMPI.campi.feed_obbligatori,
  error: set(CAMPI.campi.errore),
  page: set(CAMPI.campi.pagina),
  provenance: set(CAMPI.campi.provenance),
  block: set(CAMPI.campi.blocco),
  chart: set(CAMPI.campi.grafico),
  prov: set(CAMPI.campi.provenienza),
};
const PAGES = set(CAMPI.pagine);
const ESITI = set(CAMPI.esiti);
const FRESHNESS = set(CAMPI.stati_freschezza);
const BLOCK_STATES = set(CAMPI.stati_blocco);
const CHART_TYPES = set(CAMPI.tipi_grafico);
const MESSAGES: Readonly<Record<string, string>> = CAMPI.messaggi_errore;
const FORBIDDEN_KEY = new RegExp(CAMPI.chiavi_vietate, "i");

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const isStr = (v: unknown): v is string => typeof v === "string";
const isNum = (v: unknown) => typeof v === "number" && Number.isFinite(v);
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;

/** Istante ISO con fuso (come `_is_instant` del contratto Python) in millisecondi; null se non valido. */
export function instantMs(v: unknown): number | null {
  if (!isStr(v) || !INSTANT.test(v)) return null;
  const ms = Date.parse(v);
  return Number.isFinite(ms) ? ms : null;
}
const isDate = (v: unknown) => isStr(v) && DATE.test(v) && Number.isFinite(Date.parse(`${v}T00:00:00Z`));

/** Esito della validazione: `codice` fisso per la risposta, `problemi` con soli percorsi di campi (mai valori). */
export type FeedCheck =
  | { ok: true; generatoMs: number; esito: string }
  | { ok: false; codice: string; problemi: string[] };

function extraKeys(o: Obj, allowed: ReadonlySet<string>, where: string, out: string[]): void {
  for (const k of Object.keys(o))
    if (!allowed.has(k)) out.push(`${where}: campo non ammesso «${k.slice(0, 40)}»`);
}

/** Nessuna chiave che sembri una credenziale, a qualunque profondita' (difesa in piu': il Mac le toglie gia'). */
export function forbiddenKeys(v: unknown, where = "feed", out: string[] = [], depth = 0): string[] {
  if (out.length >= MAX_PROBLEMS) return out;
  if (depth > 40) {
    out.push(`${where}: annidamento eccessivo`);
    return out;
  }
  if (Array.isArray(v)) for (const [i, x] of v.entries()) forbiddenKeys(x, `${where}[${i}]`, out, depth + 1);
  else if (isObj(v))
    for (const [k, x] of Object.entries(v)) {
      if (FORBIDDEN_KEY.test(k)) out.push(`${where}: chiave non ammessa (sembra una credenziale)`);
      forbiddenKeys(x, `${where}.${k.slice(0, 40)}`, out, depth + 1);
    }
  return out;
}

function checkProv(v: unknown, where: string, out: string[]): void {
  if (!isObj(v)) out.push(`${where}: oggetto atteso`);
  else extraKeys(v, K.prov, where, out);
}

function checkPage(doc: unknown, name: string, out: string[]): void {
  const w = `pagine.${name}`;
  if (!isObj(doc)) {
    out.push(`${w}: oggetto atteso`);
    return;
  }
  extraKeys(doc, K.page, w, out);
  if (doc.schema !== CAMPI.schema_pagina) out.push(`${w}.schema: versione della pagina non supportata`);
  if (doc.pagina !== name) out.push(`${w}.pagina: diverso dalla chiave`);
  if (doc.materia !== undefined && doc.materia !== null && !isStr(doc.materia))
    out.push(`${w}.materia: testo`);
  const rt = doc.riferimento_temporale;
  if (!isObj(rt) || instantMs(rt.now) === null || !isDate(rt.data_riferimento))
    out.push(`${w}.riferimento_temporale: now (istante con fuso) e data_riferimento obbligatori`);
  const pv = doc.provenance;
  if (!isObj(pv) || !isStr(pv.dashboard_version)) out.push(`${w}.provenance.dashboard_version obbligatoria`);
  else extraKeys(pv, K.provenance, `${w}.provenance`, out);
  const blocks = doc.blocchi;
  if (!Array.isArray(blocks) || blocks.length > MAX_BLOCKS) out.push(`${w}.blocchi: lista attesa`);
  else
    for (const [i, b] of blocks.entries()) {
      const bw = `${w}.blocchi[${i}]`;
      if (!isObj(b)) {
        out.push(`${bw}: oggetto atteso`);
        continue;
      }
      extraKeys(b, K.block, bw, out);
      if (!isStr(b.id) || !isStr(b.titolo)) out.push(`${bw}: id e titolo obbligatori`);
      if (!BLOCK_STATES.has(b.stato as string)) out.push(`${bw}.stato: non ammesso`);
      checkProv(b.provenienza, `${bw}.provenienza`, out);
      if (b.stato === "dati" && (b.dati === null || b.dati === undefined))
        out.push(`${bw}: stato dati senza dati`);
    }
  const charts = doc.grafici;
  if (!Array.isArray(charts) || charts.length > MAX_BLOCKS) out.push(`${w}.grafici: lista attesa`);
  else
    for (const [i, c] of charts.entries()) {
      const cw = `${w}.grafici[${i}]`;
      if (!isObj(c) || !isStr(c.id) || !CHART_TYPES.has(c.tipo as string)) {
        out.push(`${cw}: id e tipo ammesso obbligatori`);
        continue;
      }
      extraKeys(c, K.chart, cw, out);
      checkProv(c.provenienza, `${cw}.provenienza`, out);
      const serie = c.serie;
      if (!Array.isArray(serie) || serie.length === 0) {
        out.push(`${cw}.serie: lista non vuota attesa`);
        continue;
      }
      const okPoint =
        c.tipo === "calendario"
          ? (p: Obj) => isDate(p.x) && isStr(p.y) && p.y.trim() !== ""
          : (p: Obj) => isNum(p.y);
      serie.forEach((s, j) => {
        const pts = isObj(s) ? s.punti : null;
        if (!Array.isArray(pts) || !pts.every((p) => isObj(p) && okPoint(p)))
          out.push(`${cw}.serie[${j}].punti: punti non conformi al tipo`);
      });
    }
}

/**
 * Validazione completa del feed ricevuto (gia' letto come JSON). Stesse regole di `validate_feed` del contratto
 * Python (app/bridge/cuore_school_contract.py) piu' lo schema chiuso dei campi ammessi.
 */
export function checkSchoolFeed(feed: unknown): FeedCheck {
  const no = (codice: string, problemi: string[]): FeedCheck => ({
    ok: false,
    codice,
    problemi: problemi.slice(0, MAX_PROBLEMS),
  });
  if (!isObj(feed)) return no("schema_non_valido", ["feed: oggetto atteso"]);
  if (feed.schema !== SCHOOL_FEED_SCHEMA)
    return isStr(feed.schema) && feed.schema.startsWith(CAMPI.prefisso_schema)
      ? no("versione_non_supportata", [`schema: atteso ${SCHOOL_FEED_SCHEMA}`])
      : no("schema_non_valido", [`schema: atteso ${SCHOOL_FEED_SCHEMA}`]);
  const p: string[] = [];
  extraKeys(feed, K.feed, "feed", p);
  for (const k of K.required) if (!(k in feed)) p.push(`feed.${k}: obbligatorio`);
  if (!ESITI.has(feed.esito as string)) p.push("esito: non ammesso");
  if (typeof feed.sola_lettura_verificata !== "boolean")
    p.push("sola_lettura_verificata: booleano obbligatorio");
  const generatoMs = instantMs(feed.generato);
  if (generatoMs === null) p.push("generato: istante con fuso obbligatorio");
  const pages = feed.pagine;
  if (!isObj(pages)) p.push("pagine: oggetto atteso");
  else
    for (const [name, doc] of Object.entries(pages)) {
      if (!PAGES.has(name)) p.push(`pagine: pagina non ammessa «${name.slice(0, 40)}»`);
      else checkPage(doc, name, p);
    }
  const errs = feed.errori;
  if (!Array.isArray(errs) || errs.length > MAX_ERRORS) p.push("errori: lista attesa");
  else
    errs.forEach((e, i) => {
      if (
        !isObj(e) ||
        !isStr(e.ambito) ||
        e.ambito.length > 40 ||
        !isStr(e.codice) ||
        MESSAGES[e.codice] === undefined ||
        e.messaggio !== MESSAGES[e.codice]
      )
        p.push(`errori[${i}]: {ambito, codice ammesso, messaggio pubblico del codice} attesi`);
      else extraKeys(e, K.error, `errori[${i}]`, p);
    });
  const fr = feed.freschezza;
  if (!isObj(fr) || typeof fr.disponibile !== "boolean" || !FRESHNESS.has(fr.stato as string))
    p.push("freschezza: disponibile (booleano) e stato ammesso obbligatori");
  if (
    feed.esito === "completo" &&
    (!Array.isArray(errs) || errs.length > 0 || !isObj(pages) || !Object.keys(pages).length)
  )
    p.push("esito completo incoerente con errori o pagine assenti");
  if (feed.esito !== "non_disponibile" && feed.sola_lettura_verificata !== true)
    p.push("esito con dati senza sola lettura verificata");
  if (
    feed.automazione !== undefined &&
    !(isObj(feed.automazione) && typeof feed.automazione.disponibile === "boolean")
  )
    p.push("automazione: disponibile (booleano) obbligatorio");
  if (feed.materie !== undefined && !(isObj(feed.materie) && Object.values(feed.materie).every(isStr)))
    p.push("materie: {chiave: nome} atteso");
  if (feed.versione_motore !== undefined && !isStr(feed.versione_motore)) p.push("versione_motore: testo");
  if (feed.data_riferimento !== undefined && !isDate(feed.data_riferimento)) p.push("data_riferimento: data");
  if (feed.contratto !== undefined && !isObj(feed.contratto)) p.push("contratto: oggetto");
  if (
    feed.scritture !== undefined &&
    !(isObj(feed.scritture) && Object.values(feed.scritture).every((v) => v === false))
  )
    p.push("scritture: il Mac non scrive mai sulle fonti (tutti false)");
  if (p.length > 0) return no("non_conforme", p);
  const bad = forbiddenKeys(feed);
  if (bad.length > 0) return no("campo_sensibile", bad);
  return { ok: true, generatoMs: generatoMs as number, esito: feed.esito as string };
}

/** Riga conservata nel Durable Object School: solo l'ultima istantanea. */
export interface StoredFeed {
  readonly generato: string;
  readonly generato_ms: number;
  readonly ricevuto: string;
  readonly byte: number;
  readonly impronta: string;
  readonly feed: string;
}

/**
 * Risposta per l'interfaccia: il feed com'e' arrivato piu' `consegna` (ricevuto, eta', stato). Feed oltre la soglia =
 * «vecchio» e `freschezza.stato` = `dati_vecchi` (mai presentato come aggiornato). Caso comune (recente): nessuna
 * rilettura del JSON, si antepone solo `consegna` al testo conservato (il Mac non puo' inviarla: schema chiuso).
 */
export function deliveryText(row: StoredFeed, nowMs: number): string {
  const etaMs = Math.max(0, nowMs - row.generato_ms);
  const vecchio = etaMs > SCHOOL_SOGLIA_ORE * 3600 * 1000;
  const consegna = {
    ricevuto: row.ricevuto,
    generato: row.generato,
    eta_minuti: Math.floor(etaMs / 60000),
    soglia_ore: SCHOOL_SOGLIA_ORE,
    stato: vecchio ? "vecchio" : "recente",
    byte: row.byte,
  };
  if (!vecchio) return `{"consegna":${JSON.stringify(consegna)},${row.feed.slice(1)}`;
  const feed = JSON.parse(row.feed) as Obj;
  const fr = isObj(feed.freschezza) ? feed.freschezza : { disponibile: false };
  return JSON.stringify({ consegna, ...feed, freschezza: { ...fr, stato: "dati_vecchi" } });
}
