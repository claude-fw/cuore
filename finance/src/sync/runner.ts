/**
 * Sincronizzazione incrementale di un conto da una fonte (AccountDataSource) attraverso il motore d'importazione.
 *
 * Garanzie:
 *   - TUTTO O NIENTE: tutte le pagine e i saldi si leggono prima di scrivere; un errore della fonte interrompe la
 *     sincronizzazione senza alcuna scrittura (stato `interrotta`, con la categoria d'errore). Si riprende da sola: la
 *     finestra si ricava dallo storico delle importazioni, quindi il giro successivo rilegge lo stesso periodo.
 *   - NESSUNO STATO NASCOSTO: la finestra coperta e' nel nome dell'importazione (`sync:<fornitore>:<dal>..<al>`);
 *     l'ultima finestra `committed` (o gia' importata) e' il punto di ripresa. Nessuna tabella nuova.
 *   - SOVRAPPOSIZIONE: si rilegge dall'ultima data coperta meno `overlapDays` (default 10) per cogliere le pending
 *     diventate contabilizzate e le correzioni tardive; il motore riconosce le righe gia' presenti (id della fonte o
 *     impronta) e aggiorna pending -> booked con audit.
 *   - STORICO LIMITATO: senza un nuovo consenso le banche danno di norma solo gli ultimi 90 giorni (PSD2); se l'ultima
 *     copertura e' piu' vecchia, il buco viene DICHIARATO (`gap`) e mai colmato con dati inventati.
 *   - Ogni scrittura passa dal motore (`importFile`): storico grezzo immutabile, audit, rifiuto dell'intero pacchetto al
 *     primo problema. Dopo un'importazione con righe nuove si propongono i collegamenti (trasferimenti tra conti,
 *     rimborsi): solo PROPOSTE, mai confermate qui.
 */
import type { Ctx } from "../context.ts";
import { type ImportReport, importFile } from "../import/importer.ts";
import { type DetectCounts, detectLinks } from "../services/links.ts";
import {
  encodePayload,
  OPEN_BANKING_PARSER_ID,
  openBankingJsonV1,
  PAYLOAD_SCHEMA,
} from "./open-banking-json.ts";
import {
  type AccountDataSource,
  type FetchStrategy,
  RETRY_AFTER_HOURS,
  SourceError,
  type SourceErrorKind,
} from "./source.ts";

export interface SyncOptions {
  accountId: string;
  /** identificativo del conto presso il fornitore (es. uid della sessione Enable Banking) */
  accountRef: string;
  source: AccountDataSource;
  /** giorno di riferimento (YYYY-MM-DD, Europe/Rome dal chiamante) */
  today: string;
  historyDays?: number;
  overlapDays?: number;
  maxPages?: number;
  detect?: boolean;
}

export interface SyncReport {
  accountId: string;
  provider: string;
  status: "importata" | "nessun_dato_nuovo" | "interrotta" | "rifiutata";
  window: { from: string; to: string };
  firstSync: boolean;
  /** periodo non piu' leggibile senza un nuovo consenso con storico (dichiarato, mai colmato) */
  gap: { from: string; to: string } | null;
  pages: number;
  fetched: { transactions: number; balances: number };
  balancesRead: boolean;
  error: { kind: SourceErrorKind | "unexpected"; message: string } | null;
  /** ore dopo cui riprovare (null = serve il titolare: consenso, richiesta o risposta da esaminare) */
  retryAfterHours: number | null;
  /** strategia usata: `longest` alla prima acquisizione o quando il periodo richiesto non e' disponibile */
  strategy: FetchStrategy;
  import: (Pick<ImportReport, "runId" | "status" | "counts"> & { issues: number }) | null;
  links: DetectCounts | null;
}

const WINDOW_NAME = /^sync:([\w.-]+):(\d{4}-\d\d-\d\d)\.\.(\d{4}-\d\d-\d\d)$/;

export function addDays(day: string, n: number): string {
  const d = new Date(`${day}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

/** Ultima finestra coperta con successo (importata o gia' importata identica) per conto e fornitore. */
export async function lastWindow(
  ctx: Ctx,
  accountId: string,
  provider: string,
): Promise<{ from: string; to: string } | null> {
  const rows = await ctx.db.all<{ file_name: string | null }>(
    `SELECT file_name FROM import_runs WHERE account_id = ? AND parser_id = ? AND status IN ('committed', 'duplicate_file')
     ORDER BY finished_at DESC, id DESC`,
    [accountId, OPEN_BANKING_PARSER_ID],
  );
  let best: { from: string; to: string } | null = null;
  for (const r of rows) {
    const m = WINDOW_NAME.exec(r.file_name ?? "");
    if (m && m[1] === provider && (best === null || (m[3] ?? "") > best.to))
      best = { from: m[2] ?? "", to: m[3] ?? "" };
  }
  return best;
}

const canonicalOrder = (xs: readonly unknown[]) =>
  [...xs]
    .map((x) => [JSON.stringify(x), x] as const)
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .map((p) => p[1]);

export async function syncAccount(ctx: Ctx, o: SyncOptions): Promise<SyncReport> {
  const historyDays = o.historyDays ?? 90;
  const overlap = o.overlapDays ?? 10;
  const maxPages = o.maxPages ?? 100;
  const earliest = addDays(o.today, -historyDays);
  const last = await lastWindow(ctx, o.accountId, o.source.provider);
  const wanted = last === null ? earliest : addDays(last.to, -overlap);
  const from = wanted < earliest ? earliest : wanted;
  const gap = last !== null && wanted < earliest ? { from: last.to, to: earliest } : null;
  const report: SyncReport = {
    accountId: o.accountId,
    provider: o.source.provider,
    status: "interrotta",
    window: { from, to: o.today },
    firstSync: last === null,
    gap,
    pages: 0,
    fetched: { transactions: 0, balances: 0 },
    balancesRead: false,
    error: null,
    retryAfterHours: null,
    strategy: last === null ? "longest" : "default",
    import: null,
    links: null,
  };

  const readAll = async (strategy: FetchStrategy): Promise<unknown[]> => {
    const out: unknown[] = [];
    const seen = new Set<string>();
    let key: string | null = null;
    do {
      if (report.pages >= maxPages)
        throw new SourceError("invalid_response", `oltre ${maxPages} pagine: lettura interrotta`);
      // una pagina vuota con chiave di continuazione e' normale (ricerca del primo movimento): si prosegue
      const page = await o.source.transactions(o.accountRef, from, o.today, key, strategy);
      report.pages += 1;
      out.push(...page.transactions);
      key = page.continuationKey;
      if (key !== null) {
        if (seen.has(key))
          throw new SourceError("invalid_response", "paginazione ripetuta: la fonte non termina");
        seen.add(key);
      }
    } while (key !== null);
    return out;
  };

  let transactions: unknown[] = [];
  let balances: readonly unknown[] = [];
  try {
    try {
      transactions = await readAll(report.strategy);
    } catch (e) {
      // periodo non disponibile con la strategia esatta: una sola nuova lettura dal primo movimento disponibile
      if (!(e instanceof SourceError && e.kind === "period_unavailable" && report.strategy === "default"))
        throw e;
      report.strategy = "longest";
      transactions = await readAll("longest");
    }
    report.fetched.transactions = transactions.length;
    if (report.strategy === "longest") {
      // `longest` parte dal primo movimento disponibile: la finestra registrata dice cio' che e' stato davvero letto
      const days = transactions.flatMap((t) => {
        const r = t as Record<string, unknown>;
        const d = r.transaction_date ?? r.booking_date ?? r.value_date;
        return typeof d === "string" && /^\d{4}-\d\d-\d\d$/.test(d) ? [d] : [];
      });
      const first = days.sort()[0];
      if (first !== undefined && first < report.window.from) report.window.from = first;
    }
    try {
      balances = await o.source.balances(o.accountRef);
      report.balancesRead = true;
      report.fetched.balances = balances.length;
    } catch (e) {
      // i saldi completano il quadro ma non bloccano i movimenti: dichiarati come non letti
      if (!(e instanceof SourceError)) throw e;
    }
  } catch (e) {
    report.error =
      e instanceof SourceError
        ? { kind: e.kind, message: e.message }
        : { kind: "unexpected", message: "errore imprevisto della fonte" };
    report.retryAfterHours = RETRY_AFTER_HOURS[report.error.kind];
    return report;
  }

  const content = encodePayload({
    schema: PAYLOAD_SCHEMA,
    provider: o.source.provider,
    window: report.window,
    transactions: canonicalOrder(transactions),
    balances: canonicalOrder(balances),
  });
  const r = await importFile(ctx, {
    accountId: o.accountId,
    parserId: OPEN_BANKING_PARSER_ID,
    parser: openBankingJsonV1,
    fileName: `sync:${o.source.provider}:${report.window.from}..${o.today}`,
    content,
  });
  report.import = { runId: r.runId, status: r.status, counts: r.counts, issues: r.issues.length };
  if (r.status === "rejected") {
    report.status = "rifiutata";
    return report;
  }
  const changed =
    r.status === "committed" && r.counts.created + r.counts.updated + r.counts.balancesCreated > 0;
  report.status = changed ? "importata" : "nessun_dato_nuovo";
  if (changed && r.counts.created > 0 && (o.detect ?? true)) report.links = await detectLinks(ctx);
  return report;
}

/** Piu' conti in sequenza: l'errore di uno non ferma gli altri. */
export async function syncAccounts(ctx: Ctx, items: readonly SyncOptions[]): Promise<SyncReport[]> {
  const out: SyncReport[] = [];
  for (const it of items) out.push(await syncAccount(ctx, it));
  return out;
}
