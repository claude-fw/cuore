/**
 * Client Enable Banking (accesso ai conti PSD2, AIS) come `AccountDataSource`.
 *
 * STATO: implementato secondo la documentazione pubblica (Quick Start e API reference, consultate il 2026-10-25) e
 * provato SOLO con un trasporto finto nei test. Nessuna applicazione registrata, nessuna chiave, nessuna chiamata reale:
 * il collegamento bancario NON e' realizzato finche' il titolare non registra l'applicazione, autorizza i conti e una
 * prima sincronizzazione reale non viene collaudata.
 *
 * Autenticazione: JWT RS256 firmato con la chiave privata dell'applicazione (header `kid` = id dell'applicazione;
 * payload iss "enablebanking.com", aud "api.enablebanking.com", validita' massima un'ora). Firma con WebCrypto (Node e
 * workerd). La chiave arriva dal chiamante (file indicato dal titolare): questo modulo non legge file ne' variabili.
 * Consenso: POST /auth (banca, redirect, `access.valid_until` entro il massimo della banca, tipicamente 180 giorni) ->
 * l'utente autorizza nella banca -> POST /sessions con il `code` -> elenco dei conti (uid). Dati: GET
 * /accounts/{uid}/transactions?date_from&date_to[&continuation_key] e GET /accounts/{uid}/balances.
 */
import { type AccountDataSource, type FetchStrategy, SourceError, type TransactionsPage } from "./source.ts";

export const ENABLE_BANKING_API = "https://api.enablebanking.com";

export interface HttpResponse {
  readonly status: number;
  json(): Promise<unknown>;
}
export type Transport = (
  url: string,
  init: { method: string; headers: Record<string, string>; body?: string },
) => Promise<HttpResponse>;

export interface EnableBankingOptions {
  appId: string;
  /** chiave privata PKCS#8 in formato PEM (mai salvata da questo modulo) */
  privateKeyPem: string;
  transport?: Transport;
  baseUrl?: string;
  /** orologio in secondi (iniettato nei test) */
  nowSeconds?: () => number;
}

// Solo API web (btoa/atob): lo stesso modulo gira in Node e nel Worker di Cloudflare, dove Buffer non c'e'.
const b64url = (bytes: Uint8Array | string): string => {
  const u8 = typeof bytes === "string" ? new TextEncoder().encode(bytes) : bytes;
  let bin = "";
  for (const b of u8) bin += String.fromCharCode(b);
  return btoa(bin).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
};

function pemToDer(pem: string): Uint8Array {
  const body = pem.replace(/-----(BEGIN|END) PRIVATE KEY-----/g, "").replace(/\s+/g, "");
  if (!/^[A-Za-z0-9+/=]+$/.test(body) || body === "")
    throw new SourceError("invalid_request", "chiave privata non in formato PEM PKCS#8");
  return Uint8Array.from(atob(body), (c) => c.charCodeAt(0));
}

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);

export interface SessionInfo {
  sessionId: string;
  /** scadenza del consenso dichiarata dal fornitore (RFC 3339) */
  validUntil: string | null;
  accounts: { uid: string; currency: string | null; ibanLast4: string | null; name: string | null }[];
}

export class EnableBankingSource implements AccountDataSource {
  readonly provider = "enable-banking";
  private readonly opts: Required<Omit<EnableBankingOptions, "transport">> & { transport: Transport };
  private key: Awaited<ReturnType<typeof crypto.subtle.importKey>> | null = null;

  constructor(opts: EnableBankingOptions) {
    if (!opts.appId) throw new SourceError("invalid_request", "id dell'applicazione assente");
    this.opts = {
      appId: opts.appId,
      privateKeyPem: opts.privateKeyPem,
      baseUrl: opts.baseUrl ?? ENABLE_BANKING_API,
      nowSeconds: opts.nowSeconds ?? (() => Math.floor(Date.now() / 1000)),
      transport:
        opts.transport ??
        (async (url, init) => {
          const r = await fetch(url, init);
          return { status: r.status, json: () => r.json() };
        }),
    };
  }

  /** JWT dell'applicazione, valido un'ora. */
  async jwt(): Promise<string> {
    if (this.key === null) {
      this.key = await crypto.subtle.importKey(
        "pkcs8",
        pemToDer(this.opts.privateKeyPem),
        { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
        false,
        ["sign"],
      );
    }
    const iat = this.opts.nowSeconds();
    const head = b64url(JSON.stringify({ typ: "JWT", alg: "RS256", kid: this.opts.appId }));
    const body = b64url(
      JSON.stringify({ iss: "enablebanking.com", aud: "api.enablebanking.com", iat, exp: iat + 3600 }),
    );
    const sig = await crypto.subtle.sign(
      "RSASSA-PKCS1-v1_5",
      this.key,
      new TextEncoder().encode(`${head}.${body}`),
    );
    return `${head}.${body}.${b64url(new Uint8Array(sig))}`;
  }

  private async call(method: string, path: string, body?: Obj): Promise<Obj> {
    let res: HttpResponse;
    try {
      res = await this.opts.transport(`${this.opts.baseUrl}${path}`, {
        method,
        headers: {
          Authorization: `Bearer ${await this.jwt()}`,
          Accept: "application/json",
          ...(body ? { "Content-Type": "application/json" } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
    } catch (e) {
      if (e instanceof SourceError) throw e;
      throw new SourceError("temporary", "fornitore non raggiungibile (rete)");
    }
    const s = res.status;
    // mai il corpo dell'errore nel messaggio: puo' contenere dati del conto
    if (s === 401 || s === 403)
      throw new SourceError("consent_expired", "consenso scaduto o applicazione non autorizzata", s);
    if (s === 429)
      throw new SourceError("rate_limited", "troppe richieste: limite del fornitore o della banca", s);
    if (s >= 500)
      throw new SourceError("temporary", "servizio del fornitore o della banca non disponibile", s);
    if (s < 200 || s >= 300) {
      // solo il CODICE simbolico dell'errore (es. WRONG_TRANSACTIONS_PERIOD), mai il messaggio
      let code: unknown = null;
      try {
        const e = await res.json();
        code = isObj(e) ? (e.error ?? e.code) : null;
      } catch {
        code = null;
      }
      if (code === "WRONG_TRANSACTIONS_PERIOD")
        throw new SourceError("period_unavailable", "periodo non disponibile presso la banca", s);
      throw new SourceError("invalid_request", `richiesta rifiutata (HTTP ${s})`, s);
    }
    let data: unknown;
    try {
      data = await res.json();
    } catch {
      throw new SourceError("invalid_response", "risposta non JSON", s);
    }
    if (!isObj(data)) throw new SourceError("invalid_response", "risposta: oggetto atteso", s);
    return data;
  }

  /** Avvio del consenso: URL della banca su cui il titolare autorizza l'accesso (in sola lettura). */
  async startAuthorization(input: {
    aspspName: string;
    country: string;
    redirectUrl: string;
    validUntil: string;
    state: string;
  }): Promise<{ url: string }> {
    const r = await this.call("POST", "/auth", {
      access: { valid_until: input.validUntil },
      aspsp: { name: input.aspspName, country: input.country },
      state: input.state,
      redirect_url: input.redirectUrl,
      psu_type: "personal",
    });
    if (typeof r.url !== "string") throw new SourceError("invalid_response", "url di autorizzazione assente");
    return { url: r.url };
  }

  /**
   * Chiusura del consenso: dal `code` ricevuto sul redirect alla sessione e ai conti autorizzati. Dell'IBAN restano
   * solo le ultime 4 cifre (per riconoscere il conto quando lo si associa a un conto Finance).
   */
  async createSession(code: string): Promise<SessionInfo> {
    const r = await this.call("POST", "/sessions", { code });
    if (typeof r.session_id !== "string" || !Array.isArray(r.accounts))
      throw new SourceError("invalid_response", "sessione: session_id e accounts attesi");
    const access = isObj(r.access) ? r.access : {};
    return {
      sessionId: r.session_id,
      validUntil: typeof access.valid_until === "string" ? access.valid_until : null,
      accounts: r.accounts.filter(isObj).map((a) => {
        const id = isObj(a.account_id) ? a.account_id : {};
        const iban = typeof id.iban === "string" ? id.iban.replace(/\s+/g, "") : null;
        return {
          uid: String(a.uid),
          currency: typeof a.currency === "string" ? a.currency : null,
          ibanLast4: iban && iban.length >= 4 ? iban.slice(-4) : null,
          name: typeof a.name === "string" ? a.name.slice(0, 60) : null,
        };
      }),
    };
  }

  /** Banche raggiungibili per paese (GET /aspsps): nome, paese, durata massima del consenso in giorni. */
  async aspsps(country: string): Promise<{ name: string; country: string; maxConsentDays: number | null }[]> {
    const r = await this.call("GET", `/aspsps?${new URLSearchParams({ country })}`);
    if (!Array.isArray(r.aspsps)) throw new SourceError("invalid_response", "aspsps: lista attesa");
    return r.aspsps.filter(isObj).map((a) => ({
      name: String(a.name),
      country: String(a.country),
      maxConsentDays:
        typeof a.maximum_consent_validity === "number"
          ? Math.floor(a.maximum_consent_validity / 86400)
          : null,
    }));
  }

  async transactions(
    accountRef: string,
    from: string,
    to: string,
    continuationKey: string | null,
    strategy: FetchStrategy = "default",
  ): Promise<TransactionsPage> {
    const q = new URLSearchParams({ date_from: from, date_to: to });
    if (strategy !== "default") q.set("strategy", strategy);
    if (continuationKey !== null) q.set("continuation_key", continuationKey);
    const r = await this.call("GET", `/accounts/${encodeURIComponent(accountRef)}/transactions?${q}`);
    if (!Array.isArray(r.transactions))
      throw new SourceError("invalid_response", "transactions: lista attesa");
    const next =
      typeof r.continuation_key === "string" && r.continuation_key !== "" ? r.continuation_key : null;
    return { transactions: r.transactions, continuationKey: next };
  }

  async balances(accountRef: string): Promise<readonly unknown[]> {
    const r = await this.call("GET", `/accounts/${encodeURIComponent(accountRef)}/balances`);
    if (!Array.isArray(r.balances)) throw new SourceError("invalid_response", "balances: lista attesa");
    return r.balances;
  }
}
