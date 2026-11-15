/**
 * MOVIMENTI INSERITI A MANO (richiede la migrazione v7, in attesa: src/db/migrations/007_manual_and_budgets.ts).
 *
 * Regole:
 *   - si inseriscono solo su conti esistenti e aperti alla data, nella valuta del conto, con descrizione non vuota;
 *   - `requestId` (scelto dal client) rende l'inserimento idempotente: la stessa richiesta ripetuta restituisce lo
 *     stesso movimento, con contenuto diverso viene rifiutata;
 *   - movimenti esistenti con lo stesso importo a +-3 giorni sono possibili doppioni: senza `confirmDuplicate` non si
 *     scrive nulla e si restituiscono i candidati;
 *   - si correggono e si annullano SOLO i movimenti manuali: quelli importati restano come la fonte li ha dati (si
 *     interviene con classificazione e collegamenti). Nessuna cancellazione: l'annullamento e' una riga con motivo;
 *   - ogni scrittura porta la sua riga di audit (prima/dopo, motivo) nello stesso batch: lo storico e' `audit_log`.
 */
import type { Ctx } from "../context.ts";
import { NotFoundError, ValidationError } from "../context.ts";
import { stmt } from "../db/types.ts";
import { commitGuarded, readWriteSeq } from "../db/write.ts";
import { canonicalJson, sha256Hex } from "../domain/hash.ts";
import { assertMinor } from "../domain/money.ts";
import { addDays, isIsoDate, normalizeText } from "../domain/text.ts";
import { getAccount, isClosedAt, listAccountStatuses } from "./accounts.ts";
import { auditStatement } from "./audit.ts";

export const DUPLICATE_WINDOW_DAYS = 3;

export interface NewManualTransaction {
  requestId: string;
  accountId: string;
  transactionDate: string;
  amountMinor: number;
  currency: string;
  description: string;
  counterparty?: string | null;
  /** `booked` (predefinito: contanti, spesa gia' contabilizzata) o `pending` (in attesa della banca) */
  status?: "booked" | "pending";
  bookingDate?: string | null;
  note?: string | null;
  confirmDuplicate?: boolean;
}

export interface ManualCorrection {
  transactionDate?: string;
  amountMinor?: number;
  description?: string;
  counterparty?: string | null;
  status?: "booked";
  bookingDate?: string;
}

export interface DuplicateCandidate {
  id: string;
  origin: string;
  transactionDate: string;
  amountMinor: number;
  description: string;
}

export type AddManualResult =
  | { status: "created"; id: string }
  | { status: "already_recorded"; id: string }
  | { status: "possible_duplicate"; candidates: DuplicateCandidate[] };

interface TxnRow {
  id: string;
  account_id: string;
  origin: string;
  source_txn_id: string | null;
  status: "booked" | "pending";
  transaction_date: string;
  booking_date: string | null;
  amount_minor: number;
  currency: string;
  description: string;
  counterparty: string | null;
}

const TXN_COLUMNS = `id, account_id, origin, source_txn_id, status, transaction_date, booking_date, amount_minor,
  currency, description, counterparty`;

/** Stessa impronta dell'importazione (importer.ts), cosi' i rilevatori trattano i movimenti allo stesso modo. */
async function fingerprintOf(
  accountId: string,
  date: string,
  amountMinor: number,
  currency: string,
  description: string,
  counterparty: string | null,
): Promise<string> {
  return sha256Hex(
    canonicalJson([
      accountId,
      date,
      amountMinor,
      currency,
      normalizeText(description),
      counterparty === null ? null : normalizeText(counterparty),
      null,
      null,
    ]),
  );
}

function requireText(value: unknown, field: string): string {
  if (typeof value !== "string" || value.trim() === "") throw new ValidationError(`${field} obbligatorio`);
  return value.trim();
}

function snapshot(r: TxnRow): Record<string, unknown> {
  return {
    account: r.account_id,
    status: r.status,
    transaction_date: r.transaction_date,
    booking_date: r.booking_date,
    amount_minor: r.amount_minor,
    currency: r.currency,
    description: r.description,
    counterparty: r.counterparty,
  };
}

async function assertOpen(ctx: Ctx, accountId: string, date: string): Promise<void> {
  const status = (await listAccountStatuses(ctx)).find((s) => s.accountId === accountId);
  if (isClosedAt(status, date)) throw new ValidationError(`conto ${accountId} chiuso alla data ${date}`);
}

async function isVoided(ctx: Ctx, id: string): Promise<boolean> {
  return (await ctx.db.first("SELECT 1 AS x FROM manual_voids WHERE transaction_id = ?", [id])) !== null;
}

export async function findDuplicateCandidates(
  ctx: Ctx,
  accountId: string,
  date: string,
  amountMinor: number,
  excludeId: string | null = null,
): Promise<DuplicateCandidate[]> {
  const rows = await ctx.db.all<TxnRow>(
    `SELECT ${TXN_COLUMNS} FROM transactions
     WHERE account_id = ? AND amount_minor = ? AND transaction_date BETWEEN ? AND ?
       AND id NOT IN (SELECT transaction_id FROM manual_voids) AND (? IS NULL OR id <> ?)
     ORDER BY transaction_date, id`,
    [
      accountId,
      amountMinor,
      addDays(date, -DUPLICATE_WINDOW_DAYS),
      addDays(date, DUPLICATE_WINDOW_DAYS),
      excludeId,
      excludeId,
    ],
  );
  return rows.map((r) => ({
    id: r.id,
    origin: r.origin,
    transactionDate: r.transaction_date,
    amountMinor: r.amount_minor,
    description: r.description,
  }));
}

export async function addManualTransaction(ctx: Ctx, input: NewManualTransaction): Promise<AddManualResult> {
  const requestId = requireText(input.requestId, "requestId");
  if (requestId.length > 100) throw new ValidationError("requestId troppo lungo");
  const account = await getAccount(ctx, input.accountId);
  if (!isIsoDate(input.transactionDate))
    throw new ValidationError(`data non valida: ${input.transactionDate}`);
  assertMinor(input.amountMinor);
  if (input.amountMinor === 0) throw new ValidationError("importo zero");
  if (input.currency !== account.currency) {
    throw new ValidationError(`valuta ${input.currency} diversa da quella del conto (${account.currency})`);
  }
  const description = requireText(input.description, "descrizione");
  const counterparty =
    input.counterparty == null || input.counterparty.trim() === "" ? null : input.counterparty.trim();
  const status = input.status ?? "booked";
  if (status !== "booked" && status !== "pending") throw new ValidationError(`stato non valido: ${status}`);
  const bookingDate = status === "booked" ? (input.bookingDate ?? input.transactionDate) : null;
  if (bookingDate !== null && !isIsoDate(bookingDate))
    throw new ValidationError(`data non valida: ${bookingDate}`);
  await assertOpen(ctx, account.id, input.transactionDate);

  const sourceTxnId = `manual:${requestId}`;
  const existing = await ctx.db.first<TxnRow>(
    `SELECT ${TXN_COLUMNS} FROM transactions WHERE account_id = ? AND source_txn_id = ?`,
    [account.id, sourceTxnId],
  );
  if (existing !== null) {
    const same =
      existing.transaction_date === input.transactionDate &&
      existing.amount_minor === input.amountMinor &&
      existing.description === description;
    if (!same) throw new ValidationError(`richiesta ${requestId} gia' usata con un contenuto diverso`);
    return { status: "already_recorded", id: existing.id };
  }
  if (input.confirmDuplicate !== true) {
    const candidates = await findDuplicateCandidates(
      ctx,
      account.id,
      input.transactionDate,
      input.amountMinor,
    );
    if (candidates.length > 0) return { status: "possible_duplicate", candidates };
  }

  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const id = ctx.newId();
  const fingerprint = await fingerprintOf(
    account.id,
    input.transactionDate,
    input.amountMinor,
    input.currency,
    description,
    counterparty,
  );
  const row: TxnRow = {
    id,
    account_id: account.id,
    origin: "manual",
    source_txn_id: sourceTxnId,
    status,
    transaction_date: input.transactionDate,
    booking_date: bookingDate,
    amount_minor: input.amountMinor,
    currency: input.currency,
    description,
    counterparty,
  };
  await commitGuarded(ctx.db, seq, [
    stmt(
      `INSERT INTO transactions (id, account_id, origin, source_txn_id, fingerprint, occurrence, status,
         transaction_date, booking_date, amount_minor, currency, original_amount_minor, original_currency,
         description, counterparty, description_norm, counterparty_norm, first_import_run_id, last_import_run_id,
         created_at, updated_at, source_type)
       VALUES (?, ?, 'manual', ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL)`,
      id,
      account.id,
      sourceTxnId,
      fingerprint,
      status,
      input.transactionDate,
      bookingDate,
      input.amountMinor,
      input.currency,
      description,
      counterparty,
      normalizeText(description),
      counterparty === null ? null : normalizeText(counterparty),
      at,
      at,
    ),
    auditStatement({
      at,
      actor: "user",
      action: "add_manual_transaction",
      entity: "transaction",
      entityId: id,
      after: { ...snapshot(row), request: requestId, duplicate_confirmed: input.confirmDuplicate === true },
      reason: input.note ?? null,
    }),
  ]);
  return { status: "created", id };
}

async function loadManual(ctx: Ctx, id: string): Promise<TxnRow> {
  const row = await ctx.db.first<TxnRow>(`SELECT ${TXN_COLUMNS} FROM transactions WHERE id = ?`, [id]);
  if (row === null) throw new NotFoundError("movimento", id);
  if (row.origin !== "manual") {
    throw new ValidationError(
      "si correggono solo i movimenti inseriti a mano: quelli importati restano come la fonte",
    );
  }
  if (await isVoided(ctx, id)) throw new ValidationError(`movimento ${id} annullato`);
  return row;
}

export async function correctManualTransaction(
  ctx: Ctx,
  id: string,
  changes: ManualCorrection,
  reason: string,
): Promise<{ id: string; changed: boolean }> {
  const why = requireText(reason, "motivo");
  const before = await loadManual(ctx, id);
  const after: TxnRow = { ...before };
  if (changes.transactionDate !== undefined) {
    if (!isIsoDate(changes.transactionDate))
      throw new ValidationError(`data non valida: ${changes.transactionDate}`);
    after.transaction_date = changes.transactionDate;
    if (before.status === "booked" && changes.bookingDate === undefined)
      after.booking_date = changes.transactionDate;
  }
  if (changes.amountMinor !== undefined) {
    assertMinor(changes.amountMinor);
    if (changes.amountMinor === 0) throw new ValidationError("importo zero");
    after.amount_minor = changes.amountMinor;
  }
  if (changes.description !== undefined) after.description = requireText(changes.description, "descrizione");
  if (changes.counterparty !== undefined) {
    after.counterparty =
      changes.counterparty === null || changes.counterparty.trim() === ""
        ? null
        : changes.counterparty.trim();
  }
  if (changes.status !== undefined) {
    if (changes.status !== "booked")
      throw new ValidationError("un movimento contabilizzato non torna in attesa");
    after.status = "booked";
    after.booking_date = changes.bookingDate ?? after.booking_date ?? after.transaction_date;
  } else if (changes.bookingDate !== undefined) {
    if (after.status !== "booked")
      throw new ValidationError("data di contabilizzazione solo per i contabilizzati");
    after.booking_date = changes.bookingDate;
  }
  if (after.booking_date !== null && !isIsoDate(after.booking_date)) {
    throw new ValidationError(`data non valida: ${after.booking_date}`);
  }
  const changed = JSON.stringify(snapshot(before)) !== JSON.stringify(snapshot(after));
  if (!changed) return { id, changed: false };
  await assertOpen(ctx, after.account_id, after.transaction_date);

  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const fingerprint = await fingerprintOf(
    after.account_id,
    after.transaction_date,
    after.amount_minor,
    after.currency,
    after.description,
    after.counterparty,
  );
  await commitGuarded(ctx.db, seq, [
    stmt(
      `UPDATE transactions SET status = ?, transaction_date = ?, booking_date = ?, amount_minor = ?, description = ?,
         counterparty = ?, description_norm = ?, counterparty_norm = ?, fingerprint = ?, updated_at = ?
       WHERE id = ?`,
      after.status,
      after.transaction_date,
      after.booking_date,
      after.amount_minor,
      after.description,
      after.counterparty,
      normalizeText(after.description),
      after.counterparty === null ? null : normalizeText(after.counterparty),
      fingerprint,
      at,
      id,
    ),
    auditStatement({
      at,
      actor: "user",
      action: "correct_manual_transaction",
      entity: "transaction",
      entityId: id,
      before: snapshot(before),
      after: snapshot(after),
      reason: why,
    }),
  ]);
  return { id, changed: true };
}

export async function voidManualTransaction(ctx: Ctx, id: string, reason: string): Promise<{ id: string }> {
  const why = requireText(reason, "motivo");
  const before = await loadManual(ctx, id);
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  await commitGuarded(ctx.db, seq, [
    stmt("INSERT INTO manual_voids (transaction_id, reason, created_at) VALUES (?, ?, ?)", id, why, at),
    auditStatement({
      at,
      actor: "user",
      action: "void_manual_transaction",
      entity: "transaction",
      entityId: id,
      before: snapshot(before),
      reason: why,
    }),
  ]);
  return { id };
}

export interface ManualHistoryEntry {
  at: string;
  action: string;
  before: unknown;
  after: unknown;
  reason: string | null;
}

/** Storico di un movimento manuale, dal registro di audit (dal piu' vecchio). */
export async function manualHistory(ctx: Ctx, id: string): Promise<ManualHistoryEntry[]> {
  const rows = await ctx.db.all<{
    at: string;
    action: string;
    before_json: string | null;
    after_json: string | null;
    reason: string | null;
  }>(
    "SELECT at, action, before_json, after_json, reason FROM audit_log WHERE entity = 'transaction' AND entity_id = ? ORDER BY at, rowid",
    [id],
  );
  return rows.map((r) => ({
    at: r.at,
    action: r.action,
    before: r.before_json === null ? null : JSON.parse(r.before_json),
    after: r.after_json === null ? null : JSON.parse(r.after_json),
    reason: r.reason,
  }));
}
