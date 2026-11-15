/**
 * Appunti Finance ONLINE: porting fedele di `app/bridge/finance_appunti.py` (`cuore.finance.appunti/1`), con gli eventi
 * nella tabella `cloud_appunti` del Durable Object Finance invece del file JSON Lines del Mac. Stesse regole: soli eventi
 * aggiunti, mai riscritti o cancellati; `crea`, `modifica`, `promemoria`, `completa`, `annulla`; `richiesta` (il
 * `requestId` della pagina) rende idempotente ogni scrittura. Separati dal motore Finance (tabelle del motore intatte).
 * Gli appunti scritti qui NON tornano da soli sul Mac (e viceversa), come le note School online.
 */
import type { Db } from "../../finance/src/db/types.ts";

export const APPUNTI_SCHEMA = "cuore.finance.appunti/1";
export const APPUNTI_TABLE =
  "CREATE TABLE IF NOT EXISTS cloud_appunti (n INTEGER PRIMARY KEY AUTOINCREMENT, richiesta TEXT UNIQUE, evento TEXT NOT NULL) STRICT";
const MAX_EVENTS = 20_000;
const WHEN = /^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$/;
const KEY = /^[a-z0-9][a-z0-9_:.-]{0,80}$/;
const FIELDS = ["acquisto", "categoria", "quando", "testo"]; // ordinati come `sorted(_FIELDS)`

// biome-ignore lint/suspicious/noExplicitAny: eventi JSON
type Ev = Record<string, any>;
export class NonValido extends Error {}

const isObj = (v: unknown): v is Ev => typeof v === "object" && v !== null && !Array.isArray(v);
function text(v: unknown, n: number, name: string): string {
  if (typeof v !== "string" || !v.trim() || v.length > n)
    throw new NonValido(`${name}: testo da 1 a ${n} caratteri`);
  return v.trim();
}
function key(v: unknown, name: string, optional = false): string | null {
  if ((v === null || v === undefined) && optional) return null;
  if (typeof v !== "string" || !KEY.test(v)) throw new NonValido(`${name} non valido`);
  return v;
}
function when(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  if (typeof v === "string" && WHEN.test(v)) {
    const d = new Date(`${v.length === 10 ? `${v}T00:00` : v}:00Z`);
    const [hh, mm] = [Number(v.slice(11, 13) || 0), Number(v.slice(14, 16) || 0)];
    if (!Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === v.slice(0, 10) && hh < 24 && mm < 60)
      return v;
  }
  throw new NonValido("quando: AAAA-MM-GG oppure AAAA-MM-GGTHH:MM (ora italiana)");
}
const field = (name: string, v: unknown) =>
  name === "testo" ? text(v, 300, "testo") : name === "quando" ? when(v) : key(v, name, true);

/** Evento pulito (solo i campi ammessi) o NonValido, come `valida` del Mac. */
export function valida(ev: unknown): Ev {
  if (!isObj(ev)) throw new NonValido("evento non valido");
  const t = ev.tipo;
  if (t === "crea") {
    if (Object.keys(ev).some((k) => ![...FIELDS, "tipo", "promemoria"].includes(k)))
      throw new NonValido("crea: campi ammessi testo, categoria, quando, promemoria, acquisto");
    const out: Ev = { tipo: t };
    for (const k of FIELDS) out[k] = field(k, ev[k]);
    const on = "promemoria" in ev ? ev.promemoria : out.quando !== null;
    if (typeof on !== "boolean") throw new NonValido("promemoria: vero o falso");
    if (on && out.quando === null) throw new NonValido("promemoria acceso senza data: indicare quando");
    return { ...out, promemoria: on };
  }
  const target = key(ev.id, "id");
  if (t === "modifica") {
    const changes: Ev = {};
    for (const k of FIELDS) if (k in ev) changes[k] = field(k, ev[k]);
    if (!Object.keys(changes).length || Object.keys(ev).some((k) => ![...FIELDS, "tipo", "id"].includes(k)))
      throw new NonValido("modifica: indicare almeno uno fra testo, categoria, quando, acquisto");
    return { tipo: t, appunto: target, ...changes };
  }
  if (t === "promemoria") {
    if (typeof ev.attivo !== "boolean") throw new NonValido("attivo: vero o falso");
    return { tipo: t, appunto: target, attivo: ev.attivo };
  }
  if (t === "completa") return { tipo: t, appunto: target };
  if (t === "annulla")
    return { tipo: t, appunto: target, motivo: text(ev.motivo || "annullato", 200, "motivo") };
  throw new NonValido("tipo sconosciuto");
}

const leggi = async (db: Db): Promise<Ev[]> =>
  (await db.all<{ evento: string }>("SELECT evento FROM cloud_appunti ORDER BY n")).map((r) =>
    JSON.parse(r.evento),
  );

function apply(evs: Ev[]): Map<string, Ev> {
  const out = new Map<string, Ev>();
  for (const e of evs) {
    if (e.tipo === "crea") {
      out.set(e.id, {
        id: e.id,
        testo: e.testo,
        categoria: e.categoria ?? null,
        quando: e.quando ?? null,
        promemoria: e.promemoria ?? false,
        acquisto: e.acquisto ?? null,
        stato: "aperto",
        creato: e.at,
        aggiornato: e.at,
        motivo: null,
      });
      continue;
    }
    const a = out.get(e.appunto);
    if (a === undefined) continue;
    a.aggiornato = e.at;
    if (e.tipo === "modifica") {
      for (const k of FIELDS) if (k in e) a[k] = e[k];
      if (a.quando === null) a.promemoria = false;
    } else if (e.tipo === "promemoria") a.promemoria = e.attivo;
    else if (e.tipo === "completa") a.stato = "completato";
    else if (e.tipo === "annulla") [a.stato, a.motivo] = ["annullato", e.motivo];
  }
  return out;
}

/** Come `aggiungi` del Mac: valida, idempotenza per `richiesta`, regole che dipendono dallo stato, poi un evento. */
export async function aggiungi(
  db: Db,
  input: unknown,
  richiesta: string | null,
  now = new Date(),
): Promise<Ev> {
  const ev = valida(input);
  const evs = await leggi(db);
  if (richiesta !== null) {
    if (typeof richiesta !== "string" || richiesta.length < 1 || richiesta.length > 100)
      throw new NonValido("requestId non valido");
    const prev = evs.find((e) => e.richiesta === richiesta);
    if (prev !== undefined) {
      if (Object.keys(ev).some((k) => JSON.stringify(prev[k] ?? null) !== JSON.stringify(ev[k])))
        throw new NonValido("richiesta gia' usata con un contenuto diverso");
      return prev;
    }
  }
  if (evs.length >= MAX_EVENTS) throw new NonValido("archivio pieno");
  if (ev.tipo !== "crea") {
    const a = apply(evs).get(ev.appunto);
    if (a === undefined) throw new NonValido("appunto inesistente");
    if (a.stato !== "aperto") throw new NonValido(`appunto gia' ${a.stato}: non si modifica`);
    if (ev.tipo === "promemoria" && ev.attivo && a.quando === null)
      throw new NonValido("promemoria acceso senza data: indicare quando");
  }
  const rec: Ev = {
    id: crypto.randomUUID().replaceAll("-", ""),
    at: `${now.toISOString().slice(0, 19)}+00:00`,
    ...ev,
    ...(richiesta === null ? {} : { richiesta }),
  };
  await db.batch([
    {
      sql: "INSERT INTO cloud_appunti (richiesta, evento) VALUES (?, ?)",
      params: [richiesta, JSON.stringify(rec)],
    },
  ]);
  return rec;
}

const cmp = (a: unknown, b: unknown) =>
  a === b ? 0 : (a as string | number) < (b as string | number) ? -1 : 1;

/** Tutti gli appunti (aperti prima, per data del promemoria; poi i chiusi), come `stato` del Mac. */
export async function stato(db: Db): Promise<Ev> {
  const evs = await leggi(db);
  const items = [...apply(evs).values()];
  const k = (a: Ev) => [a.stato !== "aperto" ? 1 : 0, a.quando === null ? 1 : 0, a.quando ?? "", a.creato];
  items.sort((x, y) => {
    const [a, b] = [k(x), k(y)];
    for (let i = 0; i < a.length; i++) if (cmp(a[i], b[i])) return cmp(a[i], b[i]);
    return 0;
  });
  return {
    schema: APPUNTI_SCHEMA,
    appunti: items,
    aperti: items.filter((a) => a.stato === "aperto").length,
    eventi: evs.length,
  };
}
