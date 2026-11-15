/**
 * Percorso Wrangler CLI: l'SQL delle migrazioni applicato con `wrangler d1 execute --local` (D1 locale su disco in
 * una cartella temporanea, telemetria disattivata, nessuna credenziale, nessuna risorsa remota).
 * Confronta lo schema risultante con quello creato dal runner delle migrazioni su SQLite via Node.
 * Eseguito con CLOUD_FINANCE_TEST_DB=d1.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";
import { migrate } from "../src/db/migrate.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import { NodeSqliteDb } from "../src/db/node-sqlite.ts";
import { BACKEND } from "./db-backend.ts";

const SKIP = BACKEND === "d1" ? false : "solo con CLOUD_FINANCE_TEST_DB=d1 (usa la CLI di Wrangler)";
const ROOT = new URL("..", import.meta.url).pathname;
const WRANGLER = join(ROOT, "node_modules/wrangler/bin/wrangler.js");
const dir = mkdtempSync(join(tmpdir(), "cloud-finance-wrangler-"));
after(() => rmSync(dir, { recursive: true, force: true }));

function wrangler(...args: string[]): { code: number; json: unknown; err: string } {
  const r = spawnSync(
    process.execPath,
    [
      WRANGLER,
      "d1",
      "execute",
      "cloud-finance-local",
      "--local",
      "--persist-to",
      join(dir, "state"),
      "--config",
      join(dir, "wrangler.jsonc"),
      ...args,
    ],
    {
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: dir, WRANGLER_SEND_METRICS: "false", CI: "1" },
      timeout: 120_000,
    },
  );
  let json: unknown = null;
  try {
    json = JSON.parse(r.stdout);
  } catch {
    json = null;
  }
  return { code: r.status ?? -1, json, err: `${r.stderr}${r.stdout}`.slice(-2000) };
}

type Obj = { type: string; name: string };
const SCHEMA_QUERY =
  "SELECT type, name FROM sqlite_master WHERE name NOT LIKE '_cf_%' AND name NOT LIKE 'sqlite_%' ORDER BY type, name";

describe("Wrangler CLI con D1 locale", { skip: SKIP }, () => {
  it("lo schema delle migrazioni si applica con wrangler d1 execute ed e' identico a quello su SQLite via Node", async () => {
    writeFileSync(
      join(dir, "wrangler.jsonc"),
      JSON.stringify({
        name: "cloud-finance-local-test",
        compatibility_date: "2026-09-01",
        d1_databases: [
          {
            binding: "DB",
            database_name: "cloud-finance-local",
            database_id: "00000000-0000-0000-0000-000000000000",
          },
        ],
      }),
    );
    writeFileSync(
      join(dir, "schema.sql"),
      `${MIGRATIONS.flatMap((m) => m.statements)
        .map((s) => `${s};`)
        .join("\n")}\n`,
    );
    const applied = wrangler("--file", join(dir, "schema.sql"));
    assert.equal(applied.code, 0, applied.err);

    const remote = wrangler("--json", "--command", SCHEMA_QUERY);
    assert.equal(remote.code, 0, remote.err);
    const d1Objects = ((remote.json as { results: Obj[] }[])[0]?.results ?? []).map(
      (o) => `${o.type}:${o.name}`,
    );

    const node = new NodeSqliteDb(":memory:");
    await migrate(node, MIGRATIONS, () => "t");
    const nodeObjects = (await node.all<Obj>(SCHEMA_QUERY)).map((o) => `${o.type}:${o.name}`);
    node.close();
    assert.ok(nodeObjects.length > 60);
    assert.deepEqual(d1Objects, nodeObjects);

    // i trigger funzionano anche passando dalla CLI (agiscono per riga: serve almeno una riga)
    const inserted = wrangler(
      "--command",
      "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (1, 'core', 'x', 't')",
    );
    assert.equal(inserted.code, 0, inserted.err);
    for (const sql of ["DELETE FROM schema_migrations", "UPDATE schema_migrations SET name = 'y'"]) {
      const immutable = wrangler("--command", sql);
      assert.notEqual(immutable.code, 0, sql);
      assert.match(immutable.err, /storico delle migrazioni immutabile/, sql);
    }
  });
});
