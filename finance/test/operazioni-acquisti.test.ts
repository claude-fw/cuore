/**
 * Operazioni «Da comprare» del ponte e del Worker (`aggiungi_acquisto`, `segna_acquisto` in scripts/operazioni.ts), dati
 * SINTETICI: la voce entra come idea, si toglie (cancelled) o si segna comprata solo con una transazione reale in uscita.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { OPS } from "../scripts/operazioni.ts";
import { ValidationError } from "../src/context.ts";
import { runQuery } from "../src/query/operations.ts";
import { addManualTransaction } from "../src/services/manual.ts";
import { setupWithAccounts } from "./helpers.ts";

const planning = async (db: Parameters<typeof runQuery>[0]) => {
  const r = await runQuery(db, "planning");
  assert.equal(r.ok, true);
  return (r as { data: { purchases: Record<string, unknown>[] } }).data.purchases;
};

describe("operazioni Da comprare", () => {
  it("aggiunge, toglie e segna comprato con la transazione", async () => {
    const env = await setupWithAccounts();
    const a = (await OPS.aggiungi_acquisto?.(
      env.ctx,
      { title: "Cassa DEMO", currency: "EUR", estimatedMinor: 8500, priority: 2 },
      {},
    )) as {
      id: string;
    };
    const b = (await OPS.aggiungi_acquisto?.(
      env.ctx,
      { title: "Lego DEMO", currency: "EUR", estimatedMinor: null },
      {},
    )) as {
      id: string;
    };
    let ps = await planning(env.db);
    assert.deepEqual(ps.map((p) => [p.title, p.status, p.estimatedMinor]).sort(), [
      ["Cassa DEMO", "idea", 8500],
      ["Lego DEMO", "idea", null],
    ]);
    await OPS.segna_acquisto?.(env.ctx, { id: b.id, stato: "cancelled" }, {});
    await assert.rejects(
      OPS.segna_acquisto?.(env.ctx, { id: a.id, stato: "purchased" }, {}) as Promise<unknown>,
      ValidationError,
    );
    const t = (await addManualTransaction(env.ctx, {
      requestId: "rid-acquisto-0001",
      accountId: "conto",
      transactionDate: "2026-01-10",
      amountMinor: -8500,
      currency: "EUR",
      description: "Cassa DEMO",
    } as never)) as { transaction?: { id: string }; id?: string };
    const tid = t.transaction?.id ?? t.id;
    assert.ok(tid);
    assert.deepEqual(
      await OPS.segna_acquisto?.(env.ctx, { id: a.id, stato: "purchased", transactionId: tid }, {}),
      {
        id: a.id,
        stato: "purchased",
      },
    );
    ps = await planning(env.db);
    assert.deepEqual(Object.fromEntries(ps.map((p) => [p.title, p.status])), {
      "Cassa DEMO": "purchased",
      "Lego DEMO": "cancelled",
    });
  });

  it("rifiuta titolo vuoto e stima non positiva", async () => {
    const env = await setupWithAccounts();
    await assert.rejects(
      OPS.aggiungi_acquisto?.(env.ctx, { title: " ", currency: "EUR" }, {}) as Promise<unknown>,
      ValidationError,
    );
    await assert.rejects(
      OPS.aggiungi_acquisto?.(
        env.ctx,
        { title: "X", currency: "EUR", estimatedMinor: 0 },
        {},
      ) as Promise<unknown>,
      ValidationError,
    );
  });
});
