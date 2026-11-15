#!/usr/bin/env node
/**
 * Chiavi VAPID delle notifiche push di CUORE (una volta sola; nessuna rete, nessun account).
 *
 *   npm run vapid                 crea la coppia: la PRIVATA va in un file 0600 fuori da iCloud e dal repository
 *                                 (~/Library/Application Support/CUORE/vapid-privata.json), mai a schermo; stampa la
 *                                 PUBBLICA (non segreta) e i comandi da lanciare
 *   npm run vapid -- --file X     altro file per la privata (stesse regole)
 *
 * Poi (lo fa il titolare, con l'ok per la pubblicazione):
 *   ../finance/node_modules/.bin/wrangler secret put VAPID_PRIVATE_JWK < "<file>"   (la chiave non si vede mai)
 *   in wrangler.jsonc, vars: "VAPID_PUBLIC_KEY": "<pubblica>", "VAPID_SUBJECT": "https://cuore.<sottodominio>.workers.dev"
 * Un file gia' esistente non viene mai sovrascritto: cambiare chiave obbliga a reiscrivere ogni dispositivo.
 */
import { webcrypto } from "node:crypto";
import { existsSync, mkdirSync, realpathSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, relative } from "node:path";
import { parseArgs } from "node:util";

const REPO = new URL("../..", import.meta.url).pathname;
export const FILE_PREDEFINITO = join(homedir(), "Library/Application Support/CUORE/vapid-privata.json");

export interface ChiaviVapid {
  privata: { kty: "EC"; crv: "P-256"; x: string; y: string; d: string };
  pubblica: string;
}

/** Coppia P-256 nuova: JWK privato (per il segreto) e chiave pubblica base64url non compressa (per la pagina). */
export async function generaVapid(): Promise<ChiaviVapid> {
  const kp = await webcrypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, [
    "sign",
    "verify",
  ]);
  const j = (await webcrypto.subtle.exportKey("jwk", kp.privateKey)) as Record<string, string>;
  const raw = new Uint8Array(await webcrypto.subtle.exportKey("raw", kp.publicKey));
  return {
    privata: { kty: "EC", crv: "P-256", x: j.x as string, y: j.y as string, d: j.d as string },
    pubblica: Buffer.from(raw).toString("base64url"),
  };
}

const dentro = (figlio: string, padre: string) => {
  const r = relative(padre, figlio);
  return r === "" || !(r.startsWith("..") || isAbsolute(r));
};

/** Il file della privata: assoluto, .json, mai in iCloud, nel repository o gia' esistente. */
export function controllaFile(path: string): string | null {
  if (!isAbsolute(path) || !path.endsWith(".json")) return "serve un percorso assoluto di un file .json";
  const lib = join(homedir(), "Library");
  const vero = existsSync(dirname(path)) ? join(realpathSync(dirname(path)), "x") : path;
  for (const f of [join(lib, "Mobile Documents"), join(lib, "CloudStorage"), REPO])
    if (dentro(path, f) || dentro(vero, existsSync(f) ? realpathSync(f) : f))
      return "mai in iCloud o nel repository";
  if (existsSync(path))
    return "il file esiste gia': non lo sovrascrivo (spostalo nel Cestino solo se vuoi chiavi nuove)";
  return null;
}

async function main() {
  const { values } = parseArgs({ options: { file: { type: "string" } } });
  const file = values.file ?? FILE_PREDEFINITO;
  const no = controllaFile(file);
  if (no !== null) {
    console.error(`Chiavi VAPID NON create: ${no} (${file})`);
    process.exit(2);
  }
  const k = await generaVapid();
  mkdirSync(dirname(file), { recursive: true, mode: 0o700 });
  writeFileSync(file, `${JSON.stringify(k.privata)}\n`, { mode: 0o600, flag: "wx" });
  console.log(`Chiave privata salvata (non mostrata) in: ${file}`);
  console.log(`Chiave PUBBLICA (non segreta): ${k.pubblica}`);
  console.log("\nProssimi passi (dalla cartella cloud/):");
  console.log(`  1. ../finance/node_modules/.bin/wrangler secret put VAPID_PRIVATE_JWK < "${file}"`);
  console.log(`  2. wrangler.jsonc, vars: "VAPID_PUBLIC_KEY": "${k.pubblica}"`);
  console.log('     e "VAPID_SUBJECT": "https://cuore.<sottodominio>.workers.dev" (oppure mailto:)');
  console.log("  3. npm run configurazione, poi la pubblicazione (con l'ok)");
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
