/**
 * Prototipo MCP locale in sola lettura (scripts/mcp-server.ts) su un database SINTETICO: sessione stdio completa.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { migrate } from "../src/db/migrate.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import { NodeSqliteDb } from "../src/db/node-sqlite.ts";
import { OPERATIONS } from "../src/query/operations.ts";
import { createAccount } from "../src/services/accounts.ts";
import { addManualBalance } from "../src/services/balances.ts";
import { BACKEND, importTx, row, testCtx } from "./helpers.ts";

const SCRIPT = new URL("../scripts/mcp-server.ts", import.meta.url).pathname;

describe("MCP locale in sola lettura (prototipo)", {
  skip: BACKEND === "d1" ? "lo script usa SQLite via Node" : false,
}, () => {
  it("sessione stdio: initialize, tools/list, tools/call, errori; solo protocollo su stdout; database invariato", async () => {
    const dir = mkdtempSync(join(tmpdir(), "cloud-finance-mcp-test-"));
    try {
      const path = join(dir, "prova.sqlite3");
      const db = new NodeSqliteDb(path);
      const ctx = testCtx(db);
      await migrate(db, MIGRATIONS, ctx.now);
      await createAccount(ctx, {
        id: "conto",
        name: "Conto DEMO",
        type: "current",
        currency: "EUR",
        holderRole: "other",
      });
      await importTx({ db, ctx }, "conto", [
        row("2026-01-02", "100.00", "Entrata DEMO"),
        row("2026-01-03", "-30.00", "Spesa DEMO"),
      ]);
      await addManualBalance(ctx, "conto", "2026-01-03", 7000);
      db.close();
      const before = readFileSync(path);
      const msgs = [
        {
          jsonrpc: "2.0",
          id: 1,
          method: "initialize",
          params: {
            protocolVersion: "2025-06-18",
            capabilities: {},
            clientInfo: { name: "test", version: "0" },
          },
        },
        { jsonrpc: "2.0", method: "notifications/initialized" },
        { jsonrpc: "2.0", id: 2, method: "tools/list" },
        {
          jsonrpc: "2.0",
          id: 3,
          method: "tools/call",
          params: { name: "cloud_finance_summary", arguments: { from: "2026-01-01", to: "2026-01-31" } },
        },
        {
          jsonrpc: "2.0",
          id: 4,
          method: "tools/call",
          params: { name: "cloud_finance_summary", arguments: { from: "2026-01-01" } },
        },
        {
          jsonrpc: "2.0",
          id: 5,
          method: "tools/call",
          params: { name: "cloud_finance_balances", arguments: { asOf: "2026-01-03" } },
        },
        { jsonrpc: "2.0", id: 6, method: "tools/call", params: { name: "elimina_tutto", arguments: {} } },
        { jsonrpc: "2.0", id: 7, method: "resources/list" },
        { jsonrpc: "2.0", id: 8, method: "ping" },
      ];
      const input = `${msgs.map((m) => JSON.stringify(m)).join("\n")}\n{non json\n`;
      const r = spawnSync(process.execPath, [SCRIPT, "--db", path], { input, encoding: "utf8" });
      assert.equal(r.status, 0, r.stderr);
      const lines = r.stdout.trim().split("\n");
      const out = lines.map(
        (l) =>
          JSON.parse(l) as { id: number | null; result?: Record<string, unknown>; error?: { code: number } },
      );
      assert.equal(
        out.length,
        9,
        "nessuna risposta alla notifica; una per ogni richiesta e per il JSON non valido",
      );
      const byId = (id: number) => out.find((o) => o.id === id);
      const init = byId(1)?.result?.serverInfo as { name: string } | undefined;
      assert.equal(init?.name, "cloud-finance");
      const tools = byId(2)?.result?.tools as {
        name: string;
        annotations: { readOnlyHint: boolean };
        inputSchema: { required: string[] };
      }[];
      assert.equal(tools.length, OPERATIONS.length);
      assert.ok(tools.every((t) => t.annotations.readOnlyHint && t.name.startsWith("cloud_finance_")));
      assert.deepEqual(tools.find((t) => t.name === "cloud_finance_summary")?.inputSchema.required, [
        "from",
        "to",
      ]);
      const ok = byId(3)?.result as { isError: boolean; content: { text: string }[] };
      assert.equal(ok.isError, false);
      const data = JSON.parse(ok.content[0]?.text ?? "{}") as {
        currencies: { currency: string; unclassifiedIn: number }[];
      };
      assert.deepEqual([data.currencies[0]?.currency, data.currencies[0]?.unclassifiedIn], ["EUR", 10000]);
      const bad = byId(4)?.result as { isError: boolean; content: { text: string }[] };
      assert.equal(bad.isError, true);
      assert.match(bad.content[0]?.text ?? "", /invalid_input/);
      const bal = byId(5)?.result as { isError: boolean } | undefined;
      assert.equal(bal?.isError, false);
      assert.equal(byId(6)?.error?.code, -32602);
      assert.equal(byId(7)?.error?.code, -32601);
      assert.deepEqual(byId(8)?.result, {});
      assert.ok(out.some((o) => o.id === null && o.error?.code === -32700));
      assert.ok(Buffer.compare(before, readFileSync(path)) === 0, "database invariato");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
