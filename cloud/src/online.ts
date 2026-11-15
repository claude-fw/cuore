/**
 * CUORE online con i DATI VERI (`CUORE_FINANCE_ORIGINE` = "mac", dopo la migrazione M3): la stessa pagina e le stesse
 * API del ponte del Mac (`app/bridge/serve.py`), cosi' `app/index.html` funziona uguale su Mac, iPad e telefono.
 * Tutto passa prima dai controlli di app.ts (interruttore, Access verificato nel Worker, richieste da altri siti).
 *
 *   GET  /                       la pagina con `cuore-online` (+ `cuore-scritture` se `CUORE_SCRITTURE` = "attive")
 *   GET  /api/money              feed `cuore.money.feed/1` calcolato nel Durable Object Finance (stesso piano del ponte:
 *                                app/bridge/cuore_money_query.mts), con provenienza, aggiornamento ed esito
 *   GET  /api/school             gia' in app.ts: l'ultimo feed inviato dal Mac (Durable Object School)
 *   GET  /api/search?q=          `cuore.search/1`: stessa ricerca del ponte (cuore_search.py) su School e Money;
 *                                Classroom ed eventi «non collegati» online
 *   GET  /api/scritture          `cuore.scritture/1`: operazioni ammesse online
 *   GET  /api/events             503 «non collegato»: il registro eventi resta sul Mac
 *   GET  /api/promemoria         impostazioni dei promemoria online (`cuore.promemoria.impostazioni/1`, notifiche.ts)
 *   GET  /api/notifiche          stato del push: chiave pubblica VAPID, interruttore, dispositivi iscritti
 *   GET  /api/notifiche/recenti  le ultime notifiche (12 ore, al massimo 5): le legge il service worker al push
 *   POST /api/notifiche/{iscrizione,annulla,imposta,prova}   dispositivi e interruttore (app.ts, come le scritture)
 *   POST /api/scritture/esegui   `{op, input, requestId}`: Finance → API C2 del DO (idempotente, versione); School →
 *                                note/impegni/compiti fatti nel DO School; promemoria e appunti → DO Finance
 *                                (promemoria.ts, appunti.ts: stessi formati del ponte del Mac)
 */
import { queryMoney } from "../../app/bridge/cuore_money_query.mts";
import type { Db } from "../../finance/src/db/types.ts";

export const ONLINE_META = '<meta name="cuore-online" content="reali">';
export const WRITES_META = '<meta name="cuore-scritture" content="reali">';
export const ONLINE_WRITE_PATH = "/api/scritture/esegui";
export const NOTIFICHE_PATH = "/api/notifiche/";
export const MONEY_SCHEMA = "cuore.money.feed/1";
export const SEARCH_SCHEMA = "cuore.search/1";

/** Operazioni Finance del ponte reale (scritture.py) ammesse online: tutte passano dall'API C2 del Durable Object. */
export const FINANCE_OPS: ReadonlySet<string> = new Set([
  "consulta",
  "aggiungi_manuale",
  "correggi_manuale",
  "annulla_manuale",
  "storico_manuale",
  "imposta_budget",
  "stato_budget",
  "rileva_abbinamenti",
  "importa_movimenti",
  "registro_crediti",
  "conferma_abbinamento",
  "rifiuta_abbinamento",
  "revoca_abbinamento",
  "crea_conto",
  "saldo_manuale",
  "classifica",
  "aggiungi_acquisto",
  "segna_acquisto",
  "pubblica_regole",
  "classifica_tutte",
  "aggiorna_banca",
  "aggiorna_banca_se_serve", // all'apertura dell'app: legge la banca solo se l'ultima lettura ha piu' di 3 ore
  "stato_automazioni", // sola lettura: coda e ultimi lavori del Cron (backup, promemoria, abbinamenti, budget, push)
]);
/** `richiedi_school`: «Sincronizza tutto» dal telefono lascia una richiesta che il Mac esegue appena e' acceso. */
export const SCHOOL_OPS: ReadonlySet<string> = new Set(["school_stato", "school_scrivi", "richiedi_school"]);
/** Promemoria e appunti Finance: portati nel cloud (promemoria.ts, appunti.ts), stessi formati del ponte. */
export const PROMEMORIA_OPS: ReadonlySet<string> = new Set([
  "promemoria_stato",
  "promemoria_imposta",
  "appunti_stato",
  "appunti_scrivi",
]);
const REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{7,99}$/;

type Rec = Record<string, unknown>;
export interface Stub {
  fetch(request: Request): Promise<Response>;
}
export interface Reply {
  status: number;
  body: Rec;
}

/** La pagina online: `app/index.html` con i segnali «online · dati reali» (e «modifiche attive» se aperte). */
export function onlinePage(page: string, writes: boolean): string {
  const i = page.indexOf("<head>");
  if (i < 0) throw new Error("pagina senza <head>");
  return `${page.slice(0, i + 6)}\n${ONLINE_META}${writes ? WRITES_META : ""}${page.slice(i + 6)}`;
}

export const capacita = (writes: boolean): Rec => ({
  schema: "cuore.scritture/1",
  attive: writes,
  dati: "reali",
  origine: "cloud",
  operazioni: writes ? [...FINANCE_OPS, ...SCHOOL_OPS, ...PROMEMORIA_OPS] : [],
  solo_sul_mac: [],
  promemoria: { impostazioni: writes, appunti_finance: writes, notifiche: "web push" },
  finance: { backup: "copia giornaliera cifrata in R2 (ultime 7) e ritiro dal Mac" },
  school: { archivio: "personale nel cloud, separato dal feed del Mac" },
});

// ------------------------------------------------------------------------------------------------ feed Money
const EXPECTED_OPS = [
  "accounts",
  "overview",
  "summary",
  "categories",
  "planning",
  "balances",
  "data_status",
  "obligations",
  "transactions",
  "trend",
  "manual",
  "budgets",
  "daily",
];
const isDate = (v: unknown): v is string => typeof v === "string" && /^\d{4}-\d\d-\d\d/.test(v);

/** Istante locale di Roma con lo scarto (`2026-11-05T21:30:00+01:00`), come il ponte del Mac. */
export function romaIso(d: Date): string {
  const p = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: "Europe/Rome",
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    })
      .formatToParts(d)
      .map((x) => [x.type, x.value]),
  );
  const local = `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}`;
  const off = Math.round((Date.parse(`${local}Z`) - Math.floor(d.getTime() / 1000) * 1000) / 60000);
  const a = Math.abs(off);
  return `${local}${off < 0 ? "-" : "+"}${String(Math.floor(a / 60)).padStart(2, "0")}:${String(a % 60).padStart(2, "0")}`;
}

const maxOf = (xs: string[]) => (xs.length ? xs.reduce((m, x) => (x > m ? x : m)) : null);

/** Aggiornamento dei dati (non della lettura), come `_freshness` di cuore_money.py. */
function aggiornamento(feed: Rec): Rec {
  const ds = ((feed.operazioni as Rec).data_status ?? {}) as Rec;
  const data = (ds.ok === true ? ds.data : {}) as Rec;
  const accts = (Array.isArray(data.accounts) ? data.accounts : []) as Rec[];
  const anomalies = (Array.isArray(data.anomalies) ? data.anomalies : []) as Rec[];
  const dates = accts.map((a) => a.lastBookedOn).filter(isDate);
  const stale = anomalies.filter((a) => String(a.code ?? "").startsWith("stale")).length;
  return {
    letto_il: feed.generato,
    ultimo_movimento: maxOf(dates),
    ultima_importazione: maxOf(
      accts.map((a) => a.lastImportAt).filter((x): x is string => typeof x === "string"),
    ),
    avvisi_dati_vecchi: stale,
    stato: ds.ok !== true ? "non_disponibile" : stale ? "dati_vecchi" : dates.length ? "ok" : "nessun_dato",
  };
}

/**
 * Feed `cuore.money.feed/1` dal motore nel Durable Object. Solo consultazioni (`runQuery` apre il database in sola
 * lettura: `readOnlyDb` rifiuta le scritture). `attesa` = migrazione M3 non ancora arrivata: nessun dato, dichiarato.
 */
export async function moneyFeed(db: Db, now: Date, attesa: boolean): Promise<Rec> {
  const generato = romaIso(now);
  const feed: Rec = {
    schema: MONEY_SCHEMA,
    fonte: "reale",
    origine: "cloud",
    motore: "Cloud Finance · consultazione v1 · Durable Object",
    generato,
    oggi: generato.slice(0, 10),
    operazioni: {},
    errori: [] as Rec[],
    sola_lettura_verificata: false,
    scritture: { database: false, banche: false },
    esito: "non_disponibile",
  };
  const errori = feed.errori as Rec[];
  if (attesa) {
    errori.push({
      ambito: "cloud",
      codice: "in_attesa_dei_dati_del_mac",
      messaggio: "dati del Mac non ancora migrati",
    });
    return feed;
  }
  let out: Rec;
  try {
    out = await queryMoney(db, feed.oggi as string);
  } catch {
    errori.push({ ambito: "lettura", messaggio: "consultazione non riuscita" });
    return feed;
  }
  feed.sola_lettura_verificata = true;
  feed.periodo = out.periodo;
  const ops = (out.operazioni ?? {}) as Rec;
  for (const op of EXPECTED_OPS) {
    let env = ops[op] as Rec | undefined;
    if (typeof env !== "object" || env === null || env.contractVersion !== 1)
      env = {
        ok: false,
        error: { code: "contratto_non_valido", message: "busta della consultazione non riconosciuta" },
      };
    (feed.operazioni as Rec)[op] = env;
    if (env.ok !== true) {
      const e = (env.error ?? {}) as Rec;
      errori.push({ ambito: op, codice: e.code, messaggio: e.message });
    }
  }
  feed.aggiornamento = aggiornamento(feed);
  feed.esito = Object.values(feed.operazioni as Rec).some((e) => (e as Rec).ok === true)
    ? errori.length
      ? "parziale"
      : "completo"
    : "non_disponibile";
  return feed;
}

// ------------------------------------------------------------------------------------------------ ricerca
// Porting fedele di app/bridge/cuore_search.py (school_items, money_items, search): stessi risultati a parita' di feed.
const MIN_Q = 2;
const MAX_Q = 80;
const MAX_RESULTS = 200;
const SCHOOL_SECTION: Record<string, string> = {
  home: "oggi",
  voti: "voti",
  assenze: "assenze",
  verifiche: "verifiche",
  compiti: "compiti",
  materiali: "materiali",
  studio: "studio",
  qualita_dati: "oggi",
};
const TITLE_KEYS = ["testo", "oggetto", "obiettivo", "tipo", "periodo", "materia"];
const DETAIL_KEYS = ["materia", "tipo", "motivo", "affidabilita", "stato_decisione", "origine"];

export class QueryError extends Error {}

export const norm = (s: unknown) =>
  String(s)
    .normalize("NFKD")
    .replace(/[\u0080-\uffff]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
const str = (v: unknown) => (typeof v === "string" && v.trim() ? v.trim() : null);
const isObj = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);
const byKey = (o: Rec) => Object.entries(o).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));

interface Item extends Rec {
  _testo: string;
  _titolo: string;
  data: string | null;
}

function item(
  modulo: string,
  tipo: unknown,
  titolo: string,
  dettaglio: string | null,
  data: string | null,
  rotta: string,
  provenienza: Rec,
  extra = "",
  searchDetail = true,
): Item {
  const words = [titolo, searchDetail ? (dettaglio ?? "") : "", extra];
  return {
    modulo,
    tipo,
    titolo,
    dettaglio,
    data,
    rotta,
    link: null,
    provenienza,
    _testo: norm(words.filter(Boolean).join(" ")),
    _titolo: norm(titolo),
  };
}

export function schoolItems(feed: Rec): Item[] {
  const out: Item[] = [];
  for (const [k, desc] of byKey(isObj(feed.materie) ? feed.materie : {})) {
    const name = str(desc) ?? str(k);
    if (name) out.push(item("school", "materia", name, null, null, "school/studio", { fonte: "materie" }, k));
  }
  for (const [page, doc] of byKey(isObj(feed.pagine) ? feed.pagine : {})) {
    const blocchi = isObj(doc) && Array.isArray(doc.blocchi) ? (doc.blocchi as Rec[]) : [];
    for (const b of blocchi) {
      if (!isObj(b) || b.stato !== "dati") continue;
      const d = b.dati;
      const lists = Array.isArray(d) ? [d] : isObj(d) ? Object.values(d).filter(Array.isArray) : [];
      for (const lst of lists)
        for (const x of lst as unknown[]) {
          if (!isObj(x)) continue;
          const title = TITLE_KEYS.map((k) => str(x[k])).find(Boolean);
          if (!title) continue;
          const det = [...new Set(DETAIL_KEYS.map((k) => str(x[k])).filter((v) => v && v !== title))].join(
            " · ",
          );
          const day =
            typeof x.data === "string" && /^\d{4}-\d\d-\d\d/.test(x.data) ? x.data.slice(0, 10) : null;
          out.push(
            item(
              "school",
              b.titolo || b.id || page,
              title,
              det || null,
              day,
              `school/${SCHOOL_SECTION[page] ?? "oggi"}`,
              { fonte: `pagina ${page}`, blocco: b.id ?? null },
              (b.titolo as string) || "",
            ),
          );
        }
    }
  }
  return out;
}

const money = (minor: unknown, cur: unknown) => {
  if (!Number.isInteger(minor)) return null;
  const a = Math.abs(minor as number);
  return `${(minor as number) < 0 ? "-" : ""}${Math.floor(a / 100)},${String(a % 100).padStart(2, "0")} ${cur || ""}`;
};
const join = (xs: unknown[]) => xs.filter(Boolean).join(" · ");

export function moneyItems(feed: Rec): Item[] {
  const ops = (isObj(feed.operazioni) ? feed.operazioni : {}) as Rec;
  const data = (op: string) => {
    const e = ops[op];
    return isObj(e) && e.ok ? e.data : null;
  };
  const out: Item[] = [];
  for (const a of (data("accounts") as Rec[] | null) ?? [])
    if (str(a.name))
      out.push(
        item(
          "money",
          "conto",
          a.name as string,
          join([a.type, a.currency, a.closed ? "chiuso" : null]),
          (a.lastMovementOn as string) ?? null,
          "money/saldi",
          { fonte: "accounts" },
          (a.id as string) || "",
        ),
      );
  for (const c of ((data("categories") as Rec | null)?.items as Rec[]) ?? [])
    if (str(c.name))
      out.push(
        item(
          "money",
          "categoria",
          c.name as string,
          (c.budgetGroup as string) ?? null,
          null,
          "money/budget",
          {
            fonte: "categories",
          },
        ),
      );
  const plan = (data("planning") as Rec | null) ?? {};
  for (const g of (plan.goals as Rec[]) ?? [])
    if (str(g.name))
      out.push(
        item(
          "money",
          "obiettivo",
          g.name as string,
          (g.status as string) ?? null,
          (g.deadline as string) ?? null,
          "money/budget",
          {
            fonte: "planning",
          },
        ),
      );
  for (const p of (plan.purchases as Rec[]) ?? [])
    if (str(p.title))
      out.push(
        item(
          "money",
          "acquisto pianificato",
          p.title as string,
          (p.status as string) ?? null,
          null,
          "money/budget",
          {
            fonte: "planning",
          },
        ),
      );
  for (const t of ((data("transactions") as Rec | null)?.items as Rec[]) ?? [])
    if (str(t.description)) {
      const m = money(t.amountMinor, t.currency);
      out.push(
        item(
          "money",
          "movimento",
          t.description as string,
          join([m, t.accountId ? `conto ${t.accountId}` : null, t.status === "pending" ? "in attesa" : null]),
          (t.date as string) ?? null,
          "money/movimenti",
          { fonte: "transactions (ultimi 30 giorni)" },
          [t.accountId, m].filter(Boolean).join(" "),
          false,
        ),
      );
    }
  return out;
}

/** Ricerca su School e Money gia' letti. `school`/`money`: il feed, oppure un motivo se non leggibile. */
export function cerca(q: string, limit: number, school: Rec | string, moneyFeed: Rec | string): Rec {
  const query = q.trim();
  const len = [...query].length;
  if (len < MIN_Q || len > MAX_Q)
    throw new QueryError(`la ricerca richiede da ${MIN_Q} a ${MAX_Q} caratteri`);
  const words = norm(query).split(" ").filter(Boolean);
  if (!words.length) throw new QueryError("la ricerca non contiene lettere o cifre");
  const fonti: Rec = {};
  const items: Item[] = [];
  const read = (name: string, feed: Rec | string, ok: (f: Rec) => boolean, build: (f: Rec) => Item[]) => {
    if (typeof feed === "string" || !ok(feed)) {
      fonti[name] = {
        stato: "non_disponibile",
        motivo: typeof feed === "string" ? feed : "feed_non_riconosciuto",
      };
      return;
    }
    const got = build(feed);
    fonti[name] = { stato: "letta", voci: got.length };
    items.push(...got);
  };
  read("school", school, (f) => isObj(f.pagine), schoolItems);
  fonti.classroom = { stato: "non_collegata" };
  read("money", moneyFeed, (f) => isObj(f.operazioni), moneyItems);
  fonti.eventi = { stato: "non_collegata" };
  const hits = items
    .filter((it) => words.every((w) => it._testo.includes(w)))
    .map((it) => ({
      it,
      score: words.reduce((s, w) => s + (new RegExp(`\\b${w}`).test(it._titolo) ? 2 : 1), 0),
    }))
    .sort(
      (a, b) =>
        b.score - a.score ||
        ((b.it.data ?? "") > (a.it.data ?? "") ? 1 : (b.it.data ?? "") < (a.it.data ?? "") ? -1 : 0),
    );
  const n = Math.max(1, Math.min(limit, MAX_RESULTS));
  const risultati = hits
    .slice(0, n)
    .map(({ it }) => Object.fromEntries(Object.entries(it).filter(([k]) => !k.startsWith("_"))));
  return {
    schema: SEARCH_SCHEMA,
    domanda: query,
    risultati,
    totale: hits.length,
    omessi: Math.max(0, hits.length - n),
    fonti,
  };
}

// ------------------------------------------------------------------------------------------------ rotte
const NON_COLLEGATO: Record<string, Rec> = {
  "/api/events": {
    schema: "cuore.events/1",
    eventi: [],
    fonti: {},
    errori: [{ ambito: "cloud", messaggio: "registro degli eventi non collegato online (resta sul Mac)" }],
  },
};

async function leggi(stub: Stub | undefined, url: string): Promise<Rec | string> {
  if (stub === undefined) return "non_collegata";
  try {
    const r = await stub.fetch(new Request(url));
    if (!r.ok) return r.status === 404 ? "nessun_feed" : `risposta_${r.status}`;
    return (await r.json()) as Rec;
  } catch {
    return "durable_object_non_disponibile";
  }
}

async function post(stub: Stub, url: string, doc: Rec): Promise<Reply> {
  try {
    const r = await stub.fetch(new Request(url, { method: "POST", body: JSON.stringify(doc) }));
    return { status: r.status, body: (await r.json()) as Rec };
  } catch {
    return { status: 502, body: { ok: false, errore: "durable_object_non_disponibile" } };
  }
}

/** Feed Money dal DO Finance, con l'indicazione delle scritture online (come `money_with_writes` del ponte). */
async function feedMoney(finance: Stub, writes: boolean): Promise<Rec | string> {
  const f = await leggi(finance, "https://finance.interno/money");
  if (typeof f !== "string" && writes) f.scritture = { database: "dati reali", banche: false, attive: true };
  return f;
}

/** Letture online (dopo Access). `null` = rotta non sua (la gestisce app.ts). */
export async function onlineGet(
  url: URL,
  deps: { finance: Stub; school?: Stub | undefined; writes: boolean },
): Promise<Reply | null> {
  const p = url.pathname;
  if (p === "/api/money") {
    const f = await feedMoney(deps.finance, deps.writes);
    return typeof f === "string"
      ? { status: 502, body: { schema: MONEY_SCHEMA, errore: f } }
      : { status: 200, body: f };
  }
  if (p === "/api/scritture") return { status: 200, body: capacita(deps.writes) };
  if (p === "/api/promemoria") {
    const r = await post(deps.finance, "https://finance.interno/promemoria", {
      op: "promemoria_stato",
      input: {},
    });
    return r.status === 200 && isObj(r.body.risultato) ? { status: 200, body: r.body.risultato } : r;
  }
  if (p === "/api/notifiche" || p === "/api/notifiche/recenti") {
    const f = await leggi(
      deps.finance,
      `https://finance.interno/notifiche/${p.endsWith("recenti") ? "recenti" : "stato"}`,
    );
    return typeof f === "string" ? { status: 502, body: { errore: f } } : { status: 200, body: f };
  }
  if (p in NON_COLLEGATO) return { status: 503, body: NON_COLLEGATO[p] as Rec };
  if (p === "/api/search") {
    const lim = url.searchParams.get("limite") ?? "50";
    if (!/^-?\d{1,6}$/.test(lim))
      return {
        status: 400,
        body: { schema: SEARCH_SCHEMA, errore: "domanda_non_valida", messaggio: "limite non valido" },
      };
    const [school, m] = await Promise.all([
      leggi(deps.school, "https://school.interno/feed"),
      feedMoney(deps.finance, deps.writes),
    ]);
    try {
      return { status: 200, body: cerca(url.searchParams.get("q") ?? "", Number(lim), school, m) };
    } catch (e) {
      if (!(e instanceof QueryError)) throw e;
      return {
        status: 400,
        body: { schema: SEARCH_SCHEMA, errore: "domanda_non_valida", messaggio: e.message },
      };
    }
  }
  return null;
}

/** `POST /api/scritture/esegui` (corpo gia' limitato e verificato da app.ts): smista fra Finance, School e «solo Mac». */
export async function onlineWrite(
  body: ArrayBuffer,
  deps: { finance: Stub; school?: Stub | undefined },
): Promise<Reply> {
  let req: unknown;
  try {
    req = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
  } catch {
    return { status: 400, body: { ok: false, errore: "richiesta_non_valida" } };
  }
  const { op, input = {}, requestId } = (isObj(req) ? req : {}) as Rec;
  if (
    typeof op !== "string" ||
    !isObj(input) ||
    !(FINANCE_OPS.has(op) || SCHOOL_OPS.has(op) || PROMEMORIA_OPS.has(op))
  )
    return { status: 400, body: { ok: false, errore: "operazione_sconosciuta" } };
  const forward = post;
  if (FINANCE_OPS.has(op)) return forward(deps.finance, "https://finance.interno/esegui", req as Rec);
  if (PROMEMORIA_OPS.has(op))
    return forward(deps.finance, "https://finance.interno/promemoria", { op, input, richiesta: requestId });
  if (deps.school === undefined) return { status: 503, body: { ok: false, errore: "school_non_collegato" } };
  if (op === "richiedi_school") return forward(deps.school, "https://school.interno/richiesta", {});
  if (op === "school_scrivi" && (typeof requestId !== "string" || !REQUEST_ID.test(requestId)))
    return { status: 400, body: { ok: false, errore: "requestId_mancante" } };
  return forward(deps.school, "https://school.interno/personale", { op, input, richiesta: requestId });
}
