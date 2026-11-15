/**
 * Verifica del token di Cloudflare Access (src/access.ts) in Node, con chiavi di prova: accesso consentito, ogni
 * motivo di rifiuto, configurazione mancante o non valida, cache delle chiavi del team.
 */
import assert from "node:assert/strict";
import { before, describe, it } from "node:test";
import {
  type AccessConfig,
  type KeySource,
  parseAccessConfig,
  teamKeySource,
  verifyAccessJwt,
} from "../src/access.ts";
import { AUD, EMAIL, nowS, TEAM, type TestKey, testKey, token } from "./helpers.ts";

const CONFIG: AccessConfig = { teamDomain: TEAM, aud: AUD, allowedEmails: [EMAIL] };
let key: TestKey;
let other: TestKey;
let keys: KeySource;

before(async () => {
  key = await testKey("chiave-1");
  other = await testKey("chiave-2");
  keys = async (kid) => (kid === key.kid ? key.jwk : null);
});

const verify = (t: string | null, cfg: AccessConfig = CONFIG, src: KeySource = keys) =>
  verifyAccessJwt(t, cfg, src, nowS());

describe("token di Cloudflare Access", () => {
  it("consentito: firma, emittente, audience, scadenza ed email corretti", async () => {
    assert.deepEqual(await verify(await token(key)), { ok: true, email: EMAIL });
    // aud anche come stringa singola; email senza distinzione di maiuscole
    assert.deepEqual(await verify(await token(key, { aud: AUD, email: EMAIL.toUpperCase() })), {
      ok: true,
      email: EMAIL,
    });
  });

  it("negato: token assente o malformato", async () => {
    for (const t of [null, "", "abc", "a.b", "a.b.c.d", "!!.??.**", "x".repeat(9000)])
      assert.equal((await verify(t)).ok, false, String(t).slice(0, 20));
    assert.deepEqual(await verify(null), { ok: false, reason: "token_assente" });
    assert.deepEqual(await verify("a.b.c"), { ok: false, reason: "token_malformato" });
  });

  it("negato: algoritmo diverso da RS256 (none, HS256) e chiave sconosciuta", async () => {
    const good = await token(key);
    const [, p, s] = good.split(".");
    const none = `${Buffer.from(JSON.stringify({ alg: "none", kid: key.kid })).toString("base64url")}.${p}.${s}`;
    assert.deepEqual(await verify(none), { ok: false, reason: "algoritmo" });
    assert.deepEqual(await verify(await token(key, {}, { alg: "HS256" })), {
      ok: false,
      reason: "algoritmo",
    });
    assert.deepEqual(await verify(await token(other)), { ok: false, reason: "chiave_sconosciuta" });
    assert.deepEqual(await verify(await token(key, {}, { kid: null })), {
      ok: false,
      reason: "chiave_sconosciuta",
    });
  });

  it("negato: firma di un'altra chiave con lo stesso kid, e contenuto alterato dopo la firma", async () => {
    const forged = await token({ ...other, kid: key.kid });
    assert.deepEqual(await verify(forged), { ok: false, reason: "firma" });
    const [h, , s] = (await token(key)).split(".");
    const p2 = Buffer.from(
      JSON.stringify({
        iss: `https://${TEAM}`,
        aud: [AUD],
        email: "altro@prova.invalid",
        type: "app",
        exp: nowS() + 60,
      }),
    ).toString("base64url");
    assert.deepEqual(await verify(`${h}.${p2}.${s}`), { ok: false, reason: "firma" });
  });

  it("negato: emittente, audience, tipo", async () => {
    assert.deepEqual(await verify(await token(key, { iss: "https://altro.cloudflareaccess.com" })), {
      ok: false,
      reason: "emittente",
    });
    assert.deepEqual(await verify(await token(key, { aud: ["altra-applicazione"] })), {
      ok: false,
      reason: "audience",
    });
    assert.deepEqual(await verify(await token(key, { aud: null })), { ok: false, reason: "audience" });
    assert.deepEqual(await verify(await token(key, { type: "org" })), { ok: false, reason: "tipo" });
  });

  it("negato: scaduto, non ancora valido, emesso nel futuro (oltre la tolleranza di 60 s)", async () => {
    const t = nowS();
    assert.deepEqual(await verify(await token(key, { exp: t - 61 })), { ok: false, reason: "scaduto" });
    assert.deepEqual(await verify(await token(key, { exp: null })), { ok: false, reason: "scaduto" });
    assert.deepEqual(await verify(await token(key, { nbf: t + 120 })), {
      ok: false,
      reason: "non_ancora_valido",
    });
    assert.deepEqual(await verify(await token(key, { iat: t + 120 })), {
      ok: false,
      reason: "emesso_nel_futuro",
    });
    assert.equal((await verify(await token(key, { exp: t - 30 }))).ok, true, "entro la tolleranza");
  });

  it("negato: token di servizio (senza email) ed email non ammessa", async () => {
    assert.deepEqual(await verify(await token(key, { email: null, common_name: "id.access", sub: "" })), {
      ok: false,
      reason: "email_assente",
    });
    assert.deepEqual(await verify(await token(key, { email: "estraneo@prova.invalid" })), {
      ok: false,
      reason: "email_non_ammessa",
    });
  });

  it("una fonte di chiavi che fallisce equivale a chiave sconosciuta (mai un'eccezione)", async () => {
    const broken: KeySource = async () => {
      throw new Error("rete");
    };
    assert.deepEqual(await verify(await token(key), CONFIG, broken), {
      ok: false,
      reason: "chiave_sconosciuta",
    });
  });
});

describe("configurazione dell'accesso", () => {
  const full = {
    ACCESS_TEAM_DOMAIN: TEAM,
    ACCESS_AUD: AUD,
    ACCESS_ALLOWED_EMAILS: ` ${EMAIL.toUpperCase()} , secondo@prova.invalid`,
  };

  it("completa: normalizzata", () => {
    assert.deepEqual(parseAccessConfig(full), {
      config: { teamDomain: TEAM, aud: AUD, allowedEmails: [EMAIL, "secondo@prova.invalid"] },
      problems: [],
    });
  });

  it("mancante o non valida: nessuna configurazione, con i problemi elencati", () => {
    for (const env of [
      {},
      { ...full, ACCESS_TEAM_DOMAIN: "" },
      { ...full, ACCESS_TEAM_DOMAIN: "esempio.com" },
      { ...full, ACCESS_TEAM_DOMAIN: "https://prova.cloudflareaccess.com" },
      { ...full, ACCESS_AUD: "" },
      { ...full, ACCESS_AUD: "corto" },
      { ...full, ACCESS_ALLOWED_EMAILS: "" },
      { ...full, ACCESS_ALLOWED_EMAILS: "non-una-email" },
      {
        ...full,
        ACCESS_ALLOWED_EMAILS:
          "a@uno.invalid,b@uno.invalid,c@uno.invalid,d@uno.invalid,e@uno.invalid,f@uno.invalid",
      },
    ]) {
      const r = parseAccessConfig(env);
      assert.equal(r.config, null, JSON.stringify(env));
      assert.ok(r.problems.length > 0);
    }
    assert.equal(parseAccessConfig({}).problems.length, 3);
  });
});

describe("chiavi pubbliche del team", () => {
  it("lette dall'indirizzo documentato, tenute in cache, rilette al massimo una volta al minuto per un kid nuovo", async () => {
    let t = 1_000_000;
    const urls: string[] = [];
    const src = teamKeySource(
      TEAM,
      async (url) => {
        urls.push(url);
        return Response.json({
          keys: [key.jwk, { kid: "rotta", kty: "EC" }],
          public_cert: {},
          public_certs: [],
        });
      },
      () => t,
    );
    assert.deepEqual(await src(key.kid), key.jwk);
    assert.deepEqual(await src(key.kid), key.jwk);
    assert.equal(await src("nuovo"), null);
    assert.equal(urls.length, 1, "kid sconosciuto subito dopo la lettura: nessuna rilettura");
    t += 61_000;
    assert.equal(await src("nuovo"), null);
    assert.equal(urls.length, 2);
    assert.equal(await src("rotta"), null, "solo chiavi RSA");
    assert.deepEqual(urls, [`https://${TEAM}/cdn-cgi/access/certs`, `https://${TEAM}/cdn-cgi/access/certs`]);
  });

  it("risposta in errore: eccezione verso la verifica, che la tratta come chiave sconosciuta", async () => {
    const src = teamKeySource(TEAM, async () => new Response("no", { status: 500 }));
    assert.deepEqual(await verify(await token(key), CONFIG, src), {
      ok: false,
      reason: "chiave_sconosciuta",
    });
  });
});
