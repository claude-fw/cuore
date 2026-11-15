/**
 * Riconciliazione manuale ↔ banca (A4) con dati SINTETICI; migrazione v7 in attesa applicata dopo MIGRATIONS.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { migrate } from "../src/db/migrate.ts";
import { PENDING_MIGRATIONS } from "../src/db/migrations/007_manual_and_budgets.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import { stmt } from "../src/db/types.ts";
import { createAccount } from "../src/services/accounts.ts";
import { budgetStatus, setBudget } from "../src/services/budgets.ts";
import { publishRuleSet, setClassification } from "../src/services/classification.ts";
import { decideLink } from "../src/services/links.ts";
import { addManualTransaction, voidManualTransaction } from "../src/services/manual.ts";
import {
  confirmManualMatch,
  detectManualMatches,
  manualMatchState,
  rejectManualMatch,
  reverseManualMatch,
} from "../src/services/manual-matching.ts";
import { freshDb, importTx, row, type TestEnv, testCtx } from "./helpers.ts";

const RULES = {
  schema: "cloud-finance-rules/1",
  note: "SINTETICHE",
  categories: [{ id: "cibo", name: "Cibo", parent: null, budget_group: "needs" }],
  rules: [
    {
      id: "esempio",
      priority: 10,
      description: "non scatta",
      when: { all: [{ field: "description", op: "starts_with", value: "zzz-mai" }] },
      assign: { kind: "expense", category: "cibo", confidence: "high" },
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
  await createAccount(ctx, {
    id: "carta",
    name: "Carta DEMO",
    type: "card",
    currency: "EUR",
    holderRole: "owner",
  });
  await publishRuleSet(ctx, RULES);
  return { db, ctx };
}

async function manual(
  e: TestEnv,
  req: string,
  date: string,
  amount: number,
  account = "conto",
): Promise<string> {
  const r = await addManualTransaction(e.ctx, {
    requestId: req,
    accountId: account,
    transactionDate: date,
    amountMinor: amount,
    currency: "EUR",
    description: `Spesa ${req}`,
    confirmDuplicate: true,
  });
  return (r as { id: string }).id;
}

async function bankId(e: TestEnv, sourceId: string): Promise<string> {
  const r = await e.db.first<{ id: string }>("SELECT id FROM transactions WHERE source_txn_id = ?", [
    sourceId,
  ]);
  return r?.id as string;
}

async function proposals(
  e: TestEnv,
  manualId: string,
): Promise<{ id: string; from_txn_id: string; note: string }[]> {
  return e.db.all(
    "SELECT id, from_txn_id, note FROM transaction_links WHERE to_txn_id = ? AND status = 'proposed' ORDER BY id",
    [manualId],
  );
}

describe("riconciliazione manuale ↔ banca", () => {
  it("propone l'abbinamento univoco, mai confermato da solo; la conferma esclude il manuale (la banca resta)", async () => {
    const e = await env();
    const m = await manual(e, "m1", "2026-03-10", -1250);
    await importTx(e, "conto", [row("2026-03-12", "-12.50", "SUPERMERCATO DEMO", { id: "b1" })]);
    const counts = await detectManualMatches(e.ctx);
    assert.deepEqual(counts, { manualOpen: 1, proposed: 1, unique: 1, ambiguous: 0 });
    const [p] = await proposals(e, m);
    assert.equal(p?.from_txn_id, await bankId(e, "b1"));
    assert.equal(JSON.parse(p?.note as string).esito, "univoco");
    assert.equal((await manualMatchState(e.ctx, m)).state, "proposto");
    const open = await e.db.first<{ n: number }>(
      "SELECT count(*) AS n FROM review_items WHERE entity_id = ? AND status = 'open'",
      [m],
    );
    assert.equal(open?.n, 1);

    await setClassification(e.ctx, await bankId(e, "b1"), "expense", "cibo");
    await setClassification(e.ctx, m, "expense", "cibo");
    await setBudget(e.ctx, {
      scope: "total",
      target: null,
      validFrom: "2026-03",
      currency: "EUR",
      amountMinor: 5000,
    });
    assert.equal((await budgetStatus(e.ctx, "2026-03", "EUR")).lines[0]?.spentMinor, 2500); // doppio conteggio
    await confirmManualMatch(e.ctx, p?.id as string);
    assert.equal((await budgetStatus(e.ctx, "2026-03", "EUR")).lines[0]?.spentMinor, 1250); // contata una volta
    const st = await manualMatchState(e.ctx, m);
    assert.equal(st.state, "riconciliato");
    const closed = await e.db.first<{ n: number }>(
      "SELECT count(*) AS n FROM review_items WHERE entity_id = ? AND status = 'open'",
      [m],
    );
    assert.equal(closed?.n, 0);
    const audit = await e.db.all<{ action: string }>(
      "SELECT action FROM audit_log WHERE entity = 'transaction_link' ORDER BY id",
    );
    assert.deepEqual(
      audit.map((a) => a.action),
      ["confirm_manual_match"],
    );
  });

  it("ripetere il rilevamento non crea duplicati; un rifiuto non viene riproposto", async () => {
    const e = await env();
    const m = await manual(e, "m1", "2026-03-10", -1250);
    await importTx(e, "conto", [row("2026-03-11", "-12.50", "BAR DEMO", { id: "b1" })]);
    await detectManualMatches(e.ctx);
    assert.equal((await detectManualMatches(e.ctx)).proposed, 0);
    const [p] = await proposals(e, m);
    await assert.rejects(rejectManualMatch(e.ctx, p?.id as string, " "), /motivo/);
    await rejectManualMatch(e.ctx, p?.id as string, "non e' la stessa spesa");
    assert.equal((await detectManualMatches(e.ctx)).proposed, 0);
    assert.equal((await manualMatchState(e.ctx, m)).state, "aperto");
    const n = await e.db.first<{ n: number }>("SELECT count(*) AS n FROM transaction_links");
    assert.equal(n?.n, 1);
  });

  it("ambiguo: due movimenti bancari per un manuale, tutte le coppie proposte, la conferma scarta l'altra", async () => {
    const e = await env();
    const m = await manual(e, "m1", "2026-03-10", -500);
    await importTx(e, "conto", [
      row("2026-03-09", "-5.00", "BAR UNO", { id: "b1" }),
      row("2026-03-11", "-5.00", "BAR DUE", { id: "b2" }),
    ]);
    const c = await detectManualMatches(e.ctx);
    assert.deepEqual(c, { manualOpen: 1, proposed: 2, unique: 0, ambiguous: 1 });
    const ps = await proposals(e, m);
    assert.ok(ps.every((p) => JSON.parse(p.note).esito === "ambiguo"));
    assert.equal((await manualMatchState(e.ctx, m)).state, "ambiguo");
    const b2 = await bankId(e, "b2");
    const chosen = ps.find((p) => p.from_txn_id === b2) as { id: string };
    assert.ok(chosen !== undefined);
    const res = await confirmManualMatch(e.ctx, chosen.id);
    assert.equal(res.rejected.length, 1);
    assert.equal((await proposals(e, m)).length, 0);
    const st = await manualMatchState(e.ctx, m);
    assert.equal(st.state, "riconciliato");
    assert.equal((st as { bankId: string }).bankId, b2);
  });

  it("ambiguo: due manuali per un movimento; confermato uno, l'altro torna aperto senza revisione pendente", async () => {
    const e = await env();
    const m1 = await manual(e, "m1", "2026-03-10", -700);
    const m2 = await manual(e, "m2", "2026-03-11", -700);
    await importTx(e, "conto", [row("2026-03-12", "-7.00", "PIZZA DEMO", { id: "b1" })]);
    assert.deepEqual(await detectManualMatches(e.ctx), {
      manualOpen: 2,
      proposed: 2,
      unique: 0,
      ambiguous: 2,
    });
    const [p1] = await proposals(e, m1);
    const [p2] = await proposals(e, m2);
    await confirmManualMatch(e.ctx, p1?.id as string);
    await assert.rejects(confirmManualMatch(e.ctx, p2?.id as string), /gia' deciso/);
    assert.equal((await manualMatchState(e.ctx, m2)).state, "aperto");
    const open = await e.db.first<{ n: number }>(
      "SELECT count(*) AS n FROM review_items WHERE entity_id = ? AND status = 'open'",
      [m2],
    );
    assert.equal(open?.n, 0);
    assert.equal((await detectManualMatches(e.ctx)).proposed, 0); // la banca e' gia' usata
    await manual(e, "m3", "2026-03-12", -700); // nuovo manuale, stessa spesa: la banca non si riusa
    const again = await detectManualMatches(e.ctx);
    assert.equal(again.proposed, 0);
    assert.equal(again.manualOpen, 2);
  });

  it("fuori finestra, altro importo, altro conto o manuale annullato: nessuna proposta", async () => {
    const e = await env();
    await manual(e, "lontano", "2026-03-01", -900);
    await manual(e, "importo", "2026-03-10", -901);
    await manual(e, "conto", "2026-03-10", -902, "carta");
    const v = await manual(e, "annullato", "2026-03-10", -903);
    await voidManualTransaction(e.ctx, v, "errore");
    await importTx(e, "conto", [
      row("2026-03-05", "-9.00", "A", { id: "b1" }), // 4 giorni dopo «lontano»
      row("2026-03-10", "-9.00", "B", { id: "b2" }),
      row("2026-03-10", "-9.02", "C", { id: "b3" }),
      row("2026-03-10", "-9.03", "D", { id: "b4" }),
    ]);
    const c = await detectManualMatches(e.ctx);
    assert.equal(c.proposed, 0);
    assert.equal(c.manualOpen, 3);
  });

  it("la revoca con motivo riapre il manuale; il collegamento resta nello storico", async () => {
    const e = await env();
    const m = await manual(e, "m1", "2026-03-10", -1250);
    await importTx(e, "conto", [row("2026-03-10", "-12.50", "X", { id: "b1" })]);
    await detectManualMatches(e.ctx);
    const [p] = await proposals(e, m);
    await assert.rejects(reverseManualMatch(e.ctx, p?.id as string, "presto"), /confermato/);
    await confirmManualMatch(e.ctx, p?.id as string);
    await reverseManualMatch(e.ctx, p?.id as string, "era un'altra spesa");
    assert.equal((await manualMatchState(e.ctx, m)).state, "aperto");
    await assert.rejects(reverseManualMatch(e.ctx, p?.id as string, "di nuovo"), /gia' revocato/);
    await assert.rejects(e.db.batch([stmt("DELETE FROM link_reversals")]), /non si cancella/);
    const link = await e.db.first<{ status: string }>("SELECT status FROM transaction_links WHERE id = ?", [
      p?.id as string,
    ]);
    assert.equal(link?.status, "confirmed");
  });

  it("rifiuta la conferma di collegamenti che non sono abbinamenti manuale ↔ banca", async () => {
    const e = await env();
    await assert.rejects(confirmManualMatch(e.ctx, "nessuno"), /non trovato/);
    const m = await manual(e, "m1", "2026-03-10", -100);
    await importTx(e, "conto", [row("2026-03-10", "-1.00", "X", { id: "b1" })]);
    await detectManualMatches(e.ctx);
    const [p] = await proposals(e, m);
    await decideLink(e.ctx, p?.id as string, "rejected"); // decisione con il servizio generico: coerente
    await assert.rejects(confirmManualMatch(e.ctx, p?.id as string), /gia' deciso/);
  });
});
