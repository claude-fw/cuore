/**
 * Riepiloghi finanziari: funzioni PURE su dati gia' filtrati.
 *
 * Regole:
 *   - mai sommare valute diverse: un blocco per valuta;
 *   - contano solo le transazioni contabilizzate (le pending sono riportate a parte);
 *   - i trasferimenti tra conti propri non sono ne' entrate ne' spese;
 *   - nemmeno i movimenti collegati a crediti/debiti (prestiti dati o ricevuti, restituzioni): sono riportati a parte;
 *   - i rimborsi riducono la spesa netta ma restano visibili a parte;
 *   - cio' che non e' classificato resta "da classificare" (in entrata e in uscita), mai attribuito a una categoria.
 */
import { addMinor } from "./money.ts";
import type { Kind } from "./rules.ts";

export type BudgetGroup = "needs" | "wants" | "savings";

/** Tipo effettivo nei riepiloghi: classificazione, oppure `obligation` per i movimenti collegati a crediti/debiti. */
export type SummaryKind = Kind | "obligation";

export interface SummaryItem {
  amountMinor: number;
  currency: string;
  status: "pending" | "booked";
  kind: SummaryKind;
  categoryId: string | null;
  budgetGroup: BudgetGroup | null;
}

export interface CurrencySummary {
  currency: string;
  income: number;
  expenses: number;
  fees: number;
  refunds: number;
  /** spese + commissioni - rimborsi */
  netSpending: number;
  /** entrate + rimborsi - spese - commissioni (trasferimenti esclusi) */
  net: number;
  transfersIn: number;
  transfersOut: number;
  /** denaro ricevuto/dato per crediti e debiti (prestiti, restituzioni): non entrate ne' spese */
  obligationsIn: number;
  obligationsOut: number;
  unclassifiedIn: number;
  unclassifiedOut: number;
  unclassifiedCount: number;
  /** entrate per categoria (es. stipendio, sostegno familiare); "(senza categoria)" = entrata senza categoria */
  incomeByCategory: Record<string, number>;
  /** spesa lorda (spese + commissioni) per categoria; null = senza categoria */
  spendingByCategory: Record<string, number>;
  /** spesa lorda per gruppo di budget (50/30/20); "unassigned" = categoria senza gruppo o assente */
  spendingByBudgetGroup: Record<BudgetGroup | "unassigned", number>;
  pendingCount: number;
  pendingSum: number;
  bookedCount: number;
}

function emptySummary(currency: string): CurrencySummary {
  return {
    currency,
    income: 0,
    expenses: 0,
    fees: 0,
    refunds: 0,
    netSpending: 0,
    net: 0,
    transfersIn: 0,
    transfersOut: 0,
    obligationsIn: 0,
    obligationsOut: 0,
    unclassifiedIn: 0,
    unclassifiedOut: 0,
    unclassifiedCount: 0,
    spendingByCategory: {},
    incomeByCategory: {},
    spendingByBudgetGroup: { needs: 0, wants: 0, savings: 0, unassigned: 0 },
    pendingCount: 0,
    pendingSum: 0,
    bookedCount: 0,
  };
}

const NO_CATEGORY = "(senza categoria)";

export function summarize(items: readonly SummaryItem[]): CurrencySummary[] {
  const byCurrency = new Map<string, CurrencySummary>();
  for (const it of items) {
    let s = byCurrency.get(it.currency);
    if (s === undefined) {
      s = emptySummary(it.currency);
      byCurrency.set(it.currency, s);
    }
    if (it.status === "pending") {
      s.pendingCount += 1;
      s.pendingSum = addMinor(s.pendingSum, it.amountMinor);
      continue;
    }
    s.bookedCount += 1;
    const abs = Math.abs(it.amountMinor);
    switch (it.kind) {
      case "income":
        s.income = addMinor(s.income, it.amountMinor);
        s.incomeByCategory[it.categoryId ?? NO_CATEGORY] = addMinor(
          s.incomeByCategory[it.categoryId ?? NO_CATEGORY] ?? 0,
          it.amountMinor,
        );
        break;
      case "refund":
        s.refunds = addMinor(s.refunds, it.amountMinor);
        break;
      case "expense":
      case "fee": {
        if (it.kind === "expense") s.expenses = addMinor(s.expenses, abs);
        else s.fees = addMinor(s.fees, abs);
        const cat = it.categoryId ?? NO_CATEGORY;
        s.spendingByCategory[cat] = addMinor(s.spendingByCategory[cat] ?? 0, abs);
        const group = it.budgetGroup ?? "unassigned";
        s.spendingByBudgetGroup[group] = addMinor(s.spendingByBudgetGroup[group], abs);
        break;
      }
      case "transfer":
        if (it.amountMinor >= 0) s.transfersIn = addMinor(s.transfersIn, it.amountMinor);
        else s.transfersOut = addMinor(s.transfersOut, abs);
        break;
      case "obligation":
        if (it.amountMinor >= 0) s.obligationsIn = addMinor(s.obligationsIn, it.amountMinor);
        else s.obligationsOut = addMinor(s.obligationsOut, abs);
        break;
      case "unknown":
        s.unclassifiedCount += 1;
        if (it.amountMinor >= 0) s.unclassifiedIn = addMinor(s.unclassifiedIn, it.amountMinor);
        else s.unclassifiedOut = addMinor(s.unclassifiedOut, abs);
        break;
    }
  }
  for (const s of byCurrency.values()) {
    s.netSpending = addMinor(addMinor(s.expenses, s.fees), -s.refunds);
    s.net = addMinor(addMinor(s.income, s.refunds), -addMinor(s.expenses, s.fees));
  }
  return [...byCurrency.values()].sort((a, b) => a.currency.localeCompare(b.currency));
}

/**
 * Quote 50/30/20 della spesa classificata, in punti base (10000 = 100%). La quota "unassigned" esclude nulla:
 * le percentuali si calcolano sul totale di spesa lorda, cosi' la parte non assegnata resta visibile.
 */
export function budgetShares(s: CurrencySummary): Record<BudgetGroup | "unassigned", number> {
  const total = addMinor(s.expenses, s.fees);
  const share = (v: number) => (total === 0 ? 0 : Math.round((v * 10_000) / total));
  return {
    needs: share(s.spendingByBudgetGroup.needs),
    wants: share(s.spendingByBudgetGroup.wants),
    savings: share(s.spendingByBudgetGroup.savings),
    unassigned: share(s.spendingByBudgetGroup.unassigned),
  };
}
