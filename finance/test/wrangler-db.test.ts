/**
 * L1 — bersaglio e account della verifica via Wrangler. Test PURI: nessun processo Wrangler viene avviato e nessuna
 * richiesta di rete e' possibile (i casi `--remote` qui falliscono tutti nella validazione, prima di Wrangler; la CLI
 * gira con una HOME temporanea vuota, quindi senza credenziali). L'ID account e' un valore SINTETICO di test.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";
import {
  assertReadOnlySql,
  configAccountIds,
  type ResolveDeps,
  resolveInvocation,
  type WranglerTarget,
  WranglerTargetError,
} from "../scripts/wrangler-db.ts";

const ID = "id-account-sintetico-di-test";
const CONFIG = (accountLines: string) =>
  `{\n  // configurazione dedicata di prova (sintetica)\n  "name": "prova",\n${accountLines}  "d1_databases": [{ "binding": "DB", "database_name": "db-prova", "database_id": "00000000-0000-0000-0000-000000000000" }]\n}\n`;
const deps = (configText: string, processEnv: Record<string, string | undefined> = {}): ResolveDeps => ({
  readConfig: () => configText,
  processEnv: { PATH: "/usr/bin", HOME: "/home/finto", ...processEnv },
});
const remote = (accountId = ID, env?: Record<string, string>): WranglerTarget => ({
  database: "db-prova",
  config: "/percorso/finto/wrangler.jsonc",
  mode: { kind: "remote", accountId },
  ...(env === undefined ? {} : { env }),
});
const rejects = (target: WranglerTarget, d: ResolveDeps, pattern: RegExp) =>
  assert.throws(
    () => resolveInvocation(target, d),
    (err: unknown) => err instanceof WranglerTargetError && pattern.test(err.message),
  );

describe("WranglerCliDb: solo query di lettura", () => {
  it("accetta una sola SELECT senza parametri; rifiuta tutto il resto", () => {
    assert.doesNotThrow(() => assertReadOnlySql("SELECT name FROM sqlite_master"));
    assert.doesNotThrow(() => assertReadOnlySql("  select 1"));
    for (const [sql, pattern] of [
      ["DELETE FROM d1_migrations", /solo query SELECT/],
      ["PRAGMA table_list", /solo query SELECT/],
      ["WITH x AS (SELECT 1) DELETE FROM t", /solo query SELECT/],
      ["SELECT 1; DELETE FROM d1_migrations", /senza ';'/],
      ["SELECT 1;", /senza ';'/],
    ] as const) {
      assert.throws(() => assertReadOnlySql(sql), pattern, sql);
    }
    assert.throws(() => assertReadOnlySql("SELECT ?", [1]), /parametri/);
  });
});

describe("account Cloudflare: regola di passaggio (L1)", () => {
  it("--local: CLOUDFLARE_ACCOUNT_ID non viene mai passato, anche se presente nell'ambiente", () => {
    const r = resolveInvocation(
      { database: "db", config: "c", mode: { kind: "local", persistTo: "/tmp/x" } },
      deps(CONFIG(""), { CLOUDFLARE_ACCOUNT_ID: ID, CLOUDFLARE_API_TOKEN: "non-deve-passare" }),
    );
    assert.deepEqual(r.modeArgs, ["--local", "--persist-to", "/tmp/x"]);
    assert.equal(r.env.CLOUDFLARE_ACCOUNT_ID, undefined);
    assert.equal(r.env.CLOUDFLARE_API_TOKEN, undefined);
  });

  it("--remote coerente: l'ID passa al processo; nessuna variabile di credenziali viene inoltrata", () => {
    const r = resolveInvocation(
      remote(),
      deps(CONFIG(`  "account_id": "${ID}",\n`), { CLOUDFLARE_API_TOKEN: "x", CLOUDFLARE_API_KEY: "y" }),
    );
    assert.deepEqual(r.modeArgs, ["--remote"]);
    assert.equal(r.env.CLOUDFLARE_ACCOUNT_ID, ID);
    assert.deepEqual(Object.keys(r.env).sort(), [
      "CI",
      "CLOUDFLARE_ACCOUNT_ID",
      "HOME",
      "PATH",
      "WRANGLER_SEND_METRICS",
    ]);
    assert.equal(r.env.WRANGLER_SEND_METRICS, "false");
  });

  it("--remote con la stessa variabile nell'ambiente: ammesso", () => {
    assert.equal(
      resolveInvocation(remote(), deps(CONFIG(`  "account_id": "${ID}",\n`), { CLOUDFLARE_ACCOUNT_ID: ID }))
        .env.CLOUDFLARE_ACCOUNT_ID,
      ID,
    );
  });

  it("--remote: ID assente, vuoto o con caratteri non ammessi -> rifiuto", () => {
    const cfg = deps(CONFIG(`  "account_id": "${ID}",\n`));
    rejects(remote(""), cfg, /mancante o con caratteri/);
    rejects(remote("<inserire-id>"), cfg, /caratteri non ammessi/);
    rejects(remote("id con spazi"), cfg, /caratteri non ammessi/);
    rejects(remote("a".repeat(65)), cfg, /caratteri non ammessi/);
  });

  it("--remote: configurazione senza account_id, con piu' account_id o con un valore diverso -> rifiuto", () => {
    rejects(remote(), deps(CONFIG("")), /deve contenere account_id/);
    rejects(
      remote(),
      deps(CONFIG(`  "account_id": "${ID}",\n  "env": { "x": { "account_id": "${ID}" } },\n`)),
      /piu' volte/,
    );
    rejects(
      remote(),
      deps(CONFIG(`  "account_id": "altro-account-sintetico",\n`)),
      /diverso da quello indicato/,
    );
  });

  it("--remote: variabile d'ambiente diversa dall'ID indicato -> rifiuto (bersaglio ambiguo)", () => {
    rejects(
      remote(),
      deps(CONFIG(`  "account_id": "${ID}",\n`), { CLOUDFLARE_ACCOUNT_ID: "altro-account-sintetico" }),
      /dell'ambiente diverso/,
    );
  });

  it("variabili CLOUDFLARE_* o CF_* nell'env esplicito -> rifiuto (l'account passa solo da accountId)", () => {
    const cfg = deps(CONFIG(`  "account_id": "${ID}",\n`));
    rejects(remote(ID, { CLOUDFLARE_ACCOUNT_ID: ID }), cfg, /non ammessa in env/);
    rejects(remote(ID, { CLOUDFLARE_API_TOKEN: "x" }), cfg, /non ammessa in env/);
    rejects(remote(ID, { CF_API_TOKEN: "x" }), cfg, /non ammessa in env/);
  });

  it("configurazione non leggibile -> rifiuto", () => {
    rejects(
      remote(),
      {
        readConfig: () => {
          throw new Error("ENOENT");
        },
        processEnv: {},
      },
      /non leggibile/,
    );
  });

  it("lettura di account_id anche da JSONC con commenti", () => {
    assert.deepEqual(configAccountIds(CONFIG(`  // commento\n  "account_id"  :  "${ID}",\n`)), [ID]);
  });
});

describe("CLI verify-wrangler: errori prima di avviare Wrangler", () => {
  const dir = mkdtempSync(join(tmpdir(), "cloud-finance-verify-cli-"));
  after(() => rmSync(dir, { recursive: true, force: true }));
  const SCRIPT = new URL("../scripts/migrations.ts", import.meta.url).pathname;
  const cli = (...args: string[]) =>
    spawnSync(process.execPath, [SCRIPT, "verify-wrangler", "--database", "db-prova", ...args], {
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: dir },
      timeout: 60_000,
    });

  it("--remote senza --account-id, --account-id con --local, account diversi: uscita 2 con messaggio", () => {
    const cfgOk = join(dir, "con-account.jsonc");
    const cfgNo = join(dir, "senza-account.jsonc");
    writeFileSync(cfgOk, CONFIG(`  "account_id": "${ID}",\n`));
    writeFileSync(cfgNo, CONFIG(""));
    for (const [args, pattern] of [
      [["--config", cfgOk, "--remote"], /--remote richiede --account-id/],
      [["--config", cfgOk, "--local", "--persist-to", dir, "--account-id", ID], /vale solo con --remote/],
      [["--config", cfgNo, "--remote", "--account-id", ID], /deve contenere account_id/],
      [
        ["--config", cfgOk, "--remote", "--account-id", "altro-account-sintetico"],
        /diverso da quello indicato/,
      ],
      [["--config", cfgOk, "--local", "--remote"], /esattamente uno/],
    ] as const) {
      const r = cli(...args);
      assert.equal(r.status, 2, `${args.join(" ")}: ${r.stderr}`);
      assert.match(r.stderr, pattern);
      assert.equal(r.stdout, "", "nessun risultato: Wrangler non e' stato avviato");
    }
  });
});
