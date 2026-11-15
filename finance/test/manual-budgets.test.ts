/**
 * Movimenti manuali e budget (migrazione v7, attiva in MIGRATIONS) con dati SINTETICI.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { migrate } from "../src/db/migrate.ts";
import { PENDING_MIGRATIONS } from "../src/db/migrations/007_manual_and_budgets.ts";
import { MIGRATIONS, SCHEMA_VERSION } from "../src/db/migrations/index.ts";
import { stmt } from "../src/db/types.ts";
import { createAccount } from "../src/services/accounts.ts";
import { budgetStatus, setBudget } from "../src/services/budgets.ts";
import { publishRuleSet, setClassification } from "../src/services/classification.ts";
import { excludedTransactionIds } from "../src/services/links.ts";
import {
  addManualTransaction,
  correctManualTransaction,
  manualHistory,
  voidManualTransaction,
} from "../src/services/manual.ts";
import { reverseManualMatch } from "../src/services/manual-matching.ts";
import { count, freshDb, importTx, row, type TestEnv, testCtx } from "./helpers.ts";

const RULES = {
  schema: "cloud-finance-rules/1",
  note: "SINTETICHE",
  categories: [
    { id: "cibo", name: "Cibo", parent: null, budget_group: "needs" },
    { id: "bar", name: "Bar", parent: "cibo", budget_group: "wants" },
    { id: "svago", name: "Svago", parent: null, budget_group: "wants" },
  ],
  rules: [
    {
      id: "esempio",
      priority: 10,
      description: "regola d'esempio che non scatta",
      when: { all: [{ field: "description", op: "starts_with", value: "zzz-mai" }] },
      assign: { kind: "expense", category: "svago", confidence: "high" },
    },
  ],
};

async function env(): Promise<TestEnv> {
  const db = await freshDb();
  const ctx = testCtx(db);
  await migrate(db, [...MIGRATIONS, ...PENDING_MIGRATIONS], ctx.now);
  await createAccount(ctx, {
    id: "conto",
    name: "Conto DEMO",
    type: "current",
    currency: "EUR",
    holderRole: "owner",
  });
  await publishRuleSet(ctx, RULES);
  return { db, ctx };
}

const spesa = (over: Partial<Parameters<typeof addManualTransaction>[1]> = {}) => ({
  requestId: "tel-0001",
  accountId: "conto",
  transactionDate: "2026-03-10",
  amountMinor: -450,
  currency: "EUR",
  description: "Caffe e cornetto",
  ...over,
});

describe("migrazione v7", () => {
  it("e' attiva; dal 05/11 anche la v8 (ricerca, A6): SCHEMA_VERSION 8, nessuna in coda", () => {
    assert.equal(SCHEMA_VERSION, 8);
    assert.deepEqual(PENDING_MIGRATIONS, []);
  });

  it(
    "un database v6 con un manuale e un doppione bancario migra a v7 senza perdite; manual_voids esclude " +
      "l'annullato dalle consultazioni, link_reversals rimette in gioco il doppione la cui revoca esiste",
    async () => {
      const db = await freshDb();
      const ctx = testCtx(db);
      await migrate(db, MIGRATIONS.slice(0, 6), ctx.now); // v1..v6: senza manual_voids / link_reversals
      await createAccount(ctx, {
        id: "conto",
        name: "Conto DEMO",
        type: "current",
        currency: "EUR",
        holderRole: "owner",
      });

      // movimento manuale inserito su v6 (confirmDuplicate: true evita di interrogare manual_voids, assente su v6)
      const manual = (await addManualTransaction(ctx, {
        requestId: "m1",
        accountId: "conto",
        transactionDate: "2026-03-10",
        amountMinor: -1250,
        currency: "EUR",
        description: "Spesa manuale v6",
        confirmDuplicate: true,
      })) as { id: string };

      // lo stesso movimento arriva dalla banca: doppione confermato gia' su v6 (servizio generico dei collegamenti)
      await importTx({ db, ctx }, "conto", [row("2026-03-10", "-12.50", "SUPERMERCATO DEMO", { id: "b1" })]);
      const bank = await db.first<{ id: string }>("SELECT id FROM transactions WHERE source_txn_id = ?", [
        "b1",
      ]);
      assert.ok(bank);
      const linkId = ctx.newId();
      await db.batch([
        stmt(
          `INSERT INTO transaction_links (id, type, from_txn_id, to_txn_id, status, origin, created_at, decided_at)
           VALUES (?, 'possible_duplicate', ?, ?, 'confirmed', 'user', ?, ?)`,
          linkId,
          bank.id,
          manual.id,
          ctx.now(),
          ctx.now(),
        ),
      ]);
      // EXCLUDED_IDS_SQL interroga manual_voids/link_reversals: su v6 (assenti) non e' ancora utilizzabile.

      // migrazione a v7 (e v8, ricerca): nessuna perdita di dati, la storia avanza solo delle versioni mancanti
      const res = await migrate(db, MIGRATIONS, ctx.now);
      assert.deepEqual(res.applied, [7, 8]);
      assert.equal(await count(db, "transactions"), 2, "nessuna perdita di dati nella migrazione");
      assert.deepEqual(await excludedTransactionIds(ctx), new Set([manual.id]), "escluso dopo la migrazione");

      // v7: manual_voids esclude un manuale annullato, indipendentemente da qualsiasi collegamento
      const toVoid = (await addManualTransaction(ctx, {
        requestId: "m2",
        accountId: "conto",
        transactionDate: "2026-03-11",
        amountMinor: -300,
        currency: "EUR",
        description: "Da annullare",
        confirmDuplicate: true,
      })) as { id: string };
      assert.equal((await excludedTransactionIds(ctx)).has(toVoid.id), false);
      await voidManualTransaction(ctx, toVoid.id, "inserito per errore");
      assert.equal(
        (await excludedTransactionIds(ctx)).has(toVoid.id),
        true,
        "manual_voids esclude il manuale annullato",
      );

      // v7: link_reversals rimette in gioco il doppione confermato (anche se risalente a v6)
      await assert.rejects(reverseManualMatch(ctx, linkId, " "), /motivo/);
      await reverseManualMatch(ctx, linkId, "abbinamento errato, risalente alla v6");
      assert.equal(
        (await excludedTransactionIds(ctx)).has(manual.id),
        false,
        "la revoca rimette in gioco il doppione",
      );
      assert.equal(
        (await db.first("SELECT status FROM transaction_links WHERE id = ?", [linkId])) !== null,
        true,
        "il collegamento resta nello storico, non viene toccato",
      );
    },
  );
});

describe("movimenti manuali", () => {
  it("inserisce, e' idempotente sulla stessa richiesta e rifiuta la richiesta riusata con altro contenuto", async () => {
    const e = await env();
    const first = await addManualTransaction(e.ctx, spesa());
    assert.equal(first.status, "created");
    const again = await addManualTransaction(e.ctx, spesa());
    assert.deepEqual(again, { status: "already_recorded", id: (first as { id: string }).id });
    await assert.rejects(addManualTransaction(e.ctx, spesa({ amountMinor: -500 })), /contenuto diverso/);
    const n = await e.db.first<{ n: number }>(
      "SELECT count(*) AS n FROM transactions WHERE origin = 'manual'",
    );
    assert.equal(n?.n, 1);
  });

  it("segnala i possibili doppioni senza scrivere e inserisce solo con conferma", async () => {
    const e = await env();
    await importTx(e, "conto", [row("2026-03-11", "-4.50", "BAR CENTRALE", { id: "b1" })]);
    const res = await addManualTransaction(e.ctx, spesa());
    assert.equal(res.status, "possible_duplicate");
    assert.equal((res as { candidates: unknown[] }).candidates.length, 1);
    const n0 = await e.db.first<{ n: number }>(
      "SELECT count(*) AS n FROM transactions WHERE origin = 'manual'",
    );
    assert.equal(n0?.n, 0);
    const ok = await addManualTransaction(e.ctx, spesa({ confirmDuplicate: true }));
    assert.equal(ok.status, "created");
  });

  it("valida conto, valuta, importo, data e descrizione", async () => {
    const e = await env();
    await assert.rejects(addManualTransaction(e.ctx, spesa({ accountId: "nessuno" })));
    await assert.rejects(addManualTransaction(e.ctx, spesa({ currency: "USD" })), /valuta/);
    await assert.rejects(addManualTransaction(e.ctx, spesa({ amountMinor: 0 })), /zero/);
    await assert.rejects(addManualTransaction(e.ctx, spesa({ transactionDate: "2026-02-30" })), /data/);
    await assert.rejects(addManualTransaction(e.ctx, spesa({ description: "  " })), /descrizione/);
    await assert.rejects(addManualTransaction(e.ctx, spesa({ requestId: "" })), /requestId/);
  });

  it("corregge con motivo e storico, poi annulla; nessuna cancellazione", async () => {
    const e = await env();
    const { id } = (await addManualTransaction(e.ctx, spesa())) as { id: string };
    await assert.rejects(correctManualTransaction(e.ctx, id, { amountMinor: -480 }, " "), /motivo/);
    assert.deepEqual(await correctManualTransaction(e.ctx, id, { amountMinor: -450 }, "nessun cambio"), {
      id,
      changed: false,
    });
    await correctManualTransaction(
      e.ctx,
      id,
      { amountMinor: -480, description: "Caffe, cornetto e acqua" },
      "scontrino",
    );
    const t = await e.db.first<{ amount_minor: number; description_norm: string }>(
      "SELECT amount_minor, description_norm FROM transactions WHERE id = ?",
      [id],
    );
    assert.equal(t?.amount_minor, -480);
    await voidManualTransaction(e.ctx, id, "inserita per errore");
    await assert.rejects(correctManualTransaction(e.ctx, id, { amountMinor: -1 }, "x"), /annullato/);
    await assert.rejects(voidManualTransaction(e.ctx, id, "di nuovo"), /annullato/);
    const h = await manualHistory(e.ctx, id);
    assert.deepEqual(
      h.map((x) => x.action),
      ["add_manual_transaction", "correct_manual_transaction", "void_manual_transaction"],
    );
    const before = h[1]?.before as { amount_minor: number } | undefined;
    assert.equal(before?.amount_minor, -450);
    assert.equal(h[1]?.reason, "scontrino");
    await assert.rejects(e.db.batch([stmt("DELETE FROM manual_voids")]), /non si cancella/);
    await assert.rejects(e.db.batch([stmt("DELETE FROM transactions")]), /nessuna cancellazione/);
  });

  it("non corregge ne' annulla i movimenti importati", async () => {
    const e = await env();
    await importTx(e, "conto", [row("2026-03-11", "-4.50", "BAR CENTRALE", { id: "b1" })]);
    const imp = await e.db.first<{ id: string }>("SELECT id FROM transactions WHERE origin = 'import'");
    await assert.rejects(
      correctManualTransaction(e.ctx, imp?.id as string, { amountMinor: -1 }, "x"),
      /importati/,
    );
    await assert.rejects(voidManualTransaction(e.ctx, imp?.id as string, "x"), /importati/);
  });

  it("un movimento in attesa diventa contabilizzato, mai il contrario", async () => {
    const e = await env();
    const { id } = (await addManualTransaction(e.ctx, spesa({ status: "pending" }))) as { id: string };
    const p = await e.db.first<{ status: string; booking_date: string | null }>(
      "SELECT status, booking_date FROM transactions WHERE id = ?",
      [id],
    );
    assert.equal(p?.status, "pending");
    assert.equal(p?.booking_date, null);
    await correctManualTransaction(
      e.ctx,
      id,
      { status: "booked", bookingDate: "2026-03-12" },
      "arrivata in banca",
    );
    const b = await e.db.first<{ status: string }>("SELECT status FROM transactions WHERE id = ?", [id]);
    assert.equal(b?.status, "booked");
  });
});

describe("budget", () => {
  async function withSpending(): Promise<TestEnv> {
    const e = await env();
    const add = async (req: string, date: string, amount: number, desc: string, cat: string | null) => {
      const r = (await addManualTransaction(
        e.ctx,
        spesa({
          requestId: req,
          transactionDate: date,
          amountMinor: amount,
          description: desc,
          confirmDuplicate: true,
        }),
      )) as { id: string };
      if (cat !== null) await setClassification(e.ctx, r.id, "expense", cat);
      return r.id;
    };
    await add("r1", "2026-03-02", -2000, "Spesa supermercato", "cibo");
    await add("r2", "2026-03-05", -350, "Bar", "bar");
    await add("r3", "2026-03-20", -1500, "Cinema", "svago");
    await add("r4", "2026-03-21", -999, "Non classificata", null);
    const voided = await add("r5", "2026-03-22", -10000, "Errore", "cibo");
    await voidManualTransaction(e.ctx, voided, "errore");
    await add("r6", "2026-04-01", -700, "Aprile", "cibo");
    await addManualTransaction(
      e.ctx,
      spesa({
        requestId: "r7",
        transactionDate: "2026-03-25",
        amountMinor: -300,
        description: "In attesa",
        status: "pending",
        confirmDuplicate: true,
      }),
    );
    return e;
  }

  it("calcola speso per categoria (con sottocategorie), gruppo e totale; il resto a parte", async () => {
    const e = await withSpending();
    await setBudget(e.ctx, {
      scope: "category",
      target: "cibo",
      validFrom: "2026-01",
      currency: "EUR",
      amountMinor: 2000,
    });
    await setBudget(e.ctx, {
      scope: "budget_group",
      target: "wants",
      validFrom: "2026-01",
      currency: "EUR",
      amountMinor: 3000,
    });
    await setBudget(e.ctx, {
      scope: "total",
      target: null,
      validFrom: "2026-03",
      currency: "EUR",
      amountMinor: 5000,
    });
    const s = await budgetStatus(e.ctx, "2026-03", "EUR");
    const line = (scope: string, target: string | null) =>
      s.lines.find((l) => l.scope === scope && l.target === target);
    assert.equal(line("category", "cibo")?.spentMinor, 2350); // 2000 + bar 350; l'annullata (100,00) esclusa
    assert.equal(line("category", "cibo")?.overBudget, true);
    assert.equal(line("budget_group", "wants")?.spentMinor, 1850); // bar 350 + cinema 1500
    assert.equal(line("total", null)?.spentMinor, 3850);
    assert.equal(line("total", null)?.remainingMinor, 1150);
    assert.equal(s.unclassifiedOutflowMinor, 999);
    assert.equal(s.pendingOutflowMinor, 300);
    const feb = await budgetStatus(e.ctx, "2026-02", "EUR");
    assert.equal(
      feb.lines.some((l) => l.scope === "total"),
      false,
    ); // il totale vale da marzo
  });

  it("versioni nel tempo: l'ultima vale, un budget tolto sparisce, lo storico resta", async () => {
    const e = await withSpending();
    await setBudget(e.ctx, {
      scope: "category",
      target: "cibo",
      validFrom: "2026-01",
      currency: "EUR",
      amountMinor: 2000,
    });
    await setBudget(e.ctx, {
      scope: "category",
      target: "cibo",
      validFrom: "2026-03",
      currency: "EUR",
      amountMinor: 3000,
    });
    await setBudget(e.ctx, {
      scope: "category",
      target: "cibo",
      validFrom: "2026-04",
      currency: "EUR",
      amountMinor: null,
    });
    assert.equal((await budgetStatus(e.ctx, "2026-03", "EUR")).lines[0]?.budgetMinor, 3000);
    assert.equal((await budgetStatus(e.ctx, "2026-04", "EUR")).lines.length, 0);
    const n = await e.db.first<{ n: number }>("SELECT count(*) AS n FROM budgets");
    assert.equal(n?.n, 3);
    await assert.rejects(e.db.batch([stmt("UPDATE budgets SET amount_minor = 1")]), /non si modifica/);
  });

  it("valida ambito, categoria, gruppo, mese, valuta e importo", async () => {
    const e = await env();
    const base = {
      scope: "category" as const,
      target: "cibo",
      validFrom: "2026-03",
      currency: "EUR",
      amountMinor: 100,
    };
    await assert.rejects(setBudget(e.ctx, { ...base, target: "inventata" }), /categoria inesistente/);
    await assert.rejects(setBudget(e.ctx, { ...base, scope: "budget_group", target: "lusso" }), /gruppo/);
    await assert.rejects(setBudget(e.ctx, { ...base, validFrom: "2026-13" }), /mese/);
    await assert.rejects(setBudget(e.ctx, { ...base, currency: "XXX" }), /valuta/);
    await assert.rejects(setBudget(e.ctx, { ...base, amountMinor: -5 }), /maggiore di zero/);
    await assert.rejects(setBudget(e.ctx, { ...base, scope: "total" }), /totale/);
    await assert.rejects(budgetStatus(e.ctx, "marzo", "EUR"), /mese/);
  });
});
