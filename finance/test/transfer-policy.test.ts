/**
 * Politica di conferma dei trasferimenti del piano (`confirmTransfers` in scripts/import-plan.ts), con dati SINTETICI in
 * cartelle temporanee: stesso comportamento nella prova, in --commit e in --apply. Requisito: una coppia tra istituti
 * diversi, o con istituto non dichiarato, non viene MAI confermata dalla politica (solo importo e data non dimostrano
 * un trasferimento tra conti propri; un id della fonte vale solo dentro la stessa fonte): resta una proposta.
 * Anche: reimportazione, pending -> contabilizzata, errore di preparazione senza applicazione parziale.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { NodeSqliteDb } from "../src/db/node-sqlite.ts";
import { BACKEND, csvBytes, row } from "./helpers.ts";

const SCRIPT = new URL("../scripts/import-plan.ts", import.meta.url).pathname;
const FORMAT = "cloud-finance-csv";
type Policy = "none" | "same_source_id" | "all_unique";

// Revolut (conto + deposito) e MPS: istituti diversi. Coppie possibili:
//   t-1 > t-1  stesso id della fonte, stesso istituto           (variante A: confermabile)
//   s-1 > s-2  solo importo e data, stesso istituto              (solo con all_unique)
//   b-1 > m-1  solo importo e data, istituti DIVERSI             (mai confermata dalla politica)
//   c-1 > d-2 | m-2  due candidati: ambigua                      (mai una proposta, mai confermata)
const FILES: Record<string, string[]> = {
  "rev-conto.csv": [
    row("2026-01-02", "100.00", "Ricarica DEMO", { id: "a-1" }),
    row("2026-01-03", "-10.00", "Giroconto DEMO", { id: "t-1" }),
    row("2026-01-04", "-20.00", "Bonifico DEMO", { id: "b-1" }),
    row("2026-01-06", "-30.00", "Uscita DEMO", { id: "c-1" }),
    row("2026-01-08", "-5.00", "Spostamento DEMO", { id: "s-1" }),
  ],
  "rev-deposito.csv": [
    row("2026-01-03", "10.00", "Giroconto DEMO", { id: "t-1" }),
    row("2026-01-06", "30.00", "Entrata DEMO", { id: "d-2" }),
    row("2026-01-08", "5.00", "Spostamento DEMO", { id: "s-2" }),
  ],
  "mps.csv": [
    row("2026-01-04", "20.00", "Accredito DEMO", { id: "m-1" }),
    row("2026-01-06", "30.00", "Accredito DEMO", { id: "m-2" }),
  ],
};
const ACCOUNT_OF: Record<string, [account: string, institution: string]> = {
  "rev-conto.csv": ["rev-conto", "rev"],
  "rev-deposito.csv": ["rev-deposito", "rev"],
  "mps.csv": ["mps-conto", "mps"],
};

function setup() {
  const root = mkdtempSync(join(tmpdir(), "cloud-finance-transfer-policy-"));
  const base = join(root, "finance");
  mkdirSync(join(base, "piani"), { recursive: true });
  chmodSync(base, 0o700);
  for (const [name, lines] of Object.entries(FILES)) writeFileSync(join(base, name), csvBytes(lines));
  const imports = Object.keys(FILES).map((f) => ({
    account: ACCOUNT_OF[f]?.[0],
    format: FORMAT,
    file: `../${f}`,
  }));
  const plan = (
    name: string,
    policy: Policy,
    opts: { institutions?: boolean; incremental?: boolean } = {},
  ) => {
    const withInst = opts.institutions ?? true;
    const p = join(base, "piani", name);
    writeFileSync(
      p,
      JSON.stringify({
        ...(opts.incremental
          ? { accounts: [] }
          : {
              institutions: withInst
                ? [
                    { id: "rev", name: "Revolut DEMO" },
                    { id: "mps", name: "MPS DEMO" },
                  ]
                : [],
              accounts: Object.values(ACCOUNT_OF).map(([id, inst]) => ({
                id,
                name: `${id} DEMO`,
                type: "current",
                currency: "EUR",
                holderRole: "owner",
                ...(withInst ? { institutionId: inst } : {}),
              })),
            }),
        confirmTransfers: policy,
        imports,
      }),
    );
    return p;
  };
  return { root, base, target: join(base, "db", "finanza.sqlite3"), plan };
}

const run = (args: string[]) => {
  const r = spawnSync(process.execPath, [SCRIPT, ...args], { encoding: "utf8" });
  if (r.stdout.trim() === "")
    assert.fail(`nessun rapporto JSON (codice ${r.status}): ${r.stderr.slice(0, 300)}`);
  return { code: r.status, out: JSON.parse(r.stdout) as Record<string, unknown> };
};

async function links(path: string): Promise<{ confirmed: string[]; proposed: string[]; ambiguous: number }> {
  const db = new NodeSqliteDb(path, { readOnly: true });
  try {
    const rows = await db.all<{ status: string; pair: string }>(
      `SELECT l.status, a.source_txn_id || '>' || b.source_txn_id AS pair FROM transaction_links l
       JOIN transactions a ON a.id = l.from_txn_id JOIN transactions b ON b.id = l.to_txn_id
       WHERE l.type = 'transfer' ORDER BY pair`,
    );
    const amb = await db.first<{ n: number }>(
      "SELECT COUNT(*) AS n FROM review_items WHERE kind = 'transfer_ambiguous' AND status = 'open'",
    );
    return {
      confirmed: rows.filter((r) => r.status === "confirmed").map((r) => r.pair),
      proposed: rows.filter((r) => r.status === "proposed").map((r) => r.pair),
      ambiguous: amb?.n ?? -1,
    };
  } finally {
    db.close();
  }
}

const EXPECTED: Record<Policy, { confirmed: string[]; proposed: string[] }> = {
  none: { confirmed: [], proposed: ["b-1>m-1", "s-1>s-2", "t-1>t-1"] },
  same_source_id: { confirmed: ["t-1>t-1"], proposed: ["b-1>m-1", "s-1>s-2"] },
  all_unique: { confirmed: ["s-1>s-2", "t-1>t-1"], proposed: ["b-1>m-1"] },
};

describe("politica dei trasferimenti del piano (scripts/import-plan.ts)", {
  skip: BACKEND === "d1" ? "lo script usa SQLite via Node" : false,
}, () => {
  for (const policy of ["none", "same_source_id", "all_unique"] as const) {
    it(`${policy}: stessa conferma nella prova e in --commit; mai tra istituti diversi, mai le ambigue`, async () => {
      const d = setup();
      try {
        const p = d.plan(`piano-${policy}.json`, policy);
        const dry = run(["--plan", p]);
        assert.equal(dry.code, 0, JSON.stringify(dry.out));
        const commit = run(["--plan", p, "--commit", d.target]);
        assert.equal(commit.code, 0, JSON.stringify(commit.out));
        const got = await links(d.target);
        assert.deepEqual({ confirmed: got.confirmed, proposed: got.proposed }, EXPECTED[policy]);
        assert.equal(got.ambiguous, 1, "c-1: due candidati, resta da verificare");
        const tp = dry.out.transferPolicy as Record<string, unknown>;
        assert.equal(tp.policy, policy);
        assert.equal(tp.confirmed, EXPECTED[policy].confirmed.length, "prova = commit");
        assert.deepEqual(
          tp.notConfirmedByInstitution,
          // b-1 > m-1 sarebbe confermata solo da all_unique (nessun id condiviso): e' quella trattenuta per l'istituto
          { differentInstitution: policy === "all_unique" ? 1 : 0, unknownInstitution: 0 },
        );
      } finally {
        rmSync(d.root, { recursive: true, force: true });
      }
    });
  }

  it("istituto non dichiarato: nessuna conferma automatica, nemmeno con lo stesso id della fonte (limite segnalato)", async () => {
    const d = setup();
    try {
      for (const policy of ["same_source_id", "all_unique"] as const) {
        const target = join(d.base, "db", `senza-istituto-${policy}.sqlite3`);
        const r = run([
          "--plan",
          d.plan(`senza-${policy}.json`, policy, { institutions: false }),
          "--commit",
          target,
        ]);
        assert.equal(r.code, 0, JSON.stringify(r.out));
        const got = await links(target);
        assert.deepEqual(got.confirmed, [], policy);
        assert.deepEqual(got.proposed, ["b-1>m-1", "s-1>s-2", "t-1>t-1"], policy);
        assert.deepEqual((r.out.transferPolicy as Record<string, unknown>).notConfirmedByInstitution, {
          differentInstitution: 0,
          unknownInstitution: policy === "all_unique" ? 3 : 1,
        });
      }
    } finally {
      rmSync(d.root, { recursive: true, force: true });
    }
  });

  it("--apply: reimportazione degli stessi file senza doppioni; la politica conferma solo dentro lo stesso istituto", async () => {
    const d = setup();
    try {
      assert.equal(run(["--plan", d.plan("iniziale.json", "none"), "--commit", d.target]).code, 0);
      assert.deepEqual((await links(d.target)).confirmed, []);
      const r = run([
        "--plan",
        d.plan("incrementale.json", "all_unique", { incremental: true }),
        "--apply",
        d.target,
      ]);
      assert.equal(r.code, 0, JSON.stringify(r.out));
      assert.equal(r.out.esito, "AGGIORNATO (nessun dato nuovo)");
      assert.equal(
        (r.out.delta as Record<string, number>).transactions,
        0,
        "stessi file: nessun movimento nuovo",
      );
      const got = await links(d.target);
      assert.deepEqual({ confirmed: got.confirmed, proposed: got.proposed }, EXPECTED.all_unique);
      const again = run([
        "--plan",
        d.plan("incrementale.json", "all_unique", { incremental: true }),
        "--apply",
        d.target,
      ]);
      assert.equal(again.code, 0, JSON.stringify(again.out));
      assert.deepEqual(
        (await links(d.target)).confirmed,
        EXPECTED.all_unique.confirmed,
        "ripetuto: nulla cambia",
      );
    } finally {
      rmSync(d.root, { recursive: true, force: true });
    }
  });

  it("--apply: movimento in attesa che si contabilizza, aggiornato senza doppioni", async () => {
    const d = setup();
    try {
      const pend = (status: "pending" | "booked") => [
        row("2026-01-10", "-7.00", "Pagamento DEMO", {
          id: "p-1",
          status,
          booking: status === "booked" ? "2026-01-11" : "",
        }),
      ];
      writeFileSync(join(d.base, "p-v1.csv"), csvBytes(pend("pending")));
      writeFileSync(join(d.base, "p-v2.csv"), csvBytes(pend("booked")));
      const plan = (name: string, file: string, incremental: boolean) => {
        const p = join(d.base, "piani", name);
        writeFileSync(
          p,
          JSON.stringify({
            accounts: incremental
              ? []
              : [{ id: "conto", name: "Conto DEMO", type: "current", currency: "EUR", holderRole: "owner" }],
            imports: [{ account: "conto", format: FORMAT, file: `../${file}` }],
          }),
        );
        return p;
      };
      assert.equal(run(["--plan", plan("p1.json", "p-v1.csv", false), "--commit", d.target]).code, 0);
      const r = run(["--plan", plan("p2.json", "p-v2.csv", true), "--apply", d.target]);
      assert.equal(r.code, 0, JSON.stringify(r.out));
      const db = new NodeSqliteDb(d.target, { readOnly: true });
      try {
        const rows = await db.all<{ status: string; booking_date: string | null }>(
          "SELECT status, booking_date FROM transactions WHERE source_txn_id = 'p-1'",
        );
        assert.deepEqual(
          rows.map((x) => ({ ...x })),
          [{ status: "booked", booking_date: "2026-01-11" }],
        );
      } finally {
        db.close();
      }
    } finally {
      rmSync(d.root, { recursive: true, force: true });
    }
  });

  it("errore nella preparazione (file non valido): nessuna applicazione parziale in --apply e in --commit", async () => {
    const d = setup();
    try {
      assert.equal(run(["--plan", d.plan("iniziale.json", "none"), "--commit", d.target]).code, 0);
      const before = readFileSync(d.target);
      writeFileSync(
        join(d.base, "rotto.csv"),
        csvBytes([
          row("2026-01-12", "1.00", "Valida DEMO", { id: "v-1" }),
          row("2026-01-13", "non-un-importo", "Rotta DEMO", { id: "v-2" }),
        ]),
      );
      const p = join(d.base, "piani", "rotto.json");
      writeFileSync(
        p,
        JSON.stringify({
          accounts: [],
          confirmTransfers: "all_unique",
          imports: [{ account: "rev-conto", format: FORMAT, file: "../rotto.csv" }],
        }),
      );
      const r = run(["--plan", p, "--apply", d.target]);
      assert.equal(r.code, 1);
      assert.match(String(r.out.esito), /BLOCCATO prima di scrivere/);
      assert.ok(Buffer.compare(before, readFileSync(d.target)) === 0, "database identico byte per byte");
      assert.ok(!existsSync(`${d.target}.in-corso`));
      assert.deepEqual((await links(d.target)).confirmed, [], "nessuna conferma applicata");
      const pb = join(d.base, "piani", "nuovo-rotto-2.json");
      writeFileSync(
        pb,
        JSON.stringify({
          accounts: [{ id: "x", name: "X DEMO", type: "current", currency: "EUR", holderRole: "owner" }],
          imports: [{ account: "x", format: FORMAT, file: "../rotto.csv" }],
        }),
      );
      const other = join(d.base, "altro", "finanza.sqlite3");
      const bad = run(["--plan", pb, "--commit", other]);
      assert.equal(bad.code, 1);
      assert.ok(!existsSync(join(d.base, "altro")), "nessun file creato");
    } finally {
      rmSync(d.root, { recursive: true, force: true });
    }
  });
});
