import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { stmt } from "../src/db/types.ts";
import { evaluateRules, type RuleDef, validateRuleFile } from "../src/domain/rules.ts";
import { classifyTransactions, publishRuleSet, setClassification } from "../src/services/classification.ts";
import { listReviewItems } from "../src/services/review.ts";
import { count, importTx, row, setupWithAccounts, type TestEnv } from "./helpers.ts";

const DEMO_RULES = JSON.parse(
  readFileSync(new URL("../examples/sintetici/regole-demo.json", import.meta.url), "utf8"),
) as {
  rules: unknown[];
  categories: unknown[];
};

const rule = (over: Partial<RuleDef> & Pick<RuleDef, "id">): RuleDef => ({
  priority: 10,
  description: "r",
  when: { all: [{ field: "description", op: "contains", value: "caffe" }] },
  assign: { kind: "expense", category: "bar", confidence: "high" },
  ...over,
});

const facts = (description: string, amount_minor = -350, counterparty: string | null = null) => ({
  account_id: "conto",
  amount_minor,
  currency: "EUR",
  description,
  counterparty,
});

const current = (env: TestEnv, description: string) =>
  env.db.first<{
    kind: string;
    category_id: string | null;
    status: string;
    source: string;
    rule_set_version: number | null;
  }>(
    `SELECT c.* FROM current_classifications c JOIN transactions t ON t.id = c.transaction_id WHERE t.description = ?`,
    [description],
  );

describe("regole (funzioni pure)", () => {
  it("nessuna regola: unknown da verificare, senza inventare una categoria", () => {
    const d = evaluateRules([rule({ id: "a" })], facts("Negozio sconosciuto"));
    assert.deepEqual([d.kind, d.categoryId, d.status], ["unknown", null, "needs_review"]);
  });

  it("vince la priorita' piu' alta; conflitto a pari priorita' -> da verificare", () => {
    const rules = [
      rule({ id: "a", priority: 10 }),
      rule({ id: "b", priority: 20, assign: { kind: "expense", category: "colazione", confidence: "high" } }),
    ];
    assert.equal(evaluateRules(rules, facts("Caffe")).categoryId, "colazione");
    const conflict = evaluateRules(
      [
        rule({ id: "a" }),
        rule({ id: "b", assign: { kind: "expense", category: "altro", confidence: "high" } }),
      ],
      facts("Caffe"),
    );
    assert.equal(conflict.status, "needs_review");
    assert.equal(conflict.kind, "unknown");
    assert.match(conflict.reason, /conflitto/);
    // stesse conclusioni a pari priorita': nessun conflitto
    assert.equal(evaluateRules([rule({ id: "a" }), rule({ id: "b" })], facts("Caffe")).status, "auto");
  });

  it("bassa confidenza: proposta conservata ma da verificare", () => {
    const d = evaluateRules(
      [rule({ id: "a", assign: { kind: "expense", category: "bar", confidence: "low" } })],
      facts("Caffe"),
    );
    assert.deepEqual([d.kind, d.categoryId, d.status], ["expense", "bar", "needs_review"]);
  });

  it("segno incoerente con il tipo: da verificare", () => {
    const d = evaluateRules([rule({ id: "a" })], facts("Caffe rimborsato", 350));
    assert.equal(d.status, "needs_review");
    assert.match(d.reason, /incoerente/);
  });

  it("confronti su testo normalizzato, importi e direzione", () => {
    const rules = [
      rule({
        id: "grandi",
        when: {
          all: [
            { field: "counterparty", op: "starts_with", value: "BAR" },
            { field: "amount_minor", op: "lte", value: -1000 },
            { field: "direction", op: "equals", value: "out" },
          ],
        },
      }),
    ];
    assert.equal(evaluateRules(rules, facts("x", -1500, "  bar   Esempio")).status, "auto");
    assert.equal(evaluateRules(rules, facts("x", -500, "Bar Esempio")).kind, "unknown");
  });

  it("validazione del file di regole", () => {
    assert.equal(validateRuleFile(DEMO_RULES).problems.length, 0);
    const bad = validateRuleFile({
      schema: "cloud-finance-rules/1",
      categories: [{ id: "a", name: "A", parent: "manca", budget_group: "lusso" }],
      rules: [
        {
          id: "r",
          priority: 1.5,
          description: "",
          when: { all: [] },
          assign: { kind: "furto", confidence: "media" },
        },
        {
          id: "s",
          priority: 1,
          description: "d",
          when: { all: [{ field: "amount_minor", op: "contains", value: "x" }] },
          assign: { kind: "expense", category: "zzz", confidence: "high" },
        },
        {
          id: "t",
          priority: 1,
          description: "d",
          when: { all: [{ field: "description", op: "regex", value: ".*" }] },
          assign: { kind: "expense", confidence: "high" },
        },
      ],
    });
    assert.equal(bad.file, null);
    for (const fragment of [
      "parent sconosciuto",
      "budget_group",
      "priority",
      "description",
      "almeno una condizione",
      "assign.kind",
      "assign.confidence",
      "non valido per amount_minor",
      "assign.category sconosciuta",
      "operatore non ammesso",
    ]) {
      assert.ok(
        bad.problems.some((p) => p.includes(fragment)),
        fragment,
      );
    }
  });
});

describe("classificazione nel database", () => {
  const LINES = [
    row("2026-01-02", "1200.00", "Stipendio DEMO", { counterparty: "Azienda Esempio" }),
    row("2026-01-03", "-45.20", "Spesa supermercato", { counterparty: "Supermercato Esempio" }),
    row("2026-01-05", "-3.50", "Caffe", { counterparty: "Bar Esempio" }),
    row("2026-01-07", "-200.00", "Trasferimento verso carta"),
  ];

  it("versionamento: ripubblicare lo stesso file non crea versioni; le regole pubblicate sono immutabili", async () => {
    const env = await setupWithAccounts();
    const v1 = await publishRuleSet(env.ctx, DEMO_RULES);
    assert.deepEqual([v1.version, v1.created], [1, true]);
    const again = await publishRuleSet(env.ctx, structuredClone(DEMO_RULES));
    assert.deepEqual([again.version, again.created], [1, false]);
    await assert.rejects(env.db.batch([stmt("UPDATE rules SET priority = 1")]), /immutabili/);
    await assert.rejects(env.db.batch([stmt("DELETE FROM categories")]), /immutabili/);
    await assert.rejects(publishRuleSet(env.ctx, { schema: "x" }), /non valido/);
  });

  it("categoria esistente ridefinita: pubblicazione rifiutata", async () => {
    const env = await setupWithAccounts();
    await publishRuleSet(env.ctx, DEMO_RULES);
    const changed = structuredClone(DEMO_RULES) as { categories: { id: string; name: string }[] };
    const first = changed.categories[0];
    assert.ok(first);
    first.name = "Altro nome";
    await assert.rejects(publishRuleSet(env.ctx, changed), /immutabili/);
  });

  it("classifica, mette in verifica le incerte, e' idempotente", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", LINES);
    await publishRuleSet(env.ctx, DEMO_RULES);
    const c1 = await classifyTransactions(env.ctx);
    assert.equal(c1.examined, 4);
    assert.equal(c1.changed, 4);
    assert.equal(c1.needsReview, 2); // caffe (bassa confidenza) + trasferimento (nessuna regola)
    assert.deepEqual(
      await current(env, "Spesa supermercato").then((c) => [c?.kind, c?.category_id, c?.status]),
      ["expense", "spesa-alimentare", "auto"],
    );
    assert.deepEqual(
      await current(env, "Trasferimento verso carta").then((c) => [c?.kind, c?.category_id, c?.status]),
      ["unknown", null, "needs_review"],
    );
    const reviews = (await listReviewItems(env.ctx)).filter((i) => i.kind === "classification_needed");
    assert.equal(reviews.length, 2);
    const c2 = await classifyTransactions(env.ctx);
    assert.deepEqual([c2.changed, c2.unchanged], [0, 4]);
    assert.equal(await count(env.db, "classifications"), 4);
  });

  it("nuova versione delle regole: riclassifica solo cio' che cambia, mai le scelte dell'utente", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", LINES);
    await publishRuleSet(env.ctx, DEMO_RULES);
    await classifyTransactions(env.ctx);
    const caffeId =
      (await env.db.first<{ id: string }>("SELECT id FROM transactions WHERE description = 'Caffe'"))?.id ??
      "";
    await setClassification(env.ctx, caffeId, "expense", "bar-ristoranti", "confermato a mano");
    assert.ok(!(await listReviewItems(env.ctx)).some((i) => i.entity_id === caffeId), "verifica chiusa");

    const v2 = structuredClone(DEMO_RULES) as {
      rules: { id: string; assign: { confidence: string; category: string } }[];
    };
    for (const r of v2.rules) {
      if (r.id === "caffe") r.assign.category = "spesa-alimentare";
      if (r.id === "supermercato") r.assign.confidence = "low";
    }
    const pub = await publishRuleSet(env.ctx, v2);
    assert.equal(pub.version, 2);
    const c = await classifyTransactions(env.ctx);
    assert.equal(c.skippedUser, 1);
    assert.equal(c.changed, 1); // solo il supermercato (ora a bassa confidenza)
    const caffe = await current(env, "Caffe");
    assert.deepEqual([caffe?.source, caffe?.category_id], ["user", "bar-ristoranti"]);
    const spesa = await current(env, "Spesa supermercato");
    assert.deepEqual([spesa?.status, spesa?.rule_set_version], ["needs_review", 2]);
    // storico append-only: la classificazione v1 resta
    assert.equal(await count(env.db, "classifications", "rule_set_version = 1"), 4);
  });

  it("senza regole pubblicate la classificazione si rifiuta in modo esplicito", async () => {
    const env = await setupWithAccounts();
    await assert.rejects(classifyTransactions(env.ctx), /nessuna regola/);
  });
});
