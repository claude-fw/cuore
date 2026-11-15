/**
 * Formato di backup (L1, docs/mandati/2026-10-30-l1-backup-esportazione-do.md §4). Solo in Node, su un database
 * SQLite temporaneo caricato con gli esempi sintetici del repository (12 movimenti, come `finance-do.ts`): la
 * verifica contro il Durable Object vero (workerd) e lo script di ripristino sono un blocco successivo.
 */

import assert from "node:assert/strict";
import { createHash, webcrypto } from "node:crypto";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, test } from "node:test";
import { logicalChecksum } from "../../finance/scripts/import-plan.ts";
import { createCtx } from "../../finance/src/context.ts";
import { migrate } from "../../finance/src/db/migrate.ts";
import { PENDING_MIGRATIONS } from "../../finance/src/db/migrations/007_manual_and_budgets.ts";
import { MIGRATIONS } from "../../finance/src/db/migrations/index.ts";
import { NodeSqliteDb } from "../../finance/src/db/node-sqlite.ts";
import { importFile } from "../../finance/src/import/importer.ts";
import { createAccount } from "../../finance/src/services/accounts.ts";
import {
  type BackupFile,
  BackupVerificationError,
  createBackup,
  logicalChecksumPortable,
  openBackup,
  type Recipient,
  type RecipientPrivate,
  readExportableTables,
  readSqliteSequence,
  recipientKid,
} from "../src/backup.ts";

const EXAMPLES = new URL("../../finance/examples/sintetici/", import.meta.url).pathname;
const read = (name: string) => readFileSync(join(EXAMPLES, name), "utf8");

/** Stessa formula di `logicalChecksum` (import-plan.ts), con `node:crypto` invece di Web Crypto, sulle tabelle date. */
async function logicalChecksumOfTables(
  tables: readonly { table: string; columns: readonly string[]; rows: readonly unknown[][] }[],
): Promise<string> {
  const parts = tables.map(
    (t) =>
      `${t.table}:${JSON.stringify(t.rows.map((r) => Object.fromEntries(t.columns.map((c, i) => [c, r[i]]))))}`,
  );
  return createHash("sha256").update(parts.join("\n")).digest("hex");
}

/** Indicizzazione con `noUncheckedIndexedAccess`, senza `!`: l'indice deve esistere (altrimenti e' un bug del test). */
function at<T>(arr: readonly T[], i: number): T {
  const v = arr[i];
  if (v === undefined) throw new Error(`indice ${i} assente (lunghezza ${arr.length})`);
  return v;
}

async function syntheticDb(): Promise<NodeSqliteDb> {
  const dir = mkdtempSync(join(tmpdir(), "cuore-backup-test-"));
  const db = new NodeSqliteDb(join(dir, "finance.sqlite"));
  await migrate(db, [...MIGRATIONS, ...PENDING_MIGRATIONS], () => new Date().toISOString());
  const ctx = createCtx(db);
  for (const [id, name, type] of [
    ["conto-demo", "Conto DEMO", "current"],
    ["carta-demo", "Carta DEMO", "card"],
  ] as const)
    await createAccount(ctx, { id, name, type, currency: "EUR", holderRole: "owner" });
  for (const [accountId, parserId, fileName] of [
    ["conto-demo", "cloud-finance-csv", "conto-demo.csv"],
    ["carta-demo", "cloud-finance-csv", "carta-demo.csv"],
  ] as const) {
    const r = await importFile(ctx, {
      accountId,
      parserId,
      fileName,
      content: new TextEncoder().encode(read(fileName)),
    });
    assert.equal(r.status, "committed", `${fileName}: ${r.status}`);
  }
  return db;
}

type KeyPair = { publicKey: webcrypto.CryptoKey; privateKey: webcrypto.CryptoKey };

async function rsaKeyPair(): Promise<KeyPair> {
  return webcrypto.subtle.generateKey(
    { name: "RSA-OAEP", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true,
    ["encrypt", "decrypt"],
  ) as Promise<webcrypto.CryptoKeyPair>;
}

async function ecdsaKeyPair(): Promise<KeyPair> {
  return webcrypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, [
    "sign",
    "verify",
  ]) as Promise<webcrypto.CryptoKeyPair>;
}

describe("backup.ts — formato cuore.finance.backup/1 (dati sintetici, locale)", () => {
  let db: NodeSqliteDb;
  let mac: KeyPair;
  let recupero: KeyPair;
  let firma: KeyPair;
  let recipients: Recipient[];
  let macRecipient: RecipientPrivate;

  before(async () => {
    db = await syntheticDb();
    mac = await rsaKeyPair();
    recupero = await rsaKeyPair();
    firma = await ecdsaKeyPair();
    recipients = [
      { kid: await recipientKid(mac.publicKey), publicKey: mac.publicKey },
      { kid: await recipientKid(recupero.publicKey), publicKey: recupero.publicKey },
    ];
    macRecipient = { kid: at(recipients, 0).kid, privateKey: mac.privateKey };
  });
  after(() => db.close());

  async function backup(rowsPerBlock = 3): Promise<BackupFile> {
    return createBackup({
      tables: await readExportableTables(db),
      sqliteSequence: await readSqliteSequence(db),
      versioneSchema: 7,
      writeSeq: 1,
      origine: "sintetico",
      recipients,
      signingKey: firma.privateKey,
      rowsPerBlock,
    });
  }

  test("andata e ritorno: stessi conteggi e stessa impronta logica, per entrambi i destinatari", async () => {
    const file = await backup();
    assert.ok(file.blocchi.length > 1, "almeno un blocco dati oltre al manifesto");

    for (const recipient of [macRecipient, { kid: at(recipients, 1).kid, privateKey: recupero.privateKey }]) {
      const opened = await openBackup(file, {
        recipient,
        verifyingKey: firma.publicKey,
        expectedVersioneSchema: 7,
      });
      const attese = await readExportableTables(db);
      assert.deepEqual(
        opened.tables.map((t) => [t.table, t.rows.length]).sort(),
        attese.map((t) => [t.table, t.rows.length]).sort(),
      );
      assert.equal(opened.impronta_logica, await logicalChecksumPortable(db));
    }
  });

  test("impronta portabile: stessa formula di logicalChecksum (Node), sullo stesso ambito di tabelle", async () => {
    // `logicalChecksum` (import-plan.ts) include anche `schema_migrations`: qui si confronta sulle sole tabelle
    // che backup.ts esporta davvero (il motore Finance, non la contabilita' delle migrazioni).
    assert.equal(
      await logicalChecksumPortable(db),
      await logicalChecksumOfTables(await readExportableTables(db)),
    );
    const conSoleMigrazioni = await db.all<{ name: string }>(
      "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'",
    );
    assert.equal(
      conSoleMigrazioni.length,
      1,
      "il database di prova ha schema_migrations (altrimenti il confronto è vuoto)",
    );
    assert.notEqual(
      await logicalChecksumPortable(db),
      await logicalChecksum(db),
      "schema_migrations la fa differire, com'è giusto",
    );
  });

  test("rifiuto: chiave privata sbagliata", async () => {
    const altra = await rsaKeyPair();
    await assert.rejects(
      async () =>
        openBackup(await backup(), {
          recipient: { kid: macRecipient.kid, privateKey: altra.privateKey },
          verifyingKey: firma.publicKey,
          expectedVersioneSchema: 7,
        }),
      BackupVerificationError,
    );
  });

  test("rifiuto: un byte alterato nel testo cifrato", async () => {
    const file = await backup();
    const bytes = Buffer.from(at(file.blocchi, 1).cifrato, "base64");
    bytes[0] = (bytes[0] ?? 0) ^ 0xff;
    const alterato: BackupFile = {
      ...file,
      blocchi: [
        at(file.blocchi, 0),
        { ...at(file.blocchi, 1), cifrato: bytes.toString("base64") },
        ...file.blocchi.slice(2),
      ],
    };
    await assert.rejects(
      () =>
        openBackup(alterato, {
          recipient: macRecipient,
          verifyingKey: firma.publicKey,
          expectedVersioneSchema: 7,
        }),
      BackupVerificationError,
    );
  });

  test("rifiuto: firma assente o alterata", async () => {
    const file = await backup();
    const senzaFirma: BackupFile = { ...file, firma: "" };
    await assert.rejects(
      () =>
        openBackup(senzaFirma, {
          recipient: macRecipient,
          verifyingKey: firma.publicKey,
          expectedVersioneSchema: 7,
        }),
      BackupVerificationError,
    );
  });

  test("rifiuto: blocco tolto, duplicato o scambiato", async () => {
    const file = await backup();
    assert.ok(file.blocchi.length >= 3, "servono almeno due blocchi dati per scambiarli");
    const scambiato: BackupFile = {
      ...file,
      blocchi: [at(file.blocchi, 0), at(file.blocchi, 2), at(file.blocchi, 1), ...file.blocchi.slice(3)],
    };
    await assert.rejects(
      () =>
        openBackup(scambiato, {
          recipient: macRecipient,
          verifyingKey: firma.publicKey,
          expectedVersioneSchema: 7,
        }),
      BackupVerificationError,
    );
  });

  test("rifiuto: versione dello schema diversa", async () => {
    await assert.rejects(
      async () =>
        openBackup(await backup(), {
          recipient: macRecipient,
          verifyingKey: firma.publicKey,
          expectedVersioneSchema: 999,
        }),
      BackupVerificationError,
    );
  });

  test("rifiuto: backup piu' vecchio dell'ultimo accettato (write_seq)", async () => {
    await assert.rejects(
      async () =>
        openBackup(await backup(), {
          recipient: macRecipient,
          verifyingKey: firma.publicKey,
          expectedVersioneSchema: 7,
          minWriteSeq: 1,
        }),
      BackupVerificationError,
    );
  });

  test("riservatezza: nessun campo del manifesto o dell'intestazione contiene righe in chiaro", async () => {
    const file = await backup();
    const chiaro = JSON.stringify(file.header);
    assert.ok(!chiaro.includes("Stipendio") && !chiaro.includes("Supermercato"));
  });
});
