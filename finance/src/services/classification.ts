/**
 * Pubblicazione di regole versionate e classificazione delle transazioni.
 *
 *   - un file di regole valido diventa una NUOVA versione solo se il contenuto (checksum) e' nuovo: ripubblicarlo
 *     e' idempotente; le regole pubblicate non si modificano (trigger);
 *   - le categorie sono immutabili: una categoria esistente con definizione diversa blocca la pubblicazione;
 *   - la classificazione automatica non sovrascrive MAI una scelta dell'utente;
 *   - lo storico delle classificazioni e' append-only: ogni cambiamento aggiunge una riga con versione e regola.
 */
import type { Ctx } from "../context.ts";
import { NotFoundError, ValidationError } from "../context.ts";
import type { Statement } from "../db/types.ts";
import { stmt } from "../db/types.ts";
import { commitGuarded, readWriteSeq } from "../db/write.ts";
import { canonicalJson, type Json, sha256Hex } from "../domain/hash.ts";
import {
  type CategoryDef,
  type Condition,
  evaluateRules,
  KINDS,
  type Kind,
  type RuleDef,
  type RuleKind,
  validateRuleFile,
} from "../domain/rules.ts";
import { auditStatement, reviewStatement } from "./audit.ts";

export interface PublishResult {
  version: number;
  created: boolean;
  categoriesCreated: number;
}

export async function publishRuleSet(ctx: Ctx, input: unknown): Promise<PublishResult> {
  const { file, problems } = validateRuleFile(input);
  if (file === null) throw new ValidationError(`file di regole non valido:\n- ${problems.join("\n- ")}`);
  const checksum = await sha256Hex(canonicalJson(file as unknown as Json));
  const known = await ctx.db.first<{ version: number }>("SELECT version FROM rule_sets WHERE checksum = ?", [
    checksum,
  ]);
  if (known !== null) return { version: known.version, created: false, categoriesCreated: 0 };

  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const statements: Statement[] = [];
  const existingCats = new Map(
    (
      await ctx.db.all<CategoryDef & { parent_id: string | null }>(
        "SELECT id, name, parent_id, budget_group FROM categories",
      )
    ).map((c) => [c.id, c]),
  );
  const toCreate: CategoryDef[] = [];
  for (const c of file.categories) {
    const ex = existingCats.get(c.id);
    if (ex === undefined) toCreate.push(c);
    else if (ex.name !== c.name || ex.parent_id !== c.parent || ex.budget_group !== c.budget_group) {
      throw new ValidationError(
        `la categoria ${c.id} esiste gia' con una definizione diversa (le categorie sono immutabili)`,
      );
    }
  }
  // genitori prima dei figli
  const created = new Set(existingCats.keys());
  let pending = [...toCreate];
  while (pending.length > 0) {
    const ready = pending.filter((c) => c.parent === null || created.has(c.parent));
    if (ready.length === 0) throw new ValidationError("categorie con riferimenti circolari");
    for (const c of ready) {
      statements.push(
        stmt(
          "INSERT INTO categories (id, name, parent_id, budget_group, created_at) VALUES (?, ?, ?, ?, ?)",
          c.id,
          c.name,
          c.parent,
          c.budget_group,
          at,
        ),
      );
      created.add(c.id);
    }
    pending = pending.filter((c) => !ready.includes(c));
  }

  const last = await ctx.db.first<{ v: number | null }>("SELECT MAX(version) AS v FROM rule_sets");
  const version = (last?.v ?? 0) + 1;
  statements.push(
    stmt(
      "INSERT INTO rule_sets (version, checksum, note, created_at) VALUES (?, ?, ?, ?)",
      version,
      checksum,
      file.note,
      at,
    ),
  );
  for (const r of file.rules) {
    statements.push(
      stmt(
        `INSERT INTO rules (rule_set_version, rule_id, priority, description, conditions_json, kind, category_id, confidence)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
        version,
        r.id,
        r.priority,
        r.description,
        JSON.stringify(r.when.all),
        r.assign.kind,
        r.assign.category,
        r.assign.confidence,
      ),
    );
  }
  statements.push(
    auditStatement({
      at,
      actor: "user",
      action: "publish_rules",
      entity: "rule_set",
      entityId: String(version),
      after: { checksum, rules: file.rules.length, categories_created: toCreate.length },
    }),
  );
  await commitGuarded(ctx.db, seq, statements);
  return { version, created: true, categoriesCreated: toCreate.length };
}

interface RuleRow {
  rule_id: string;
  priority: number;
  description: string;
  conditions_json: string;
  kind: RuleKind;
  category_id: string | null;
  confidence: "high" | "low";
}

export async function loadRuleSet(
  ctx: Ctx,
  version?: number,
): Promise<{ version: number; rules: RuleDef[] } | null> {
  const v =
    version ??
    (await ctx.db.first<{ v: number | null }>("SELECT MAX(version) AS v FROM rule_sets"))?.v ??
    null;
  if (v === null) return null;
  const rows = await ctx.db.all<RuleRow>("SELECT * FROM rules WHERE rule_set_version = ? ORDER BY rule_id", [
    v,
  ]);
  return {
    version: v,
    rules: rows.map((r) => ({
      id: r.rule_id,
      priority: r.priority,
      description: r.description,
      when: { all: JSON.parse(r.conditions_json) as Condition[] },
      assign: { kind: r.kind, category: r.category_id, confidence: r.confidence },
    })),
  };
}

export interface ClassifyCounts {
  ruleSetVersion: number;
  examined: number;
  changed: number;
  unchanged: number;
  skippedUser: number;
  needsReview: number;
}

interface TxnForClassification {
  id: string;
  account_id: string;
  amount_minor: number;
  currency: string;
  description: string;
  counterparty: string | null;
  source_type: string | null;
  date: string;
  c_source: "rule" | "user" | null;
  c_kind: Kind | null;
  c_category: string | null;
  c_status: string | null;
  c_rule_id: string | null;
  c_version: number | null;
}

/** Applica l'ultima versione delle regole a tutte le transazioni non classificate dall'utente. Idempotente. */
export async function classifyTransactions(ctx: Ctx): Promise<ClassifyCounts> {
  const ruleSet = await loadRuleSet(ctx);
  if (ruleSet === null) throw new ValidationError("nessuna regola pubblicata: usare rules:publish");
  const seq = await readWriteSeq(ctx.db);
  const rows = await ctx.db.all<TxnForClassification>(
    `SELECT t.id, t.account_id, t.amount_minor, t.currency, t.description, t.counterparty, t.source_type,
            COALESCE(t.booking_date, t.transaction_date) AS date,
            c.source AS c_source, c.kind AS c_kind, c.category_id AS c_category, c.status AS c_status,
            c.rule_id AS c_rule_id, c.rule_set_version AS c_version
     FROM transactions t LEFT JOIN current_classifications c ON c.transaction_id = t.id
     ORDER BY t.transaction_date, t.id`,
  );
  const at = ctx.now();
  const counts: ClassifyCounts = {
    ruleSetVersion: ruleSet.version,
    examined: rows.length,
    changed: 0,
    unchanged: 0,
    skippedUser: 0,
    needsReview: 0,
  };
  const statements: Statement[] = [];
  for (const t of rows) {
    if (t.c_source === "user") {
      counts.skippedUser += 1;
      continue;
    }
    const d = evaluateRules(ruleSet.rules, t);
    if (d.status === "needs_review") counts.needsReview += 1;
    const same =
      t.c_source === "rule" &&
      t.c_kind === d.kind &&
      t.c_category === d.categoryId &&
      t.c_status === d.status &&
      t.c_rule_id === d.ruleId;
    if (same) {
      counts.unchanged += 1;
      continue;
    }
    counts.changed += 1;
    statements.push(
      stmt(
        `INSERT INTO classifications (transaction_id, kind, category_id, status, source, rule_set_version, rule_id, reason, created_at)
         VALUES (?, ?, ?, ?, 'rule', ?, ?, ?, ?)`,
        t.id,
        d.kind,
        d.categoryId,
        d.status,
        ruleSet.version,
        d.ruleId,
        d.reason,
        at,
      ),
    );
    if (d.status === "needs_review") {
      statements.push(
        reviewStatement({
          id: ctx.newId(),
          kind: "classification_needed",
          entity: "transaction",
          entityId: t.id,
          detail: {
            proposta: { kind: d.kind, category: d.categoryId },
            motivo: d.reason,
            versione_regole: ruleSet.version,
          },
          createdAt: at,
        }),
      );
    } else {
      statements.push(
        closeReviewStatement("classification_needed", "transaction", t.id, at, `regola ${d.ruleId}`),
      );
    }
  }
  if (statements.length === 0) return counts;
  statements.push(
    auditStatement({
      at,
      actor: "rule",
      action: "classify",
      entity: "rule_set",
      entityId: String(ruleSet.version),
      after: { ...counts },
    }),
  );
  await commitGuarded(ctx.db, seq, statements);
  return counts;
}

export function closeReviewStatement(
  kind: string,
  entity: string,
  entityId: string,
  at: string,
  resolution: string,
): Statement {
  return stmt(
    `UPDATE review_items SET status = 'resolved', closed_at = ?, resolution = ?
     WHERE kind = ? AND entity = ? AND entity_id = ? AND status = 'open'`,
    at,
    resolution,
    kind,
    entity,
    entityId,
  );
}

const USER_KINDS: readonly Kind[] = [...KINDS];

/** Classificazione decisa dall'utente: confermata, mai sovrascritta dalle regole. */
export function userClassificationStatements(
  txnId: string,
  kind: Kind,
  categoryId: string | null,
  reason: string,
  at: string,
): Statement[] {
  return [
    stmt(
      `INSERT INTO classifications (transaction_id, kind, category_id, status, source, reason, created_at)
       VALUES (?, ?, ?, 'confirmed', 'user', ?, ?)`,
      txnId,
      kind,
      categoryId,
      reason,
      at,
    ),
    closeReviewStatement("classification_needed", "transaction", txnId, at, reason),
  ];
}

export async function setClassification(
  ctx: Ctx,
  txnId: string,
  kind: Kind,
  categoryId: string | null,
  note: string | null = null,
): Promise<void> {
  if (!USER_KINDS.includes(kind)) throw new ValidationError(`tipo non valido: ${kind}`);
  const txn = await ctx.db.first<{ id: string }>("SELECT id FROM transactions WHERE id = ?", [txnId]);
  if (txn === null) throw new NotFoundError("transazione", txnId);
  if (
    categoryId !== null &&
    (await ctx.db.first("SELECT id FROM categories WHERE id = ?", [categoryId])) === null
  ) {
    throw new NotFoundError("categoria", categoryId);
  }
  const before = await ctx.db.first(
    "SELECT kind, category_id, status, source FROM current_classifications WHERE transaction_id = ?",
    [txnId],
  );
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  await commitGuarded(ctx.db, seq, [
    ...userClassificationStatements(txnId, kind, categoryId, note ?? "classificazione dell'utente", at),
    auditStatement({
      at,
      actor: "user",
      action: "classify",
      entity: "transaction",
      entityId: txnId,
      before,
      after: { kind, category_id: categoryId },
      reason: note,
    }),
  ]);
}
