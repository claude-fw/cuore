/**
 * BUDGET MENSILI (richiede la migrazione v7, in attesa: src/db/migrations/007_manual_and_budgets.ts).
 *
 * Un budget vale per un ambito (una categoria con le sue sottocategorie, un gruppo di budget, il totale) da un mese in
 * poi, finche' una versione piu' recente non lo cambia o lo toglie. Le versioni sono append-only (storico completo).
 *
 * Speso del mese, con le stesse regole dei riepiloghi (services/effective.ts, domain/summary.ts):
 *   - solo movimenti contabilizzati, non esclusi, non annullati, nella valuta del budget, per data effettiva;
 *   - uscite = tipo effettivo `expense` + `fee`, in valore assoluto; categoria e gruppo solo da una classificazione
 *     decisa: nessuna categoria viene assegnata qui;
 *   - rimborsi, movimenti in attesa e uscite non ancora classificate sono riportati A PARTE, non sommati: il budget
 *     dice quanto e' certo e quanto potrebbe ancora pesare.
 */
import type { Ctx } from "../context.ts";
import { ValidationError } from "../context.ts";
import { stmt } from "../db/types.ts";
import { commitGuarded, readWriteSeq } from "../db/write.ts";
import { addMinor, assertMinor, isSupportedCurrency } from "../domain/money.ts";
import { auditStatement } from "./audit.ts";
import { EFFECTIVE_COLUMNS, EFFECTIVE_FROM } from "./effective.ts";

export type BudgetScope = "category" | "budget_group" | "total";
const GROUPS = ["needs", "wants", "savings"];

export interface BudgetInput {
  scope: BudgetScope;
  target: string | null;
  validFrom: string;
  currency: string;
  /** importo in unita' minime, > 0; `null` = budget tolto da quel mese */
  amountMinor: number | null;
  note?: string | null;
}

export interface BudgetLine {
  scope: BudgetScope;
  target: string | null;
  budgetMinor: number;
  spentMinor: number;
  remainingMinor: number;
  overBudget: boolean;
  /** versione del budget applicata */
  budgetId: string;
}

export interface BudgetStatus {
  month: string;
  currency: string;
  lines: BudgetLine[];
  /** non sommati alle righe: uscite non classificate, in attesa, rimborsi (positivi) del mese */
  unclassifiedOutflowMinor: number;
  pendingOutflowMinor: number;
  refundsMinor: number;
}

function isMonth(value: string): boolean {
  return /^[0-9]{4}-(0[1-9]|1[0-2])$/.test(value);
}

function nextMonth(month: string): string {
  const [y, m] = month.split("-").map(Number) as [number, number];
  return m === 12 ? `${y + 1}-01` : `${y}-${String(m + 1).padStart(2, "0")}`;
}

export async function setBudget(ctx: Ctx, input: BudgetInput): Promise<{ id: string }> {
  if (!["category", "budget_group", "total"].includes(input.scope)) {
    throw new ValidationError(`ambito non valido: ${input.scope}`);
  }
  if (!isMonth(input.validFrom)) throw new ValidationError(`mese non valido: ${input.validFrom} (AAAA-MM)`);
  if (!isSupportedCurrency(input.currency))
    throw new ValidationError(`valuta non supportata: ${input.currency}`);
  if (input.scope === "total" && input.target !== null)
    throw new ValidationError("il budget totale non ha obiettivo");
  if (input.scope === "budget_group" && !GROUPS.includes(input.target ?? "")) {
    throw new ValidationError(`gruppo di budget non valido: ${input.target}`);
  }
  if (input.scope === "category") {
    const cat = await ctx.db.first("SELECT id FROM categories WHERE id = ?", [input.target ?? ""]);
    if (cat === null) throw new ValidationError(`categoria inesistente: ${input.target}`);
  }
  if (input.amountMinor !== null) {
    assertMinor(input.amountMinor);
    if (input.amountMinor <= 0) throw new ValidationError("il budget deve essere maggiore di zero");
  }
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const id = ctx.newId();
  const after = {
    scope: input.scope,
    target: input.target,
    valid_from: input.validFrom,
    amount_minor: input.amountMinor,
    currency: input.currency,
  };
  await commitGuarded(ctx.db, seq, [
    stmt(
      `INSERT INTO budgets (id, scope, target, valid_from, amount_minor, currency, note, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      id,
      input.scope,
      input.target,
      input.validFrom,
      input.amountMinor,
      input.currency,
      input.note ?? null,
      at,
    ),
    auditStatement({
      at,
      actor: "user",
      action: input.amountMinor === null ? "remove_budget" : "set_budget",
      entity: "budget",
      entityId: id,
      after,
      reason: input.note ?? null,
    }),
  ]);
  return { id };
}

interface BudgetRow {
  id: string;
  scope: BudgetScope;
  target: string | null;
  amount_minor: number | null;
}

/** Versione in vigore per ogni ambito nel mese (l'ultima con valid_from <= mese). */
async function budgetsInForce(ctx: Ctx, month: string, currency: string): Promise<BudgetRow[]> {
  const rows = await ctx.db.all<BudgetRow & { valid_from: string; created_at: string }>(
    `SELECT id, scope, target, amount_minor, valid_from, created_at FROM budgets
     WHERE currency = ? AND valid_from <= ? ORDER BY valid_from, created_at, id`,
    [currency, month],
  );
  const latest = new Map<string, BudgetRow>();
  for (const r of rows) latest.set(`${r.scope}|${r.target ?? ""}`, r);
  return [...latest.values()].filter((r) => r.amount_minor !== null);
}

export async function budgetStatus(ctx: Ctx, month: string, currency: string): Promise<BudgetStatus> {
  if (!isMonth(month)) throw new ValidationError(`mese non valido: ${month} (AAAA-MM)`);
  const from = `${month}-01`;
  const to = `${nextMonth(month)}-01`;
  const items = await ctx.db.all<{
    status: string;
    amount_minor: number;
    effective_kind: string;
    effective_category_id: string | null;
    effective_budget_group: string | null;
  }>(
    `SELECT * FROM (
       SELECT t.status, t.amount_minor, t.currency, t.id, ${EFFECTIVE_COLUMNS} FROM ${EFFECTIVE_FROM}
     ) WHERE currency = ? AND effective_date >= ? AND effective_date < ? AND excluded = 0
       AND id NOT IN (SELECT transaction_id FROM manual_voids)`,
    [currency, from, to],
  );
  const parents = new Map<string, string | null>(
    (await ctx.db.all<{ id: string; parent_id: string | null }>("SELECT id, parent_id FROM categories")).map(
      (c) => [c.id, c.parent_id],
    ),
  );
  const within = (category: string | null, target: string): boolean => {
    for (let c = category, guard = 0; c !== null && guard < 50; c = parents.get(c) ?? null, guard++) {
      if (c === target) return true;
    }
    return false;
  };

  let unclassified = 0;
  let pending = 0;
  let refunds = 0;
  const spending: { abs: number; category: string | null; group: string | null }[] = [];
  for (const it of items) {
    if (it.status === "pending") {
      if (it.amount_minor < 0) pending = addMinor(pending, Math.abs(it.amount_minor));
      continue;
    }
    if (it.effective_kind === "expense" || it.effective_kind === "fee") {
      spending.push({
        abs: Math.abs(it.amount_minor),
        category: it.effective_category_id,
        group: it.effective_budget_group,
      });
    } else if (it.effective_kind === "refund") {
      refunds = addMinor(refunds, it.amount_minor);
    } else if (it.effective_kind === "unknown" && it.amount_minor < 0) {
      unclassified = addMinor(unclassified, Math.abs(it.amount_minor));
    }
  }

  const lines: BudgetLine[] = [];
  for (const b of await budgetsInForce(ctx, month, currency)) {
    const budget = b.amount_minor as number;
    let spent = 0;
    for (const s of spending) {
      const counts =
        b.scope === "total" ||
        (b.scope === "budget_group" && s.group === b.target) ||
        (b.scope === "category" && within(s.category, b.target as string));
      if (counts) spent = addMinor(spent, s.abs);
    }
    lines.push({
      scope: b.scope,
      target: b.target,
      budgetMinor: budget,
      spentMinor: spent,
      remainingMinor: budget - spent,
      overBudget: spent > budget,
      budgetId: b.id,
    });
  }
  lines.sort((a, b) => `${a.scope}|${a.target ?? ""}`.localeCompare(`${b.scope}|${b.target ?? ""}`));
  return {
    month,
    currency,
    lines,
    unclassifiedOutflowMinor: unclassified,
    pendingOutflowMinor: pending,
    refundsMinor: refunds,
  };
}
