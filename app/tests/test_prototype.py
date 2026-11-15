"""CUORE prototipo: due dashboard distinte (School, Money), home senza dati; aperto come file solo dati dimostrativi e nessuna rete.
Esecuzione: python3 -m unittest discover -s app/tests (dalla radice del repository)."""
import json, os, re, shutil, subprocess, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(os.path.dirname(HERE), "index.html")

# Esegue lo script della pagina in Node con un DOM minimo e restituisce l'HTML di ogni vista.
HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const el = () => ({ innerHTML: "", textContent: "", dataset: {}, setAttribute() {}, scrollIntoView() {} });
const els = {};
const ctx = {
  console, Intl, Math, Date, String, Number, Object, Array, JSON, RegExp,
  location: { hash: "" }, history: { replaceState() {} },
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  addEventListener() {}, scrollTo() {},
  document: { documentElement: { dataset: {} }, getElementById: id => (els[id] = els[id] || el()),
              querySelectorAll: () => [], querySelector: () => null, addEventListener() {} },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const C = ctx.CUORE, out = { home: C.viewHtml(C.parse("")) };
for (const app of ["school", "money"])
  for (const [s] of C.APPS[app].sections) out[app + "/" + s] = C.viewHtml(C.parse(app + "/" + s));
out.__sections = { school: C.APPS.school.sections.map(s => s[0]), money: C.APPS.money.sections.map(s => s[0]) };
out.__parse = [C.parse("#/school"), C.parse("#/money/inesistente"), C.parse("#/altro")];
console.log(JSON.stringify(out));
"""


class Prototipo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PAGE, encoding="utf-8") as f:
            cls.html = f.read()
        cls.script = re.search(r"<script>(.*?)</script>", cls.html, re.S).group(1)
        cls.views = None
        if shutil.which("node"):
            with tempfile.TemporaryDirectory() as tmp:
                js, h = os.path.join(tmp, "page.js"), os.path.join(tmp, "harness.js")
                for path, text in ((js, cls.script), (h, HARNESS)):
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(text)
                r = subprocess.run(["node", h, js], capture_output=True, text=True)
                if r.returncode != 0:
                    raise AssertionError(r.stderr)
                cls.views = json.loads(r.stdout)

    def need_views(self):
        if self.views is None:
            self.skipTest("node non disponibile")
        return self.views

    def test_nessuna_rete_ne_risorse_esterne(self):
        # solo due collegamenti che l'utente apre a mano (brief 06/11: Classroom e Claude con il piano), mai risorse caricate
        self.assertEqual(set(re.findall(r"https?://[^\s\"'<>)]+", self.html)), {"https://classroom.google.com/", "https://claude.ai/new?q="})
        self.assertEqual(re.findall(r'src="([^"]*)"', self.html), ["push.js"])                 # unico script esterno: locale (notifiche push)
        for api in ("XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "importScripts", "sessionStorage", "indexedDB"):
            self.assertNotIn(api, self.html, api)
        # PWA (D1): solo i due <link> locali attesi (manifest, icona), mai una risorsa esterna o uno stylesheet/script via <link>.
        links = re.findall(r'<link\s+([^>]*)>', self.html)
        self.assertEqual(len(links), 2, links)
        for attrs in links:
            rel = re.search(r'rel="([^"]+)"', attrs).group(1)
            href = re.search(r'href="([^"]+)"', attrs).group(1)
            self.assertIn(rel, ("manifest", "apple-touch-icon"), attrs)
            self.assertNotRegex(href, r"^https?://", href)
        # nel browser solo le preferenze delle Impostazioni (tema, promemoria), mai dati
        self.assertEqual(set(re.findall(r"localStorage\.\w+\(([^,)]*)", self.html)), {'"cuore.impostazioni"'})
        self.assertEqual(sorted(re.findall(r"fetch\(([^,)]*)", self.html)),                 # feed del ponte e stato della vetrina, stessa origine
                         ['"/api/events"', '"/api/money"', '"/api/promemoria"', '"/api/school"', '"/api/search?q=" + encodeURIComponent(q', '"/api/vetrina/stato"', "WRITE_URL"])
        self.assertIn('const WRITE_URL = PROVA ? "/api/prova/esegui" : "/api/scritture/esegui";', self.html)   # scritture: solo i due ponti

    def test_sezioni_richieste(self):
        v = self.need_views()
        self.assertEqual(v["__sections"]["school"], ["oggi", "verifiche", "voti", "assenze", "materie", "materiali", "studio"])
        self.assertEqual(v["__sections"]["money"], ["quadro", "flussi", "movimenti", "saldi", "budget", "acquisti", "debiti", "patrimonio", "investimenti", "appunti"])   # brief 06/11: Appunti in fondo

    def test_banner_dimostrativo_ovunque(self):
        for k, html in self.need_views().items():
            if not k.startswith("__"):
                self.assertIn("DATI DIMOSTRATIVI", html, k)

    def test_home_senza_dati(self):
        home = self.need_views()["home"]
        self.assertIn('data-route="school"', home)
        self.assertIn('data-route="money"', home)
        self.assertNotIn("€", home)
        self.assertNotIn('class="card kpi', home)
        self.assertIsNone(re.search(r"\d+,\d", home), "nessun valore numerico nella home")

    def test_dashboard_separate(self):
        v = self.need_views()
        school_words = ("Media generale", "Verifica", "Matematica", "Assenze", "Compiti")
        for k, html in v.items():
            if k.startswith("school/"):
                self.assertNotIn("€", html, k)
                self.assertNotIn("DEMO", html.replace("DATI DIMOSTRATIVI", ""), k)       # nessun conto o movimento
            if k.startswith("money/"):
                for w in school_words:
                    self.assertNotIn(w, html, (k, w))

    def test_investimenti_senza_dati_inventati(self):
        inv = self.need_views()["money/investimenti"]
        self.assertIn("non sono implementati", inv)
        self.assertNotIn("€", inv)
        self.assertIsNone(re.search(r"\d+,\d", inv))

    def test_routing(self):
        p = self.need_views()["__parse"]
        self.assertEqual(p[0], {"app": "school", "section": "oggi"})
        self.assertEqual(p[1], {"app": "money", "section": "quadro"})
        self.assertEqual(p[2], {"app": "home", "section": None})

    def test_responsive(self):
        for s in ('name="viewport"', "@media (max-width: 960px)", "@media (max-width: 700px)", "safe-area-inset-bottom",
                  "prefers-color-scheme: dark", "overflow-x: hidden"):
            self.assertIn(s, self.html, s)


# Pagina servita dal Worker della vetrina (cloud/): segnale <meta name="cuore-vetrina">, origine http, fetch registrato.
VETRINA_HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const el = () => ({ innerHTML: "", textContent: "", dataset: {}, setAttribute() {}, scrollIntoView() {} });
const els = {}, fetched = [];
const stato = { schema: "cuore.vetrina.stato/1", dati: "sintetici", finance: { movimenti: 12, conti: 2, versione_schema: 6,
  consultazione_ok: true, integrita: "ok", classificati: 12, da_verificare: 3, trasferimenti_proposti: 1, operazioni_riuscite: "9 su 9" } };
const ctx = {
  console, Intl, Math, Date, String, Number, Object, Array, JSON, RegExp, Promise, setTimeout, clearTimeout,
  location: { hash: "", protocol: "https:", host: "cuore.esempio.workers.dev" }, history: { replaceState() {} },
  matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {}, scrollTo() {},
  fetch: async url => { fetched.push(url); return { ok: url === "/api/vetrina/stato", status: 200, json: async () => stato }; },
  document: { documentElement: { dataset: {} }, getElementById: id => (els[id] = els[id] || el()), querySelectorAll: () => [],
              querySelector: s => s === 'meta[name="cuore-vetrina"]' ? {} : null, addEventListener() {} },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
setTimeout(() => {
  const C = ctx.CUORE, out = { fetched, vetrina: C.VETRINA, sources: C.systemSources() };
  for (const r of ["", "school/oggi", "school/studio", "money/quadro", "money/patrimonio", "cuore/diagnostica", "cuore/cerca", "cuore/eventi", "cuore/automazioni"])
    out[r || "home"] = C.viewHtml(C.parse(r));
  console.log(JSON.stringify(out));
}, 20);
"""


class Vetrina(unittest.TestCase):
    """Con il segnale della vetrina: solo dati dimostrativi, nessuna lettura delle API reali, stato del Durable Object."""

    @classmethod
    def setUpClass(cls):
        cls.out = None
        if not shutil.which("node"):
            return
        with open(PAGE, encoding="utf-8") as f:
            script = re.search(r"<script>(.*?)</script>", f.read(), re.S).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            js, h = os.path.join(tmp, "page.js"), os.path.join(tmp, "harness.js")
            for path, text in ((js, script), (h, VETRINA_HARNESS)):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
            r = subprocess.run(["node", h, js], capture_output=True, text=True)
            if r.returncode != 0:
                raise AssertionError(r.stderr)
            cls.out = json.loads(r.stdout)

    def need(self):
        if self.out is None:
            self.skipTest("node non disponibile")
        return self.out

    def test_nessuna_lettura_delle_api_reali(self):
        o = self.need()
        self.assertTrue(o["vetrina"])
        self.assertEqual(o["fetched"], ["/api/vetrina/stato"])

    def test_dati_inventati_dichiarati_ovunque(self):
        o = self.need()
        for k in ("home", "school/oggi", "school/studio", "money/quadro", "money/patrimonio"):
            self.assertIn("VETRINA · DATI INVENTATI", o[k], k)
            self.assertNotIn("ponte locale", o[k], k)

    def test_stato_dal_durable_object_sintetico(self):
        o = self.need()
        d = o["cuore/diagnostica"]
        self.assertIn("Durable Object Finance (dati sintetici)", d)
        self.assertIn("12 movimenti sintetici in 2 conti", d)
        self.assertNotIn("Ponte locale", d)
        names = {s["name"]: s["st"] for s in o["sources"]}
        self.assertEqual(names["Durable Object Finance (dati sintetici)"], "ok")
        self.assertEqual(names["Collegamento bancario"], "off")

    def test_sezioni_dei_dati_reali_non_disponibili(self):
        o = self.need()
        for k in ("cuore/cerca", "cuore/automazioni"):                    # «Eventi» tolto dal brief 06/11 (porta a Stato)
            self.assertIn("Non disponibile nella vetrina", o[k], k)

    def test_home_non_dichiara_regolare_il_sistema_reale(self):
        home = self.need()["home"]
        self.assertNotIn("tutto regolare", home)
        self.assertIn(">vetrina<", home)


# Impostazioni dei promemoria e Finance › Appunti con il ponte (origine http): con le modifiche (segnale cuore-prova) le
# letture e le scritture passano da /api/prova/esegui con un requestId; senza, solo GET /api/promemoria e nessuna modifica.
# Documenti dei motori veri (promemoria.py, finance_appunti.py) su file sintetici in una cartella temporanea.
SETTINGS_HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const [page, docsPath, meta] = process.argv.slice(2), docs = JSON.parse(fs.readFileSync(docsPath, "utf8"));
const el = () => ({ innerHTML: "", textContent: "", dataset: {}, setAttribute() {}, scrollIntoView() {}, querySelector: () => null });
const els = {}, L = {}, sent = [], fetched = [];
const reply = (status, doc) => ({ ok: status === 200, status, json: async () => doc });
const ctx = { console, Intl, Math, Date, String, Number, Object, Array, JSON, RegExp, Set, Promise, setTimeout, clearTimeout,
  location: { hash: "", protocol: "http:", host: "127.0.0.1:8797" }, history: { replaceState() {}, pushState() {} },
  matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {}, scrollTo() {},
  fetch: async (url, o) => {
    fetched.push(url);
    if (url === "/api/promemoria") return reply(200, docs.promemoria);
    if (url !== "/api/prova/esegui") return reply(503, {});
    const b = JSON.parse(o.body); sent.push(b);
    const r = { promemoria_stato: docs.promemoria, promemoria_imposta: docs.promemoria, appunti_stato: docs.appunti }[b.op] || { id: "nuovo" };
    return reply(200, { ok: true, risultato: r });
  },
  document: { documentElement: { dataset: {} }, body: { appendChild: x => x }, getElementById: id => (els[id] = els[id] || el()),
    querySelectorAll: () => [], querySelector: s => s.includes(meta) ? {} : null, addEventListener: (t, f) => (L[t] = L[t] || []).push(f) } };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(page, "utf8"), ctx);
const C = ctx.CUORE, wait = () => new Promise(r => setTimeout(r, 30));
const target = (sel, dataset, attrs) => ({ closest: s => s === sel ? { dataset, getAttribute: k => (attrs || {})[k] } : null, classList: { contains: () => false } });
const click = t => L.click.forEach(f => f({ target: t, preventDefault() {} }));
(async () => {
  C.viewHtml(C.parse("cuore/impostazioni")); await wait();
  const out = { impostazioni: C.viewHtml(C.parse("cuore/impostazioni")) };
  await C.loadNotes(); out.appunti = C.viewHtml(C.parse("money/appunti")); out.vicini = C.notesSoonCard();
  click(target("[data-rem-on]", { remOn: "sch_voti" })); await wait();
  click(target("[data-mom]", { rem: "sch_voti", mom: "pomeriggio" })); await wait();
  click(target("[data-prom]", { prom: "attivo" })); await wait();
  click(target("[data-sheet]", { sheet: "appunto" }));
  const f = { "#f-text": { value: " Comprare il casco " }, "#f-ncat": { value: "da_comprare" }, "#f-ndate": { value: "2026-11-20" },
              "#f-ntime": { value: "15:00" }, "#f-nrem": { checked: true } };
  L.submit.forEach(fn => fn({ target: { matches: s => s === "#sheet form", querySelector: k => f[k] }, preventDefault() {} })); await wait();
  const id = (docs.appunti.appunti.find(a => a.promemoria) || {}).id;
  click(target("[data-nota-rem]", { notaRem: id }, { "aria-checked": "true" })); await wait();
  console.log(JSON.stringify(Object.assign(out, { sent, fetched })));
})();
"""


class ImpostazioniEAppunti(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import datetime as dt, sys
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bridge"))
        import finance_appunti as FA, scritture
        cls.out = {}
        if not shutil.which("node"):
            return
        with open(PAGE, encoding="utf-8") as f:
            script = re.search(r"<script>(.*?)</script>", f.read(), re.S).group(1)
        today = dt.date.today()
        with tempfile.TemporaryDirectory() as tmp:
            notes, on = os.path.join(tmp, "appunti.jsonl"), os.path.join(tmp, "promemoria-acceso.json")
            soon = FA.aggiungi(notes, {"tipo": "crea", "testo": "Casco <b>", "categoria": "da_comprare",
                                       "quando": (today + dt.timedelta(days=1)).isoformat() + "T15:00"})
            FA.aggiungi(notes, {"tipo": "crea", "testo": "Vacanze lontane", "quando": (today + dt.timedelta(days=30)).isoformat()})
            FA.aggiungi(notes, {"tipo": "crea", "testo": "Occhio alle spese"})
            done = FA.aggiungi(notes, {"tipo": "crea", "testo": "Quaderni comprati"})
            FA.aggiungi(notes, {"tipo": "completa", "id": done["id"]})
            gone = FA.aggiungi(notes, {"tipo": "crea", "testo": "Regalo annullato"})
            FA.aggiungi(notes, {"tipo": "annulla", "id": gone["id"], "motivo": "non serve più"})
            scritture.promemoria.update_settings(on, {"attivo": True, "promemoria": {"fin_appunti": True}})
            docs = {"promemoria": scritture.promemoria_op(os.path.join(tmp, "assente.json"), notes, "promemoria_stato", {}),
                    "appunti": scritture.promemoria_op(on, notes, "appunti_stato", {})}
            cls.soon_id = soon["id"]
            paths = {n: os.path.join(tmp, n) for n in ("page.js", "h.js", "docs.json")}
            for n, text in (("page.js", script), ("h.js", SETTINGS_HARNESS), ("docs.json", json.dumps(docs))):
                with open(paths[n], "w", encoding="utf-8") as f:
                    f.write(text)
            for meta in ("cuore-prova", "nessuno"):
                r = subprocess.run(["node", paths["h.js"], paths["page.js"], paths["docs.json"], meta], capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    raise AssertionError(r.stderr)
                cls.out[meta] = json.loads(r.stdout)

    def need(self, meta):
        if not self.out:
            self.skipTest("node non disponibile")
        return self.out[meta]

    def test_promemoria_tutti_spenti_e_modificabili(self):
        o = self.need("cuore-prova")
        h = o["impostazioni"]
        self.assertEqual(len(re.findall(r'data-rem-on="', h)), 14)
        self.assertEqual(re.findall(r'aria-checked="true"[^>]*data-rem-on', h), [])             # tutti spenti all'inizio
        self.assertRegex(h, r'aria-checked="false" aria-label="Promemoria del Mac" data-prom="attivo">')
        for s in ("Testi riservati", "Controlli alle 07:15, 15:00, 19:00", "le notifiche arrivano su questo Mac",
                  'data-id="Y.I.3.1"', 'data-id="Y.I.4.1"', 'data-id="Y.I.4.2"', "Verifica in arrivo", "Spese in contanti",
                  "solo sera, alle 19:00", "proposta", "Nessuna scelta salvata finora"):
            self.assertIn(s, h, s)
        self.assertNotIn('data-mom="mattina" data-rem="fin_contanti"', h)                    # solo i momenti ammessi
        self.assertNotIn("disabled", re.search(r'data-rem-on="sch_voti"[^>]*>', h).group(0))
        for bad in ("undefined", "NaN", "[object"):
            self.assertNotIn(bad, h)

    def test_modifiche_con_request_id(self):
        sent = self.need("cuore-prova")["sent"]
        ops = [(b["op"], b["input"]) for b in sent]
        self.assertEqual(ops[:2], [("appunti_stato", {}), ("promemoria_stato", {})])           # appunti all'avvio, promemoria in Impostazioni
        self.assertIn(("promemoria_imposta", {"promemoria": {"sch_voti": {"attivo": True}}}), ops)
        self.assertIn(("promemoria_imposta", {"promemoria": {"sch_voti": {"momenti": ["mattina", "sera"]}}}), ops)
        self.assertIn(("promemoria_imposta", {"attivo": True}), ops)
        self.assertIn(("appunti_scrivi", {"tipo": "crea", "testo": "Comprare il casco", "categoria": "da_comprare",
                                          "quando": "2026-11-20T15:00", "promemoria": True}), ops)
        self.assertIn(("appunti_scrivi", {"tipo": "promemoria", "id": self.soon_id, "attivo": False}), ops)
        for b in sent:
            self.assertTrue(isinstance(b["requestId"], str) and 8 <= len(b["requestId"]) <= 100, b)
        self.assertEqual(len({b["requestId"] for b in sent}), len(sent))

    def test_appunti(self):
        o = self.need("cuore-prova")
        a = o["appunti"]
        for s in ('data-id="F.N.1"', 'data-id="F.N.2"', 'data-id="F.N.2.1"', 'data-id="F.N.3"', "+ Appunto", "Casco &lt;b&gt;",
                  "Vacanze lontane", "Occhio alle spese", "Quaderni comprati", "motivo: non serve più", "Da comprare · domani alle 15:00",
                  'data-nota-rem="' + self.soon_id + '"', 'data-sheet="nannulla"', 'data-sheet="nfatto"'):
            self.assertIn(s, a, s)
        self.assertNotIn("Casco <b>", a)                                                      # testo sempre escapato
        self.assertIn("F.R.8", o["vicini"])
        self.assertIn("Casco", o["vicini"])
        self.assertNotIn("Vacanze lontane", o["vicini"])                                      # solo i promemoria vicini
        for bad in ("undefined", "NaN", "[object"):
            self.assertNotIn(bad, a)

    def test_sola_lettura(self):
        o = self.need("nessuno")
        self.assertIn("/api/promemoria", o["fetched"])
        self.assertEqual(o["sent"], [])                                                       # nessuna scrittura
        h = o["impostazioni"]
        self.assertIn("Qui puoi solo guardare", h)
        self.assertEqual(len(re.findall(r'data-rem-on="[a-z_]+" disabled', h)), 14)
        self.assertIn("Disponibili con le modifiche attive", o["appunti"])
        self.assertNotIn("+ Appunto", o["appunti"])


# Pagina servita dal Worker con i DATI VERI (cloud/src/online.ts): segnali cuore-online + cuore-scritture, origine https.
ONLINE_HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const el = () => ({ innerHTML: "", textContent: "", dataset: {}, setAttribute() {}, scrollIntoView() {}, querySelector: () => null });
const els = {}, fetched = [], sent = [];
const metas = ['meta[name="cuore-online"]', 'meta[name="cuore-scritture"]'];
const ctx = { console, Intl, Math, Date, String, Number, Object, Array, JSON, RegExp, Set, Promise, setTimeout, clearTimeout,
  location: { hash: "", protocol: "https:", host: "cuore.esempio.workers.dev" }, history: { replaceState() {}, pushState() {} },
  matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {}, scrollTo() {},
  fetch: async (url, o) => { fetched.push(url); if (o && o.body) sent.push(JSON.parse(o.body));
    return { ok: false, status: 503, json: async () => ({ ok: false, errore: "non_collegato" }) }; },
  document: { documentElement: { dataset: {} }, body: { appendChild: x => x }, getElementById: id => (els[id] = els[id] || el()),
    querySelectorAll: () => [], querySelector: s => metas.includes(s) ? {} : null, addEventListener() {} } };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
setTimeout(async () => {
  const C = ctx.CUORE;
  C.viewHtml(C.parse("cuore/impostazioni")); await C.loadProm(); await C.loadNotes();
  console.log(JSON.stringify({ online: C.ONLINE, label: C.MODE_LABEL, fetched, sent,
    impostazioni: C.viewHtml(C.parse("cuore/impostazioni")), appunti: C.viewHtml(C.parse("money/appunti")) }));
}, 20);
"""


class Online(unittest.TestCase):
    """Con i segnali online: le stesse API del ponte reale, «online · dati reali», promemoria e appunti restano sul Mac."""

    @classmethod
    def setUpClass(cls):
        cls.out = None
        if not shutil.which("node"):
            return
        with open(PAGE, encoding="utf-8") as f:
            script = re.search(r"<script>(.*?)</script>", f.read(), re.S).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            js, h = os.path.join(tmp, "page.js"), os.path.join(tmp, "harness.js")
            for path, text in ((js, script), (h, ONLINE_HARNESS)):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
            r = subprocess.run(["node", h, js], capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise AssertionError(r.stderr)
            cls.out = json.loads(r.stdout)

    def need(self):
        if self.out is None:
            self.skipTest("node non disponibile")
        return self.out

    def test_api_del_ponte_e_segnale_online(self):
        o = self.need()
        self.assertTrue(o["online"])
        self.assertEqual(o["label"], "ONLINE · DATI REALI · MODIFICHE ATTIVE")
        for api in ("/api/school", "/api/money"):
            self.assertIn(api, o["fetched"])
        self.assertNotIn("/api/vetrina/stato", o["fetched"])

    def test_promemoria_e_appunti_anche_online(self):
        # dal 06/11 sera promemoria e appunti vivono anche nel cloud (Durable Object Finance, stesse operazioni del ponte)
        o = self.need()
        self.assertNotIn("/api/promemoria", o["fetched"])                       # si leggono con le modifiche attive
        ops = {b["op"] for b in o["sent"]}
        self.assertIn("promemoria_stato", ops)
        self.assertIn("appunti_stato", ops)
        self.assertNotIn("aprendo CUORE sul Mac", o["impostazioni"])
        self.assertNotIn("Promemoria del Mac", o["impostazioni"])


if __name__ == "__main__":
    unittest.main()
