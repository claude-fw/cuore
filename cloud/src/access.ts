/**
 * Verifica del token di Cloudflare Access (`Cf-Access-Jwt-Assertion`), secondo la documentazione ufficiale
 * ("Validate JWTs", aggiornata il 06/05/2026; "Application token", 25/06/2026): RS256, chiave scelta per `kid` fra quelle
 * pubblicate in `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`, `iss` = dominio del team, `aud` = AUD tag
 * dell'applicazione. In piu', per prudenza: `exp`, `nbf`, `iat`, `type` = "app" ed email in un elenco esplicito.
 *
 * Nessuna dipendenza: solo Web Crypto (uguale in workerd e in Node). Gli esiti negativi sono parole fisse: mai il
 * token, mai l'email ricevuta, mai dettagli della chiave.
 */

export interface AccessConfig {
  /** es. `squadra.cloudflareaccess.com` (senza schema) */
  readonly teamDomain: string;
  /** Application Audience (AUD) tag */
  readonly aud: string;
  /** email ammesse, minuscole */
  readonly allowedEmails: readonly string[];
}

export interface RsaJwk {
  readonly kid: string;
  readonly kty: string;
  readonly n: string;
  readonly e: string;
  readonly alg?: string;
}

/** Restituisce la chiave pubblica per `kid` (null = sconosciuta). In produzione: certs del team; nei test: chiavi di prova. */
export type KeySource = (kid: string) => Promise<RsaJwk | null>;

export type Rejection =
  | "token_assente"
  | "token_malformato"
  | "algoritmo"
  | "chiave_sconosciuta"
  | "firma"
  | "emittente"
  | "audience"
  | "scaduto"
  | "non_ancora_valido"
  | "emesso_nel_futuro"
  | "tipo"
  | "email_assente"
  | "email_non_ammessa"
  | "non_di_servizio"
  | "servizio_non_ammesso";

export type Verdict = { ok: true; email: string } | { ok: false; reason: Rejection };
export type ServiceVerdict = { ok: true; clientId: string } | { ok: false; reason: Rejection };

/** Tolleranza sugli orari (secondi) fra gli orologi di Access e del Worker. */
export const CLOCK_SKEW_S = 60;
const MAX_TOKEN_LENGTH = 8192;
const TEAM_DOMAIN = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com$/;
const AUD = /^[A-Za-z0-9]{16,128}$/;
const EMAIL = /^[^\s@,]{1,64}@[^\s@,]{1,255}\.[a-z]{2,}$/;

/**
 * Configurazione dalle variabili del Worker. Qualunque mancanza o valore non valido = configurazione assente: il
 * Worker allora rifiuta tutto (chiuso per difetto), non apre.
 */
export function parseAccessConfig(env: {
  ACCESS_TEAM_DOMAIN?: unknown;
  ACCESS_AUD?: unknown;
  ACCESS_ALLOWED_EMAILS?: unknown;
}): { config: AccessConfig; problems: [] } | { config: null; problems: string[] } {
  const problems: string[] = [];
  const team = typeof env.ACCESS_TEAM_DOMAIN === "string" ? env.ACCESS_TEAM_DOMAIN.trim().toLowerCase() : "";
  const aud = typeof env.ACCESS_AUD === "string" ? env.ACCESS_AUD.trim() : "";
  const emails =
    typeof env.ACCESS_ALLOWED_EMAILS === "string"
      ? env.ACCESS_ALLOWED_EMAILS.split(",")
          .map((e) => e.trim().toLowerCase())
          .filter((e) => e !== "")
      : [];
  if (!TEAM_DOMAIN.test(team)) problems.push("ACCESS_TEAM_DOMAIN mancante o non valido");
  if (!AUD.test(aud)) problems.push("ACCESS_AUD mancante o non valido");
  if (emails.length === 0 || emails.length > 5 || !emails.every((e) => EMAIL.test(e)))
    problems.push("ACCESS_ALLOWED_EMAILS mancante o non valido (da 1 a 5 email)");
  if (problems.length > 0) return { config: null, problems };
  return { config: { teamDomain: team, aud, allowedEmails: emails }, problems: [] };
}

function b64urlBytes(s: string): Uint8Array<ArrayBuffer> | null {
  if (!/^[A-Za-z0-9_-]*$/.test(s)) return null;
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4);
  try {
    return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  } catch {
    return null;
  }
}

function b64urlJson(s: string): Record<string, unknown> | null {
  const bytes = b64urlBytes(s);
  if (bytes === null) return null;
  try {
    const v: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    return v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

type Claims = { ok: true; payload: Record<string, unknown> } | { ok: false; reason: Rejection };

/** Parte comune: firma, emittente, audience, orari e `type` = "app". Non lancia. */
async function verifyClaims(
  token: string | null | undefined,
  config: AccessConfig,
  keys: KeySource,
  nowSeconds: number,
): Promise<Claims> {
  const no = (reason: Rejection): Claims => ({ ok: false, reason });
  if (typeof token !== "string" || token === "") return no("token_assente");
  if (token.length > MAX_TOKEN_LENGTH) return no("token_malformato");
  const parts = token.split(".");
  if (parts.length !== 3) return no("token_malformato");
  const [h, p, sig] = parts as [string, string, string];
  const header = b64urlJson(h);
  const payload = b64urlJson(p);
  const signature = b64urlBytes(sig);
  if (header === null || payload === null || signature === null || signature.length === 0)
    return no("token_malformato");
  if (header.alg !== "RS256") return no("algoritmo");
  if (typeof header.kid !== "string" || header.kid === "") return no("chiave_sconosciuta");

  let jwk: RsaJwk | null;
  try {
    jwk = await keys(header.kid);
  } catch {
    jwk = null;
  }
  if (jwk === null || jwk.kty !== "RSA" || (jwk.alg !== undefined && jwk.alg !== "RS256"))
    return no("chiave_sconosciuta");
  let valid = false;
  try {
    const key = await crypto.subtle.importKey(
      "jwk",
      { kty: "RSA", n: jwk.n, e: jwk.e, alg: "RS256", ext: true },
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["verify"],
    );
    valid = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5",
      key,
      signature,
      new TextEncoder().encode(`${h}.${p}`),
    );
  } catch {
    valid = false;
  }
  if (!valid) return no("firma");

  // solo dopo la firma si guardano i contenuti
  if (payload.iss !== `https://${config.teamDomain}`) return no("emittente");
  const aud = payload.aud;
  const auds = typeof aud === "string" ? [aud] : Array.isArray(aud) ? aud : [];
  if (!auds.includes(config.aud)) return no("audience");
  if (!isNum(payload.exp) || payload.exp <= nowSeconds - CLOCK_SKEW_S) return no("scaduto");
  if (payload.nbf !== undefined && (!isNum(payload.nbf) || payload.nbf > nowSeconds + CLOCK_SKEW_S))
    return no("non_ancora_valido");
  if (payload.iat !== undefined && (!isNum(payload.iat) || payload.iat > nowSeconds + CLOCK_SKEW_S))
    return no("emesso_nel_futuro");
  if (payload.type !== "app") return no("tipo");
  return { ok: true, payload };
}

/** Verifica completa per il titolare. Non lancia: ogni problema e' un esito negativo con un motivo fisso. */
export async function verifyAccessJwt(
  token: string | null | undefined,
  config: AccessConfig,
  keys: KeySource,
  nowSeconds: number,
): Promise<Verdict> {
  const c = await verifyClaims(token, config, keys, nowSeconds);
  if (!c.ok) return c;
  const { payload } = c;
  // token di servizio: niente email (documentazione «Application token»), quindi non ammessi alle pagine del titolare
  if (typeof payload.email !== "string" || payload.email === "")
    return { ok: false, reason: "email_assente" };
  const email = payload.email.trim().toLowerCase();
  if (!config.allowedEmails.includes(email)) return { ok: false, reason: "email_non_ammessa" };
  return { ok: true, email };
}

/** Client ID di un token di servizio di Access (pannello «Service Tokens»): non e' un segreto, il segreto resta al Mac. */
const SERVICE_CLIENT_ID = /^[A-Za-z0-9]{8,64}\.access$/;

/**
 * Client ID ammessi su una rotta di macchina (es. `ACCESS_SERVIZIO_SCHOOL` = token `mac-school`): 1 o 2 separati da
 * virgola (2 solo durante una rotazione). Mancante o non valido = elenco vuoto = rotta chiusa.
 */
export function parseServiceIds(v: unknown): string[] {
  if (typeof v !== "string") return [];
  const ids = v
    .split(",")
    .map((x) => x.trim())
    .filter((x) => x !== "");
  return ids.length >= 1 && ids.length <= 2 && ids.every((x) => SERVICE_CLIENT_ID.test(x)) ? ids : [];
}

/**
 * Token di Access di una MACCHINA (token di servizio: Access verifica `CF-Access-Client-Id/Secret` e inoltra un token
 * firmato): stessa firma e stessi claim del titolare, ma senza email e con `common_name` = Client ID del token, che
 * deve essere fra quelli ammessi su quella rotta. Un token del titolare (con email) qui e' rifiutato, come un token di
 * servizio lo e' sulle pagine del titolare: ogni identita' ha le sue rotte.
 */
export async function verifyServiceJwt(
  token: string | null | undefined,
  config: AccessConfig,
  keys: KeySource,
  nowSeconds: number,
  allowedClientIds: readonly string[],
): Promise<ServiceVerdict> {
  const c = await verifyClaims(token, config, keys, nowSeconds);
  if (!c.ok) return c;
  const { payload } = c;
  if (payload.email !== undefined && payload.email !== "") return { ok: false, reason: "non_di_servizio" };
  const cn = payload.common_name;
  if (typeof cn !== "string" || !allowedClientIds.includes(cn))
    return { ok: false, reason: "servizio_non_ammesso" };
  return { ok: true, clientId: cn };
}

/**
 * Chiavi pubbliche del team (produzione): `GET https://<team>/cdn-cgi/access/certs`, campo `keys`. Cache in memoria
 * per `ttlMs`; un `kid` sconosciuto provoca al massimo una rilettura ogni `minRefetchMs` (rotazione delle chiavi:
 * ogni 6 settimane, la precedente valida ancora 7 giorni, secondo la documentazione).
 */
export function teamKeySource(
  teamDomain: string,
  fetcher: (url: string) => Promise<Response>,
  now: () => number = Date.now,
  ttlMs = 60 * 60 * 1000,
  minRefetchMs = 60 * 1000,
): KeySource {
  let cache: { at: number; keys: Map<string, RsaJwk> } | null = null;
  const load = async (): Promise<Map<string, RsaJwk>> => {
    const res = await fetcher(`https://${teamDomain}/cdn-cgi/access/certs`);
    if (!res.ok) throw new Error(`certs ${res.status}`);
    const body = (await res.json()) as { keys?: unknown };
    const map = new Map<string, RsaJwk>();
    for (const k of Array.isArray(body.keys) ? body.keys : []) {
      const j = k as Partial<RsaJwk>;
      if (typeof j.kid === "string" && j.kty === "RSA" && typeof j.n === "string" && typeof j.e === "string")
        map.set(j.kid, {
          kid: j.kid,
          kty: j.kty,
          n: j.n,
          e: j.e,
          ...(typeof j.alg === "string" ? { alg: j.alg } : {}),
        });
    }
    cache = { at: now(), keys: map };
    return map;
  };
  return async (kid) => {
    const c = cache;
    if (c !== null && now() - c.at < ttlMs) {
      const hit = c.keys.get(kid);
      if (hit !== undefined) return hit;
      if (now() - c.at < minRefetchMs) return null;
    }
    return (await load()).get(kid) ?? null;
  };
}
