/**
 * Pacchetto di produzione costruito da wrangler (`deploy --dry-run`, nessuna rete, nessun account) e controllato:
 * niente moduli `node:`, niente accesso simulato, niente segreti, dimensione compressa entro il limite del Free.
 * Il controllo dei controlli usa cartelle finte con valori FITTIZI evidenti (mai segreti reali).
 */
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { before, describe, it } from "node:test";
import {
  ambienteSenzaRete,
  analizzaPacchetto,
  costruisciPacchetto,
  type EsitoPacchetto,
  LIMITI,
} from "../scripts/pacchetto.ts";

let vero: EsitoPacchetto;

before(() => {
  const cartella = mkdtempSync(join(tmpdir(), "cuore-pacchetto-test-"));
  const { wrangler } = costruisciPacchetto(cartella);
  vero = analizzaPacchetto(cartella, wrangler);
});

function finto(file: Record<string, string>): EsitoPacchetto {
  const cartella = mkdtempSync(join(tmpdir(), "cuore-pacchetto-finto-"));
  for (const [nome, testo] of Object.entries(file)) writeFileSync(join(cartella, nome), testo);
  return analizzaPacchetto(cartella);
}
const BUONO = "// src/index.ts\nexport default {};\n";

describe("pacchetto di produzione (wrangler --dry-run, senza rete)", () => {
  it("si costruisce da src/index.ts, senza problemi e molto sotto il limite del Free", () => {
    assert.deepEqual(vero.problemi, []);
    assert.ok(vero.file.some((f) => f.nome === "index.js"));
    assert.ok(
      vero.file.some((f) => f.nome.endsWith("index.html")),
      "la pagina viaggia come modulo di testo",
    );
    assert.ok(vero.entroFree && vero.entroPaid);
    assert.ok(vero.compressi < LIMITI.free / 4, `compresso ${vero.compressi} byte: margine ampio`);
    assert.match(vero.wrangler ?? "", /Total Upload: .* gzip: /);
  });

  it("wrangler gira senza credenziali e con la rete deviata su una porta chiusa", () => {
    const env = ambienteSenzaRete({
      CLOUDFLARE_API_TOKEN: "fittizio-mai-reale",
      CF_ACCOUNT_ID: "fittizio",
      https_proxy: "http://proxy.invalid",
      PATH: "/usr/bin",
    });
    assert.equal(env.CLOUDFLARE_API_TOKEN, undefined);
    assert.equal(env.CF_ACCOUNT_ID, undefined);
    assert.equal(env.https_proxy, undefined);
    assert.equal(env.HTTPS_PROXY, "http://127.0.0.1:9");
    assert.equal(env.WRANGLER_SEND_METRICS, "false");
    assert.equal(env.PATH, "/usr/bin");
  });

  it("controllo dei controlli: moduli node:, accesso simulato e segreti vengono trovati", () => {
    assert.deepEqual(finto({ "index.js": BUONO }).problemi, []);
    const casi: [Record<string, string>, RegExp][] = [
      [{ "index.js": `${BUONO}import fs from "node:fs";` }, /modulo node:/],
      [{ "index.js": `${BUONO}const x = await import("node:crypto");` }, /modulo node:/],
      [{ "index.js": `${BUONO}// src/locale.ts\n` }, /SOLO locale/],
      [{ "index.js": `${BUONO}fetch("/__locale/accedi")` }, /accesso simulato/],
      [{ "index.js": BUONO, "pagina.html": "titolare@vetrina.invalid" }, /accesso simulato/],
      [{ "index.js": `${BUONO}"-----BEGIN PRIVATE KEY-----FITTIZIA"` }, /PEM/],
      [{ "index.js": `${BUONO}{"kty":"EC","d":"FITTIZIOfittizioFITTIZIOfittizio"}` }, /JWK/],
      [{ "index.js": `${BUONO}"CF-Access-Client-Secret": "fittizio-0001"` }, /Access/],
      [{ "index.js": "export default {};" }, /src\/index\.ts/],
      [{ "altro.js": BUONO }, /manca il modulo principale/],
    ];
    for (const [file, atteso] of casi) {
      const p = finto(file).problemi;
      assert.ok(
        p.some((x) => atteso.test(x)),
        `${JSON.stringify(file)} → ${JSON.stringify(p)}`,
      );
    }
  });

  it("oltre il limite del Free = problema", () => {
    const enorme = Array.from({ length: 900_000 }, (_, i) => ((i * 2654435761) >>> 0).toString(36)).join("");
    const e = finto({ "index.js": `${BUONO}"${enorme}"` });
    assert.equal(e.entroFree, false);
    assert.ok(e.problemi.some((p) => p.includes("Free")));
  });
});
