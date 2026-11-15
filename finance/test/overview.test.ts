/**
 * Requisiti funzionali (dati SINTETICI): MPS conto di base, Revolut conto di spesa, deposito di risparmio, pocket
 * chiuso; stipendio, sostegno familiare con evidenza per periodo, trasferimenti MPS -> Revolut contati una volta,
 * profili dei conti dichiarati (mai dedotti), panoramica con data dell'ultimo aggiornamento.
 * Gira sia su SQLite via Node sia su D1 locale (CLOUD_FINANCE_TEST_DB=d1).
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { stmt } from "../src/db/types.ts";
import { evaluateRules, validateRuleFile } from "../src/domain/rules.ts";
import type { OverviewOutput } from "../src/query/consultation.ts";
import { runQuery } from "../src/query/operations.ts";
import {
  closeAccount,
  createAccount,
  createInstitution,
  listAccountProfiles,
  setAccountProfile,
} from "../src/services/accounts.ts";
import { addManualBalance } from "../src/services/balances.ts";
import { classifyTransactions, publishRuleSet } from "../src/services/classification.ts";
import { decideLink, detectLinks, listLinks } from "../src/services/links.ts";
import { importTx, row, setup, type TestEnv } from "./helpers.ts";

const PAPA = "Pagamento da parte di Papa Demo";
const external = (id: string, extra: object[], kind = "income", confidence = "high") => ({
  id,
  priority: 50,
  description: `sostegno familiare ${id}`,
  when: {
    all: [
      { field: "description", op: "contains", value: PAPA },
      { field: "direction", op: "equals", value: "in" },
      ...extra,
    ],
  },
  assign: { kind, category: "sostegno-familiare", confidence },
});
const RULES = {
  schema: "cloud-finance-rules/1",
  note: "regole SINTETICHE",
  categories: [
    { id: "stipendio", name: "Stipendio", parent: null, budget_group: null },
    { id: "sostegno-familiare", name: "Sostegno familiare", parent: null, budget_group: null },
    { id: "interessi", name: "Interessi", parent: null, budget_group: "savings" },
    { id: "regali", name: "Regali", parent: null, budget_group: null },
    { id: "acquisti", name: "Acquisti", parent: null, budget_group: "wants" },
  ],
  rules: [
    {
      id: "stipendio",
      priority: 50,
      description: "accredito stipendio",
      when: { all: [{ field: "description", op: "contains", value: "stipendio" }] },
      assign: { kind: "income", category: "stipendio", confidence: "high" },
    },
    // prima dell'apertura del conto di base il denaro di una fonte esterna non puo' venire da li'
    external("esterna-prima-base", [{ field: "date", op: "lte", value: "2025-12-14" }]),
    // periodo coperto dagli estratti del conto di base: cio' che non e' abbinato viene da altre fonti
    external("esterna-coperto", [
      { field: "date", op: "gte", value: "2025-12-15" },
      { field: "date", op: "lte", value: "2026-06-27" },
    ]),
    // estratti del conto di base mancanti: non verificabile -> da classificare
    external("esterna-non-coperto", [{ field: "date", op: "gte", value: "2026-07-01" }], "income", "low"),
    {
      id: "interessi",
      priority: 50,
      description: "interessi netti del deposito",
      when: { all: [{ field: "description", op: "starts_with", value: "interessi netti" }] },
      assign: { kind: "income", category: "interessi", confidence: "high" },
    },
    {
      id: "regalo",
      priority: 50,
      description: "regalo",
      when: {
        all: [
          { field: "description", op: "contains", value: "regalo" },
          { field: "direction", op: "equals", value: "in" },
        ],
      },
      assign: { kind: "income", category: "regali", confidence: "high" },
    },
    {
      id: "pos",
      priority: 50,
      description: "pagamento con carta",
      when: {
        all: [
          { field: "description", op: "starts_with", value: "pos demo" },
          { field: "direction", op: "equals", value: "out" },
        ],
      },
      assign: { kind: "expense", category: "acquisti", confidence: "high" },
    },
  ],
};

async function scenario(): Promise<TestEnv> {
  const env = await setup();
  await createInstitution(env.ctx, "mps", "Banca DEMO base");
  await createInstitution(env.ctx, "revolut", "Banca DEMO spesa");
  const acc = (id: string, type: "current" | "savings", institutionId: string) =>
    createAccount(env.ctx, { id, name: id, type, currency: "EUR", holderRole: "other", institutionId });
  await acc("mps", "current", "mps");
  await acc("rev", "current", "revolut");
  await acc("dep", "savings", "revolut");
  await acc("pocket", "savings", "revolut");
  await importTx(env, "mps", [
    row("2026-01-27", "575.40", "ACCREDITO STIPENDIO DEMO"),
    row("2026-02-11", "-200.00", "BONIFICO A FAVORE PAPA DEMO"),
    row("2026-02-20", "100.00", "REGALO NONNI DEMO"),
  ]);
  await importTx(env, "rev", [
    row("2025-11-10", "50.00", PAPA),
    row("2026-02-10", "200.00", PAPA),
    row("2026-03-05", "30.00", PAPA),
    row("2026-08-01", "40.00", PAPA),
    row("2026-02-12", "-25.00", "POS DEMO SUPERMERCATO"),
    row("2026-02-13", "-100.00", "A EUR Conto deposito DEMO"),
    row("2026-03-10", "-10.00", "Accredita pocket DEMO"),
    row("2026-03-20", "10.00", "Prelievo da pocket DEMO"),
  ]);
  await importTx(env, "dep", [
    row("2026-02-13", "100.00", "Da EUR Conto DEMO"),
    row("2026-02-14", "0.01", "Interessi netti pagati DEMO"),
  ]);
  await importTx(env, "pocket", [
    row("2026-03-10", "10.00", "Da conto DEMO"),
    row("2026-03-20", "-10.00", "Verso conto DEMO"),
  ]);
  await addManualBalance(env.ctx, "mps", "2026-02-28", 47540);
  await addManualBalance(env.ctx, "rev", "2026-08-31", 19500);
  await addManualBalance(env.ctx, "dep", "2026-02-28", 10001);
  await addManualBalance(env.ctx, "pocket", "2026-03-20", 0);
  await publishRuleSet(env.ctx, RULES);
  await classifyTransactions(env.ctx);
  await detectLinks(env.ctx);
  for (const l of await listLinks(env.ctx, "proposed")) await decideLink(env.ctx, l.id, "confirmed");
  return env;
}

const overview = async (env: TestEnv, input: object) => {
  const r = await runQuery(env.db, "overview", input);
  if (!r.ok) assert.fail(`${r.error.code}: ${r.error.message}`);
  return r.data as OverviewOutput;
};

describe("regole per data effettiva", () => {
  it("validazione: solo equals/gte/lte con data valida; confini inclusi", () => {
    const base = { schema: "cloud-finance-rules/1", note: null, categories: [] };
    const bad = (cond: object) =>
      validateRuleFile({
        ...base,
        rules: [
          {
            id: "r",
            priority: 1,
            description: "d",
            when: { all: [cond] },
            assign: { kind: "income", category: null, confidence: "high" },
          },
        ],
      }).problems;
    assert.ok(bad({ field: "date", op: "contains", value: "2026" }).length > 0);
    assert.ok(bad({ field: "date", op: "gte", value: "2026-02-30" }).length > 0);
    assert.deepEqual(bad({ field: "date", op: "gte", value: "2026-02-28" }), []);
    const { file } = validateRuleFile({
      ...base,
      rules: [
        {
          id: "r",
          priority: 1,
          description: "d",
          when: {
            all: [
              { field: "date", op: "gte", value: "2026-01-01" },
              { field: "date", op: "lte", value: "2026-01-31" },
            ],
          },
          assign: { kind: "income", category: null, confidence: "high" },
        },
      ],
    });
    const facts = (date: string | null) => ({
      account_id: "a",
      amount_minor: 1,
      currency: "EUR",
      description: "x",
      counterparty: null,
      date,
    });
    assert.equal(evaluateRules(file?.rules ?? [], facts("2026-01-01")).kind, "income");
    assert.equal(evaluateRules(file?.rules ?? [], facts("2026-01-31")).kind, "income");
    assert.equal(evaluateRules(file?.rules ?? [], facts("2026-02-01")).kind, "unknown");
    assert.equal(evaluateRules(file?.rules ?? [], facts(null)).kind, "unknown");
  });
});

describe("profili dei conti e chiusura", () => {
  it("profilo: unknown finche' non dichiarato; storico conservato, vale l'ultima dichiarazione; valori non validi rifiutati", async () => {
    const env = await scenario();
    const p0 = (await listAccountProfiles(env.ctx)).find((p) => p.accountId === "mps");
    assert.deepEqual(
      [p0?.formalHolder, p0?.primaryUser, p0?.supervision, p0?.declaredAt],
      ["unknown", "unknown", "unknown", null],
    );
    await setAccountProfile(env.ctx, "mps", {
      formalHolder: "unknown",
      primaryUser: "self",
      supervision: "parent",
      note: null,
    });
    await setAccountProfile(env.ctx, "mps", {
      formalHolder: "self",
      primaryUser: "self",
      supervision: "parent",
      note: "dichiarato",
    });
    const p1 = (await listAccountProfiles(env.ctx)).find((p) => p.accountId === "mps");
    assert.deepEqual([p1?.formalHolder, p1?.primaryUser, p1?.supervision], ["self", "self", "parent"]);
    await assert.rejects(
      setAccountProfile(env.ctx, "mps", {
        formalHolder: "papa" as "self",
        primaryUser: "self",
        supervision: "parent",
        note: null,
      }),
      /non valido/,
    );
    await assert.rejects(
      env.db.batch([stmt("UPDATE account_profiles SET formal_holder = 'parent'")]),
      /immutabil/,
    );
    const r = await runQuery(env.db, "accounts", {});
    assert.ok(r.ok);
    const info = (r.data as { id: string; profile: { formalHolder: string }; institutionId: string }[]).find(
      (a) => a.id === "mps",
    );
    assert.deepEqual([info?.profile.formalHolder, info?.institutionId], ["self", "mps"]);
  });

  it("chiusura: rifiutata con saldo non zero, saldo mancante o movimenti successivi; dopo, storico intatto e conto non attivo", async () => {
    const env = await scenario();
    const declared = (effectiveDate: string, reason = "prova") => ({
      effectiveDate,
      dateStatus: "declared" as const,
      reason,
    });
    await assert.rejects(closeAccount(env.ctx, "dep", declared("2026-02-28")), /saldo osservato/);
    await assert.rejects(closeAccount(env.ctx, "pocket", declared("2026-03-15")), /movimenti successivi/);
    await assert.rejects(closeAccount(env.ctx, "pocket", declared("2026-03-20", " ")), /motivo/);
    await assert.rejects(
      closeAccount(env.ctx, "pocket", { effectiveDate: "2026-03-20", dateStatus: "unknown", reason: "x" }),
      /null con stato unknown/,
    );
    await closeAccount(env.ctx, "pocket", declared("2026-03-20", "soldi spostati, pocket chiuso"));
    await assert.rejects(closeAccount(env.ctx, "pocket", declared("2026-03-21", "di nuovo")), /gia' chiuso/);
    const st = await runQuery(env.db, "data_status", { asOf: "2026-08-31" });
    assert.ok(st.ok);
    assert.ok(
      !(st.data as { anomalies: { accountId: string }[] }).anomalies.some((a) => a.accountId === "pocket"),
    );
    const ov = await overview(env, { from: "2025-11-01", to: "2026-08-31" });
    assert.ok(!ov.savings.accountIds.includes("pocket"));
    assert.equal(ov.lastUpdate.byAccount.find((a) => a.accountId === "pocket")?.closed, true);
    assert.equal(ov.money.byAccount.find((a) => a.accountId === "pocket")?.closed, true);
  });

  it("chiusura con data IGNOTA: nessuna data registrata, ultimo movimento separato; chiuso solo dopo l'ultimo movimento", async () => {
    const env = await scenario();
    const s = await closeAccount(env.ctx, "pocket", {
      effectiveDate: null,
      dateStatus: "unknown",
      reason: "dichiarato: soldi spostati e pocket chiuso, data non nota",
    });
    assert.deepEqual(
      [s.closed, s.closureDate, s.closureDateStatus, s.lastMovementOn],
      [true, null, "unknown", "2026-03-20"],
    );
    assert.equal(
      await env.db
        .first<{ c: string | null }>("SELECT closed_at AS c FROM accounts WHERE id = 'pocket'")
        .then((r) => r?.c),
      null,
    );
    const acc = await runQuery(env.db, "accounts", {});
    assert.ok(acc.ok);
    const info = (
      acc.data as {
        id: string;
        status: { closed: boolean; closureDate: string | null; lastMovementOn: string };
      }[]
    ).find((a) => a.id === "pocket");
    assert.deepEqual(
      [info?.status.closed, info?.status.closureDate, info?.status.lastMovementOn],
      [true, null, "2026-03-20"],
    );
    // nel giorno dell'ultimo movimento il conto era attivo; dopo, non e' piu' un conto attivo
    const onLast = await overview(env, { from: "2026-03-01", to: "2026-03-20" });
    assert.equal(onLast.money.byAccount.find((a) => a.accountId === "pocket")?.closed, false);
    const after = await overview(env, { from: "2026-03-01", to: "2026-08-31" });
    assert.equal(after.money.byAccount.find((a) => a.accountId === "pocket")?.closed, true);
    assert.ok(!after.savings.accountIds.includes("pocket"));
    // lo storico resta: i movimenti del pocket sono ancora nella panoramica del periodo
    assert.ok((after.currencies[0]?.internalTransfers.moved ?? 0) >= 1000);
    await assert.rejects(env.db.batch([stmt("DELETE FROM account_status_events")]), /immutabil/);
  });
});

describe("panoramica", () => {
  it("stipendio, sostegno familiare per evidenza, interessi, altre entrate, spesa reale, trasferimenti contati una volta", async () => {
    const env = await scenario();
    const ov = await overview(env, { from: "2025-11-01", to: "2026-08-31" });
    const eur = ov.currencies[0];
    assert.deepEqual(eur?.income, {
      salary: 57540,
      familySupport: 8000,
      interest: 1,
      other: 10000,
      total: 75541,
    });
    // il bonifico MPS -> genitore -> Revolut abbinato e' un trasferimento: non e' sostegno familiare ne' spesa
    assert.deepEqual(eur?.spending, { purchases: 2500, fees: 0, refunds: 0, net: 2500 });
    assert.deepEqual(eur?.internalTransfers, { moved: 20000 + 10000 + 1000 + 1000, intoSavingsNet: 10000 });
    // dopo la copertura degli estratti del conto di base: da classificare (regola a bassa confidenza)
    assert.deepEqual(eur?.unclassified, { inflow: 4000, outflow: 0, count: 1 });
    assert.deepEqual(ov.money.byInstitution, [
      { institutionId: "mps", currency: "EUR", observedSumMinor: 47540, complete: true },
      { institutionId: "revolut", currency: "EUR", observedSumMinor: 29501, complete: true },
    ]);
    assert.equal(ov.money.totals[0]?.observedSumMinor, 77041);
    assert.deepEqual(ov.savings.byCurrency, [{ currency: "EUR", observedSumMinor: 10001, complete: true }]);
    assert.equal(ov.lastUpdate.lastMovementOn, "2026-08-01");
    assert.ok(ov.notes.some((n) => /dati aggiornati al 2026-08-01/.test(n)));
    assert.ok(ov.notes.some((n) => /mps: movimenti disponibili solo fino al 2026-02-20/.test(n)));
    assert.ok(ov.notes.some((n) => /da classificare/.test(n)));
  });

  it("senza conferma del trasferimento MPS -> Revolut: nessun doppio conteggio come trasferimento, resta proposta", async () => {
    const env = await setup();
    await createAccount(env.ctx, {
      id: "mps",
      name: "mps",
      type: "current",
      currency: "EUR",
      holderRole: "other",
    });
    await createAccount(env.ctx, {
      id: "rev",
      name: "rev",
      type: "current",
      currency: "EUR",
      holderRole: "other",
    });
    await importTx(env, "mps", [row("2026-02-11", "-200.00", "BONIFICO A FAVORE PAPA DEMO")]);
    await importTx(env, "rev", [row("2026-02-10", "200.00", PAPA)]);
    await publishRuleSet(env.ctx, RULES);
    await classifyTransactions(env.ctx);
    assert.equal((await detectLinks(env.ctx)).transfersProposed, 1);
    const eur = (await overview(env, { from: "2026-02-01", to: "2026-02-28" })).currencies[0];
    // finche' non e' confermato, l'entrata segue la regola (sostegno) e l'uscita resta da classificare:
    // la panoramica lo segnala tra le verifiche aperte e i trasferimenti non sono gonfiati
    assert.deepEqual(
      [eur?.internalTransfers.moved, eur?.income.familySupport, eur?.unclassified.outflow],
      [0, 20000, 20000],
    );
    const [link] = await listLinks(env.ctx, "proposed");
    assert.ok(link);
    await decideLink(env.ctx, link.id, "confirmed");
    const after = (await overview(env, { from: "2026-02-01", to: "2026-02-28" })).currencies[0];
    assert.deepEqual(
      [after?.internalTransfers.moved, after?.income.familySupport, after?.unclassified.outflow],
      [20000, 0, 0],
    );
  });

  it("input non valido: periodo invertito o data mancante", async () => {
    const env = await setup();
    for (const input of [
      { from: "2026-02-01", to: "2026-01-01" },
      { from: "2026-01-01" },
      { from: "2026-01-01", to: "2026-01-31", x: 1 },
    ]) {
      const r = await runQuery(env.db, "overview", input);
      assert.ok(!r.ok && r.error.code === "invalid_input", JSON.stringify(input));
    }
  });
});
