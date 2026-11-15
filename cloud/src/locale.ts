/**
 * Ingresso SOLO LOCALE della vetrina (wrangler.locale.jsonc, `wrangler dev` senza --remote). Fa la parte che in
 * produzione fa Cloudflare Access: una pagina di accesso SIMULATO firma, con una chiave generata in memoria a ogni avvio,
 * un token con la stessa forma di quello di Access e lo mette nell'intestazione `Cf-Access-Jwt-Assertion`. La verifica
 * resta quella di produzione (app.ts → access.ts), con le stesse regole: firma, emittente, audience, scadenza, email.
 * Simula anche il token di servizio del Mac (E3): con `CF-Access-Client-Id/Secret` uguali ai valori fittizi
 * `SIMULATED_SERVICE` (o `SIMULATED_BACKUP_SERVICE`, L1) firma un token senza email con `common_name` = Client ID, come
 * fa Access; valori diversi = 403 (come Access); le due intestazioni non arrivano mai oltre questo ingresso.
 *
 * Non puo' servire in rete: risponde solo a localhost/127.0.0.1 (403 per ogni altro host), la configurazione locale non
 * pubblica nessun indirizzo (`workers_dev: false`) e l'ingresso di produzione (index.ts) non contiene questo codice.
 */

import type { webcrypto } from "node:crypto";
import page from "../../app/index.html";
import { type AccessConfig, parseServiceIds, type RsaJwk } from "./access.ts";
import { ACCESS_HEADER, financeName, handle, runCron, SECURITY_HEADERS, writesOn } from "./app.ts";
import { ASSETS } from "./assets.ts";
import type { BackupBucket } from "./backup-cloud.ts";

import { origineMac } from "./migrazione.ts";

export { FinanceVetrina } from "./finance-do.ts";
export { SchoolFeed } from "./school-do.ts";

interface Env {
  FINANCE: DurableObjectNamespace;
  SCHOOL?: DurableObjectNamespace;
  ACCESS_SERVIZIO_SCHOOL?: string;
  ACCESS_SERVIZIO_MIGRAZIONE?: string;
  ACCESS_SERVIZIO_BACKUP?: string;
  BACKUP?: BackupBucket;
  CUORE_FINANCE_ORIGINE?: string;
  CUORE_STATO?: string;
  CUORE_SCRITTURE?: string;
  CATTURA_TOKEN_SHA256?: string;
  BACKUP_SIGNING_PRIVATE_JWK?: string;
  BACKUP_MAC_PUBLIC_JWK?: string;
  BACKUP_RECOVERY_PUBLIC_JWK?: string;
}

export const LOCAL_HOSTS: ReadonlySet<string> = new Set(["localhost", "127.0.0.1", "[::1]"]);
const COOKIE = "cuore_accesso_simulato";
const KID = "simulato-locale";
/** Identita' fittizie: dominio riservato `.invalid`, mai un indirizzo reale. */
export const SIMULATED: AccessConfig = {
  teamDomain: "accesso-simulato.invalid",
  aud: "vetrinalocalesimulata0000000000",
  allowedEmails: ["titolare@vetrina.invalid"],
};
const OUTSIDER = "estraneo@vetrina.invalid";
/** Token di servizio FITTIZIO del Mac (solo locale, mai un valore reale): stesso formato del Client ID di Access. */
export const SIMULATED_SERVICE = {
  clientId: "macschoolsimulato0001.access",
  clientSecret: "segreto-simulato-mac-school-solo-locale-0001",
} as const;
/** L1: token di servizio FITTIZIO `mac-backup` (ritiro dei backup), solo locale. */
export const SIMULATED_BACKUP_SERVICE = {
  clientId: "macbackupsimulato0001.access",
  clientSecret: "segreto-simulato-mac-backup-solo-locale-0001",
} as const;
const CLIENT_ID = "CF-Access-Client-Id";
const CLIENT_SECRET = "CF-Access-Client-Secret";

let pair: Promise<{ privateKey: webcrypto.CryptoKey; jwk: RsaJwk }> | null = null;
function keyPair() {
  pair ??= (async () => {
    const kp = (await crypto.subtle.generateKey(
      {
        name: "RSASSA-PKCS1-v1_5",
        modulusLength: 2048,
        publicExponent: new Uint8Array([1, 0, 1]),
        hash: "SHA-256",
      },
      true,
      ["sign", "verify"],
    )) as webcrypto.CryptoKeyPair;
    const pub = (await crypto.subtle.exportKey("jwk", kp.publicKey)) as { n: string; e: string };
    return { privateKey: kp.privateKey, jwk: { kid: KID, kty: "RSA", n: pub.n, e: pub.e, alg: "RS256" } };
  })();
  return pair;
}

const b64url = (bytes: Uint8Array) =>
  btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");

/** Token simulato: del titolare (`email`) o di servizio (`common_name`, senza email: come Access). */
async function simulatedToken(who: { email: string } | { common_name: string }): Promise<string> {
  const { privateKey } = await keyPair();
  const now = Math.floor(Date.now() / 1000);
  const enc = (o: unknown) => b64url(new TextEncoder().encode(JSON.stringify(o)));
  const body = `${enc({ alg: "RS256", kid: KID, typ: "JWT" })}.${enc({
    iss: `https://${SIMULATED.teamDomain}`,
    aud: [SIMULATED.aud],
    ...who,
    type: "app",
    iat: now,
    nbf: now,
    exp: now + 24 * 3600,
    sub: "email" in who ? "simulato" : "",
  })}`;
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", privateKey, new TextEncoder().encode(body));
  return `${body}.${b64url(new Uint8Array(sig))}`;
}

const html = (status: number, body: string, extra: Record<string, string> = {}) =>
  new Response(body, {
    status,
    headers: { ...SECURITY_HEADERS, "Content-Type": "text/html; charset=utf-8", ...extra },
  });

const LOGIN_PAGE = `<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CUORE · accesso simulato</title><style>body{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 16px;color:#1d1d1f}
.b{background:#fff4e5;border:1px solid #f0b46a;border-radius:12px;padding:12px 14px}a{display:block;margin:14px 0;padding:12px 14px;border-radius:12px;background:#f2f2f7;color:#0a58ca;text-decoration:none}</style></head>
<body><p class="b"><b>ACCESSO SIMULATO · SOLO LOCALE.</b> Questa pagina non è un login: sostituisce Cloudflare Access per provare la vetrina
sul Mac. In produzione l'accesso è di Cloudflare Access (GitHub) e questa pagina non esiste.</p>
<a href="/__locale/accedi?come=titolare">Entra come titolare simulato (titolare@vetrina.invalid)</a>
<a href="/__locale/accedi?come=estraneo">Prova con un utente non ammesso (deve essere rifiutato)</a>
<a href="/__locale/esci">Esci (cancella il token simulato)</a></body></html>`;

const BANNER =
  '<div role="note" style="position:sticky;top:0;z-index:99;background:#fff4e5;color:#7a4b00;border-bottom:1px solid #f0b46a;padding:6px 12px;font:13px/1.4 -apple-system,system-ui,sans-serif;text-align:center">' +
  "<b>ACCESSO SIMULATO · SOLO LOCALE</b> — in produzione l'accesso è verificato da Cloudflare Access · " +
  '<a href="/__locale/esci" style="color:inherit">esci</a></div>';

const decorate = (h: string) => h.replace(/<body([^>]*)>/, (m) => `${m}\n${BANNER}`);

function cookie(request: Request): string | null {
  for (const part of (request.headers.get("Cookie") ?? "").split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === COOKIE) return v.join("=");
  }
  return null;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (!LOCAL_HOSTS.has(url.hostname))
      return new Response("Ingresso locale: risponde solo a localhost.", {
        status: 403,
        headers: { ...SECURITY_HEADERS, "Content-Type": "text/plain; charset=utf-8" },
      });
    if (url.pathname === "/__locale/backup") {
      const r = await env.FINANCE.get(env.FINANCE.idFromName(financeName(env.CUORE_FINANCE_ORIGINE))).fetch(
        new Request("https://finance.interno/backup/esporta", { method: "POST" }),
      );
      return new Response(await r.text(), {
        status: r.status,
        headers: { ...SECURITY_HEADERS, "Content-Type": "application/json; charset=utf-8" },
      });
    }
    if (url.pathname === "/__locale/accedi") {
      const who = url.searchParams.get("come");
      if (who !== "titolare" && who !== "estraneo") return html(200, LOGIN_PAGE);
      const token = await simulatedToken({
        email: who === "titolare" ? (SIMULATED.allowedEmails[0] as string) : OUTSIDER,
      });
      return html(303, "", {
        Location: "/",
        "Set-Cookie": `${COOKIE}=${token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400`,
      });
    }
    if (url.pathname === "/__locale/esci")
      return html(303, "", {
        Location: "/__locale/accedi",
        "Set-Cookie": `${COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0`,
      });

    // come Access: l'intestazione arriva solo da qui, mai dal client
    const headers = new Headers(request.headers);
    headers.delete(ACCESS_HEADER);
    const service = [headers.get(CLIENT_ID), headers.get(CLIENT_SECRET)];
    headers.delete(CLIENT_ID);
    headers.delete(CLIENT_SECRET);
    if (service[0] !== null || service[1] !== null) {
      const ok = [SIMULATED_SERVICE, SIMULATED_BACKUP_SERVICE].find(
        (x) => x.clientId === service[0] && x.clientSecret === service[1],
      );
      if (ok === undefined)
        return new Response("Token di servizio non valido (accesso simulato).", {
          status: 403,
          headers: { ...SECURITY_HEADERS, "Content-Type": "text/plain; charset=utf-8" },
        });
      headers.set(ACCESS_HEADER, await simulatedToken({ common_name: ok.clientId }));
    }
    const token = service[0] !== null ? null : cookie(request);
    if (token === null && request.method === "GET" && url.pathname === "/")
      return html(302, "", { Location: "/__locale/accedi" });
    if (token !== null) headers.set(ACCESS_HEADER, token);
    const { jwk } = await keyPair();
    const school = env.SCHOOL;
    const body = request.method === "POST" ? await request.arrayBuffer() : null;
    return handle(new Request(request.url, { method: request.method, headers, body }), {
      config: SIMULATED,
      keys: async (kid) => (kid === jwk.kid ? jwk : null),
      nowMs: Date.now,
      page,
      assets: ASSETS,
      finance: () => env.FINANCE.get(env.FINANCE.idFromName(financeName(env.CUORE_FINANCE_ORIGINE))),
      decoratePage: decorate,
      state: env.CUORE_STATO,
      writes: writesOn(env.CUORE_SCRITTURE),
      catturaTokenHash: env.CATTURA_TOKEN_SHA256,
      ...(school === undefined ? {} : { school: () => school.get(school.idFromName("school")) }),
      schoolServiceIds: parseServiceIds(env.ACCESS_SERVIZIO_SCHOOL),
      migrazioneServiceIds: parseServiceIds(env.ACCESS_SERVIZIO_MIGRAZIONE),
      origineMac: origineMac(env.CUORE_FINANCE_ORIGINE),
      ...(env.BACKUP === undefined ? {} : { backupBucket: env.BACKUP }),
      backupServiceIds: parseServiceIds(env.ACCESS_SERVIZIO_BACKUP),
    });
  },
  async scheduled(controller: { scheduledTime: number; cron?: string }, env: Env): Promise<void> {
    await runCron(
      env.CUORE_STATO,
      env.FINANCE.get(env.FINANCE.idFromName(financeName(env.CUORE_FINANCE_ORIGINE))),
      controller.scheduledTime,
      controller.cron,
    );
  },
};
