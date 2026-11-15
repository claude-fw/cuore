/**
 * L1 (§4 del mandato): andata e ritorno a 1300 e 5000 movimenti sintetici, misura della CPU per blocco (misura, non
 * limite applicato da workerd), riservatezza su larga scala, ed esclusione delle tabelle non del motore Finance
 * (`_cf_*`, `cloud_*`, `schema_migrations`, `sqlite_*`). Solo Node, solo dati sintetici e inventati.
 */
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { createCtx } from "../../finance/src/context.ts";
import { migrate } from "../../finance/src/db/migrate.ts";
import { PENDING_MIGRATIONS } from "../../finance/src/db/migrations/007_manual_and_budgets.ts";
import { MIGRATIONS } from "../../finance/src/db/migrations/index.ts";
import { NodeSqliteDb } from "../../finance/src/db/node-sqlite.ts";
import { importFile } from "../../finance/src/import/importer.ts";
import { createAccount } from "../../finance/src/services/accounts.ts";
import {
  type BackupFile,
  createBackup,
  openBackup,
  readExportableTables,
  readSqliteSequence,
  recipientKid,
} from "../src/backup.ts";

const ALL_MIGRATIONS = [...MIGRATIONS, ...PENDING_MIGRATIONS];
const MARCATORE = "RISERVATO-INVENTATO";

/** CSV `cloud-finance-csv` deterministico: `n` righe uniche (data diversa ogni giorno), mai duplicate. */
function generaCsv(n: number): string {
  const righe = [
    "transaction_id,transaction_date,booking_date,status,amount,currency,description,counterparty,original_amount,original_currency",
  ];
  const inizio = Date.parse("2010-01-01");
  for (let i = 0; i < n; i++) {
    const data = new Date(inizio + i * 86_400_000).toISOString().slice(0, 10);
    const importo = ((i % 2 === 0 ? 1 : -1) * (1 + (i % 9999))) / 100;
    righe.push(`,${data},${data},booked,${importo.toFixed(2)},EUR,${MARCATORE} ${i},Controparte ${i % 97},,`);
  }
  return `${righe.join("\n")}\n`;
}

async function dbConMovimenti(n: number): Promise<NodeSqliteDb> {
  const dir = mkdtempSync(join(tmpdir(), "cuore-backup-scala-"));
  const db = new NodeSqliteDb(join(dir, "finance.sqlite"));
  await migrate(db, ALL_MIGRATIONS, () => new Date().toISOString());
  const ctx = createCtx(db);
  await createAccount(ctx, {
    id: "conto-prova",
    name: "Conto prova",
    type: "current",
    currency: "EUR",
    holderRole: "owner",
  });
  const r = await importFile(ctx, {
    accountId: "conto-prova",
    parserId: "cloud-finance-csv",
    fileName: "prova.csv",
    content: new TextEncoder().encode(generaCsv(n)),
  });
  assert.equal(r.status, "committed", JSON.stringify(r));
  return db;
}

async function rsaKeyPair() {
  return webcrypto.subtle.generateKey(
    { name: "RSA-OAEP", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true,
    ["encrypt", "decrypt"],
  ) as Promise<webcrypto.CryptoKeyPair>;
}

for (const n of [1300, 5000]) {
  describe(`andata e ritorno a larga scala: ${n} movimenti sintetici`, () => {
    it(`${n} movimenti: conteggi, impronta e sqlite_sequence coerenti; CPU per blocco misurata`, async () => {
      const db = await dbConMovimenti(n);
      try {
        const mac = await rsaKeyPair();
        const firma = await webcrypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, [
          "sign",
          "verify",
        ]);
        const kid = await recipientKid(mac.publicKey);
        const rowsPerBlock = 200;

        const tables = await readExportableTables(db);
        const sqliteSequence = await readSqliteSequence(db);
        const t0 = performance.now();
        const file: BackupFile = await createBackup({
          tables,
          sqliteSequence,
          versioneSchema: ALL_MIGRATIONS.length,
          writeSeq: 1,
          origine: "sintetico",
          recipients: [{ kid, publicKey: mac.publicKey }],
          signingKey: firma.privateKey,
          rowsPerBlock,
        });
        const ms = performance.now() - t0;
        const blocchiDati = file.blocchi.length - 1;
        console.log(
          `  [misura] ${n} movimenti, ${blocchiDati} blocchi da ${rowsPerBlock} righe: ${ms.toFixed(1)} ms totali, ${(ms / blocchiDati).toFixed(2)} ms/blocco (Node, non workerd)`,
        );

        const opened = await openBackup(file, {
          recipient: { kid, privateKey: mac.privateKey },
          verifyingKey: firma.publicKey,
          expectedVersioneSchema: ALL_MIGRATIONS.length,
        });
        const movimenti = opened.tables.find((t) => t.table === "transactions");
        assert.equal(movimenti?.rows.length, n);
        assert.deepEqual(opened.sqliteSequence, sqliteSequence);

        // riservatezza su larga scala: il marcatore non compare mai fuori dai blocchi cifrati (base64)
        const fuoriDaiBlocchi = JSON.stringify(file.header);
        assert.ok(!fuoriDaiBlocchi.includes(MARCATORE));
      } finally {
        db.close();
      }
    });
  });
}

describe("esclusione delle tabelle non del motore Finance", () => {
  it("_cf_*, cloud_*, schema_migrations e sqlite_sequence non entrano nel backup", async () => {
    const db = await dbConMovimenti(12);
    try {
      await db.batch([
        { sql: "CREATE TABLE _cf_interna (id INTEGER PRIMARY KEY) STRICT", params: [] },
        { sql: "CREATE TABLE cloud_coda_prova (id INTEGER PRIMARY KEY) STRICT", params: [] },
      ]);
      const nomi = (await readExportableTables(db)).map((t) => t.table);
      assert.ok(!nomi.includes("_cf_interna"));
      assert.ok(!nomi.includes("cloud_coda_prova"));
      assert.ok(!nomi.includes("schema_migrations"));
      assert.ok(!nomi.includes("sqlite_sequence"));
      assert.ok(nomi.includes("accounts"), "le tabelle del motore restano");
    } finally {
      db.close();
    }
  });
});
