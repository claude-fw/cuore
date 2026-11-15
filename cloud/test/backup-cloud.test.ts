/**
 * L1, backup CONSERVATI nel cloud (src/backup-cloud.ts): 7 copie e rotazione (Node, bucket finto in memoria), poi in
 * workerd (Miniflare) con un bucket R2 SIMULATO in memoria: copia giornaliera dal Cron, ritiro solo con il token di
 * servizio `mac-backup`, rotte chiuse per difetto, andata e ritorno Cron → R2 → ritiro (scripts/ritira-backup.ts) →
 * ripristino con impronta uguale. Chiavi di PROVA generate a ogni esecuzione, dati sintetici, nessuna rete.
 */
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { before, describe, it } from "node:test";
import { ritira } from "../scripts/ritira-backup.ts";
import type { BackupFile } from "../src/backup.ts";
import {
  type BackupBucket,
  elencoBackup,
  giornoValido,
  nomeBackup,
  salvaBackup,
} from "../src/backup-cloud.ts";
import { AUD, bundle, EMAIL, startWorker, TEAM, type TestKey, testKey, token } from "./helpers.ts";

const MAC_BACKUP = "a1b2c3d4e5f60718293a4b5c6d7e8f90.access";
const MAC_SCHOOL = "0123456789abcdef0123456789abcdef.access";
const URL0 = "https://cuore.esempio.workers.dev";
const DAY = 86_400_000;
const R2 = { r2Buckets: ["BACKUP"] };
/** Token di servizio FITTIZI dell'ingresso locale (src/locale.ts, che importa l'HTML e non si carica in Node). */
const SIMULATED_BACKUP_SERVICE = {
  clientId: "macbackupsimulato0001.access",
  clientSecret: "segreto-simulato-mac-backup-solo-locale-0001",
};
const SIMULATED_SERVICE = {
  clientId: "macschoolsimulato0001.access",
  clientSecret: "segreto-simulato-mac-school-solo-locale-0001",
};

/** Bucket finto con la stessa API usata dal codice (list a pagine da 3 per provare il cursore). */
function bucketFinto(): BackupBucket & { dati: Map<string, string> } {
  const dati = new Map<string, string>();
  return {
    dati,
    async put(k, v) {
      dati.set(k, v);
    },
    async get(k) {
      const v = dati.get(k);
      return v === undefined ? null : { body: new Response(v).body as ReadableStream, size: v.length };
    },
    async list({ prefix, cursor }) {
      const keys = [...dati.keys()].filter((k) => k.startsWith(prefix)).sort();
      const from = Number(cursor ?? 0);
      const page = keys.slice(from, from + 3);
      const truncated = from + 3 < keys.length;
      return {
        objects: page.map((key) => ({ key, size: (dati.get(key) as string).length })),
        truncated,
        ...(truncated ? { cursor: String(from + 3) } : {}),
      };
    },
    async delete(k) {
      for (const x of Array.isArray(k) ? k : [k]) dati.delete(x);
    },
  };
}
const fileFinto = (n: number) =>
  ({
    header: { write_seq: n, creato: "2026-11-05T05:00:00.000Z" },
    firma: "",
    blocchi: [],
  }) as unknown as BackupFile;

describe("backup conservati: 7 copie e rotazione (bucket finto)", () => {
  it("tiene le ultime 7 per giorno, sostituisce lo stesso giorno, ignora oggetti estranei", async () => {
    const b = bucketFinto();
    await b.put("altro/2026-01-01.cuorebak.json", "x");
    await b.put("finance/non-un-giorno.cuorebak.json", "x");
    let ultimo: Awaited<ReturnType<typeof salvaBackup>> | undefined;
    for (let i = 1; i <= 9; i++) ultimo = await salvaBackup(b, `2026-11-0${i}`, fileFinto(i));
    assert.deepEqual(ultimo, { giorno: "2026-11-09", byte: ultimo?.byte, conservati: 7, eliminati: 1 });
    const giorni = (await elencoBackup(b)).map((c) => c.giorno);
    assert.deepEqual(giorni, [
      "2026-11-09",
      "2026-11-08",
      "2026-11-07",
      "2026-11-06",
      "2026-11-05",
      "2026-11-04",
      "2026-11-03",
    ]);
    assert.ok(
      b.dati.has("altro/2026-01-01.cuorebak.json") && b.dati.has("finance/non-un-giorno.cuorebak.json"),
    );
    await salvaBackup(b, "2026-11-09", fileFinto(99));
    assert.equal(JSON.parse(b.dati.get(nomeBackup("2026-11-09")) as string).header.write_seq, 99);
    assert.equal((await elencoBackup(b)).length, 7);
    await assert.rejects(() => salvaBackup(b, "../segreti", fileFinto(1)), /giorno/);
    for (const g of ["2026-11-31x", "2026-13-40", "", "2026/11/05"]) assert.equal(giornoValido(g), false, g);
  });
});

describe("backup conservati in workerd: Cron → R2 → ritiro del Mac → ripristino", () => {
  let key: TestKey;
  let script = "";
  let chiavi: Record<string, string>;
  let dir: string;
  let fileChiave: string;
  let fileFirma: string;
  const outbound = (r: Request) =>
    r.url === `https://${TEAM}/cdn-cgi/access/certs`
      ? Response.json({ keys: [key.jwk] })
      : new Response("rete non disponibile nei test", { status: 599 });
  const servizio = (cn: string) => token(key, { email: null, sub: "", common_name: cn });
  const base = {
    ACCESS_TEAM_DOMAIN: TEAM,
    ACCESS_AUD: AUD,
    ACCESS_ALLOWED_EMAILS: EMAIL,
    CUORE_STATO: "attivo",
  };

  before(async () => {
    key = await testKey("chiave-team-backup");
    script = await bundle("index");
    const rsa = () =>
      webcrypto.subtle.generateKey(
        { name: "RSA-OAEP", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
        true,
        ["encrypt", "decrypt"],
      ) as Promise<webcrypto.CryptoKeyPair>;
    const mac = await rsa();
    const recupero = await rsa();
    const firma = await webcrypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, [
      "sign",
      "verify",
    ]);
    const jwk = async (k: webcrypto.CryptoKey) => JSON.stringify(await webcrypto.subtle.exportKey("jwk", k));
    chiavi = {
      BACKUP_SIGNING_PRIVATE_JWK: await jwk(firma.privateKey),
      BACKUP_MAC_PUBLIC_JWK: await jwk(mac.publicKey),
      BACKUP_RECOVERY_PUBLIC_JWK: await jwk(recupero.publicKey),
    };
    dir = mkdtempSync(join(tmpdir(), "cuore-ritiro-test-"));
    fileChiave = join(dir, "mac-privata.jwk.json");
    fileFirma = join(dir, "firma-pubblica.jwk.json");
    writeFileSync(fileChiave, await jwk(mac.privateKey));
    writeFileSync(fileFirma, await jwk(firma.publicKey));
  });

  type Mf = Awaited<ReturnType<typeof startWorker>>;
  const cron = async (mf: Mf, ms: number) => {
    const w = await (
      mf as unknown as { getWorker(): Promise<{ scheduled(o: unknown): Promise<unknown> }> }
    ).getWorker();
    await w.scheduled({ scheduledTime: new Date(ms), cron: "0 5,11,17,21 * * *" });
  };
  const get = async (mf: Mf, path: string, headers: Record<string, string> = {}) => {
    const r = await mf.dispatchFetch(`${URL0}${path}`, { headers });
    const text = await r.text();
    return { status: r.status, headers: r.headers, text, doc: text.startsWith("{") ? JSON.parse(text) : {} };
  };
  const comeMac = async () => ({ "Cf-Access-Jwt-Assertion": await servizio(MAC_BACKUP) });

  it("chiuso per difetto: senza bucket o senza Client ID il ritiro risponde 503, senza chiavi nessuna copia", async () => {
    const senzaBucket = await startWorker(
      script,
      { ...base, ...chiavi, ACCESS_SERVIZIO_BACKUP: MAC_BACKUP },
      outbound,
    );
    await cron(senzaBucket, Date.now());
    for (const p of ["/api/finance/backup", "/api/finance/backup/elenco"])
      assert.deepEqual(
        [(await get(senzaBucket, p, await comeMac())).doc.errore],
        ["ritiro_backup_spento"],
        p,
      );
    const senzaId = await startWorker(script, { ...base, ...chiavi }, outbound, R2);
    const r = await get(senzaId, "/api/finance/backup", await comeMac());
    assert.deepEqual([r.status, r.doc.errore], [503, "ritiro_backup_spento"]);
    const senzaChiavi = await startWorker(
      script,
      { ...base, ACCESS_SERVIZIO_BACKUP: MAC_BACKUP },
      outbound,
      R2,
    );
    await cron(senzaChiavi, Date.now());
    assert.deepEqual((await get(senzaChiavi, "/api/finance/backup/elenco", await comeMac())).doc.copie, []);
    assert.equal((await get(senzaChiavi, "/api/finance/backup", await comeMac())).status, 404);
    const spento = await startWorker(
      script,
      { ...base, ...chiavi, ACCESS_SERVIZIO_BACKUP: MAC_BACKUP, CUORE_STATO: "spento" },
      outbound,
      R2,
    );
    assert.equal((await get(spento, "/api/finance/backup", await comeMac())).status, 503);
  });

  it("una copia al giorno dal Cron, ultime 7; ritiro solo con il token mac-backup; andata e ritorno con impronta uguale", async () => {
    const mf = await startWorker(
      script,
      { ...base, ...chiavi, ACCESS_SERVIZIO_BACKUP: MAC_BACKUP },
      outbound,
      R2,
    );
    const oggi = Date.now();
    for (let i = 8; i >= 0; i--) await cron(mf, oggi - i * DAY);
    await cron(mf, oggi); // secondo turno dello stesso giorno: nessuna copia in piu'
    const el = await get(mf, "/api/finance/backup/elenco", await comeMac());
    assert.equal(el.status, 200);
    const giorni = (el.doc.copie as { giorno: string; byte: number }[]).map((c) => c.giorno);
    const giornoDi = (ms: number) => new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Rome" }).format(ms);
    assert.deepEqual(
      giorni,
      [0, 1, 2, 3, 4, 5, 6].map((i) => giornoDi(oggi - i * DAY)),
    );

    // chi non e' il Mac dei backup non ritira nulla (e non tocca il bucket)
    const nessuno = await get(mf, "/api/finance/backup");
    assert.deepEqual([nessuno.status, nessuno.doc.motivo], [403, "token_assente"]);
    const titolare = await get(mf, "/api/finance/backup", { "Cf-Access-Jwt-Assertion": await token(key) });
    assert.deepEqual([titolare.status, titolare.doc.motivo], [403, "non_di_servizio"]);
    const school = await get(mf, "/api/finance/backup", {
      "Cf-Access-Jwt-Assertion": await servizio(MAC_SCHOOL),
    });
    assert.deepEqual([school.status, school.doc.motivo], [403, "servizio_non_ammesso"]);
    const intestazioni = await get(mf, "/api/finance/backup", {
      "CF-Access-Client-Id": MAC_BACKUP,
      "CF-Access-Client-Secret": "x".repeat(40),
    });
    assert.equal(intestazioni.status, 403, "serve il token firmato da Access, non le sole intestazioni");
    const daAltroSito = await get(mf, "/api/finance/backup", {
      ...(await comeMac()),
      "Sec-Fetch-Site": "cross-site",
    });
    assert.equal(daAltroSito.status, 403);

    // giorni: il piu' recente per difetto, uno vecchio ancora presente, uno ruotato via, uno non valido
    const ultimo = await get(mf, "/api/finance/backup", await comeMac());
    assert.equal(ultimo.status, 200);
    assert.equal(ultimo.headers.get("X-Cuore-Backup-Giorno"), giorni[0]);
    assert.equal(ultimo.headers.get("Cache-Control"), "no-store");
    const file = JSON.parse(ultimo.text) as BackupFile;
    assert.equal(file.header.schema, "cuore.finance.backup/1");
    assert.equal(file.header.destinatari.length, 2);
    assert.ok(!ultimo.text.includes("Supermercato Esempio"), "nel bucket solo testo cifrato");
    assert.equal((await get(mf, `/api/finance/backup?giorno=${giorni[6]}`, await comeMac())).status, 200);
    const ruotato = await get(mf, `/api/finance/backup?giorno=${giornoDi(oggi - 8 * DAY)}`, await comeMac());
    assert.deepEqual([ruotato.status, ruotato.doc.errore], [404, "backup_non_trovato"]);
    assert.equal((await get(mf, "/api/finance/backup?giorno=../x", await comeMac())).status, 400);

    // il Mac ritira e ripristina in un database nuovo: firma, integrita', schema e impronta coerenti
    const headers = await comeMac();
    const fetcher = (u: string, i: RequestInit) => mf.dispatchFetch(u, i);
    const cartella = join(dir, "ritiri");
    const ritiro = await ritira(
      { url: URL0, cartella, chiavePrivata: fileChiave, chiaveFirma: fileFirma },
      headers,
      fetcher,
    );
    assert.equal(ritiro.giorno, giorni[0]);
    assert.equal(ritiro.report.integrityCheck, "ok");
    assert.equal(ritiro.report.foreignKeyErrori, 0);
    assert.equal(ritiro.report.schemaCoerente, true);
    assert.equal(ritiro.report.sqliteSequenceCoerente, true);
    assert.equal(
      ritiro.report.improntaCoerente,
      true,
      "impronta del database ripristinato = manifesto firmato",
    );
    assert.equal(ritiro.report.tabelle.find((t) => t.table === "accounts")?.righe, 3);
    assert.ok(existsSync(ritiro.file) && !readFileSync(ritiro.file, "utf8").includes("Supermercato Esempio"));
    // nulla si sovrascrive; iCloud e indirizzi non https rifiutati
    await assert.rejects(
      () =>
        ritira({ url: URL0, cartella, chiavePrivata: fileChiave, chiaveFirma: fileFirma }, headers, fetcher),
      /non sovrascritto/,
    );
    await assert.rejects(
      () =>
        ritira(
          {
            url: URL0,
            cartella: "/Users/x/Library/Mobile Documents/com~apple~CloudDocs/b",
            chiavePrivata: "",
            chiaveFirma: "",
          },
          headers,
          fetcher,
        ),
      /iCloud/,
    );
    await assert.rejects(
      () =>
        ritira(
          { url: "http://cuore.esempio.workers.dev", cartella, chiavePrivata: "", chiaveFirma: "" },
          headers,
          fetcher,
        ),
      /https/,
    );
    // un giorno vecchio, scaricato a parte, si ripristina uguale (stesso motore, stessi dati sintetici)
    const vecchio = await ritira(
      { url: URL0, cartella, chiavePrivata: fileChiave, chiaveFirma: fileFirma, giorno: giorni[6] as string },
      headers,
      fetcher,
    );
    assert.equal(vecchio.report.impronta, ritiro.report.impronta);
  });

  it("ingresso locale: il token mac-backup SIMULATO ritira, quello di School no, un segreto sbagliato = 403", async () => {
    const locale = await bundle("locale");
    const mf = await startWorker(
      locale,
      {
        CUORE_STATO: "attivo",
        ...chiavi,
        ACCESS_SERVIZIO_SCHOOL: SIMULATED_SERVICE.clientId,
        ACCESS_SERVIZIO_BACKUP: SIMULATED_BACKUP_SERVICE.clientId,
      },
      outbound,
      R2,
    );
    await cron(mf, Date.now());
    const come = (s: { clientId: string; clientSecret: string }) =>
      mf.dispatchFetch("http://localhost:8787/api/finance/backup/elenco", {
        headers: { "CF-Access-Client-Id": s.clientId, "CF-Access-Client-Secret": s.clientSecret },
      });
    const ok = await come(SIMULATED_BACKUP_SERVICE);
    assert.equal(ok.status, 200);
    assert.equal(((await ok.json()) as { copie: unknown[] }).copie.length, 1);
    const school = await come(SIMULATED_SERVICE);
    assert.equal(school.status, 403);
    assert.equal(((await school.json()) as { motivo: string }).motivo, "servizio_non_ammesso");
    assert.equal((await come({ ...SIMULATED_BACKUP_SERVICE, clientSecret: "sbagliato" })).status, 403);
  });
});
