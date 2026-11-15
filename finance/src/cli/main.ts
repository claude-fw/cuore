#!/usr/bin/env node
/**
 * CLI locale di Cloud Finance. Uscita: JSON su stdout; errori su stderr con codice di uscita != 0.
 * Il database va indicato esplicitamente (--db o CLOUD_FINANCE_DB): nessun percorso predefinito, cosi' un dato reale
 * non finisce mai per sbaglio dentro il repository o in iCloud.
 */
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { parseArgs } from "node:util";
import { type Ctx, createCtx, NotFoundError, ValidationError } from "../context.ts";
import { migrate, migrationStatus } from "../db/migrate.ts";
import { MIGRATIONS } from "../db/migrations/index.ts";
import { SCHEMA_SNAPSHOT } from "../db/migrations/schema-snapshot.generated.ts";
import { NodeSqliteDb, readImmutable, SnapshotUnavailableError } from "../db/node-sqlite.ts";
import { verifyDatabase } from "../db/schema-check.ts";
import { ConflictError } from "../db/write.ts";
import { parseAmountToMinor } from "../domain/money.ts";
import type { Kind } from "../domain/rules.ts";
import { importFile } from "../import/importer.ts";
import { PENDING_BANK_FORMATS } from "../import/parsers/bank-formats.ts";
import { FormatNotAvailableError, getParser, listFormats } from "../import/parsers/registry.ts";
import { OPERATIONS, runQuery } from "../query/operations.ts";
import {
  type AccountType,
  createAccount,
  createInstitution,
  getAccount,
  type HolderRole,
  listAccounts,
} from "../services/accounts.ts";
import { addManualBalance } from "../services/balances.ts";
import { classifyTransactions, publishRuleSet, setClassification } from "../services/classification.ts";
import { decideLink, detectLinks, listLinks } from "../services/links.ts";
import {
  type Allocation,
  createCounterparty,
  createObligation,
  linkTransactionToObligations,
  listObligations,
  obligationsPositionAt,
  recordObligationEvent,
} from "../services/obligations.ts";
import {
  addFxRate,
  allocateToGoal,
  createGoal,
  createPlannedPurchase,
  listGoalsWithProgress,
  listPlannedPurchases,
  type PurchaseStatus,
  setPurchaseStatus,
} from "../services/planning.ts";
import { reconcileAccount, reconcileObservedPeriods } from "../services/reconciliation.ts";
import { liquidity, periodReport, status } from "../services/reports.ts";
import { closeReviewItem, listReviewItems } from "../services/review.ts";

const HELP = `cloud-finance --db <file.sqlite3> <comando> [opzioni]

Database e stato
  migrate                         applica le migrazioni mancanti
  migrate:status                  versione attuale e migrazioni in attesa (nessuna scrittura)
  migrate:verify                  versioni, checksum, schema e registro di Wrangler (sola lettura)
  status                          conteggi, ultime importazioni, elementi da verificare

Conti e dati
  institution:add --id --name
  account:add --id --name --type <current|savings|card|cash|investment|other> --currency EUR
              [--holder owner|joint|delegate|other] [--institution <id>]
  accounts
  formats                         formati di importazione (implementati e solo predisposti)
  format:info --id <formato>      dettagli; per i formati predisposti: fatti verificati e cosa manca
  import --account <id> --format <id> --file <percorso>
  balance:add --account <id> --date YYYY-MM-DD --amount 123.45 [--note]

Classificazione, collegamenti, verifica
  rules:publish --file regole.json
  classify                        applica l'ultima versione delle regole
  classify:set --txn <id> --kind <income|expense|transfer|refund|fee> [--category <id>] [--note]
  detect                          propone trasferimenti tra conti propri e rimborsi
  links [--status proposed|confirmed|rejected]
  link:decide --id <id> --decision confirmed|rejected
  review [--status open|resolved|dismissed|all]
  review:close --id <id> --resolution <testo> [--dismiss]
  reconcile --account <id> (--from YYYY-MM-DD --to YYYY-MM-DD | --observed)

Riepiloghi e pianificazione
  report --from YYYY-MM-DD --to YYYY-MM-DD [--account <id>]...
  liquidity --date YYYY-MM-DD [--base EUR]
  goal:add --name --target 500.00 --currency EUR [--deadline YYYY-MM-DD]
  goal:allocate --goal <id> --amount 50.00 --date YYYY-MM-DD [--note]
  goals
  purchase:add --title --currency EUR [--estimate 30.00] [--priority 1-5] [--goal <id>]
  purchase:status --id <id> --status idea|planned|purchased|cancelled [--txn <id>]
  purchases
  fx:add --date YYYY-MM-DD --base USD --quote EUR --rate 0.9234 --source <testo>

Crediti e debiti (non sono entrate ne' spese: la cassa resta nei movimenti bancari)
  counterparty:add --id --name [--note]
  obligation:add --counterparty <id> --direction receivable|payable --amount 50.00 --currency EUR --date YYYY-MM-DD
                 --description <testo> [--due YYYY-MM-DD] [--txn <movimento verificato>] [--confirm-duplicate]
  obligation:event --id <id> --kind increase|payment|forgive|adjust --amount 10.00 --date YYYY-MM-DD [--note]
                   [--confirm-duplicate]                 (senza movimento bancario, es. contanti o condono)
  obligation:link --txn <id> --allocation <obbligazione>:<payment|increase>:<importo> [--allocation ...] [--note]
                  (le quote devono coprire esattamente il movimento)
  obligations --date YYYY-MM-DD                          elenco con residui e posizione per valuta

Consultazione (contratto v1, database aperto in SOLA LETTURA; risposta { ok, data | error })
  query <operazione> [--input '<json>']                  operazioni: accounts, summary, balances, transactions,
                                                         obligations, data_status (vedi docs/consultazione.md);
                                                         search --input '{"q":"..."}' solo con la v8 (docs/ricerca.md)`;

const OPTIONS = {
  db: { type: "string" },
  help: { type: "boolean", short: "h" },
  id: { type: "string" },
  name: { type: "string" },
  type: { type: "string" },
  currency: { type: "string" },
  holder: { type: "string" },
  institution: { type: "string" },
  account: { type: "string", multiple: true },
  format: { type: "string" },
  file: { type: "string" },
  date: { type: "string" },
  amount: { type: "string" },
  note: { type: "string" },
  txn: { type: "string" },
  kind: { type: "string" },
  category: { type: "string" },
  decision: { type: "string" },
  status: { type: "string" },
  resolution: { type: "string" },
  dismiss: { type: "boolean" },
  from: { type: "string" },
  to: { type: "string" },
  observed: { type: "boolean" },
  base: { type: "string" },
  target: { type: "string" },
  deadline: { type: "string" },
  goal: { type: "string" },
  title: { type: "string" },
  estimate: { type: "string" },
  priority: { type: "string" },
  counterparty: { type: "string" },
  direction: { type: "string" },
  due: { type: "string" },
  description: { type: "string" },
  allocation: { type: "string", multiple: true },
  "confirm-duplicate": { type: "boolean" },
  input: { type: "string" },
  quote: { type: "string" },
  rate: { type: "string" },
  source: { type: "string" },
} as const;

type Values = ReturnType<typeof parseArgs<{ options: typeof OPTIONS; allowPositionals: true }>>["values"];

function need(values: Values, name: keyof Values): string {
  const v = values[name];
  const s = Array.isArray(v) ? v[0] : v;
  if (typeof s !== "string" || s === "") throw new ValidationError(`opzione obbligatoria: --${String(name)}`);
  return s;
}

function opt(values: Values, name: keyof Values): string | null {
  const v = values[name];
  const s = Array.isArray(v) ? v[0] : v;
  return typeof s === "string" && s !== "" ? s : null;
}

async function run(ctx: Ctx, command: string, v: Values): Promise<unknown> {
  switch (command) {
    case "migrate":
      return migrate(ctx.db, MIGRATIONS, ctx.now);
    case "migrate:verify":
      return verifyDatabase(ctx.db, MIGRATIONS, SCHEMA_SNAPSHOT);
    case "migrate:status":
      return migrationStatus(ctx.db, MIGRATIONS);
    case "status":
      return status(ctx);
    case "institution:add":
      await createInstitution(ctx, need(v, "id"), need(v, "name"));
      return { ok: true };
    case "account:add":
      return createAccount(ctx, {
        id: need(v, "id"),
        name: need(v, "name"),
        type: need(v, "type") as AccountType,
        currency: need(v, "currency"),
        holderRole: (opt(v, "holder") ?? "owner") as HolderRole,
        institutionId: opt(v, "institution"),
      });
    case "accounts":
      return listAccounts(ctx);
    case "formats":
      return listFormats();
    case "format:info": {
      const id = need(v, "id");
      const pending = PENDING_BANK_FORMATS.find((f) => f.id === id);
      if (pending !== undefined) return pending;
      const p = getParser(id);
      return {
        id: p.id,
        status: "implementato",
        version: p.version,
        description: p.description,
        verifiedAgainstRealExports: p.verifiedAgainstRealExports,
      };
    }
    case "import": {
      const path = resolve(need(v, "file"));
      return importFile(ctx, {
        accountId: need(v, "account"),
        parserId: need(v, "format"),
        fileName: path.split("/").pop() ?? null,
        content: readFileSync(path),
      });
    }
    case "balance:add": {
      const account = await getAccount(ctx, need(v, "account"));
      return addManualBalance(
        ctx,
        account.id,
        need(v, "date"),
        parseAmountToMinor(need(v, "amount"), account.currency),
        opt(v, "note"),
      );
    }
    case "rules:publish":
      return publishRuleSet(ctx, JSON.parse(readFileSync(resolve(need(v, "file")), "utf8")) as unknown);
    case "classify":
      return classifyTransactions(ctx);
    case "classify:set":
      await setClassification(
        ctx,
        need(v, "txn"),
        need(v, "kind") as Kind,
        opt(v, "category"),
        opt(v, "note"),
      );
      return { ok: true };
    case "detect":
      return detectLinks(ctx);
    case "links":
      return listLinks(
        ctx,
        (opt(v, "status") ?? undefined) as "proposed" | "confirmed" | "rejected" | undefined,
      );
    case "link:decide": {
      const decision = need(v, "decision");
      if (decision !== "confirmed" && decision !== "rejected")
        throw new ValidationError("--decision confirmed|rejected");
      return decideLink(ctx, need(v, "id"), decision);
    }
    case "review":
      return listReviewItems(ctx, (opt(v, "status") ?? "open") as "open" | "resolved" | "dismissed" | "all");
    case "review:close":
      await closeReviewItem(ctx, need(v, "id"), v.dismiss ? "dismissed" : "resolved", need(v, "resolution"));
      return { ok: true };
    case "reconcile": {
      const account = need(v, "account");
      if (v.observed) return reconcileObservedPeriods(ctx, account);
      return reconcileAccount(ctx, account, need(v, "from"), need(v, "to"));
    }
    case "report":
      return periodReport(ctx, need(v, "from"), need(v, "to"), v.account ?? []);
    case "liquidity":
      return liquidity(ctx, need(v, "date"), opt(v, "base") ?? undefined);
    case "goal:add": {
      const currency = need(v, "currency");
      return createGoal(ctx, {
        name: need(v, "name"),
        targetMinor: parseAmountToMinor(need(v, "target"), currency),
        currency,
        deadline: opt(v, "deadline"),
      });
    }
    case "goal:allocate": {
      const goalId = need(v, "goal");
      const goal = (await listGoalsWithProgress(ctx)).find((g) => g.id === goalId);
      if (goal === undefined) throw new NotFoundError("obiettivo", goalId);
      return {
        id: await allocateToGoal(
          ctx,
          goalId,
          parseAmountToMinor(need(v, "amount"), goal.currency),
          need(v, "date"),
          opt(v, "note"),
        ),
      };
    }
    case "goals":
      return listGoalsWithProgress(ctx);
    case "purchase:add": {
      const currency = need(v, "currency");
      const estimate = opt(v, "estimate");
      const priority = opt(v, "priority");
      return createPlannedPurchase(ctx, {
        title: need(v, "title"),
        currency,
        estimatedMinor: estimate === null ? null : parseAmountToMinor(estimate, currency),
        priority: priority === null ? null : Number(priority),
        goalId: opt(v, "goal"),
      });
    }
    case "purchase:status":
      await setPurchaseStatus(ctx, need(v, "id"), need(v, "status") as PurchaseStatus, opt(v, "txn"));
      return { ok: true };
    case "purchases":
      return listPlannedPurchases(ctx);
    case "counterparty:add":
      await createCounterparty(ctx, { id: need(v, "id"), name: need(v, "name"), note: opt(v, "note") });
      return { ok: true };
    case "obligation:add": {
      const currency = need(v, "currency");
      return createObligation(ctx, {
        counterpartyId: need(v, "counterparty"),
        direction: need(v, "direction") as "receivable" | "payable",
        currency,
        amountMinor: parseAmountToMinor(need(v, "amount"), currency),
        openedOn: need(v, "date"),
        dueOn: opt(v, "due"),
        description: need(v, "description"),
        transactionId: opt(v, "txn"),
        confirmDuplicate: v["confirm-duplicate"] === true,
      });
    }
    case "obligation:event": {
      const id = need(v, "id");
      const o = await ctx.db.first<{ currency: string }>("SELECT currency FROM obligations WHERE id = ?", [
        id,
      ]);
      if (o === null) throw new NotFoundError("credito/debito", id);
      return {
        id: await recordObligationEvent(ctx, id, {
          kind: need(v, "kind") as "increase" | "payment" | "forgive" | "adjust",
          amountMinor: parseAmountToMinor(need(v, "amount"), o.currency),
          occurredOn: need(v, "date"),
          note: opt(v, "note"),
          confirmDuplicate: v["confirm-duplicate"] === true,
        }),
      };
    }
    case "obligation:link": {
      const txnId = need(v, "txn");
      const t = await ctx.db.first<{ currency: string }>("SELECT currency FROM transactions WHERE id = ?", [
        txnId,
      ]);
      if (t === null) throw new NotFoundError("transazione", txnId);
      const allocations: Allocation[] = (v.allocation ?? []).map((spec) => {
        const m = /^([^:]+):(payment|increase):(.+)$/.exec(spec);
        if (m === null)
          throw new ValidationError(`quota non valida: ${spec} (obbligazione:payment|increase:importo)`);
        return {
          obligationId: m[1] ?? "",
          kind: m[2] as "payment" | "increase",
          amountMinor: parseAmountToMinor(m[3] ?? "", t.currency),
        };
      });
      return { events: await linkTransactionToObligations(ctx, txnId, allocations, opt(v, "note")) };
    }
    case "obligations": {
      const date = need(v, "date");
      return {
        obligations: await listObligations(ctx, date),
        position: await obligationsPositionAt(ctx, date),
      };
    }
    case "fx:add":
      await addFxRate(
        ctx,
        need(v, "date"),
        need(v, "base"),
        need(v, "quote"),
        need(v, "rate"),
        need(v, "source"),
      );
      return { ok: true };
    default:
      throw new ValidationError(`comando sconosciuto: ${command}\n\n${HELP}`);
  }
}

async function main(argv: string[]): Promise<number> {
  const { values, positionals } = parseArgs({
    args: argv,
    options: OPTIONS,
    allowPositionals: true,
    strict: true,
  });
  const command = positionals[0];
  if (values.help || command === undefined) {
    process.stdout.write(`${HELP}\n`);
    return command === undefined && !values.help ? 64 : 0;
  }
  const dbPath = values.db ?? process.env.CLOUD_FINANCE_DB;
  if (dbPath === undefined || dbPath === "")
    throw new ValidationError("indicare il database con --db o CLOUD_FINANCE_DB");
  if (command === "query") return consult(dbPath, positionals[1], values.input);
  process.umask(0o077);
  if (dbPath !== ":memory:") mkdirSync(dirname(resolve(dbPath)), { recursive: true, mode: 0o700 });
  const db = new NodeSqliteDb(dbPath);
  try {
    if (command !== "migrate" && command !== "migrate:status" && command !== "migrate:verify") {
      const st = await migrationStatus(db, MIGRATIONS);
      if (st.pending.length > 0) {
        throw new ValidationError(
          `database alla versione ${st.current}, attesa ${st.target}: eseguire "migrate"`,
        );
      }
    }
    const result = await run(createCtx(db), command, values);
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    // file rifiutato: esito leggibile su stdout, ma codice di uscita diverso da 0 per gli script
    const rejected =
      typeof result === "object" && result !== null && "status" in result && result.status === "rejected";
    return command === "import" && rejected ? 3 : 0;
  } finally {
    db.close();
  }
}

/**
 * Consultazione in SOLA LETTURA su un'istantanea in memoria (`readImmutable`: mai creato, mai migrato, nessun -wal/-shm,
 * nessun lock; rifiutata se e' in corso una scrittura), risposta nella busta di `runQuery`.
 * Codice di uscita 0 se `ok`, 2 altrimenti. La data odierna (per `asOf` assente) e' quella di Europe/Rome.
 */
async function consult(
  dbPath: string,
  operation: string | undefined,
  inputJson: string | undefined,
): Promise<number> {
  if (operation === undefined)
    throw new ValidationError(`indicare l'operazione: ${OPERATIONS.map((o) => o.name).join(", ")}`);
  if (dbPath === ":memory:" || !existsSync(dbPath)) throw new ValidationError("database inesistente");
  let input: unknown = {};
  if (inputJson !== undefined) {
    try {
      input = JSON.parse(inputJson);
    } catch {
      throw new ValidationError("--input: JSON non valido");
    }
  }
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Rome" }).format(new Date());
  let response: Awaited<ReturnType<typeof runQuery>>;
  try {
    response = await readImmutable(dbPath, (db) => runQuery(db, operation, input, { today }));
  } catch (err) {
    if (err instanceof SnapshotUnavailableError)
      throw new ValidationError(`database non leggibile: ${err.message}`);
    throw err;
  }
  process.stdout.write(`${JSON.stringify(response, null, 2)}\n`);
  return response.ok ? 0 : 2;
}

main(process.argv.slice(2)).then(
  (code) => {
    process.exitCode = code;
  },
  (err: unknown) => {
    const expected =
      err instanceof ValidationError ||
      err instanceof NotFoundError ||
      err instanceof ConflictError ||
      err instanceof FormatNotAvailableError ||
      err instanceof TypeError;
    process.stderr.write(`errore: ${err instanceof Error ? err.message : String(err)}\n`);
    process.exitCode = expected ? 2 : 1;
  },
);
