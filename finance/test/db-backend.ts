/**
 * Database dei test: SQLite via Node (predefinito) oppure Cloudflare D1 LOCALE (Miniflare/workerd) con
 * `CLOUD_FINANCE_TEST_DB=d1`. Gli stessi test girano sui due motori: nessun test ha una versione "per D1".
 *
 * D1: un'istanza Miniflare per processo di test con N database vuoti e indipendenti (DB0..DBn-1); se finiscono se ne
 * avvia un'altra. Nessuna persistenza su disco, nessuna risorsa remota, nessuna credenziale.
 */
import { after } from "node:test";
import { type D1DatabaseLike, D1Db } from "../src/db/d1.ts";
import { NodeSqliteDb } from "../src/db/node-sqlite.ts";
import type { Db } from "../src/db/types.ts";

export const BACKEND: "node" | "d1" = process.env.CLOUD_FINANCE_TEST_DB === "d1" ? "d1" : "node";

const POOL_SIZE = 64;

interface MiniflareLike {
  ready: Promise<unknown>;
  getD1Database(name: string): Promise<unknown>;
  dispatchFetch(url: string, init?: RequestInit): Promise<Response>;
  dispose(): Promise<void>;
}

const instances: MiniflareLike[] = [];
let current: { mf: MiniflareLike; next: number } | null = null;

after(async () => {
  for (const mf of instances) await mf.dispose();
});

/**
 * Avvia Miniflare (workerd reale) con le opzioni indicate; chiuso automaticamente a fine file. `cf: false`: senza,
 * Miniflare scarica https://workers.cloudflare.com/cf.json (cache di 30 giorni in node_modules/.mf): niente rete.
 */
export async function startMiniflare(options: Record<string, unknown>): Promise<MiniflareLike> {
  const { Miniflare, convertV4MiniflareOptions } = await import("miniflare");
  const mf = new Miniflare(
    convertV4MiniflareOptions({ cf: false, ...options } as never),
  ) as unknown as MiniflareLike;
  await mf.ready;
  instances.push(mf);
  return mf;
}

async function nextD1(): Promise<D1DatabaseLike> {
  if (current === null || current.next >= POOL_SIZE) {
    const d1Databases = Object.fromEntries(
      Array.from({ length: POOL_SIZE }, (_, i) => [`DB${i}`, `test-${instances.length}-${i}`]),
    );
    const mf = await startMiniflare({
      modules: true,
      script: "export default { fetch() { return new Response('') } }",
      compatibilityDate: "2026-09-01",
      d1Databases,
    });
    current = { mf, next: 0 };
  }
  const name = `DB${current.next}`;
  current.next += 1;
  return (await current.mf.getD1Database(name)) as D1DatabaseLike;
}

/** Binding D1 locale grezzo (senza adattatore), per provare il comportamento di D1 stesso. */
export function rawD1(): Promise<D1DatabaseLike> {
  return nextD1();
}

/** Database vuoto (non migrato) sul motore scelto. */
export async function freshDb(): Promise<Db> {
  return BACKEND === "d1" ? new D1Db(await nextD1()) : new NodeSqliteDb(":memory:");
}

/**
 * Controllo d'integrita' disponibile su ENTRAMBI i motori: `quick_check` + `foreign_key_check`
 * (`integrity_check` non e' autorizzato su D1). Su Node si aggiunge anche `integrity_check`.
 */
export async function integrityCheck(db: Db): Promise<string> {
  const quick = await db.first<{ quick_check: string }>("PRAGMA quick_check");
  const fk = await db.all("PRAGMA foreign_key_check");
  const problems: string[] = [];
  if (quick?.quick_check !== "ok") problems.push(`quick_check: ${quick?.quick_check}`);
  if (fk.length > 0) problems.push(`foreign_key_check: ${fk.length} violazioni`);
  if (db instanceof NodeSqliteDb && db.integrityCheck() !== "ok") problems.push("integrity_check");
  return problems.length === 0 ? "ok" : problems.join("; ");
}
