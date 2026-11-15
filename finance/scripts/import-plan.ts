#!/usr/bin/env node
/**
 * PROVA (dry-run) di un piano di importazione su un database TEMPORANEO, eliminato alla fine.
 *
 *   node scripts/import-plan.ts --plan <piano.json> [--keep]
 *
 * Il piano (file locale, MAI nel repository o in iCloud se contiene percorsi o saldi reali):
 *   { "accounts": [{ "id", "name", "type", "currency", "holderRole" }],
 *     "imports":  [{ "account", "format", "file", "corrections"? }],    // in ordine; corrections = file JSON
 *     "balances": [{ "account", "date", "balanceMinor", "note" }],      // saldi osservati da riepiloghi degli estratti
 *     "expect":   { "<conto>": { "closingDate", "closingBalanceMinor", "transactions" } },
 *     "expectRows": [{ "account", "transactionDate", "bookingDate", "amountMinor" }] }   // ognuna = una sola riga
 *
 * Controlli (uno solo fallito = esito BLOCCATO, codice di uscita 1):
 *   ROLLBACK: ogni importazione eseguita prima con un errore di scrittura forzato all'ultima istruzione deve lasciare
 *   i dati invariati; un backup (VACUUM INTO) preso prima delle importazioni deve restituire esattamente lo stato
 *   logico di partenza (impronta sha256 di tutte le tabelle);
 *   ogni file importato senza errori; reimportazione di ogni file = duplicate_file (idempotenza); nessun id della
 *   fonte ripetuto nello stesso conto; saldo finale, numero di movimenti e righe attese; ogni periodo tra saldi
 *   osservati riconciliato (ok); nessun saldo in conflitto; riepilogo della consultazione coerente.
 * Uscita: SOLO conteggi, somme, date e codici (nessuna descrizione, controparte o riga grezza).
 * Il database definitivo non viene mai toccato da questo script.
 */
import { createHash } from "node:crypto";
import {
  closeSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  openSync,
  readFileSync,
  readSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { parseArgs } from "node:util";
import { createCtx } from "../src/context.ts";
import { migrate } from "../src/db/migrate.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import { NodeSqliteDb } from "../src/db/node-sqlite.ts";
import { NOT_DERIVED } from "../src/db/schema-check.ts";
import { type Db, type Statement, stmt } from "../src/db/types.ts";
import type { BookingDateCorrection } from "../src/import/corrections.ts";
import { importFile } from "../src/import/importer.ts";
import { runQuery } from "../src/query/operations.ts";
import {
  type AccountProfile,
  type AccountType,
  type ClosureDateStatus,
  closeAccount,
  createAccount,
  createInstitution,
  type HolderRole,
  listAccountStatuses,
  setAccountProfile,
} from "../src/services/accounts.ts";
import { addManualBalance } from "../src/services/balances.ts";
import { classifyTransactions, publishRuleSet } from "../src/services/classification.ts";
import { decideLink, detectLinks, listLinks } from "../src/services/links.ts";
import { reconcileObservedPeriods } from "../src/services/reconciliation.ts";

interface Plan {
  institutions?: { id: string; name: string }[];
  accounts: {
    id: string;
    name: string;
    type: AccountType;
    currency: string;
    holderRole: HolderRole;
    institutionId?: string;
  }[];
  /** dichiarazioni dell'utente (mai dedotte dagli estratti) */
  profiles?: (Omit<AccountProfile, "accountId" | "declaredAt" | "note"> & {
    account: string;
    note?: string;
  })[];
  /** chiusure (ammesse solo con saldo osservato zero alla data), dopo importazioni e saldi */
  closures?: {
    account: string;
    effectiveDate: string | null;
    dateStatus: ClosureDateStatus;
    reason: string;
  }[];
  /** file di regole (cloud-finance-rules/1), pubblicato e applicato dopo le importazioni */
  rules?: string;
  /**
   * quali proposte di trasferimento confermare, con lo STESSO effetto nella prova, in --commit e in --apply: "none"
   * (predefinito), "same_source_id" (solo gambe con lo stesso id della fonte), "all_unique" (anche le proposte univoche
   * per importo e data). In ogni caso solo tra conti dello STESSO istituto dichiarato: tra istituti diversi o con
   * istituto non dichiarato la coppia resta una proposta (decisione a mano). Le ambigue non sono proposte: restano aperte.
   */
  confirmTransfers?: "none" | "same_source_id" | "all_unique";
  imports: { account: string; format: string; file: string; corrections?: string }[];
  balances?: { account: string; date: string; balanceMinor: number; note?: string }[];
  expect?: Record<string, { closingDate?: string; closingBalanceMinor?: number; transactions?: number }>;
  expectRows?: { account: string; transactionDate: string; bookingDate: string; amountMinor: number }[];
}

const DATA_TABLES = [
  "transactions",
  "raw_records",
  "transaction_observations",
  "balance_observations",
  "excluded_rows",
  "review_items",
  "transaction_links",
];

/** Impronta logica del database: sha256 del contenuto di tutte le tabelle (ordinato), esclusi i metadati SQLite. */
export async function logicalChecksum(db: Db): Promise<string> {
  const tables = await db.all<{ name: string }>(
    `SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND ${NOT_DERIVED} ORDER BY name`,
  );
  const parts: string[] = [];
  for (const t of tables) {
    const rows = await db.all<Record<string, unknown>>(`SELECT * FROM "${t.name}" ORDER BY rowid`);
    parts.push(`${t.name}:${JSON.stringify(rows.map((r) => ({ ...r })))}`);
  }
  return createHash("sha256").update(parts.join("\n")).digest("hex");
}

async function dataCounts(db: Db): Promise<Record<string, number>> {
  const out: Record<string, number> = {};
  for (const t of DATA_TABLES)
    out[t] = (await db.first<{ n: number }>(`SELECT COUNT(*) AS n FROM ${t}`))?.n ?? 0;
  return out;
}

/** Db che sostituisce l'ULTIMA istruzione del primo batch con una che viola un vincolo (rollback forzato). */
function failingLast(inner: Db): Db {
  let armed = true;
  return {
    all: (sql, params) => inner.all(sql, params),
    first: (sql, params) => inner.first(sql, params),
    batch: (statements: readonly Statement[]) => {
      if (!armed || statements.length < 2) return inner.batch(statements);
      armed = false;
      return inner.batch([...statements.slice(0, -1), stmt("INSERT INTO accounts (id) VALUES (NULL)")]);
    },
  };
}

interface RunOptions {
  dbPath: string;
  /** cartella di lavoro per il backup di prova (solo con le sonde) */
  workDir: string;
  /** sonde di prova (errore forzato, reimportazione, backup di prova): SOLO nel dry-run, mai nel database definitivo */
  probes: boolean;
}

/** Esegue il piano su `dbPath` e lo verifica. Restituisce rapporto (solo aggregati) e controlli falliti. */
async function runPlan(
  plan: Plan,
  fromPlan: (p: string) => string,
  opts: RunOptions,
): Promise<{ report: Record<string, unknown>; failures: string[] }> {
  const db = new NodeSqliteDb(opts.dbPath);
  const ctx = createCtx(db);
  const failures: string[] = [];
  const report: Record<string, unknown> = {};
  try {
    await migrate(db, MIGRATIONS, ctx.now);
    for (const i of plan.institutions ?? []) await createInstitution(ctx, i.id, i.name);
    for (const a of plan.accounts) await createAccount(ctx, a);
    for (const p of plan.profiles ?? [])
      await setAccountProfile(ctx, p.account, {
        formalHolder: p.formalHolder,
        primaryUser: p.primaryUser,
        supervision: p.supervision,
        note: p.note ?? null,
      });

    // punto di ripristino: backup fisico + impronta logica prima di qualsiasi importazione
    const backupPath = join(opts.workDir, "backup-prima-delle-importazioni.sqlite3");
    let checksumBefore: string | null = null;
    if (opts.probes) {
      await db.all(`VACUUM INTO '${backupPath.replaceAll("'", "''")}'`);
      checksumBefore = await logicalChecksum(db);
    }

    const imports: unknown[] = [];
    for (const i of plan.imports) {
      const content = new Uint8Array(readFileSync(fromPlan(i.file)));
      const corrections =
        i.corrections === undefined
          ? []
          : (JSON.parse(readFileSync(fromPlan(i.corrections), "utf8")) as BookingDateCorrection[]);
      const request = {
        accountId: i.account,
        parserId: i.format,
        fileName: basename(i.file),
        content,
        corrections,
      };

      // rollback atomico: errore forzato all'ultima istruzione -> nessun dato scritto (solo nella prova: nel database
      // definitivo lascerebbe esecuzioni rifiutate fittizie nello storico)
      let rollbackOk: boolean | null = null;
      // file gia' importato (stesso conto, formato e contenuto): nessun dato da scrivere, nessuna sonda da provare
      const fileSha = createHash("sha256").update(content).digest("hex");
      const already = await db.first<{ n: number }>(
        "SELECT COUNT(*) AS n FROM import_runs WHERE account_id = ? AND parser_id = ? AND file_sha256 = ? AND status = 'committed'",
        [i.account, i.format, fileSha],
      );
      const alreadyImported = (already?.n ?? 0) > 0;
      if (opts.probes && !alreadyImported) {
        const before = await dataCounts(db);
        // file rifiutato dall'analisi: l'errore forzato cade sulla registrazione del rifiuto e risale come eccezione;
        // il blocco e' comunque atomico, quindi basta che nessun dato sia stato scritto (il rifiuto vero segue sotto)
        const forced = await importFile({ ...ctx, db: failingLast(db) }, request).catch(() => null);
        const after = await dataCounts(db);
        rollbackOk =
          (forced === null ||
            (forced.status === "rejected" && forced.issues.some((x) => x.code === "write_failed"))) &&
          JSON.stringify(before) === JSON.stringify(after);
        if (!rollbackOk)
          failures.push(
            `${i.account}/${i.format}: rollback non verificato (${forced?.status ?? "eccezione"})`,
          );
      }

      const r = await importFile(ctx, request);
      const codes: Record<string, number> = {};
      for (const x of r.issues) codes[x.code] = (codes[x.code] ?? 0) + 1;
      if (r.status !== "committed" && r.status !== "duplicate_file")
        failures.push(`${i.account}/${i.format}: importazione ${r.status}`);
      const again = opts.probes ? await importFile(ctx, request) : null;
      if (again !== null && again.status !== "duplicate_file")
        failures.push(`${i.account}/${i.format}: reimportazione ${again.status}`);
      imports.push({
        account: i.account,
        format: i.format,
        status: r.status,
        counts: r.counts,
        issues: codes,
        corrections: r.corrections.map((c) => `${c.from} -> ${c.to}`),
        forcedWriteError: alreadyImported
          ? "file gia' importato: nessuna scrittura"
          : rollbackOk === null
            ? "verificato nella prova"
            : rollbackOk
              ? "rollback completo (nessun dato scritto)"
              : "NON VERIFICATO",
        reimport: again === null ? "verificata nella prova" : again.status,
      });
    }
    report.imports = imports;

    // il backup restituisce lo stato di partenza
    const restored = opts.probes ? new NodeSqliteDb(backupPath, { readOnly: true }) : null;
    if (restored !== null)
      try {
        const checksumBackup = await logicalChecksum(restored);
        const ok = checksumBackup === checksumBefore;
        if (!ok) failures.push("backup: lo stato ripristinato non coincide con quello di partenza");
        report.restorePoint = {
          backup: "VACUUM INTO prima delle importazioni",
          sameAsBeforeImports: ok,
          changedByImports: (await logicalChecksum(db)) !== checksumBefore,
        };
      } finally {
        restored.close();
      }
    else report.restorePoint = "verificato nella prova";

    // anti-doppione: nessun id della fonte ripetuto nello stesso conto
    const repeatedIds = await db.first<{ n: number }>(
      `SELECT COUNT(*) AS n FROM (SELECT account_id, source_txn_id FROM transactions WHERE source_txn_id IS NOT NULL
         GROUP BY 1, 2 HAVING COUNT(*) > 1)`,
    );
    if ((repeatedIds?.n ?? 0) > 0)
      failures.push(`${repeatedIds?.n} id della fonte ripetuti nello stesso conto`);
    for (const e of plan.expectRows ?? []) {
      const n = await db.first<{ n: number }>(
        "SELECT COUNT(*) AS n FROM transactions WHERE account_id = ? AND transaction_date = ? AND booking_date = ? AND amount_minor = ?",
        [e.account, e.transactionDate, e.bookingDate, e.amountMinor],
      );
      if (n?.n !== 1)
        failures.push(
          `${e.account}: riga attesa ${e.transactionDate}/${e.bookingDate}/${e.amountMinor} trovata ${n?.n} volte`,
        );
    }
    report.antiDuplicate = {
      repeatedSourceIdsSameAccount: repeatedIds?.n ?? 0,
      expectedRowsChecked: plan.expectRows?.length ?? 0,
    };

    for (const b of plan.balances ?? [])
      await addManualBalance(ctx, b.account, b.date, b.balanceMinor, b.note ?? null);
    for (const c of plan.closures ?? [])
      await closeAccount(ctx, c.account, {
        effectiveDate: c.effectiveDate,
        dateStatus: c.dateStatus,
        reason: c.reason,
      });

    const accounts: Record<string, unknown> = {};
    // verifiche su TUTTI i conti del database (anche quelli non citati da un piano incrementale)
    const allAccounts = await db.all<{ id: string }>("SELECT id FROM accounts ORDER BY id");
    for (const a of allAccounts) {
      const t = await db.first<{
        n: number;
        inflow: number | null;
        outflow: number | null;
        first: string | null;
        last: string | null;
        pending: number | null;
      }>(
        `SELECT COUNT(*) AS n, SUM(CASE WHEN amount_minor > 0 THEN amount_minor END) AS inflow,
                -SUM(CASE WHEN amount_minor < 0 THEN amount_minor END) AS outflow,
                MIN(booking_date) AS first, MAX(booking_date) AS last, SUM(status = 'pending') AS pending
         FROM transactions WHERE account_id = ?`,
        [a.id],
      );
      const firstBal = await db.first<{ d: string; v: number }>(
        "SELECT as_of_date AS d, balance_minor AS v FROM balance_observations WHERE account_id = ? ORDER BY as_of_date LIMIT 1",
        [a.id],
      );
      const upToFirst = firstBal
        ? await db.first<{ s: number | null }>(
            "SELECT SUM(amount_minor) AS s FROM transactions WHERE account_id = ? AND status = 'booked' AND booking_date <= ?",
            [a.id, firstBal.d],
          )
        : null;
      const last = await db.first<{ d: string; v: number }>(
        "SELECT as_of_date AS d, balance_minor AS v FROM balance_observations WHERE account_id = ? ORDER BY as_of_date DESC LIMIT 1",
        [a.id],
      );
      const conflicts = await db.first<{ n: number }>(
        `SELECT COUNT(*) AS n FROM (SELECT as_of_date FROM balance_observations WHERE account_id = ?
           GROUP BY as_of_date HAVING COUNT(DISTINCT balance_minor) > 1)`,
        [a.id],
      );
      const rec = await reconcileObservedPeriods(ctx, a.id, { recordIfUnchanged: false });
      const byStatus: Record<string, number> = {};
      for (const r of rec) byStatus[r.status] = (byStatus[r.status] ?? 0) + 1;
      const notOk = rec
        .filter((r) => r.status !== "ok")
        .map((r) => ({ from: r.fromDate, to: r.toDate, status: r.status, difference: r.difference }));
      if (notOk.length > 0) failures.push(`${a.id}: ${notOk.length} periodi non riconciliati`);
      if ((conflicts?.n ?? 0) > 0) failures.push(`${a.id}: saldi in conflitto`);
      const exp = plan.expect?.[a.id];
      if (exp?.transactions !== undefined && exp.transactions !== t?.n)
        failures.push(`${a.id}: movimenti ${t?.n} invece di ${exp.transactions}`);
      if (
        exp?.closingBalanceMinor !== undefined &&
        (last?.v !== exp.closingBalanceMinor || last?.d !== exp.closingDate)
      )
        failures.push(
          `${a.id}: saldo finale ${last?.d}=${last?.v} invece di ${exp.closingDate}=${exp.closingBalanceMinor}`,
        );
      accounts[a.id] = {
        transactions: t?.n ?? 0,
        pending: t?.pending ?? 0,
        inflowMinor: t?.inflow ?? 0,
        outflowMinor: t?.outflow ?? 0,
        firstBooking: t?.first,
        lastBooking: t?.last,
        firstObservedBalance: firstBal,
        // saldo prima del primo movimento: DEDOTTO (primo saldo osservato - movimenti fino a quel giorno), non osservato
        openingDeduced: firstBal === null ? null : firstBal.v - (upToFirst?.s ?? 0),
        lastObservedBalance: last,
        balanceConflicts: conflicts?.n ?? 0,
        reconciliations: byStatus,
        notReconciled: notOk.slice(0, 10),
      };
    }
    report.accounts = accounts;

    if (plan.rules !== undefined) {
      const published = await publishRuleSet(ctx, JSON.parse(readFileSync(fromPlan(plan.rules), "utf8")));
      const classified = await classifyTransactions(ctx);
      const byRule = await db.all<{ rule: string; status: string; kind: string; n: number }>(
        `SELECT COALESCE(rule_id, '(nessuna regola)') AS rule, status, kind, COUNT(*) AS n FROM current_classifications
         GROUP BY 1, 2, 3 ORDER BY 1, 2, 3`,
      );
      report.classification = {
        ruleSetVersion: published.version,
        counts: classified,
        byRule: byRule.map((r) => `${r.rule} -> ${r.kind} (${r.status}): ${r.n}`),
      };
    }

    const detect = await detectLinks(ctx);
    const policy = plan.confirmTransfers ?? "none";
    let confirmed = 0;
    // coppie che la politica confermerebbe ma che restano proposte per l'istituto (diverso o non dichiarato): importo e
    // data non dimostrano un trasferimento tra conti propri e un id della fonte vale solo dentro la stessa fonte
    const notConfirmedByInstitution = { differentInstitution: 0, unknownInstitution: 0 };
    if (policy !== "none") {
      const candidates = await db.all<{
        id: string;
        sameId: number;
        institution: "same" | "different" | "unknown";
      }>(
        `SELECT l.id, (a.source_txn_id IS NOT NULL AND a.source_txn_id = b.source_txn_id) AS sameId,
                CASE WHEN aa.institution_id IS NULL OR ab.institution_id IS NULL THEN 'unknown'
                     WHEN aa.institution_id = ab.institution_id THEN 'same' ELSE 'different' END AS institution
         FROM transaction_links l JOIN transactions a ON a.id = l.from_txn_id JOIN transactions b ON b.id = l.to_txn_id
         JOIN accounts aa ON aa.id = a.account_id JOIN accounts ab ON ab.id = b.account_id
         WHERE l.type = 'transfer' AND l.status = 'proposed' ORDER BY l.created_at, l.id`,
      );
      for (const c of candidates) {
        if (policy === "same_source_id" && !c.sameId) continue;
        if (c.institution !== "same") {
          if (c.institution === "different") notConfirmedByInstitution.differentInstitution += 1;
          else notConfirmedByInstitution.unknownInstitution += 1;
          continue;
        }
        await decideLink(ctx, c.id, "confirmed");
        confirmed += 1;
      }
    }
    report.transferPolicy = { policy, confirmed, notConfirmedByInstitution };
    const proposed = await listLinks(ctx, "proposed");
    // proposte di trasferimento per coppia di conti e per criterio (stesso id della fonte o solo importo e data)
    const byPair = await db.all<{ pair: string; sameId: number; n: number }>(
      `SELECT a.account_id || ' -> ' || b.account_id AS pair,
              (a.source_txn_id IS NOT NULL AND a.source_txn_id = b.source_txn_id) AS sameId, COUNT(*) AS n
       FROM transaction_links l JOIN transactions a ON a.id = l.from_txn_id JOIN transactions b ON b.id = l.to_txn_id
       WHERE l.type = 'transfer' AND l.status = 'proposed' GROUP BY 1, 2 ORDER BY 1, 2`,
    );
    const reviews = await db.all<{ kind: string; n: number }>(
      "SELECT kind, COUNT(*) AS n FROM review_items WHERE status = 'open' GROUP BY kind ORDER BY kind",
    );
    report.links = {
      detect,
      proposedByType: proposed.reduce<Record<string, number>>((m, l) => {
        m[l.type] = (m[l.type] ?? 0) + 1;
        return m;
      }, {}),
      transferProposalsByAccountPair: byPair.map(
        (r) => `${r.pair} [${r.sameId ? "stesso id" : "solo importo e data"}]: ${r.n}`,
      ),
      openReviewItems: Object.fromEntries(reviews.map((r) => [r.kind, r.n])),
    };

    const range = await db.first<{ from: string; to: string }>(
      "SELECT MIN(booking_date) AS 'from', MAX(booking_date) AS 'to' FROM transactions",
    );
    if (range?.from && range.to) {
      const s = await runQuery(db, "summary", { from: range.from, to: range.to });
      const st = await runQuery(db, "data_status", { asOf: range.to });
      if (!s.ok || !st.ok) failures.push("consultazione non riuscita");
      const totals = await db.first<{ n: number }>(
        "SELECT COUNT(*) AS n FROM transactions WHERE status = 'booked'",
      );
      const booked = s.ok
        ? (s.data as { currencies: { bookedCount: number }[]; excludedDuplicates: number }).currencies.reduce(
            (n, c) => n + c.bookedCount,
            0,
          ) + (s.data as { excludedDuplicates: number }).excludedDuplicates
        : -1;
      if (booked !== totals?.n)
        failures.push(`riepilogo: ${booked} movimenti contabilizzati invece di ${totals?.n}`);
      report.summary = s.ok
        ? (s.data as { currencies: Record<string, unknown>[] }).currencies.map((c) => ({
            currency: c.currency,
            bookedCount: c.bookedCount,
            unclassifiedCount: c.unclassifiedCount,
            income: c.income,
            expenses: c.expenses,
            transfersIn: c.transfersIn,
            transfersOut: c.transfersOut,
            unclassifiedIn: c.unclassifiedIn,
            unclassifiedOut: c.unclassifiedOut,
          }))
        : s;
      report.anomalies = st.ok
        ? (st.data as { anomalies: { code: string; accountId: string | null }[] }).anomalies.map(
            (a) => `${a.accountId}:${a.code}`,
          )
        : st;
      const ov = await runQuery(db, "overview", { from: range.from, to: range.to });
      if (!ov.ok) failures.push("panoramica non riuscita");
      report.overview = ov.ok ? ov.data : ov;

      // --- doppio conteggio dei trasferimenti
      const sOk = s.ok
        ? (s.data as { currencies: { currency: string; transfersIn: number; transfersOut: number }[] })
        : null;
      for (const c of sOk?.currencies ?? [])
        if (c.transfersIn !== c.transfersOut)
          failures.push(
            `${c.currency}: trasferimenti in entrata ${c.transfersIn} <> in uscita ${c.transfersOut} (doppio conteggio o gamba mancante)`,
          );
      const multi = await db.first<{ n: number }>(
        `SELECT COUNT(*) AS n FROM (SELECT id FROM (
           SELECT from_txn_id AS id FROM transaction_links WHERE type = 'transfer' AND status = 'confirmed'
           UNION ALL SELECT to_txn_id FROM transaction_links WHERE type = 'transfer' AND status = 'confirmed')
         GROUP BY id HAVING COUNT(*) > 1)`,
      );
      if ((multi?.n ?? 0) > 0) failures.push(`${multi?.n} movimenti in piu' di un trasferimento confermato`);
      const confirmedKinds = await db.all<{ sameId: number; crossInstitution: number; n: number }>(
        `SELECT (a.source_txn_id IS NOT NULL AND a.source_txn_id = b.source_txn_id) AS sameId,
                (COALESCE(aa.institution_id, '') <> COALESCE(ab.institution_id, '')) AS crossInstitution, COUNT(*) AS n
         FROM transaction_links l JOIN transactions a ON a.id = l.from_txn_id JOIN transactions b ON b.id = l.to_txn_id
         JOIN accounts aa ON aa.id = a.account_id JOIN accounts ab ON ab.id = b.account_id
         WHERE l.type = 'transfer' AND l.status = 'confirmed' GROUP BY 1, 2`,
      );
      if (policy === "same_source_id" && confirmedKinds.some((k) => !k.sameId))
        failures.push("politica same_source_id: confermato un trasferimento senza id condiviso");
      report.doubleCounting = {
        transfersInEqualsOut: sOk?.currencies.every((c) => c.transfersIn === c.transfersOut) ?? false,
        txnInMoreThanOneConfirmedTransfer: multi?.n ?? 0,
        confirmed: confirmedKinds.map(
          (k) =>
            `${k.sameId ? "stesso id" : "solo importo e data"}${k.crossInstitution ? ", tra istituti" : ""}: ${k.n}`,
        ),
      };

      // --- conti chiusi
      const statuses = await listAccountStatuses(ctx);
      const ovData = ov.ok
        ? (ov.data as {
            savings: { accountIds: string[] };
            lastUpdate: {
              lastMovementOn: string | null;
              byAccount: { accountId: string; closed: boolean; lastMovementOn: string | null }[];
            };
          })
        : null;
      const stData = st.ok ? (st.data as { anomalies: { accountId: string | null }[] }) : null;
      report.closedAccounts = (plan.closures ?? []).map((c) => {
        const x = statuses.find((y) => y.accountId === c.account);
        const inOverview = ovData?.lastUpdate.byAccount.find((b) => b.accountId === c.account);
        const checks = {
          closed: x?.closed === true,
          closureDateAsDeclared:
            (x?.closureDate ?? null) === c.effectiveDate && (x?.closureDateStatus ?? null) === c.dateStatus,
          lastMovementSeparate:
            x?.lastMovementOn !== undefined && (c.effectiveDate === null ? x.closureDate === null : true),
          notActiveInOverview:
            inOverview?.closed === true && !(ovData?.savings.accountIds ?? []).includes(c.account),
          noAnomalies: !(stData?.anomalies ?? []).some((a) => a.accountId === c.account),
        };
        for (const [k, v] of Object.entries(checks))
          if (!v) failures.push(`${c.account}: verifica conto chiuso fallita (${k})`);
        return {
          account: c.account,
          closureDate: x?.closureDate,
          closureDateStatus: x?.closureDateStatus,
          lastMovementOn: x?.lastMovementOn,
          ...checks,
        };
      });

      // --- data di aggiornamento
      const maxMovement = await db.first<{ d: string | null }>(
        "SELECT MAX(COALESCE(booking_date, transaction_date)) AS d FROM transactions",
      );
      if ((ovData?.lastUpdate.lastMovementOn ?? null) !== (maxMovement?.d ?? null))
        failures.push("panoramica: data di aggiornamento diversa dall'ultimo movimento");
      report.lastUpdate = ovData?.lastUpdate;
    }
    const integrity = await db.first<{ integrity_check: string }>("PRAGMA integrity_check");
    if (integrity?.integrity_check !== "ok") failures.push("integrity_check non ok");
  } finally {
    db.close();
  }
  return { report, failures };
}

/** Controlli comuni sul percorso: assoluto, .sqlite3, fuori da cartelle sincronizzate e dal repository. */
function pathProblems(target: string, repoRoot: string): string[] {
  const p: string[] = [];
  if (!isAbsolute(target)) p.push("percorso non assoluto");
  if (!target.endsWith(".sqlite3")) p.push("estensione attesa .sqlite3");
  const real = resolve(target);
  for (const forbidden of [
    "/Library/Mobile Documents/",
    "/Library/CloudStorage/",
    "/Desktop/",
    "/Documents/",
    "/Downloads/",
  ])
    if (real.includes(forbidden)) p.push(`cartella sincronizzata o non protetta: ${forbidden}`);
  if (real === repoRoot || real.startsWith(`${repoRoot}/`)) p.push("dentro il repository");
  const base = dirname(dirname(real));
  if (!existsSync(base)) p.push("cartella protetta inesistente");
  else if ((statSync(base).mode & 0o077) !== 0) p.push("cartella protetta con permessi aperti (attesi 700)");
  return p;
}

/** Controlli sul percorso del database definitivo da CREARE (nessuna scrittura). */
export function targetProblems(target: string, repoRoot: string): string[] {
  const p = pathProblems(target, repoRoot);
  const real = resolve(target);
  for (const f of [real, `${real}.in-corso`, `${real}-wal`, `${real}-shm`])
    if (existsSync(f)) p.push(`esiste gia': ${basename(f)} (questa procedura crea solo un database nuovo)`);
  return p;
}

/** Controlli sul database ESISTENTE da aggiornare con --apply (sola lettura). */
export async function applyTargetProblems(target: string, repoRoot: string, plan: Plan): Promise<string[]> {
  const p: string[] = [];
  if ((plan.institutions ?? []).length + plan.accounts.length + (plan.profiles ?? []).length > 0)
    p.push("il piano incrementale non deve creare istituti, conti o profili (gia' presenti)");
  if ((plan.closures ?? []).length > 0)
    p.push("il piano incrementale non deve chiudere conti (azione separata)");
  return existingTargetProblems(target, repoRoot, p);
}

/**
 * Controlli su un database ESISTENTE da aggiornare (sola lettura): percorso protetto, permessi, nessun aggiornamento
 * in corso o interrotto, integrita', schema alla versione attesa. Usati da --apply e dalla sincronizzazione bancaria.
 */
export async function existingTargetProblems(
  target: string,
  repoRoot: string,
  extra: readonly string[] = [],
): Promise<string[]> {
  const p = pathProblems(target, repoRoot);
  const real = resolve(target);
  if (!existsSync(real)) return [...p, "database da aggiornare inesistente"];
  if ((statSync(real).mode & 0o077) !== 0) p.push("database con permessi aperti (attesi 600)");
  if ((statSync(dirname(real)).mode & 0o077) !== 0)
    p.push("cartella del database con permessi aperti (attesi 700)");
  for (const f of [`${real}.in-corso`, `${real}-wal`, `${real}-shm`])
    if (existsSync(f)) p.push(`file presente: ${basename(f)} (database aperto o aggiornamento interrotto)`);
  // intestazione SQLite, byte 18-19 = 2: modalita' WAL; anche una sola lettura lascerebbe -wal/-shm accanto
  const header = Buffer.alloc(20);
  const fd = openSync(real, "r");
  try {
    readSync(fd, header, 0, 20, 0);
  } finally {
    closeSync(fd);
  }
  if (header[18] === 2 || header[19] === 2)
    p.push("database in modalita' WAL (attesa DELETE, come dopo --commit)");
  p.push(...extra);
  if (p.length > 0) return p;
  const db = new NodeSqliteDb(real, { readOnly: true });
  try {
    const integrity = await db.first<{ integrity_check: string }>("PRAGMA integrity_check");
    if (integrity?.integrity_check !== "ok") p.push("integrity_check non ok");
    const v = await db.first<{ v: number | null }>("SELECT MAX(version) AS v FROM schema_migrations");
    if (v?.v !== MIGRATIONS.length) p.push(`schema alla versione ${v?.v}, attesa ${MIGRATIONS.length}`);
  } finally {
    db.close();
  }
  return p;
}

/** Copia coerente di un database (sola lettura sull'originale). */
export async function snapshot(from: string, to: string): Promise<void> {
  const db = new NodeSqliteDb(from, { readOnly: true });
  try {
    await db.all(`VACUUM INTO '${to.replaceAll("'", "''")}'`);
  } finally {
    db.close();
  }
}

export async function checksumOf(path: string): Promise<string> {
  const db = new NodeSqliteDb(path, { readOnly: true });
  try {
    return await logicalChecksum(db);
  } finally {
    db.close();
  }
}

/**
 * Lo storico e' append-only: ogni movimento contabilizzato dell'originale deve esistere identico nell'aggiornato
 * (id, conto, importo, date); le righe delle tabelle di dati possono solo aumentare.
 */
export async function appendOnlyProblems(
  before: string,
  after: string,
): Promise<{ problems: string[]; delta: Record<string, number> }> {
  const a = new NodeSqliteDb(before, { readOnly: true });
  const b = new NodeSqliteDb(after, { readOnly: true });
  const problems: string[] = [];
  const delta: Record<string, number> = {};
  try {
    const ca = await dataCounts(a);
    const cb = await dataCounts(b);
    for (const t of DATA_TABLES) {
      delta[t] = (cb[t] ?? 0) - (ca[t] ?? 0);
      if ((delta[t] ?? 0) < 0) problems.push(`${t}: righe diminuite (${delta[t]})`);
    }
    const key =
      "id || '|' || account_id || '|' || amount_minor || '|' || transaction_date || '|' || COALESCE(booking_date, '')";
    const old = await a.all<{ k: string }>(`SELECT ${key} AS k FROM transactions WHERE status = 'booked'`);
    const now = new Set((await b.all<{ k: string }>(`SELECT ${key} AS k FROM transactions`)).map((r) => r.k));
    const changed = old.filter((r) => !now.has(r.k)).length;
    if (changed > 0) problems.push(`${changed} movimenti contabilizzati modificati o mancanti`);
  } finally {
    a.close();
    b.close();
  }
  return { problems, delta };
}

/**
 * Aggiornamento incrementale di un database ESISTENTE: prova su copia, backup indipendente verificato, applicazione su
 * copia di lavoro, controlli (piano, integrita', append-only, concorrenza), sostituzione atomica, rapporto. L'originale
 * non viene mai modificato in loco: in caso di problemi resta identico.
 */
async function applyIncremental(
  plan: Plan,
  fromPlan: (p: string) => string,
  target: string,
  out: (r: Record<string, unknown>) => void,
): Promise<number> {
  const repoRoot = resolve(dirname(new URL(import.meta.url).pathname), "..");
  const problems = await applyTargetProblems(target, repoRoot, plan);
  if (problems.length > 0) {
    out({ esito: "BLOCCATO prima di scrivere", database: "invariato", problemi: problems });
    return 1;
  }
  const checksumStart = await checksumOf(target);
  // 1. prova completa su una copia del database
  const dir = mkdtempSync(join(tmpdir(), "cloud-finance-apply-prova-"));
  let dry: { report: Record<string, unknown>; failures: string[] };
  try {
    const copy = join(dir, "copia.sqlite3");
    await snapshot(target, copy);
    dry = await runPlan(plan, fromPlan, { dbPath: copy, workDir: dir, probes: true });
    const ao = await appendOnlyProblems(target, copy);
    dry.failures.push(...ao.problems);
    dry.report.delta = ao.delta;
  } catch (err) {
    dry = {
      report: {},
      failures: [`errore nella prova: ${err instanceof Error ? err.message : String(err)}`],
    };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
  const prova = {
    esito: dry.failures.length === 0 ? "COERENTE" : "BLOCCATO",
    failures: dry.failures,
    delta: dry.report.delta,
  };
  if (dry.failures.length > 0) {
    out({ esito: "BLOCCATO prima di scrivere", database: "invariato", prova, report: dry.report });
    return 1;
  }
  const previousUmask = process.umask(0o077);
  const work = `${target}.in-corso`;
  try {
    // 2. backup indipendente, fuori dalla cartella del database, verificato
    const base = dirname(dirname(target));
    const backupDir = join(base, "backup");
    mkdirSync(backupDir, { recursive: true, mode: 0o700 });
    const backup = join(backupDir, `${basename(target, ".sqlite3")}-prima-di-apply-${stamp()}.sqlite3`);
    await snapshot(target, backup);
    if ((await checksumOf(backup)) !== checksumStart) {
      out({
        esito: "BLOCCATO prima di scrivere",
        database: "invariato",
        motivo: "backup diverso dal database",
        backup,
      });
      return 1;
    }
    // 3. applicazione su copia di lavoro
    await snapshot(target, work);
    const fail = (failures: string[]) => {
      const failed = `${target}.fallito-${stamp()}`;
      if (existsSync(work)) renameSync(work, failed);
      out({ esito: "BLOCCATO", database: "invariato", backup, fileConservatoPerAnalisi: failed, failures });
      return 1;
    };
    let run: { report: Record<string, unknown>; failures: string[] };
    try {
      run = await runPlan(plan, fromPlan, { dbPath: work, workDir: dirname(target), probes: false });
    } catch (err) {
      return fail([`errore nell'applicazione: ${err instanceof Error ? err.message : String(err)}`]);
    }
    const finalize = new NodeSqliteDb(work);
    try {
      await finalize.all("PRAGMA journal_mode = DELETE");
      const integrity = await finalize.first<{ integrity_check: string }>("PRAGMA integrity_check");
      if (integrity?.integrity_check !== "ok") run.failures.push("integrity_check non ok");
    } finally {
      finalize.close();
    }
    if (existsSync(`${work}-wal`)) run.failures.push("file WAL rimasto dopo la chiusura");
    const ao = await appendOnlyProblems(target, work);
    run.failures.push(...ao.problems);
    // 4. nessuna modifica concorrente dell'originale durante l'aggiornamento
    if ((await checksumOf(target)) !== checksumStart)
      run.failures.push("il database e' cambiato durante l'aggiornamento");
    if (run.failures.length > 0) return fail(run.failures);
    const checksumAfter = await checksumOf(work);
    // 5. sostituzione atomica (stessa cartella) e rapporto
    renameSync(work, target);
    const final = {
      esito:
        ao.delta.transactions === 0 && ao.delta.balance_observations === 0
          ? "AGGIORNATO (nessun dato nuovo)"
          : "AGGIORNATO",
      database: target,
      backupPrima: backup,
      improntaPrima: checksumStart,
      improntaDopo: checksumAfter,
      delta: ao.delta,
      ...run.report,
      prova,
    };
    const reportDir = join(base, "report");
    mkdirSync(reportDir, { recursive: true, mode: 0o700 });
    const reportPath = join(reportDir, `apply-${stamp()}.json`);
    writeFileSync(reportPath, `${JSON.stringify(final, null, 2)}\n`, { mode: 0o600 });
    out({ ...final, rapporto: reportPath });
    return 0;
  } finally {
    process.umask(previousUmask);
  }
}

export const stamp = () => new Date().toISOString().replace(/[:.]/g, "-");

async function main(argv: string[]): Promise<number> {
  const { values } = parseArgs({
    args: argv,
    options: {
      plan: { type: "string" },
      keep: { type: "boolean" },
      commit: { type: "string" },
      apply: { type: "string" },
    },
  });
  if (values.plan === undefined) {
    process.stderr.write(
      "uso: import-plan.ts --plan <piano.json> [--keep] [--commit <nuovo.sqlite3> | --apply <esistente.sqlite3>]\n",
    );
    return 64;
  }
  const planPath = values.plan;
  const plan = JSON.parse(readFileSync(planPath, "utf8")) as Plan;
  // percorsi relativi del piano: rispetto alla cartella del piano
  const fromPlan = (p: string) => resolve(dirname(resolve(planPath)), p);
  const out = (report: Record<string, unknown>) =>
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);

  if (values.apply !== undefined) {
    if (values.commit !== undefined) {
      process.stderr.write("--commit e --apply sono alternativi\n");
      return 64;
    }
    return applyIncremental(plan, fromPlan, resolve(values.apply), out);
  }

  // 1. prova completa su database temporaneo (sempre, anche prima del commit)
  const dir = mkdtempSync(join(tmpdir(), "cloud-finance-dry-run-"));
  // la copia temporanea contiene i dati del piano: eliminata anche se la prova si interrompe con un'eccezione
  const dry = await runPlan(plan, fromPlan, {
    dbPath: join(dir, "dry-run.sqlite3"),
    workDir: dir,
    probes: true,
  }).finally(() => {
    if (!values.keep) rmSync(dir, { recursive: true, force: true });
  });
  const dryReport = {
    database: values.keep ? join(dir, "dry-run.sqlite3") : "temporaneo (eliminato)",
    ...dry.report,
    esito: dry.failures.length === 0 ? "COERENTE" : "BLOCCATO",
    failures: dry.failures,
  };
  if (values.commit === undefined) {
    out(dryReport);
    return dry.failures.length === 0 ? 0 : 1;
  }

  // 2. importazione definitiva: solo se la prova e' coerente e il percorso e' sicuro
  const target = resolve(values.commit);
  const repoRoot = resolve(dirname(new URL(import.meta.url).pathname), "..");
  const problems = targetProblems(target, repoRoot);
  if (dry.failures.length > 0 || problems.length > 0) {
    out({
      esito: "BLOCCATO prima di scrivere",
      database: "nessun file creato",
      problemiPercorso: problems,
      prova: dryReport,
    });
    return 1;
  }
  const previousUmask = process.umask(0o077);
  try {
    mkdirSync(dirname(target), { recursive: true, mode: 0o700 });
    const work = `${target}.in-corso`;
    const run = await runPlan(plan, fromPlan, { dbPath: work, workDir: dirname(target), probes: false });
    // file unico e autosufficiente prima di verifiche e rinomina: niente WAL in sospeso
    const finalize = new NodeSqliteDb(work);
    try {
      await finalize.all("PRAGMA journal_mode = DELETE");
    } finally {
      finalize.close();
    }
    const check = new NodeSqliteDb(work, { readOnly: true });
    let checksum = "";
    let integrity = "";
    try {
      integrity =
        (await check.first<{ integrity_check: string }>("PRAGMA integrity_check"))?.integrity_check ?? "";
      checksum = await logicalChecksum(check);
    } finally {
      check.close();
    }
    if (integrity !== "ok") run.failures.push("integrity_check non ok");
    if (existsSync(`${work}-wal`)) run.failures.push("file WAL rimasto dopo la chiusura");
    if (run.failures.length > 0) {
      const failed = `${target}.fallito-${stamp()}`;
      renameSync(work, failed);
      out({
        esito: "BLOCCATO",
        database: "non creato",
        fileConservatoPerAnalisi: failed,
        failures: run.failures,
      });
      return 1;
    }
    // backup verificato del database appena creato, poi rinomina atomica al nome definitivo
    // backup FUORI dalla cartella del database: annullare o ripristinare il database non tocca mai il backup
    const backupDir = join(dirname(dirname(target)), "backup");
    mkdirSync(backupDir, { recursive: true, mode: 0o700 });
    const backup = join(backupDir, `${basename(target, ".sqlite3")}-dopo-import-${stamp()}.sqlite3`);
    const w = new NodeSqliteDb(work, { readOnly: true });
    try {
      await w.all(`VACUUM INTO '${backup.replaceAll("'", "''")}'`);
    } finally {
      w.close();
    }
    const b = new NodeSqliteDb(backup, { readOnly: true });
    let backupChecksum = "";
    try {
      backupChecksum = await logicalChecksum(b);
    } finally {
      b.close();
    }
    if (backupChecksum !== checksum) {
      const failed = `${target}.fallito-${stamp()}`;
      renameSync(work, failed);
      out({
        esito: "BLOCCATO",
        database: "non creato",
        motivo: "backup diverso dal database",
        fileConservatoPerAnalisi: failed,
      });
      return 1;
    }
    if (existsSync(target))
      throw new Error("il database definitivo e' comparso durante l'importazione: nessuna sovrascrittura");
    renameSync(work, target);
    const final = {
      esito: "IMPORTATO",
      database: target,
      backup,
      impronta: checksum,
      ...run.report,
      prova: { esito: dryReport.esito, failures: dryReport.failures },
    };
    const reportDir = join(dirname(dirname(target)), "report");
    mkdirSync(reportDir, { recursive: true, mode: 0o700 });
    const reportPath = join(reportDir, `import-${stamp()}.json`);
    writeFileSync(reportPath, `${JSON.stringify(final, null, 2)}\n`, { mode: 0o600 });
    out({ ...final, rapporto: reportPath });
    return 0;
  } finally {
    process.umask(previousUmask);
  }
}

if (import.meta.main) {
  main(process.argv.slice(2)).then(
    (code) => {
      process.exitCode = code;
    },
    (err: unknown) => {
      process.stderr.write(`errore: ${err instanceof Error ? err.message : String(err)}\n`);
      process.exitCode = 2;
    },
  );
}
