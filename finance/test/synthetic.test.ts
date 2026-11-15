/**
 * Ambiente sintetico (D3a): isolamento dei percorsi, marcatore, migrazioni protette, schema per percorso. Tutto in
 * cartelle temporanee nuove; le cartelle reali non vengono mai aperte (repository e iCloud solo come percorsi rifiutati).
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  chmodSync,
  linkSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  symlinkSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { migrate } from "../src/db/migrate.ts";
import { MIGRATIONS, SCHEMA_VERSION } from "../src/db/migrations/index.ts";
import { NodeSqliteDb } from "../src/db/node-sqlite.ts";
import { runQuery, SYNTHETIC_QUERY_SCHEMA_VERSION } from "../src/query/operations.ts";
import { createSyntheticSession } from "../src/synthetic/generate.ts";
import {
  checkDataDir,
  closeSession,
  createEmptySession,
  type GuardRoots,
  openSyntheticForWrite,
  SYNTHETIC_DB,
  verifySynthetic,
} from "../src/synthetic/guard.ts";
import { testCtx } from "./helpers.ts";

const FINANCE = join(dirname(fileURLToPath(import.meta.url)), "..");
const REPO = join(FINANCE, "..");
const STUPIDO = join(
  process.env.HOME ?? "/",
  "Library",
  "Mobile Documents",
  "com~apple~CloudDocs",
  "Cartella-Privata",
);

function sandbox(): { base: string; roots: GuardRoots; fakeReal: string; done: () => void } {
  const base = realpathSync(mkdtempSync(join(tmpdir(), "cuore-prova-test-")));
  const fakeReal = join(base, "finto-reale");
  mkdirSync(fakeReal, { mode: 0o700 });
  return {
    base,
    roots: { allowedRoot: base, forbidden: [fakeReal] },
    fakeReal,
    done: () => rmSync(base, { recursive: true, force: true }),
  };
}

const rows = (path: string, sql: string) => {
  const db = new DatabaseSync(path, { readOnly: true });
  try {
    return db
      .prepare(sql)
      .all()
      .map((r) => ({ ...r }));
  } finally {
    db.close();
  }
};

const edit = (path: string, sql: string) => {
  const db = new DatabaseSync(path);
  try {
    db.exec(sql);
  } finally {
    db.close();
  }
};

describe("ambiente sintetico: generazione", () => {
  it("crea una sessione nuova, marcata, deterministica nei dati; non sovrascrive", async () => {
    const s = sandbox();
    try {
      const a = await createSyntheticSession(join(s.base, "a"), s.roots);
      const b = await createSyntheticSession(join(s.base, "b"), s.roots);
      assert.deepEqual(a.counts, { transactions: 12, balances: 2, classified: 12 });
      assert.notEqual(a.session.id, b.session.id);
      const q =
        "SELECT id, account_id, fingerprint, amount_minor, transaction_date FROM transactions ORDER BY id";
      assert.deepEqual(rows(join(a.realDir, SYNTHETIC_DB), q), rows(join(b.realDir, SYNTHETIC_DB), q));
      const v = await verifySynthetic(a.realDir, s.roots);
      assert.equal(v.schemaVersion, SYNTHETIC_QUERY_SCHEMA_VERSION);
      assert.deepEqual(rows(v.path, "SELECT value FROM meta WHERE key = 'dataset'"), [
        { value: "sintetico" },
      ]);
      await assert.rejects(createSyntheticSession(a.realDir, s.roots), /esiste gia'/);
    } finally {
      s.done();
    }
  });

  it("la sessione resta disponibile fino alla chiusura; chiusa, le scritture sono rifiutate", async () => {
    const s = sandbox();
    try {
      const g = await createSyntheticSession(join(s.base, "s"), s.roots);
      const o = await openSyntheticForWrite(g.realDir, { roots: s.roots });
      o.db.close();
      await closeSession(g.realDir, s.roots);
      await assert.rejects(openSyntheticForWrite(g.realDir, { roots: s.roots }), /sessione chiusa/);
      assert.equal((await verifySynthetic(g.realDir, s.roots)).session.stato, "chiusa");
    } finally {
      s.done();
    }
  });
});

describe("ambiente sintetico: percorsi", () => {
  it("senza cartella esplicita e assoluta: rifiuto", async () => {
    await assert.rejects(createEmptySession(undefined), /obbligatoria/);
    await assert.rejects(createEmptySession("relativa/prova"), /assoluto/);
    assert.throws(() => checkDataDir(""), /obbligatoria/);
    await assert.rejects(openSyntheticForWrite(undefined), /obbligatoria/);
  });

  it("dati reali (radice vietata), fuori dalla radice ammessa, la radice stessa: rifiuto", async () => {
    const s = sandbox();
    try {
      await assert.rejects(createEmptySession(join(s.fakeReal, "sessione"), { roots: s.roots }), /vietato/);
      assert.throws(() => checkDataDir(s.fakeReal, s.roots), /vietato/);
      assert.throws(() => checkDataDir(s.base, s.roots), /fuori dalla cartella temporanea/);
      const outside = sandbox();
      try {
        assert.throws(() => checkDataDir(join(outside.base, "finto-reale"), s.roots), /fuori/);
      } finally {
        outside.done();
      }
    } finally {
      s.done();
    }
  });

  it("repository e iCloud (radici predefinite): rifiuto", async () => {
    assert.throws(() => checkDataDir(join(FINANCE, "examples")), /vietato|fuori/);
    await assert.rejects(createEmptySession(join(REPO, "cartella-di-prova-mai-creata")), /vietato|fuori/);
    assert.throws(() => checkDataDir(STUPIDO), /vietato|fuori|inesistente/);
  });

  it("collegamenti simbolici e fisici: rifiuto", async () => {
    const s = sandbox();
    try {
      const g = await createSyntheticSession(join(s.base, "vera"), s.roots);
      symlinkSync(s.fakeReal, join(s.base, "verso-reale"));
      symlinkSync(g.realDir, join(s.base, "verso-sessione"));
      assert.throws(() => checkDataDir(join(s.base, "verso-reale"), s.roots), /simbolico/);
      assert.throws(() => checkDataDir(join(s.base, "verso-sessione"), s.roots), /simbolico/);
      await assert.rejects(
        createEmptySession(join(s.base, "verso-reale"), { roots: s.roots }),
        /esiste gia'/,
      );
      // un «database reale» (finto) collegato con un hard link dentro una sessione valida
      const real = join(s.fakeReal, "cloud-finance.sqlite3");
      const r = new NodeSqliteDb(real);
      await migrate(r, MIGRATIONS, testCtx(r).now);
      r.close();
      const h = await createSyntheticSession(join(s.base, "hard"), s.roots);
      rmSync(join(h.realDir, SYNTHETIC_DB));
      linkSync(real, join(h.realDir, SYNTHETIC_DB));
      chmodSync(join(h.realDir, SYNTHETIC_DB), 0o600);
      await assert.rejects(openSyntheticForWrite(h.realDir, { roots: s.roots }), /collegamenti fisici/);
      // permessi aperti
      chmodSync(g.realDir, 0o755);
      assert.throws(() => checkDataDir(g.realDir, s.roots), /permessi/);
    } finally {
      s.done();
    }
  });
});

describe("ambiente sintetico: marcatore e migrazioni", () => {
  it("marcatore assente o errato, o di un'altra sessione: nessuna apertura in scrittura", async () => {
    const s = sandbox();
    try {
      for (const [name, sql, re] of [
        ["assente", "DELETE FROM meta WHERE key = 'dataset'", /marcatore/],
        ["errato", "UPDATE meta SET value = 'reale' WHERE key = 'dataset'", /marcatore/],
        ["altra", "UPDATE meta SET value = 'altra' WHERE key = 'sessione'", /altra sessione/],
      ] as const) {
        const g = await createSyntheticSession(join(s.base, name), s.roots);
        edit(join(g.realDir, SYNTHETIC_DB), sql);
        await assert.rejects(verifySynthetic(g.realDir, s.roots), re);
        await assert.rejects(openSyntheticForWrite(g.realDir, { roots: s.roots }), re);
      }
    } finally {
      s.done();
    }
  });

  it("nessuna migrazione prima del marcatore: un database v6 senza marcatore resta v6", async () => {
    const s = sandbox();
    try {
      const g = await createSyntheticSession(join(s.base, "v6"), s.roots);
      const path = join(g.realDir, SYNTHETIC_DB);
      rmSync(path);
      const d = new NodeSqliteDb(path);
      await migrate(d, MIGRATIONS, testCtx(d).now); // come il reale: v6, nessun marcatore
      d.close();
      chmodSync(path, 0o600);
      await assert.rejects(openSyntheticForWrite(g.realDir, { roots: s.roots, migrate: true }), /marcatore/);
      assert.deepEqual(rows(path, "SELECT MAX(version) AS v FROM schema_migrations"), [
        { v: SCHEMA_VERSION },
      ]);
    } finally {
      s.done();
    }
  });

  it("le variabili CUORE_* non cambiano nulla e il codice sintetico non legge l'ambiente", async () => {
    const s = sandbox();
    const saved = { ...process.env };
    try {
      process.env.CUORE_MONEY_DB = join(s.fakeReal, "cloud-finance.sqlite3");
      process.env.CUORE_PROVA_DATI = s.fakeReal;
      const g = await createSyntheticSession(join(s.base, "env"), s.roots);
      assert.ok(g.realDir.startsWith(s.base));
      for (const f of ["src/synthetic/guard.ts", "src/synthetic/generate.ts", "scripts/prova.ts"]) {
        const src = readFileSync(join(FINANCE, f), "utf8");
        assert.equal(/process\.env|getenv|CUORE_[A-Z]/.test(src), false, f);
      }
    } finally {
      process.env = saved;
      s.done();
    }
  });
});

describe("schema per percorso", () => {
  it("nessuna migrazione in attesa (v8 attiva dal 05/11): reale e copia sintetica alla stessa versione; altri valori rifiutati", async () => {
    const s = sandbox();
    try {
      const real = new NodeSqliteDb(":memory:");
      await migrate(real, MIGRATIONS, testCtx(real).now);
      const q = (db: NodeSqliteDb, expected?: number) =>
        runQuery(
          db,
          "data_status",
          { asOf: "2026-02-15" },
          expected === undefined ? {} : { expectedSchemaVersion: expected },
        );
      assert.equal((await q(real)).ok, true);
      assert.equal(SYNTHETIC_QUERY_SCHEMA_VERSION, SCHEMA_VERSION);
      assert.equal(
        (await q(real, SYNTHETIC_QUERY_SCHEMA_VERSION)).ok,
        true,
        "stessa versione: nessun marcatore richiesto",
      );
      assert.match(JSON.stringify(await q(real, 99)), /non ammessa/);
      const g = await createSyntheticSession(join(s.base, "q"), s.roots);
      const o = await openSyntheticForWrite(g.realDir, { roots: s.roots });
      assert.equal((await q(o.db)).ok, true);
      assert.equal((await q(o.db, SYNTHETIC_QUERY_SCHEMA_VERSION)).ok, true);
      o.db.close();
      assert.equal(SCHEMA_VERSION, 8);
    } finally {
      s.done();
    }
  });
});

describe("processo di scrittura separato (scripts/prova.ts)", () => {
  const run = (args: string[], input?: string) =>
    spawnSync(process.execPath, [join(FINANCE, "scripts", "prova.ts"), ...args], { input, encoding: "utf8" });

  it("senza --dati: 64; sessione esistente: 2; scrittura e consultazione sulla copia", () => {
    assert.equal(run(["verifica"]).status, 64);
    assert.equal(run(["esegui"]).status, 64);
    const base = realpathSync(mkdtempSync(join(tmpdir(), "cuore-prova-cli-")));
    try {
      const dir = join(base, "s");
      assert.equal(run(["crea", "--dati", dir]).status, 0);
      assert.equal(run(["crea", "--dati", dir]).status, 2);
      const add = run(
        ["esegui", "--dati", dir],
        JSON.stringify({
          op: "aggiungi_manuale",
          input: {
            requestId: "r1",
            accountId: "conto-demo",
            transactionDate: "2026-02-10",
            amountMinor: -450,
            currency: "EUR",
            description: "Caffe",
          },
        }),
      );
      assert.equal(add.status, 0, add.stdout);
      assert.equal(JSON.parse(add.stdout).risultato.status, "created");
      const query = run(
        ["esegui", "--dati", dir],
        JSON.stringify({
          op: "consulta",
          input: { operazione: "data_status", input: { asOf: "2026-02-15" } },
        }),
      );
      assert.equal(JSON.parse(query.stdout).risultato.ok, true);
      assert.equal(run(["esegui", "--dati", join(FINANCE, "examples")], "{}").status, 2);
    } finally {
      rmSync(base, { recursive: true, force: true });
    }
  });
});
