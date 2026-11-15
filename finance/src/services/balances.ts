import type { Ctx } from "../context.ts";
import { ValidationError } from "../context.ts";
import { stmt } from "../db/types.ts";
import { commitGuarded, readWriteSeq } from "../db/write.ts";
import { assertMinor } from "../domain/money.ts";
import { isIsoDate } from "../domain/text.ts";
import { getAccount } from "./accounts.ts";
import { auditStatement, reviewStatement } from "./audit.ts";

/** Saldo di fine giornata inserito a mano (fonte `manual`). Saldi diversi per la stessa data -> da verificare. */
export async function addManualBalance(
  ctx: Ctx,
  accountId: string,
  asOfDate: string,
  balanceMinor: number,
  note: string | null = null,
): Promise<{ id: string; created: boolean }> {
  const account = await getAccount(ctx, accountId);
  if (!isIsoDate(asOfDate)) throw new ValidationError(`data non valida: ${asOfDate}`);
  assertMinor(balanceMinor);
  const existing = await ctx.db.all<{ id: string; balance_minor: number; source_id: string }>(
    "SELECT id, balance_minor, source_id FROM balance_observations WHERE account_id = ? AND as_of_date = ? AND kind = 'end_of_day'",
    [accountId, asOfDate],
  );
  const same = existing.find((e) => e.source_id === "manual" && e.balance_minor === balanceMinor);
  if (same !== undefined) return { id: same.id, created: false };
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const id = ctx.newId();
  const statements = [
    stmt(
      `INSERT INTO balance_observations (id, account_id, as_of_date, balance_minor, currency, kind, source_id, note, created_at)
       VALUES (?, ?, ?, ?, ?, 'end_of_day', 'manual', ?, ?)`,
      id,
      accountId,
      asOfDate,
      balanceMinor,
      account.currency,
      note,
      at,
    ),
    auditStatement({
      at,
      actor: "user",
      action: "observe_balance",
      entity: "balance_observation",
      entityId: id,
      after: { account: accountId, date: asOfDate, balance_minor: balanceMinor },
    }),
  ];
  const different = existing.filter((e) => e.balance_minor !== balanceMinor);
  if (different.length > 0) {
    statements.push(
      reviewStatement({
        id: ctx.newId(),
        kind: "balance_conflict",
        entity: "account_date",
        entityId: `${accountId}@${asOfDate}`,
        detail: { nuovo: balanceMinor, esistenti: different.map((e) => e.balance_minor) },
        createdAt: at,
      }),
    );
  }
  await commitGuarded(ctx.db, seq, statements);
  return { id, created: true };
}
