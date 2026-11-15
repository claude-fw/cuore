/**
 * `ripristina-backup.ts` end-to-end (L1): backup vero dal Durable Object sintetico (workerd), scritto su disco in
 * una cartella temporanea, poi ripristinato con lo script in un database Node nuovo. Solo dati sintetici.
 */
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { before, describe, it } from "node:test";
import { migrate } from "../../finance/src/db/migrate.ts";
import { PENDING_MIGRATIONS } from "../../finance/src/db/migrations/007_manual_and_budgets.ts";
import { MIGRATIONS } from "../../finance/src/db/migrations/index.ts";
import { ripristina } from "../scripts/ripristina-backup.ts";
import type { BackupFile } from "../src/backup.ts";
import { bundle, startWorker } from "./helpers.ts";

const L = "http://localhost:8787";
const SCHEMA_VERSION = [...MIGRATIONS, ...PENDING_MIGRATIONS].length;

type KeyPair = { publicKey: webcrypto.CryptoKey; privateKey: webcrypto.CryptoKey };

async function rsaKeyPair(): Promise<KeyPair> {
  return webcrypto.subtle.generateKey(
    { name: "RSA-OAEP", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true,
    ["encrypt", "decrypt"],
  ) as Promise<webcrypto.CryptoKeyPair>;
}

describe("ripristina-backup.ts — end-to-end dal Durable Object vero al database Node (sintetico)", () => {
  let dir: string;
  let mac: KeyPair;
  let firma: KeyPair;
  let fileBackup: string;
  let fileChiave: string;
  let fileFirma: string;

  before(async () => {
    dir = mkdtempSync(join(tmpdir(), "cuore-ripristino-test-"));
    mac = await rsaKeyPair();
    const recupero = await rsaKeyPair();
    firma = await webcrypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, [
      "sign",
      "verify",
    ]);

    const script = await bundle("locale");
    const mf = await startWorker(
      script,
      {
        CUORE_STATO: "attivo",
        BACKUP_SIGNING_PRIVATE_JWK: JSON.stringify(await webcrypto.subtle.exportKey("jwk", firma.privateKey)),
        BACKUP_MAC_PUBLIC_JWK: JSON.stringify(await webcrypto.subtle.exportKey("jwk", mac.publicKey)),
        BACKUP_RECOVERY_PUBLIC_JWK: JSON.stringify(
          await webcrypto.subtle.exportKey("jwk", recupero.publicKey),
        ),
      },
      () => new Response("rete non disponibile nei test", { status: 599 }),
    );
    const r = await mf.dispatchFetch(`${L}/__locale/backup`);
    assert.equal(r.status, 200);
    const file = (await r.json()) as BackupFile;

    fileBackup = join(dir, "finance.cuorebak.json");
    fileChiave = join(dir, "mac-privata.jwk.json");
    fileFirma = join(dir, "firma-pubblica.jwk.json");
    writeFileSync(fileBackup, JSON.stringify(file));
    writeFileSync(fileChiave, JSON.stringify(await webcrypto.subtle.exportKey("jwk", mac.privateKey)));
    writeFileSync(fileFirma, JSON.stringify(await webcrypto.subtle.exportKey("jwk", firma.publicKey)));
  });

  it("ripristina con conteggi, integrità e schema coerenti", async () => {
    const destinazione = join(dir, "ripristinato.sqlite");
    const report = await ripristina({
      file: fileBackup,
      destinazione,
      chiavePrivata: fileChiave,
      chiaveFirma: fileFirma,
      versioneSchema: SCHEMA_VERSION,
    });
    assert.equal(report.integrityCheck, "ok");
    assert.equal(report.foreignKeyErrori, 0);
    assert.equal(report.schemaCoerente, true);
    assert.equal(report.sqliteSequenceCoerente, true);
    const conti = report.tabelle.find((t) => t.table === "accounts");
    assert.equal(conti?.righe, 3);

    // il motore, sullo stesso file ripristinato, legge gli stessi conteggi (integrazione vera, non solo i PRAGMA)
    const { NodeSqliteDb } = await import("../../finance/src/db/node-sqlite.ts");
    const db = new NodeSqliteDb(destinazione, { readOnly: true });
    assert.equal((await db.first<{ n: number }>("SELECT COUNT(*) AS n FROM accounts"))?.n, 3);
    db.close();
  });

  it("rifiuta di sovrascrivere una destinazione gia' esistente", async () => {
    const destinazione = join(dir, "gia-presente.sqlite");
    const db = new (await import("../../finance/src/db/node-sqlite.ts")).NodeSqliteDb(destinazione);
    await migrate(db, [...MIGRATIONS, ...PENDING_MIGRATIONS], () => new Date().toISOString());
    db.close();
    await assert.rejects(
      () =>
        ripristina({
          file: fileBackup,
          destinazione,
          chiavePrivata: fileChiave,
          chiaveFirma: fileFirma,
          versioneSchema: SCHEMA_VERSION,
        }),
      /gia' esistente/,
    );
  });

  it("rifiuta con la chiave di firma sbagliata", async () => {
    const altraFirma = await webcrypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, [
      "sign",
      "verify",
    ]);
    const fileAltraFirma = join(dir, "altra-firma.jwk.json");
    writeFileSync(
      fileAltraFirma,
      JSON.stringify(await webcrypto.subtle.exportKey("jwk", altraFirma.publicKey)),
    );
    await assert.rejects(() =>
      ripristina({
        file: fileBackup,
        destinazione: join(dir, "non-creato.sqlite"),
        chiavePrivata: fileChiave,
        chiaveFirma: fileAltraFirma,
        versioneSchema: SCHEMA_VERSION,
      }),
    );
  });
});
