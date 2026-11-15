/**
 * Regole di classificazione DETERMINISTICHE e versionate (funzioni pure).
 *
 * Principi:
 *   - una regola guarda solo dati gia' presenti (descrizione, controparte, importo, valuta, conto, direzione, data
 *     effettiva):
 *     non deduce MAI che cosa e' stato comprato;
 *   - vince la priorita' piu' alta; a pari priorita', esiti diversi = conflitto -> da verificare;
 *   - confidenza `low` -> proposta da verificare, mai applicata come certa;
 *   - nessuna regola applicabile -> `unknown`, da verificare;
 *   - tipo incoerente con il segno (spesa positiva, entrata negativa) -> da verificare.
 */
import { isIsoDate, normalizeText } from "./text.ts";

export const RULE_FILE_SCHEMA = "cloud-finance-rules/1";
export const KINDS = ["income", "expense", "transfer", "refund", "fee"] as const;
export type RuleKind = (typeof KINDS)[number];
export type Kind = RuleKind | "unknown";
export const TEXT_FIELDS = ["description", "counterparty"] as const;
export const NUMBER_FIELDS = ["amount_minor"] as const;
/** `source_type`: tipo di operazione come scritto dalla fonte (confronto esatto, maiuscole comprese). */
export const EXACT_FIELDS = ["currency", "account_id", "direction", "source_type"] as const;
/** `date`: data effettiva (contabilizzazione per le contabilizzate, operazione per le pending), YYYY-MM-DD. */
export const DATE_FIELDS = ["date"] as const;
export const OPS = ["equals", "contains", "starts_with", "one_of", "gte", "lte"] as const;

type Field =
  | (typeof TEXT_FIELDS)[number]
  | (typeof NUMBER_FIELDS)[number]
  | (typeof EXACT_FIELDS)[number]
  | (typeof DATE_FIELDS)[number];
type Op = (typeof OPS)[number];

export interface Condition {
  field: Field;
  op: Op;
  value: string | number | string[];
}

export interface RuleDef {
  id: string;
  priority: number;
  description: string;
  when: { all: Condition[] };
  assign: { kind: RuleKind; category: string | null; confidence: "high" | "low" };
}

export interface CategoryDef {
  id: string;
  name: string;
  parent: string | null;
  budget_group: "needs" | "wants" | "savings" | null;
}

export interface RuleFile {
  schema: typeof RULE_FILE_SCHEMA;
  note: string | null;
  categories: CategoryDef[];
  rules: RuleDef[];
}

export interface TxnFacts {
  account_id: string;
  amount_minor: number;
  currency: string;
  description: string;
  counterparty: string | null;
  source_type?: string | null;
  /** data effettiva YYYY-MM-DD (necessaria solo per le regole con il campo `date`) */
  date?: string | null;
}

export interface Decision {
  kind: Kind;
  categoryId: string | null;
  status: "auto" | "needs_review";
  ruleId: string | null;
  reason: string;
}

const ID = /^[a-z0-9][a-z0-9_.-]{0,63}$/;

/** Valida un file di regole (JSON gia' letto). Restituisce l'elenco dei problemi; vuoto = valido. */
export function validateRuleFile(input: unknown): { file: RuleFile | null; problems: string[] } {
  const problems: string[] = [];
  const p = (m: string): void => {
    problems.push(m);
  };
  if (!isObject(input)) return { file: null, problems: ["il file di regole deve essere un oggetto JSON"] };
  if (input.schema !== RULE_FILE_SCHEMA) p(`schema atteso "${RULE_FILE_SCHEMA}"`);
  if (input.note !== undefined && input.note !== null && typeof input.note !== "string") {
    p("note deve essere testo");
  }
  let categories: unknown[] = [];
  if (Array.isArray(input.categories)) categories = input.categories;
  else p("categories deve essere un elenco");
  let rules: unknown[] = [];
  if (Array.isArray(input.rules)) rules = input.rules;
  else p("rules deve essere un elenco");

  const catIds = new Set<string>();
  for (const [i, c] of categories.entries()) {
    const at = `categories[${i}]`;
    if (!isObject(c)) {
      p(`${at}: oggetto atteso`);
      continue;
    }
    if (typeof c.id !== "string" || !ID.test(c.id)) p(`${at}: id non valido`);
    else if (catIds.has(c.id)) p(`${at}: id ripetuto ${c.id}`);
    else catIds.add(c.id);
    if (typeof c.name !== "string" || c.name.trim() === "") p(`${at}: name obbligatorio`);
    if (c.parent !== null && c.parent !== undefined && typeof c.parent !== "string")
      p(`${at}: parent non valido`);
    if (![null, undefined, "needs", "wants", "savings"].includes(c.budget_group as string)) {
      p(`${at}: budget_group non valido`);
    }
  }
  for (const [i, c] of categories.entries()) {
    if (isObject(c) && typeof c.parent === "string" && !catIds.has(c.parent)) {
      p(`categories[${i}]: parent sconosciuto ${c.parent}`);
    }
  }

  const ruleIds = new Set<string>();
  for (const [i, r] of rules.entries()) {
    const at = `rules[${i}]`;
    if (!isObject(r)) {
      p(`${at}: oggetto atteso`);
      continue;
    }
    if (typeof r.id !== "string" || !ID.test(r.id)) p(`${at}: id non valido`);
    else if (ruleIds.has(r.id)) p(`${at}: id ripetuto ${r.id}`);
    else ruleIds.add(r.id);
    if (!Number.isSafeInteger(r.priority)) p(`${at}: priority intera obbligatoria`);
    if (typeof r.description !== "string" || r.description.trim() === "")
      p(`${at}: description obbligatoria`);
    const when = r.when;
    if (!isObject(when) || !Array.isArray(when.all) || when.all.length === 0) {
      p(`${at}: when.all deve contenere almeno una condizione`);
    } else {
      for (const [j, c] of when.all.entries()) validateCondition(c, `${at}.when.all[${j}]`, p);
    }
    const assign = r.assign;
    if (!isObject(assign)) {
      p(`${at}: assign obbligatorio`);
      continue;
    }
    if (!KINDS.includes(assign.kind as RuleKind)) p(`${at}: assign.kind non valido`);
    if (assign.category !== null && assign.category !== undefined) {
      if (typeof assign.category !== "string" || !catIds.has(assign.category)) {
        p(`${at}: assign.category sconosciuta`);
      }
    }
    if (assign.confidence !== "high" && assign.confidence !== "low") {
      p(`${at}: assign.confidence deve essere high o low`);
    }
  }

  if (problems.length > 0) return { file: null, problems };
  const file = input as unknown as RuleFile;
  return {
    file: {
      schema: RULE_FILE_SCHEMA,
      note: file.note ?? null,
      categories: file.categories.map((c) => ({
        id: c.id,
        name: c.name,
        parent: c.parent ?? null,
        budget_group: c.budget_group ?? null,
      })),
      rules: file.rules.map((r) => ({
        id: r.id,
        priority: r.priority,
        description: r.description,
        when: { all: r.when.all.map((c) => ({ field: c.field, op: c.op, value: c.value })) },
        assign: { kind: r.assign.kind, category: r.assign.category ?? null, confidence: r.assign.confidence },
      })),
    },
    problems,
  };
}

function validateCondition(c: unknown, at: string, p: (m: string) => void): void {
  if (!isObject(c)) {
    p(`${at}: oggetto atteso`);
    return;
  }
  const field = c.field as Field;
  const op = c.op as Op;
  const isText = (TEXT_FIELDS as readonly string[]).includes(field);
  const isNumber = (NUMBER_FIELDS as readonly string[]).includes(field);
  const isExact = (EXACT_FIELDS as readonly string[]).includes(field);
  const isDate = (DATE_FIELDS as readonly string[]).includes(field);
  if (!isText && !isNumber && !isExact && !isDate) {
    p(`${at}: campo non ammesso ${String(c.field)}`);
    return;
  }
  if (!OPS.includes(op)) {
    p(`${at}: operatore non ammesso ${String(c.op)}`);
    return;
  }
  if (isNumber) {
    if (!["equals", "gte", "lte"].includes(op)) p(`${at}: operatore ${op} non valido per ${field}`);
    if (!Number.isSafeInteger(c.value)) p(`${at}: valore intero (unita' minime) richiesto`);
    return;
  }
  if (isDate) {
    if (!["equals", "gte", "lte"].includes(op)) p(`${at}: operatore ${op} non valido per ${field}`);
    if (typeof c.value !== "string" || !isIsoDate(c.value)) p(`${at}: data YYYY-MM-DD richiesta`);
    return;
  }
  if (op === "gte" || op === "lte") {
    p(`${at}: operatore ${op} valido solo per amount_minor e date`);
    return;
  }
  if (isExact && op !== "equals" && op !== "one_of") p(`${at}: per ${field} solo equals o one_of`);
  if (op === "one_of") {
    if (
      !Array.isArray(c.value) ||
      c.value.length === 0 ||
      !c.value.every((v) => typeof v === "string" && v !== "")
    ) {
      p(`${at}: one_of richiede un elenco di testi non vuoti`);
    }
  } else if (typeof c.value !== "string" || c.value.trim() === "") {
    p(`${at}: valore testuale non vuoto richiesto`);
  }
  if (field === "direction") {
    const values = Array.isArray(c.value) ? c.value : [c.value];
    if (!values.every((v) => v === "in" || v === "out")) p(`${at}: direction ammette solo "in" o "out"`);
  }
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export function direction(amountMinor: number): "in" | "out" | "zero" {
  return amountMinor > 0 ? "in" : amountMinor < 0 ? "out" : "zero";
}

function fieldValue(t: TxnFacts, field: Field): string | number | null {
  switch (field) {
    case "description":
      return normalizeText(t.description);
    case "counterparty":
      return t.counterparty === null ? null : normalizeText(t.counterparty);
    case "amount_minor":
      return t.amount_minor;
    case "currency":
      return t.currency;
    case "account_id":
      return t.account_id;
    case "direction":
      return direction(t.amount_minor);
    case "source_type":
      return t.source_type ?? null;
    case "date":
      return t.date ?? null;
  }
}

export function conditionMatches(t: TxnFacts, c: Condition): boolean {
  const actual = fieldValue(t, c.field);
  if (actual === null) return false;
  const isText = (TEXT_FIELDS as readonly string[]).includes(c.field);
  const norm = (v: string) => (isText ? normalizeText(v) : v);
  switch (c.op) {
    case "equals":
      return typeof c.value === "string" ? actual === norm(c.value) : actual === c.value;
    case "contains":
      return typeof actual === "string" && typeof c.value === "string" && actual.includes(norm(c.value));
    case "starts_with":
      return typeof actual === "string" && typeof c.value === "string" && actual.startsWith(norm(c.value));
    case "one_of":
      return Array.isArray(c.value) && c.value.some((v) => actual === norm(v));
    case "gte":
      return (
        typeof actual === typeof c.value &&
        (typeof c.value === "number" || typeof c.value === "string") &&
        actual >= c.value
      );
    case "lte":
      return (
        typeof actual === typeof c.value &&
        (typeof c.value === "number" || typeof c.value === "string") &&
        actual <= c.value
      );
  }
}

const SIGN_OK: Record<RuleKind, (dir: "in" | "out" | "zero") => boolean> = {
  income: (d) => d === "in",
  refund: (d) => d === "in",
  expense: (d) => d === "out",
  fee: (d) => d === "out",
  transfer: (d) => d !== "zero",
};

/** Decisione per una transazione secondo un insieme di regole. Funzione pura e deterministica. */
export function evaluateRules(rules: readonly RuleDef[], t: TxnFacts): Decision {
  const matching = rules.filter((r) => r.when.all.every((c) => conditionMatches(t, c)));
  if (matching.length === 0) {
    return {
      kind: "unknown",
      categoryId: null,
      status: "needs_review",
      ruleId: null,
      reason: "nessuna regola applicabile",
    };
  }
  const top = Math.max(...matching.map((r) => r.priority));
  const best = matching.filter((r) => r.priority === top).sort((a, b) => a.id.localeCompare(b.id));
  const outcomes = new Set(best.map((r) => `${r.assign.kind}|${r.assign.category ?? ""}`));
  if (outcomes.size > 1) {
    return {
      kind: "unknown",
      categoryId: null,
      status: "needs_review",
      ruleId: null,
      reason: `regole in conflitto a pari priorita': ${best.map((r) => r.id).join(", ")}`,
    };
  }
  const rule = best[0];
  if (rule === undefined) throw new Error("invariante violata");
  const dir = direction(t.amount_minor);
  if (!SIGN_OK[rule.assign.kind](dir)) {
    return {
      kind: "unknown",
      categoryId: null,
      status: "needs_review",
      ruleId: rule.id,
      reason: `la regola ${rule.id} indica ${rule.assign.kind} ma il segno dell'importo e' incoerente`,
    };
  }
  return {
    kind: rule.assign.kind,
    categoryId: rule.assign.category,
    status: rule.assign.confidence === "high" ? "auto" : "needs_review",
    ruleId: rule.id,
    reason:
      rule.assign.confidence === "high"
        ? `regola ${rule.id}`
        : `regola ${rule.id} a bassa confidenza: proposta da confermare`,
  };
}
