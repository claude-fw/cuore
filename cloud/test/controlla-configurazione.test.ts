/**
 * Controllo della configurazione di pubblicazione: sul vero `wrangler.jsonc` (oggi: mancano i passi dell'account) e su
 * copie modificate con valori FITTIZI evidenti (ID e AUD inventati, team `prova-cuore`), mai valori reali.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import {
  classiEsportate,
  controllaConfigurazione,
  type Esito,
  type Ingresso,
  parseJsonc,
  testo,
} from "../scripts/controlla-configurazione.ts";

const leggi = (f: string) => readFileSync(new URL(`../${f}`, import.meta.url), "utf8");
// biome-ignore lint/suspicious/noExplicitAny: configurazione letta da JSONC
type Cfg = Record<string, any>;
const VERA = parseJsonc(leggi("wrangler.jsonc")) as Cfg;
const LOCALE = parseJsonc(leggi("wrangler.locale.jsonc")) as Cfg;
const MISURE = parseJsonc(leggi("wrangler.misure.jsonc")) as Cfg;
const CLASSI = classiEsportate(leggi("src/index.ts"));
const OGGI = "2026-11-04";

const prova = (config: Cfg, extra: Partial<Ingresso> = {}): Esito =>
  controllaConfigurazione({ config, locale: LOCALE, classi: CLASSI, oggi: OGGI, ...extra });
const copia = (): Cfg => structuredClone(VERA);
/** Configurazione completa con valori inventati: ID account fittizio, team e AUD di prova. */
function completa(): Cfg {
  const c = copia();
  c.account_id = "0123456789abcdef0123456789abcdef";
  c.vars.ACCESS_TEAM_DOMAIN = "prova-cuore.cloudflareaccess.com";
  c.vars.ACCESS_AUD = "aud0prova0vetrina0cuore0000000000";
  // base «vetrina»: senza la modalità dati veri accesa il 06/11 sul file vero
  delete c.vars.CUORE_FINANCE_ORIGINE;
  delete c.vars.CUORE_SCRITTURE;
  // e senza le notifiche push accese il 06/11 sera
  delete c.vars.VAPID_PUBLIC_KEY;
  delete c.vars.VAPID_SUBJECT;
  return c;
}

describe("controllo della configurazione (wrangler.jsonc)", () => {
  it("JSONC: commenti fuori dalle stringhe, URL e // dentro le stringhe intatti, virgole finali", () => {
    assert.deepEqual(parseJsonc('// c\n{ "a": "https://x.invalid//y", /* b */ "b": [1, 2,], }'), {
      a: "https://x.invalid//y",
      b: [1, 2],
    });
    assert.deepEqual(parseJsonc('{ "a": "con \\" virgolette // e /* */" }'), {
      a: 'con " virgolette // e /* */',
    });
  });

  it("classi esportate dall'ingresso di produzione", () => {
    assert.deepEqual(CLASSI.sort(), ["FinanceVetrina", "SchoolFeed"]);
  });

  it("modello pubblicato (da compilare): coerente, non pronto, dice cosa manca", () => {
    const e = prova(VERA, {
      segretiCreati: ["ACCESS_ALLOWED_EMAILS", "BACKUP_SIGNING_PRIVATE_JWK", "VAPID_PRIVATE_JWK"],
    });
    assert.deepEqual(e.errori, []);
    assert.equal(e.pronto, false, "senza account, team e AUD non e' pronto");
    assert.ok(e.daFare.length > 0);
    assert.ok(e.aPosto.some((x) => x.includes("Modifiche dal telefono aperte")));
  });

  it("con ID, team, AUD e segreto dichiarato: pronto per la vetrina", () => {
    const e = prova(completa(), { segretiCreati: ["ACCESS_ALLOWED_EMAILS"] });
    assert.deepEqual(e.errori, []);
    assert.equal(e.pronto, true);
    assert.match(testo(e), /Stato: PRONTO/);
    assert.equal(prova(completa()).pronto, false, "senza il segreto non e' pronto");
  });

  it("incoerenze trovate", () => {
    const casi: [(c: Cfg) => void, RegExp, Partial<Ingresso>?][] = [
      [
        (c) => {
          c.migrations = c.migrations.slice(0, 1);
        },
        /v2/,
      ],
      [
        (c) => {
          c.migrations[0].new_classes = c.migrations[0].new_sqlite_classes;
          delete c.migrations[0].new_sqlite_classes;
        },
        /new_classes/,
      ],
      [
        (c) => {
          c.migrations.push({ tag: "v3", deleted_classes: ["SchoolFeed"] });
        },
        /deleted_classes/,
      ],
      [
        (c) => {
          c.durable_objects.bindings[0].class_name = "Altra";
        },
        /FINANCE/,
      ],
      [
        (c) => {
          c.main = "src/locale.ts";
        },
        /main/,
      ],
      [
        (c) => {
          c.vars.ACCESS_ALLOWED_EMAILS = "titolare@prova.invalid";
        },
        /segreto/,
      ],
      [
        (c) => {
          c.vars.VAPID_PRIVATE_JWK = "{}";
        },
        /segreto/,
      ],
      [
        (c) => {
          c.vars.CUORE_SCRITTURE = "attive";
          delete c.vars.CUORE_FINANCE_ORIGINE;
        },
        /SCRITTURE = attive senza CUORE_FINANCE_ORIGINE/,
      ],
      [
        (c) => {
          c.vars.BACKUP_MAC_PUBLIC_JWK = '{"kty":"RSA","n":"x","e":"AQAB","d":"segreto"}';
        },
        /PRIVATA/,
      ],
      [
        (c) => {
          c.vars.BACKUP_RECOVERY_PUBLIC_JWK = "non json";
        },
        /BACKUP_RECOVERY_PUBLIC_JWK non e' una chiave/,
      ],
      [
        (c) => {
          c.queues = { producers: [] };
        },
        /queues/,
      ],
      [
        (c) => {
          c.kv_namespaces = [];
        },
        /kv_namespaces/,
      ],
      [
        (c) => {
          c.routes = ["cuore.esempio.invalid/*"];
        },
        /dominio/,
      ],
      [
        (c) => {
          c.compatibility_date = "2027-01-01";
        },
        /futuro/,
      ],
      [
        (c) => {
          c.account_id = "non-valido";
        },
        /account_id/,
      ],
      [
        (c) => {
          c.vars.ACCESS_TEAM_DOMAIN = "esempio.invalid";
        },
        /ACCESS_TEAM_DOMAIN/,
      ],
      [
        (c) => {
          c.vars.ACCESS_SERVIZIO_MIGRAZIONE = "migrazioneprova0001.access";
          delete c.vars.CUORE_FINANCE_ORIGINE;
          delete c.vars.CUORE_SCRITTURE;
        },
        /CUORE_FINANCE_ORIGINE/,
      ],
      [
        (c) => {
          c.triggers = { crons: ["ogni ora"] };
        },
        /Cron/,
      ],
      [
        (c) => {
          c.observability = { enabled: true };
        },
        /observability/,
      ],
      [() => {}, /non esportata/, { classi: ["FinanceVetrina"] }],
      [() => {}, /wrangler.locale.jsonc/, { locale: { ...LOCALE, migrations: [] } }],
    ];
    for (const [modifica, atteso, extra] of casi) {
      const c = copia();
      modifica(c);
      const e = prova(c, extra);
      assert.ok(
        e.errori.some((x) => atteso.test(x)),
        `${atteso} → ${JSON.stringify(e.errori)}`,
      );
      assert.equal(e.pronto, false);
    }
  });

  it("backup L1 nel cloud: R2 + Cron + mac-backup coerenti; incoerenze trovate", () => {
    const pieno = (): Cfg => {
      const c = completa();
      c.r2_buckets = [{ binding: "BACKUP", bucket_name: "cuore-backup" }];
      c.triggers = { crons: ["0 5,11,17,21 * * *"] };
      c.vars.ACCESS_SERVIZIO_BACKUP = "macbackupprova0001.access";
      c.vars.BACKUP_MAC_PUBLIC_JWK = '{"kty":"RSA"}';
      c.vars.BACKUP_RECOVERY_PUBLIC_JWK = '{"kty":"RSA"}';
      return c;
    };
    const ok = prova(pieno(), { segretiCreati: ["ACCESS_ALLOWED_EMAILS", "BACKUP_SIGNING_PRIVATE_JWK"] });
    assert.deepEqual(ok.errori, []);
    assert.ok(ok.aPosto.some((x) => x.includes("una copia al giorno, ultime 7")));
    assert.ok(!ok.daFare.some((d) => /R2|mac-backup|Chiavi dei backup/.test(d.cosa)));
    const casi: [(c: Cfg) => void, RegExp][] = [
      [(c) => delete c.triggers, /senza Cron/],
      [(c) => (c.r2_buckets = [{ binding: "ALTRO", bucket_name: "x" }]), /un solo binding BACKUP/],
      [
        (c) => (c.r2_buckets = [...c.r2_buckets, { binding: "BACKUP2", bucket_name: "y" }]),
        /un solo binding/,
      ],
      [(c) => delete c.r2_buckets, /ritiro resterebbe chiusa/],
      [(c) => (c.vars.ACCESS_SERVIZIO_BACKUP = "non-un-id"), /ACCESS_SERVIZIO_BACKUP non valido/],
    ];
    for (const [modifica, atteso] of casi) {
      const c = pieno();
      modifica(c);
      const e = prova(c);
      assert.ok(
        e.errori.some((x) => atteso.test(x)),
        `${atteso} → ${JSON.stringify(e.errori)}`,
      );
    }
    const senzaChiavi = prova(pieno(), { segretiCreati: ["ACCESS_ALLOWED_EMAILS"] });
    assert.ok(senzaChiavi.daFare.some((d) => d.cosa.includes("Chiavi dei backup")));
  });

  it("banco di misura: il file vero e' un Worker separato e spento; incoerenze trovate", () => {
    const e = prova(VERA, { misure: MISURE });
    assert.deepEqual(e.errori, []);
    assert.ok(
      e.daFare.some((d) => d.cosa.includes("wrangler.misure.jsonc") && d.quando === "dopo, facoltativo"),
    );
    const casi: [(m: Cfg) => void, RegExp][] = [
      [(m) => (m.name = "cuore"), /sostituirebbe/],
      [(m) => (m.main = "src/index.ts"), /main/],
      [(m) => m.durable_objects.bindings.push({ name: "FINANCE", class_name: "FinanceVetrina" }), /BANCO/],
      [(m) => (m.vars.MISURE_TOKEN_SHA256 = "ab".repeat(32)), /--var/],
      [(m) => (m.triggers = { crons: ["* * * * *"] }), /triggers/],
      [(m) => (m.account_id = "0123456789abcdef0123456789abcdef"), /account_id/],
      [(m) => (m.observability = { enabled: true }), /observability/],
    ];
    for (const [modifica, atteso] of casi) {
      const m = structuredClone(MISURE);
      modifica(m);
      const r = prova(VERA, { misure: m });
      assert.ok(
        r.errori.some((x) => atteso.test(x)),
        `${atteso} → ${JSON.stringify(r.errori)}`,
      );
    }
  });

  it("M3 configurato in modo coerente: nessuna incoerenza, passo della migrazione segnato come fatto", () => {
    const c = completa();
    c.vars.CUORE_FINANCE_ORIGINE = "mac";
    c.vars.ACCESS_SERVIZIO_MIGRAZIONE = "migrazioneprova0001.access";
    const e = prova(c, { segretiCreati: ["ACCESS_ALLOWED_EMAILS"] });
    assert.deepEqual(e.errori, []);
    assert.ok(!e.daFare.some((d) => d.cosa.includes("token di servizio temporaneo")));
    assert.ok(e.daFare.some((d) => d.cosa.includes("eliminare il token `mac-migrazione`")));
    assert.ok(
      e.daFare.some((d) => d.cosa.includes("CUORE_SCRITTURE")),
      "modifiche dal telefono da aprire",
    );
  });

  it("dati veri online con le modifiche aperte: coerente, nessuna incoerenza", () => {
    const c = completa();
    c.vars.CUORE_FINANCE_ORIGINE = "mac";
    c.vars.CUORE_SCRITTURE = "attive";
    const e = prova(c, { segretiCreati: ["ACCESS_ALLOWED_EMAILS"] });
    assert.deepEqual(e.errori, []);
    assert.ok(e.aPosto.some((x) => x.includes("Modifiche dal telefono aperte")));
  });
});
