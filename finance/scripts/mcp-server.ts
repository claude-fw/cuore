#!/usr/bin/env node
/**
 * PROTOTIPO di server MCP locale in SOLA LETTURA (stdio, JSON-RPC 2.0 una riga per messaggio, nessuna dipendenza).
 *
 *   node scripts/mcp-server.ts --db <database.sqlite3>
 *
 * Espone come strumenti le operazioni del contratto di consultazione (src/query/operations.ts), con lo schema
 * d'ingresso derivato dal contratto e l'indicazione `readOnlyHint`. A ogni chiamata `readImmutable` copia un'istantanea
 * coerente del database in memoria e interroga solo quella: nessuna scrittura, nessun lock, nessun `-wal`/`-shm`; un
 * `-wal` non vuoto (scrittura in corso) o un file cambiato durante la copia danno un errore dello strumento, mai dati
 * incoerenti.
 * Sullo stdout escono SOLO messaggi del protocollo; i log (senza dati) vanno su stderr.
 * Non e' registrato in alcun client: la registrazione e' un passo separato da autorizzare.
 */
import { existsSync } from "node:fs";
import { createInterface } from "node:readline";
import { parseArgs } from "node:util";
import { readImmutable, SnapshotUnavailableError } from "../src/db/node-sqlite.ts";
import type { Db } from "../src/db/types.ts";
import { type FieldSpec, OPERATIONS, runQuery } from "../src/query/operations.ts";

export const SERVER_INFO = { name: "cloud-finance", version: "0.1.0" };
const DEFAULT_PROTOCOL = "2025-06-18";
const TOOL_PREFIX = "cloud_finance_";

type Json = null | boolean | number | string | Json[] | { [k: string]: Json };
interface Request {
  jsonrpc: "2.0";
  id?: number | string | null;
  method: string;
  params?: Record<string, unknown>;
}

function fieldSchema(f: FieldSpec): Record<string, Json> {
  const base = { description: f.description };
  switch (f.type) {
    case "date":
      return { ...base, type: "string", pattern: "^\\d{4}-\\d{2}-\\d{2}$" };
    case "string":
      return { ...base, type: "string" };
    case "string[]":
      return { ...base, type: "array", items: { type: "string" } };
    case "integer":
      return { ...base, type: "integer" };
    case "boolean":
      return { ...base, type: "boolean" };
    case "string|null":
      return { ...base, type: ["string", "null"] };
  }
}

/** Elenco degli strumenti MCP derivato dal contratto di consultazione. */
export function toolList(): Record<string, Json>[] {
  return OPERATIONS.map((op) => ({
    name: `${TOOL_PREFIX}${op.name}`,
    description: `${op.description} (sola lettura; importi in unita' minime)`,
    inputSchema: {
      type: "object",
      properties: Object.fromEntries(Object.entries(op.input).map(([k, f]) => [k, fieldSchema(f)])),
      required: Object.entries(op.input)
        .filter(([, f]) => f.required)
        .map(([k]) => k),
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  }));
}

const today = () => new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Rome" }).format(new Date());

/** Accesso al database per una sola chiamata (apertura e chiusura comprese). */
export type Reader = <T>(fn: (db: Db) => Promise<T>) => Promise<T>;

/** Gestisce un messaggio; restituisce la risposta (o null per le notifiche). */
export async function handle(read: Reader, req: Request): Promise<Record<string, unknown> | null> {
  const reply = (result: unknown) => ({ jsonrpc: "2.0", id: req.id ?? null, result });
  const error = (code: number, message: string) => ({
    jsonrpc: "2.0",
    id: req.id ?? null,
    error: { code, message },
  });
  const isNotification = req.id === undefined;
  switch (req.method) {
    case "initialize":
      return reply({
        protocolVersion:
          typeof req.params?.protocolVersion === "string" ? req.params.protocolVersion : DEFAULT_PROTOCOL,
        capabilities: { tools: { listChanged: false } },
        serverInfo: SERVER_INFO,
        instructions:
          "Cloud Finance in sola lettura: riepiloghi, saldi osservati, movimenti, crediti/debiti, stato dei dati. " +
          "Importi in unita' minime (centesimi). I movimenti incerti restano 'da classificare'; i saldi non osservati non vengono stimati.",
      });
    case "notifications/initialized":
    case "notifications/cancelled":
      return null;
    case "ping":
      return reply({});
    case "tools/list":
      return reply({ tools: toolList() });
    case "tools/call": {
      const name = String(req.params?.name ?? "");
      if (!name.startsWith(TOOL_PREFIX)) return error(-32602, `strumento sconosciuto: ${name}`);
      let res: Awaited<ReturnType<typeof runQuery>>;
      try {
        res = await read((db) =>
          runQuery(db, name.slice(TOOL_PREFIX.length), req.params?.arguments ?? {}, { today: today() }),
        );
      } catch (err) {
        if (!(err instanceof SnapshotUnavailableError)) throw err;
        const e = { code: "database_unavailable", reason: err.reason, message: err.message };
        return reply({ content: [{ type: "text", text: JSON.stringify(e) }], isError: true });
      }
      return reply({
        content: [{ type: "text", text: JSON.stringify(res.ok ? res.data : res.error) }],
        structuredContent: res,
        isError: !res.ok,
      });
    }
    default:
      return isNotification ? null : error(-32601, `metodo non supportato: ${req.method}`);
  }
}

async function main(argv: string[]): Promise<number> {
  const { values } = parseArgs({ args: argv, options: { db: { type: "string" } } });
  const path = values.db ?? process.env.CLOUD_FINANCE_DB;
  if (path === undefined || !existsSync(path)) {
    process.stderr.write("uso: mcp-server.ts --db <database.sqlite3> (il file deve esistere)\n");
    return 64;
  }
  const read: Reader = (fn) => readImmutable(path, fn);
  process.stderr.write("cloud-finance MCP: avviato in sola lettura (immutable)\n");
  const rl = createInterface({ input: process.stdin, crlfDelay: Number.POSITIVE_INFINITY });
  for await (const line of rl) {
    if (line.trim() === "") continue;
    let req: Request;
    try {
      req = JSON.parse(line) as Request;
    } catch {
      process.stdout.write(
        `${JSON.stringify({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "JSON non valido" } })}\n`,
      );
      continue;
    }
    try {
      const res = await handle(read, req);
      if (res !== null) process.stdout.write(`${JSON.stringify(res)}\n`);
    } catch {
      process.stdout.write(
        `${JSON.stringify({ jsonrpc: "2.0", id: req.id ?? null, error: { code: -32603, message: "errore interno" } })}\n`,
      );
    }
  }
  return 0;
}

if (import.meta.main) {
  main(process.argv.slice(2)).then(
    (code) => {
      process.exitCode = code;
    },
    (err: unknown) => {
      process.stderr.write(`errore: ${err instanceof Error ? err.message : String(err)}\n`);
      process.exitCode = 2;
    },
  );
}
