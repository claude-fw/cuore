/**
 * Consultazione v1, operazioni additive `categories` e `planning` (dati SINTETICI): stessi gruppi del riepilogo, stato
 * dichiarato distinto dall'avanzamento calcolato, «mancante» (null) distinto da zero, nessuna scrittura.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import type { Db } from "../src/db/types.ts";
import { OPERATIONS, runQuery } from "../src/query/operations.ts";
import { publishRuleSet } from "../src/services/classification.ts";
import {
  allocateToGoal,
  createGoal,
  createPlannedPurchase,
  setPurchaseStatus,
} from "../src/services/planning.ts";
import { setup, setupWithAccounts } from "./helpers.ts";

const DEMO_RULES = JSON.parse(
  readFileSync(new URL("../examples/sintetici/regole-demo.json", import.meta.url), "utf8"),
) as { categories: { id: string; name: string; parent: string | null; budget_group: string | null }[] };

/** Conta i tentativi di scrittura: la consultazione non deve mai chiamare `batch`. */
function counting(db: Db): { db: Db; writes: () => number } {
  let n = 0;
  return {
    db: {
      all: (sql, params) => db.all(sql, params),
      first: (sql, params) => db.first(sql, params),
      batch: async (s) => {
        n++;
        return db.batch(s);
      },
    },
    writes: () => n,
  };
}

describe("consultazione: categorie e pianificazione", () => {
  it("le operazioni sono nel contratto (quindi anche strumenti MCP) senza campi d'ingresso", () => {
    for (const name of ["categories", "planning"]) {
      const op = OPERATIONS.find((o) => o.name === name);
      assert.ok(op, name);
      assert.deepEqual(Object.keys(op.input), []);
    }
  });

  it("categories: nomi e gruppi di budget come nel set di regole, null = senza gruppo", async () => {
    const env = await setup();
    await publishRuleSet(env.ctx, DEMO_RULES);
    const r = await runQuery(env.db, "categories");
    assert.equal(r.ok, true, JSON.stringify(r));
    const items = (
      r as {
        data: { items: { id: string; name: string; parentId: string | null; budgetGroup: string | null }[] };
      }
    ).data.items;
    assert.equal(items.length, DEMO_RULES.categories.length);
    for (const c of DEMO_RULES.categories) {
      const got = items.find((i) => i.id === c.id);
      assert.deepEqual(got, { id: c.id, name: c.name, parentId: c.parent, budgetGroup: c.budget_group });
    }
    const bad = await runQuery(env.db, "categories", { extra: 1 });
    assert.deepEqual([bad.ok, bad.ok ? "" : bad.error.code], [false, "invalid_input"]);
  });

  it("planning: zero elementi e' una lista vuota, non un errore", async () => {
    const env = await setup();
    const r = await runQuery(env.db, "planning");
    assert.deepEqual(r.ok ? r.data : r.error, { goals: [], purchases: [] });
  });

  it("planning: stato dichiarato e avanzamento calcolato separati; stima mancante = null (il motore rifiuta stime <= 0); nessuna scrittura", async () => {
    const env = await setupWithAccounts();
    const goal = await createGoal(env.ctx, {
      name: "Fondo DEMO",
      targetMinor: 10000,
      currency: "EUR",
      deadline: null,
    });
    await allocateToGoal(env.ctx, goal.id, 12000, "2026-01-10", null);
    const p1 = await createPlannedPurchase(env.ctx, {
      title: "Zaino DEMO",
      currency: "EUR",
      estimatedMinor: 8000,
      goalId: goal.id,
    });
    await createPlannedPurchase(env.ctx, { title: "Idea DEMO", currency: "EUR" });
    await setPurchaseStatus(env.ctx, p1.id, "planned");
    const c = counting(env.db);
    const r = await runQuery(c.db, "planning");
    assert.equal(c.writes(), 0);
    assert.equal(r.ok, true, JSON.stringify(r));
    const data = (r as { data: { goals: Record<string, unknown>[]; purchases: Record<string, unknown>[] } })
      .data;
    assert.deepEqual(data.goals[0], {
      id: goal.id,
      name: "Fondo DEMO",
      currency: "EUR",
      targetMinor: 10000,
      deadline: null,
      status: "active", // dichiarato: non cambiato d'ufficio...
      allocatedMinor: 12000,
      remainingMinor: 0,
      progressBp: 12000,
      reached: true, // ...anche se l'avanzamento calcolato ha raggiunto l'obiettivo
    });
    const byTitle = Object.fromEntries(data.purchases.map((p) => [p.title, p]));
    assert.equal(byTitle["Zaino DEMO"]?.estimatedMinor, 8000);
    assert.equal(byTitle["Idea DEMO"]?.estimatedMinor, null); // stima mancante
    assert.equal(byTitle["Zaino DEMO"]?.status, "planned");
    for (const p of data.purchases) {
      assert.deepEqual(Object.keys(p).sort(), [
        "currency",
        "estimatedMinor",
        "goalId",
        "id",
        "linkedToTransaction",
        "priority",
        "status",
        "title",
      ]);
    }
  });
});
