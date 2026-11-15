/**
 * Obiettivi di risparmio, acquisti pianificati e cambi osservati.
 * Un acquisto pianificato NON e' una transazione: diventa "purchased" solo collegandolo (a mano) a una transazione
 * reale. `notion_block_id` e' una colonna STORICA (schema 003) mantenuta per compatibilita': nessun nuovo valore viene
 * scritto e nessun modulo accede a Notion (Notion non fa parte di CUORE, 2026-10-24).
 */
import type { Ctx } from "../context.ts";
import { NotFoundError, ValidationError } from "../context.ts";
import { stmt } from "../db/types.ts";
import { commitGuarded, readWriteSeq } from "../db/write.ts";
import { type GoalProgress, goalProgress } from "../domain/goals.ts";
import { assertMinor, isSupportedCurrency } from "../domain/money.ts";
import { isIsoDate } from "../domain/text.ts";
import { auditStatement } from "./audit.ts";

export interface Goal {
  id: string;
  name: string;
  target_minor: number;
  currency: string;
  deadline: string | null;
  status: "active" | "paused" | "reached" | "cancelled";
}

export async function createGoal(
  ctx: Ctx,
  input: { name: string; targetMinor: number; currency: string; deadline?: string | null },
): Promise<Goal> {
  if (input.name.trim() === "") throw new ValidationError("nome dell'obiettivo obbligatorio");
  assertMinor(input.targetMinor);
  if (input.targetMinor <= 0) throw new ValidationError("importo obiettivo deve essere positivo");
  if (!isSupportedCurrency(input.currency))
    throw new ValidationError(`valuta non supportata: ${input.currency}`);
  if (input.deadline != null && !isIsoDate(input.deadline)) throw new ValidationError("scadenza non valida");
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const goal: Goal = {
    id: ctx.newId(),
    name: input.name,
    target_minor: input.targetMinor,
    currency: input.currency,
    deadline: input.deadline ?? null,
    status: "active",
  };
  await commitGuarded(ctx.db, seq, [
    stmt(
      `INSERT INTO savings_goals (id, name, target_minor, currency, deadline, status, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, 'active', ?, ?)`,
      goal.id,
      goal.name,
      goal.target_minor,
      goal.currency,
      goal.deadline,
      at,
      at,
    ),
    auditStatement({
      at,
      actor: "user",
      action: "create",
      entity: "savings_goal",
      entityId: goal.id,
      after: goal,
    }),
  ]);
  return goal;
}

/** Accantonamento virtuale (positivo) o sua correzione (negativo). Append-only. */
export async function allocateToGoal(
  ctx: Ctx,
  goalId: string,
  amountMinor: number,
  allocatedOn: string,
  note: string | null = null,
): Promise<string> {
  const goal = await ctx.db.first<Goal>("SELECT * FROM savings_goals WHERE id = ?", [goalId]);
  if (goal === null) throw new NotFoundError("obiettivo", goalId);
  assertMinor(amountMinor);
  if (amountMinor === 0) throw new ValidationError("importo nullo");
  if (!isIsoDate(allocatedOn)) throw new ValidationError("data non valida");
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const id = ctx.newId();
  await commitGuarded(ctx.db, seq, [
    stmt(
      `INSERT INTO goal_allocations (id, goal_id, amount_minor, currency, allocated_on, note, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)`,
      id,
      goalId,
      amountMinor,
      goal.currency,
      allocatedOn,
      note,
      at,
    ),
    auditStatement({
      at,
      actor: "user",
      action: "allocate",
      entity: "savings_goal",
      entityId: goalId,
      after: { amountMinor, allocatedOn },
    }),
  ]);
  return id;
}

export async function listGoalsWithProgress(ctx: Ctx): Promise<(Goal & { progress: GoalProgress })[]> {
  const goals = await ctx.db.all<Goal>("SELECT * FROM savings_goals ORDER BY created_at, id");
  const allocations = await ctx.db.all<{ goal_id: string; amount_minor: number }>(
    "SELECT goal_id, amount_minor FROM goal_allocations ORDER BY allocated_on, id",
  );
  return goals.map((g) => ({
    ...g,
    progress: goalProgress(
      g.target_minor,
      allocations.filter((a) => a.goal_id === g.id).map((a) => a.amount_minor),
    ),
  }));
}

export type PurchaseStatus = "idea" | "planned" | "purchased" | "cancelled";

export interface PlannedPurchase {
  id: string;
  title: string;
  estimated_minor: number | null;
  currency: string;
  priority: number | null;
  status: PurchaseStatus;
  goal_id: string | null;
  notion_block_id: string | null;
  transaction_id: string | null;
}

export async function createPlannedPurchase(
  ctx: Ctx,
  input: {
    title: string;
    currency: string;
    estimatedMinor?: number | null;
    priority?: number | null;
    goalId?: string | null;
  },
): Promise<PlannedPurchase> {
  if (input.title.trim() === "") throw new ValidationError("titolo obbligatorio");
  if (!isSupportedCurrency(input.currency))
    throw new ValidationError(`valuta non supportata: ${input.currency}`);
  if (input.estimatedMinor != null) {
    assertMinor(input.estimatedMinor);
    if (input.estimatedMinor <= 0) throw new ValidationError("stima deve essere positiva");
  }
  if (
    input.priority != null &&
    !(Number.isInteger(input.priority) && input.priority >= 1 && input.priority <= 5)
  ) {
    throw new ValidationError("priorita' da 1 a 5");
  }
  if (
    input.goalId != null &&
    (await ctx.db.first("SELECT id FROM savings_goals WHERE id = ?", [input.goalId])) === null
  ) {
    throw new NotFoundError("obiettivo", input.goalId);
  }
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const p: PlannedPurchase = {
    id: ctx.newId(),
    title: input.title,
    estimated_minor: input.estimatedMinor ?? null,
    currency: input.currency,
    priority: input.priority ?? null,
    status: "idea",
    goal_id: input.goalId ?? null,
    notion_block_id: null, // colonna storica: mai valorizzata
    transaction_id: null,
  };
  await commitGuarded(ctx.db, seq, [
    stmt(
      `INSERT INTO planned_purchases (id, title, estimated_minor, currency, priority, status, goal_id, notion_block_id,
         created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, 'idea', ?, ?, ?, ?)`,
      p.id,
      p.title,
      p.estimated_minor,
      p.currency,
      p.priority,
      p.goal_id,
      p.notion_block_id,
      at,
      at,
    ),
    auditStatement({
      at,
      actor: "user",
      action: "create",
      entity: "planned_purchase",
      entityId: p.id,
      after: p,
    }),
  ]);
  return p;
}

const TRANSITIONS: Record<PurchaseStatus, readonly PurchaseStatus[]> = {
  idea: ["planned", "purchased", "cancelled"],
  planned: ["idea", "purchased", "cancelled"],
  purchased: [],
  cancelled: ["idea"],
};

/** Cambio di stato. `purchased` richiede la transazione reale (uscita, stessa valuta) che lo documenta. */
export async function setPurchaseStatus(
  ctx: Ctx,
  purchaseId: string,
  status: PurchaseStatus,
  transactionId: string | null = null,
): Promise<void> {
  const p = await ctx.db.first<PlannedPurchase>("SELECT * FROM planned_purchases WHERE id = ?", [purchaseId]);
  if (p === null) throw new NotFoundError("acquisto pianificato", purchaseId);
  if (!TRANSITIONS[p.status].includes(status))
    throw new ValidationError(`passaggio non ammesso: ${p.status} -> ${status}`);
  if (status === "purchased") {
    if (transactionId === null)
      throw new ValidationError("per segnare l'acquisto serve la transazione reale");
    const t = await ctx.db.first<{ amount_minor: number; currency: string }>(
      "SELECT amount_minor, currency FROM transactions WHERE id = ?",
      [transactionId],
    );
    if (t === null) throw new NotFoundError("transazione", transactionId);
    if (t.amount_minor >= 0 || t.currency !== p.currency) {
      throw new ValidationError("la transazione deve essere un'uscita nella valuta dell'acquisto");
    }
  } else if (transactionId !== null) {
    throw new ValidationError("la transazione si indica solo per lo stato purchased");
  }
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  await commitGuarded(ctx.db, seq, [
    stmt(
      "UPDATE planned_purchases SET status = ?, transaction_id = ?, updated_at = ? WHERE id = ?",
      status,
      transactionId,
      at,
      purchaseId,
    ),
    auditStatement({
      at,
      actor: "user",
      action: "set_status",
      entity: "planned_purchase",
      entityId: purchaseId,
      before: { status: p.status },
      after: { status, transaction_id: transactionId },
    }),
  ]);
}

export function listPlannedPurchases(ctx: Ctx): Promise<PlannedPurchase[]> {
  return ctx.db.all<PlannedPurchase>(
    "SELECT id, title, estimated_minor, currency, priority, status, goal_id, notion_block_id, transaction_id FROM planned_purchases ORDER BY status, priority, title",
  );
}

const RATE = /^\d+(\.\d+)?$/;

/** Cambio osservato da una fonte dichiarata (es. inserimento manuale). Le conversioni restano stime. */
export async function addFxRate(
  ctx: Ctx,
  rateDate: string,
  base: string,
  quote: string,
  rate: string,
  source: string,
): Promise<void> {
  if (!isIsoDate(rateDate)) throw new ValidationError("data non valida");
  if (!isSupportedCurrency(base) || !isSupportedCurrency(quote) || base === quote)
    throw new ValidationError("valute non valide");
  if (!RATE.test(rate) || Number(rate) === 0) throw new ValidationError(`tasso non valido: ${rate}`);
  if (source.trim() === "") throw new ValidationError("fonte obbligatoria");
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  await commitGuarded(ctx.db, seq, [
    stmt(
      "INSERT INTO fx_rates (rate_date, base, quote, rate, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
      rateDate,
      base,
      quote,
      rate,
      source,
      at,
    ),
  ]);
}
