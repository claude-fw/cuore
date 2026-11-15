/**
 * Procedura bancaria e collaudo su copia (scripts/bank-sync.ts, src/sync/connections.ts). Database, chiave e fonte
 * SINTETICI in una cartella temporanea; trasporto finto: nessuna rete, nessuna banca, nessun dato reale.
 */
import assert from "node:assert/strict";
import { createHash, webcrypto } from "node:crypto";
import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  statSync,
  writeFileSync,
  writeSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { after, before, describe, it } from "node:test";
import { copyBytes, type Deps, main } from "../scripts/bank-sync.ts";
import { createCtx } from "../src/context.ts";
import { migrate } from "../src/db/migrate.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import { NodeSqliteDb } from "../src/db/node-sqlite.ts";
import { createAccount, createInstitution } from "../src/services/accounts.ts";
import {
  attemptDecision,
  type BankConnection,
  buildStatus,
  CONNECTIONS_SCHEMA,
  consentState,
  markAttempts,
  parseConnections,
  parseStatus,
  type StatusEntry,
} from "../src/sync/connections.ts";
import type { Transport } from "../src/sync/enable-banking.ts";
import type { SyncReport } from "../src/sync/runner.ts";

const NOW = new Date("2026-10-25T10:00:00Z");
const REPO = resolve(import.meta.dirname, "..", "..");
let dir = "";
let db = "";
let conns = "";
let fixture = "";

const tx = (id: string, date: string, amount: string, dir: "CRDT" | "DBIT", status = "BOOK") => ({
  transaction_id: id,
  transaction_amount: { amount, currency: "EUR" },
  credit_debit_indicator: dir,
  status,
  booking_date: status === "BOOK" ? date : null,
  transaction_date: date,
  remittance_information: [`Movimento SINTETICO ${id}`],
});

function run(argv: string[], transport?: Transport, now = NOW, extra: Partial<Deps> = {}) {
  const out: Record<string, unknown>[] = [];
  return main(argv, {
    out: (o) => out.push(o as Record<string, unknown>),
    now: () => now,
    ...(transport ? { transport } : {}),
    ...extra,
  }).then((code) => ({ code, out: out[0] ?? {} }));
}

const sha = (p: string) => createHash("sha256").update(readFileSync(p)).digest("hex");

/** Valori sintetici che non devono mai uscire (importi, saldi, descrizioni, riferimenti del fornitore, sessioni, chiave). */
const SENSITIVE = [
  "4812.37",
  "481237",
  "638.19",
  "63819",
  "9734.58",
  "973458",
  "SINTETICO",
  "u-rev",
  "u-mps",
  "sess-SINTETICA",
  "chiave",
  "BEGIN PRIVATE",
];
/**
 * Dati sensibili nel testo. I valori sintetici sono lunghi e univoci (prima «250» e «s1» comparivano a volte in un nome
 * di cartella casuale, nei millisecondi o in un'impronta e il test falliva senza alcun dato sensibile); per sicurezza si
 * tolgono comunque cartelle temporanee, istanti e impronte esadecimali, che variano fra un'esecuzione e l'altra.
 */
function sensitiveIn(text: string, ...dirs: string[]): string[] {
  let t = text;
  for (const d of dirs.flatMap((x) => [x, realpathSync(x)])) t = t.split(d).join("<tmp>");
  t = t.replace(/\d{4}-\d\d-\d\dT[0-9:.-]+Z?/g, "<istante>").replace(/\b[0-9a-f]{64}\b/g, "<impronta>");
  return SENSITIVE.filter((s) => t.includes(s));
}

before(async () => {
  dir = mkdtempSync(join(tmpdir(), "cuore-banca-test-"));
  db = join(dir, "finance.sqlite3");
  const conn = new NodeSqliteDb(db);
  const ctx = createCtx(conn);
  await migrate(conn, MIGRATIONS, ctx.now);
  await createInstitution(ctx, "rev", "Revolut DEMO");
  await createInstitution(ctx, "mps", "MPS DEMO");
  await createAccount(ctx, {
    id: "rev-eur",
    name: "Rev DEMO",
    type: "current",
    currency: "EUR",
    holderRole: "owner",
    institutionId: "rev",
  });
  await createAccount(ctx, {
    id: "mps-eur",
    name: "MPS DEMO",
    type: "current",
    currency: "EUR",
    holderRole: "owner",
    institutionId: "mps",
  });
  conn.close();
  const pair = await webcrypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"],
  );
  const der = Buffer.from(await webcrypto.subtle.exportKey("pkcs8", pair.privateKey)).toString("base64");
  writeFileSync(join(dir, "chiave.pem"), `-----BEGIN PRIVATE KEY-----\n${der}\n-----END PRIVATE KEY-----\n`, {
    mode: 0o600,
  });
  conns = join(dir, "collegamenti.json");
  writeFileSync(
    conns,
    JSON.stringify({
      schema: CONNECTIONS_SCHEMA,
      appId: "app-SINTETICA",
      keyFile: join(dir, "chiave.pem"),
      redirectUrl: "https://localhost/ritorno",
      connections: [
        {
          accountId: "rev-eur",
          provider: "enable-banking",
          accountRef: "u-rev",
          aspsp: { name: "Revolut", country: "LT" },
          sessionId: "sess-SINTETICA-rev",
          validUntil: "2027-04-20T00:00:00+02:00",
        },
        {
          accountId: "mps-eur",
          provider: "enable-banking",
          accountRef: "u-mps",
          aspsp: { name: "Banca DEMO", country: "IT" },
          sessionId: "sess-SINTETICA-mps",
          validUntil: "2026-11-01T00:00:00+01:00",
        },
      ],
    }),
  );
  fixture = join(dir, "fonte.json");
  writeFileSync(
    fixture,
    JSON.stringify({
      "u-rev": {
        pages: [
          {
            transactions: [
              tx("r1", "2026-10-10", "4812.37", "DBIT"),
              tx("r2", "2026-10-20", "638.19", "DBIT", "PDNG"),
            ],
            continuationKey: "1",
          },
          { transactions: [], continuationKey: null },
        ],
        balances: [
          {
            balance_type: "CLBD",
            reference_date: "2026-10-24",
            balance_amount: { amount: "9734.58", currency: "EUR" },
          },
        ],
      },
      "u-mps": {
        pages: [{ transactions: [tx("m1", "2026-10-11", "4812.37", "CRDT")], continuationKey: null }],
        balances: [],
      },
    }),
  );
});

after(() => rmSync(dir, { recursive: true, force: true }));

describe("procedura bancaria: collaudo su copia", () => {
  it("sincronizza una COPIA: originale identico, solo aggiunte, trasferimento proposto, stato senza importi", async () => {
    const shaBefore = sha(db);
    const filesBefore = readdirSync(dir).sort();
    const status = join(dir, "stato-banca.json");
    const { code, out } = await run([
      "prova-su-copia",
      "--db",
      db,
      "--collegamenti",
      conns,
      "--fonte-finta",
      fixture,
      "--oggi",
      "2026-10-25",
      "--stato",
      status,
    ]);
    assert.equal(code, 0, JSON.stringify(out));
    assert.equal(out.esito, "COLLAUDO SU COPIA RIUSCITO");
    assert.equal(out.database_originale, "invariato (byte e stato del file)");
    assert.equal(out.copia, "eliminata");
    const conti = out.conti as {
      conto: string;
      esito: string;
      strategia: string;
      importazione: { nuovi: number; saldi: number };
    }[];
    assert.deepEqual(
      conti.map((c) => [c.conto, c.esito, c.strategia, c.importazione.nuovi, c.importazione.saldi]),
      [
        ["rev-eur", "importata", "longest", 2, 1],
        ["mps-eur", "importata", "longest", 1, 0],
      ],
    );
    assert.equal((out.delta_copia as Record<string, number>).transaction_links, 1); // trasferimento: solo proposto
    assert.equal(sha(db), shaBefore);
    assert.deepEqual(readdirSync(dir).sort(), [...filesBefore, "stato-banca.json"].sort()); // nessun -wal/-shm accanto
    assert.equal(statSync(status).mode & 0o777, 0o600);
    const st = JSON.parse(readFileSync(status, "utf8"));
    assert.deepEqual(
      [st.schema, st.database, st.connessioni.map((c: { consenso: { stato: string } }) => c.consenso.stato)],
      ["cuore.bank.sync_status/1", "copia", ["valido", "in_scadenza"]],
    );
    assert.deepEqual(sensitiveIn(JSON.stringify(st) + JSON.stringify(out), dir), []);
    const again = await run([
      "prova-su-copia",
      "--db",
      db,
      "--collegamenti",
      conns,
      "--fonte-finta",
      fixture,
      "--oggi",
      "2026-10-25",
      "--stato",
      status,
    ]);
    assert.equal(again.code, 0); // la copia riparte dall'originale
    assert.ok(JSON.parse(readFileSync(status, "utf8")).connessioni[0].ultima_riuscita);
  });

  it("senza fonte finta serve --conferma-rete; nessuna chiamata parte senza", async () => {
    let calls = 0;
    const t: Transport = async () => {
      calls += 1;
      return { status: 200, json: async () => ({}) };
    };
    for (const argv of [
      ["prova-su-copia", "--db", db, "--collegamenti", conns],
      ["banche", "--collegamenti", conns, "--paese", "IT"],
      ["consenso", "--collegamenti", conns, "--banca", "Revolut", "--paese", "LT"],
      ["sessione", "--collegamenti", conns, "--codice", "c"],
    ]) {
      const { code, out } = await run(argv, t);
      assert.equal(code, 64, argv[0]);
      assert.match(JSON.stringify(out.problemi), /conferma-rete/);
    }
    assert.equal(calls, 0);
  });

  it("scrittura in corso sull'originale: bloccato prima di copiare", async () => {
    writeFileSync(`${db}-wal`, "x");
    try {
      const { code, out } = await run([
        "prova-su-copia",
        "--db",
        db,
        "--collegamenti",
        conns,
        "--fonte-finta",
        fixture,
      ]);
      assert.deepEqual([code, out.esito], [1, "BLOCCATO prima di copiare"]);
      assert.deepEqual(copyBytes(db, join(dir, "x.sqlite3")), [
        "-wal non vuoto: scrittura in corso o non consolidata",
      ]);
    } finally {
      rmSync(`${db}-wal`);
    }
    assert.ok(!existsSync(join(dir, "x.sqlite3")));
  });

  it("collegamenti non sicuri o incoerenti rifiutati; conto Finance inesistente bloccato", async () => {
    const bad = (o: Record<string, unknown>) =>
      parseConnections(
        JSON.stringify({
          schema: CONNECTIONS_SCHEMA,
          appId: "a",
          keyFile: join(dir, "k.pem"),
          redirectUrl: "https://x",
          connections: [],
          ...o,
        }),
        REPO,
      ).problems;
    assert.match(bad({ keyFile: join(REPO, "finance", "chiave.pem") }).join(), /dentro il repository/);
    assert.match(bad({ keyFile: "chiave.pem" }).join(), /non assoluto/);
    assert.match(bad({ keyFile: "/Volumes/esempio/Library/Mobile Documents/k.pem" }).join(), /sincronizzata/);
    assert.match(bad({ redirectUrl: "http://x" }).join(), /https/);
    assert.match(
      bad({
        connections: [
          {
            accountId: "a",
            provider: "enable-banking",
            accountRef: "u",
            aspsp: { name: "B", country: "IT" },
            sessionId: null,
            validUntil: "domani",
          },
        ],
      }).join(),
      /RFC 3339/,
    );
    const inRepo = await run(["stato", "--collegamenti", join(REPO, "collegamenti.json")]);
    assert.equal(inRepo.code, 64);
    const ghost = join(dir, "fantasma.json");
    const c = JSON.parse(readFileSync(conns, "utf8"));
    c.connections[0].accountId = "conto-inesistente";
    writeFileSync(ghost, JSON.stringify(c));
    const r = await run(["prova-su-copia", "--db", db, "--collegamenti", ghost, "--fonte-finta", fixture]);
    assert.equal(r.code, 1);
    assert.match(JSON.stringify(r.out.problemi), /conto Finance inesistente: conto-inesistente/);
  });

  it("stato dei consensi e comandi di rete con trasporto finto", async () => {
    assert.deepEqual(consentState("2026-10-20T00:00:00Z", NOW), {
      state: "scaduto",
      daysLeft: 0,
      expiresOn: "2026-10-20",
    });
    assert.deepEqual(consentState(null, NOW).state, "assente");
    const s = await run(["stato", "--collegamenti", conns]);
    assert.deepEqual(
      (s.out.collegamenti as { consenso: { state: string; daysLeft: number } }[]).map((c) => [
        c.consenso.state,
        c.consenso.daysLeft,
      ]),
      [
        ["valido", 176],
        ["in_scadenza", 6],
      ],
    );
    const seen: { url: string; body?: string }[] = [];
    const t: Transport = async (url, init) => {
      seen.push({ url, body: init.body });
      if (url.includes("/aspsps"))
        return {
          status: 200,
          json: async () => ({
            aspsps: [
              { name: "Revolut", country: "LT", maximum_consent_validity: 15552000 },
              { name: "Altra Banca", country: "LT" },
            ],
          }),
        };
      return { status: 200, json: async () => ({ url: "https://banca.example/consenso" }) };
    };
    const b = await run(["banche", "--collegamenti", conns, "--paese", "LT", "--conferma-rete"], t);
    assert.deepEqual(b.out, {
      paese: "LT",
      banche: 2,
      rilevanti: [{ name: "Revolut", country: "LT", maxConsentDays: 180 }],
    });
    assert.equal(
      (
        await run(
          [
            "consenso",
            "--collegamenti",
            conns,
            "--banca",
            "Revolut",
            "--paese",
            "LT",
            "--giorni",
            "200",
            "--conferma-rete",
          ],
          t,
        )
      ).code,
      64,
    );
    const c = await run(
      ["consenso", "--collegamenti", conns, "--banca", "Revolut", "--paese", "LT", "--conferma-rete"],
      t,
    );
    assert.equal(c.out.apri_nel_browser, "https://banca.example/consenso");
    assert.deepEqual(JSON.parse(seen.at(-1)?.body ?? "{}").aspsp, { name: "Revolut", country: "LT" });
  });
});

describe("procedura bancaria: sincronizzazione del database (sintetico)", () => {
  // layout protetto come quello reale: <base 0700>/dati 0700/finance.sqlite3 0600; backup e rapporti in <base>
  let base = "";
  let target = "";
  let status = "";
  const hours = (h: number) => new Date(NOW.getTime() + h * 3_600_000);
  const sync = (
    extra: string[] = [],
    now = NOW,
    transport?: Transport,
    st = status,
    more: Partial<Deps> = {},
  ) =>
    run(
      [
        "sincronizza",
        "--db",
        target,
        "--collegamenti",
        conns,
        "--stato",
        st,
        "--oggi",
        "2026-10-25",
        ...extra,
      ],
      transport,
      now,
      more,
    );

  before(() => {
    base = mkdtempSync(join(tmpdir(), "cuore-banca-reale-"));
    mkdirSync(join(base, "dati"), { mode: 0o700 });
    target = join(base, "dati", "finance.sqlite3");
    copyFileSync(db, target);
    chmodSync(target, 0o600);
    status = join(base, "banca-stato.json");
  });
  before(async () => {
    // il database reale nasce da `import-plan.ts --commit` in modalita' DELETE: la fixture sintetica nasce in WAL
    const r = await sync(["--applica", "--fonte-finta", fixture]);
    assert.match(JSON.stringify(r.out.problemi), /modalita' WAL/);
    const c = new NodeSqliteDb(target);
    await c.all("PRAGMA journal_mode = DELETE");
    c.close();
    assert.ok(!existsSync(`${target}-wal`) && !existsSync(`${target}-shm`));
  });
  after(() => rmSync(base, { recursive: true, force: true }));

  it("senza --applica o senza --stato nulla parte e il database resta identico", async () => {
    const before = sha(target);
    let calls = 0;
    const t: Transport = async () => {
      calls += 1;
      return { status: 200, json: async () => ({}) };
    };
    const a = await sync(["--conferma-rete"], NOW, t);
    assert.equal(a.code, 64);
    assert.match(JSON.stringify(a.out.problemi), /--applica/);
    const b = await run(
      ["sincronizza", "--db", target, "--collegamenti", conns, "--applica", "--conferma-rete"],
      t,
    );
    assert.equal(b.code, 64);
    assert.match(JSON.stringify(b.out.problemi), /--stato obbligatorio/);
    const c = await sync(["--applica"], NOW, t);
    assert.match(JSON.stringify(c.out.problemi), /conferma-rete/);
    assert.equal(calls, 0);
    assert.equal(sha(target), before);
    assert.ok(!existsSync(status));
  });

  it("database con scrittura in corso o permessi aperti: bloccato prima di leggere", async () => {
    writeFileSync(`${target}-wal`, "x");
    try {
      const r = await sync(["--applica", "--fonte-finta", fixture]);
      assert.deepEqual([r.code, r.out.esito], [1, "BLOCCATO prima di leggere"]);
      assert.match(JSON.stringify(r.out.problemi), /finance\.sqlite3-wal/);
    } finally {
      rmSync(`${target}-wal`);
    }
    chmodSync(target, 0o644);
    try {
      const r = await sync(["--applica", "--fonte-finta", fixture]);
      assert.match(JSON.stringify(r.out.problemi), /permessi aperti/);
    } finally {
      chmodSync(target, 0o600);
    }
    assert.ok(!existsSync(join(base, "backup")));
  });

  it("aggiorna il database: backup verificato, solo aggiunte, sostituzione atomica, stato «reale» senza importi", async () => {
    const before = sha(target);
    const { code, out } = await sync(["--applica", "--fonte-finta", fixture, "--primo-avvio"]);
    assert.equal(code, 0, JSON.stringify(out));
    assert.equal(out.esito, "AGGIORNATO");
    assert.equal(out.database, "aggiornato (sostituzione atomica)");
    assert.notEqual(sha(target), before);
    assert.equal(statSync(target).mode & 0o777, 0o600);
    assert.deepEqual(readdirSync(join(base, "dati")), ["finance.sqlite3"]); // niente .in-corso, -wal, -shm
    assert.equal((out.delta as Record<string, number>).transactions, 3);
    assert.equal((out.delta as Record<string, number>).transaction_links, 1); // trasferimento: solo proposto
    const backup = String(out.backupPrima);
    assert.ok(backup.startsWith(join(base, "backup")));
    assert.equal(statSync(backup).mode & 0o777, 0o600);
    const conn = new NodeSqliteDb(backup, { readOnly: true });
    try {
      assert.equal((await conn.first<{ n: number }>("SELECT COUNT(*) AS n FROM transactions"))?.n, 0);
    } finally {
      conn.close();
    }
    const real = new NodeSqliteDb(target, { readOnly: true });
    try {
      const links = await real.all<{ status: string }>("SELECT status FROM transaction_links");
      assert.deepEqual(
        links.map((l) => l.status),
        ["proposed"],
      );
    } finally {
      real.close();
    }
    assert.equal(statSync(String(out.rapporto)).mode & 0o777, 0o600);
    const st = JSON.parse(readFileSync(status, "utf8"));
    assert.equal(st.database, "reale");
    assert.equal(st.collaudo, true); // fonte finta: stato di collaudo, mai usato per letture reali
    assert.deepEqual(
      readdirSync(base).filter((f) => f.startsWith("banca-stato")),
      ["banca-stato.json"], // niente .lock o .in-corso rimasti
    );
    assert.deepEqual(
      st.connessioni.map((c: StatusEntry) => [c.conto, c.ultimo_tentativo?.esito, c.ultimo_tentativo?.nuovi]),
      [
        ["rev-eur", "importata", 2],
        ["mps-eur", "importata", 1],
      ],
    );
    const text = JSON.stringify(st) + JSON.stringify(out) + readFileSync(String(out.rapporto), "utf8");
    assert.deepEqual(sensitiveIn(text, base, dir), []);
  });

  it("limiti della banca: nessuna seconda lettura entro 6 ore; dopo, la sovrapposizione non duplica nulla", async () => {
    const before = sha(target);
    const soon = await sync(["--applica", "--fonte-finta", fixture], hours(2));
    assert.deepEqual(
      [soon.code, soon.out.esito, soon.out.database],
      [0, "NESSUN CONTO DA SINCRONIZZARE", "invariato"],
    );
    assert.deepEqual(
      (soon.out.saltati as { conto: string; motivo: string; non_prima_di: string }[]).map((s) => [
        s.conto,
        s.motivo,
        s.non_prima_di,
      ]),
      [
        ["rev-eur", "intervallo_minimo", "2026-10-25T16:00:00.000Z"],
        ["mps-eur", "intervallo_minimo", "2026-10-25T16:00:00.000Z"],
      ],
    );
    assert.equal(sha(target), before);
    const later = await sync(["--applica", "--fonte-finta", fixture], hours(7));
    assert.equal(later.code, 0, JSON.stringify(later.out));
    assert.equal(later.out.esito, "AGGIORNATO (nessun dato nuovo)");
    assert.equal((later.out.delta as Record<string, number>).transactions, 0);
    assert.equal((later.out.delta as Record<string, number>).transaction_links, 0);
  });

  it("limite del fornitore (429): niente scritture, attesa di 6 ore registrata e rispettata", async () => {
    const before = sha(target);
    let calls = 0;
    const t: Transport = async () => {
      calls += 1;
      return { status: 429, json: async () => ({ error: "ASPSP_RATE_LIMIT_EXCEEDED" }) };
    };
    // letture reali: stato proprio, separato da quello di collaudo dei test precedenti
    const real = join(base, "banca-stato-reale.json");
    const r = await sync(["--applica", "--conferma-rete", "--primo-avvio"], hours(14), t, real);
    assert.equal(r.code, 1);
    assert.equal(r.out.esito, "AGGIORNATO IN PARTE (conti non sincronizzati)");
    assert.equal(r.out.database, "invariato");
    assert.equal(sha(target), before);
    assert.deepEqual(readdirSync(join(base, "dati")), ["finance.sqlite3"]);
    const st = JSON.parse(readFileSync(real, "utf8"));
    assert.equal(st.collaudo, undefined);
    assert.deepEqual(
      st.connessioni.map((c: StatusEntry) => [
        c.ultimo_tentativo?.esito,
        c.ultimo_tentativo?.errore,
        c.ultimo_tentativo?.riprova_dopo_ore,
      ]),
      [
        ["interrotta", "rate_limited", 6],
        ["interrotta", "rate_limited", 6],
      ],
    );
    assert.ok(st.connessioni.every((c: StatusEntry) => c.ultima_riuscita === null));
    const n = calls;
    const wait = await sync(["--applica", "--conferma-rete"], hours(17), t, real);
    assert.equal(wait.out.esito, "NESSUN CONTO DA SINCRONIZZARE");
    assert.ok((wait.out.saltati as { motivo: string }[]).every((s) => s.motivo === "attesa_dopo_errore"));
    assert.equal(calls, n); // nessuna chiamata durante l'attesa
  });

  describe("stato dei tentativi: nessun contatto con la banca se non e' affidabile", () => {
    const T = hours(30);
    let sdir = "";
    let calls = 0;
    const t: Transport = async () => {
      calls += 1;
      return { status: 429, json: async () => ({ error: "ASPSP_RATE_LIMIT_EXCEEDED" }) };
    };
    const cfg = () => {
      const c = parseConnections(readFileSync(conns, "utf8"), REPO).config;
      assert.ok(c);
      return c;
    };
    const valid = () => markAttempts(cfg(), null, [], T, "reale");
    const write = (name: string, body: unknown) => {
      const p = join(sdir, name);
      writeFileSync(p, typeof body === "string" ? body : JSON.stringify(body), { mode: 0o600 });
      return p;
    };
    const net = (st: string, extra: string[] = [], more: Partial<Deps> = {}, now = T) =>
      sync(["--applica", "--conferma-rete", ...extra], now, t, st, more);
    const enospc = (failOn: number[], partial = false) => {
      let n = 0;
      return (fd: number, text: string) => {
        n += 1;
        if (failOn.includes(n)) {
          if (partial) writeSync(fd, text.slice(0, 20)); // interruzione a meta' scrittura
          throw Object.assign(new Error("disco pieno"), { code: "ENOSPC" });
        }
        writeSync(fd, text);
      };
    };

    before(() => {
      sdir = mkdtempSync(join(tmpdir(), "cuore-banca-stato-"));
      chmodSync(sdir, 0o700);
    });
    after(() => {
      chmodSync(sdir, 0o700);
      rmSync(sdir, { recursive: true, force: true });
    });

    it("stato mancante (senza --primo-avvio) o --primo-avvio su uno stato esistente: bloccato, nessuna chiamata", async () => {
      const db0 = sha(target);
      const missing = join(sdir, "mancante.json");
      const a = await net(missing);
      assert.deepEqual([a.code, a.out.esito], [1, "BLOCCATO prima di leggere"]);
      assert.match(JSON.stringify(a.out.problemi), /file inesistente.*--primo-avvio/);
      assert.ok(!existsSync(missing) && !existsSync(`${missing}.lock`));
      const p = write("esistente.json", valid());
      const b = await net(p, ["--primo-avvio"]);
      assert.equal(b.code, 1);
      assert.match(JSON.stringify(b.out.problemi), /lo stato esiste gia'/);
      assert.equal(calls, 0);
      assert.equal(sha(target), db0);
    });

    it("stato malformato o semanticamente non valido: bloccato, file intatto, contenuto mai riportato", async () => {
      const secret = "IT60X0542811101000000123456";
      const ok = valid();
      const entry = ok.connessioni[0];
      assert.ok(entry);
      const attempt = {
        at: hours(29).toISOString(),
        esito: "importata",
        errore: null,
        riprova_dopo_ore: null,
        finestra: { dal: "2026-10-01", al: "2026-10-25" },
        strategia: "default",
        pagine: 1,
        nuovi: 0,
        aggiornati: 0,
        saldi: 0,
      };
      const withAttempt = (patch: Record<string, unknown>) => ({
        ...ok,
        connessioni: [{ ...entry, ultimo_tentativo: { ...attempt, ...patch } }, ...ok.connessioni.slice(1)],
      });
      const cases: [string, unknown, RegExp][] = [
        ["troncato", JSON.stringify(ok).slice(0, 40), /JSON non valido/],
        ["vuoto", "", /JSON non valido/],
        ["schema", { ...ok, schema: `altro ${secret}` }, /schema/],
        ["lista", { ...ok, connessioni: {} }, /lista attesa/],
        ["istante", withAttempt({ at: `ieri ${secret}` }), /RFC 3339/],
        ["futuro", withAttempt({ at: hours(40).toISOString() }), /nel futuro/],
        ["esito", withAttempt({ esito: secret }), /esito sconosciuto/],
        ["attesa", withAttempt({ riprova_dopo_ore: -1 }), /riprova_dopo_ore/],
        ["conteggio", withAttempt({ nuovi: 1.5 }), /nuovi: intero/],
        ["ripetuto", { ...ok, connessioni: [entry, entry] }, /conto ripetuto/],
        ["collaudo", { ...ok, collaudo: "si" }, /collaudo/],
      ];
      for (const [name, body, re] of cases) {
        const p = write(`${name}.json`, body);
        const before = readFileSync(p);
        const r = await net(p);
        assert.deepEqual([r.code, r.out.esito], [1, "BLOCCATO prima di leggere"], name);
        assert.match(JSON.stringify(r.out.problemi), re, name);
        assert.ok(!JSON.stringify(r.out).includes(secret), name);
        assert.deepEqual(readFileSync(p), before, name);
        assert.ok(!existsSync(`${p}.lock`) && !existsSync(`${p}.in-corso`), name);
      }
      assert.equal(calls, 0);
      // anche la consultazione segnala lo stato non valido invece di mostrarlo «vuoto»
      const s = await run(["stato", "--collegamenti", conns, "--stato", join(sdir, "troncato.json")]);
      assert.deepEqual([s.code, s.out.esito], [1, "STATO NON VALIDO"]);
    });

    it("stato non leggibile o cartella non scrivibile: bloccato con il codice di sistema, nessuna chiamata", async () => {
      const p = write("chiuso.json", valid());
      chmodSync(p, 0o000);
      try {
        const r = await net(p);
        assert.equal(r.code, 1);
        assert.match(JSON.stringify(r.out.problemi), /non leggibile \(EACCES\)/);
      } finally {
        chmodSync(p, 0o600);
      }
      const ro = mkdtempSync(join(sdir, "sola-lettura-"));
      const q = join(ro, "stato.json");
      writeFileSync(q, JSON.stringify(valid()), { mode: 0o600 });
      chmodSync(ro, 0o500);
      try {
        const r = await net(q);
        assert.equal(r.code, 1);
        assert.match(JSON.stringify(r.out.problemi), /cartella non scrivibile \(EACCES\)/);
      } finally {
        chmodSync(ro, 0o700);
      }
      assert.equal(calls, 0);
    });

    it("scrittura del tentativo fallita PRIMA delle chiamate: nessun contatto, stato e database intatti", async () => {
      const db0 = sha(target);
      const p = write("prima.json", valid());
      const before = readFileSync(p);
      for (const partial of [false, true]) {
        const r = await net(p, [], { writeStatus: enospc([1], partial) });
        assert.deepEqual([r.code, r.out.esito], [1, "BLOCCATO prima di leggere"]);
        assert.match(
          JSON.stringify(r.out.problemi),
          /stato non scrivibile \(ENOSPC\).*nessun contatto con la banca/,
        );
        assert.deepEqual(readFileSync(p), before); // mai uno stato parziale al posto di quello valido
        assert.ok(!existsSync(`${p}.in-corso`) && !existsSync(`${p}.lock`));
      }
      assert.equal(calls, 0);
      assert.equal(sha(target), db0);
      assert.deepEqual(readdirSync(join(base, "dati")), ["finance.sqlite3"]); // copia di lavoro rimossa
    });

    it("scrittura fallita DOPO le chiamate: resta il tentativo «in_corso» e l'intervallo minimo vale", async () => {
      const p = write("dopo.json", valid());
      const r = await net(p, [], { writeStatus: enospc([2]) });
      assert.equal(r.code, 1);
      assert.match(JSON.stringify(r.out.problemi), /ENOSPC.*in_corso/);
      const n = calls;
      assert.ok(n > 0);
      const st = JSON.parse(readFileSync(p, "utf8"));
      assert.ok(st.connessioni.every((c: StatusEntry) => c.ultimo_tentativo?.esito === "in_corso"));
      assert.deepEqual(parseStatus(readFileSync(p, "utf8"), T).problems, []);
      const again = await net(p, [], {}, hours(32));
      assert.equal(again.out.esito, "NESSUN CONTO DA SINCRONIZZARE");
      assert.ok((again.out.saltati as { motivo: string }[]).every((s) => s.motivo === "intervallo_minimo"));
      assert.equal(calls, n);
    });

    it("concorrenza: .lock o .in-corso presenti bloccano e non vengono rimossi; collaudo e reale non si mescolano", async () => {
      const p = write("conc.json", valid());
      for (const suffix of [".lock", ".in-corso"]) {
        writeFileSync(`${p}${suffix}`, "altra esecuzione\n", { mode: 0o600 });
        try {
          const r = await net(p);
          assert.equal(r.code, 1, suffix);
          assert.match(
            JSON.stringify(r.out.problemi),
            suffix === ".lock" ? /\.lock presente/ : /\.in-corso presente/,
          );
          assert.ok(existsSync(`${p}${suffix}`), suffix); // il file di un'altra esecuzione non si tocca
        } finally {
          rmSync(`${p}${suffix}`);
        }
      }
      const fake = await sync(["--applica", "--fonte-finta", fixture], T, undefined, p);
      assert.match(JSON.stringify(fake.out.problemi), /file di stato separato/);
      const proof = await run(
        ["prova-su-copia", "--db", db, "--collegamenti", conns, "--fonte-finta", fixture, "--stato", p],
        undefined,
        T,
      );
      assert.match(JSON.stringify(proof.out.problemi), /file di stato separato/);
      const testState = write("collaudo.json", { ...valid(), collaudo: true });
      const realOnTest = await net(testState);
      assert.match(JSON.stringify(realOnTest.out.problemi), /stato di collaudo/);
      const noState = await run(
        ["prova-su-copia", "--db", db, "--collegamenti", conns, "--conferma-rete"],
        t,
        T,
      );
      assert.equal(noState.code, 64);
      assert.match(JSON.stringify(noState.out.problemi), /serve --stato/);
      assert.equal(calls > 0, true); // solo le chiamate del test precedente
    });
  });
});

describe("procedura bancaria: regole dei tentativi", () => {
  const conn = (validUntil: string | null): BankConnection => ({
    accountId: "c",
    provider: "enable-banking",
    accountRef: "u",
    aspsp: { name: "B", country: "IT" },
    sessionId: null,
    validUntil,
  });
  const last = (at: string, esito: string, errore: string | null, riprova: number | null): StatusEntry => ({
    conto: "c",
    fornitore: "enable-banking",
    banca: { nome: "B", paese: "IT" },
    consenso: { stato: "valido", giorni_rimasti: 100, scade_il: "2027-01-01" },
    ultimo_tentativo: {
      at,
      esito: esito as "interrotta",
      errore,
      riprova_dopo_ore: riprova,
      finestra: null,
      strategia: null,
      pagine: 0,
      nuovi: 0,
      aggiornati: 0,
      saldi: 0,
    },
    ultima_riuscita: null,
  });
  const valid = conn("2027-01-01T00:00:00Z");

  it("consenso, errori che richiedono il titolare, attesa e intervallo minimo", () => {
    assert.deepEqual(attemptDecision(conn(null), null, NOW), {
      due: false,
      reason: "consenso_da_rinnovare",
      notBefore: null,
    });
    assert.equal(
      (attemptDecision(conn("2026-10-25T09:00:00Z"), null, NOW) as { reason: string }).reason,
      "consenso_da_rinnovare",
    );
    assert.deepEqual(attemptDecision(valid, null, NOW), { due: true });
    const owner = last("2026-10-24T10:00:00Z", "interrotta", "consent_expired", null);
    assert.equal((attemptDecision(valid, owner, NOW) as { reason: string }).reason, "serve_il_titolare");
    assert.deepEqual(attemptDecision(valid, owner, NOW, true), { due: true }); // --riprova dopo il rinnovo
    const rejected = last("2026-10-24T10:00:00Z", "rifiutata", "pacchetto_rifiutato", null);
    assert.equal((attemptDecision(valid, rejected, NOW) as { reason: string }).reason, "serve_il_titolare");
    const temp = last("2026-10-25T09:30:00Z", "interrotta", "temporary", 1);
    assert.deepEqual(attemptDecision(valid, temp, NOW), {
      due: false,
      reason: "attesa_dopo_errore",
      notBefore: "2026-10-25T10:30:00.000Z",
    });
    // dopo l'attesa di un errore temporaneo vale comunque l'intervallo minimo dall'ultima lettura
    assert.equal(
      (attemptDecision(valid, temp, new Date("2026-10-25T11:00:00Z")) as { reason: string }).reason,
      "intervallo_minimo",
    );
    // istante illeggibile o nel futuro: mai «mai tentato» (azzererebbe i limiti)
    for (const at of ["ieri", "2026-10-26T10:00:00Z"])
      assert.deepEqual(attemptDecision(valid, last(at, "importata", null, null), NOW), {
        due: false,
        reason: "stato_non_valido",
        notBefore: null,
      });
    // tentativo rimasto «in_corso» (interruzione dopo le chiamate): conta per l'intervallo minimo
    assert.equal(
      (
        attemptDecision(valid, last("2026-10-25T08:00:00Z", "in_corso", null, null), NOW) as {
          reason: string;
        }
      ).reason,
      "intervallo_minimo",
    );
    const ok = last("2026-10-25T04:00:00Z", "importata", null, null);
    assert.deepEqual(attemptDecision(valid, ok, NOW), { due: true }); // 6 ore esatte
    assert.equal(
      (attemptDecision(valid, ok, new Date("2026-10-25T09:59:59Z")) as { reason: string }).reason,
      "intervallo_minimo",
    );
  });

  it("aggiornamento bloccato: letture registrate come rifiutate, nessun conteggio, ultima riuscita invariata", () => {
    const cfg = parseConnections(readFileSync(conns, "utf8"), REPO).config;
    assert.ok(cfg);
    const report = {
      accountId: "rev-eur",
      provider: "enable-banking",
      status: "importata",
      window: { from: "2026-10-01", to: "2026-10-25" },
      firstSync: false,
      gap: null,
      pages: 1,
      fetched: { transactions: 2, balances: 1 },
      balancesRead: true,
      error: null,
      retryAfterHours: null,
      strategy: "default",
      import: {
        runId: "r",
        status: "committed",
        counts: { created: 2, updated: 0, unchanged: 0, excluded: 0, balancesCreated: 1 },
        issues: 0,
      },
      links: null,
    } as unknown as SyncReport;
    const st = buildStatus(cfg, [report], NOW, "reale", null, false);
    assert.deepEqual(
      st.connessioni[0]?.ultimo_tentativo && {
        esito: st.connessioni[0].ultimo_tentativo.esito,
        errore: st.connessioni[0].ultimo_tentativo.errore,
        nuovi: st.connessioni[0].ultimo_tentativo.nuovi,
      },
      { esito: "rifiutata", errore: "applicazione_bloccata", nuovi: 0 },
    );
    assert.equal(st.connessioni[0]?.ultima_riuscita, null);
    assert.equal(st.connessioni[1]?.ultimo_tentativo, null); // conto non letto: nessun tentativo inventato
  });
});
