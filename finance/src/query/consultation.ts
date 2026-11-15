/**
 * Livello di CONSULTAZIONE in sola lettura (contratto v1), pensato per i futuri strumenti MCP e per la dashboard.
 *
 * Principi:
 *   - nessuna logica contabile nuova: riepiloghi, liquidita' e crediti/debiti vengono dai servizi esistenti; tipo e
 *     categoria dei movimenti dalla definizione unica di `services/effective.ts` (elenco e riepilogo coincidono);
 *   - sola lettura garantita: il database passa da `readOnlyDb`, che rifiuta qualsiasi `batch`;
 *   - nessun dato inventato: un saldo e' solo un saldo OSSERVATO (mai ricostruito dai movimenti); se manca, e' in
 *     conflitto, non e' riconciliato o non e' del giorno richiesto, l'uscita lo dice esplicitamente;
 *   - dati personali ridotti al necessario: niente righe grezze, file, impronte o id della fonte; IBAN e numeri
 *     lunghi nelle descrizioni mascherati (`redactIdentifiers`).
 * Importi sempre interi in unita' minime della valuta indicata accanto; valute mai sommate tra loro.
 */

import type { Ctx } from "../context.ts";
import { NotFoundError, ValidationError } from "../context.ts";
import type { Db } from "../db/types.ts";
import { addMinor } from "../domain/money.ts";
import type { Direction } from "../domain/obligations.ts";
import { obligationsPosition, type PositionLine } from "../domain/obligations.ts";
import { redactIdentifiers } from "../domain/redact.ts";
import type { BudgetGroup, CurrencySummary, SummaryKind } from "../domain/summary.ts";
import { daysBetween, isIsoDate } from "../domain/text.ts";
import {
  type AccountProfile,
  type AccountStatus,
  isClosedAt,
  listAccountProfiles,
  listAccountStatuses,
  listAccounts,
} from "../services/accounts.ts";
import { type BudgetStatus, budgetStatus } from "../services/budgets.ts";
import {
  EFFECTIVE_COLUMNS,
  EFFECTIVE_DATE_SQL,
  EFFECTIVE_FROM,
  type EffectiveColumns,
} from "../services/effective.ts";
import { listObligations } from "../services/obligations.ts";
import { listGoalsWithProgress, listPlannedPurchases } from "../services/planning.ts";
import {
  dailyReport,
  liquidity,
  monthlyReport,
  type PeriodReport,
  periodReport,
} from "../services/reports.ts";

export const CONSULTATION_CONTRACT_VERSION = 1;

export const LIMITS = {
  /** movimenti per pagina: predefinito e massimo */
  pageDefault: 50,
  pageMax: 200,
  /** conti per richiesta (D1 accetta al massimo 100 parametri per istruzione) */
  accountsMax: 20,
  /** crediti/debiti restituiti (la posizione e' calcolata comunque su tutti quelli filtrati) */
  obligationsMax: 200,
  staleAfterDaysDefault: 7,
  /** giorni al massimo per la serie giornaliera (`daily`) */
  dailyDaysMax: 93,
} as const;

export const SUMMARY_KINDS: readonly SummaryKind[] = [
  "income",
  "expense",
  "transfer",
  "refund",
  "fee",
  "obligation",
  "unknown",
];

/** Database in sola lettura: qualsiasi scrittura e' rifiutata prima di arrivare al motore. */
export function readOnlyDb(db: Db): Db {
  return {
    all: (sql, params) => db.all(sql, params),
    first: (sql, params) => db.first(sql, params),
    batch: async () => {
      throw new Error("consultazione in sola lettura: scrittura rifiutata");
    },
  };
}

/** Contesto per riusare i servizi esistenti SOLO in lettura: orologio e generatore di id non sono disponibili. */
function readCtx(db: Db): Ctx {
  const refuse = (): never => {
    throw new Error("consultazione in sola lettura: operazione di scrittura non ammessa");
  };
  return { db: readOnlyDb(db), now: refuse, newId: refuse };
}

function isoDate(value: string, what: string): void {
  if (!isIsoDate(value)) throw new ValidationError(`${what} non valida: ${value} (YYYY-MM-DD)`);
}

async function accountsById(db: Db, ids?: readonly string[]) {
  const all = await listAccounts(readCtx(db));
  if (ids === undefined || ids.length === 0) return all;
  if (ids.length > LIMITS.accountsMax)
    throw new ValidationError(`al massimo ${LIMITS.accountsMax} conti per richiesta`);
  for (const id of ids) if (!all.some((a) => a.id === id)) throw new NotFoundError("conto", id);
  return all.filter((a) => ids.includes(a.id));
}

// ---------------------------------------------------------------------------------------------------------------
// conti

export interface AccountInfo {
  id: string;
  name: string;
  type: string;
  currency: string;
  institutionId: string | null;
  /** dichiarazione dell'utente (intestatario formale, utilizzatore, supervisione familiare); `unknown` se assente */
  profile: Omit<AccountProfile, "accountId">;
  /** stato operativo dichiarato; data di chiusura solo se nota (con il suo stato), ultimo movimento separato */
  status: Omit<AccountStatus, "accountId">;
}

export async function queryAccounts(db: Db): Promise<AccountInfo[]> {
  const profiles = await listAccountProfiles(readCtx(db));
  const statuses = await listAccountStatuses(readCtx(db));
  return (await listAccounts(readCtx(db))).map((a) => {
    const { accountId: _, ...profile } = profiles.find((p) => p.accountId === a.id) ?? {
      accountId: a.id,
      formalHolder: "unknown" as const,
      primaryUser: "unknown" as const,
      supervision: "unknown" as const,
      note: null,
      declaredAt: null,
    };
    return {
      id: a.id,
      name: a.name,
      type: a.type,
      currency: a.currency,
      institutionId: a.institution_id,
      profile,
      status: (({ accountId: _id, ...rest }) => rest)(
        statuses.find((x) => x.accountId === a.id) ?? {
          accountId: a.id,
          closed: false,
          closureDate: null,
          closureDateStatus: null,
          lastMovementOn: null,
          reason: null,
          declaredAt: null,
        },
      ),
    };
  });
}

// ---------------------------------------------------------------------------------------------------------------
// riepilogo

export interface SummaryInput {
  from: string;
  to: string;
  accountIds?: string[];
  currency?: string;
}

export interface SummaryOutput {
  from: string;
  to: string;
  accounts: string[] | "all";
  currencies: PeriodReport["currencies"];
  excludedDuplicates: number;
  openReviewItems: number;
  /** avvertenze sulla completezza dei numeri (da classificare, pending, verifiche aperte) */
  notes: string[];
}

export async function querySummary(db: Db, input: SummaryInput): Promise<SummaryOutput> {
  isoDate(input.from, "data iniziale");
  isoDate(input.to, "data finale");
  if (input.from > input.to) throw new ValidationError("periodo non valido: from > to");
  await accountsById(db, input.accountIds);
  const report = await periodReport(readCtx(db), input.from, input.to, input.accountIds);
  const currencies = report.currencies.filter(
    (c) => input.currency === undefined || c.currency === input.currency,
  );
  const notes: string[] = [];
  for (const c of currencies) {
    if (c.unclassifiedCount > 0)
      notes.push(
        `${c.currency}: ${c.unclassifiedCount} movimenti da classificare: entrate e spese sono incomplete`,
      );
    if (c.pendingCount > 0)
      notes.push(`${c.currency}: ${c.pendingCount} movimenti in attesa esclusi dai totali`);
  }
  if (report.openReviewItems > 0) notes.push(`${report.openReviewItems} elementi da verificare aperti`);
  if (currencies.length === 0) notes.push("nessun movimento nel periodo e nei filtri indicati");
  return { ...report, currencies, notes };
}

// ---------------------------------------------------------------------------------------------------------------
// serie giornaliera

export interface DailyCurrency {
  currency: string;
  income: number;
  /** spese + commissioni - rimborsi del giorno (stessa definizione del riepilogo) */
  netSpending: number;
  unclassifiedOut: number;
  pendingCount: number;
}

export interface DailyOutput {
  from: string;
  to: string;
  /** solo i giorni con almeno un movimento, in ordine; la somma dei giorni = `summary` dello stesso periodo */
  days: { date: string; currencies: DailyCurrency[] }[];
}

/** Riepilogo mese per mese (al massimo 24 mesi) in una sola lettura: stessi campi di `summary` per valuta. */
export async function queryMonthly(
  db: Db,
  input: { from: string; to: string },
): Promise<{ from: string; to: string; months: { month: string; currencies: CurrencySummary[] }[] }> {
  isoDate(input.from, "data iniziale");
  isoDate(input.to, "data finale");
  if (input.from > input.to) throw new ValidationError("periodo non valido: from > to");
  if (daysBetween(input.from, input.to) > 731)
    throw new ValidationError("periodo troppo lungo: al massimo 24 mesi");
  return { from: input.from, to: input.to, months: await monthlyReport(readCtx(db), input.from, input.to) };
}

export async function queryDaily(db: Db, input: { from: string; to: string }): Promise<DailyOutput> {
  isoDate(input.from, "data iniziale");
  isoDate(input.to, "data finale");
  if (input.from > input.to) throw new ValidationError("periodo non valido: from > to");
  if (daysBetween(input.from, input.to) >= LIMITS.dailyDaysMax)
    throw new ValidationError(`periodo troppo lungo: al massimo ${LIMITS.dailyDaysMax} giorni`);
  const days = await dailyReport(readCtx(db), input.from, input.to);
  return {
    from: input.from,
    to: input.to,
    days: days.map((d) => ({
      date: d.date,
      currencies: d.currencies.map((c) => ({
        currency: c.currency,
        income: c.income,
        netSpending: c.netSpending,
        unclassifiedOut: c.unclassifiedOut,
        pendingCount: c.pendingCount,
      })),
    })),
  };
}

// ---------------------------------------------------------------------------------------------------------------
// saldi

export interface BalancesInput {
  asOf: string;
  accountIds?: string[];
}

export interface BalanceLine {
  accountId: string;
  accountName: string;
  /** conto chiuso alla data: storico conservato, non e' un conto attivo */
  closed: boolean;
  currency: string;
  /** missing = nessun saldo osservato fino ad asOf; conflict = saldi diversi per l'ultima data (inutilizzabile) */
  status: "observed" | "missing" | "conflict";
  /** saldo di fine giornata OSSERVATO (mai calcolato dai movimenti); null se missing o conflict */
  balanceMinor: number | null;
  observedOn: string | null;
  /** giorni tra observedOn e asOf */
  ageDays: number | null;
  /** ultima riconciliazione calcolata che termina in observedOn */
  reconciliation: { status: string; from: string; to: string; differenceMinor: number | null } | null;
  /** movimenti successivi al saldo osservato (fino ad asOf): se > 0 il saldo non e' quello corrente */
  bookedMovementsAfter: number;
  pendingAfter: number;
  /** vero SOLO se: saldo osservato proprio in asOf e riconciliazione ok che termina in quella data */
  verifiedAsOf: boolean;
  notes: string[];
}

export interface BalanceTotal {
  currency: string;
  /** somma dei soli saldi osservati: NON e' un totale se `complete` e' falso */
  observedSumMinor: number;
  accountsIncluded: number;
  accountsWithoutBalance: number;
  complete: boolean;
  allVerifiedAsOf: boolean;
}

export interface BalancesOutput {
  asOf: string;
  accounts: BalanceLine[];
  totals: BalanceTotal[];
}

export async function queryBalances(db: Db, input: BalancesInput): Promise<BalancesOutput> {
  isoDate(input.asOf, "data di riferimento");
  const accounts = await accountsById(db, input.accountIds);
  const statuses = await listAccountStatuses(readCtx(db));
  const lines = (await liquidity(readCtx(db), input.asOf)).filter((l) =>
    accounts.some((a) => a.id === l.accountId),
  );
  const out: BalanceLine[] = [];
  for (const l of lines) {
    const account = accounts.find((a) => a.id === l.accountId);
    const notes: string[] = [];
    let reconciliation: BalanceLine["reconciliation"] = null;
    let bookedAfter = 0;
    let pendingAfter = 0;
    if (l.observedOn !== null) {
      const rec = await db.first<{
        status: string;
        from_date: string;
        to_date: string;
        difference_minor: number | null;
      }>(
        `SELECT status, from_date, to_date, difference_minor FROM reconciliations
         WHERE account_id = ? AND to_date = ? ORDER BY computed_at DESC, id DESC LIMIT 1`,
        [l.accountId, l.observedOn],
      );
      if (rec !== null)
        reconciliation = {
          status: rec.status,
          from: rec.from_date,
          to: rec.to_date,
          differenceMinor: rec.difference_minor,
        };
      const after = await db.first<{ booked: number; pending: number }>(
        `SELECT SUM(status = 'booked' AND booking_date > ? AND booking_date <= ?) AS booked,
                SUM(status = 'pending' AND transaction_date > ? AND transaction_date <= ?) AS pending
         FROM transactions WHERE account_id = ?`,
        [l.observedOn, input.asOf, l.observedOn, input.asOf, l.accountId],
      );
      bookedAfter = after?.booked ?? 0;
      pendingAfter = after?.pending ?? 0;
    }
    const status = l.certainty;
    const ageDays = l.observedOn === null ? null : daysBetween(l.observedOn, input.asOf);
    const verifiedAsOf = status === "observed" && ageDays === 0 && reconciliation?.status === "ok";
    if (status === "missing")
      notes.push("saldo non disponibile: nessun saldo osservato fino alla data richiesta");
    if (status === "conflict") notes.push(`saldi diversi per il ${l.observedOn}: nessun valore utilizzabile`);
    if (status === "observed") {
      if (ageDays !== null && ageDays > 0)
        notes.push(`saldo osservato il ${l.observedOn}, ${ageDays} giorni prima della data richiesta`);
      if (account?.type === "cash") {
        if (bookedAfter > 0)
          notes.push(`saldo del ${l.observedOn} piu' ${bookedAfter} movimenti segnati dopo`);
      } else {
        if (bookedAfter > 0)
          notes.push(`${bookedAfter} movimenti contabilizzati dopo il saldo: non e' il saldo corrente`);
        if (reconciliation === null) notes.push("saldo non riconciliato con i movimenti");
        else if (reconciliation.status !== "ok")
          notes.push(`riconciliazione con esito ${reconciliation.status}`);
      }
    }
    out.push({
      accountId: l.accountId,
      accountName: account?.name ?? l.accountId,
      closed: isClosedAt(
        statuses.find((x) => x.accountId === l.accountId),
        input.asOf,
      ),
      currency: l.currency,
      status,
      balanceMinor: l.balanceMinor,
      observedOn: l.observedOn,
      ageDays,
      reconciliation,
      bookedMovementsAfter: bookedAfter,
      pendingAfter,
      verifiedAsOf,
      notes,
    });
  }
  const totals = new Map<string, BalanceTotal>();
  for (const b of out) {
    const t = totals.get(b.currency) ?? {
      currency: b.currency,
      observedSumMinor: 0,
      accountsIncluded: 0,
      accountsWithoutBalance: 0,
      complete: true,
      allVerifiedAsOf: true,
    };
    if (b.balanceMinor === null) {
      t.accountsWithoutBalance += 1;
      t.complete = false;
    } else {
      t.observedSumMinor += b.balanceMinor;
      t.accountsIncluded += 1;
    }
    if (!b.verifiedAsOf) t.allVerifiedAsOf = false;
    totals.set(b.currency, t);
  }
  return {
    asOf: input.asOf,
    accounts: out,
    totals: [...totals.values()].sort((a, b) => a.currency.localeCompare(b.currency)),
  };
}

// ---------------------------------------------------------------------------------------------------------------
// movimenti (paginati)

export interface TransactionsInput {
  from: string;
  to: string;
  accountIds?: string[];
  statuses?: ("booked" | "pending")[];
  kinds?: SummaryKind[];
  /** categoria decisa; null = movimenti senza categoria */
  categoryId?: string | null;
  /** includere doppioni confermati e pending sostituite (esclusi dai riepiloghi); predefinito no */
  includeExcluded?: boolean;
  limit?: number;
  cursor?: string;
}

export interface TransactionItem {
  id: string;
  accountId: string;
  /** data effettiva: contabilizzazione per le contabilizzate, operazione per le pending */
  date: string;
  transactionDate: string;
  bookingDate: string | null;
  status: "booked" | "pending";
  amountMinor: number;
  currency: string;
  description: string;
  counterparty: string | null;
  kind: SummaryKind;
  categoryId: string | null;
  classification: "auto" | "confirmed" | "needs_review" | "none";
  excluded: boolean;
  /** perche' ha questo tipo: la regola che l'ha classificato o il credito/debito a cui e' collegato (null = scelta a mano o nessuna) */
  why: string | null;
}

export interface TransactionsOutput {
  items: TransactionItem[];
  /** da passare come `cursor` con GLI STESSI filtri per la pagina successiva; null = fine */
  nextCursor: string | null;
  limit: number;
}

interface TxnRow extends EffectiveColumns {
  id: string;
  account_id: string;
  status: "booked" | "pending";
  transaction_date: string;
  booking_date: string | null;
  amount_minor: number;
  currency: string;
  description: string;
  counterparty: string | null;
  why: string | null;
}

/** Chiave dei filtri inclusa nel cursore: un cursore usato con filtri diversi viene rifiutato. */
function filterKey(input: TransactionsInput): string {
  return JSON.stringify([
    input.from,
    input.to,
    [...(input.accountIds ?? [])].sort(),
    [...(input.statuses ?? [])].sort(),
    [...(input.kinds ?? [])].sort(),
    input.categoryId === undefined ? "*" : input.categoryId,
    input.includeExcluded === true,
  ]);
}

function encodeCursor(date: string, id: string, key: string): string {
  return btoa(JSON.stringify({ v: 1, d: date, i: id, f: key }))
    .replace(/\+/gu, "-")
    .replace(/\//gu, "_")
    .replace(/=+$/u, "");
}

function decodeCursor(cursor: string, key: string): { date: string; id: string } {
  try {
    const json = atob(cursor.replace(/-/gu, "+").replace(/_/gu, "/"));
    const c = JSON.parse(json) as { v?: unknown; d?: unknown; i?: unknown; f?: unknown };
    if (c.v === 1 && typeof c.d === "string" && typeof c.i === "string" && c.f === key)
      return { date: c.d, id: c.i };
  } catch {
    // cursore illeggibile: stesso errore di un cursore estraneo
  }
  throw new ValidationError("cursore non valido o creato con filtri diversi: ripartire senza cursore");
}

export async function queryTransactions(db: Db, input: TransactionsInput): Promise<TransactionsOutput> {
  isoDate(input.from, "data iniziale");
  isoDate(input.to, "data finale");
  if (input.from > input.to) throw new ValidationError("periodo non valido: from > to");
  const limit = input.limit ?? LIMITS.pageDefault;
  if (!Number.isInteger(limit) || limit < 1 || limit > LIMITS.pageMax)
    throw new ValidationError(`limit deve essere un intero tra 1 e ${LIMITS.pageMax}`);
  await accountsById(db, input.accountIds);
  const key = filterKey(input);
  const after = input.cursor === undefined ? null : decodeCursor(input.cursor, key);

  const inner: string[] = [`${EFFECTIVE_DATE_SQL} BETWEEN ? AND ?`];
  const params: (string | number)[] = [input.from, input.to];
  const list = (column: string, values: readonly string[] | undefined) => {
    if (values === undefined || values.length === 0) return;
    inner.push(`${column} IN (${values.map(() => "?").join(", ")})`);
    params.push(...values);
  };
  list("t.account_id", input.accountIds);
  list("t.status", input.statuses);
  const outer: string[] = [];
  if (input.kinds !== undefined && input.kinds.length > 0) {
    outer.push(`q.effective_kind IN (${input.kinds.map(() => "?").join(", ")})`);
    params.push(...input.kinds);
  }
  if (input.categoryId === null) outer.push("q.effective_category_id IS NULL");
  else if (input.categoryId !== undefined) {
    outer.push("q.effective_category_id = ?");
    params.push(input.categoryId);
  }
  if (input.includeExcluded !== true) outer.push("q.excluded = 0");
  if (after !== null) {
    outer.push("(q.effective_date > ? OR (q.effective_date = ? AND q.id > ?))");
    params.push(after.date, after.date, after.id);
  }
  params.push(limit + 1);
  const rows = await db.all<TxnRow>(
    `SELECT * FROM (
       SELECT t.id, t.account_id, t.status, t.transaction_date, t.booking_date, t.amount_minor, t.currency,
              t.description, t.counterparty, ${EFFECTIVE_COLUMNS},
              COALESCE((SELECT o.description FROM obligation_events e JOIN obligations o ON o.id = e.obligation_id
                         WHERE e.transaction_id = t.id LIMIT 1),
                       (SELECT r.description FROM rules r
                         WHERE r.rule_set_version = c.rule_set_version AND r.rule_id = c.rule_id)) AS why
       FROM ${EFFECTIVE_FROM}
       WHERE ${inner.join(" AND ")}
     ) q
     ${outer.length > 0 ? `WHERE ${outer.join(" AND ")}` : ""}
     ORDER BY q.effective_date, q.id
     LIMIT ?`,
    params,
  );
  const page = rows.slice(0, limit);
  const last = page[page.length - 1];
  return {
    items: page.map((r) => ({
      id: r.id,
      accountId: r.account_id,
      date: r.effective_date,
      transactionDate: r.transaction_date,
      bookingDate: r.booking_date,
      status: r.status,
      amountMinor: r.amount_minor,
      currency: r.currency,
      description: redactIdentifiers(r.description),
      counterparty: r.counterparty === null ? null : redactIdentifiers(r.counterparty),
      kind: r.effective_kind,
      categoryId: r.effective_category_id,
      classification: r.classification_status ?? "none",
      excluded: r.excluded === 1,
      why: r.why,
    })),
    nextCursor:
      rows.length > limit && last !== undefined ? encodeCursor(last.effective_date, last.id, key) : null,
    limit,
  };
}

// ---------------------------------------------------------------------------------------------------------------
// crediti e debiti

export interface ObligationsInput {
  asOf: string;
  status?: "open" | "closed" | "all";
  direction?: Direction;
  currency?: string;
  counterpartyId?: string;
}

export interface ObligationItem {
  id: string;
  direction: Direction;
  counterparty: { id: string; name: string };
  currency: string;
  description: string;
  openedOn: string;
  dueOn: string | null;
  /** giorni alla scadenza (negativi = scaduto); null senza scadenza */
  daysToDue: number | null;
  status: "open" | "closed";
  overdue: boolean;
  initialMinor: number;
  increasedMinor: number;
  paidMinor: number;
  forgivenMinor: number;
  adjustedMinor: number;
  residualMinor: number;
  lastEventOn: string | null;
  /** movimenti bancari collegati (verificati dall'utente) */
  linkedTransactions: number;
}

export interface ObligationsOutput {
  asOf: string;
  items: ObligationItem[];
  /** vero se gli elementi oltre `LIMITS.obligationsMax` sono stati omessi (la posizione li include comunque) */
  truncated: boolean;
  /** per valuta, su tutti i crediti/debiti che rispettano direzione, valuta e controparte */
  position: PositionLine[];
}

export async function queryObligations(db: Db, input: ObligationsInput): Promise<ObligationsOutput> {
  isoDate(input.asOf, "data di riferimento");
  const all = (await listObligations(readCtx(db), input.asOf)).filter(
    (o) =>
      (input.direction === undefined || o.direction === input.direction) &&
      (input.currency === undefined || o.currency === input.currency) &&
      (input.counterpartyId === undefined || o.counterparty_id === input.counterpartyId),
  );
  const wanted = input.status ?? "open";
  const selected = all
    .filter((o) => wanted === "all" || o.state.status === wanted)
    .sort(
      (a, b) =>
        Number(b.state.overdue) - Number(a.state.overdue) ||
        (a.due_on ?? "9999-12-31").localeCompare(b.due_on ?? "9999-12-31") ||
        a.opened_on.localeCompare(b.opened_on) ||
        a.id.localeCompare(b.id),
    );
  return {
    asOf: input.asOf,
    items: selected.slice(0, LIMITS.obligationsMax).map((o) => ({
      id: o.id,
      direction: o.direction,
      counterparty: { id: o.counterparty_id, name: o.counterparty_name },
      currency: o.currency,
      description: o.description,
      openedOn: o.opened_on,
      dueOn: o.due_on,
      daysToDue: o.due_on === null ? null : daysBetween(input.asOf, o.due_on),
      status: o.state.status,
      overdue: o.state.overdue,
      initialMinor: o.state.initial,
      increasedMinor: o.state.increased,
      paidMinor: o.state.paid,
      forgivenMinor: o.state.forgiven,
      adjustedMinor: o.state.adjusted,
      residualMinor: o.state.residual,
      lastEventOn: o.state.lastEventOn,
      linkedTransactions: new Set(o.events.map((e) => e.transaction_id).filter((t) => t !== null)).size,
    })),
    truncated: selected.length > LIMITS.obligationsMax,
    position: obligationsPosition(
      all.map((o) => ({ direction: o.direction, currency: o.currency, state: o.state })),
    ),
  };
}

// ---------------------------------------------------------------------------------------------------------------
// stato dei dati e anomalie

export interface DataStatusInput {
  asOf: string;
  staleAfterDays?: number;
}

export type AnomalyCode =
  | "no_imports"
  | "stale_import"
  | "stale_data"
  | "last_import_rejected"
  | "balance_missing"
  | "balance_conflict"
  | "reconciliation_difference";

export interface Anomaly {
  code: AnomalyCode;
  severity: "warning" | "error";
  accountId: string | null;
  message: string;
  detail: Record<string, string | number | null>;
}

export interface AccountDataStatus {
  accountId: string;
  currency: string;
  /** istante (UTC) dell'ultima importazione riuscita */
  lastImportAt: string | null;
  lastImportStatus: string | null;
  lastBookedOn: string | null;
  lastBalanceOn: string | null;
  lastReconciliation: { status: string; from: string; to: string; differenceMinor: number | null } | null;
  pendingCount: number;
}

export interface DataStatusOutput {
  contractVersion: number;
  schemaVersion: number;
  asOf: string;
  accounts: AccountDataStatus[];
  openReviewItems: Record<string, number>;
  anomalies: Anomaly[];
}

export async function queryDataStatus(db: Db, input: DataStatusInput): Promise<DataStatusOutput> {
  isoDate(input.asOf, "data di riferimento");
  const staleAfter = input.staleAfterDays ?? LIMITS.staleAfterDaysDefault;
  if (!Number.isInteger(staleAfter) || staleAfter < 1 || staleAfter > 366)
    throw new ValidationError("staleAfterDays deve essere un intero tra 1 e 366");
  const accounts = await listAccounts(readCtx(db));
  const statuses = await listAccountStatuses(readCtx(db));
  const balances = await liquidity(readCtx(db), input.asOf);
  const anomalies: Anomaly[] = [];
  const out: AccountDataStatus[] = [];
  for (const a of accounts) {
    const lastOk = await db.first<{ at: string | null }>(
      "SELECT MAX(finished_at) AS at FROM import_runs WHERE account_id = ? AND status = 'committed'",
      [a.id],
    );
    const lastRun = await db.first<{ status: string; finished_at: string }>(
      "SELECT status, finished_at FROM import_runs WHERE account_id = ? ORDER BY finished_at DESC, id DESC LIMIT 1",
      [a.id],
    );
    const tx = await db.first<{ booked: string | null; pending: number }>(
      "SELECT MAX(booking_date) AS booked, SUM(status = 'pending') AS pending FROM transactions WHERE account_id = ?",
      [a.id],
    );
    const rec = await db.first<{
      status: string;
      from_date: string;
      to_date: string;
      difference_minor: number | null;
    }>(
      `SELECT status, from_date, to_date, difference_minor FROM reconciliations WHERE account_id = ?
       ORDER BY to_date DESC, computed_at DESC, id DESC LIMIT 1`,
      [a.id],
    );
    const balance = balances.find((b) => b.accountId === a.id);
    const lastImportAt = lastOk?.at ?? null;
    out.push({
      accountId: a.id,
      currency: a.currency,
      lastImportAt,
      lastImportStatus: lastRun?.status ?? null,
      lastBookedOn: tx?.booked ?? null,
      lastBalanceOn: balance?.observedOn ?? null,
      lastReconciliation:
        rec === null
          ? null
          : {
              status: rec.status,
              from: rec.from_date,
              to: rec.to_date,
              differenceMinor: rec.difference_minor,
            },
      pendingCount: tx?.pending ?? 0,
    });
    const warn = (code: AnomalyCode, message: string, detail: Anomaly["detail"] = {}) =>
      anomalies.push({ code, severity: "warning", accountId: a.id, message, detail });
    // conto chiuso alla data: storico conservato, nessun avviso di freschezza o di saldo mancante
    const closed = isClosedAt(
      statuses.find((x) => x.accountId === a.id),
      input.asOf,
    );
    if (closed) {
      if (lastRun?.status === "rejected")
        warn("last_import_rejected", "l'ultima importazione e' stata rifiutata", { at: lastRun.finished_at });
      continue;
    }
    // dati fermi: conta la data dell'ULTIMO MOVIMENTO, non quella dell'importazione (un estratto vecchio importato oggi
    // resta vecchio)
    if (tx?.booked != null) {
      const gap = daysBetween(tx.booked, input.asOf);
      if (gap > staleAfter)
        warn(
          "stale_data",
          `movimenti disponibili solo fino al ${tx.booked} (${gap} giorni prima della data richiesta): estratti successivi mancanti o conto inattivo`,
          { lastMovementOn: tx.booked, days: gap },
        );
    }
    if (lastImportAt === null) warn("no_imports", "nessuna importazione riuscita per questo conto");
    else {
      const age = daysBetween(lastImportAt.slice(0, 10), input.asOf);
      if (age > staleAfter)
        warn("stale_import", `ultima importazione ${age} giorni fa (soglia ${staleAfter})`, {
          lastImportAt,
          days: age,
        });
    }
    if (lastRun?.status === "rejected")
      warn("last_import_rejected", "l'ultima importazione e' stata rifiutata", { at: lastRun.finished_at });
    if (balance?.certainty === "missing")
      warn("balance_missing", "nessun saldo osservato: saldo non disponibile");
    if (balance?.certainty === "conflict")
      anomalies.push({
        code: "balance_conflict",
        severity: "error",
        accountId: a.id,
        message: "saldi osservati diversi per la stessa data",
        detail: { date: balance.observedOn },
      });
  }
  const reviews = await db.all<{ kind: string; entity_id: string; detail_json: string }>(
    "SELECT kind, entity_id, detail_json FROM review_items WHERE status = 'open' ORDER BY created_at, id",
  );
  const openReviewItems: Record<string, number> = {};
  for (const r of reviews) {
    openReviewItems[r.kind] = (openReviewItems[r.kind] ?? 0) + 1;
    if (r.kind !== "reconciliation_difference") continue;
    const detail = JSON.parse(r.detail_json) as { esito?: string; differenza?: number | null };
    const [accountId, period] = r.entity_id.split("@");
    anomalies.push({
      code: "reconciliation_difference",
      severity: "error",
      accountId: accountId ?? null,
      message: `riconciliazione non quadrata (${detail.esito ?? "?"}) nel periodo ${period ?? "?"}`,
      detail: {
        period: period ?? null,
        outcome: detail.esito ?? null,
        differenceMinor: detail.differenza ?? null,
      },
    });
  }
  const schema = await db.first<{ v: number | null }>("SELECT MAX(version) AS v FROM schema_migrations");
  return {
    contractVersion: CONSULTATION_CONTRACT_VERSION,
    schemaVersion: schema?.v ?? 0,
    asOf: input.asOf,
    accounts: out,
    openReviewItems,
    anomalies,
  };
}

// ---------------------------------------------------------------------------------------------------------------
// panoramica: stipendio, altre entrate, sostegno familiare, spese reali, trasferimenti, risparmi, denaro rimasto

/**
 * Categorie con significato FISSO nella panoramica: le regole di classificazione devono usare questi id.
 * Tutto il resto delle entrate classificate e' "altre entrate"; cio' che non e' classificato resta "da classificare".
 */
export const OVERVIEW_CATEGORIES = {
  salary: "stipendio",
  familySupport: "sostegno-familiare",
  interest: "interessi",
} as const;
/** Contano anche come sostegno della famiglia (paghetta, soldi dai genitori). */
const FAMILY_ALSO = ["paghetta"] as const;

export interface OverviewInput {
  from: string;
  to: string;
  /** data dei saldi; predefinita = `to` */
  asOf?: string;
}

export interface OverviewCurrency {
  currency: string;
  income: { salary: number; familySupport: number; interest: number; other: number; total: number };
  /** spesa reale: acquisti + commissioni - rimborsi (trasferimenti, crediti/debiti e da classificare esclusi) */
  spending: { purchases: number; fees: number; refunds: number; net: number };
  /** ogni trasferimento confermato tra conti propri contato UNA volta (gamba in uscita); mai entrata o spesa */
  internalTransfers: { moved: number; intoSavingsNet: number };
  obligations: { in: number; out: number };
  unclassified: { inflow: number; outflow: number; count: number };
  pending: { count: number; sum: number };
}

export interface OverviewOutput {
  from: string;
  to: string;
  asOf: string;
  /** data dell'ultimo movimento disponibile per conto: dopo questa data possono mancare movimenti */
  lastUpdate: {
    lastMovementOn: string | null;
    byAccount: {
      accountId: string;
      institutionId: string | null;
      closed: boolean;
      lastMovementOn: string | null;
    }[];
  };
  currencies: OverviewCurrency[];
  /** denaro rimasto: SOLO saldi osservati dei conti attivi alla data (mai ricostruiti dai movimenti) */
  money: {
    byAccount: Pick<
      BalanceLine,
      "accountId" | "currency" | "status" | "balanceMinor" | "observedOn" | "verifiedAsOf" | "closed"
    >[];
    byInstitution: {
      institutionId: string | null;
      currency: string;
      observedSumMinor: number;
      complete: boolean;
    }[];
    totals: BalanceTotal[];
  };
  /** accantonato: saldi osservati dei conti di risparmio attivi */
  savings: {
    accountIds: string[];
    byCurrency: { currency: string; observedSumMinor: number; complete: boolean }[];
  };
  openReviewItems: Record<string, number>;
  notes: string[];
}

export async function queryOverview(db: Db, input: OverviewInput): Promise<OverviewOutput> {
  isoDate(input.from, "data iniziale");
  isoDate(input.to, "data finale");
  if (input.from > input.to) throw new ValidationError("periodo non valido: from > to");
  const asOf = input.asOf ?? input.to;
  isoDate(asOf, "data di riferimento");
  const ctx = readCtx(db);
  const accounts = await listAccounts(ctx);
  const statuses = await listAccountStatuses(ctx);
  const active = accounts.filter(
    (a) =>
      !isClosedAt(
        statuses.find((x) => x.accountId === a.id),
        asOf,
      ),
  );
  const savingsIds = accounts.filter((a) => a.type === "savings").map((a) => a.id);
  const report = await periodReport(ctx, input.from, input.to);
  const savingsReport =
    savingsIds.length > 0 ? await periodReport(ctx, input.from, input.to, savingsIds) : null;
  const balances = await queryBalances(db, { asOf });
  const last = await db.all<{ account_id: string; d: string | null }>(
    `SELECT a.id AS account_id, MAX(COALESCE(t.booking_date, t.transaction_date)) AS d
     FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id AND COALESCE(t.booking_date, t.transaction_date) <= ?
     GROUP BY a.id ORDER BY a.id`,
    [asOf],
  );
  const reviews = await db.all<{ kind: string; n: number }>(
    "SELECT kind, COUNT(*) AS n FROM review_items WHERE status = 'open' GROUP BY kind ORDER BY kind",
  );

  const currencies: OverviewCurrency[] = report.currencies.map((c) => {
    const cat = (id: string) => c.incomeByCategory[id] ?? 0;
    const salary = cat(OVERVIEW_CATEGORIES.salary);
    const familySupport = FAMILY_ALSO.reduce(
      (a, id) => addMinor(a, cat(id)),
      cat(OVERVIEW_CATEGORIES.familySupport),
    );
    const interest = cat(OVERVIEW_CATEGORIES.interest);
    const s = savingsReport?.currencies.find((x) => x.currency === c.currency);
    return {
      currency: c.currency,
      income: {
        salary,
        familySupport,
        interest,
        other: c.income - salary - familySupport - interest,
        total: c.income,
      },
      spending: { purchases: c.expenses, fees: c.fees, refunds: c.refunds, net: c.netSpending },
      internalTransfers: { moved: c.transfersOut, intoSavingsNet: s ? s.transfersIn - s.transfersOut : 0 },
      obligations: { in: c.obligationsIn, out: c.obligationsOut },
      unclassified: { inflow: c.unclassifiedIn, outflow: c.unclassifiedOut, count: c.unclassifiedCount },
      pending: { count: c.pendingCount, sum: c.pendingSum },
    };
  });

  const activeIds = new Set(active.map((a) => a.id));
  const activeLines = balances.accounts.filter((b) => activeIds.has(b.accountId));
  const group = (lines: BalanceLine[], key: (b: BalanceLine) => string) => {
    const m = new Map<string, { observedSumMinor: number; complete: boolean; currency: string }>();
    for (const b of lines) {
      const k = `${key(b)}|${b.currency}`;
      const g = m.get(k) ?? { observedSumMinor: 0, complete: true, currency: b.currency };
      if (b.balanceMinor === null) g.complete = false;
      else g.observedSumMinor += b.balanceMinor;
      m.set(k, g);
    }
    return [...m.entries()].map(([k, v]) => ({ key: k.split("|")[0] ?? "", ...v }));
  };
  const institutionOf = (id: string) => accounts.find((a) => a.id === id)?.institution_id ?? null;
  const byInstitution = group(activeLines, (b) => institutionOf(b.accountId) ?? "")
    .map((g) => ({
      institutionId: g.key === "" ? null : g.key,
      currency: g.currency,
      observedSumMinor: g.observedSumMinor,
      complete: g.complete,
    }))
    .sort(
      (a, b) =>
        (a.institutionId ?? "").localeCompare(b.institutionId ?? "") || a.currency.localeCompare(b.currency),
    );
  const savingsLines = activeLines.filter((b) => savingsIds.includes(b.accountId));
  const savingsByCurrency = group(savingsLines, () => "").map(({ currency, observedSumMinor, complete }) => ({
    currency,
    observedSumMinor,
    complete,
  }));
  const totals: BalanceTotal[] = group(activeLines, () => "").map(
    ({ currency, observedSumMinor, complete }) => {
      const lines = activeLines.filter((b) => b.currency === currency);
      return {
        currency,
        observedSumMinor,
        accountsIncluded: lines.filter((b) => b.balanceMinor !== null).length,
        accountsWithoutBalance: lines.filter((b) => b.balanceMinor === null).length,
        complete,
        allVerifiedAsOf: lines.every((b) => b.verifiedAsOf),
      };
    },
  );

  const lastMovementOn = last.reduce<string | null>(
    (m, r) => (r.d !== null && (m === null || r.d > m) ? r.d : m),
    null,
  );
  const notes: string[] = [];
  if (lastMovementOn !== null)
    notes.push(
      `dati aggiornati al ${lastMovementOn}: possono mancare movimenti successivi o non ancora esportati`,
    );
  for (const r of last) {
    if (!activeIds.has(r.account_id)) continue;
    if (r.d === null) notes.push(`${r.account_id}: nessun movimento disponibile`);
    else if (lastMovementOn !== null && r.d < lastMovementOn)
      notes.push(`${r.account_id}: movimenti disponibili solo fino al ${r.d}`);
  }
  for (const c of currencies) {
    if (c.unclassified.count > 0)
      notes.push(
        `${c.currency}: ${c.unclassified.count} movimenti da classificare (entrate ${c.unclassified.inflow}, uscite ${c.unclassified.outflow}): i totali per tipo sono incompleti`,
      );
  }
  const openReviewItems = Object.fromEntries(reviews.map((r) => [r.kind, r.n]));
  if ((openReviewItems.transfer ?? 0) + (openReviewItems.transfer_ambiguous ?? 0) > 0)
    notes.push(
      "trasferimenti proposti o ambigui non confermati: restano esclusi dai trasferimenti e contano come da classificare",
    );
  if (totals.some((t) => !t.complete))
    notes.push("denaro totale incompleto: almeno un conto attivo non ha un saldo osservato utilizzabile");

  return {
    from: input.from,
    to: input.to,
    asOf,
    lastUpdate: {
      lastMovementOn,
      byAccount: last.map((r) => ({
        accountId: r.account_id,
        institutionId: institutionOf(r.account_id),
        closed: !activeIds.has(r.account_id),
        lastMovementOn: r.d,
      })),
    },
    currencies,
    money: {
      byAccount: balances.accounts.map(
        ({ accountId, currency, status, balanceMinor, observedOn, verifiedAsOf, closed }) => ({
          accountId,
          currency,
          status,
          balanceMinor,
          observedOn,
          verifiedAsOf,
          closed,
        }),
      ),
      byInstitution,
      totals,
    },
    savings: { accountIds: savingsIds.filter((id) => activeIds.has(id)), byCurrency: savingsByCurrency },
    openReviewItems,
    notes,
  };
}

/** Categoria con il suo gruppo di budget (50/30/20); `budgetGroup: null` = categoria senza gruppo («unassigned»). */
export interface CategoryItem {
  id: string;
  name: string;
  parentId: string | null;
  budgetGroup: BudgetGroup | null;
}

/** Categorie registrate (immutabili): stessi gruppi usati da `summary.spendingByBudgetGroup`. */
export async function queryCategories(db: Db): Promise<{ items: CategoryItem[] }> {
  const rows = await readOnlyDb(db).all<{
    id: string;
    name: string;
    parent_id: string | null;
    budget_group: BudgetGroup | null;
  }>("SELECT id, name, parent_id, budget_group FROM categories ORDER BY name, id");
  return {
    items: rows.map((r) => ({ id: r.id, name: r.name, parentId: r.parent_id, budgetGroup: r.budget_group })),
  };
}

/**
 * Obiettivo: `status` e' lo stato DICHIARATO dal titolare (active, paused, reached, cancelled); `reached` e' CALCOLATO
 * dagli accantonamenti (allocato >= obiettivo). Possono non coincidere: nessuno dei due viene corretto d'ufficio.
 */
export interface GoalItem {
  id: string;
  name: string;
  currency: string;
  targetMinor: number;
  deadline: string | null;
  status: "active" | "paused" | "reached" | "cancelled";
  allocatedMinor: number;
  remainingMinor: number;
  progressBp: number;
  reached: boolean;
}

/** Acquisto pianificato: `estimatedMinor: null` = stima non registrata (mai zero). */
export interface PurchaseItem {
  id: string;
  title: string;
  currency: string;
  estimatedMinor: number | null;
  priority: number | null;
  status: "idea" | "planned" | "purchased" | "cancelled";
  goalId: string | null;
  linkedToTransaction: boolean;
}

/** Pianificazione dai servizi esistenti, in sola lettura; niente id di blocchi Notion o di movimenti. */
export async function queryPlanning(db: Db): Promise<{ goals: GoalItem[]; purchases: PurchaseItem[] }> {
  const ctx = readCtx(db);
  const goals = await listGoalsWithProgress(ctx);
  const purchases = await listPlannedPurchases(ctx);
  return {
    goals: goals.map((g) => ({
      id: g.id,
      name: g.name,
      currency: g.currency,
      targetMinor: g.target_minor,
      deadline: g.deadline,
      status: g.status,
      allocatedMinor: g.progress.allocated,
      remainingMinor: g.progress.remaining,
      progressBp: g.progress.progressBp,
      reached: g.progress.reached,
    })),
    purchases: purchases.map((p) => ({
      id: p.id,
      title: p.title,
      currency: p.currency,
      estimatedMinor: p.estimated_minor,
      priority: p.priority,
      status: p.status,
      goalId: p.goal_id,
      linkedToTransaction: p.transaction_id !== null,
    })),
  };
}

// ---------------------------------------------------------------------------------------------------------------
// movimenti inseriti a mano e budget (v7, aggiunte additive alla v1)

/** Movimento inserito a mano: `voided` = annullato (resta nello storico, fuori da elenchi e riepiloghi). */
export interface ManualItem {
  id: string;
  accountId: string;
  transactionDate: string;
  bookingDate: string | null;
  status: "booked" | "pending";
  amountMinor: number;
  currency: string;
  description: string;
  voided: boolean;
  voidedAt: string | null;
}

export async function queryManual(
  db: Db,
  input: { from: string; to: string },
): Promise<{ items: ManualItem[]; truncated: boolean }> {
  isoDate(input.from, "data iniziale");
  isoDate(input.to, "data finale");
  if (input.from > input.to) throw new ValidationError("periodo non valido: from > to");
  const rows = await readOnlyDb(db).all<{
    id: string;
    account_id: string;
    transaction_date: string;
    booking_date: string | null;
    status: "booked" | "pending";
    amount_minor: number;
    currency: string;
    description: string;
    voided_at: string | null;
  }>(
    `SELECT t.id, t.account_id, t.transaction_date, t.booking_date, t.status, t.amount_minor, t.currency,
            t.description, v.created_at AS voided_at
     FROM transactions t LEFT JOIN manual_voids v ON v.transaction_id = t.id
     WHERE t.origin = 'manual' AND t.transaction_date BETWEEN ? AND ?
     ORDER BY t.transaction_date DESC, t.id LIMIT ?`,
    [input.from, input.to, LIMITS.pageMax + 1],
  );
  return {
    items: rows.slice(0, LIMITS.pageMax).map((r) => ({
      id: r.id,
      accountId: r.account_id,
      transactionDate: r.transaction_date,
      bookingDate: r.booking_date,
      status: r.status,
      amountMinor: r.amount_minor,
      currency: r.currency,
      description: redactIdentifiers(r.description),
      voided: r.voided_at !== null,
      voidedAt: r.voided_at,
    })),
    truncated: rows.length > LIMITS.pageMax,
  };
}

/** Budget del mese per valuta (servizio `budgetStatus`, stesse regole dei riepiloghi); senza valuta, quelle con budget. */
export async function queryBudgets(
  db: Db,
  input: { month: string; currency?: string },
): Promise<{ month: string; currencies: BudgetStatus[] }> {
  const ctx = readCtx(db);
  const list =
    input.currency !== undefined
      ? [input.currency]
      : (
          await ctx.db.all<{ currency: string }>("SELECT DISTINCT currency FROM budgets ORDER BY currency")
        ).map((r) => r.currency);
  const currencies: BudgetStatus[] = [];
  for (const c of list) currencies.push(await budgetStatus(ctx, input.month, c));
  return { month: input.month, currencies };
}
