/**
 * Notifiche push Web (C3) SENZA contenuto: al servizio push (Apple, Google, Mozilla, Microsoft) arriva solo un segnale
 * firmato VAPID (RFC 8292, ES256); il testo resta nel Durable Object e l'app lo legge con l'accesso (op `notifiche`).
 * Cosi' niente cifratura del contenuto (RFC 8291) e nessun dato Finance presso terzi.
 * Chiave VAPID: `VAPID_PRIVATE_JWK` (JWK EC P-256 privata, da segreto) e `VAPID_SUBJECT` (mailto: o https:). Senza
 * chiave valida nessun push parte.
 */
import { ValidationError } from "../../finance/src/context.ts";

const PUSH_HOSTS = [
  /^web\.push\.apple\.com$/,
  /^fcm\.googleapis\.com$/,
  /^updates\.push\.services\.mozilla\.com$/,
  /^[a-z0-9-]+\.notify\.windows\.com$/,
];

/** Solo https, porta standard, nessuna credenziale, host di un servizio push noto (niente richieste verso altri siti). */
export function checkEndpoint(value: unknown): string {
  let u: URL;
  try {
    u = new URL(String(value));
  } catch {
    throw new ValidationError("indirizzo push non valido");
  }
  if (
    String(value).length > 1024 ||
    u.protocol !== "https:" ||
    u.port !== "" ||
    u.username !== "" ||
    u.password !== "" ||
    !PUSH_HOSTS.some((h) => h.test(u.hostname))
  )
    throw new ValidationError("servizio push non ammesso");
  return u.href;
}

export interface VapidKey {
  jwk: { kty: "EC"; crv: "P-256"; x: string; y: string; d: string };
  subject: string;
}

export function parseVapid(jwkText: string | undefined, subject: string | undefined): VapidKey | null {
  try {
    const j = JSON.parse(jwkText ?? "");
    if (j.kty !== "EC" || j.crv !== "P-256" || ![j.x, j.y, j.d].every((v) => typeof v === "string"))
      return null;
    if (!/^(mailto:|https:\/\/)\S+$/.test(subject ?? "")) return null;
    return { jwk: { kty: "EC", crv: "P-256", x: j.x, y: j.y, d: j.d }, subject: subject as string };
  } catch {
    return null;
  }
}

const utf8 = (s: string) => new TextEncoder().encode(s);
const b64url = (b: Uint8Array) =>
  btoa(String.fromCharCode(...b))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
const unb64url = (s: string) =>
  Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));

/** Intestazioni di un push senza contenuto: token VAPID valido 12 ore per l'origine del servizio. */
export async function vapidHeaders(
  key: VapidKey,
  endpoint: string,
  nowS: number,
): Promise<Record<string, string>> {
  const enc = (o: unknown) => b64url(utf8(JSON.stringify(o)));
  const body = `${enc({ typ: "JWT", alg: "ES256" })}.${enc({
    aud: new URL(endpoint).origin,
    exp: nowS + 12 * 3600,
    sub: key.subject,
  })}`;
  const k = await crypto.subtle.importKey("jwk", key.jwk, { name: "ECDSA", namedCurve: "P-256" }, false, [
    "sign",
  ]);
  const sig = new Uint8Array(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, k, utf8(body)));
  const pub = new Uint8Array([4, ...unb64url(key.jwk.x), ...unb64url(key.jwk.y)]);
  return {
    Authorization: `vapid t=${body}.${b64url(sig)}, k=${b64url(pub)}`,
    TTL: "86400",
    Urgency: "normal",
  };
}

/** Chiave pubblica VAPID (punto P-256 non compresso, base64url) ricavata dalla privata: e' quella che la pagina passa
 * a `pushManager.subscribe` come `applicationServerKey`. */
export const publicKeyOf = (key: VapidKey) =>
  b64url(new Uint8Array([4, ...unb64url(key.jwk.x), ...unb64url(key.jwk.y)]));

/** Stato del push per la pagina: chiave pubblica di `vars.VAPID_PUBLIC_KEY` solo se coerente con il segreto. */
export function pushInfo(key: VapidKey | null, publicVar: string | undefined) {
  const pub = (publicVar ?? "").trim();
  if (key === null) return { chiavePubblica: null, motivo: "chiave_privata_o_subject_mancanti" };
  if (pub === "") return { chiavePubblica: null, motivo: "chiave_pubblica_mancante" };
  if (pub !== publicKeyOf(key)) return { chiavePubblica: null, motivo: "chiavi_vapid_non_coerenti" };
  return { chiavePubblica: pub, motivo: null };
}
