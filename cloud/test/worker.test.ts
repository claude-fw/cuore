/**
 * Vetrina dentro workerd (Miniflare), senza rete: le chiavi del team di Access sono servite da `outboundService`
 * con una chiave di PROVA generata qui. Ingresso di produzione (src/index.ts) e ingresso locale (src/locale.ts).
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { before, describe, it } from "node:test";
import { AUD, bundle, EMAIL, startWorker, TEAM, type TestKey, testKey, token } from "./helpers.ts";

const PROD_URL = "https://cuore.esempio.workers.dev";
const CONFIG = {
  ACCESS_TEAM_DOMAIN: TEAM,
  ACCESS_AUD: AUD,
  ACCESS_ALLOWED_EMAILS: EMAIL,
  CUORE_STATO: "attivo",
};
const PAGE = readFileSync(new URL("../../app/index.html", import.meta.url), "utf8");
const SYNTH = ["conto-demo.csv", "carta-demo.csv"].map(
  (f) =>
    readFileSync(new URL(`../../finance/examples/sintetici/${f}`, import.meta.url), "utf8")
      .trim()
      .split("\n").length - 1,
);

let key: TestKey;
let prodScript = "";
let localScript = "";
const outbound: string[] = [];
const certs = (request: Request) => {
  outbound.push(request.url);
  return request.url === `https://${TEAM}/cdn-cgi/access/certs`
    ? Response.json({ keys: [key.jwk] })
    : new Response("rete non disponibile nei test", { status: 599 });
};

before(async () => {
  key = await testKey("chiave-team");
  prodScript = await bundle("index");
  localScript = await bundle("locale");
});

const get = (
  mf: { dispatchFetch(u: string, i?: RequestInit): Promise<Response> },
  path: string,
  headers: Record<string, string> = {},
  method = "GET",
) => mf.dispatchFetch(`${PROD_URL}${path}`, { method, headers, redirect: "manual" });

describe("ingresso di produzione", () => {
  it("il pacchetto non contiene l'accesso simulato ne' moduli node:", () => {
    for (const s of [
      "__locale",
      "accesso-simulato",
      "cuore_accesso_simulato",
      "ACCESSO SIMULATO",
      "vetrina.invalid",
    ])
      assert.ok(!prodScript.includes(s), s);
    assert.ok(!/from\s*["']node:/.test(prodScript));
    assert.ok(
      localScript.includes("__locale"),
      "controllo del controllo: l'ingresso locale invece lo contiene",
    );
  });

  it("configurazione mancante: tutto rifiutato con 503, nessuna pagina, anche con un token valido", async () => {
    const mf = await startWorker(
      prodScript,
      { ACCESS_TEAM_DOMAIN: "", ACCESS_AUD: "", CUORE_STATO: "attivo" },
      certs,
    );
    const t = await token(key);
    for (const path of ["/", "/api/vetrina/stato", "/qualsiasi"]) {
      const r = await get(mf, path, { "Cf-Access-Jwt-Assertion": t });
      const body = await r.text();
      assert.equal(r.status, 503, path);
      assert.match(body, /configurazione_mancante/);
      assert.ok(!body.includes("<script"), "nessuna pagina servita");
    }
  });

  it("accesso negato senza token, con email non ammessa, audience o emittente sbagliati, token scaduto", async () => {
    const mf = await startWorker(prodScript, CONFIG, certs);
    const cases: [string, Record<string, string>][] = [
      ["token_assente", {}],
      [
        "email_non_ammessa",
        { "Cf-Access-Jwt-Assertion": await token(key, { email: "estraneo@prova.invalid" }) },
      ],
      ["audience", { "Cf-Access-Jwt-Assertion": await token(key, { aud: ["altra"] }) }],
      [
        "emittente",
        { "Cf-Access-Jwt-Assertion": await token(key, { iss: "https://altro.cloudflareaccess.com" }) },
      ],
      [
        "scaduto",
        { "Cf-Access-Jwt-Assertion": await token(key, { exp: Math.floor(Date.now() / 1000) - 3600 }) },
      ],
      [
        "email_assente",
        { "Cf-Access-Jwt-Assertion": await token(key, { email: null, common_name: "servizio.access" }) },
      ],
    ];
    for (const [reason, headers] of cases) {
      for (const path of ["/", "/api/vetrina/stato"]) {
        const r = await get(mf, path, headers);
        const body = await r.text();
        assert.equal(r.status, 403, `${reason} ${path}`);
        assert.ok(body.includes(reason), `${reason} ${path}: ${body}`);
        assert.ok(!body.includes("<script"));
        assert.ok(!body.includes("estraneo@"), "l'email ricevuta non viene ripetuta");
      }
    }
    // il cookie da solo non basta: conta l'intestazione aggiunta da Access
    assert.equal((await get(mf, "/", { Cookie: `CF_Authorization=${await token(key)}` })).status, 403);
  });

  it("accesso consentito: pagina in modalita' vetrina con le intestazioni di sicurezza; stato del Durable Object sintetico", async () => {
    outbound.length = 0;
    const mf = await startWorker(prodScript, CONFIG, certs);
    const t = await token(key);
    const r = await get(mf, "/", { "Cf-Access-Jwt-Assertion": t });
    const html = await r.text();
    assert.equal(r.status, 200);
    assert.match(r.headers.get("Content-Type") ?? "", /text\/html/);
    assert.match(r.headers.get("Content-Security-Policy") ?? "", /frame-ancestors 'none'/);
    assert.equal(r.headers.get("Cache-Control"), "no-store");
    assert.equal(r.headers.get("X-Frame-Options"), "DENY");
    assert.ok(html.includes('<meta name="cuore-vetrina" content="1">'));
    assert.equal(
      html.replace('\n<meta name="cuore-vetrina" content="1">', ""),
      PAGE,
      "pagina identica a app/index.html",
    );
    assert.ok(!html.includes("ACCESSO SIMULATO"));

    const s = await get(mf, "/api/vetrina/stato", { "Cf-Access-Jwt-Assertion": t });
    const d = (await s.json()) as { schema: string; dati: string; finance: Record<string, unknown> };
    assert.equal(s.status, 200);
    assert.equal(d.schema, "cuore.vetrina.stato/1");
    assert.equal(d.dati, "sintetici");
    assert.equal(
      d.finance.movimenti,
      (SYNTH[0] ?? 0) + (SYNTH[1] ?? 0),
      "solo i movimenti degli esempi sintetici",
    );
    assert.equal(d.finance.conti, 3, "conto, carta e contanti sintetici");
    assert.equal(d.finance.versione_schema, 8);
    assert.equal(d.finance.integrita, "ok");
    assert.equal(d.finance.consultazione_ok, true, String(d.finance.operazioni_riuscite));
    // l'unica uscita di rete e' la lettura delle chiavi pubbliche del team, una sola volta (cache)
    assert.deepEqual(outbound, [`https://${TEAM}/cdn-cgi/access/certs`]);
  });

  it("solo GET e HEAD; nessun'altra API; richieste da altri siti rifiutate; interruttore spento", async () => {
    const mf = await startWorker(prodScript, CONFIG, certs);
    const auth = { "Cf-Access-Jwt-Assertion": await token(key) };
    for (const m of ["POST", "PUT", "DELETE", "PATCH"])
      assert.equal((await get(mf, "/", auth, m)).status, 405, m);
    const h = await get(mf, "/", auth, "HEAD");
    assert.equal(h.status, 200);
    assert.equal(await h.text(), "");
    for (const p of [
      "/api/money",
      "/api/school",
      "/api/events",
      "/api/search?q=x",
      "/api/vetrina",
      "/__locale/accedi",
    ])
      assert.equal((await get(mf, p, auth)).status, 404, p);
    assert.equal(
      (await get(mf, "/api/vetrina/stato", { ...auth, "Sec-Fetch-Site": "cross-site" })).status,
      403,
    );
    const off = await startWorker(prodScript, { ...CONFIG, CUORE_STATO: "spento" }, certs);
    assert.equal((await get(off, "/", auth)).status, 503);
    assert.equal((await get(off, "/api/vetrina/stato", auth)).status, 503);
  });

  it("interruttore chiuso per difetto: valore sconosciuto o variabile mancante = 503; ' Attivo ' apre", async () => {
    const auth = { "Cf-Access-Jwt-Assertion": await token(key) };
    const { CUORE_STATO: _, ...senza } = CONFIG;
    for (const [nome, vars] of [
      ["mancante", senza],
      ["vuoto", { ...CONFIG, CUORE_STATO: "" }],
      ["refuso", { ...CONFIG, CUORE_STATO: "spentoo" }],
      ["maiuscole", { ...CONFIG, CUORE_STATO: "SPENTO" }],
    ] as const) {
      const mf = await startWorker(prodScript, vars, certs);
      assert.equal((await get(mf, "/", auth)).status, 503, nome);
    }
    const on = await startWorker(prodScript, { ...CONFIG, CUORE_STATO: " Attivo " }, certs);
    assert.equal((await get(on, "/", auth)).status, 200);
  });

  it("chiavi del team non raggiungibili: accesso negato, mai consentito", async () => {
    const mf = await startWorker(prodScript, CONFIG, () => new Response("errore", { status: 500 }));
    const r = await get(mf, "/", { "Cf-Access-Jwt-Assertion": await token(key) });
    assert.equal(r.status, 403);
    assert.match(await r.text(), /chiave_sconosciuta/);
  });
});

describe("ingresso locale (accesso simulato)", () => {
  const L = "http://localhost:8787";
  const local = (
    mf: { dispatchFetch(u: string, i?: RequestInit): Promise<Response> },
    path: string,
    headers: Record<string, string> = {},
    base = L,
  ) => mf.dispatchFetch(`${base}${path}`, { headers, redirect: "manual" });

  it("rifiuta ogni host diverso da localhost, anche con un token simulato valido", async () => {
    const mf = await startWorker(localScript, { CUORE_STATO: "attivo" }, certs);
    const login = await local(mf, "/__locale/accedi?come=titolare");
    const cookie = (login.headers.get("Set-Cookie") ?? "").split(";")[0] ?? "";
    for (const base of [
      PROD_URL,
      "https://cuore-locale-non-pubblicare.esempio.workers.dev",
      "http://192.168.1.10:8787",
    ]) {
      for (const path of ["/", "/__locale/accedi?come=titolare", "/api/vetrina/stato"]) {
        const r = await local(mf, path, { Cookie: cookie }, base);
        assert.equal(r.status, 403, `${base}${path}`);
      }
    }
  });

  it("senza accesso simulato: rimanda alla pagina dell'accesso simulato; le API rispondono 403", async () => {
    const mf = await startWorker(localScript, { CUORE_STATO: "attivo" }, certs);
    const r = await local(mf, "/");
    assert.equal(r.status, 302);
    assert.equal(r.headers.get("Location"), "/__locale/accedi");
    const p = await local(mf, "/__locale/accedi");
    assert.match(await p.text(), /ACCESSO SIMULATO · SOLO LOCALE/);
    assert.equal((await local(mf, "/api/vetrina/stato")).status, 403);
  });

  it("titolare simulato: pagina con il segnale vetrina e la fascia «accesso simulato»; utente non ammesso: 403", async () => {
    const mf = await startWorker(localScript, { CUORE_STATO: "attivo" }, certs);
    const ok = await local(mf, "/__locale/accedi?come=titolare");
    assert.equal(ok.status, 303);
    const setCookie = ok.headers.get("Set-Cookie") ?? "";
    assert.match(setCookie, /HttpOnly/);
    assert.match(setCookie, /SameSite=Strict/);
    const cookie = setCookie.split(";")[0] ?? "";
    const page = await local(mf, "/", { Cookie: cookie });
    const html = await page.text();
    assert.equal(page.status, 200);
    assert.ok(html.includes('<meta name="cuore-vetrina" content="1">'));
    assert.ok(html.includes("ACCESSO SIMULATO · SOLO LOCALE"));
    const st = await local(mf, "/api/vetrina/stato", { Cookie: cookie });
    assert.equal(((await st.json()) as { dati: string }).dati, "sintetici");

    const no = await local(mf, "/__locale/accedi?come=estraneo");
    const bad = (no.headers.get("Set-Cookie") ?? "").split(";")[0] ?? "";
    const r = await local(mf, "/", { Cookie: bad });
    assert.equal(r.status, 403);
    assert.match(await r.text(), /email_non_ammessa/);
  });

  it("un token portato dal client nell'intestazione di Access viene ignorato; un cookie alterato e' rifiutato", async () => {
    const mf = await startWorker(localScript, { CUORE_STATO: "attivo" }, certs);
    const forged = await token(key, {
      iss: "https://accesso-simulato.invalid",
      aud: ["vetrinalocalesimulata0000000000"],
      email: "titolare@vetrina.invalid",
    });
    assert.equal((await local(mf, "/api/vetrina/stato", { "Cf-Access-Jwt-Assertion": forged })).status, 403);
    assert.equal(
      (await local(mf, "/api/vetrina/stato", { Cookie: `cuore_accesso_simulato=${forged}` })).status,
      403,
      "firmato con una chiave diversa",
    );
    const ok = await local(mf, "/__locale/accedi?come=titolare");
    const c = (ok.headers.get("Set-Cookie") ?? "").split(";")[0] ?? "";
    const tampered = `${c.slice(0, -3)}${c.endsWith("AAA") ? "BBB" : "AAA"}`;
    assert.equal((await local(mf, "/api/vetrina/stato", { Cookie: tampered })).status, 403);
    // anche un token simulato VALIDO conta solo come cookie dell'accesso simulato, mai come intestazione del client
    const valid = c.slice(c.indexOf("=") + 1);
    assert.equal((await local(mf, "/api/vetrina/stato", { Cookie: c })).status, 200);
    assert.equal((await local(mf, "/api/vetrina/stato", { "Cf-Access-Jwt-Assertion": valid })).status, 403);
  });
});
