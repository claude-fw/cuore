/**
 * Promemoria di CUORE ONLINE: porting fedele di `app/bridge/promemoria.py` (stesse chiavi `sch_*`/`fin_*`, stesse
 * impostazioni `cuore.promemoria.impostazioni/1`, stessi testi, stesse chiavi anti-ripetizione, stessi raggruppamenti),
 * con le impostazioni e la memoria «gia' mostrati» nel Durable Object Finance invece che in file del Mac.
 * Le funzioni di decisione sono pure: ricevono i feed gia' letti (School: l'ultimo feed del Mac nel DO School; Money: il
 * feed calcolato nel DO Finance) e l'istante; la consegna (notifica registrata + push) e' in automazioni.ts.
 * Differenze dichiarate dal Mac: niente file (le impostazioni sono una riga di `cloud_impostazioni`, «presente» se
 * scritta almeno una volta), orari = Cron di Cloudflare (07:15, 15:00, 19:00 ora italiana), compiti fatti e impegni
 * dall'archivio personale School del cloud (school-personale.ts), appunti dal cloud (appunti.ts).
 */
import type { Db } from "../../finance/src/db/types.ts";
import { romaIso } from "./online.ts";

export const SETTINGS_SCHEMA = "cuore.promemoria.impostazioni/1";
export const KEEP_DAYS = 400;
export const MOMENTI: Record<string, string> = { mattina: "07:15", pomeriggio: "15:00", sera: "19:00" };
const ALL = ["mattina", "pomeriggio", "sera"] as const;
const SERA = ["sera"] as const;
type R = readonly string[];
// chiave: area, titolo, momenti predefiniti, momenti possibili, acceso nel modello, proposta nuova
export const REMINDERS: Record<string, [string, string, R, R, boolean, boolean]> = {
  sch_verifica: ["school", "Verifica domani o tra pochi giorni", ALL, ALL, true, false],
  sch_compiti: ["school", "Compito per domani non segnato fatto", ALL, ALL, true, false],
  sch_assenze: ["school", "Giustificazioni da fare (lunedì e giovedì)", ALL, ALL, true, false],
  sch_dati: ["school", "Dati School non aggiornati", ALL, ALL, true, false],
  sch_voti: ["school", "Voto nuovo", ALL, ALL, true, false],
  sch_comunicazioni: ["school", "Comunicazione o circolare nuova", ALL, ALL, true, false],
  sch_impegni: ["school", "Impegni personali di domani", SERA, ALL, true, false],
  sch_compiti_sera: [
    "school",
    "La sera: compiti per domani ancora da segnare fatti",
    SERA,
    SERA,
    false,
    true,
  ],
  fin_budget: ["finance", "Budget del mese quasi finito o superato", ALL, ALL, true, false],
  fin_dati: ["finance", "Dati Finance vecchi", ALL, ALL, true, false],
  fin_classificare: ["finance", "Movimenti da classificare", ALL, ALL, true, false],
  fin_contanti: ["finance", "Segna le spese in contanti", SERA, SERA, true, false],
  fin_appunti: ["finance", "Appunti con promemoria", ALL, ALL, true, false],
  fin_settimana: ["finance", "Domenica sera: guarda le spese della settimana", SERA, SERA, false, true],
};
export const KINDS = Object.keys(REMINDERS);
export const BASELINE = ["sch_voti", "sch_comunicazioni"];
export const FEED_KINDS = {
  school: [
    "sch_verifica",
    "sch_compiti",
    "sch_assenze",
    "sch_dati",
    "sch_voti",
    "sch_comunicazioni",
    "sch_compiti_sera",
  ],
  money: ["fin_budget", "fin_dati", "fin_classificare"],
};
const THRESHOLDS = {
  verifica_giorni: [1, 3],
  school_dati_ore: 24,
  budget_percento: 85,
  finance_dati_giorni: 7,
};
const GROUPS: Record<string, string> = {
  needs: "Necessità",
  wants: "Desideri",
  savings: "Risparmio",
  unassigned: "Senza gruppo",
};
const DONE_STATES = ["consegnato", "restituito"];
const COMMS: Record<string, string> = {
  notice: "Circolare nuova",
  announcement: "Annuncio nuovo su Classroom",
};

export class ConfigNonValida extends Error {}

// biome-ignore lint/suspicious/noExplicitAny: documenti JSON letti come nel Python
type Rec = Record<string, any>;
export interface Settings {
  attivo: boolean;
  riservato: boolean;
  promemoria: Record<string, { attivo: boolean; momenti: string[] }>;
  soglie: {
    verifica_giorni: number[];
    school_dati_ore: number;
    budget_percento: number;
    finance_dati_giorni: number;
  };
  nota?: string;
}
export interface Engine {
  on: Record<string, boolean>;
  momenti: Record<string, string[]>;
  soglie: Settings["soglie"];
  riservato: boolean;
}
export interface Reminder {
  chiave: string;
  tipo: string;
  gruppo: string;
  testo: string;
  riservato: string;
  plurale: string;
  dettaglio: string | null;
}

const isObj = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);
const isInt = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v);
const extra = (o: Rec, allowed: string[]) => Object.keys(o).some((k) => !allowed.includes(k));

// ------------------------------------------------------------------------------------------------ date (ora italiana)
const DAY_MS = 86_400_000;
export const addDays = (day: string, n: number) =>
  new Date(Date.parse(`${day}T00:00:00Z`) + n * DAY_MS).toISOString().slice(0, 10);
const daysBetween = (a: string, b: string) =>
  Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / DAY_MS);
/** 0 = lunedi' ... 6 = domenica (come `weekday()` di Python) */
const weekday = (day: string) => (new Date(`${day}T00:00:00Z`).getUTCDay() + 6) % 7;
function isoWeek(day: string): [number, number, number] {
  const d = new Date(`${day}T00:00:00Z`);
  const wd = weekday(day) + 1;
  d.setUTCDate(d.getUTCDate() + 4 - wd);
  const y = d.getUTCFullYear();
  const w = Math.floor((d.getTime() - Date.UTC(y, 0, 1)) / DAY_MS / 7) + 1;
  return [y, w, wd];
}
const pad = (n: number) => String(n).padStart(2, "0");
const isoWeekKey = (day: string) => {
  const [y, w] = isoWeek(day);
  return `${y}-W${pad(w)}`;
};
/** Istante dell'ora locale di Roma `day` + `hm` (con il cambio d'ora). */
export function romeAt(day: string, hm: string): number {
  const guess = Date.parse(`${day}T${hm}:00Z`);
  let t = guess;
  for (let i = 0; i < 2; i++) {
    const iso = romaIso(new Date(t));
    const off = Date.parse(`${iso.slice(0, 19)}Z`) - t;
    t = guess - off;
  }
  return t;
}
const parseDt = (v: unknown): number | null =>
  typeof v === "string" &&
  /^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?(Z|[+-]\d\d:\d\d)$/.test(v) &&
  !Number.isNaN(Date.parse(v))
    ? Date.parse(v)
    : null;

// ------------------------------------------------------------------------------------------------ impostazioni
function bool(v: unknown, name: string): boolean {
  if (typeof v !== "boolean") throw new ConfigNonValida(`${name}: true o false`);
  return v;
}

function moments(k: string, v: unknown): string[] {
  const allowed = REMINDERS[k]?.[3] ?? [];
  if (!Array.isArray(v) || !v.length || new Set(v).size !== v.length || !v.every((m) => allowed.includes(m)))
    throw new ConfigNonValida(`${k}.momenti: elenco non vuoto fra ${allowed.join(", ")}`);
  return ALL.filter((m) => v.includes(m));
}

/** Come `normalize` del Mac: impostazioni complete e canoniche oppure ConfigNonValida. */
export function normalize(raw: unknown): Settings {
  if (!isObj(raw) || extra(raw, ["attivo", "riservato", "promemoria", "soglie", "nota"]))
    throw new ConfigNonValida("campi ammessi: attivo, riservato, promemoria, soglie, nota");
  const on = raw.promemoria ?? {};
  if (!isObj(on) || extra(on, KINDS))
    throw new ConfigNonValida(`promemoria: chiavi ammesse ${KINDS.join(", ")}`);
  const rems: Settings["promemoria"] = {};
  for (const k of KINDS) {
    let v = on[k] ?? false;
    if (typeof v === "boolean") v = { attivo: v };
    if (!isObj(v) || extra(v, ["attivo", "momenti"]))
      throw new ConfigNonValida(`${k}: true|false oppure {attivo, momenti}`);
    rems[k] = {
      attivo: bool(v.attivo ?? false, `${k}.attivo`),
      momenti: "momenti" in v ? moments(k, v.momenti) : [...(REMINDERS[k]?.[2] ?? [])],
    };
  }
  const given = raw.soglie ?? {};
  if (!isObj(given)) throw new ConfigNonValida(`soglie ammesse: ${Object.keys(THRESHOLDS).join(", ")}`);
  const th: Rec = { ...THRESHOLDS, ...given };
  if (extra(th, Object.keys(THRESHOLDS)))
    throw new ConfigNonValida(`soglie ammesse: ${Object.keys(THRESHOLDS).join(", ")}`);
  const days = th.verifica_giorni;
  if (!Array.isArray(days) || !days.length || !days.every((d) => isInt(d) && d >= 1 && d <= 14))
    throw new ConfigNonValida("verifica_giorni: elenco di giorni da 1 a 14");
  for (const [k, lo, hi] of [
    ["school_dati_ore", 6, 24 * 14],
    ["budget_percento", 50, 99],
    ["finance_dati_giorni", 2, 90],
  ] as const)
    if (!isInt(th[k]) || th[k] < lo || th[k] > hi) throw new ConfigNonValida(`${k}: intero da ${lo} a ${hi}`);
  const nota = raw.nota;
  if (nota !== undefined && nota !== null && (typeof nota !== "string" || nota.length > 1000))
    throw new ConfigNonValida("nota: testo fino a 1000 caratteri");
  const out: Settings = {
    attivo: bool(raw.attivo ?? false, "attivo"),
    riservato: bool(raw.riservato ?? true, "riservato"),
    promemoria: rems,
    soglie: th as Settings["soglie"],
  };
  return typeof nota === "string" ? { ...out, nota } : out;
}

export const engine = (n: Settings): Engine => ({
  on: Object.fromEntries(KINDS.map((k) => [k, n.promemoria[k]?.attivo === true])),
  momenti: Object.fromEntries(KINDS.map((k) => [k, n.promemoria[k]?.momenti ?? []])),
  soglie: n.soglie,
  riservato: n.riservato,
});

/** Tutto spento, in generale e uno per uno (decisione del 05/11). */
export const modelSettings = () => normalize({ attivo: false });

export type FileState = "presente" | "assente" | "non_valido";

/** Impostazioni del cloud: riga `promemoria` di `cloud_impostazioni` (assente = modello spento). */
export async function readSettings(db: Db): Promise<[Settings, FileState, string | null]> {
  const row = await db.first<{ valore: string }>(
    "SELECT valore FROM cloud_impostazioni WHERE chiave = 'promemoria'",
  );
  if (row === null) return [modelSettings(), "assente", null];
  try {
    return [normalize(JSON.parse(row.valore)), "presente", null];
  } catch (e) {
    return [modelSettings(), "non_valido", e instanceof ConfigNonValida ? e.message : "JSON non valido"];
  }
}

const saveSetting = (db: Db, chiave: string, valore: unknown) =>
  db.batch([
    {
      sql: "INSERT INTO cloud_impostazioni (chiave, valore) VALUES (?, ?) ON CONFLICT (chiave) DO UPDATE SET valore = excluded.valore",
      params: [chiave, JSON.stringify(valore)],
    },
  ]);

/** Come `update_settings` del Mac: cambiamento parziale, validato, poi lo stato nuovo. */
export async function updateSettings(db: Db, change: unknown, nowMs: number): Promise<Rec> {
  if (
    !isObj(change) ||
    !Object.keys(change).length ||
    extra(change, ["attivo", "riservato", "promemoria", "soglie"])
  )
    throw new ConfigNonValida("modifica: almeno uno fra attivo, riservato, promemoria, soglie");
  const [cur, state, err] = await readSettings(db);
  if (state === "non_valido")
    throw new ConfigNonValida(`impostazioni attuali non valide (${err}): non modificate`);
  const next: Rec = JSON.parse(JSON.stringify(cur));
  for (const k of ["attivo", "riservato"]) if (k in change) next[k] = change[k];
  const rems = change.promemoria ?? {};
  if (!isObj(rems) || extra(rems, KINDS))
    throw new ConfigNonValida(`promemoria: chiavi ammesse ${KINDS.join(", ")}`);
  for (const [k, v0] of Object.entries(rems)) {
    const v = typeof v0 === "boolean" ? { attivo: v0 } : v0;
    if (!isObj(v) || !Object.keys(v).length || extra(v, ["attivo", "momenti"]))
      throw new ConfigNonValida(`${k}: true|false oppure {attivo?, momenti?}`);
    Object.assign(next.promemoria[k], v);
  }
  if ("soglie" in change) {
    if (!isObj(change.soglie)) throw new ConfigNonValida("soglie: oggetto");
    Object.assign(next.soglie, change.soglie);
  }
  await saveSetting(db, "promemoria", normalize(next));
  return settingsStatus(db, nowMs);
}

const at = (day: string, moment: string) => romeAt(day, MOMENTI[moment] as string);
const romeDay = (ms: number) => romaIso(new Date(ms)).slice(0, 10);

/** Prossimo orario dopo `now` in cui il promemoria puo' partire. */
export function nextCheck(kind: string, momenti: string[], nowMs: number): number | null {
  for (let i = 0; i < 9; i++) {
    const day = addDays(romeDay(nowMs), i);
    for (const m of kind !== "fin_settimana" || weekday(day) === 6 ? momenti : []) {
      const t = at(day, m);
      if (t > nowMs) return t;
    }
  }
  return null;
}

/** Documento per le Impostazioni dell'app, stesso formato del ponte del Mac. */
export async function settingsStatus(db: Db, nowMs: number): Promise<Rec> {
  const [settings, state, err] = await readSettings(db);
  const rows = Object.entries(REMINDERS).map(([k, [area, titolo, , allowed, , proposta]]) => {
    const r = settings.promemoria[k] as { attivo: boolean; momenti: string[] };
    const on = settings.attivo && state === "presente" && r.attivo;
    const nxt = on ? nextCheck(k, r.momenti, nowMs) : null;
    return {
      chiave: k,
      area,
      titolo,
      acceso: r.attivo,
      momenti: r.momenti,
      momenti_possibili: [...allowed],
      proposta,
      prossimo_controllo: nxt === null ? null : romaIso(new Date(nxt)),
    };
  });
  const doc: Rec = {
    schema: SETTINGS_SCHEMA,
    file: state,
    attivo: settings.attivo && state === "presente",
    riservato: settings.riservato,
    momenti: { ...MOMENTI },
    soglie: settings.soglie,
    promemoria: rows,
    generato: romaIso(new Date(Math.floor(nowMs / 1000) * 1000)),
    automazione: "online: Cron di Cloudflare alle 07:15, 15:00 e 19:00 ora italiana (notifiche Web Push)",
  };
  return err ? { ...doc, errore: err } : doc;
}

/** Istante da cui un appunto va ricordato; solo data = inizio del giorno (ora italiana). */
export function dueAt(when: unknown): number | null {
  if (typeof when !== "string") return null;
  const m = /^(\d{4}-\d\d-\d\d)(?:T(\d\d:\d\d))?$/.exec(when);
  if (m === null) return null;
  const t = romeAt(m[1] as string, m[2] ?? "00:00");
  return Number.isNaN(t) ? null : t;
}

/** Quando partira' il promemoria di un appunto (primo orario utile dalla sua data); null = spento. */
export function appuntoSend(settings: Settings, quando: unknown, _nowMs: number): number | null {
  const r = settings.promemoria.fin_appunti as { attivo: boolean; momenti: string[] };
  const when = dueAt(quando);
  if (!(settings.attivo && r.attivo) || when === null) return null;
  for (let i = 0; i < 2; i++)
    for (const m of r.momenti) {
      const t = at(addDays(romeDay(when), i), m);
      if (t >= when) return t;
    }
  return null;
}

/** Momento di un'esecuzione: l'ultimo orario gia' passato oggi; null prima del primo. */
export function momentOf(nowMs: number): string | null {
  const t = romaIso(new Date(nowMs)).slice(11, 16);
  return [...ALL].reverse().find((m) => (MOMENTI[m] as string) <= t) ?? null;
}

// ------------------------------------------------------------------------------------------------ decisione (pura)
const ddmm = (iso: string) => `${iso.slice(8, 10)}/${iso.slice(5, 7)}`;
function subject(subjects: Rec, key: unknown): string | null {
  const name = typeof key === "string" ? subjects[key] : undefined;
  if (typeof name !== "string") return name ?? null;
  return name === name.toUpperCase() && name !== name.toLowerCase()
    ? name.charAt(0).toUpperCase() + name.slice(1).toLowerCase()
    : name;
}
const when = (days: number) => (days === 1 ? "domani" : `tra ${days} giorni`);
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

function block(feed: Rec | null, page: string, id: string, emptyOk = false): unknown {
  const doc = feed?.pagine?.[page] ?? {};
  const b = (Array.isArray(doc.blocchi) ? doc.blocchi : []).find((x: unknown) => isObj(x) && x.id === id);
  const ok = emptyOk ? ["dati", "vuoto_valido"] : ["dati"];
  return b && ok.includes(b.stato) ? b.dati : null;
}

export function halfWeek(day: string): string {
  const [y, w, d] = isoWeek(day);
  return `${y}-W${pad(w)}-${d <= 3 ? "lun" : "gio"}`;
}

function grades(feed: Rec | null): [string, unknown][] | null {
  const data = block(feed, "voti", "complessivo", true);
  if (!isObj(data)) return null;
  const out: [unknown, unknown][] = (Array.isArray(data.serie) ? data.serie : [])
    .filter(isObj)
    .map((g: Rec) => [g.uid, g.valore]);
  for (const [why, uids] of Object.entries(isObj(data.esclusi) ? data.esclusi : {}))
    if (why !== "escluso_annullato" && Array.isArray(uids))
      out.push(...uids.map((u): [unknown, unknown] => [u, null]));
  return out.filter((x): x is [string, unknown] => typeof x[0] === "string" && x[0] !== "");
}

function comms(feed: Rec | null): [unknown, unknown, string][] | null {
  const data = block(feed, "home", "novita", true);
  if (!isObj(data)) return null;
  return (Array.isArray(data.ultimi) ? data.ultimi : [])
    .filter((r: unknown) => isObj(r) && r.evento === "create" && r.kind in COMMS && r.uid_fonte)
    .map((r: Rec) => [r.fonte, r.uid_fonte, r.kind]);
}

export const baselineReady = (feed: Rec | null, kind: string) =>
  (kind === "sch_voti" ? grades(feed) : comms(feed)) !== null;

const rem = (
  chiave: string,
  tipo: string,
  gruppo: string,
  testo: string,
  riservato: string,
  plurale: string,
  dettaglio: string | null = null,
): Reminder => ({ chiave, tipo, gruppo, testo, riservato, plurale, dettaglio });

function schoolLastOk(feed: Rec | null): [number, string] | null {
  const raw = feed?.automazione?.job?.ultimo_commit_ok;
  const last = parseDt(raw);
  if (last !== null) return [last, raw];
  const fr = feed?.freschezza ?? {};
  const dates = Object.values(isObj(fr.per_fonte) ? fr.per_fonte : {})
    .filter(isObj)
    .map((s) => [parseDt(s.aggiornato_al), s.aggiornato_al] as [number | null, string])
    .filter((x): x is [number, string] => x[0] !== null);
  return fr.disponibile && dates.length ? dates.reduce((a, b) => (b[0] < a[0] ? b : a)) : null;
}

/** Promemoria School dal feed `cuore.school.feed/1` e dai compiti segnati fatti. */
export function schoolReminders(
  feed: Rec | null,
  cfg: Engine,
  today: string,
  done: Set<string>,
  nowMs: number,
): Reminder[] {
  const { on, soglie: th } = cfg;
  const out: Reminder[] = [];
  const subjects = isObj(feed?.materie) ? (feed?.materie as Rec) : {};
  const tomorrow = addDays(today, 1);
  if (on.sch_verifica)
    for (const v of (block(feed, "verifiche", "verifiche_future") as Rec[] | null) ?? []) {
      if (typeof v?.data !== "string" || !/^\d{4}-\d\d-\d\d$/.test(v.data)) continue;
      const days = daysBetween(today, v.data);
      if (th.verifica_giorni.includes(days) && v.uid) {
        const name = subject(subjects, v.materia);
        const text = name ? `Verifica di ${name} ${when(days)}` : `Verifica ${when(days)}`;
        out.push(
          rem(
            `school:verifica:${v.uid}:${days}`,
            "sch_verifica",
            `verifica:${days}`,
            text,
            `${cap(when(days))} c'è una verifica`,
            `${cap(when(days))} ci sono {n} verifiche`,
            name,
          ),
        );
      }
    }
  if (on.sch_compiti || on.sch_compiti_sera)
    for (const c of (block(feed, "compiti", "compiti_aperti") as Rec[] | null) ?? []) {
      const uid = c?.uid;
      if (c?.scadenza !== tomorrow || !uid || done.has(uid) || DONE_STATES.includes(c.stato_operativo))
        continue;
      const name = subject(subjects, c.materia);
      const text = name
        ? `Compito di ${name} per domani non segnato fatto`
        : "Compito per domani non segnato fatto";
      if (on.sch_compiti)
        out.push(
          rem(
            `school:compito:${uid}:${tomorrow}`,
            "sch_compiti",
            "compito",
            text,
            "Un compito per domani non è segnato fatto",
            "{n} compiti per domani non sono segnati fatti",
            name,
          ),
        );
      if (on.sch_compiti_sera)
        out.push(
          rem(
            `school:compito-sera:${uid}:${tomorrow}`,
            "sch_compiti_sera",
            "compito-sera",
            text,
            "Hai ancora un compito per domani da segnare fatto",
            "Hai ancora {n} compiti per domani da segnare fatti",
            name,
          ),
        );
    }
  if (on.sch_assenze) {
    const half = halfWeek(today);
    for (const a of (block(feed, "assenze", "eventi") as Rec[] | null) ?? [])
      if (a?.uid && a.stato_giustificazione === "Da giustificare" && a.giustificata !== true) {
        const label = typeof a.data === "string" ? `${a.tipo || "Assenza"} del ${ddmm(a.data)}` : "Assenza";
        out.push(
          rem(
            `school:assenza:${a.uid}:${half}`,
            "sch_assenze",
            "assenza",
            `${label} da giustificare`,
            "C'è una giustificazione da fare",
            "Ci sono {n} giustificazioni da fare",
            label,
          ),
        );
      }
  }
  if (on.sch_voti)
    for (const [uid, value] of grades(feed) ?? []) {
      const shown =
        typeof value === "number"
          ? value.toFixed(2).replace(/0+$/, "").replace(/\.$/, "").replace(".", ",")
          : null;
      out.push(
        rem(
          `school:voto:${uid}`,
          "sch_voti",
          "voto",
          shown ? `Voto nuovo: ${shown}` : "Voto nuovo",
          "È arrivato un voto nuovo",
          "Sono arrivati {n} voti nuovi",
          shown,
        ),
      );
    }
  if (on.sch_comunicazioni)
    for (const [source, uid, kind] of comms(feed) ?? [])
      out.push(
        rem(
          `school:comunicazione:${source}:${uid}`,
          "sch_comunicazioni",
          "comunicazione",
          COMMS[kind] as string,
          "C'è una comunicazione nuova",
          "Ci sono {n} comunicazioni nuove",
          COMMS[kind] as string,
        ),
      );
  if (on.sch_dati) {
    const last = schoolLastOk(feed);
    if (last !== null) {
      const hours = Math.floor((nowMs - last[0]) / 3_600_000);
      if (hours > th.school_dati_ore) {
        const l = romaIso(new Date(last[0]));
        const text = `Dati School non aggiornati da ${hours} ore (ultimo giro riuscito: ${ddmm(l)} ${l.slice(11, 16)})`;
        out.push(rem(`school:dati-vecchi:${last[1]}`, "sch_dati", "school-dati", text, text, text));
      }
    }
  }
  return out;
}

const op = (feed: Rec | null, name: string) => {
  const env = feed?.operazioni?.[name] ?? {};
  return env.ok && feed?.sola_lettura_verificata ? env.data : null;
};
function money(minor: number, currency: string): string {
  const a = Math.abs(minor);
  return `${minor < 0 ? "-" : ""}${Math.floor(a / 100)},${pad(a % 100)}${currency === "EUR" ? " €" : ` ${currency}`}`;
}

/** Promemoria Finance dal feed `cuore.money.feed/1` (solo operazioni riuscite e validate). */
export function financeReminders(feed: Rec | null, cfg: Engine, today: string): Reminder[] {
  const { on, soglie: th } = cfg;
  const out: Reminder[] = [];
  const week = isoWeekKey(today);
  if (on.fin_budget) {
    const data = op(feed, "budgets") ?? {};
    const names = Object.fromEntries((op(feed, "categories")?.items ?? []).map((c: Rec) => [c.id, c.name]));
    const pct = th.budget_percento;
    for (const cur of data.currencies ?? [])
      for (const line of cur.lines ?? []) {
        const budget = line.budgetMinor;
        const spent = line.spentMinor;
        if (!isInt(budget) || !isInt(spent) || budget <= 0) continue;
        const over = line.overBudget === true || spent > budget;
        if (!over && spent * 100 < pct * budget) continue;
        const { scope, target } = line;
        const label =
          scope === "total"
            ? "totale"
            : scope === "budget_group"
              ? (GROUPS[target] ?? target)
              : names[target] || "di una categoria";
        const m = `${money(spent, cur.currency)} su ${money(budget, cur.currency)}`;
        const key = `finance:budget:${data.month ?? "None"}:${scope}:${target || "totale"}:${cur.currency}`;
        if (over)
          out.push(
            rem(
              `${key}:superato`,
              "fin_budget",
              "budget-superato",
              `Budget ${label} superato: ${m}`,
              "Un budget del mese è stato superato",
              "{n} budget del mese sono stati superati",
              label,
            ),
          );
        else
          out.push(
            rem(
              `${key}:soglia`,
              "fin_budget",
              "budget-soglia",
              `Budget ${label} al ${Math.floor((spent * 100) / budget)}%: ${m}`,
              `Un budget del mese ha passato l'${pct}%`,
              `{n} budget del mese hanno passato l'${pct}%`,
              label,
            ),
          );
      }
  }
  if (on.fin_dati && feed?.sola_lettura_verificata) {
    const last = feed?.aggiornamento?.ultimo_movimento;
    const days = typeof last === "string" && /^\d{4}-\d\d-\d\d$/.test(last) ? daysBetween(last, today) : null;
    if (days !== null && days > th.finance_dati_giorni) {
      const text = `Dati Finance fermi al ${ddmm(last)} (${days} giorni fa): servono estratti nuovi`;
      out.push(rem(`finance:dati-vecchi:${last}:${week}`, "fin_dati", "finance-dati", text, text, text));
    }
  }
  if (on.fin_classificare) {
    const n = op(feed, "data_status")?.openReviewItems?.classification_needed;
    if (isInt(n) && n > 0)
      out.push(
        rem(
          `finance:da-classificare:${week}`,
          "fin_classificare",
          "classificare",
          `${n} moviment${n === 1 ? "o" : "i"} da classificare`,
          "Ci sono movimenti da classificare",
          "Ci sono movimenti da classificare",
        ),
      );
  }
  return out;
}

/** Impegni personali di domani, contanti la sera, settimana la domenica, appunti con promemoria arrivato. */
export function personalReminders(
  cfg: Engine,
  nowMs: number,
  impegni: Rec[] | null,
  appunti: Rec[] | null,
): Reminder[] {
  const { on } = cfg;
  const out: Reminder[] = [];
  const today = romeDay(nowMs);
  const tomorrow = addDays(today, 1);
  if (on.sch_impegni)
    for (const e of impegni ?? [])
      if (e.data === tomorrow && e.id)
        out.push(
          rem(
            `school:impegno:${e.id}:${tomorrow}`,
            "sch_impegni",
            "impegno",
            `Domani: ${e.testo}`,
            "Domani hai un impegno personale",
            "Domani hai {n} impegni personali",
            e.testo ?? null,
          ),
        );
  if (on.fin_contanti) {
    const text = "Segna le spese in contanti di oggi";
    out.push(rem(`finance:contanti:${today}`, "fin_contanti", "contanti", text, text, text));
  }
  if (on.fin_settimana && weekday(today) === 6) {
    const text = "Dai un'occhiata alle spese della settimana";
    out.push(rem(`finance:settimana:${isoWeekKey(today)}`, "fin_settimana", "settimana", text, text, text));
  }
  if (on.fin_appunti)
    for (const a of appunti ?? []) {
      const w = a.stato === "aperto" && a.promemoria === true ? dueAt(a.quando) : null;
      if (w !== null && w <= nowMs && a.id)
        out.push(
          rem(
            `finance:appunto:${a.id}:${a.quando}`,
            "fin_appunti",
            "appunto",
            a.testo,
            "Hai un appunto da ricordare",
            "Hai {n} appunti da ricordare",
            a.testo ?? null,
          ),
        );
    }
  return out;
}

export interface Nota {
  gruppo: string;
  titolo: string;
  testo: string;
  chiavi: string[];
}

/** Promemoria dello stesso gruppo uniti in una notifica (come `group` del Mac). */
export function group(reminders: Reminder[], reserved = true): Nota[] {
  const by = new Map<string, Reminder[]>();
  for (const r of reminders) by.set(r.gruppo, [...(by.get(r.gruppo) ?? []), r]);
  return [...by.entries()].map(([gruppo, rs]) => {
    const first = rs[0] as Reminder;
    const titolo = `CUORE · ${first.tipo.startsWith("sch_") ? "School" : "Finance"}`;
    let testo: string;
    if (rs.length === 1) testo = reserved ? first.riservato : first.testo;
    else {
      testo = first.plurale.replace("{n}", String(rs.length));
      const details = rs.map((r) => r.dettaglio).filter((d): d is string => !!d);
      if (!reserved && details.length) testo += `: ${[...new Set(details)].join(", ")}`;
    }
    return { gruppo, titolo, testo, chiavi: rs.map((r) => r.chiave) };
  });
}

/** (da notificare, registrati senza notifica, prime letture nuove), come `plan` del Mac. */
export function planReminders(
  rems: Reminder[],
  shown: Set<string>,
  bases: Record<string, string>,
  ready: Set<string>,
  nowIso: string,
): [Reminder[], Reminder[], Record<string, string>] {
  const fresh = Object.fromEntries([...ready].filter((k) => !(k in bases)).map((k) => [k, nowIso]));
  const nuovi = rems.filter((r) => !shown.has(r.chiave));
  const silent = nuovi.filter((r) => BASELINE.includes(r.tipo) && !(r.tipo in bases));
  return [nuovi.filter((r) => !silent.includes(r)), silent, fresh];
}

// ------------------------------------------------------------------------------------------------ esecuzione nel DO
export const PROMEMORIA_TABLES = [
  "CREATE TABLE IF NOT EXISTS cloud_promemoria_mostrati (chiave TEXT PRIMARY KEY, quando TEXT NOT NULL) STRICT",
];

export interface Fonti {
  school: () => Promise<Rec | null>;
  personale: () => Promise<Rec | null>;
  money: () => Promise<Rec | null>;
  appunti: () => Promise<Rec[] | null>;
}

/**
 * Un turno dei promemoria (`esegui` del Mac) per il momento `moment`: legge solo le fonti che servono, registra lo
 * storico dei «nuovi» alla prima lettura, restituisce le notifiche da consegnare e salva le chiavi mostrate (con la
 * consegna fatta dal chiamante nello stesso turno). Uscita: soli conteggi, mai testi.
 */
export async function runReminders(
  db: Db,
  fonti: Fonti,
  moment: string,
  nowMs: number,
  deliver: (notes: Nota[]) => Promise<void>,
): Promise<Rec> {
  const [settings, state] = await readSettings(db);
  if (!(settings.attivo && state === "presente")) return { saltato: "promemoria_spenti" };
  const cfg = engine(settings);
  const today = romeDay(nowMs);
  const on = Object.fromEntries(
    KINDS.map((k) => [k, cfg.on[k] === true && (cfg.momenti[k] ?? []).includes(moment)]),
  );
  const active: Engine = { ...cfg, on };
  const school = FEED_KINDS.school.some((k) => on[k]) ? await fonti.school() : null;
  const money = FEED_KINDS.money.some((k) => on[k]) ? await fonti.money() : null;
  const needPers = on.sch_compiti || on.sch_compiti_sera || on.sch_impegni;
  const pers = needPers ? await fonti.personale() : {};
  const appunti = on.fin_appunti ? await fonti.appunti() : null;
  const done = new Set(Object.keys(pers?.fatti ?? {}));
  const rems = [
    ...(school !== null ? schoolReminders(school, active, today, done, nowMs) : []),
    ...(money !== null ? financeReminders(money, active, today) : []),
    ...personalReminders(active, nowMs, pers?.impegni ?? null, appunti),
  ];
  const ready = new Set(BASELINE.filter((k) => on[k] && school !== null && baselineReady(school, k)));
  const shown = new Set(
    (await db.all<{ chiave: string }>("SELECT chiave FROM cloud_promemoria_mostrati")).map((r) => r.chiave),
  );
  const basiRow = await db.first<{ valore: string }>(
    "SELECT valore FROM cloud_impostazioni WHERE chiave = 'promemoria_basi'",
  );
  const bases: Record<string, string> = basiRow ? JSON.parse(basiRow.valore) : {};
  const dropped = Object.keys(bases).filter((k) => !cfg.on[k]);
  for (const k of dropped) delete bases[k];
  const nowIso = romaIso(new Date(nowMs));
  const [nuovi, silent, fresh] = planReminders(rems, shown, bases, ready, nowIso);
  Object.assign(bases, fresh);
  const notes = group(nuovi, cfg.riservato);
  await deliver(notes);
  const limit = romaIso(new Date(nowMs - KEEP_DAYS * DAY_MS));
  const keys = [...silent.map((r) => r.chiave), ...notes.flatMap((n) => n.chiavi)];
  await db.batch([
    ...keys.map((k) => ({
      sql: "INSERT OR IGNORE INTO cloud_promemoria_mostrati (chiave, quando) VALUES (?, ?)",
      params: [k, nowIso],
    })),
    { sql: "DELETE FROM cloud_promemoria_mostrati WHERE quando < ?", params: [limit] },
    {
      sql: "INSERT INTO cloud_impostazioni (chiave, valore) VALUES ('promemoria_basi', ?) ON CONFLICT (chiave) DO UPDATE SET valore = excluded.valore",
      params: [JSON.stringify(bases)],
    },
  ]);
  return {
    momento: moment,
    promemoria: rems.length,
    nuovi: nuovi.length,
    prima_lettura: silent.length,
    notifiche: notes.length,
    fonti: {
      school: school === null ? "non richiesta o non disponibile" : "letta",
      finance: money === null ? "non richiesta o non disponibile" : "letta",
      personale_school: !needPers ? "non richiesto" : pers === null ? "illeggibile" : "letto",
      appunti_finance: !on.fin_appunti
        ? "non richiesti"
        : appunti === null
          ? "illeggibili"
          : `letti (${appunti.length})`,
    },
  };
}
