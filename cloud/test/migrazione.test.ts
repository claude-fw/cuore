/**
 * M3 (solo prova, SOLO DATABASE SINTETICI): migrazione Finance Mac → Durable Object in workerd (Miniflare) → backup L1
 * → ripristino sul Mac, con le impronte logiche confrontate a ogni passaggio. Database creati dal generatore della
 * copia sintetica (`finance/src/synthetic/generate.ts`) in cartelle temporanee nuove; chiavi di prova generate qui;
 * token di servizio simulati o di prova. Nessuna rete: l'`outboundService` risponde 599.
 */
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { before, describe, it } from "node:test";
import type { Migration } from "../../finance/src/db/migrate.ts";
import { PENDING_MIGRATIONS } from "../../finance/src/db/migrations/007_manual_and_budgets.ts";
import { MIGRATIONS } from "../../finance/src/db/migrations/index.ts";
import { NodeSqliteDb, readImmutable } from "../../finance/src/db/node-sqlite.ts";
import { createSyntheticSession } from "../../finance/src/synthetic/generate.ts";
import { SYNTHETIC_DB } from "../../finance/src/synthetic/guard.ts";
import {
  inviaMigrazione,
  main,
  type PianoMigrazione,
  parseCli,
  preparaMigrazione,
} from "../scripts/migra-finance.ts";
import { ripristina } from "../scripts/ripristina-backup.ts";
import { type BackupFile, logicalChecksumFromTables, logicalChecksumPortable } from "../src/backup.ts";
import { AUD, bundle, EMAIL, startWorker, TEAM, type TestKey, testKey, token } from "./helpers.ts";

const L = "http://localhost:8787";
const PROD = "https://cuore.esempio.workers.dev";
const ALL = [...MIGRATIONS, ...PENDING_MIGRATIONS];
const MIGRA_ID = "migrazioneprova0001.access";
/** Token di servizio FITTIZIO simulato dall'ingresso locale (`SIMULATED_SERVICE` in src/locale.ts). */
const SIMULATED_SERVICE = {
  clientId: "macschoolsimulato0001.access",
  clientSecret: "segreto-simulato-mac-school-solo-locale-0001",
};
const senzaRete = () => new Response("rete non disponibile nei test", { status: 599 });

type Mf = Awaited<ReturnType<typeof startWorker>>;
// biome-ignore lint/suspicious/noExplicitAny: risposte JSON lette nei test
type Doc = Record<string, any>;

let localScript = "";
let prodScript = "";
let key: TestKey;
let dir: string;
let backupBindings: Record<string, string>;
let fileChiave: string;
let fileFirma: string;

const certs = (r: Request) =>
  r.url === `https://${TEAM}/cdn-cgi/access/certs` ? Response.json({ keys: [key.jwk] }) : senzaRete();

before(async () => {
  [localScript, prodScript] = await Promise.all([bundle("locale"), bundle("index")]);
  key = await testKey("chiave-team");
  dir = mkdtempSync(join(tmpdir(), "cuore-m3-test-"));
  const rsa = () =>
    webcrypto.subtle.generateKey(
      { name: "RSA-OAEP", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
      true,
      ["encrypt", "decrypt"],
    ) as Promise<webcrypto.CryptoKeyPair>;
  const [mac, recupero] = await Promise.all([rsa(), rsa()]);
  const firma = await webcrypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, [
    "sign",
    "verify",
  ]);
  const jwk = async (k: webcrypto.CryptoKey) => JSON.stringify(await webcrypto.subtle.exportKey("jwk", k));
  backupBindings = {
    BACKUP_SIGNING_PRIVATE_JWK: await jwk(firma.privateKey),
    BACKUP_MAC_PUBLIC_JWK: await jwk(mac.publicKey),
    BACKUP_RECOVERY_PUBLIC_JWK: await jwk(recupero.publicKey),
  };
  fileChiave = join(dir, "mac-privata.jwk.json");
  fileFirma = join(dir, "firma-pubblica.jwk.json");
  writeFileSync(fileChiave, await jwk(mac.privateKey));
  writeFileSync(fileFirma, await jwk(firma.publicKey));
});

let n = 0;
/** Database sintetico NUOVO (generatore D3a) in una cartella temporanea mai usata: percorso del file. */
async function sintetico(migrations: readonly Migration[]): Promise<string> {
  const s = await createSyntheticSession(join(dir, `sessione-${++n}`), undefined, migrations);
  return join(s.realDir, SYNTHETIC_DB);
}

const impronta = (path: string) => readImmutable(path, (db) => logicalChecksumPortable(db));

/** Ingresso locale con il DO in attesa dei dati del Mac e il token di servizio SIMULATO ammesso sulla migrazione. */
const localeMac = () =>
  startWorker(
    localScript,
    {
      CUORE_STATO: "attivo",
      CUORE_FINANCE_ORIGINE: "mac",
      ACCESS_SERVIZIO_MIGRAZIONE: SIMULATED_SERVICE.clientId,
      ...backupBindings,
    },
    senzaRete,
  );

const invia = (mf: Mf, piano: PianoMigrazione) =>
  inviaMigrazione(piano, {
    base: L,
    fetch: (u, i) => mf.dispatchFetch(u, i),
    clientId: SIMULATED_SERVICE.clientId,
    clientSecret: SIMULATED_SERVICE.clientSecret,
  });

async function stato(mf: Mf): Promise<Doc> {
  const login = await mf.dispatchFetch(`${L}/__locale/accedi?come=titolare`, { redirect: "manual" });
  const cookie = (login.headers.get("Set-Cookie") ?? "").split(";")[0] ?? "";
  return (await (
    await mf.dispatchFetch(`${L}/api/vetrina/stato`, { headers: { Cookie: cookie } })
  ).json()) as Doc;
}

async function andataERitorno(migrations: readonly Migration[]) {
  const sorgente = await sintetico(migrations);
  const impMac = await impronta(sorgente);
  const piano = await readImmutable(sorgente, (db) => preparaMigrazione(db, { righePerBlocco: 3 }));
  assert.equal(piano.manifesto.impronta_logica, impMac, "il manifesto porta l'impronta del Mac");
  assert.equal(piano.manifesto.versione_schema, migrations.length);
  assert.ok(piano.blocchi.length > 3, "piu' blocchi (3 righe ciascuno)");

  const mf = await localeMac();
  const prima = await stato(mf);
  assert.equal(prima.dati, "in_attesa_dei_dati_del_mac");
  assert.equal(prima.finance.movimenti, 0, "il DO nasce vuoto: nessun esempio caricato");

  const e = await invia(mf, piano);
  assert.equal(e.ok, true, JSON.stringify(e));
  assert.equal(e.risposta.impronta_logica, impMac, "impronta nel DO = impronta del Mac");
  assert.equal(e.risposta.impronta_coerente, true);

  const dopo = await stato(mf);
  const movimenti = piano.manifesto.tabelle.find((t) => t.table === "transactions")?.righe;
  assert.equal(dopo.finance.movimenti, movimenti);
  assert.equal(dopo.finance.integrita, "ok");
  assert.equal(dopo.finance.consultazione_ok, true, "il motore nel DO legge i dati arrivati");

  const r = await mf.dispatchFetch(`${L}/__locale/backup`);
  assert.equal(r.status, 200);
  const file = (await r.json()) as BackupFile;
  assert.equal(file.header.origine, "sintetico", "il marcatore del database sintetico viaggia con i dati");
  const fileBackup = join(dir, `backup-${n}.json`);
  writeFileSync(fileBackup, JSON.stringify(file));
  const destinazione = join(dir, `ripristinato-${n}.sqlite`);
  const report = await ripristina({
    file: fileBackup,
    destinazione,
    chiavePrivata: fileChiave,
    chiaveFirma: fileFirma,
    versioneSchema: ALL.length,
  });
  assert.equal(report.integrityCheck, "ok");
  assert.equal(report.foreignKeyErrori, 0);
  assert.equal(report.schemaCoerente, true);
  assert.equal(report.sqliteSequenceCoerente, true);
  const db = new NodeSqliteDb(destinazione);
  const impRipristino = await logicalChecksumPortable(db);
  db.close();
  assert.equal(impRipristino, impMac, "Mac → DO → backup → Mac: impronta logica uguale");
  return { mf, piano };
}

describe("M3: migrazione Finance Mac → Durable Object (solo database sintetici)", () => {
  it("andata e ritorno da un database v8 (schema attivo dal 05/11): impronte uguali; secondo invio rifiutato", async () => {
    const { mf, piano } = await andataERitorno(MIGRATIONS);
    const ancora = await invia(mf, piano);
    assert.equal(ancora.ok, false);
    assert.equal(ancora.risposta.errore, "migrazione_gia_completata");
  });

  it("andata e ritorno da un database v7 (reale non ancora migrato): accettato, l'indice si ricostruisce", async () => {
    await andataERitorno(ALL.filter((m) => m.version <= 7));
  });

  it("blocco alterato: impronta diversa e nulla scritto; poi l'invio corretto riesce", async () => {
    const sorgente = await sintetico(MIGRATIONS);
    const piano = await readImmutable(sorgente, (db) => preparaMigrazione(db, { righePerBlocco: 4 }));
    // un importo cambiato di 1 centesimo in un blocco di movimenti, manifesto (impronta) originale
    const i = piano.blocchi.findIndex((b) => b.tabella === "transactions");
    const b = piano.blocchi[i];
    assert.ok(b !== undefined);
    const col = b.colonne.indexOf("amount_minor");
    assert.ok(col >= 0);
    const righe = b.righe.map((r, k) => (k === 0 ? r.map((v, c) => (c === col ? Number(v) + 1 : v)) : r));
    const blocchi = piano.blocchi.map((x, k) => (k === i ? { ...x, righe } : x));
    const alterato: PianoMigrazione = { ...piano, blocchi };
    const mf = await localeMac();
    const e = await invia(mf, alterato);
    assert.equal(e.fase, "fine");
    assert.equal(e.status, 422);
    assert.equal(e.risposta.errore, "impronta_diversa");
    assert.equal((await stato(mf)).finance.movimenti, 0, "nessuna riga scritta nel motore");

    const nuovo = await readImmutable(sorgente, (db) => preparaMigrazione(db, { righePerBlocco: 4 }));
    const ok = await invia(mf, nuovo);
    assert.equal(ok.ok, true, JSON.stringify(ok));
    assert.equal(ok.risposta.impronta_logica, await impronta(sorgente));
  });

  it("dati coerenti con l'impronta ma chiave esterna rotta: transazione annullata, DO ancora vuoto", async () => {
    const sorgente = await sintetico(MIGRATIONS);
    const piano = await readImmutable(sorgente, (db) => preparaMigrazione(db, { righePerBlocco: 4 }));
    const i = piano.blocchi.findIndex((b) => b.tabella === "transactions");
    const b = piano.blocchi[i];
    assert.ok(b !== undefined);
    const col = b.colonne.indexOf("account_id");
    const righe = b.righe.map((r, k) =>
      k === 0 ? r.map((v, c) => (c === col ? "conto-inesistente" : v)) : r,
    );
    const blocchi = piano.blocchi.map((x, k) => (k === i ? { ...x, righe } : x));
    // impronta ricalcolata sulle righe rotte: passa il primo controllo, deve fermarlo la transazione
    const tabelle = piano.manifesto.tabelle.map((t) => ({
      table: t.table,
      columns: t.columns,
      rows: blocchi.filter((x) => x.tabella === t.table).flatMap((x) => x.righe as unknown[][]),
    }));
    const manifesto = { ...piano.manifesto, impronta_logica: await logicalChecksumFromTables(tabelle) };
    const mf = await localeMac();
    const e = await invia(mf, { ...piano, manifesto, blocchi });
    assert.equal(e.status, 422);
    assert.equal(e.risposta.motivo, "chiavi_esterne_non_valide");
    const s = await stato(mf);
    assert.equal(s.finance.movimenti, 0, "nessuna riga rimasta: rollback completo");
    assert.equal(s.finance.conti, 0);
  });

  it("schema diverso o DO senza origine «mac»: rifiutato prima di toccare il motore", async () => {
    const sorgente = await sintetico(MIGRATIONS);
    const piano = await readImmutable(sorgente, (db) => preparaMigrazione(db));
    const mf = await localeMac();
    const senzaTabella: PianoMigrazione = {
      ...piano,
      manifesto: { ...piano.manifesto, tabelle: piano.manifesto.tabelle.slice(1) },
    };
    const e = await invia(mf, senzaTabella);
    assert.equal(e.status, 422);
    assert.equal(e.risposta.errore, "tabelle_diverse_dal_motore");

    const vetrina = await startWorker(
      localScript,
      { CUORE_STATO: "attivo", ACCESS_SERVIZIO_MIGRAZIONE: SIMULATED_SERVICE.clientId },
      senzaRete,
    );
    const v = await invia(vetrina, piano);
    assert.equal(v.status, 503, "vetrina sintetica: la migrazione non esiste");
    assert.equal(v.risposta.errore, "migrazione_spenta");
  });

  it("ingresso di produzione: chiusa per difetto, solo il token di servizio ammesso", async () => {
    const sorgente = await sintetico(MIGRATIONS);
    const piano = await readImmutable(sorgente, (db) => preparaMigrazione(db));
    const base = {
      ACCESS_TEAM_DOMAIN: TEAM,
      ACCESS_AUD: AUD,
      ACCESS_ALLOWED_EMAILS: EMAIL,
      CUORE_STATO: "attivo",
    };
    const post = async (mf: Mf, jwt: string) => {
      const r = await mf.dispatchFetch(`${PROD}/api/finance/migrazione`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Cf-Access-Jwt-Assertion": jwt },
        body: JSON.stringify(piano.manifesto),
      });
      return { status: r.status, doc: (await r.json()) as Doc };
    };
    const servizio = (cn: string) => token(key, { email: null, sub: "", common_name: cn });

    const chiusa = await startWorker(prodScript, { ...base, CUORE_FINANCE_ORIGINE: "mac" }, certs);
    assert.equal(
      (await post(chiusa, await servizio(MIGRA_ID))).status,
      503,
      "senza ACCESS_SERVIZIO_MIGRAZIONE",
    );
    const senzaOrigine = await startWorker(
      prodScript,
      { ...base, ACCESS_SERVIZIO_MIGRAZIONE: MIGRA_ID },
      certs,
    );
    assert.equal((await post(senzaOrigine, await servizio(MIGRA_ID))).doc.errore, "migrazione_spenta");

    const aperta = await startWorker(
      prodScript,
      { ...base, CUORE_FINANCE_ORIGINE: "mac", ACCESS_SERVIZIO_MIGRAZIONE: MIGRA_ID },
      certs,
    );
    const titolare = await post(aperta, await token(key));
    assert.equal(titolare.status, 403);
    assert.equal(titolare.doc.motivo, "non_di_servizio");
    const altro = await post(aperta, await servizio("altroservizio0001.access"));
    assert.equal(altro.doc.motivo, "servizio_non_ammesso");
    const giusto = await post(aperta, await servizio(MIGRA_ID));
    assert.equal(giusto.status, 200, JSON.stringify(giusto.doc));
    assert.equal(giusto.doc.stato, "aperta");

    // in attesa dei dati: le scritture del motore sono ferme (il DO deve restare vuoto fino alla migrazione)
    const scrivi = await startWorker(
      prodScript,
      { ...base, CUORE_FINANCE_ORIGINE: "mac", CUORE_SCRITTURE: "attive" },
      certs,
    );
    const r = await scrivi.dispatchFetch(`${PROD}/api/finance/esegui`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Origin: PROD,
        "Cf-Access-Jwt-Assertion": await token(key),
      },
      body: JSON.stringify({ op: "stato_budget" }),
    });
    assert.equal(r.status, 409);
    assert.equal(((await r.json()) as Doc).errore, "in_attesa_dei_dati_del_mac");
  });

  it("riga di comando: a vuoto per difetto (nessuna rete); invio solo con https, conferma e token", async () => {
    const sorgente = await sintetico(MIGRATIONS);
    const imp = await impronta(sorgente);
    let chiamate = 0;
    const fetchSpia = async () => {
      chiamate++;
      return new Response("{}", { status: 599 });
    };
    const log: string[] = [];
    const deps = { fetch: fetchSpia, env: {}, log: (s: string) => log.push(s) };
    assert.equal(await main(["--database", sorgente], deps), 0);
    assert.equal(chiamate, 0, "a vuoto: nessuna richiesta");
    const testo = log.join("\n");
    assert.match(testo, /PROVA A VUOTO/);
    assert.ok(testo.includes(imp), "mostra l'impronta logica");
    assert.ok(!/DEMO|Pizza|EUR/.test(testo), "solo conteggi, nessun dato delle righe");

    assert.throws(
      () =>
        parseCli([
          "--database",
          sorgente,
          "--invia",
          "--url",
          "http://x.invalid",
          "--conferma-impronta",
          imp.slice(0, 12),
        ]),
      /https/,
    );
    assert.throws(
      () =>
        parseCli([
          "--database",
          sorgente,
          "--invia",
          "--url",
          "https://a:b@x.invalid",
          "--conferma-impronta",
          imp.slice(0, 12),
        ]),
      /credenziali/,
    );
    assert.throws(
      () =>
        parseCli([
          "--database",
          sorgente,
          "--invia",
          "--url",
          "https://x.invalid/api",
          "--conferma-impronta",
          imp.slice(0, 12),
        ]),
      /percorso/,
    );
    assert.throws(
      () => parseCli(["--database", sorgente, "--invia", "--url", "https://x.invalid"]),
      /conferma/,
    );
    assert.throws(() => parseCli(["--database", sorgente, "--url", "https://x.invalid"]), /solo con --invia/);

    const invio = ["--database", sorgente, "--invia", "--url", "https://cuore.esempio.invalid"];
    assert.equal(await main([...invio, "--conferma-impronta", "000000000000"], deps), 1);
    assert.equal(await main([...invio, "--conferma-impronta", imp.slice(0, 12)], deps), 1, "senza token");
    assert.equal(chiamate, 0, "conferma sbagliata o token assente: nessuna richiesta");
    const conToken = {
      ...deps,
      env: { CF_ACCESS_CLIENT_ID: "x.access", CF_ACCESS_CLIENT_SECRET: "fittizio" },
    };
    assert.equal(await main([...invio, "--conferma-impronta", imp.slice(0, 12)], conToken), 1);
    assert.equal(chiamate, 1, "con tutto in regola parte la prima richiesta (qui una finta risposta 599)");
    assert.ok(!log.join("\n").includes("fittizio"), "il segreto del token non viene mai stampato");
  });
});
