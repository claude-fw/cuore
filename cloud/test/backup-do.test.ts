/**
 * Formato di backup (L1) contro il VERO Durable Object sintetico, in workerd (Miniflare), tramite la rotta solo
 * locale `/__locale/backup` (src/locale.ts → `POST /backup/esporta` nel DO, src/finance-do.ts). Chiavi di prova
 * passate come binding (mai un segreto reale, mai l'ingresso di produzione: index.ts non ha questa rotta).
 */
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { before, describe, it } from "node:test";
import { SYNTHETIC_QUERY_SCHEMA_VERSION } from "../../finance/src/query/operations.ts";
import { type BackupFile, openBackup, recipientKid } from "../src/backup.ts";
import { bundle, startWorker } from "./helpers.ts";

const L = "http://localhost:8787";

type KeyPair = { publicKey: webcrypto.CryptoKey; privateKey: webcrypto.CryptoKey };

async function rsaKeyPair(): Promise<KeyPair> {
  return webcrypto.subtle.generateKey(
    { name: "RSA-OAEP", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true,
    ["encrypt", "decrypt"],
  ) as Promise<webcrypto.CryptoKeyPair>;
}

let script = "";
let mac: KeyPair;
let recupero: KeyPair;
let firma: KeyPair;
let bindings: Record<string, string>;

before(async () => {
  script = await bundle("locale");
  mac = await rsaKeyPair();
  recupero = await rsaKeyPair();
  firma = await webcrypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, [
    "sign",
    "verify",
  ]);
  bindings = {
    CUORE_STATO: "attivo",
    BACKUP_SIGNING_PRIVATE_JWK: JSON.stringify(await webcrypto.subtle.exportKey("jwk", firma.privateKey)),
    BACKUP_MAC_PUBLIC_JWK: JSON.stringify(await webcrypto.subtle.exportKey("jwk", mac.publicKey)),
    BACKUP_RECOVERY_PUBLIC_JWK: JSON.stringify(await webcrypto.subtle.exportKey("jwk", recupero.publicKey)),
  };
});

const senzaRete = () => new Response("rete non disponibile nei test", { status: 599 });

describe("backup del Durable Object sintetico in workerd (L1, chiuso per difetto, dati sintetici)", () => {
  it("senza chiavi configurate: 503, nessun dato", async () => {
    const mf = await startWorker(script, { CUORE_STATO: "attivo" }, senzaRete);
    const r = await mf.dispatchFetch(`${L}/__locale/backup`);
    assert.equal(r.status, 503);
  });

  it("con le chiavi: file valido, apribile dal Mac con entrambi i destinatari, impronta coerente", async () => {
    const mf = await startWorker(script, bindings, senzaRete);
    const r = await mf.dispatchFetch(`${L}/__locale/backup`);
    assert.equal(r.status, 200);
    const file = (await r.json()) as BackupFile;
    assert.equal(file.header.schema, "cuore.finance.backup/1");
    assert.equal(file.header.origine, "sintetico");
    assert.ok(file.header.destinatari.length === 2);

    const kidMac = await recipientKid(mac.publicKey);
    const opened = await openBackup(file, {
      recipient: { kid: kidMac, privateKey: mac.privateKey },
      verifyingKey: firma.publicKey,
      expectedVersioneSchema: SYNTHETIC_QUERY_SCHEMA_VERSION,
    });
    const conti = opened.tables.find((t) => t.table === "accounts");
    assert.equal(conti?.rows.length, 3, "i 3 conti sintetici di seedSynthetic");
    const movimenti = opened.tables.find((t) => t.table === "transactions");
    assert.ok((movimenti?.rows.length ?? 0) > 0, "almeno un movimento importato");

    const kidRecupero = await recipientKid(recupero.publicKey);
    const opened2 = await openBackup(file, {
      recipient: { kid: kidRecupero, privateKey: recupero.privateKey },
      verifyingKey: firma.publicKey,
      expectedVersioneSchema: SYNTHETIC_QUERY_SCHEMA_VERSION,
    });
    assert.equal(opened2.impronta_logica, opened.impronta_logica);
  });

  it("versione dello schema diversa da quella del motore: rifiutato", async () => {
    const mf = await startWorker(script, bindings, senzaRete);
    const file = (await (await mf.dispatchFetch(`${L}/__locale/backup`)).json()) as BackupFile;
    const kidMac = await recipientKid(mac.publicKey);
    await assert.rejects(() =>
      openBackup(file, {
        recipient: { kid: kidMac, privateKey: mac.privateKey },
        verifyingKey: firma.publicKey,
        expectedVersioneSchema: SYNTHETIC_QUERY_SCHEMA_VERSION + 1,
      }),
    );
  });
});
