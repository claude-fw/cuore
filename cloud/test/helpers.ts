/**
 * Strumenti dei test della vetrina: chiavi RSA di PROVA generate a ogni esecuzione, token con la forma di quelli di
 * Cloudflare Access, impacchettamento degli ingressi con esbuild e avvio in workerd (Miniflare) senza rete.
 * Nessuna credenziale reale: domini `.invalid` o di prova, email fittizie.
 */
import { webcrypto } from "node:crypto";
import { createRequire } from "node:module";
import { after } from "node:test";
import type { RsaJwk } from "../src/access.ts";

const FINANCE = new URL("../../finance/package.json", import.meta.url).pathname;
const req = createRequire(FINANCE);

export const TEAM = "prova-cuore.cloudflareaccess.com";
export const AUD = "aud0prova0vetrina0cuore0000000000000000000000000000000000000000";
export const EMAIL = "titolare@prova.invalid";

export interface TestKey {
  kid: string;
  privateKey: webcrypto.CryptoKey;
  jwk: RsaJwk;
}

export async function testKey(kid: string): Promise<TestKey> {
  const kp = await webcrypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"],
  );
  const pub = (await webcrypto.subtle.exportKey("jwk", kp.publicKey)) as { n: string; e: string };
  return { kid, privateKey: kp.privateKey, jwk: { kid, kty: "RSA", n: pub.n, e: pub.e, alg: "RS256" } };
}

const b64url = (b: Uint8Array | string) =>
  Buffer.from(typeof b === "string" ? new TextEncoder().encode(b) : b).toString("base64url");

export const nowS = () => Math.floor(Date.now() / 1000);

/** Token con i claim di un token d'applicazione di Access; `claims` e `header` sovrascrivono (null = rimuove). */
export async function token(
  key: TestKey,
  claims: Record<string, unknown> = {},
  header: Record<string, unknown> = {},
): Promise<string> {
  const t = nowS();
  const base: Record<string, unknown> = {
    iss: `https://${TEAM}`,
    aud: [AUD],
    email: EMAIL,
    type: "app",
    iat: t,
    nbf: t,
    exp: t + 3600,
    sub: "utente-di-prova",
    ...claims,
  };
  for (const [k, v] of Object.entries(base)) if (v === null) delete base[k];
  const h = { alg: "RS256", kid: key.kid, typ: "JWT", ...header };
  const body = `${b64url(JSON.stringify(h))}.${b64url(JSON.stringify(base))}`;
  const sig = await webcrypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key.privateKey,
    new TextEncoder().encode(body),
  );
  return `${body}.${b64url(new Uint8Array(sig))}`;
}

/** Impacchetta un ingresso del Worker come farebbe wrangler (testo per .html e .csv, JSON con attributo). */
export async function bundle(entry: "index" | "locale" | "misure"): Promise<string> {
  const { build } = req("esbuild") as {
    build(o: Record<string, unknown>): Promise<{ outputFiles?: { text: string }[] }>;
  };
  const out = await build({
    entryPoints: [new URL(`../src/${entry}.ts`, import.meta.url).pathname],
    bundle: true,
    format: "esm",
    platform: "browser",
    target: "es2024",
    write: false,
    logLevel: "silent",
    external: ["cloudflare:workers"],
    loader: { ".html": "text", ".csv": "text", ".webmanifest": "text", ".png": "binary" },
    // come le regole di wrangler.jsonc: il service worker e push.js della pagina entrano come testo
    plugins: [
      {
        name: "testo-app",
        setup(b: { onLoad(o: { filter: RegExp }, f: (a: { path: string }) => unknown): void }) {
          b.onLoad({ filter: /[\\/]app[\\/](sw|push)\.js$/ }, async (a) => ({
            contents: await (await import("node:fs/promises")).readFile(a.path, "utf8"),
            loader: "text",
          }));
        },
      },
    ],
  });
  return out.outputFiles?.[0]?.text ?? "";
}

interface MiniflareLike {
  ready: Promise<unknown>;
  dispatchFetch(url: string, init?: RequestInit): Promise<Response>;
  dispose(): Promise<void>;
}
const running: MiniflareLike[] = [];
after(async () => {
  for (const mf of running) await mf.dispose();
});

/**
 * Avvia l'ingresso in workerd con un Durable Object SQLite in memoria; `outbound` riceve ogni fetch in uscita.
 * `extra`: altre opzioni di Miniflare (es. `r2Buckets: ["BACKUP"]`, bucket in memoria; `durableObjects` del banco).
 */
export async function startWorker(
  script: string,
  bindings: Record<string, string>,
  outbound: (request: Request) => Response | Promise<Response>,
  extra: Record<string, unknown> = {},
): Promise<MiniflareLike> {
  const { Miniflare, convertV4MiniflareOptions } = req("miniflare") as {
    Miniflare: new (o: unknown) => MiniflareLike;
    convertV4MiniflareOptions: (o: unknown) => unknown;
  };
  const mf = new Miniflare(
    convertV4MiniflareOptions({
      modules: true,
      script,
      compatibilityDate: "2026-09-01",
      bindings,
      durableObjects: {
        FINANCE: { className: "FinanceVetrina", useSQLite: true },
        SCHOOL: { className: "SchoolFeed", useSQLite: true },
      },
      outboundService: outbound,
      // senza `cf: false` Miniflare scarica https://workers.cloudflare.com/cf.json (cache di 30 giorni): niente rete
      cf: false,
      ...extra,
    }),
  );
  await mf.ready;
  running.push(mf);
  return mf;
}
