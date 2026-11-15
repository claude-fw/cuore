/**
 * CUORE Money, parte COMUNE al ponte del Mac (`cuore_money.ts`, database aperto in sola lettura immutabile) e al cloud
 * (`cloud/src/online.ts`, Durable Object Finance con i dati veri): stesso piano di consultazioni, stesso contratto
 * minimo, stessa serie `trend`. Nessun modulo Node, nessuna rete, nessun file: solo `runQuery` del motore Finance su un
 * `Db` gia' aperto da chi chiama. Dettagli del contratto: intestazione di `cuore_money.ts`.
 */
import { type QueryResponse, runQuery } from "../../finance/src/query/operations.ts";

export const SCHEMA = "cuore.money.query/1";
export const TRANSACTIONS_DAYS = 30;
export const TRANSACTIONS_LIMIT = 50;
export const TREND_MONTHS = 12;
const TREND_FIELDS = [
  "currency",
  "income",
  "netSpending",
  "unclassifiedOut",
  "unclassifiedCount",
  "pendingCount",
  "spendingByCategory",
];

function addDays(day: string, n: number): string {
  const d = new Date(`${day}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

type Rec = Record<string, unknown>;
const pick = (o: Rec, keys: readonly string[]): Rec =>
  Object.fromEntries(keys.filter((k) => k in o).map((k) => [k, o[k]]));

/** Riduce i dati al contratto minimo (le buste di errore passano invariate). */
function minimal(op: string, r: QueryResponse): QueryResponse {
  if (!r.ok) return r;
  const data = r.data as unknown;
  if (op === "accounts")
    return {
      ...r,
      data: (data as Rec[]).map((a) => ({
        ...pick(a, ["id", "name", "type", "currency", "institutionId"]),
        closed: ((a.status as Rec | undefined)?.closed as boolean | undefined) ?? false,
        lastMovementOn: ((a.status as Rec | undefined)?.lastMovementOn as string | null | undefined) ?? null,
      })),
    };
  if (op === "obligations") {
    const d = data as Rec;
    return {
      ...r,
      data: {
        asOf: d.asOf,
        items: (d.items as Rec[]).map((o) =>
          pick(o, ["direction", "currency", "dueOn", "daysToDue", "status", "overdue", "residualMinor"]),
        ),
        truncated: d.truncated,
        position: d.position,
      },
    };
  }
  if (op === "summary") {
    const d = data as Rec;
    return {
      ...r,
      data: {
        ...pick(d, ["from", "to", "openReviewItems", "notes"]),
        currencies: (d.currencies as Rec[]).map((c) =>
          pick(c, [
            "currency",
            "netSpending",
            "unclassifiedOut",
            "unclassifiedCount",
            "pendingCount",
            "spendingByCategory",
            "spendingByBudgetGroup",
            "budgetSharesBp",
          ]),
        ),
      },
    };
  }
  if (op === "planning") {
    const d = data as { goals: Rec[]; purchases: Rec[] };
    return {
      ...r,
      data: {
        goals: d.goals,
        purchases: d.purchases.map((x) =>
          pick(x, ["id", "title", "currency", "estimatedMinor", "priority", "status", "goalId"]),
        ),
      },
    };
  }
  if (op === "categories") {
    const d = data as { items: Rec[] };
    return { ...r, data: { items: d.items.map((c) => pick(c, ["id", "name", "parentId", "budgetGroup"])) } };
  }
  if (op === "transactions") {
    const d = data as Rec;
    return {
      ...r,
      data: {
        items: (d.items as Rec[]).map((t) =>
          pick(t, [
            "id",
            "date",
            "status",
            "accountId",
            "amountMinor",
            "currency",
            "description",
            "kind",
            "categoryId",
            "excluded",
          ]),
        ),
        truncated: d.nextCursor != null,
        limit: d.limit,
      },
    };
  }
  return r;
}

/** Primo giorno dei mesi di calendario, dal piu' vecchio al mese di `today`. */
export function monthStarts(today: string, n: number): string[] {
  const y = Number(today.slice(0, 4));
  const m = Number(today.slice(5, 7)) - 1;
  return Array.from({ length: n }, (_, i) => {
    const d = new Date(Date.UTC(y, m - (n - 1 - i), 1));
    return d.toISOString().slice(0, 10);
  });
}

type QueryOptions = NonNullable<Parameters<typeof runQuery>[3]>;

/** Serie mensile da `summary`: al primo errore la busta di errore (mai una serie con mesi mancanti). */
async function readTrend(
  db: Parameters<typeof runQuery>[0],
  today: string,
  q: QueryOptions,
): Promise<QueryResponse> {
  // una sola lettura per tutti i mesi (operazione `monthly`): prima erano 12 `summary`, ognuna su tutti i movimenti
  const starts = monthStarts(today, TREND_MONTHS);
  const r = await runQuery(db, "monthly", { from: starts[0] as string, to: today }, q);
  if (!r.ok) return { ...r, operation: "trend" };
  const byMonth = new Map(
    (r.data as { months: { month: string; currencies: Rec[] }[] }).months.map((m) => [m.month, m.currencies]),
  );
  const months: Rec[] = starts.map((from) => {
    const last = new Date(Date.UTC(Number(from.slice(0, 4)), Number(from.slice(5, 7)), 0))
      .toISOString()
      .slice(0, 10);
    const to = last < today ? last : today;
    return {
      month: from.slice(0, 7),
      from,
      to,
      currencies: (byMonth.get(from.slice(0, 7)) ?? []).map((c) => pick(c, TREND_FIELDS)),
    };
  });
  return { ok: true, contractVersion: 1, operation: "trend", data: { months } };
}

type Db = Parameters<typeof runQuery>[0];

/** Documento `cuore.money.query/1` letto da un `Db` gia' aperto (in sola lettura per chi chiama). */
export async function queryMoney(
  db: Db,
  today: string,
  schema?: { expectedSchemaVersion: number },
): Promise<Rec> {
  const q: QueryOptions = schema ? { today, expectedSchemaVersion: schema.expectedSchemaVersion } : { today };
  const monthStart = `${today.slice(0, 8)}01`;
  const prevStart = monthStarts(today, 2)[0] as string;
  const plan: [string, Rec][] = [
    ["accounts", {}],
    ["overview", { from: monthStart, to: today }],
    ["summary", { from: monthStart, to: today }],
    ["categories", {}],
    ["planning", {}],
    ["balances", { asOf: today }],
    ["data_status", { asOf: today }],
    ["obligations", { asOf: today }],
    ["transactions", { from: addDays(today, -TRANSACTIONS_DAYS), to: today, limit: TRANSACTIONS_LIMIT }],
    ["manual", { from: addDays(today, -TRANSACTIONS_DAYS), to: today }],
    ["budgets", { month: today.slice(0, 7) }],
    ["daily", { from: prevStart, to: today }],
  ];
  const operazioni: Rec = {};
  // nel Durable Object il Db conta le righe lette (quota del piano Free): una riga di log per parte del feed
  const righe = () => (db as { rowsRead?: number }).rowsRead;
  const costo: Rec = {};
  for (const [op, input] of plan) {
    const prima = righe();
    operazioni[op] = minimal(op, await runQuery(db, op, input, q));
    if (prima !== undefined) costo[op] = (righe() as number) - prima;
  }
  const primaTrend = righe();
  operazioni.trend = await readTrend(db, today, q);
  if (primaTrend !== undefined) {
    costo.trend = (righe() as number) - primaTrend;
    console.log(JSON.stringify({ righe_per_parte: costo }));
  }
  return {
    schema: SCHEMA,
    motore: "Cloud Finance · consultazione v1",
    oggi: today,
    periodo: {
      mese: { from: monthStart, to: today },
      movimenti: { from: addDays(today, -TRANSACTIONS_DAYS), to: today },
      giorni: { from: prevStart, to: today },
    },
    operazioni,
  };
}
