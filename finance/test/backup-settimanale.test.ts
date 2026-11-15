/** Backup settimanale verificato (`scripts/backup-settimanale.ts`), su copie SINTETICHE in cartelle temporanee. */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { verifiedBackup } from "../scripts/scrivi.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import { parseBackupName, planRetention } from "../src/domain/backup-retention.ts";
import { createSyntheticSession } from "../src/synthetic/generate.ts";

const FINANCE = join(dirname(fileURLToPath(import.meta.url)), "..");
const SCRIPT = join(FINANCE, "scripts", "backup-settimanale.ts");

/** Database sintetico in `<base>/finance/db/cloud-finance.sqlite3`, come quello reale. */
async function realLike(): Promise<{ base: string; db: string; done: () => void }> {
  const base = realpathSync(mkdtempSync(join(tmpdir(), "cuore-settimanale-test-")));
  const s = await createSyntheticSession(join(base, "s"), { allowedRoot: base, forbidden: [] }, MIGRATIONS);
  const dir = join(base, "finance", "db");
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  const db = join(dir, "cloud-finance.sqlite3");
  copyFileSync(join(s.realDir, "finance-sintetico.sqlite3"), db);
  return { base, db, done: () => rmSync(base, { recursive: true, force: true }) };
}

const run = (args: string[]) => spawnSync(process.execPath, [SCRIPT, ...args], { encoding: "utf8" });

describe("backup-settimanale.ts", () => {
  it("crea una copia verificata (0600, schema della rotazione), una al giorno; originale invariato", async () => {
    const t = await realLike();
    try {
      const before = readFileSync(t.db);
      const first = run(["--db", t.db]);
      assert.equal(first.status, 0, first.stdout + first.stderr);
      const doc = JSON.parse(first.stdout);
      assert.equal(doc.stato, "creato");
      const again = JSON.parse(run(["--db", t.db]).stdout);
      assert.deepEqual([again.stato, again.file], ["presente", doc.file]);
      const dir = join(t.base, "finance", "backup");
      const files = readdirSync(dir);
      assert.deepEqual(files, [doc.file]);
      assert.equal(parseBackupName(doc.file)?.kind, "settimanale");
      assert.equal(statSync(join(dir, doc.file)).mode & 0o777, 0o600);
      assert.ok(readFileSync(t.db).equals(before));
      assert.deepEqual(readdirSync(join(t.base, "finance", "db")), ["cloud-finance.sqlite3"]);
      // la rotazione lo vede come gruppo a parte e non tocca mai il piu' recente
      assert.deepEqual(
        planRetention(files, { keepLast: 0, keepDaily: 0, keepMonthly: 0 }).keep.map((k) => k.name),
        [doc.file],
      );
    } finally {
      t.done();
    }
  });

  it("indipendente dal backup prima della scrittura dello stesso giorno", async () => {
    const t = await realLike();
    try {
      const at = () => new Date("2026-11-08T10:30:00.000Z");
      assert.equal((await verifiedBackup(t.db, "scrittura", { now: at })).stato, "creato");
      assert.equal((await verifiedBackup(t.db, "settimanale", { now: at })).stato, "creato");
      const kinds = readdirSync(join(t.base, "finance", "backup")).map((n) => parseBackupName(n)?.kind);
      assert.deepEqual(kinds.sort(), ["scrittura", "settimanale"]);
    } finally {
      t.done();
    }
  });

  it("copia non verificata: rifiuto (2), nessun nome definitivo", async () => {
    const t = await realLike();
    try {
      await assert.rejects(
        verifiedBackup(t.db, "settimanale", {
          copy: async (_from, to) => {
            writeFileSync(to, "non un database");
          },
        }),
        /backup non/,
      );
      assert.ok(readdirSync(join(t.base, "finance", "backup")).every((n) => parseBackupName(n) === null));
    } finally {
      t.done();
    }
  });

  it("database in uso (-wal non vuoto): nessun backup (2)", async () => {
    const t = await realLike();
    try {
      writeFileSync(`${t.db}-wal`, "x");
      const r = run(["--db", t.db]);
      assert.equal(r.status, 2, r.stdout);
      assert.match(JSON.parse(r.stdout).motivo, /in uso/);
      assert.equal(existsSync(join(t.base, "finance", "backup")), false);
    } finally {
      t.done();
    }
  });

  it("senza --db: 64; percorso relativo o nel repository: 2", () => {
    assert.equal(run([]).status, 64);
    assert.equal(run(["--db", "relativo.sqlite3"]).status, 2);
    const r = run(["--db", join(FINANCE, "examples", "x.sqlite3")]);
    assert.equal(r.status, 2);
    assert.equal(JSON.parse(r.stdout).ok, false);
  });
});
