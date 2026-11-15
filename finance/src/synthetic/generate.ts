/**
 * Generatore DETERMINISTICO della copia sintetica (D3a): solo i file di `finance/examples/sintetici`, orologio e id
 * fissi. Due sessioni hanno gli stessi dati (stessi id, impronte, importi); cambiano solo id e data della sessione.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Ctx } from "../context.ts";
import type { Migration } from "../db/migrate.ts";
import { importFile } from "../import/importer.ts";
import { createAccount } from "../services/accounts.ts";
import { classifyTransactions, publishRuleSet } from "../services/classification.ts";
import { createEmptySession, type GuardRoots, type SessionInfo } from "./guard.ts";

export const EXAMPLES_DIR = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "examples",
  "sintetici",
);
export const GENERATOR_VERSION = 1;

function deterministicCtx(ctx: Pick<Ctx, "db">): Ctx {
  let t = Date.parse("2026-01-01T00:00:00.000Z");
  let n = 0;
  return {
    db: ctx.db,
    now: () => {
      t += 1000;
      return new Date(t).toISOString();
    },
    newId: () => {
      n += 1;
      return `sint-${String(n).padStart(5, "0")}`;
    },
  };
}

const read = (name: string) => readFileSync(join(EXAMPLES_DIR, name));

export interface GeneratedSession {
  realDir: string;
  session: SessionInfo;
  counts: { transactions: number; balances: number; classified: number };
}

/** `migrations`: per difetto quelle della copia sintetica (attive + in attesa); `MIGRATIONS` = copia «come la reale». */
export async function createSyntheticSession(
  dir: unknown,
  roots?: GuardRoots,
  migrations?: readonly Migration[],
): Promise<GeneratedSession> {
  const fixed = deterministicCtx({ db: null as never }); // solo l'orologio, per le righe di schema_migrations
  const { realDir, session, db } = await createEmptySession(dir, { roots, now: fixed.now, migrations });
  try {
    const ctx = deterministicCtx({ db });
    for (let i = 0; i < 50; i++) ctx.now(); // stessi istanti dopo le migrazioni, qualunque sia il loro numero
    await createAccount(ctx, {
      id: "conto-demo",
      name: "Conto DEMO",
      type: "current",
      currency: "EUR",
      holderRole: "owner",
    });
    await createAccount(ctx, {
      id: "carta-demo",
      name: "Carta DEMO",
      type: "card",
      currency: "EUR",
      holderRole: "owner",
    });
    await publishRuleSet(ctx, JSON.parse(read("regole-demo.json").toString("utf8")));
    const files: [string, string, string][] = [
      ["conto-demo", "cloud-finance-csv", "conto-demo.csv"],
      ["carta-demo", "cloud-finance-csv", "carta-demo.csv"],
      ["conto-demo", "cloud-finance-balances-csv", "saldi-conto-demo.csv"],
    ];
    for (const [accountId, parserId, fileName] of files) {
      const r = await importFile(ctx, { accountId, parserId, fileName, content: read(fileName) });
      if (r.status !== "committed")
        throw new Error(`esempio sintetico non importato: ${fileName} (${r.status})`);
    }
    const classified = await classifyTransactions(ctx);
    const t = await db.first<{ n: number }>("SELECT count(*) AS n FROM transactions");
    const b = await db.first<{ n: number }>("SELECT count(*) AS n FROM balance_observations");
    return {
      realDir,
      session,
      counts: { transactions: t?.n ?? 0, balances: b?.n ?? 0, classified: classified.examined },
    };
  } finally {
    db.close();
  }
}
