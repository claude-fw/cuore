/**
 * Collegamenti bancari (`cuore.bank.connections/1`) e stato della sincronizzazione per CUORE (`cuore.bank.sync_status/1`).
 *
 * Il file dei collegamenti e' PRIVATO del titolare, fuori dal repository e da cartelle sincronizzate: id dell'applicazione,
 * percorso (non contenuto) della chiave privata, redirect, e per ogni conto Finance il riferimento del fornitore, la
 * banca, la sessione e la scadenza del consenso. Nessun segreto nel file: la chiave resta nel suo file.
 *
 * Il file di stato e' cio' che CUORE legge per mostrare ultima sincronizzazione, esito, errori e consensi da rinnovare:
 * solo conti, date, esiti, categorie d'errore e conteggi. Mai importi, descrizioni, IBAN o identificativi del fornitore.
 */
import { isAbsolute, resolve } from "node:path";
import type { SyncReport } from "./runner.ts";

export const CONNECTIONS_SCHEMA = "cuore.bank.connections/1";
export const STATUS_SCHEMA = "cuore.bank.sync_status/1";
/** consenso «in scadenza» quando mancano meno di questi giorni: tempo per rinnovarlo con calma */
export const CONSENT_WARN_DAYS = 14;
const UNSAFE_DIRS = [
  "/Library/Mobile Documents/",
  "/Library/CloudStorage/",
  "/Desktop/",
  "/Documents/",
  "/Downloads/",
];

export interface BankConnection {
  accountId: string;
  provider: "enable-banking";
  accountRef: string;
  aspsp: { name: string; country: string };
  sessionId: string | null;
  validUntil: string | null;
}

export interface ConnectionsConfig {
  schema: typeof CONNECTIONS_SCHEMA;
  appId: string;
  keyFile: string;
  redirectUrl: string;
  connections: BankConnection[];
}

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const RFC3339 = /^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?(Z|[+-]\d\d:\d\d)$/;

/** Percorsi privati: assoluti, fuori dal repository e da cartelle sincronizzate o non protette. */
export function privatePathProblems(path: string, repoRoot: string, what: string): string[] {
  if (!isAbsolute(path)) return [`${what}: percorso non assoluto`];
  const real = resolve(path);
  const p: string[] = [];
  for (const d of UNSAFE_DIRS)
    if (real.includes(d)) p.push(`${what}: cartella sincronizzata o non protetta (${d})`);
  if (real === repoRoot || real.startsWith(`${repoRoot}/`)) p.push(`${what}: dentro il repository`);
  return p;
}

export function parseConnections(
  text: string,
  repoRoot: string,
): { config: ConnectionsConfig | null; problems: string[] } {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    return { config: null, problems: ["collegamenti: JSON non valido"] };
  }
  if (!isObj(doc) || doc.schema !== CONNECTIONS_SCHEMA)
    return { config: null, problems: [`collegamenti: schema ${CONNECTIONS_SCHEMA} atteso`] };
  const p: string[] = [];
  if (typeof doc.appId !== "string" || doc.appId === "") p.push("appId assente");
  if (typeof doc.keyFile !== "string") p.push("keyFile assente");
  else p.push(...privatePathProblems(doc.keyFile, repoRoot, "keyFile"));
  if (typeof doc.redirectUrl !== "string" || !/^https:\/\//.test(doc.redirectUrl))
    p.push("redirectUrl: https atteso");
  const conns = Array.isArray(doc.connections) ? doc.connections : [];
  if (!Array.isArray(doc.connections)) p.push("connections: lista attesa");
  const seen = new Set<string>();
  conns.forEach((c, i) => {
    const w = `connections[${i}]`;
    if (!isObj(c)) {
      p.push(`${w}: oggetto atteso`);
      return;
    }
    if (typeof c.accountId !== "string" || c.accountId === "") p.push(`${w}.accountId assente`);
    else if (seen.has(c.accountId)) p.push(`${w}.accountId ripetuto: un collegamento per conto Finance`);
    else seen.add(c.accountId);
    if (c.provider !== "enable-banking") p.push(`${w}.provider: solo enable-banking`);
    if (typeof c.accountRef !== "string" || c.accountRef === "") p.push(`${w}.accountRef assente`);
    if (!isObj(c.aspsp) || typeof c.aspsp.name !== "string" || !/^[A-Z]{2}$/.test(String(c.aspsp.country)))
      p.push(`${w}.aspsp: nome e paese (due lettere) attesi`);
    if (c.validUntil !== null && (typeof c.validUntil !== "string" || !RFC3339.test(c.validUntil)))
      p.push(`${w}.validUntil: istante RFC 3339 con fuso o null`);
  });
  return p.length > 0
    ? { config: null, problems: p }
    : { config: doc as unknown as ConnectionsConfig, problems: [] };
}

export interface ConsentState {
  state: "valido" | "in_scadenza" | "scaduto" | "assente";
  daysLeft: number | null;
  expiresOn: string | null;
}

export function consentState(validUntil: string | null, now: Date): ConsentState {
  if (validUntil === null) return { state: "assente", daysLeft: null, expiresOn: null };
  const t = Date.parse(validUntil);
  if (Number.isNaN(t)) return { state: "assente", daysLeft: null, expiresOn: null };
  const days = Math.floor((t - now.getTime()) / 86_400_000);
  const expiresOn = new Date(t).toISOString().slice(0, 10);
  if (t <= now.getTime()) return { state: "scaduto", daysLeft: 0, expiresOn };
  return { state: days < CONSENT_WARN_DAYS ? "in_scadenza" : "valido", daysLeft: days, expiresOn };
}

export interface StatusEntry {
  conto: string;
  fornitore: string;
  banca: { nome: string; paese: string };
  consenso: { stato: ConsentState["state"]; giorni_rimasti: number | null; scade_il: string | null };
  ultimo_tentativo: {
    at: string;
    /** `in_corso`: registrato PRIMA delle chiamate al fornitore; resta tale se il programma si interrompe */
    esito: SyncReport["status"] | "non_eseguita" | "in_corso";
    errore: string | null;
    riprova_dopo_ore: number | null;
    finestra: { dal: string; al: string } | null;
    strategia: string | null;
    pagine: number;
    nuovi: number;
    aggiornati: number;
    saldi: number;
  } | null;
  ultima_riuscita: { at: string; finestra: { dal: string; al: string } } | null;
}

export interface SyncStatus {
  schema: typeof STATUS_SCHEMA;
  generato: string;
  /** dove e' avvenuta l'ultima sincronizzazione registrata: in questa fase solo `copia` */
  database: "copia" | "reale";
  /** stato scritto solo da collaudi con fonte finta: mai usato per decidere letture reali (e viceversa) */
  collaudo?: true;
  connessioni: StatusEntry[];
}

/**
 * Intervallo minimo fra due letture reali dello stesso conto: 6 ore = al massimo 4 al giorno, il limite tipico delle
 * banche senza l'utente presente (FAQ Enable Banking). La pianificazione prevista ne usa 2.
 */
export const MIN_INTERVAL_HOURS = 6;
/** scarto ammesso fra l'orologio di chi ha scritto lo stato e quello attuale; oltre, l'istante non e' attendibile */
export const FUTURE_TOLERANCE_MS = 5 * 60_000;

const ESITI = new Set([
  "importata",
  "nessun_dato_nuovo",
  "interrotta",
  "rifiutata",
  "non_eseguita",
  "in_corso",
]);
const DAY = /^\d{4}-\d\d-\d\d$/;
const isCount = (v: unknown) => Number.isInteger(v) && (v as number) >= 0;
const isWindow = (v: unknown) => isObj(v) && DAY.test(String(v.dal)) && DAY.test(String(v.al));

/**
 * Stato dei tentativi letto in modo STRETTO: ogni difetto (forma, tipi, istanti, conti ripetuti) e' un problema, mai
 * un «nessun tentativo precedente», che azzererebbe i limiti della banca. I messaggi non riportano il contenuto.
 */
export function parseStatus(text: string, now: Date): { status: SyncStatus | null; problems: string[] } {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    return { status: null, problems: ["stato: JSON non valido (file troncato o modificato a mano?)"] };
  }
  if (!isObj(doc) || doc.schema !== STATUS_SCHEMA)
    return { status: null, problems: [`stato: schema ${STATUS_SCHEMA} atteso`] };
  const p: string[] = [];
  const instant = (v: unknown, w: string) => {
    const t = typeof v === "string" && RFC3339.test(v) ? Date.parse(v) : Number.NaN;
    if (Number.isNaN(t)) p.push(`${w}: istante RFC 3339 atteso`);
    else if (t > now.getTime() + FUTURE_TOLERANCE_MS) p.push(`${w}: istante nel futuro`);
  };
  instant(doc.generato, "stato.generato");
  if (doc.database !== "copia" && doc.database !== "reale") p.push("stato.database: copia o reale atteso");
  if (doc.collaudo !== undefined && doc.collaudo !== true) p.push("stato.collaudo: true o assente atteso");
  if (!Array.isArray(doc.connessioni)) p.push("stato.connessioni: lista attesa");
  const seen = new Set<string>();
  (Array.isArray(doc.connessioni) ? doc.connessioni : []).forEach((c, i) => {
    const w = `stato.connessioni[${i}]`;
    if (!isObj(c)) {
      p.push(`${w}: oggetto atteso`);
      return;
    }
    if (typeof c.conto !== "string" || c.conto === "") p.push(`${w}.conto assente`);
    else if (seen.has(c.conto)) p.push(`${w}.conto ripetuto`);
    else seen.add(c.conto);
    const t = c.ultimo_tentativo;
    if (t !== null) {
      if (!isObj(t)) p.push(`${w}.ultimo_tentativo: oggetto o null atteso`);
      else {
        instant(t.at, `${w}.ultimo_tentativo.at`);
        if (!ESITI.has(String(t.esito))) p.push(`${w}.ultimo_tentativo.esito sconosciuto`);
        if (t.errore !== null && typeof t.errore !== "string") p.push(`${w}.ultimo_tentativo.errore`);
        if (
          t.riprova_dopo_ore !== null &&
          !(
            typeof t.riprova_dopo_ore === "number" &&
            Number.isFinite(t.riprova_dopo_ore) &&
            t.riprova_dopo_ore >= 0
          )
        )
          p.push(`${w}.ultimo_tentativo.riprova_dopo_ore: numero >= 0 o null atteso`);
        if (t.finestra !== null && !isWindow(t.finestra)) p.push(`${w}.ultimo_tentativo.finestra`);
        if (t.strategia !== null && typeof t.strategia !== "string")
          p.push(`${w}.ultimo_tentativo.strategia`);
        for (const k of ["pagine", "nuovi", "aggiornati", "saldi"])
          if (!isCount(t[k])) p.push(`${w}.ultimo_tentativo.${k}: intero >= 0 atteso`);
      }
    }
    const ok = c.ultima_riuscita;
    if (ok !== null) {
      if (!isObj(ok) || !isWindow(ok.finestra)) p.push(`${w}.ultima_riuscita: oggetto o null atteso`);
      else instant(ok.at, `${w}.ultima_riuscita.at`);
    }
  });
  return p.length > 0
    ? { status: null, problems: p }
    : { status: doc as unknown as SyncStatus, problems: [] };
}

/**
 * Stato da scrivere PRIMA di contattare il fornitore: i conti che stanno per essere letti hanno un tentativo `in_corso`
 * all'istante attuale. Se il programma si interrompe dopo le chiamate, il tentativo resta registrato e conta per i
 * limiti (intervallo minimo); l'ultima riuscita e gli altri conti restano come prima.
 */
export function markAttempts(
  config: ConnectionsConfig,
  previous: SyncStatus | null,
  accountIds: readonly string[],
  now: Date,
  database: SyncStatus["database"],
): SyncStatus {
  const st = buildStatus(config, [], now, database, previous);
  for (const e of st.connessioni)
    if (accountIds.includes(e.conto))
      e.ultimo_tentativo = {
        at: now.toISOString(),
        esito: "in_corso",
        errore: null,
        riprova_dopo_ore: null,
        finestra: null,
        strategia: null,
        pagine: 0,
        nuovi: 0,
        aggiornati: 0,
        saldi: 0,
      };
  return st;
}

export type SkipReason =
  | "consenso_da_rinnovare" // scaduto o assente: nessuna chiamata finche' il titolare non lo rinnova
  | "serve_il_titolare" // ultimo errore senza ripresa automatica (consenso revocato, richiesta o risposta da esaminare)
  | "attesa_dopo_errore" // errore temporaneo o limite del fornitore: si attende il tempo indicato
  | "intervallo_minimo" // ultima lettura troppo recente
  | "stato_non_valido"; // istante dell'ultimo tentativo illeggibile o nel futuro: mai interpretato come «mai tentato»

export type AttemptDecision = { due: true } | { due: false; reason: SkipReason; notBefore: string | null };

/**
 * Se un conto puo' essere letto adesso, dal consenso e dall'ultimo tentativo registrato nello stato. Le regole valgono
 * per ogni lettura reale (comando manuale o pianificato): il limite della banca non distingue chi chiama.
 * `retryAfterOwner` = il titolare ha esaminato l'errore e chiede un nuovo tentativo (`--riprova`).
 */
export function attemptDecision(
  c: BankConnection,
  prev: StatusEntry | null,
  now: Date,
  retryAfterOwner = false,
  minHours = MIN_INTERVAL_HOURS,
): AttemptDecision {
  const cs = consentState(c.validUntil, now);
  if (cs.state === "scaduto" || cs.state === "assente")
    return { due: false, reason: "consenso_da_rinnovare", notBefore: null };
  const last = prev?.ultimo_tentativo ?? null;
  if (last === null) return { due: true };
  const at = Date.parse(last.at);
  if (Number.isNaN(at) || at > now.getTime() + FUTURE_TOLERANCE_MS)
    return { due: false, reason: "stato_non_valido", notBefore: null };
  const after = (h: number) => new Date(at + h * 3_600_000);
  const failed = last.esito === "interrotta" || last.esito === "rifiutata";
  if (failed && last.riprova_dopo_ore === null && !retryAfterOwner)
    return { due: false, reason: "serve_il_titolare", notBefore: null };
  if (failed && last.riprova_dopo_ore !== null && after(last.riprova_dopo_ore) > now)
    return {
      due: false,
      reason: "attesa_dopo_errore",
      notBefore: after(last.riprova_dopo_ore).toISOString(),
    };
  if (after(minHours) > now)
    return { due: false, reason: "intervallo_minimo", notBefore: after(minHours).toISOString() };
  return { due: true };
}

/**
 * Nuovo stato: i tentativi di questo giro sostituiscono i precedenti, l'ultima riuscita resta finche' non ce n'e' una nuova.
 * `applied = false`: le letture sono avvenute ma l'aggiornamento del database e' stato bloccato dai controlli; i tentativi
 * restano registrati (contano per i limiti della banca) come `rifiutata` / `applicazione_bloccata`, senza conteggi.
 */
export function buildStatus(
  config: ConnectionsConfig,
  reports: readonly SyncReport[],
  now: Date,
  database: SyncStatus["database"],
  previous: SyncStatus | null = null,
  applied = true,
): SyncStatus {
  const at = now.toISOString();
  const entries = config.connections.map((c): StatusEntry => {
    const read = reports.find((x) => x.accountId === c.accountId) ?? null;
    const r: SyncReport | null =
      read === null || applied || read.status === "interrotta"
        ? read
        : { ...read, status: "rifiutata", import: null };
    const prev = previous?.connessioni.find((x) => x.conto === c.accountId) ?? null;
    const ok = r !== null && (r.status === "importata" || r.status === "nessun_dato_nuovo");
    const cs = consentState(c.validUntil, now);
    return {
      conto: c.accountId,
      fornitore: c.provider,
      banca: { nome: c.aspsp.name, paese: c.aspsp.country },
      consenso: { stato: cs.state, giorni_rimasti: cs.daysLeft, scade_il: cs.expiresOn },
      ultimo_tentativo:
        r === null
          ? (prev?.ultimo_tentativo ?? null)
          : {
              at,
              esito: r.status,
              errore:
                r.error?.kind ??
                (r !== read
                  ? "applicazione_bloccata"
                  : r.status === "rifiutata"
                    ? "pacchetto_rifiutato"
                    : null),
              riprova_dopo_ore: r.retryAfterHours,
              finestra: { dal: r.window.from, al: r.window.to },
              strategia: r.strategy,
              pagine: r.pages,
              nuovi: r.import?.counts.created ?? 0,
              aggiornati: r.import?.counts.updated ?? 0,
              saldi: r.import?.counts.balancesCreated ?? 0,
            },
      ultima_riuscita: ok
        ? { at, finestra: { dal: r.window.from, al: r.window.to } }
        : (prev?.ultima_riuscita ?? null),
    };
  });
  return { schema: STATUS_SCHEMA, generato: at, database, connessioni: entries };
}
