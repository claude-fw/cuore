/**
 * Dossier derivati (Revolut v3, staging MPS) e saldo dopo l'operazione, con dati SINTETICI che ricalcano SOLO la
 * struttura dei file reali (intestazioni, tipi di cella): nessun valore reale.
 * Gira sia su SQLite via Node sia su D1 locale (CLOUD_FINANCE_TEST_DB=d1).
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { NodeSqliteDb } from "../src/db/node-sqlite.ts";
import { findTransferPairs } from "../src/domain/matching.ts";
import { importFile } from "../src/import/importer.ts";
import {
  MPS_STAGING_CSV,
  REVOLUT_V3_CONTO,
  REVOLUT_V3_DEPOSITO,
} from "../src/import/parsers/dossier-formats.ts";
import { getParser, listFormats } from "../src/import/parsers/registry.ts";
import { createTabularParser } from "../src/import/tabular/engine.ts";
import { type TabularSpec, validateTabularSpec } from "../src/import/tabular/spec.ts";
import { runQuery } from "../src/query/operations.ts";
import { createAccount } from "../src/services/accounts.ts";
import { detectLinks } from "../src/services/links.ts";
import { reconcileObservedPeriods } from "../src/services/reconciliation.ts";
import { BACKEND, count, setupWithAccounts, type TestEnv } from "./helpers.ts";
import { buildXlsx, type XCell } from "./xlsx-builder.ts";

const HEADER: XCell[] = [
  "Data",
  "Descrizione originale",
  "Direzione stimata",
  "Importo €",
  "Uscita €",
  "Entrata €",
  "Saldo dopo operazione €",
  "Carta (mascherata)",
  "Controparte (dal PDF)",
  "Riferimento",
  "ID transazione",
  "Classificazione provvisoria",
  "Dettagli originali",
  "Sezione origine",
  "Contesto utente / classificazione affinata",
];

/** Riga sintetica: importo con segno (0 = riga "Zero/ND"), saldo dopo l'operazione. */
type R = [date: string, desc: string, signed: number, balance: number, id: string];
const row = ([date, desc, signed, balance, id]: R): XCell[] => [
  date,
  desc,
  signed > 0 ? "Entrata" : signed < 0 ? "Uscita" : "Zero/ND",
  Math.abs(signed),
  signed < 0 ? -signed : null,
  signed > 0 ? signed : null,
  balance,
  desc.startsWith("Spesa") ? "999999******0000" : "",
  "",
  "",
  id,
  "Classificazione provvisoria DEMO (non deve classificare)",
  "",
  "Sezione DEMO",
  "",
];
const workbook = (sheets: Record<string, R[]>) =>
  buildXlsx([
    { name: "LEGGIMI", rows: [["Dossier DEMO"], ["solo dati inventati"]] },
    ...Object.entries(sheets).map(([name, rows]) => ({ name, rows: [HEADER, ...rows.map(row)] })),
  ]);

const CONTO: R[] = [
  ["2026-01-02", "Ricarica DEMO", 50, 50, "a-1"],
  ["2026-01-02", "Spesa DEMO", -12.5, 37.5, "a-2"],
  ["2026-01-03", "A EUR Conto deposito DEMO", -10, 27.5, "t-1"],
  ["2026-01-05", "Spesa DEMO", -2.5, 25, "a-3"],
];
const DEPOSITO: R[] = [
  ["2026-01-03", "Da EUR Conto DEMO", 10, 10, "t-1"],
  ["2026-01-04", "Movimento senza importo DEMO", 0, 10, "z-1"],
  ["2026-01-04", "Interessi DEMO", 0.01, 10.01, "i-1"],
];

async function env2(): Promise<TestEnv> {
  const env = await setupWithAccounts();
  await createAccount(env.ctx, {
    id: "deposito",
    name: "Deposito DEMO",
    type: "savings",
    currency: "EUR",
    holderRole: "owner",
  });
  return env;
}
const imp = (
  env: TestEnv,
  account: string,
  parserId: string,
  content: Uint8Array,
  fileName = "dossier.xlsx",
) => importFile(env.ctx, { accountId: account, parserId, fileName, content });

describe("saldo dopo l'operazione (balanceAfter): catena verificata", () => {
  it("specifiche dei dossier valide; balanceAfter incompatibile con commissioni separate", () => {
    for (const s of [REVOLUT_V3_CONTO, REVOLUT_V3_DEPOSITO, MPS_STAGING_CSV])
      assert.deepEqual(validateTabularSpec(s), [], s.id);
    const bad: TabularSpec = {
      ...REVOLUT_V3_CONTO,
      fields: {
        ...REVOLUT_V3_CONTO.fields,
        fee: { column: "Importo €", amountIncludesFee: false, costSign: "positive" },
      },
    };
    assert.ok(validateTabularSpec(bad).some((p) => /balanceAfter/.test(p)));
    const missing: TabularSpec = {
      ...REVOLUT_V3_CONTO,
      fields: { ...REVOLUT_V3_CONTO.fields, balanceAfter: { column: "Saldo" } },
    };
    assert.ok(validateTabularSpec(missing).some((p) => /balanceAfter: colonna "Saldo"/.test(p)));
  });

  it("importa movimenti e saldi di fine giornata (ultimo saldo del giorno); riga senza importo esclusa; riconciliazione ok", async () => {
    const env = await env2();
    const r = await imp(env, "conto", REVOLUT_V3_CONTO.id, workbook({ "Movimenti Titolare": CONTO }));
    assert.equal(r.status, "committed", JSON.stringify(r.issues));
    assert.deepEqual([r.counts.created, r.counts.balancesCreated], [4, 3]);
    assert.deepEqual(
      (
        await env.db.all<object>(
          "SELECT as_of_date, balance_minor FROM balance_observations WHERE account_id = 'conto' ORDER BY as_of_date",
        )
      ).map((x) => Object.assign({}, x)),
      [
        { as_of_date: "2026-01-02", balance_minor: 3750 },
        { as_of_date: "2026-01-03", balance_minor: 2750 },
        { as_of_date: "2026-01-05", balance_minor: 2500 },
      ],
    );
    const d = await imp(env, "deposito", REVOLUT_V3_DEPOSITO.id, workbook({ Deposito: DEPOSITO }));
    assert.deepEqual(
      [d.status, d.counts.created, d.counts.excluded, d.counts.balancesCreated],
      ["committed", 2, 1, 2],
    );
    const rec = await reconcileObservedPeriods(env.ctx, "conto");
    assert.deepEqual(
      rec.map((x) => x.status),
      ["ok", "ok"],
    );
    // la carta (mascherata) e' il tipo di operazione della fonte; vuota = nessuna carta
    assert.equal(await count(env.db, "transactions", "source_type = '999999******0000'"), 2);
    assert.equal(await count(env.db, "transactions", "account_id = 'conto' AND source_type IS NULL"), 2);
    // la classificazione provvisoria del dossier non classifica nulla
    assert.equal(await count(env.db, "classifications"), 0);
  });

  it("catena rotta, riga esclusa che cambia il saldo, righe fuori ordine: file rifiutato, nulla scritto", async () => {
    const env = await env2();
    const cases: [R[], string][] = [
      [[CONTO[0] as R, ["2026-01-02", "Spesa DEMO", -12.5, 37.4, "a-2"]], "balance_chain_break"],
      [[DEPOSITO[0] as R, ["2026-01-04", "Movimento nascosto DEMO", 0, 9, "z-1"]], "balance_chain_break"],
      [[CONTO[1] as R, ["2026-01-01", "Ricarica DEMO", 50, 87.5, "a-1"]], "balance_order"],
      [[["2026-01-02", "Senza saldo DEMO", 5, Number.NaN, "x"]], "missing_value"],
    ];
    for (const [rows, code] of cases) {
      const fixed = rows.map((x) =>
        Number.isNaN(x[3]) ? row(x).map((c, i) => (i === 6 ? null : c)) : row(x),
      );
      const content = buildXlsx([{ name: "Movimenti Titolare", rows: [HEADER, ...fixed] }]);
      const r = await imp(env, "conto", REVOLUT_V3_CONTO.id, content, `${code}.xlsx`);
      assert.equal(r.status, "rejected", code);
      assert.ok(
        r.issues.some((i) => i.code === code),
        `${code}: ${JSON.stringify(r.issues)}`,
      );
    }
    assert.deepEqual(
      [await count(env.db, "transactions"), await count(env.db, "balance_observations")],
      [0, 0],
    );
  });

  it("riga in attesa con saldo dichiarato: ambigua, rifiutata", async () => {
    const spec: TabularSpec = {
      ...REVOLUT_V3_CONTO,
      id: "esempio-pending",
      fields: {
        ...REVOLUT_V3_CONTO.fields,
        status: { column: "Direzione stimata", map: { Entrata: "pending", Uscita: "booked" } },
      },
    };
    const parser = createTabularParser(spec);
    const r = await parser.parseFile(workbook({ "Movimenti Titolare": CONTO.slice(0, 1) }), {
      accountCurrency: "EUR",
    });
    assert.ok(r.issues.some((i) => i.code === "balance_with_pending"));
  });
});

describe("dossier Revolut v3: idempotenza, registri separati, trasferimenti interni", () => {
  it("stesso file: duplicate_file; nuovo export con una riga in piu': solo quella creata, saldi esistenti invariati", async () => {
    const env = await env2();
    await imp(env, "conto", REVOLUT_V3_CONTO.id, workbook({ "Movimenti Titolare": CONTO }));
    const again = await imp(
      env,
      "conto",
      REVOLUT_V3_CONTO.id,
      workbook({ "Movimenti Titolare": CONTO }),
      "copia.xlsx",
    );
    assert.equal(again.status, "duplicate_file");
    const next = await imp(
      env,
      "conto",
      REVOLUT_V3_CONTO.id,
      workbook({ "Movimenti Titolare": [...CONTO, ["2026-01-06", "Spesa DEMO", -5, 20, "a-4"]] }),
      "successivo.xlsx",
    );
    assert.deepEqual(
      [
        next.status,
        next.counts.created,
        next.counts.unchanged,
        next.counts.balancesCreated,
        next.counts.balancesUnchanged,
      ],
      ["committed", 1, 4, 1, 3],
    );
    assert.equal(await count(env.db, "transactions"), 5);
    assert.equal(await count(env.db, "review_items", "kind = 'balance_conflict'"), 0);
  });

  it("le due gambe di un trasferimento hanno lo stesso ID in conti diversi: entrambe importate e proposte come trasferimento", async () => {
    const env = await env2();
    await imp(env, "conto", REVOLUT_V3_CONTO.id, workbook({ "Movimenti Titolare": CONTO }));
    await imp(env, "deposito", REVOLUT_V3_DEPOSITO.id, workbook({ Deposito: DEPOSITO }));
    assert.equal(await count(env.db, "transactions", "source_txn_id = 't-1'"), 2);
    assert.equal((await detectLinks(env.ctx)).transfersProposed, 1);
  });

  it("foglio sbagliato o mancante: rifiuto esplicito (le viste Carta/Verifiche non hanno un formato)", async () => {
    const env = await env2();
    const r = await imp(env, "conto", REVOLUT_V3_CONTO.id, workbook({ "Carta DEMO": CONTO }));
    assert.equal(r.status, "rejected");
    assert.ok(!listFormats().some((f) => /carta|verifiche/i.test(f.id)));
  });

  it("consultazione: i saldi del dossier sono osservati; verificato solo con riconciliazione ok del giorno", async () => {
    const env = await env2();
    await imp(env, "conto", REVOLUT_V3_CONTO.id, workbook({ "Movimenti Titolare": CONTO }));
    await reconcileObservedPeriods(env.ctx, "conto");
    const res = await runQuery(env.db, "balances", { asOf: "2026-01-05", accountIds: ["conto"] });
    assert.ok(res.ok);
    const line = (res.data as { accounts: { balanceMinor: number; verifiedAsOf: boolean }[] }).accounts[0];
    assert.deepEqual([line?.balanceMinor, line?.verifiedAsOf], [2500, true]);
  });
});

describe("staging MPS (CSV derivato dai PDF)", () => {
  const H =
    "source_id;booking_date;value_date;description_raw_redacted;direction;amount;currency;signed_amount;classification_status;category;counterparty;source_format;source_file;transaction_id_source;import_status";
  const line = (booking: string, value: string, signed: string, desc = "BONIFICO DEMO", currency = "EUR") =>
    `S1;${booking};${value};${desc};${signed.startsWith("-") ? "out" : "in"};${signed.replace("-", "")};${currency};${signed};unclassified;;;pdf;demo.pdf;;staging_only`;
  const csv = (lines: string[]) => new TextEncoder().encode(`﻿${[H, ...lines].join("\r\n")}\r\n`);

  it("BOM, ';', date DD/MM/YYYY; data operazione = valuta, data contabile separata; senza id due righe identiche restano due", async () => {
    const env = await setupWithAccounts();
    const r = await imp(
      env,
      "conto",
      MPS_STAGING_CSV.id,
      csv([
        line("13/11/2025", "13/11/2025", "-90.0"),
        line("13/11/2025", "13/11/2025", "100.0"),
        line("13/11/2025", "13/11/2025", "100.0"),
      ]),
      "mps.csv",
    );
    assert.equal(r.status, "committed", JSON.stringify(r.issues));
    assert.deepEqual(
      (
        await env.db.all<object>(
          "SELECT transaction_date, booking_date, amount_minor FROM transactions ORDER BY amount_minor, occurrence",
        )
      ).map((x) => Object.assign({}, x)),
      [
        { transaction_date: "2025-11-13", booking_date: "2025-11-13", amount_minor: -9000 },
        { transaction_date: "2025-11-13", booking_date: "2025-11-13", amount_minor: 10000 },
        { transaction_date: "2025-11-13", booking_date: "2025-11-13", amount_minor: 10000 },
      ],
    );
    // una versione dello staging con la data contabile vera non crea doppioni (identita' sulla valuta): il cambio
    // di una contabilizzata non viene applicato in silenzio, va in verifica (source_changed)
    const fixed = await imp(
      env,
      "conto",
      MPS_STAGING_CSV.id,
      csv([
        line("17/11/2025", "13/11/2025", "-90.0"),
        line("17/11/2025", "13/11/2025", "100.0"),
        line("17/11/2025", "13/11/2025", "100.0"),
      ]),
      "mps-corretto.csv",
    );
    assert.deepEqual([fixed.status, fixed.counts.created, fixed.counts.conflicts], ["committed", 0, 3]);
  });

  it("valuta diversa dal conto, importo non numerico, colonna in piu': rifiuto", async () => {
    const env = await setupWithAccounts();
    for (const content of [
      csv([line("01/04/2026", "01/04/2026", "-200.0", "BONIFICO DEMO", "USD")]),
      csv([line("01/04/2026", "01/04/2026", "-2OO.0")]),
      new TextEncoder().encode(`${H};extra\r\n${line("01/04/2026", "01/04/2026", "-200.0")};x\r\n`),
    ]) {
      const r = await imp(env, "conto", MPS_STAGING_CSV.id, content, `bad-${Math.random()}.csv`);
      assert.equal(r.status, "rejected");
    }
    assert.equal(await count(env.db, "transactions"), 0);
  });

  it("registro: formati dei dossier implementati ma dichiarati NON verificati su export bancari", () => {
    const ids = [
      "revolut-dossier-v3-conto",
      "revolut-dossier-v3-demo",
      "revolut-dossier-v3-deposito",
      "mps-staging-csv",
    ];
    for (const id of ids) {
      assert.equal(getParser(id).verifiedAgainstRealExports, false, id);
      assert.ok(listFormats().some((f) => f.id === id && f.status === "implementato"));
    }
  });
});

describe("trasferimenti interni con identificativo condiviso", () => {
  const t = (id: string, account: string, amount: number, sid: string | null) => ({
    id,
    account_id: account,
    amount_minor: amount,
    currency: "EUR",
    date: "2026-03-01",
    description: "Giroconto DEMO",
    counterparty_norm: null,
    source_txn_id: sid,
  });
  it("molti giroconti identici nello stesso giorno: abbinati per id; senza id restano ambigui; rifiuto rispettato", () => {
    const legs = [1, 2, 3].flatMap((n) => [
      t(`o${n}`, "conto", -100, `g${n}`),
      t(`i${n}`, "deposito", 100, `g${n}`),
    ]);
    const withIds = findTransferPairs(legs, new Set(), new Set());
    assert.deepEqual(withIds.proposals.map((p) => `${p.fromId}>${p.toId}`).sort(), [
      "o1>i1",
      "o2>i2",
      "o3>i3",
    ]);
    assert.deepEqual(withIds.ambiguous, []);
    const noIds = findTransferPairs(
      legs.map((x) => ({ ...x, source_txn_id: null })),
      new Set(),
      new Set(),
    );
    assert.deepEqual([noIds.proposals.length, noIds.ambiguous.length], [0, 3]);
    const rejected = findTransferPairs(legs, new Set(), new Set(["o1>i1"]));
    assert.ok(!rejected.proposals.some((p) => p.fromId === "o1" && p.toId === "i1"));
    // stesso id ma stesso conto o importi non opposti: nessuna proposta per id
    const odd = findTransferPairs(
      [t("a", "conto", -100, "x"), t("b", "conto", 100, "x")],
      new Set(),
      new Set(),
    );
    assert.deepEqual(odd.proposals, []);
  });
});

describe("prova del piano di importazione (scripts/import-plan.ts)", {
  skip: BACKEND === "d1" ? "lo script usa SQLite via Node" : false,
}, () => {
  it("piano coerente: esito COERENTE; saldo atteso diverso: BLOCCATO con codice 1; nessun database lasciato", () => {
    const dir = mkdtempSync(join(tmpdir(), "cloud-finance-plan-test-"));
    try {
      const file = join(dir, "dossier.xlsx");
      writeFileSync(file, workbook({ "Movimenti Titolare": CONTO }));
      const plan = (closing: number) => ({
        accounts: [
          { id: "conto", name: "Conto DEMO", type: "current", currency: "EUR", holderRole: "owner" },
        ],
        imports: [{ account: "conto", format: REVOLUT_V3_CONTO.id, file }],
        expect: { conto: { transactions: 4, closingDate: "2026-01-05", closingBalanceMinor: closing } },
      });
      const run = (closing: number) => {
        const p = join(dir, `piano-${closing}.json`);
        writeFileSync(p, JSON.stringify(plan(closing)));
        const r = spawnSync(
          process.execPath,
          [new URL("../scripts/import-plan.ts", import.meta.url).pathname, "--plan", p],
          {
            encoding: "utf8",
          },
        );
        return {
          code: r.status,
          out: JSON.parse(r.stdout) as { esito: string; failures: string[]; database: string },
        };
      };
      const ok = run(2500);
      assert.deepEqual([ok.code, ok.out.esito, ok.out.failures], [0, "COERENTE", []]);
      assert.match(ok.out.database, /eliminato/);
      const bad = run(2499);
      assert.deepEqual([bad.code, bad.out.esito], [1, "BLOCCATO"]);
      assert.ok(bad.out.failures.some((f) => /saldo finale/.test(f)));
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("importazione definitiva (scripts/import-plan.ts --commit)", {
  skip: BACKEND === "d1" ? "lo script usa SQLite via Node" : false,
}, () => {
  const SCRIPT = new URL("../scripts/import-plan.ts", import.meta.url).pathname;
  const run = (args: string[]) => {
    const r = spawnSync(process.execPath, [SCRIPT, ...args], { encoding: "utf8" });
    return { code: r.status, out: JSON.parse(r.stdout) as Record<string, unknown> };
  };
  const setupDir = (mode: number) => {
    const root = mkdtempSync(join(tmpdir(), "cloud-finance-commit-test-"));
    const base = join(root, "finance");
    mkdirSync(join(base, "piani"), { recursive: true });
    chmodSync(base, mode);
    const file = join(base, "dossier.xlsx");
    writeFileSync(file, workbook({ "Movimenti Titolare": CONTO }));
    const plan = (closing: number) => ({
      accounts: [{ id: "conto", name: "Conto DEMO", type: "current", currency: "EUR", holderRole: "other" }],
      imports: [{ account: "conto", format: REVOLUT_V3_CONTO.id, file: "../dossier.xlsx" }],
      expect: { conto: { transactions: 4, closingDate: "2026-01-05", closingBalanceMinor: closing } },
    });
    const planFile = (closing: number) => {
      const p = join(base, "piani", `piano-${closing}.json`);
      writeFileSync(p, JSON.stringify(plan(closing)));
      return p;
    };
    return { root, base, target: join(base, "db", "finanza.sqlite3"), planFile };
  };

  it("commit riuscito: file definitivo, backup e rapporto a 600, nessuna esecuzione fittizia nello storico", async () => {
    const d = setupDir(0o700);
    try {
      const r = run(["--plan", d.planFile(2500), "--commit", d.target]);
      assert.equal(r.code, 0, JSON.stringify(r.out));
      assert.equal(r.out.esito, "IMPORTATO");
      assert.ok(existsSync(d.target) && !existsSync(`${d.target}.in-corso`));
      for (const f of [d.target, r.out.backup as string, r.out.rapporto as string])
        assert.equal(statSync(f).mode & 0o777, 0o600, f);
      assert.equal(statSync(join(d.base, "db")).mode & 0o777, 0o700);
      // backup e rapporto FUORI dalla cartella del database: annullare l'importazione non li tocca
      for (const f of [r.out.backup as string, r.out.rapporto as string])
        assert.ok(!f.startsWith(`${join(d.base, "db")}/`), `fuori da db/: ${f}`);
      assert.equal(statSync(join(d.base, "backup")).mode & 0o777, 0o700);
      assert.equal(statSync(join(d.base, "report")).mode & 0o777, 0o700);
      const db = new NodeSqliteDb(d.target, { readOnly: true });
      try {
        const rows = await db.all<{ status: string; n: number }>(
          "SELECT status, COUNT(*) AS n FROM import_runs GROUP BY status",
        );
        assert.deepEqual(
          rows.map((x) => ({ ...x })),
          [{ status: "committed", n: 1 }],
        );
      } finally {
        db.close();
      }
    } finally {
      rmSync(d.root, { recursive: true, force: true });
    }
  });

  it("database gia' esistente: rifiuto senza toccare il file; cartella con permessi aperti o piano incoerente: nessun file", () => {
    const d = setupDir(0o700);
    try {
      assert.equal(run(["--plan", d.planFile(2500), "--commit", d.target]).code, 0);
      const before = readFileSync(d.target);
      const again = run(["--plan", d.planFile(2500), "--commit", d.target]);
      assert.equal(again.code, 1);
      assert.match(String(again.out.esito), /BLOCCATO prima di scrivere/);
      assert.ok(Buffer.compare(before, readFileSync(d.target)) === 0, "file definitivo invariato");
    } finally {
      rmSync(d.root, { recursive: true, force: true });
    }
    const open = setupDir(0o755);
    try {
      const r = run(["--plan", open.planFile(2500), "--commit", open.target]);
      assert.equal(r.code, 1);
      assert.ok((r.out.problemiPercorso as string[]).some((p) => /permessi aperti/.test(p)));
      assert.ok(!existsSync(join(open.base, "db")));
    } finally {
      rmSync(open.root, { recursive: true, force: true });
    }
    const bad = setupDir(0o700);
    try {
      const r = run(["--plan", bad.planFile(2499), "--commit", bad.target]);
      assert.equal(r.code, 1);
      assert.ok(!existsSync(join(bad.base, "db")), "piano incoerente: nessun file creato");
    } finally {
      rmSync(bad.root, { recursive: true, force: true });
    }
  });

  it("percorsi non sicuri rifiutati: relativo, cartelle sincronizzate, repository, estensione", async () => {
    const { targetProblems } = await import("../scripts/import-plan.ts");
    const repo = new URL("..", import.meta.url).pathname.replace(/\/$/, "");
    assert.ok(targetProblems("relativo/x.sqlite3", repo).some((p) => /non assoluto/.test(p)));
    assert.ok(
      targetProblems("/Users/x/Library/Mobile Documents/c/x.sqlite3", repo).some((p) =>
        /sincronizzata/.test(p),
      ),
    );
    assert.ok(targetProblems("/Users/x/Downloads/db/x.sqlite3", repo).some((p) => /sincronizzata/.test(p)));
    assert.ok(targetProblems(join(repo, "db", "x.sqlite3"), repo).some((p) => /repository/.test(p)));
    assert.ok(targetProblems("/tmp/x/db/x.db", repo).some((p) => /\.sqlite3/.test(p)));
  });
});

describe("aggiornamento incrementale (scripts/import-plan.ts --apply)", {
  skip: BACKEND === "d1" ? "lo script usa SQLite via Node" : false,
}, () => {
  const SCRIPT = new URL("../scripts/import-plan.ts", import.meta.url).pathname;
  const run = (args: string[]) => {
    const r = spawnSync(process.execPath, [SCRIPT, ...args], { encoding: "utf8" });
    return { code: r.status, out: JSON.parse(r.stdout) as Record<string, unknown> };
  };
  const NEW_ROW: R = ["2026-01-06", "Spesa DEMO", -5, 20, "a-4"];
  async function setup() {
    const root = mkdtempSync(join(tmpdir(), "cloud-finance-apply-test-"));
    const base = join(root, "finance");
    mkdirSync(join(base, "piani"), { recursive: true });
    chmodSync(base, 0o700);
    writeFileSync(join(base, "v1.xlsx"), workbook({ "Movimenti Titolare": CONTO }));
    writeFileSync(join(base, "v2.xlsx"), workbook({ "Movimenti Titolare": [...CONTO, NEW_ROW] }));
    const write = (name: string, plan: object) => {
      const p = join(base, "piani", name);
      writeFileSync(p, JSON.stringify(plan));
      return p;
    };
    const imp = (file: string) => [{ account: "conto", format: REVOLUT_V3_CONTO.id, file: `../${file}` }];
    const initial = write("iniziale.json", {
      accounts: [{ id: "conto", name: "Conto DEMO", type: "current", currency: "EUR", holderRole: "other" }],
      imports: imp("v1.xlsx"),
      expect: { conto: { transactions: 4, closingDate: "2026-01-05", closingBalanceMinor: 2500 } },
    });
    const incremental = (closing: number, extra: object = {}) =>
      write(`incrementale-${closing}.json`, {
        accounts: [],
        imports: imp("v2.xlsx"),
        expect: { conto: { transactions: 5, closingDate: "2026-01-06", closingBalanceMinor: closing } },
        ...extra,
      });
    const target = join(base, "db", "finanza.sqlite3");
    assert.equal(run(["--plan", initial, "--commit", target]).code, 0);
    return { root, base, target, incremental };
  }
  const count = async (path: string, sql: string) => {
    const db = new NodeSqliteDb(path, { readOnly: true });
    try {
      return (await db.first<{ n: number }>(sql))?.n ?? -1;
    } finally {
      db.close();
    }
  };

  it("aggiunge solo il nuovo, backup prima dell'applicazione verificato e fuori da db/, rapporto; ripetuto: nulla di nuovo", async () => {
    const d = await setup();
    try {
      const recBefore = await count(d.target, "SELECT COUNT(*) AS n FROM reconciliations");
      const r = run(["--plan", d.incremental(2000), "--apply", d.target]);
      assert.equal(r.code, 0, JSON.stringify(r.out));
      assert.equal(r.out.esito, "AGGIORNATO");
      assert.equal((r.out.delta as Record<string, number>).transactions, 1);
      const backup = r.out.backupPrima as string;
      assert.ok(backup.startsWith(join(d.base, "backup")) && existsSync(backup));
      assert.equal(statSync(backup).mode & 0o777, 0o600);
      assert.equal(
        await count(backup, "SELECT COUNT(*) AS n FROM transactions"),
        4,
        "backup = stato precedente",
      );
      assert.equal(await count(d.target, "SELECT COUNT(*) AS n FROM transactions"), 5);
      assert.ok(existsSync(r.out.rapporto as string) && !existsSync(`${d.target}.in-corso`));
      const recAfter = await count(d.target, "SELECT COUNT(*) AS n FROM reconciliations");
      assert.equal(recAfter, recBefore + 1, "solo il periodo nuovo viene registrato");
      const again = run(["--plan", d.incremental(2000), "--apply", d.target]);
      assert.equal(again.code, 0, JSON.stringify(again.out));
      assert.equal(again.out.esito, "AGGIORNATO (nessun dato nuovo)");
      assert.equal(
        await count(d.target, "SELECT COUNT(*) AS n FROM reconciliations"),
        recAfter,
        "riconciliazioni non duplicate",
      );
      assert.equal(await count(d.target, "SELECT COUNT(*) AS n FROM transactions"), 5);
    } finally {
      rmSync(d.root, { recursive: true, force: true });
    }
  });

  it("piano incoerente, piano che crea conti, database aperto: rifiuto e database identico byte per byte", async () => {
    const d = await setup();
    try {
      const before = readFileSync(d.target);
      const bad = run(["--plan", d.incremental(1999), "--apply", d.target]);
      assert.equal(bad.code, 1);
      assert.match(String(bad.out.esito), /BLOCCATO prima di scrivere/);
      const withAccounts = run([
        "--plan",
        d.incremental(2000, {
          accounts: [{ id: "altro", name: "x", type: "current", currency: "EUR", holderRole: "other" }],
        }),
        "--apply",
        d.target,
      ]);
      assert.equal(withAccounts.code, 1);
      assert.ok((withAccounts.out.problemi as string[]).some((p) => /non deve creare/.test(p)));
      writeFileSync(`${d.target}-wal`, "");
      const open = run(["--plan", d.incremental(2000), "--apply", d.target]);
      assert.equal(open.code, 1);
      assert.ok((open.out.problemi as string[]).some((p) => /aperto o aggiornamento interrotto/.test(p)));
      assert.ok(Buffer.compare(before, readFileSync(d.target)) === 0, "database invariato");
      assert.ok(!existsSync(`${d.target}.in-corso`));
    } finally {
      rmSync(d.root, { recursive: true, force: true });
    }
  });
});

describe("foglio del conto Revolut scelto per prefisso (nome del titolare mai nel codice)", () => {
  // regressione 2026-10-24: il nome fisso "Movimenti Titolare" non trovava il foglio reale "Movimenti <titolare>"
  it("legge l'unico foglio 'Movimenti …' qualunque sia il nome del titolare; le altre viste non contano", async () => {
    const env = await env2();
    const r = await imp(
      env,
      "conto",
      REVOLUT_V3_CONTO.id,
      workbook({ "Movimenti Persona Demo": CONTO, "Carta 0000": CONTO, Deposito: DEPOSITO }),
    );
    assert.equal(r.status, "committed", JSON.stringify(r.issues));
    assert.equal(r.counts.created, 4);
  });

  it("nessuno o piu' fogli col prefisso: file rifiutato, nessuna scelta, nessun dato scritto", async () => {
    const env = await env2();
    const cases: Record<string, R[]>[] = [
      { "Conto DEMO": CONTO },
      { "Movimenti Uno": CONTO, "Movimenti Due": CONTO },
    ];
    for (const sheets of cases) {
      const r = await imp(env, "conto", REVOLUT_V3_CONTO.id, workbook(sheets));
      assert.equal(r.status, "rejected");
      assert.ok(
        r.issues.some((x) => x.code === "xlsx_malformed" && /atteso esattamente uno/.test(x.message)),
        JSON.stringify(r.issues),
      );
    }
    assert.equal(await count(env.db, "transactions"), 0);
  });

  it("specifica: sheet e sheetPrefix alternativi, prefisso non vuoto", () => {
    assert.deepEqual(validateTabularSpec(REVOLUT_V3_CONTO), []);
    const both: TabularSpec = {
      ...REVOLUT_V3_CONTO,
      input: { kind: "xlsx", sheet: "X", sheetPrefix: "Movimenti " },
    };
    assert.ok(validateTabularSpec(both).some((p) => /alternativi/.test(p)));
    const empty: TabularSpec = { ...REVOLUT_V3_CONTO, input: { kind: "xlsx", sheetPrefix: " " } };
    assert.ok(validateTabularSpec(empty).some((p) => /sheetPrefix vuoto/.test(p)));
  });
});
