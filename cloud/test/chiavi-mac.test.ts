/**
 * Chiavi dei backup nel Portachiavi (L1), SOLO CON UN PORTACHIAVI FINTO in memoria: nessuna chiamata a `security`,
 * nessuna chiave vera. Generazione → backup cifrato e firmato → ripristino con le chiavi lette dal «Portachiavi».
 */
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { migrate } from "../../finance/src/db/migrate.ts";
import { MIGRATIONS } from "../../finance/src/db/migrations/index.ts";
import { NodeSqliteDb } from "../../finance/src/db/node-sqlite.ts";
import { createAccount } from "../../finance/src/services/accounts.ts";
import {
  chiaviRitiro,
  controllaRecupero,
  generaChiavi,
  type Portachiavi,
  rigaAggiungi,
  SERVIZI,
} from "../scripts/chiavi-mac.ts";
import { ripristina } from "../scripts/ripristina-backup.ts";
import {
  createBackup,
  logicalChecksumPortable,
  parseBackupKeys,
  readExportableTables,
  readSqliteSequence,
} from "../src/backup.ts";

const finto = (): Portachiavi & { voci: Map<string, string> } => {
  const voci = new Map<string, string>();
  return {
    voci,
    leggi: (s) => voci.get(s) ?? null,
    aggiungi: (s, v) => {
      voci.set(s, v);
    },
  };
};

describe("chiavi dei backup nel Portachiavi (finto)", () => {
  it("genera: privata del Mac e firma nel Portachiavi, fuori solo pubbliche e kid; il recupero mai nel Portachiavi", async () => {
    const pc = finto();
    const e = await generaChiavi(pc);
    assert.deepEqual([...pc.voci.keys()].sort(), Object.values(SERVIZI).sort());
    assert.equal(JSON.parse(pc.voci.get(SERVIZI.mac) as string).kty, "RSA");
    assert.ok("d" in JSON.parse(pc.voci.get(SERVIZI.mac) as string), "privata del Mac nel Portachiavi");
    assert.equal("d" in JSON.parse(pc.voci.get(SERVIZI.firma) as string), false, "solo la pubblica di firma");
    for (const v of Object.values(e.pubbliche))
      assert.equal("d" in JSON.parse(v), false, "stampate solo pubbliche");
    assert.match(e.kid.mac, /^[0-9a-f]{64}$/);
    assert.notEqual(e.kid.mac, e.kid.recupero);
    for (const v of pc.voci.values()) assert.notEqual(v, e.recuperoPrivata);
    await assert.rejects(generaChiavi(pc), /gia' presente/, "mai sovrascritte");
  });

  it("backup del Worker con quelle chiavi → ripristino con le chiavi lette dal Portachiavi (e col recupero)", async () => {
    const pc = finto();
    const e = await generaChiavi(pc);
    const dir = mkdtempSync(join(tmpdir(), "cuore-chiavi-test-"));
    const db = new NodeSqliteDb(join(dir, "sorgente.sqlite"));
    await migrate(db, MIGRATIONS, () => "2026-11-06T00:00:00.000Z");
    const ctx = { db, now: () => new Date().toISOString(), newId: () => crypto.randomUUID() };
    await createAccount(ctx, {
      id: "conto",
      name: "Conto DEMO",
      type: "current",
      currency: "EUR",
      holderRole: "owner",
    });
    const keys = await parseBackupKeys({
      ...e.pubbliche,
      BACKUP_SIGNING_PRIVATE_JWK: pc.voci.get(SERVIZI.firmaPrivata) as string,
    });
    assert.ok(keys !== null, "il Worker accetta le chiavi generate");
    const file = await createBackup({
      tables: await readExportableTables(db),
      sqliteSequence: await readSqliteSequence(db),
      versioneSchema: MIGRATIONS.length,
      writeSeq: 1,
      origine: "sintetico",
      recipients: keys.recipients,
      signingKey: keys.signingKey,
    });
    const impronta = await logicalChecksumPortable(db);
    db.close();
    const percorso = join(dir, "finance.cuorebak.json");
    writeFileSync(percorso, JSON.stringify(file));
    const daPortachiavi = await ripristina({
      file: percorso,
      destinazione: join(dir, "ripristino.sqlite"),
      ...chiaviRitiro(pc),
      versioneSchema: MIGRATIONS.length,
    });
    assert.equal(daPortachiavi.improntaCoerente, true);
    assert.equal(daPortachiavi.impronta, impronta);
    const conRecupero = await ripristina({
      file: percorso,
      destinazione: join(dir, "ripristino-recupero.sqlite"),
      chiavePrivata: JSON.parse(e.recuperoPrivata),
      chiaveFirma: chiaviRitiro(pc).chiaveFirma,
      versioneSchema: MIGRATIONS.length,
    });
    assert.equal(conRecupero.improntaCoerente, true, "la chiave di recupero apre lo stesso backup");
    assert.throws(() => chiaviRitiro(finto()), /assenti nel Portachiavi/);
  });

  it("security -i: valore in base64url (nessuna virgoletta), mai sulla riga di comando; recupero mai in iCloud o nel repo", () => {
    const riga = rigaAggiungi(SERVIZI.mac, '{"kty":"RSA","d":"x y"}');
    assert.match(
      riga,
      /^add-generic-password -a cuore -s cuore\.backup\.mac -l cuore\.backup\.mac -w [A-Za-z0-9_-]+\n$/,
    );
    assert.throws(() => rigaAggiungi("x; rm", "v"), /servizio non valido/);
    assert.throws(
      () => controllaRecupero("/Users/x/Library/Mobile Documents/com~apple~CloudDocs/r.json", "/repo"),
      /iCloud/,
    );
    assert.throws(() => controllaRecupero("/repo/cloud/r.json", "/repo"), /repository/);
    assert.throws(
      () => controllaRecupero("/Volumes/chiavetta-inesistente-cuore/r.json", "/repo"),
      /inesistente/,
    );
  });
});
