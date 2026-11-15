/**
 * Riconciliazione dei saldi: confronta saldi osservati e movimenti contabilizzati e registra l'esito (append-only).
 * Una differenza o un conflitto apre un elemento da verificare; un esito ok chiude quello eventualmente aperto.
 */
import type { Ctx } from "../context.ts";
import { ValidationError } from "../context.ts";
import type { Statement } from "../db/types.ts";
import { stmt } from "../db/types.ts";
import { commitGuarded, readWriteSeq } from "../db/write.ts";
import { type ReconcileResult, reconcile } from "../domain/reconcile.ts";
import { isIsoDate } from "../domain/text.ts";
import { getAccount } from "./accounts.ts";
import { reviewStatement } from "./audit.ts";
import { closeReviewStatement } from "./classification.ts";
import { excludedTransactionIds } from "./links.ts";

export interface Reconciliation extends ReconcileResult {
  id: string;
  accountId: string;
  fromDate: string;
  toDate: string;
  pendingInPeriod: number;
}

export interface ReconcileOptions {
  /**
   * false: se l'ultima riconciliazione registrata per lo stesso periodo ha lo stesso esito e gli stessi importi, non ne
   * registra un'altra (aggiornamenti incrementali senza duplicare lo storico). Predefinito: true (registra sempre).
   */
  recordIfUnchanged?: boolean;
}

export async function reconcileAccount(
  ctx: Ctx,
  accountId: string,
  fromDate: string,
  toDate: string,
  options: ReconcileOptions = {},
): Promise<Reconciliation & { recorded: boolean }> {
  await getAccount(ctx, accountId);
  if (!isIsoDate(fromDate) || !isIsoDate(toDate) || fromDate >= toDate) {
    throw new ValidationError("periodo non valido: servono due date ISO con from < to");
  }
  const seq = await readWriteSeq(ctx.db);
  const observations = async (date: string) =>
    (
      await ctx.db.all<{ balance_minor: number }>(
        "SELECT balance_minor FROM balance_observations WHERE account_id = ? AND as_of_date = ? AND kind = 'end_of_day'",
        [accountId, date],
      )
    ).map((r) => r.balance_minor);
  const excluded = await excludedTransactionIds(ctx);
  const movements = (
    await ctx.db.all<{ id: string; amount_minor: number }>(
      `SELECT id, amount_minor FROM transactions
       WHERE account_id = ? AND status = 'booked' AND booking_date > ? AND booking_date <= ?`,
      [accountId, fromDate, toDate],
    )
  ).filter((m) => !excluded.has(m.id));
  const pending = await ctx.db.first<{ n: number }>(
    `SELECT COUNT(*) AS n FROM transactions WHERE account_id = ? AND status = 'pending' AND transaction_date > ? AND transaction_date <= ?`,
    [accountId, fromDate, toDate],
  );
  const result = reconcile({
    openingObservations: await observations(fromDate),
    closingObservations: await observations(toDate),
    movements: movements.map((m) => m.amount_minor),
  });
  if (options.recordIfUnchanged === false) {
    const last = await ctx.db.first<{
      id: string;
      status: string;
      opening_minor: number | null;
      closing_minor: number | null;
      transactions_sum_minor: number;
      difference_minor: number | null;
    }>(
      `SELECT id, status, opening_minor, closing_minor, transactions_sum_minor, difference_minor FROM reconciliations
       WHERE account_id = ? AND from_date = ? AND to_date = ? ORDER BY computed_at DESC, id DESC LIMIT 1`,
      [accountId, fromDate, toDate],
    );
    if (
      last !== null &&
      last.status === result.status &&
      last.opening_minor === result.opening &&
      last.closing_minor === result.closing &&
      last.transactions_sum_minor === result.movementsSum &&
      last.difference_minor === result.difference
    ) {
      return {
        id: last.id,
        accountId,
        fromDate,
        toDate,
        pendingInPeriod: pending?.n ?? 0,
        ...result,
        recorded: false,
      };
    }
  }
  const id = ctx.newId();
  const at = ctx.now();
  const entityId = `${accountId}@${fromDate}..${toDate}`;
  const statements: Statement[] = [
    stmt(
      `INSERT INTO reconciliations (id, account_id, from_date, to_date, status, opening_minor, closing_minor,
         transactions_sum_minor, difference_minor, detail_json, computed_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      id,
      accountId,
      fromDate,
      toDate,
      result.status,
      result.opening,
      result.closing,
      result.movementsSum,
      result.difference,
      JSON.stringify({ movimenti: movements.length, pending_nel_periodo: pending?.n ?? 0 }),
      at,
    ),
  ];
  if (result.status === "difference" || result.status === "conflict") {
    statements.push(
      reviewStatement({
        id: ctx.newId(),
        kind: "reconciliation_difference",
        entity: "account_period",
        entityId,
        detail: { esito: result.status, differenza: result.difference, riconciliazione: id },
        createdAt: at,
      }),
    );
  } else if (result.status === "ok") {
    statements.push(
      closeReviewStatement(
        "reconciliation_difference",
        "account_period",
        entityId,
        at,
        `riconciliazione ${id} ok`,
      ),
    );
  }
  await commitGuarded(ctx.db, seq, statements);
  return { id, accountId, fromDate, toDate, pendingInPeriod: pending?.n ?? 0, ...result, recorded: true };
}

/** Riconcilia ogni coppia di date consecutive con saldo osservato. */
export async function reconcileObservedPeriods(
  ctx: Ctx,
  accountId: string,
  options: ReconcileOptions = {},
): Promise<(Reconciliation & { recorded: boolean })[]> {
  const dates = (
    await ctx.db.all<{ d: string }>(
      "SELECT DISTINCT as_of_date AS d FROM balance_observations WHERE account_id = ? AND kind = 'end_of_day' ORDER BY d",
      [accountId],
    )
  ).map((r) => r.d);
  const out: (Reconciliation & { recorded: boolean })[] = [];
  for (let i = 1; i < dates.length; i += 1) {
    const from = dates[i - 1];
    const to = dates[i];
    if (from !== undefined && to !== undefined)
      out.push(await reconcileAccount(ctx, accountId, from, to, options));
  }
  return out;
}
