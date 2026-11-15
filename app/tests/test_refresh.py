"""CUORE: aggiornamento dei dati senza ricaricare la pagina (pulsanti Aggiorna/Riprova). Ponte SIMULATO in Node con feed
fittizi minimi: nessun ponte reale, nessun dato reale."""
import json, os, re, shutil, subprocess, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
NODE = shutil.which("node") or ("/usr/local/bin/node" if os.path.isfile("/usr/local/bin/node") else None)

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const el = () => ({ innerHTML: "", textContent: "", dataset: {}, setAttribute() {}, scrollIntoView() {} });
const els = {};
const feed = { schema: "cuore.money.feed/1", fonte: "reale", sola_lettura_verificata: true, generato: "2026-01-31T09:15:00+01:00", oggi: "2026-01-31",
  esito: "completo", errori: [], operazioni: { accounts: { ok: true, contractVersion: 1, data: [] } },
  aggiornamento: { stato: "nessun_dato", letto_il: "2026-01-31T09:15:00+01:00" } };
let answers = [];                                     // risposte simulate di /api/money, in ordine
const fetch = async url => { const a = answers.shift(); if (a instanceof Error) throw a;
  return { ok: true, status: 200, json: async () => (url === "/api/money" ? a : { schema: "x" }) }; };
const ctx = { console, Intl, Math, Date, String, Number, Object, Array, JSON, RegExp, Set, Promise, setTimeout, fetch,
  location: { hash: "", protocol: "http:" }, history: { replaceState() {}, pushState() {} }, matchMedia: () => ({ matches: false, addEventListener() {} }),
  addEventListener() {}, scrollTo() {},
  document: { documentElement: { dataset: {} }, getElementById: id => (els[id] = els[id] || el()), querySelectorAll: () => [], querySelector: () => null, addEventListener() {} } };
ctx.window = ctx;
(async () => {
  answers = [{ schema: "x" }, feed];                  // avvio: School (non valido) e Money
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
  await new Promise(r => setTimeout(r, 20));
  const C = ctx.CUORE, out = {};
  out.primo = { mode: C.MONEY_STATE.mode, html: C.viewHtml(C.parse("money/quadro")) };
  answers = [new TypeError("Failed to fetch")];      // come il browser quando il ponte non risponde
  await C.loadMoney();
  out.fallito = { mode: C.MONEY_STATE.mode, refreshError: C.MONEY_STATE.refreshError, html: C.viewHtml(C.parse("money/quadro")), home: C.viewHtml(C.parse("")) };
  answers = [Object.assign({}, feed, { generato: "2026-01-31T10:00:00+01:00" })];
  await C.loadMoney();
  out.ripreso = { mode: C.MONEY_STATE.mode, refreshError: C.MONEY_STATE.refreshError, html: C.viewHtml(C.parse("money/quadro")) };
  console.log(JSON.stringify(out));
})();
"""


@unittest.skipUnless(NODE, "node non disponibile")
class Aggiornamento(unittest.TestCase):
    def test_aggiornamento_fallito_conserva_i_dati_dichiarandolo(self):
        with open(os.path.join(APP, "index.html"), encoding="utf-8") as fh:
            script = re.search(r"<script>(.*?)</script>", fh.read(), re.S).group(1)
        tmp = tempfile.mkdtemp()
        try:
            page, h = os.path.join(tmp, "page.js"), os.path.join(tmp, "h.js")
            with open(page, "w", encoding="utf-8") as f:
                f.write(script)
            with open(h, "w", encoding="utf-8") as f:
                f.write(HARNESS)
            r = subprocess.run([NODE, h, page], capture_output=True, text=True, timeout=60)
        finally:
            shutil.rmtree(tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        v = json.loads(r.stdout)
        self.assertEqual(v["primo"]["mode"], "real")
        self.assertIn('data-reload="money"', v["primo"]["html"])                    # pulsante Aggiorna nel banner
        self.assertNotIn("non riuscito", v["primo"]["html"])
        # aggiornamento fallito: restano i dati, con l'ora della lettura precedente
        self.assertEqual((v["fallito"]["mode"], v["fallito"]["refreshError"]), ("real", "ponte locale non raggiungibile: Mac spento o in stop, ponte fermo o rete assente"))
        self.assertIn("ultimo aggiornamento non riuscito", v["fallito"]["html"])
        self.assertIn("dati della lettura delle 09:15", v["fallito"]["html"])
        self.assertIn(">sola lettura<", v["fallito"]["html"])                     # modalità nella riga di stato
        self.assertIn("non riuscito: dati della lettura precedente", v["fallito"]["home"])
        # nuovo tentativo riuscito: avviso sparito, dati nuovi
        self.assertEqual((v["ripreso"]["mode"], v["ripreso"]["refreshError"]), ("real", None))
        self.assertNotIn("non riuscito", v["ripreso"]["html"])
        self.assertIn("10:00", v["ripreso"]["html"])


if __name__ == "__main__":
    unittest.main()
