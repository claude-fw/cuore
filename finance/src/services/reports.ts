/**
 * Riepiloghi letti dal database: preparano i dati e delegano i calcoli alle funzioni pure di src/domain.
 * Nessuna scrittura.
 */
import type { Ctx } from "../context.ts";
import { ValidationError } from "../context.ts";
import { convertMinor } from "../domain/money.ts";
import { budgetShares, type CurrencySummary, type SummaryItem, summarize } from "../domain/summary.ts";
import { isIsoDate } from "../domain/text.ts";
import { EFFECTIVE_COLUMNS, EFFECTIVE_FROM, type EffectiveColumns, EXCLUDED_IDS_SQL } from "./effective.ts";

export interface PeriodReport {
  from: string;
  to: string;
  accounts: string[] | "all";
  currencies: (CurrencySummary & { budgetSharesBp: ReturnType<typeof budgetShares> })[];
  excludedDuplicates: number;
  openReviewItems: number;
}

interface Row extends EffectiveColumns {
  amount_minor: number;
  currency: string;
  status: "pending" | "booked";
}

async function effectiveRows(ctx: Ctx, from: string, to: string, accountIds?: string[]): Promise<Row[]> {
  if (!isIsoDate(from) || !isIsoDate(to) || from > to) throw new ValidationError("periodo non valido");
  const accountFilter =
    accountIds && accountIds.length > 0 ? `AND t.account_id IN (${accountIds.map(() => "?").join(",")})` : "";
  return ctx.db.all<Row>(
    `SELECT t.amount_minor, t.currency, t.status, ${EFFECTIVE_COLUMNS}
     FROM ${EFFECTIVE_FROM}
     WHERE ((t.status = 'booked' AND t.booking_date BETWEEN ? AND ?)
         OR (t.status = 'pending' AND t.transaction_date BETWEEN ? AND ?)) ${accountFilter}`,
    [from, to, from, to, ...(accountIds ?? [])],
  );
}

const summaryItem = (r: Row): SummaryItem => ({
  amountMinor: r.amount_minor,
  currency: r.currency,
  status: r.status,
  kind: r.effective_kind,
  categoryId: r.effective_category_id,
  budgetGroup: r.effective_budget_group,
});

/**
 * Riepilogo del periodo [from, to] (date incluse) sulla data effettiva: contabilizzate per data di contabilizzazione,
 * pending per data operazione. Tipo, categoria ed esclusioni dalla definizione unica di `effective.ts`
 * (collegato a crediti/debiti, trasferimento confermato, classificazione decisa; altrimenti da classificare).
 */
export async function periodReport(
  ctx: Ctx,
  from: string,
  to: string,
  accountIds?: string[],
): Promise<PeriodReport> {
  const rows = await effectiveRows(ctx, from, to, accountIds);
  const open = await ctx.db.first<{ n: number }>(
    "SELECT COUNT(*) AS n FROM review_items WHERE status = 'open'",
  );
  return {
    from,
    to,
    accounts: accountIds && accountIds.length > 0 ? accountIds : "all",
    currencies: summarize(rows.filter((r) => !r.excluded).map(summaryItem)).map((s) => ({
      ...s,
      budgetSharesBp: budgetShares(s),
    })),
    excludedDuplicates: rows.filter((r) => r.excluded).length,
    openReviewItems: open?.n ?? 0,
  };
}

/**
 * Lo stesso riepilogo di `periodReport`, mese per mese, in UNA lettura (la serie di 12 mesi con 12 `periodReport`
 * rileggeva tutti i movimenti 12 volte: quota di righe lette del piano Free). Anche i mesi senza movimenti.
 */
export async function monthlyReport(
  ctx: Ctx,
  from: string,
  to: string,
): Promise<{ month: string; currencies: CurrencySummary[] }[]> {
  const byMonth = new Map<string, SummaryItem[]>();
  for (let m = from.slice(0, 7); m <= to.slice(0, 7); ) {
    byMonth.set(m, []);
    const [y, mm] = m.split("-").map(Number) as [number, number];
    m = mm === 12 ? `${y + 1}-01` : `${y}-${String(mm + 1).padStart(2, "0")}`;
  }
  for (const r of await effectiveRows(ctx, from, to)) {
    if (!r.excluded) byMonth.get(r.effective_date.slice(0, 7))?.push(summaryItem(r));
  }
  return [...byMonth].map(([month, items]) => ({ month, currencies: summarize(items) }));
}

/**
 * Lo stesso riepilogo di `periodReport`, giorno per giorno (data effettiva): solo i giorni con almeno un movimento
 * non escluso, in ordine. La somma dei giorni coincide con il riepilogo dell'intero periodo.
 */
export async function dailyReport(
  ctx: Ctx,
  from: string,
  to: string,
): Promise<{ date: string; currencies: CurrencySummary[] }[]> {
  const byDay = new Map<string, SummaryItem[]>();
  for (const r of await effectiveRows(ctx, from, to)) {
    if (r.excluded) continue;
    const list = byDay.get(r.effective_date) ?? [];
    list.push(summaryItem(r));
    byDay.set(r.effective_date, list);
  }
  return [...byDay.keys()].sort().map((date) => ({ date, currencies: summarize(byDay.get(date) ?? []) }));
}

export interface LiquidityLine {
  accountId: string;
  currency: string;
  balanceMinor: number | null;
  observedOn: string | null;
  /** conflitto: per l'ultima data esistono piu' saldi diversi */
  certainty: "observed" | "conflict" | "missing";
  converted: {
    currency: string;
    amountMinor: number;
    rate: string;
    rateDate: string;
    estimated: true;
  } | null;
}

/**
 * Liquidita' alla data: ultimo saldo osservato <= asOf per ogni conto (nessuna stima dai movimenti).
 * Con `baseCurrency`, converte con l'ultimo cambio disponibile <= asOf e marca il valore come STIMA.
 */
export async function liquidity(ctx: Ctx, asOf: string, baseCurrency?: string): Promise<LiquidityLine[]> {
  if (!isIsoDate(asOf)) throw new ValidationError("data non valida");
  const accounts = await ctx.db.all<{ id: string; currency: string; type: string }>(
    "SELECT id, currency, type FROM accounts ORDER BY id",
  );
  const out: LiquidityLine[] = [];
  for (const a of accounts) {
    const last = await ctx.db.first<{ d: string | null }>(
      "SELECT MAX(as_of_date) AS d FROM balance_observations WHERE account_id = ? AND as_of_date <= ?",
      [a.id, asOf],
    );
    if (last?.d == null) {
      out.push({
        accountId: a.id,
        currency: a.currency,
        balanceMinor: null,
        observedOn: null,
        certainty: "missing",
        converted: null,
      });
      continue;
    }
    const values = [
      ...new Set(
        (
          await ctx.db.all<{ balance_minor: number }>(
            "SELECT balance_minor FROM balance_observations WHERE account_id = ? AND as_of_date = ?",
            [a.id, last.d],
          )
        ).map((r) => r.balance_minor),
      ),
    ];
    let balance = values.length === 1 ? (values[0] ?? null) : null;
    // Contanti: nessuna banca manda il saldo. Vale l'ultimo saldo indicato a mano piu' i movimenti segnati dopo
    // (spese, entrate, restituzioni), esclusi gli annullati e i doppioni.
    if (balance !== null && a.type === "cash") {
      const dopo = await ctx.db.first<{ s: number | null }>(
        `SELECT SUM(amount_minor) AS s FROM transactions
         WHERE account_id = ? AND status = 'booked' AND booking_date > ? AND booking_date <= ?
           AND id NOT IN (${EXCLUDED_IDS_SQL})`,
        [a.id, last.d, asOf],
      );
      balance += dopo?.s ?? 0;
    }
    let converted: LiquidityLine["converted"] = null;
    if (balance !== null && baseCurrency !== undefined && baseCurrency !== a.currency) {
      const fx = await ctx.db.first<{ rate: string; rate_date: string }>(
        "SELECT rate, rate_date FROM fx_rates WHERE base = ? AND quote = ? AND rate_date <= ? ORDER BY rate_date DESC, source LIMIT 1",
        [a.currency, baseCurrency, asOf],
      );
      if (fx !== null) {
        converted = {
          currency: baseCurrency,
          amountMinor: convertMinor(balance, a.currency, baseCurrency, fx.rate),
          rate: fx.rate,
          rateDate: fx.rate_date,
          estimated: true,
        };
      }
    }
    out.push({
      accountId: a.id,
      currency: a.currency,
      balanceMinor: balance,
      observedOn: last.d,
      certainty: values.length === 1 ? "observed" : "conflict",
      converted,
    });
  }
  return out;
}

export interface Status {
  schemaVersion: number;
  accounts: number;
  transactions: { booked: number; pending: number };
  lastImports: { accountId: string; lastCommitted: string | null }[];
  openReviewItems: number;
  ruleSetVersion: number | null;
}

export async function status(ctx: Ctx): Promise<Status> {
  const schema = await ctx.db.first<{ v: number | null }>("SELECT MAX(version) AS v FROM schema_migrations");
  const accounts = await ctx.db.all<{ id: string }>("SELECT id FROM accounts ORDER BY id");
  const counts = await ctx.db.all<{ status: string; n: number }>(
    "SELECT status, COUNT(*) AS n FROM transactions GROUP BY status",
  );
  const lastImports = await ctx.db.all<{ account_id: string; last: string | null }>(
    `SELECT a.id AS account_id, MAX(r.finished_at) AS last FROM accounts a
     LEFT JOIN import_runs r ON r.account_id = a.id AND r.status = 'committed' GROUP BY a.id ORDER BY a.id`,
  );
  const open = await ctx.db.first<{ n: number }>(
    "SELECT COUNT(*) AS n FROM review_items WHERE status = 'open'",
  );
  const rules = await ctx.db.first<{ v: number | null }>("SELECT MAX(version) AS v FROM rule_sets");
  return {
    schemaVersion: schema?.v ?? 0,
    accounts: accounts.length,
    transactions: {
      booked: counts.find((c) => c.status === "booked")?.n ?? 0,
      pending: counts.find((c) => c.status === "pending")?.n ?? 0,
    },
    lastImports: lastImports.map((r) => ({ accountId: r.account_id, lastCommitted: r.last })),
    openReviewItems: open?.n ?? 0,
    ruleSetVersion: rules?.v ?? null,
  };
}
