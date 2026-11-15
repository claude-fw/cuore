"""CUORE School: storico dell'automazione da log JSONL e calendario del LaunchAgent (`cuore_automation.py`).
Solo log e plist SINTETICI in cartelle temporanee: i file reali del job non vengono mai letti dai test."""
import datetime as dt
import json
import os
import plistlib
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bridge"))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "..", "scuola", "mcp", "_common")))   # cuore_automation
import cuore_automation as A  # noqa: E402
import cuore_school as CS  # noqa: E402

Z = A.ZONE
UTC = dt.timezone.utc


def at(day, hhmm, sec=0):
    """Ora LOCALE (Europe/Rome) del giorno '2026-10-DD' -> datetime UTC."""
    h, m = map(int, hhmm.split(":"))
    y, mo, d = map(int, day.split("-"))
    return dt.datetime(y, mo, d, h, m, sec, tzinfo=Z).astimezone(UTC)


def line(t, evento, **kw):
    return json.dumps(dict(ts=t.isoformat(), evento=evento, **kw), sort_keys=True)


def run(day, start, minutes=1.5, esito="OK", fonti=("ok", "ok"), retries=0, anomalie=(), work_s=80.0):
    """Righe di un giro come le scrive il job (stessi nomi di evento e campi)."""
    t0 = at(day, start)
    t1 = t0 + dt.timedelta(minutes=minutes)
    out = [line(t0, "avvio", modo="commit", fonti=["classroom", "classeviva"])]
    for i in range(retries):
        out.append(line(t0 + dt.timedelta(seconds=10 + i), "retry", lettura="classroom/course_work", tentativo=i + 1,
                        attesa_s=5, errore="timed out con TOKEN-SINTETICO-XYZ cvv:1234567 /Users/sintetico"))
    for f, e in zip(("classroom", "classeviva"), fonti):
        out.append(line(t1 - dt.timedelta(seconds=5), "fonte", fonte=f, esito=e))
    out.append(line(t1, "fine", esito=esito, exit_code={"OK": 0, "PARZIALE": 1, "FALLITO": 2, "AUTORIZZAZIONE": 3,
                                                        "OCCUPATO": 4}[esito], durata_s=work_s, anomalie=list(anomalie),
                    oltre_soglia=[]))
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.log = os.path.join(self.dir, "school-job.jsonl")
        self.plist = os.path.join(self.dir, "cloud.scuola.job.plist")
        self.write_plist([{"Hour": 6, "Minute": 45}, {"Hour": 14, "Minute": 30}, {"Hour": 20, "Minute": 0}])

    def write_plist(self, sci, **extra):
        with open(self.plist, "wb") as f:
            plistlib.dump(dict(Label="cloud.scuola.job", RunAtLoad=True, StartCalendarInterval=sci, **extra), f)

    def write_log(self, lines, name=None):
        with open(os.path.join(self.dir, name) if name else self.log, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))

    def history(self, now):
        return A.history(self.log, self.plist, now)

    def slot(self, h, hhmm):
        return next(o for o in h["orari"] if o["orario"][11:16] == hhmm and o["orario"][:10] == self.day(o))

    @staticmethod
    def day(o):
        return o["orario"][:10]


class Esiti(Base):
    def test_ok_parziale_autorizzazione(self):
        self.write_log(["%s" % x for x in
                        run("2026-10-22", "20:00") + run("2026-10-23", "06:45", esito="PARZIALE", fonti=("fallita", "ok"))
                        + run("2026-10-23", "14:30", esito="AUTORIZZAZIONE", fonti=("autorizzazione", "ok"))
                        + run("2026-10-23", "20:00")])
        h = self.history(at("2026-10-23", "21:00"))
        st = {o["orario"][5:16]: (o["stato"], o.get("esito")) for o in h["orari"]}
        self.assertEqual(st["10-23T06:45"], ("eseguito", "parziale"))
        self.assertEqual(st["10-23T14:30"], ("eseguito", "autorizzazione"))
        self.assertEqual(st["10-23T20:00"], ("eseguito", "ok"))
        self.assertEqual(h["conteggi"]["parziale"], 1)
        self.assertEqual(h["conteggi"]["autorizzazione"], 1)
        g = h["giri"][0]
        self.assertEqual((g["esito"], g["exit_code"], g["orario_previsto"], g["ritardo_avvio_min"]), ("ok", 0, "20:00", 0))
        self.assertEqual(h["giri"][2]["fonti"], {"classeviva": "ok", "classroom": "fallita"})
        self.assertEqual(h["prossimo_giro"], "2026-10-24T06:45:00+02:00")

    def test_tentativi_contati_senza_testi(self):
        self.write_log(run("2026-10-23", "06:45", retries=3, anomalie=("titoli_non_determinabili:classroom/course_work",
                                                                          "uid_sospetto:cvv:7654321")))
        h = self.history(at("2026-10-23", "07:30"))
        g = h["giri"][0]
        self.assertEqual(g["tentativi"], 3)
        self.assertEqual(g["anomalie"], ["titoli_non_determinabili", "uid_sospetto"])     # solo la categoria
        text = json.dumps(h)
        for secret in ("TOKEN-SINTETICO", "cvv:1234567", "cvv:7654321", "/Users/", "course_work", "timed out"):
            self.assertNotIn(secret, text, secret)


class Mancati(Base):
    def test_mancato_con_log_che_copre_l_intervallo(self):
        self.write_log(run("2026-10-23", "06:45") + run("2026-10-23", "20:00"))
        h = self.history(at("2026-10-23", "21:00"))
        o = next(x for x in h["orari"] if x["orario"].startswith("2026-10-23T14:30"))
        self.assertEqual(o["stato"], "mancato")
        self.assertIn("nessun avvio registrato", o["motivo"])
        self.assertFalse(o["sovrapposto"])

    def test_mancato_perche_il_giro_precedente_era_in_corso(self):
        # partito alle 06:45 con il Mac in stop, concluso alle 15:09: launchd non avvia il giro delle 14:30
        self.write_log(run("2026-10-22", "06:46", minutes=503, work_s=370.9, retries=3) + run("2026-10-22", "20:00"))
        h = self.history(at("2026-10-22", "21:00"))
        o = next(x for x in h["orari"] if x["orario"].startswith("2026-10-22T14:30"))
        self.assertEqual((o["stato"], o["sovrapposto"]), ("mancato", True))
        self.assertIn("ancora in corso", o["motivo"])
        g = next(x for x in h["giri"] if x["avvio"].startswith("2026-10-22T06:46"))
        self.assertTrue(g["conclusione_tardiva"])                    # 503 min reali per 371 s di lavoro
        self.assertEqual((g["durata_reale_min"], g["durata_lavoro_s"]), (503, 370.9))

    def test_avvio_in_ritardo_dopo_lo_stop(self):
        self.write_log(run("2026-10-23", "08:20"))                       # orario 06:45, Mac risvegliato alle 08:20
        h = self.history(at("2026-10-23", "09:00"))
        o = next(x for x in h["orari"] if x["orario"].startswith("2026-10-23T06:45"))
        self.assertEqual((o["stato"], o["ritardo_avvio_min"], o["in_ritardo"]), ("eseguito", 95, True))
        self.assertEqual(h["in_ritardo"], 1)

    def test_orario_appena_passato_e_in_attesa(self):
        self.write_log(run("2026-10-23", "06:45"))
        h = self.history(at("2026-10-23", "14:35"))
        o = next(x for x in h["orari"] if x["orario"].startswith("2026-10-23T14:30"))
        self.assertEqual(o["stato"], "in_attesa")


class LogImperfetti(Base):
    def test_log_assente_e_vuoto(self):
        h = self.history(at("2026-10-23", "21:00"))
        self.assertEqual(h["log"]["stato"], "assente")
        self.assertTrue(h["orari"])
        self.assertTrue(all(o["stato"] in ("non_verificabile",) for o in h["orari"]))   # mai «mancato» senza prove
        self.write_log([])
        h = self.history(at("2026-10-23", "21:00"))
        self.assertEqual(h["log"]["stato"], "vuoto")
        self.assertNotIn("mancato", h["conteggi"])

    def test_riga_corrotta_nell_intervallo_rende_non_verificabile(self):
        lines = run("2026-10-23", "06:45") + ['{"ts": "2026-10-23T12:40:00+00:00", "evento": "avv'] + run("2026-10-23", "20:00")
        self.write_log(lines)
        h = self.history(at("2026-10-23", "21:00"))
        o = next(x for x in h["orari"] if x["orario"].startswith("2026-10-23T14:30"))
        self.assertEqual(o["stato"], "non_verificabile")
        self.assertEqual(h["log"]["righe_illeggibili"], 1)
        self.assertEqual(h["log"]["stato"], "con_righe_illeggibili")

    def test_riga_troncata_in_coda(self):
        self.write_log(run("2026-10-23", "06:45") + ['{"ts": "2026-10-23T12:30:'])
        h = self.history(at("2026-10-23", "15:00"))
        o = next(x for x in h["orari"] if x["orario"].startswith("2026-10-23T14:30"))
        self.assertEqual(o["stato"], "non_verificabile")

    def test_righe_di_tipo_inatteso_ignorate(self):
        self.write_log(run("2026-10-23", "06:45") + ["[1, 2]", '{"evento": "avvio"}', '{"ts": "2026-10-23T08:00:00", "evento": "avvio"}'])
        h = self.history(at("2026-10-23", "07:30"))
        self.assertEqual(len(h["giri"]), 1)                              # nessun giro inventato da righe senza orario valido
        self.assertEqual(h["log"]["righe_illeggibili"], 3)

    def test_log_ruotato(self):
        self.write_log(run("2026-10-22", "20:00"), name="school-job.jsonl.1")
        self.write_log(run("2026-10-23", "06:45"))
        h = self.history(at("2026-10-23", "07:30"))
        self.assertEqual(h["fonte"]["log"], "school-job.jsonl (+1 ruotati)")
        self.assertEqual([g["avvio"][:16] for g in h["giri"]], ["2026-10-23T06:45", "2026-10-22T20:00"])
        early = [o for o in h["orari"] if o["orario"] < "2026-10-22T20:00"]
        self.assertTrue(early and all(o["stato"] == "non_verificabile" for o in early))   # prima del log: nessuna prova

    def test_giro_senza_conclusione(self):
        self.write_log(run("2026-10-23", "06:45")[:1])                   # solo l'avvio
        self.assertEqual(self.history(at("2026-10-23", "07:30"))["giri"][0]["esito"], "in_corso")
        self.assertEqual(self.history(at("2026-10-23", "12:00"))["giri"][0]["esito"], "incompleto")

    def test_giro_occupato_non_scambiato_per_quello_vero(self):
        long_run = run("2026-10-23", "06:45", minutes=20)
        busy = run("2026-10-23", "06:50", minutes=0, esito="OCCUPATO", fonti=())
        merged = sorted(long_run + busy, key=lambda s: json.loads(s)["ts"])
        self.write_log(merged)
        h = self.history(at("2026-10-23", "08:00"))
        o = next(x for x in h["orari"] if x["orario"].startswith("2026-10-23T06:45"))
        self.assertEqual((o["esito"], o["ritardo_avvio_min"]), ("ok", 0))
        self.assertEqual(sorted(g["esito"] for g in h["giri"]), ["occupato", "ok"])


class Calendario(Base):
    def test_plist_assente_non_valido_non_supportato_disattivato(self):
        self.write_log(run("2026-10-23", "06:45"))
        os.remove(self.plist)
        h = self.history(at("2026-10-23", "08:00"))
        self.assertEqual((h["calendario"]["stato"], h["orari"], h["prossimo_giro"]), ("assente", [], None))
        self.assertEqual(h["giri"][0]["orario_previsto"], None)
        with open(self.plist, "wb") as f:
            f.write(b"non e' un plist")
        self.assertEqual(self.history(at("2026-10-23", "08:00"))["calendario"]["stato"], "non_valido")
        self.write_plist([{"Hour": 6, "Minute": 45, "Weekday": 1}])
        self.assertEqual(self.history(at("2026-10-23", "08:00"))["calendario"]["stato"], "non_supportato")
        self.write_plist([{"Hour": 6, "Minute": 45}], Disabled=True)
        self.assertEqual(self.history(at("2026-10-23", "08:00"))["calendario"]["stato"], "disattivato")

    def test_check_senza_orari_fissi(self):
        # 14/11: il job parte con `check` ogni 5 minuti (StartInterval), nessun orario da recuperare o da dare per perso
        self.write_log(run("2026-10-23", "06:45"))
        with open(self.plist, "wb") as f:
            plistlib.dump(dict(Label="cloud.scuola.job", RunAtLoad=True, StartInterval=300), f)
        h = self.history(at("2026-10-23", "08:00"))
        self.assertEqual((h["calendario"]["stato"], h["calendario"]["ogni_s"], h["orari"]), ("ok", 300, []))
        self.assertEqual(len(h["giri"]), 1)
        with open(self.plist, "wb") as f:
            plistlib.dump(dict(Label="cloud.scuola.job", StartInterval="5 min"), f)
        self.assertEqual(self.history(at("2026-10-23", "08:00"))["calendario"]["stato"], "non_valido")

    def test_cambio_di_giorno_e_giro_fuori_calendario(self):
        self.write_log(run("2026-10-22", "20:00") + run("2026-10-22", "23:55"))   # il secondo: all'accesso
        h = self.history(at("2026-10-23", "06:00"))
        late = next(g for g in h["giri"] if g["avvio"].startswith("2026-10-22T23:55"))
        self.assertEqual((late["orario_previsto"], late["ritardo_avvio_min"]), (None, None))   # fuori calendario
        self.assertEqual(h["prossimo_giro"], "2026-10-23T06:45:00+02:00")

    def test_ora_legale(self):
        # 25 ottobre 2026: fine dell'ora legale (03:00 -> 02:00). Lo stesso orario locale cambia istante UTC.
        s = A.slots_between([(6, 45)], at("2026-10-24", "00:00"), at("2026-10-26", "00:00"))
        self.assertEqual([x.strftime("%m-%dT%H:%M") for x in s], ["10-24T04:45", "10-25T05:45"])
        # 29 marzo 2026: 02:30 non esiste; un solo giro, spostato come l'orologio
        s = A.slots_between([(2, 30)], dt.datetime(2026, 3, 28, 22, 0, tzinfo=UTC), dt.datetime(2026, 3, 29, 22, 0, tzinfo=UTC))
        self.assertEqual(len(s), 1)
        self.write_log(run("2026-10-25", "06:45"))
        h = self.history(dt.datetime(2026, 10, 25, 6, 0, tzinfo=UTC))
        o = next(x for x in h["orari"] if x["orario"].startswith("2026-10-25T06:45"))
        self.assertEqual((o["stato"], o["orario"]), ("eseguito", "2026-10-25T06:45:00+01:00"))


VIEW_HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const el = () => ({ innerHTML: "", textContent: "", dataset: {}, setAttribute() {}, scrollIntoView() {} });
const els = {};
const ctx = { console, Intl, Math, Date, String, Number, Object, Array, JSON, RegExp, Set, location: { hash: "", protocol: "file:" },
  history: { replaceState() {}, pushState() {} }, matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {}, scrollTo() {},
  document: { documentElement: { dataset: {} }, getElementById: id => (els[id] = els[id] || el()), querySelectorAll: () => [], querySelector: () => null, addEventListener() {} } };
ctx.window = ctx; vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const C = ctx.CUORE;
C.SCHOOL_STATE.mode = "error"; C.SCHOOL_STATE.error = null;
C.SCHOOL_STATE.feed = { schema: "cuore.school.feed/1", sola_lettura_verificata: false, pagine: {}, errori: [{ ambito: "registro", messaggio: "x" }],
  automazione: { disponibile: false, storico: JSON.parse(fs.readFileSync(process.argv[3], "utf8")) } };
console.log(JSON.stringify({ home: C.viewHtml(C.parse("")), sistema: C.viewHtml(C.parse("cuore/automazioni")) }));
"""


class NelleViste(Base):
    def test_sistema_mostra_lo_storico_senza_dati_sensibili_e_la_home_no(self):
        node = shutil.which("node") or ("/usr/local/bin/node" if os.path.isfile("/usr/local/bin/node") else None)
        if not node:
            self.skipTest("node non disponibile")
        import re
        import subprocess
        self.write_log(run("2026-10-23", "06:45", retries=2) + ['{"rotta']
                       + run("2026-10-23", "20:00", esito="PARZIALE", fonti=("fallita", "ok")) + run("2026-10-24", "06:45"))
        h = self.history(at("2026-10-24", "12:00"))
        with open(os.path.join(HERE, "..", "index.html"), encoding="utf-8") as fh:
            script = re.search(r"<script>(.*?)</script>", fh.read(), re.S).group(1)
        paths = {n: os.path.join(self.dir, n) for n in ("page.js", "h.js", "storico.json")}
        for n, text in (("page.js", script), ("h.js", VIEW_HARNESS), ("storico.json", json.dumps(h))):
            with open(paths[n], "w", encoding="utf-8") as fh:
                fh.write(text)
        r = subprocess.run([node, paths["h.js"], paths["page.js"], paths["storico.json"]], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        v = json.loads(r.stdout)
        for w in ("Prossimo giro previsto", "24 ott 14:30", "Giri nelle ultime 48 ore", "1 parziale", "non verificabil",
                  "Automazione del job scuola", "Ultimi giri registrati", "non verificabile", "2 nuovi tentativi",
                  "caricamento in launchd non verificabile", "Mac spento o in stop", "Classroom fallita"):
            self.assertIn(w, v["sistema"], w)
        for w in ("Giri nelle ultime 48 ore", "Ultimi giri registrati", "launchd"):     # la Home non mostra dettagli tecnici
            self.assertNotIn(w, v["home"], w)
        for bad in ("TOKEN-SINTETICO", "cvv:1234567", "/Users/", "timed out", "undefined", "NaN", "[object"):
            self.assertNotIn(bad, v["home"] + v["sistema"], bad)


class DatoMancanteNonESuccesso(Base):
    def test_solo_orari_non_verificabili_non_sono_verdi(self):
        h = self.history(at("2026-10-24", "12:00"))                     # log assente: nessuna prova
        self.assertEqual(set(h["conteggi"]), {"non_verificabile"})
        row = self.home_row(h)
        self.assertIn("non verificabil", row)
        self.assertNotIn("chip good", row)                               # dato mancante: mai verde

    def test_colore_del_riepilogo_per_ogni_esito(self):
        """Riepilogo delle 48 ore coerente con il colore delle singole righe: verde solo con tutti gli orari OK."""
        def unknown(day, start):                                         # esito fuori vocabolario -> «sconosciuto»
            lines = run(day, start)
            last = json.loads(lines[-1])
            last.update(esito="STRANO", exit_code=9)
            return lines[:-1] + [json.dumps(last, sort_keys=True)]
        cases = [
            ("tutti ok", run("2026-10-23", "06:45") + run("2026-10-23", "14:30") + run("2026-10-23", "20:00")
             + run("2026-10-24", "06:45"), {"ok"}, "chip good"),
            ("fallito", run("2026-10-23", "06:45") + run("2026-10-23", "14:30", esito="FALLITO", fonti=("fallita", "fallita"))
             + run("2026-10-23", "20:00") + run("2026-10-24", "06:45"), {"ok", "fallito"}, "chip bad"),
            ("mancato", run("2026-10-23", "06:45") + run("2026-10-23", "20:00") + run("2026-10-24", "06:45"),
             {"ok", "mancato"}, "chip bad"),
            ("non riconosciuto", run("2026-10-23", "06:45") + unknown("2026-10-23", "14:30") + run("2026-10-23", "20:00")
             + run("2026-10-24", "06:45"), {"ok", "sconosciuto"}, "chip warn"),
            ("in corso", run("2026-10-23", "06:45") + run("2026-10-23", "14:30") + run("2026-10-23", "20:00")
             + run("2026-10-24", "06:45")[:1], {"ok", "in_corso"}, "chip \""),
        ]
        before = run("2026-10-22", "14:30") + run("2026-10-22", "20:00")    # log che copre tutta la finestra
        for name, lines, keys, cls in cases:
            with self.subTest(name):
                self.write_log(before + lines)
                h = self.history(at("2026-10-24", "07:00"))
                self.assertEqual(set(h["conteggi"]), keys, h["conteggi"])
                self.assertIn(cls, self.home_row(h))

    def home_row(self, h):
        node = shutil.which("node") or ("/usr/local/bin/node" if os.path.isfile("/usr/local/bin/node") else None)
        if not node:
            self.skipTest("node non disponibile")
        import re
        import subprocess
        with open(os.path.join(HERE, "..", "index.html"), encoding="utf-8") as fh:
            script = re.search(r"<script>(.*?)</script>", fh.read(), re.S).group(1)
        paths = {n: os.path.join(self.dir, n) for n in ("page.js", "h.js", "storico.json")}
        for n, text in (("page.js", script), ("h.js", VIEW_HARNESS), ("storico.json", json.dumps(h))):
            with open(paths[n], "w", encoding="utf-8") as fh:
                fh.write(text)
        r = subprocess.run([node, paths["h.js"], paths["page.js"], paths["storico.json"]], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        page = json.loads(r.stdout)["sistema"]
        return re.search(r"Giri nelle ultime 48 ore</div></div>(<span[^>]*>[^<]*</span>)", page).group(1)


class NelPonte(unittest.TestCase):
    def test_registry_esplicito_non_legge_i_file_reali(self):
        log, plist = CS.automation_paths("/tmp/sintetico/school/registry.sqlite3")
        self.assertEqual((log, plist), ("/tmp/sintetico/school/job/school-job.jsonl",
                                        "/tmp/sintetico/school/job/cloud.scuola.job.plist"))
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        a = CS.automation_status(os.path.join(tmp, "job"), *CS.automation_paths(os.path.join(tmp, "registry.sqlite3")))
        self.assertEqual(a["disponibile"], False)                        # nessuno stato del job
        self.assertEqual((a["storico"]["log"]["stato"], a["storico"]["calendario"]["stato"]), ("assente", "assente"))


if __name__ == "__main__":
    unittest.main()
