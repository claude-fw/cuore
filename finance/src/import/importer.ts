/**
 * Importazione da file: leggi -> analizza -> pianifica (puro) -> scrivi tutto in UN batch atomico.
 *
 * Garanzie:
 *   - file gia' importato (stesso conto, formato e sha256) -> nessuna modifica, esito `duplicate_file`;
 *   - stesso contenuto in un file diverso (righe riordinate, CRLF, periodi sovrapposti) -> nessun doppione (piano);
 *   - un solo problema di analisi -> file rifiutato per intero (`rejected`), nessuna riga scritta;
 *   - errore di scrittura -> rollback completo; l'esecuzione viene registrata come `rejected`;
 *   - ogni riga letta e' conservata nello storico grezzo immutabile e collegata alla transazione (provenienza);
 *   - ogni creazione/modifica di transazione ha una riga di audit nello stesso batch;
 *   - correzioni dichiarate della data contabile (con motivo e prova) applicate dopo l'analisi, registrate nell'audit;
 *     i dati grezzi restano quelli del file.
 */
import type { Ctx } from "../context.ts";
import type { Statement } from "../db/types.ts";
import { stmt } from "../db/types.ts";
import { ConflictError, commitGuarded, readWriteSeq } from "../db/write.ts";
import { canonicalJson, type Json, sha256Hex } from "../domain/hash.ts";
import { normalizeText } from "../domain/text.ts";
import { getAccount } from "../services/accounts.ts";
import { auditStatement, reviewStatement } from "../services/audit.ts";
import {
  type AppliedCorrection,
  applyBookingDateCorrections,
  type BookingDateCorrection,
} from "./corrections.ts";
import { getParser } from "./parsers/registry.ts";
import { type ExistingTxn, type ImportPlan, type IncomingTxn, planImport } from "./plan.ts";
import type {
  ExcludedRow,
  ParsedBalance,
  ParsedTransaction,
  ParseIssue,
  ParseNotice,
  StatementParser,
} from "./types.ts";

export interface ImportRequest {
  accountId: string;
  parserId: string;
  fileName: string | null;
  content: Uint8Array;
  /** parser esplicito (es. creato da una specifica nei test); se assente si usa il registro per `parserId` */
  parser?: StatementParser;
  /** correzioni dichiarate della data contabile (vedi corrections.ts); rifiuto se non si applicano esattamente */
  corrections?: readonly BookingDateCorrection[];
}

export interface ImportCounts {
  /** righe di dati lette (transazioni, saldi, escluse) */
  rows: number;
  /** transazioni derivate dalle righe (una riga puo' produrne due, es. commissione separata) */
  transactions: number;
  excluded: number;
  created: number;
  unchanged: number;
  updated: number;
  conflicts: number;
  balancesCreated: number;
  balancesUnchanged: number;
  reviews: number;
  links: number;
}

export interface ImportReport {
  runId: string;
  status: "committed" | "rejected" | "duplicate_file";
  parser: { id: string; version: number; verifiedAgainstRealExports: boolean };
  fileSha256: string;
  counts: ImportCounts;
  issues: ParseIssue[];
  notices: ParseNotice[];
  duplicateOf: string | null;
  /** correzioni applicate (vuoto se nessuna) */
  corrections: AppliedCorrection[];
}

const MAX_ISSUES_STORED = 200;
/** Limite di D1 sui parametri per query: le liste IN (...) si spezzano. */
const IN_CHUNK = 90;

function emptyCounts(): ImportCounts {
  return {
    rows: 0,
    transactions: 0,
    excluded: 0,
    created: 0,
    unchanged: 0,
    updated: 0,
    conflicts: 0,
    balancesCreated: 0,
    balancesUnchanged: 0,
    reviews: 0,
    links: 0,
  };
}

export async function importFile(ctx: Ctx, req: ImportRequest): Promise<ImportReport> {
  const account = await getAccount(ctx, req.accountId);
  const parser = req.parser ?? getParser(req.parserId);
  if (req.parser !== undefined && req.parser.id !== req.parserId) {
    throw new Error(`parser esplicito (${req.parser.id}) diverso da parserId (${req.parserId})`);
  }
  const parserInfo = {
    id: parser.id,
    version: parser.version,
    verifiedAgainstRealExports: parser.verifiedAgainstRealExports,
  };
  const fileSha256 = await sha256Hex(req.content);
  const startedAt = ctx.now();
  const runId = ctx.newId();
  const sourceId = `file:${parser.id}`;
  const seq = await readWriteSeq(ctx.db);

  let notices: ParseNotice[] = [];
  let applied: AppliedCorrection[] = [];
  const report = (
    status: ImportReport["status"],
    counts: ImportCounts,
    issues: ParseIssue[],
    duplicateOf: string | null,
  ) =>
    ({
      runId,
      status,
      parser: parserInfo,
      fileSha256,
      counts,
      issues,
      notices,
      duplicateOf,
      corrections: applied,
    }) satisfies ImportReport;

  const runStatement = (
    status: ImportReport["status"],
    counts: ImportCounts,
    issues: ParseIssue[],
    extra: Json = null,
  ) =>
    stmt(
      `INSERT INTO import_runs (id, source_id, account_id, parser_id, parser_version, file_name, file_sha256, status,
         started_at, finished_at, counts_json, errors_json)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      runId,
      sourceId,
      account.id,
      parser.id,
      parser.version,
      req.fileName,
      fileSha256,
      status,
      startedAt,
      ctx.now(),
      canonicalJson({ ...counts, extra, notices: notices as unknown as Json }),
      JSON.stringify(issues.slice(0, MAX_ISSUES_STORED)),
    );
  const sourceStatement = stmt(
    "INSERT OR IGNORE INTO sources (id, kind, name, created_at) VALUES (?, 'file', ?, ?)",
    sourceId,
    parser.description,
    startedAt,
  );
  const recordRejected = async (issues: ParseIssue[], at: number = seq): Promise<ImportReport> => {
    await commitGuarded(ctx.db, at, [sourceStatement, runStatement("rejected", emptyCounts(), issues)]);
    return report("rejected", emptyCounts(), issues, null);
  };

  const previous = await ctx.db.first<{ id: string }>(
    `SELECT id FROM import_runs WHERE account_id = ? AND parser_id = ? AND file_sha256 = ? AND status = 'committed'
     ORDER BY started_at LIMIT 1`,
    [account.id, parser.id, fileSha256],
  );
  if (previous !== null) {
    await commitGuarded(ctx.db, seq, [
      sourceStatement,
      runStatement("duplicate_file", emptyCounts(), [], { duplicate_of: previous.id }),
    ]);
    return report("duplicate_file", emptyCounts(), [], previous.id);
  }

  const parsed = await parser.parseFile(req.content, { accountCurrency: account.currency });
  notices = [...(parsed.notices ?? [])];
  if (parsed.issues.length > 0) return recordRejected([...parsed.issues]);
  const corrected = applyBookingDateCorrections(parsed.transactions, req.corrections ?? []);
  if (corrected.issues.length > 0) return recordRejected(corrected.issues);
  applied = corrected.applied;
  const parsedTxns = corrected.transactions;
  const excluded = parsed.excluded ?? [];

  const counts = emptyCounts();
  counts.rows = new Set([...parsedTxns, ...parsed.balances, ...excluded].map((r) => r.line)).size;
  counts.transactions = parsedTxns.length;
  counts.excluded = excluded.length;
  const statements: Statement[] = [sourceStatement];
  const at = ctx.now();

  // Le righe dell'esecuzione vanno PRIMA dei record grezzi (chiave esterna), con i conteggi finali: si aggiunge in testa
  // alla fine della pianificazione. Una riga del file = un record grezzo, anche se produce piu' transazioni.
  const rawIds = new Map<number, string>();
  const rawStatements: Statement[] = [];
  for (const row of [...parsedTxns, ...parsed.balances]) {
    if (rawIds.has(row.line)) continue;
    const rawId = ctx.newId();
    rawIds.set(row.line, rawId);
    const rawJson = canonicalJson(row.raw as Json);
    rawStatements.push(
      stmt(
        `INSERT INTO raw_records (id, import_run_id, line_number, record_type, content_sha256, raw_json)
         VALUES (?, ?, ?, ?, ?, ?)`,
        rawId,
        runId,
        row.line,
        "amountMinor" in row ? "transaction" : "balance",
        await sha256Hex(rawJson),
        rawJson,
      ),
    );
  }

  const txnStatements = await planTransactions(ctx, account.id, runId, at, parsedTxns, rawIds, counts);
  const balanceStatements = await planBalances(
    ctx,
    account.id,
    sourceId,
    runId,
    at,
    parsed.balances,
    rawIds,
    counts,
  );

  const excludedStatements = await planExcluded(ctx, account.id, runId, at, excluded, counts);

  statements.push(
    runStatement("committed", counts, []),
    ...rawStatements,
    ...txnStatements,
    ...balanceStatements,
    ...excludedStatements,
    auditStatement({
      at,
      actor: "import",
      action: "import_committed",
      entity: "import_run",
      entityId: runId,
      after: { file: req.fileName, sha256: fileSha256, counts, corrections: applied as unknown as Json },
      importRunId: runId,
    }),
  );

  try {
    await commitGuarded(ctx.db, seq, statements);
  } catch (err) {
    if (err instanceof ConflictError) throw err;
    const message = err instanceof Error ? err.message : String(err);
    return recordRejected([{ line: null, field: null, code: "write_failed", message }]);
  }
  return report("committed", counts, [], null);
}

/**
 * Righe escluse per regola dichiarata: conservate integralmente. Se una riga esclusa ha l'id di una transazione
 * ancora in attesa (es. pagamento poi annullato), la transazione NON viene toccata: si apre una verifica.
 */
async function planExcluded(
  ctx: Ctx,
  accountId: string,
  runId: string,
  at: string,
  rows: readonly ExcludedRow[],
  counts: ImportCounts,
): Promise<Statement[]> {
  const out: Statement[] = [];
  for (const r of rows) {
    const rawJson = canonicalJson(r.raw as Json);
    out.push(
      stmt(
        `INSERT INTO excluded_rows (id, import_run_id, line_number, reason, source_txn_id, content_sha256, raw_json)
         VALUES (?, ?, ?, ?, ?, ?, ?)`,
        ctx.newId(),
        runId,
        r.line,
        r.reason,
        r.sourceTxnId,
        await sha256Hex(rawJson),
        rawJson,
      ),
    );
  }
  const ids = [...new Set(rows.flatMap((r) => (r.sourceTxnId === null ? [] : [r.sourceTxnId])))];
  for (let i = 0; i < ids.length; i += IN_CHUNK) {
    const chunk = ids.slice(i, i + IN_CHUNK);
    const hits = await ctx.db.all<{ id: string; source_txn_id: string; status: string }>(
      `SELECT id, source_txn_id, status FROM transactions WHERE account_id = ? AND source_txn_id IN (${chunk.map(() => "?").join(",")})`,
      [accountId, ...chunk],
    );
    for (const h of hits) {
      const reason = rows.find((r) => r.sourceTxnId === h.source_txn_id)?.reason ?? "";
      counts.reviews += 1;
      out.push(
        reviewStatement({
          id: ctx.newId(),
          kind: h.status === "pending" ? "pending_excluded" : "booked_excluded",
          entity: "transaction",
          entityId: h.id,
          detail: { motivo_esclusione: reason, import_run_id: runId },
          createdAt: at,
        }),
      );
    }
  }
  return out;
}

async function toIncoming(accountId: string, rows: readonly ParsedTransaction[]): Promise<IncomingTxn[]> {
  const occurrences = new Map<string, number>();
  const out: IncomingTxn[] = [];
  for (const r of rows) {
    const descriptionNorm = normalizeText(r.description);
    const counterpartyNorm = r.counterparty === null ? null : normalizeText(r.counterparty);
    const fingerprint = await sha256Hex(
      canonicalJson([
        accountId,
        r.transactionDate,
        r.amountMinor,
        r.currency,
        descriptionNorm,
        counterpartyNorm,
        r.originalAmountMinor,
        r.originalCurrency,
      ]),
    );
    let occurrence: number | null = null;
    if (r.sourceTxnId === null) {
      occurrence = (occurrences.get(fingerprint) ?? 0) + 1;
      occurrences.set(fingerprint, occurrence);
    }
    out.push({ ...r, fingerprint, occurrence, descriptionNorm, counterpartyNorm });
  }
  return out;
}

async function loadExisting(
  ctx: Ctx,
  accountId: string,
  incoming: readonly IncomingTxn[],
): Promise<ExistingTxn[]> {
  if (incoming.length === 0) return [];
  const dates = incoming.map((t) => t.transactionDate).sort();
  const from = dates[0] ?? "";
  const to = dates[dates.length - 1] ?? "";
  const cols = `id, source_txn_id, fingerprint, occurrence, status, transaction_date, booking_date, amount_minor, currency,
    original_amount_minor, original_currency, description, counterparty, source_type`;
  const rows = await ctx.db.all<ExistingTxn>(
    `SELECT ${cols} FROM transactions WHERE account_id = ? AND transaction_date BETWEEN date(?, '-10 days') AND date(?, '+10 days')`,
    [accountId, from, to],
  );
  const byId = new Map(rows.map((r) => [r.id, r]));
  const ids = [...new Set(incoming.flatMap((t) => (t.sourceTxnId === null ? [] : [t.sourceTxnId])))];
  for (let i = 0; i < ids.length; i += IN_CHUNK) {
    const chunk = ids.slice(i, i + IN_CHUNK);
    const more = await ctx.db.all<ExistingTxn>(
      `SELECT ${cols} FROM transactions WHERE account_id = ? AND source_txn_id IN (${chunk.map(() => "?").join(",")})`,
      [accountId, ...chunk],
    );
    for (const r of more) byId.set(r.id, r);
  }
  return [...byId.values()];
}

async function planTransactions(
  ctx: Ctx,
  accountId: string,
  runId: string,
  at: string,
  rows: readonly ParsedTransaction[],
  rawIds: ReadonlyMap<number, string>,
  counts: ImportCounts,
): Promise<Statement[]> {
  const incoming = await toIncoming(accountId, rows);
  const existing = await loadExisting(ctx, accountId, incoming);
  const plan: ImportPlan = planImport(incoming, existing, () => ctx.newId());
  const out: Statement[] = [];
  const observe = (txnId: string, line: number, outcome: string) =>
    stmt(
      "INSERT INTO transaction_observations (transaction_id, import_run_id, raw_record_id, outcome) VALUES (?, ?, ?, ?)",
      txnId,
      runId,
      rawIds.get(line) ?? "",
      outcome,
    );

  for (const a of plan.actions) {
    const t = a.incoming;
    switch (a.type) {
      case "create": {
        counts.created += 1;
        out.push(
          stmt(
            `INSERT INTO transactions (id, account_id, origin, source_txn_id, fingerprint, occurrence, status,
               transaction_date, booking_date, amount_minor, currency, original_amount_minor, original_currency,
               description, counterparty, description_norm, counterparty_norm, first_import_run_id, last_import_run_id,
               created_at, updated_at, source_type)
             VALUES (?, ?, 'import', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
            a.id,
            accountId,
            t.sourceTxnId,
            t.fingerprint,
            t.occurrence,
            t.status,
            t.transactionDate,
            t.bookingDate,
            t.amountMinor,
            t.currency,
            t.originalAmountMinor,
            t.originalCurrency,
            t.description,
            t.counterparty,
            t.descriptionNorm,
            t.counterpartyNorm,
            runId,
            runId,
            at,
            at,
            t.sourceType ?? null,
          ),
          observe(a.id, t.line, "created"),
          auditStatement({
            at,
            actor: "import",
            action: "create",
            entity: "transaction",
            entityId: a.id,
            after: {
              status: t.status,
              date: t.transactionDate,
              amount_minor: t.amountMinor,
              currency: t.currency,
            },
            importRunId: runId,
          }),
        );
        break;
      }
      case "unchanged":
        counts.unchanged += 1;
        out.push(observe(a.existing.id, t.line, "unchanged"));
        break;
      case "update": {
        counts.updated += 1;
        out.push(
          stmt(
            `UPDATE transactions SET status = ?, transaction_date = ?, booking_date = ?, amount_minor = ?,
               original_amount_minor = ?, original_currency = ?, description = ?, counterparty = ?,
               description_norm = ?, counterparty_norm = ?, fingerprint = ?, last_import_run_id = ?, updated_at = ?,
               source_type = ?
             WHERE id = ?`,
            t.status,
            t.transactionDate,
            t.bookingDate,
            t.amountMinor,
            t.originalAmountMinor,
            t.originalCurrency,
            t.description,
            t.counterparty,
            t.descriptionNorm,
            t.counterpartyNorm,
            // per le righe senza id l'impronta e' la chiave: resta quella esistente
            t.sourceTxnId === null ? a.existing.fingerprint : t.fingerprint,
            runId,
            at,
            t.sourceType ?? null,
            a.existing.id,
          ),
          observe(a.existing.id, t.line, "updated"),
          auditStatement({
            at,
            actor: "import",
            action: a.existing.status === "pending" && t.status === "booked" ? "book" : "update_pending",
            entity: "transaction",
            entityId: a.existing.id,
            before: Object.fromEntries(Object.entries(a.changes).map(([k, v]) => [k, v.from])),
            after: Object.fromEntries(Object.entries(a.changes).map(([k, v]) => [k, v.to])),
            importRunId: runId,
          }),
        );
        break;
      }
      case "conflict":
        counts.conflicts += 1;
        out.push(observe(a.existing.id, t.line, "conflict"));
        break;
    }
  }

  for (const link of plan.links) {
    counts.links += 1;
    out.push(
      stmt(
        `INSERT OR IGNORE INTO transaction_links (id, type, from_txn_id, to_txn_id, status, origin, created_at)
         VALUES (?, ?, ?, ?, 'proposed', 'detector', ?)`,
        ctx.newId(),
        link.type,
        link.fromTxnId,
        link.toTxnId,
        at,
      ),
    );
  }
  for (const r of plan.reviews) {
    counts.reviews += 1;
    out.push(
      reviewStatement({
        id: ctx.newId(),
        kind: r.kind,
        entity: "transaction",
        entityId: r.entityId,
        detail: { ...r.detail, import_run_id: runId },
        createdAt: at,
      }),
    );
  }
  return out;
}

async function planBalances(
  ctx: Ctx,
  accountId: string,
  sourceId: string,
  runId: string,
  at: string,
  rows: readonly ParsedBalance[],
  rawIds: ReadonlyMap<number, string>,
  counts: ImportCounts,
): Promise<Statement[]> {
  const out: Statement[] = [];
  for (const b of rows) {
    const existing = await ctx.db.all<{ balance_minor: number; source_id: string }>(
      "SELECT balance_minor, source_id FROM balance_observations WHERE account_id = ? AND as_of_date = ? AND kind = 'end_of_day'",
      [accountId, b.asOfDate],
    );
    if (existing.some((e) => e.source_id === sourceId && e.balance_minor === b.balanceMinor)) {
      counts.balancesUnchanged += 1;
      continue;
    }
    const id = ctx.newId();
    counts.balancesCreated += 1;
    out.push(
      stmt(
        `INSERT INTO balance_observations (id, account_id, as_of_date, balance_minor, currency, kind, source_id,
           import_run_id, raw_record_id, created_at)
         VALUES (?, ?, ?, ?, ?, 'end_of_day', ?, ?, ?, ?)`,
        id,
        accountId,
        b.asOfDate,
        b.balanceMinor,
        b.currency,
        sourceId,
        runId,
        rawIds.get(b.line) ?? null,
        at,
      ),
    );
    const different = existing.filter((e) => e.balance_minor !== b.balanceMinor);
    if (different.length > 0) {
      counts.reviews += 1;
      out.push(
        reviewStatement({
          id: ctx.newId(),
          kind: "balance_conflict",
          entity: "account_date",
          entityId: `${accountId}@${b.asOfDate}`,
          detail: {
            nuovo: b.balanceMinor,
            esistenti: different.map((e) => e.balance_minor),
            import_run_id: runId,
          },
          createdAt: at,
        }),
      );
    }
  }
  return out;
}
