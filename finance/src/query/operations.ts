/**
 * Contratto pubblico di consultazione (v1): elenco delle operazioni, validazione RIGOROSA dell'input (JSON qualsiasi,
 * come arriverebbe da uno strumento MCP o da una dashboard) e busta di risposta uniforme con errori a codice.
 *
 * Nessun trasporto qui: un futuro server MCP dovra' solo mappare ogni operazione su uno strumento e passare l'input
 * a `runQuery`. Questo modulo non apre connessioni e non scrive.
 */
import { NotFoundError, ValidationError } from "../context.ts";
import { PENDING_MIGRATIONS } from "../db/migrations/007_manual_and_budgets.ts";
import { SCHEMA_VERSION } from "../db/migrations/index.ts";
import type { Db } from "../db/types.ts";
import { isSupportedCurrency } from "../domain/money.ts";
import type { SummaryKind } from "../domain/summary.ts";
import { isIsoDate } from "../domain/text.ts";
import {
  CONSULTATION_CONTRACT_VERSION,
  LIMITS,
  queryAccounts,
  queryBalances,
  queryBudgets,
  queryCategories,
  queryDaily,
  queryDataStatus,
  queryManual,
  queryMonthly,
  queryObligations,
  queryOverview,
  queryPlanning,
  querySummary,
  queryTransactions,
  readOnlyDb,
  SUMMARY_KINDS,
} from "./consultation.ts";
import { SEARCH_LIMITS, SearchUnavailableError, searchTransactions } from "./search.ts";

export type QueryErrorCode =
  | "invalid_input"
  | "unknown_operation"
  | "not_found"
  | "schema_mismatch"
  | "not_available"
  | "internal";

export type QueryResponse =
  | { ok: true; contractVersion: number; operation: string; data: unknown }
  | {
      ok: false;
      contractVersion: number;
      operation: string;
      error: { code: QueryErrorCode; message: string };
    };

export interface QueryOptions {
  /** data odierna (YYYY-MM-DD, fuso deciso dal chiamante) usata quando `asOf` manca; senza, `asOf` e' obbligatorio */
  today?: string;
  /**
   * Versione dello schema attesa. Senza: `SCHEMA_VERSION` (percorso reale, invariato). L'unico altro valore ammesso e'
   * quello della copia sintetica (attive + in attesa) e SOLO su un database con `meta.dataset = 'sintetico'` (D3a).
   */
  expectedSchemaVersion?: number;
}

export const SYNTHETIC_QUERY_SCHEMA_VERSION = SCHEMA_VERSION + PENDING_MIGRATIONS.length;

/** Descrizione di un campo d'ingresso: tipo, obbligatorieta' e spiegazione (base per gli schemi MCP futuri). */
export interface FieldSpec {
  type: "date" | "string" | "string[]" | "integer" | "boolean" | "string|null";
  required: boolean;
  description: string;
}

export interface OperationSpec {
  name: string;
  description: string;
  input: Record<string, FieldSpec>;
  run(db: Db, input: Record<string, unknown>, options: QueryOptions): Promise<unknown>;
}

const ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;

// --- validazione dell'input ---------------------------------------------------------------------------------------

function date(input: Record<string, unknown>, key: string): string | undefined {
  const v = input[key];
  if (v === undefined) return undefined;
  if (typeof v !== "string" || !isIsoDate(v)) throw new ValidationError(`${key}: data YYYY-MM-DD attesa`);
  return v;
}

function asOf(input: Record<string, unknown>, options: QueryOptions): string {
  const v = date(input, "asOf") ?? options.today;
  if (v === undefined) throw new ValidationError("asOf obbligatorio (YYYY-MM-DD)");
  if (!isIsoDate(v)) throw new ValidationError("data odierna non valida");
  return v;
}

function requiredDate(input: Record<string, unknown>, key: string): string {
  const v = date(input, key);
  if (v === undefined) throw new ValidationError(`${key} obbligatorio (YYYY-MM-DD)`);
  return v;
}

function oneOf<T extends string>(
  input: Record<string, unknown>,
  key: string,
  allowed: readonly T[],
): T | undefined {
  const v = input[key];
  if (v === undefined) return undefined;
  if (typeof v !== "string" || !allowed.includes(v as T))
    throw new ValidationError(`${key}: valori ammessi ${allowed.join(", ")}`);
  return v as T;
}

function listOf<T extends string>(
  input: Record<string, unknown>,
  key: string,
  check: (s: string) => boolean,
  max: number,
): T[] | undefined {
  const v = input[key];
  if (v === undefined) return undefined;
  if (!Array.isArray(v) || v.length > max || !v.every((x) => typeof x === "string" && check(x)))
    throw new ValidationError(`${key}: elenco di al massimo ${max} valori validi atteso`);
  return [...new Set(v as T[])];
}

function id(input: Record<string, unknown>, key: string): string | undefined {
  const v = input[key];
  if (v === undefined) return undefined;
  if (typeof v !== "string" || !ID.test(v)) throw new ValidationError(`${key}: identificativo non valido`);
  return v;
}

function currency(input: Record<string, unknown>): string | undefined {
  const v = input.currency;
  if (v === undefined) return undefined;
  if (typeof v !== "string" || !isSupportedCurrency(v))
    throw new ValidationError("currency: codice ISO 4217 atteso");
  return v;
}

function integer(input: Record<string, unknown>, key: string): number | undefined {
  const v = input[key];
  if (v === undefined) return undefined;
  if (typeof v !== "number" || !Number.isInteger(v)) throw new ValidationError(`${key}: intero atteso`);
  return v;
}

function month(input: Record<string, unknown>, options: QueryOptions): string {
  const v = input.month ?? options.today?.slice(0, 7);
  if (v === undefined) throw new ValidationError("month obbligatorio (YYYY-MM)");
  if (typeof v !== "string" || !/^[0-9]{4}-(0[1-9]|1[0-2])$/.test(v))
    throw new ValidationError("month: mese YYYY-MM atteso");
  return v;
}

const accountIds = (input: Record<string, unknown>) =>
  listOf<string>(input, "accountIds", (s) => ID.test(s), LIMITS.accountsMax);

// --- operazioni ---------------------------------------------------------------------------------------------------

const ACCOUNTS_FIELD: FieldSpec = {
  type: "string[]",
  required: false,
  description: `conti da includere (max ${LIMITS.accountsMax}); assente = tutti`,
};

export const OPERATIONS: readonly OperationSpec[] = [
  {
    name: "accounts",
    description: "Conti registrati: id, nome, tipo, valuta, titolarita'. Nessun dato bancario.",
    input: {},
    run: (db) => queryAccounts(db),
  },
  {
    name: "categories",
    description:
      "Categorie registrate: id, nome, categoria madre e gruppo di budget (needs, wants, savings; null = senza gruppo, contato come 'unassigned' nel riepilogo).",
    input: {},
    run: (db) => queryCategories(db),
  },
  {
    name: "planning",
    description:
      "Obiettivi di risparmio (stato dichiarato e avanzamento calcolato dagli accantonamenti) e acquisti pianificati (stima null = non registrata). Nessun id di movimenti o di Notion.",
    input: {},
    run: (db) => queryPlanning(db),
  },
  {
    name: "summary",
    description:
      "Riepilogo per periodo e valuta: entrate, spese, rimborsi, trasferimenti e crediti/debiti separati, da classificare, pending. Valute mai sommate.",
    input: {
      from: { type: "date", required: true, description: "inizio periodo (incluso)" },
      to: { type: "date", required: true, description: "fine periodo (inclusa)" },
      accountIds: ACCOUNTS_FIELD,
      currency: { type: "string", required: false, description: "solo questa valuta" },
    },
    run: (db, input) =>
      querySummary(db, {
        from: requiredDate(input, "from"),
        to: requiredDate(input, "to"),
        accountIds: accountIds(input),
        currency: currency(input),
      }),
  },
  {
    name: "daily",
    description: `Serie giornaliera del periodo (al massimo ${LIMITS.dailyDaysMax} giorni) per valuta: entrate, spesa reale (spese + commissioni - rimborsi), uscite da classificare e pending, con le stesse regole del riepilogo; solo i giorni con movimenti.`,
    input: {
      from: { type: "date", required: true, description: "inizio periodo (incluso)" },
      to: { type: "date", required: true, description: "fine periodo (inclusa)" },
    },
    run: (db, input) => queryDaily(db, { from: requiredDate(input, "from"), to: requiredDate(input, "to") }),
  },
  {
    name: "monthly",
    description:
      "Riepilogo mese per mese (al massimo 24 mesi) per valuta, con gli stessi campi di summary, in una sola lettura: per le serie mensili.",
    input: {
      from: { type: "date", required: true, description: "inizio periodo (incluso)" },
      to: { type: "date", required: true, description: "fine periodo (inclusa)" },
    },
    run: (db, input) =>
      queryMonthly(db, { from: requiredDate(input, "from"), to: requiredDate(input, "to") }),
  },
  {
    name: "balances",
    description:
      "Ultimo saldo OSSERVATO per conto fino ad asOf, con eta', riconciliazione e movimenti successivi. Mai ricostruito dai movimenti: verifiedAsOf e' vero solo per un saldo del giorno stesso riconciliato.",
    input: {
      asOf: { type: "date", required: false, description: "data di riferimento; assente = oggi" },
      accountIds: ACCOUNTS_FIELD,
    },
    run: (db, input, options) =>
      queryBalances(db, { asOf: asOf(input, options), accountIds: accountIds(input) }),
  },
  {
    name: "transactions",
    description: `Movimenti per intervallo (data effettiva), con filtri per conto, stato, tipo e categoria; paginati (max ${LIMITS.pageMax}). IBAN e numeri lunghi mascherati.`,
    input: {
      from: { type: "date", required: true, description: "inizio (incluso)" },
      to: { type: "date", required: true, description: "fine (inclusa)" },
      accountIds: ACCOUNTS_FIELD,
      statuses: { type: "string[]", required: false, description: "booked, pending" },
      kinds: { type: "string[]", required: false, description: SUMMARY_KINDS.join(", ") },
      categoryId: {
        type: "string|null",
        required: false,
        description: "categoria decisa; null = senza categoria",
      },
      includeExcluded: { type: "boolean", required: false, description: "includere doppioni confermati" },
      limit: {
        type: "integer",
        required: false,
        description: `1-${LIMITS.pageMax}, predefinito ${LIMITS.pageDefault}`,
      },
      cursor: { type: "string", required: false, description: "nextCursor della pagina precedente" },
    },
    run: (db, input) => {
      const cat = input.categoryId;
      if (cat !== undefined && cat !== null && (typeof cat !== "string" || !ID.test(cat)))
        throw new ValidationError("categoryId: identificativo o null atteso");
      const incl = input.includeExcluded;
      if (incl !== undefined && typeof incl !== "boolean")
        throw new ValidationError("includeExcluded: booleano");
      const cursor = input.cursor;
      if (cursor !== undefined && (typeof cursor !== "string" || cursor.length > 512))
        throw new ValidationError("cursor non valido");
      return queryTransactions(db, {
        from: requiredDate(input, "from"),
        to: requiredDate(input, "to"),
        accountIds: accountIds(input),
        statuses: listOf(input, "statuses", (s) => s === "booked" || s === "pending", 2),
        kinds: listOf<SummaryKind>(input, "kinds", (s) => SUMMARY_KINDS.includes(s as SummaryKind), 7),
        categoryId: cat as string | null | undefined,
        includeExcluded: incl as boolean | undefined,
        limit: integer(input, "limit"),
        cursor: cursor as string | undefined,
      });
    },
  },
  {
    name: "obligations",
    description:
      "Crediti e debiti alla data: residuo, scadenza, giorni alla scadenza, scaduti; posizione per valuta. Non sono entrate ne' spese.",
    input: {
      asOf: { type: "date", required: false, description: "data di riferimento; assente = oggi" },
      status: { type: "string", required: false, description: "open (predefinito), closed, all" },
      direction: { type: "string", required: false, description: "receivable (mi devono), payable (devo)" },
      currency: { type: "string", required: false, description: "solo questa valuta" },
      counterpartyId: { type: "string", required: false, description: "solo questa controparte" },
    },
    run: (db, input, options) =>
      queryObligations(db, {
        asOf: asOf(input, options),
        status: oneOf(input, "status", ["open", "closed", "all"] as const),
        direction: oneOf(input, "direction", ["receivable", "payable"] as const),
        currency: currency(input),
        counterpartyId: id(input, "counterpartyId"),
      }),
  },
  {
    name: "data_status",
    description:
      "Stato di aggiornamento per conto (ultima importazione, ultimo movimento, ultimo saldo, ultima riconciliazione) e anomalie: dati vecchi, import rifiutati, saldi mancanti o in conflitto, riconciliazioni non quadrate.",
    input: {
      asOf: { type: "date", required: false, description: "data di riferimento; assente = oggi" },
      staleAfterDays: {
        type: "integer",
        required: false,
        description: `soglia di dati vecchi, predefinita ${LIMITS.staleAfterDaysDefault}`,
      },
    },
    run: (db, input, options) =>
      queryDataStatus(db, { asOf: asOf(input, options), staleAfterDays: integer(input, "staleAfterDays") }),
  },
  {
    name: "overview",
    description:
      "Panoramica del periodo per valuta: stipendio, sostegno familiare, interessi, altre entrate, spesa reale, trasferimenti tra conti propri (contati una volta), verso i risparmi, da classificare; denaro rimasto per conto e istituto dai soli saldi osservati; data dell'ultimo aggiornamento.",
    input: {
      from: { type: "date", required: true, description: "inizio periodo (incluso)" },
      to: { type: "date", required: true, description: "fine periodo (inclusa)" },
      asOf: { type: "date", required: false, description: "data dei saldi; assente = to" },
    },
    run: (db, input) =>
      queryOverview(db, {
        from: requiredDate(input, "from"),
        to: requiredDate(input, "to"),
        asOf: date(input, "asOf"),
      }),
  },
  {
    name: "manual",
    description:
      "Movimenti inseriti a mano nel periodo (data del movimento), compresi gli annullati con `voided` e data dell'annullamento: gli annullati restano nello storico ma sono fuori da elenchi e riepiloghi.",
    input: {
      from: { type: "date", required: true, description: "inizio (incluso)" },
      to: { type: "date", required: true, description: "fine (inclusa)" },
    },
    run: (db, input) => queryManual(db, { from: requiredDate(input, "from"), to: requiredDate(input, "to") }),
  },
  {
    name: "budgets",
    description:
      "Limiti di spesa in vigore nel mese per valuta: budget, speso (solo uscite classificate e contabilizzate), residuo, superamento; a parte uscite da classificare, in attesa e rimborsi.",
    input: {
      month: { type: "string", required: false, description: "mese YYYY-MM; assente = mese di oggi" },
      currency: {
        type: "string",
        required: false,
        description: "solo questa valuta; assente = quelle con un budget",
      },
    },
    run: (db, input, options) =>
      queryBudgets(db, { month: month(input, options), currency: currency(input) }),
  },
  {
    name: "search",
    description: `Ricerca a testo libero nei movimenti (descrizione, controparte, nota dei manuali): parole come prefisso, tutte presenti, maiuscole e accenti ignorati; filtri per periodo (data effettiva), conto e categoria; esclusi doppioni e manuali annullati; dal piu' recente, max ${SEARCH_LIMITS.pageMax}. IBAN e numeri lunghi mascherati e non ricercabili.`,
    input: {
      q: {
        type: "string",
        required: true,
        description: `parole da cercare (max ${SEARCH_LIMITS.queryMaxLength} caratteri, ${SEARCH_LIMITS.termsMax} parole)`,
      },
      from: { type: "date", required: false, description: "inizio (incluso); assente = nessun limite" },
      to: { type: "date", required: false, description: "fine (inclusa); assente = nessun limite" },
      accountIds: ACCOUNTS_FIELD,
      categoryId: {
        type: "string|null",
        required: false,
        description: "categoria decisa; null = senza categoria",
      },
      limit: {
        type: "integer",
        required: false,
        description: `1-${SEARCH_LIMITS.pageMax}, predefinito ${SEARCH_LIMITS.pageDefault}`,
      },
    },
    run: (db, input) => {
      const cat = input.categoryId;
      if (cat !== undefined && cat !== null && (typeof cat !== "string" || !ID.test(cat)))
        throw new ValidationError("categoryId: identificativo o null atteso");
      return searchTransactions(db, {
        q: input.q as string, // validato da searchTerms
        from: date(input, "from"),
        to: date(input, "to"),
        accountIds: accountIds(input),
        categoryId: cat as string | null | undefined,
        limit: integer(input, "limit"),
      });
    },
  },
];

/**
 * Operazioni che dipendono da una migrazione IN ATTESA: raggiungibili con `runQuery` (e quindi dalla CLI `query`), ma
 * non elencate in `OPERATIONS` (strumenti MCP, stato della vetrina) finche' la migrazione non e' attiva; su un database
 * senza quella migrazione rispondono `not_available`. Vuoto dal 05/11/2026: `search` (v8, A6) e' in `OPERATIONS`.
 */
export const PENDING_OPERATIONS: readonly OperationSpec[] = [];

/**
 * Esegue un'operazione di consultazione. Non lancia mai: restituisce sempre la busta `QueryResponse`.
 * Errori: `invalid_input` (input non valido, chiavi sconosciute comprese), `unknown_operation`, `not_found`
 * (conto inesistente), `schema_mismatch` (database non alla versione del codice), `not_available` (operazione che
 * richiede una migrazione non applicata), `internal` (dettagli non esposti).
 */
export async function runQuery(
  db: Db,
  operation: string,
  input: unknown = {},
  options: QueryOptions = {},
): Promise<QueryResponse> {
  const base = { contractVersion: CONSULTATION_CONTRACT_VERSION, operation };
  const fail = (code: QueryErrorCode, message: string): QueryResponse => ({
    ...base,
    ok: false,
    error: { code, message },
  });
  const spec = [...OPERATIONS, ...PENDING_OPERATIONS].find((o) => o.name === operation);
  if (spec === undefined)
    return fail(
      "unknown_operation",
      `operazione sconosciuta; disponibili: ${OPERATIONS.map((o) => o.name).join(", ")}`,
    );
  if (typeof input !== "object" || input === null || Array.isArray(input))
    return fail("invalid_input", "input: oggetto JSON atteso");
  const unknown = Object.keys(input).filter((k) => !(k in spec.input));
  if (unknown.length > 0) return fail("invalid_input", `campi non previsti: ${unknown.join(", ")}`);
  const ro = readOnlyDb(db);
  try {
    const expected = options.expectedSchemaVersion ?? SCHEMA_VERSION;
    if (expected !== SCHEMA_VERSION) {
      if (expected !== SYNTHETIC_QUERY_SCHEMA_VERSION)
        return fail("schema_mismatch", `versione attesa ${expected} non ammessa`);
      const marker = await ro.first<{ value: string }>("SELECT value FROM meta WHERE key = 'dataset'");
      if (marker?.value !== "sintetico")
        return fail("schema_mismatch", `versione ${expected} ammessa solo su una copia sintetica`);
    }
    const schema = await ro.first<{ v: number | null }>("SELECT MAX(version) AS v FROM schema_migrations");
    if (schema?.v !== expected)
      return fail("schema_mismatch", `database alla versione ${schema?.v ?? 0}, attesa ${expected}`);
  } catch {
    return fail("schema_mismatch", "database non inizializzato");
  }
  try {
    return { ...base, ok: true, data: await spec.run(ro, input as Record<string, unknown>, options) };
  } catch (err) {
    if (err instanceof ValidationError) return fail("invalid_input", err.message);
    if (err instanceof NotFoundError) return fail("not_found", err.message);
    if (err instanceof SearchUnavailableError) return fail("not_available", err.message);
    return fail("internal", "errore interno durante la consultazione");
  }
}
