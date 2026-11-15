/**
 * RICERCA a testo libero sui movimenti (blocco A6; richiede la migrazione v8, IN ATTESA: 008_search.ts).
 *
 *   - cerca in descrizione, controparte e nota dei movimenti manuali; ogni parola vale come prefisso, tutte devono
 *     comparire (AND); maiuscole e accenti ignorati (tokenizer FTS5 `unicode61 remove_diacritics 2`);
 *   - nessun SQL costruito con l'input: la domanda diventa un elenco di parole fatte SOLO di lettere e cifre, ognuna
 *     racchiusa tra virgolette (`"parola"*`) e passata come parametro di MATCH; qualsiasi sintassi FTS5 nella domanda
 *     (virgolette, NEAR, AND, parentesi, `*`, `:`) e' quindi solo testo;
 *   - regole della consultazione v1: sola lettura, importi in unita' minime con la loro valuta (mai sommati), esclusi
 *     doppioni confermati, pending sostituite e manuali annullati (`EXCLUDED_IDS_SQL`), testi con IBAN e numeri
 *     lunghi mascherati (`redactIdentifiers`);
 *   - nessun oracolo sui dati mascherati: un risultato vale solo se ogni parola compare anche nel testo MOSTRATO
 *     (mascherato); cercare le cifre di un IBAN o di una carta non rivela quali movimenti le contengono.
 * Su un database senza v8 la ricerca e' «non disponibile» (`SearchUnavailableError`), senza toccare il resto.
 */
import { NotFoundError, ValidationError } from "../context.ts";
import { migration008, NOTE_OF } from "../db/migrations/008_search.ts";
import type { Db } from "../db/types.ts";
import { redactIdentifiers } from "../domain/redact.ts";
import { isIsoDate } from "../domain/text.ts";
import {
  EFFECTIVE_COLUMNS,
  EFFECTIVE_DATE_SQL,
  EFFECTIVE_FROM,
  type EffectiveColumns,
  EXCLUDED_IDS_SQL,
} from "../services/effective.ts";
import type { TransactionItem } from "./consultation.ts";

export const SEARCH_SCHEMA_VERSION = migration008.version;

export const SEARCH_LIMITS = {
  queryMaxLength: 100,
  termsMax: 8,
  /** parole di 1 carattere ignorate (prefisso troppo largo) */
  termMinLength: 2,
  pageDefault: 20,
  pageMax: 50,
  /** righe candidate lette al massimo prima del controllo sul testo mostrato */
  scanMax: 500,
} as const;

export class SearchUnavailableError extends Error {
  constructor() {
    super(`ricerca non disponibile: richiede la migrazione v${SEARCH_SCHEMA_VERSION}`);
    this.name = "SearchUnavailableError";
  }
}

export interface SearchInput {
  q: string;
  from?: string;
  to?: string;
  accountIds?: string[];
  /** categoria decisa; null = movimenti senza categoria */
  categoryId?: string | null;
  limit?: number;
}

export interface SearchItem extends Omit<TransactionItem, "excluded"> {
  /** nota del movimento manuale (mascherata); null se assente */
  note: string | null;
}

export interface SearchOutput {
  /** parole cercate dopo la normalizzazione (minuscole, senza accenti) */
  terms: string[];
  /** dal piu' recente (data effettiva, poi id) */
  items: SearchItem[];
  limit: number;
  /** vero se ci sono (o potrebbero esserci) altri risultati: restringere parole, periodo o conti */
  truncated: boolean;
}

/** Minuscole, senza accenti, solo sequenze di lettere e cifre: stessa idea del tokenizer `unicode61`. */
function tokens(text: string): string[] {
  return (
    text
      .normalize("NFKD")
      .replace(/\p{M}/gu, "")
      .toLowerCase()
      .match(/[\p{L}\p{N}]+/gu) ?? []
  );
}

export function searchTerms(q: unknown): string[] {
  if (typeof q !== "string" || q.trim() === "") throw new ValidationError("q: testo da cercare obbligatorio");
  if (q.length > SEARCH_LIMITS.queryMaxLength)
    throw new ValidationError(`q: al massimo ${SEARCH_LIMITS.queryMaxLength} caratteri`);
  const terms = [...new Set(tokens(q).filter((t) => t.length >= SEARCH_LIMITS.termMinLength))];
  if (terms.length === 0)
    throw new ValidationError(
      `q: nessuna parola da cercare (servono lettere o cifre, almeno ${SEARCH_LIMITS.termMinLength} per parola)`,
    );
  if (terms.length > SEARCH_LIMITS.termsMax)
    throw new ValidationError(`q: al massimo ${SEARCH_LIMITS.termsMax} parole`);
  return terms;
}

/** Espressione FTS5: ogni parola (solo lettere e cifre) tra virgolette, come prefisso; spazio = AND. */
export function ftsExpression(terms: readonly string[]): string {
  return terms.map((t) => `"${t}"*`).join(" ");
}

export async function searchAvailable(db: Db): Promise<boolean> {
  const row = await db.first<{ v: number | null }>("SELECT MAX(version) AS v FROM schema_migrations");
  return (row?.v ?? 0) >= SEARCH_SCHEMA_VERSION;
}

interface Row extends EffectiveColumns {
  id: string;
  account_id: string;
  status: "booked" | "pending";
  transaction_date: string;
  booking_date: string | null;
  amount_minor: number;
  currency: string;
  description: string;
  counterparty: string | null;
  note: string | null;
}

/** Ricerca in SOLA LETTURA (il chiamante passa un `readOnlyDb`, come fa `runQuery`). */
export async function searchTransactions(db: Db, input: SearchInput): Promise<SearchOutput> {
  if (!(await searchAvailable(db))) throw new SearchUnavailableError();
  const terms = searchTerms(input.q);
  for (const [key, v] of [
    ["from", input.from],
    ["to", input.to],
  ] as const)
    if (v !== undefined && !isIsoDate(v)) throw new ValidationError(`${key}: data YYYY-MM-DD attesa`);
  if (input.from !== undefined && input.to !== undefined && input.from > input.to)
    throw new ValidationError("periodo non valido: from > to");
  const limit = input.limit ?? SEARCH_LIMITS.pageDefault;
  if (!Number.isInteger(limit) || limit < 1 || limit > SEARCH_LIMITS.pageMax)
    throw new ValidationError(`limit deve essere un intero tra 1 e ${SEARCH_LIMITS.pageMax}`);

  for (const id of input.accountIds ?? [])
    if ((await db.first("SELECT 1 AS x FROM accounts WHERE id = ?", [id])) === null)
      throw new NotFoundError("conto", id);

  const inner = [
    "t.rowid IN (SELECT rowid FROM transactions_search WHERE transactions_search MATCH ?)",
    `t.id NOT IN (${EXCLUDED_IDS_SQL})`,
  ];
  const params: (string | number)[] = [ftsExpression(terms)];
  if (input.from !== undefined) {
    inner.push(`${EFFECTIVE_DATE_SQL} >= ?`);
    params.push(input.from);
  }
  if (input.to !== undefined) {
    inner.push(`${EFFECTIVE_DATE_SQL} <= ?`);
    params.push(input.to);
  }
  if (input.accountIds !== undefined && input.accountIds.length > 0) {
    inner.push(`t.account_id IN (${input.accountIds.map(() => "?").join(", ")})`);
    params.push(...input.accountIds);
  }
  let outer = "";
  if (input.categoryId === null) outer = "WHERE q.effective_category_id IS NULL";
  else if (input.categoryId !== undefined) {
    outer = "WHERE q.effective_category_id = ?";
    params.push(input.categoryId);
  }
  params.push(SEARCH_LIMITS.scanMax);
  const rows = await db.all<Row>(
    `SELECT * FROM (
       SELECT t.id, t.account_id, t.status, t.transaction_date, t.booking_date, t.amount_minor, t.currency,
              t.description, t.counterparty, ${NOTE_OF("t.id")} AS note, ${EFFECTIVE_COLUMNS}
       FROM ${EFFECTIVE_FROM}
       WHERE ${inner.join(" AND ")}
     ) q
     ${outer}
     ORDER BY q.effective_date DESC, q.id DESC
     LIMIT ?`,
    params,
  );

  const visible: SearchItem[] = [];
  for (const r of rows) {
    const description = redactIdentifiers(r.description);
    const counterparty = r.counterparty === null ? null : redactIdentifiers(r.counterparty);
    const note = r.note === null ? null : redactIdentifiers(r.note);
    const shown = tokens([description, counterparty ?? "", note ?? ""].join(" "));
    if (!terms.every((term) => shown.some((tok) => tok.startsWith(term)))) continue;
    visible.push({
      id: r.id,
      accountId: r.account_id,
      date: r.effective_date,
      transactionDate: r.transaction_date,
      bookingDate: r.booking_date,
      status: r.status,
      amountMinor: r.amount_minor,
      currency: r.currency,
      description,
      counterparty,
      note,
      kind: r.effective_kind,
      categoryId: r.effective_category_id,
      classification: r.classification_status ?? "none",
      why: null,
    });
  }
  return {
    terms,
    items: visible.slice(0, limit),
    limit,
    truncated: visible.length > limit || rows.length === SEARCH_LIMITS.scanMax,
  };
}
