"""Promemoria di CUORE sul Mac (`app/bridge/promemoria.py`): solo feed e archivi SINTETICI in cartelle temporanee,
consegna finta (nessuna notifica vera), nessun percorso reale (config, stato e archivio sempre indicati per opzione).
Esecuzione: cd app && /usr/bin/python3 -B -m unittest tests.test_promemoria"""
import datetime as dt, json, os, shutil, stat, sys, tempfile, unittest
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(APP, "bridge"))
import cuore_money as CM  # noqa: E402
import finance_appunti as FA  # noqa: E402
import promemoria as P  # noqa: E402
import school_personale as SP  # noqa: E402

ROME = ZoneInfo("Europe/Rome")
NOW = dt.datetime(2026, 11, 4, 15, 0, tzinfo=ROME)
TODAY = NOW.date()
NODE = CM._node()[0]
ALL_ON = {"attivo": True, "promemoria": {k: True for k in P.KINDS}}


def read(p):
    with open(p, "rb") as f:
        return f.read()


def day(n):
    return (TODAY + dt.timedelta(days=n)).isoformat()


def block(bid, data):
    return {"id": bid, "titolo": bid, "stato": "dati", "provenienza": {}, "dati": data}


def school_feed(last_ok=None):
    return {"schema": "cuore.school.feed/1", "sola_lettura_verificata": True, "materie": {"mat": "MATEMATICA", "fis": "Fisica"},
            "automazione": {"disponibile": True, "job": {"ultimo_commit_ok": last_ok}},
            "freschezza": {"disponibile": False},
            "pagine": {
                "verifiche": {"blocchi": [block("verifiche_future", [
                    {"uid": "cvv:1", "materia": "mat", "data": day(1), "testo": "Verifica SEGRETA"},
                    {"uid": "cvv:2", "materia": "fis", "data": day(3), "testo": "x"},
                    {"uid": "cvv:3", "materia": "fis", "data": day(2), "testo": "x"},
                    {"uid": "cvv:4", "materia": "fis", "data": day(1), "testo": "x"}])]},
                "compiti": {"blocchi": [block("compiti_aperti", [
                    {"uid": "cvv:10", "materia": "mat", "scadenza": day(1), "stato_operativo": None},
                    {"uid": "cvv:11", "materia": "mat", "scadenza": day(1), "stato_operativo": None},      # segnato fatto
                    {"uid": "cls:12", "materia": "fis", "scadenza": day(1), "stato_operativo": "consegnato"},
                    {"uid": "cvv:13", "materia": "fis", "scadenza": day(2), "stato_operativo": None}])]},
                "assenze": {"blocchi": [block("eventi", [
                    {"uid": "cvv:20", "data": "2026-10-22", "tipo": "Assenza", "giustificata": False,
                     "stato_giustificazione": "Da giustificare"},
                    {"uid": "cvv:21", "data": "2026-10-23", "tipo": "Assenza", "giustificata": False,
                     "stato_giustificazione": "In approvazione"},
                    {"uid": "cvv:22", "data": "2026-10-24", "tipo": "Assenza", "giustificata": True,
                     "stato_giustificazione": "Da giustificare"}])]}}}


def env(data):
    return {"ok": True, "contractVersion": 1, "data": data}


def line(target, budget, spent, scope="category"):
    return {"scope": scope, "target": target, "budgetMinor": budget, "spentMinor": spent, "remainingMinor": budget - spent,
            "overBudget": spent > budget, "budgetId": "b-" + str(target)}


def money_feed(last="2026-10-22", review=7, verified=True):
    return {"schema": CM.SCHEMA, "sola_lettura_verificata": verified, "aggiornamento": {"ultimo_movimento": last},
            "operazioni": {
                "budgets": env({"month": "2026-11", "currencies": [{"currency": "EUR", "month": "2026-11", "lines": [
                    line("bar", 6000, 5220), line("spesa", 10000, 12345), line("svago", 10000, 5000), line("zero", 0, 100),
                    line(None, 50000, 43000, "total")]}]}),
                "categories": env({"items": [{"id": "bar", "name": "Bar SEGRETO", "parentId": None, "budgetGroup": "wants"},
                                             {"id": "spesa", "name": "Spesa", "parentId": None, "budgetGroup": "needs"}]}),
                "data_status": env({"accounts": [], "anomalies": [], "openReviewItems": {"classification_needed": review}})}}


class Configurazione(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cuore-promemoria-test-")
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, doc):
        p = os.path.join(self.dir, "promemoria.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(doc if isinstance(doc, str) else json.dumps(doc))
        return p

    def test_spento_senza_file_con_attivo_falso_o_file_non_valido(self):
        self.assertIsNone(P.load_config(os.path.join(self.dir, "assente.json"))[0])
        self.assertIsNone(P.load_config(self.write({"promemoria": {"sch_verifica": True}}))[0])
        self.assertIsNone(P.load_config(self.write({"attivo": "si"}))[0])
        cfg, why = P.load_config(self.write("{non json"))
        self.assertIsNone(cfg)
        self.assertIn("non valida", why)
        for bad in ({"attivo": True, "altro": 1}, {"attivo": True, "promemoria": {"sconosciuto": True}},
                    {"attivo": True, "soglie": {"budget_percento": 120}}, {"attivo": True, "soglie": {"verifica_giorni": []}}):
            self.assertIsNone(P.load_config(self.write(bad))[0], bad)

    def test_solo_i_promemoria_elencati_sono_accesi(self):
        cfg, why = P.load_config(self.write({"attivo": True, "promemoria": {"sch_verifica": True, "fin_dati": False},
                                             "soglie": {"budget_percento": 90}}))
        self.assertIsNone(why)
        self.assertEqual([k for k, v in cfg["on"].items() if v], ["sch_verifica"])
        self.assertEqual(cfg["soglie"]["budget_percento"], 90)
        self.assertEqual(cfg["soglie"]["verifica_giorni"], [1, 3])

    def test_esempio_del_repository_valido(self):
        cfg, why = P.load_config(os.path.join(APP, "launchd", "promemoria.esempio.json"))
        self.assertIsNone(why)
        self.assertEqual({k for k, v in cfg["on"].items() if not v}, {"sch_compiti_sera", "fin_settimana"})   # proposte spente
        self.assertEqual((cfg["momenti"]["fin_contanti"], cfg["momenti"]["sch_impegni"]), (["sera"], ["sera"]))
        self.assertTrue(cfg["riservato"])

    def test_momenti_per_promemoria(self):
        n = P.normalize({"attivo": True, "promemoria": {"sch_voti": {"attivo": True, "momenti": ["sera", "mattina"]},
                                                        "fin_budget": True}})
        self.assertEqual(n["promemoria"]["sch_voti"], {"attivo": True, "momenti": ["mattina", "sera"]})
        self.assertEqual(n["promemoria"]["fin_budget"]["momenti"], ["mattina", "pomeriggio", "sera"])
        self.assertEqual(n["promemoria"]["fin_dati"], {"attivo": False, "momenti": ["mattina", "pomeriggio", "sera"]})
        for bad in ({"fin_contanti": {"attivo": True, "momenti": ["mattina"]}},       # quelli serali solo la sera
                    {"sch_voti": {"attivo": True, "momenti": []}}, {"sch_voti": {"attivo": True, "momenti": ["notte"]}},
                    {"sch_voti": {"attivo": "si"}}, {"sch_voti": {"acceso": True}}):
            with self.assertRaises(P.ConfigNonValida, msg=bad):
                P.normalize({"attivo": True, "promemoria": bad})
        with self.assertRaises(P.ConfigNonValida):
            P.normalize({"attivo": True, "riservato": "no"})


class Decisione(unittest.TestCase):
    cfg = P.parse_config(ALL_ON)[0]

    def keys(self, rems):
        return sorted(r["chiave"] for r in rems)

    def test_school(self):
        rems = P.school_reminders(school_feed(), self.cfg, TODAY, {"cvv:11"}, NOW)
        self.assertEqual(self.keys(rems), sorted([
            "school:verifica:cvv:1:1", "school:verifica:cvv:4:1", "school:verifica:cvv:2:3",
            "school:compito:cvv:10:%s" % day(1), "school:compito-sera:cvv:10:%s" % day(1),
            "school:assenza:cvv:20:2026-W45-lun"]))
        texts = {r["chiave"]: r["testo"] for r in rems}
        self.assertEqual(texts["school:verifica:cvv:1:1"], "Verifica di Matematica domani")
        self.assertEqual(texts["school:assenza:cvv:20:2026-W45-lun"], "Assenza del 22/10 da giustificare")

    def test_giustificazioni_due_volte_a_settimana(self):
        halves = [P.half_week(dt.date(2026, 11, d)) for d in (2, 3, 4, 5, 8, 9)]      # lun mar mer gio dom lun
        self.assertEqual(halves, ["2026-W45-lun"] * 3 + ["2026-W45-gio"] * 2 + ["2026-W46-lun"])

    def test_school_dati_vecchi(self):
        old = (NOW - dt.timedelta(hours=30)).isoformat()
        rems = P.school_reminders(school_feed(old), self.cfg, TODAY, set(), NOW)
        stale = [r for r in rems if r["tipo"] == "sch_dati"]
        self.assertEqual([r["chiave"] for r in stale], ["school:dati-vecchi:" + old])
        self.assertIn("30 ore", stale[0]["testo"])
        recent = (NOW - dt.timedelta(hours=10)).isoformat()
        self.assertFalse([r for r in P.school_reminders(school_feed(recent), self.cfg, TODAY, set(), NOW) if r["tipo"] == "sch_dati"])
        self.assertFalse([r for r in P.school_reminders(school_feed(None), self.cfg, TODAY, set(), NOW) if r["tipo"] == "sch_dati"])

    def test_finance(self):
        rems = P.finance_reminders(money_feed(), self.cfg, TODAY)
        self.assertEqual(self.keys(rems), sorted([
            "finance:budget:2026-11:category:bar:EUR:soglia", "finance:budget:2026-11:category:spesa:EUR:superato",
            "finance:budget:2026-11:total:totale:EUR:soglia", "finance:dati-vecchi:2026-10-22:2026-W45",
            "finance:da-classificare:2026-W45"]))
        texts = {r["chiave"]: r["testo"] for r in rems}
        self.assertEqual(texts["finance:budget:2026-11:category:bar:EUR:soglia"], "Budget Bar SEGRETO al 87%: 52,20 € su 60,00 €")
        self.assertEqual(texts["finance:dati-vecchi:2026-10-22:2026-W45"],
                         "Dati Finance fermi al 22/10 (13 giorni fa): servono estratti nuovi")
        self.assertEqual(P.finance_reminders(money_feed(last=day(-3), review=0), self.cfg, TODAY)[-1]["tipo"], "fin_budget")
        self.assertEqual(P.finance_reminders(money_feed(verified=False), self.cfg, TODAY), [])

    def test_testi_riservati_senza_importi_ne_dettagli(self):
        rems = P.school_reminders(school_feed(), self.cfg, TODAY, set(), NOW) + P.finance_reminders(money_feed(), self.cfg, TODAY)
        notes = P.group(rems, reserved=True)
        blob = " ".join(t for _, t, _ in notes)
        for secret in ("€", "SEGRET", "Matematica", "Fisica", "52,20", "22/10 da"):
            self.assertNotIn(secret, blob)
        by_text = {t: k for _, t, k in notes}
        self.assertEqual(len(by_text["Domani ci sono 2 verifiche"]), 2)
        self.assertIn("Un budget del mese è stato superato", by_text)
        self.assertEqual(len(by_text["2 budget del mese hanno passato l'85%"]), 2)
        detailed = [t for _, t, _ in P.group(rems, reserved=False)]
        self.assertIn("Domani ci sono 2 verifiche: Matematica, Fisica", detailed)
        self.assertEqual(sum(len(k) for _, _, k in notes), len(rems))


class Esecuzione(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="cuore-promemoria-test-"))
        self.addCleanup(shutil.rmtree, self.dir)
        self.state = os.path.join(self.dir, "CUORE", "promemoria-stato.json")
        self.pers = os.path.join(self.dir, "personale.jsonl")
        SP.aggiungi(self.pers, {"tipo": "compito", "uid": "cvv:11", "fatto": True})
        self.sent, self.lines = [], []

    def config(self, doc):
        p = os.path.join(self.dir, "promemoria.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        return p

    def run_cmd(self, cmd, config, notify=None, school=None, money=None, extra=()):
        def fake(title, text):
            self.sent.append((title, text))
            return True
        return P.run([cmd, "--config", config, "--stato", self.state, "--school-personale", self.pers,
                      "--oggi", TODAY.isoformat()] + list(extra),
                     school_fn=school or (lambda: school_feed()), money_fn=money or (lambda: money_feed()),
                     notify=notify or fake, out=self.lines.append)

    def test_spento_nessuna_lettura_nessuna_notifica(self):
        def boom():
            raise AssertionError("fonte letta da spento")
        self.assertEqual(self.run_cmd("esegui", os.path.join(self.dir, "assente.json"), school=boom, money=boom), 0)
        self.assertEqual(self.sent, [])
        self.assertFalse(os.path.exists(self.state))
        self.assertIn("spento", self.lines[-1])

    def test_una_volta_sola_e_stato_privato(self):
        before = read(self.pers)
        cfg = self.config(ALL_ON)
        self.assertEqual(self.run_cmd("esegui", cfg), 0)
        first = len(self.sent)
        self.assertEqual(first, 8)                     # 2 gruppi di verifiche, compito, assenza, 3 budget, dati, classificare = 8
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.state)).st_mode), 0o700)
        self.assertEqual(self.run_cmd("esegui", cfg), 0)
        self.assertEqual(len(self.sent), first)       # niente si ripete
        self.assertEqual(read(self.pers), before)   # archivio personale mai scritto
        summary = json.loads(self.lines[-1])
        self.assertEqual((summary["nuovi"], summary["notifiche"]), (0, 0))
        self.assertNotIn("€", "".join(self.lines))

    def test_consegna_fallita_non_segnata(self):
        cfg = self.config({"attivo": True, "promemoria": {"sch_verifica": True}})
        self.assertEqual(self.run_cmd("esegui", cfg, notify=lambda t, x: False), 1)
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.run_cmd("esegui", cfg), 0)
        self.assertEqual(len(self.sent), 2)

    def test_prova_non_notifica_e_non_scrive(self):
        self.assertEqual(self.run_cmd("prova", os.path.join(self.dir, "assente.json"), notify=lambda t, x: 1 / 0), 0)
        self.assertFalse(os.path.exists(self.state))
        out = "\n".join(self.lines)
        self.assertIn("anteprima", out)
        self.assertIn("Notifiche che partirebbero: 8", out)
        self.assertNotIn("€", out)

    def test_stato_in_icloud_o_nel_repository_rifiutato(self):
        for bad in (os.path.join(P.REPO_ROOT, "x.json"),
                    os.path.join(os.path.expanduser("~"), "Library", "Mobile Documents", "x.json"), "relativo.json"):
            self.lines.clear()
            self.assertEqual(P.run(["esegui", "--config", self.config(ALL_ON), "--stato", bad], notify=lambda t, x: 1 / 0,
                                   school_fn=lambda: 1 / 0, money_fn=lambda: 1 / 0, out=self.lines.append), 2)

    def test_notifica_del_mac_testo_come_argomento(self):
        calls = []

        class R:
            returncode = 0
        ok = P.notify_mac("CUORE", 'testo "con" virgolette', run=lambda cmd, **k: calls.append(cmd) or R())
        self.assertTrue(ok)
        cmd = calls[0]
        self.assertEqual(cmd[0], "/usr/bin/osascript")
        self.assertEqual(cmd[-2:], ["CUORE", 'testo "con" virgolette'])
        self.assertFalse(any("virgolette" in c for c in cmd[:-1]))


def with_news(feed, grades=(), comms=()):
    """Feed School con la pagina dei voti e il blocco «novita'» della Home (dati sintetici)."""
    feed = json.loads(json.dumps(feed))
    serie = [{"data": "2026-10-01", "uid": u, "valore": 7.25} for u in grades]
    feed["pagine"]["voti"] = {"blocchi": [block("complessivo", {"serie": serie, "esclusi": {"escluso_annullato": ["cvv:a"]}})]}
    rows = [{"fonte": f, "kind": k, "uid_fonte": u, "evento": "create"} for f, k, u in comms]
    news = block("novita", {"ultimi": rows + [{"fonte": "classeviva", "kind": "grade", "uid_fonte": "x", "evento": "create"}]})
    news["stato"] = "dati" if rows else "vuoto_valido"
    feed["pagine"]["home"] = {"blocchi": [news]}
    return feed


class Momenti(unittest.TestCase):
    """Tre orari, promemoria della sera, giustificazioni, voti e comunicazioni nuovi, impegni, appunti."""

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="cuore-promemoria-test-"))
        self.addCleanup(shutil.rmtree, self.dir)
        self.state = os.path.join(self.dir, "CUORE", "promemoria-stato.json")
        self.pers = os.path.join(self.dir, "personale.jsonl")
        self.notes = os.path.join(self.dir, "appunti.jsonl")
        self.sent, self.lines, self.feed = [], [], school_feed()

    def config(self, on):
        p = os.path.join(self.dir, "promemoria.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"attivo": True, "promemoria": on}, f)
        return p

    def run_at(self, on, date=TODAY, hour="12:00", cmd="esegui"):
        fake = lambda title, text: self.sent.append((title, text)) or True
        return P.run([cmd, "--config", self.config(on), "--stato", self.state, "--school-personale", self.pers,
                      "--finance-appunti", self.notes, "--oggi", date.isoformat(), "--ora", hour],
                     school_fn=lambda: self.feed, money_fn=lambda: money_feed(), notify=fake, out=self.lines.append)

    def texts(self):
        out = [t for _, t in self.sent]
        self.sent.clear()
        return out

    def test_momento_dell_esecuzione(self):
        at = lambda h: P.moment_of(dt.datetime.combine(TODAY, dt.time.fromisoformat(h), ROME))
        self.assertEqual([at(h) for h in ("06:00", "07:15", "14:59", "15:00", "19:00", "23:30")],
                         [None, "mattina", "mattina", "pomeriggio", "sera", "sera"])

    def test_prima_delle_7_nulla_e_nessuna_fonte(self):
        boom = lambda: 1 / 0
        P.run(["esegui", "--config", self.config({"sch_verifica": True}), "--stato", self.state, "--oggi", TODAY.isoformat(),
               "--ora", "06:30"], school_fn=boom, money_fn=boom, notify=lambda t, x: 1 / 0, out=self.lines.append)
        self.assertEqual(json.loads(self.lines[-1])["notifiche"], 0)

    def test_contanti_solo_la_sera_una_volta_al_giorno(self):
        on = {"fin_contanti": True}
        self.run_at(on, hour="15:00")
        self.assertEqual(self.texts(), [])
        self.run_at(on, hour="19:00")
        self.run_at(on, hour="21:00")
        self.assertEqual(self.texts(), ["Segna le spese in contanti di oggi"])
        self.run_at(on, date=TODAY + dt.timedelta(days=1), hour="19:00")
        self.assertEqual(self.texts(), ["Segna le spese in contanti di oggi"])

    def test_giustificazioni_lunedi_e_giovedi_finche_restano(self):
        on = {"sch_assenze": True}
        for d, expected in ((2, 1), (3, 0), (4, 0), (5, 1), (8, 0), (9, 1)):           # lun mar mer gio dom lun
            self.run_at(on, date=dt.date(2026, 11, d), hour="07:15")
            self.assertEqual(len(self.texts()), expected, d)
        self.feed["pagine"]["assenze"]["blocchi"][0]["dati"][0]["stato_giustificazione"] = "Giustificata"
        self.run_at(on, date=dt.date(2026, 11, 12), hour="07:15")
        self.assertEqual(self.texts(), [])                                               # giustificata: basta

    def test_voti_e_comunicazioni_solo_i_nuovi(self):
        on = {"sch_voti": True, "sch_comunicazioni": True}
        self.feed = with_news(school_feed(), grades=["cvv:g1", "cvv:g2"], comms=[("classeviva", "notice", "n1")])
        self.run_at(on)
        self.assertEqual(self.texts(), [])                                               # prima lettura: nessuna valanga
        self.assertEqual(json.loads(self.lines[-1])["prima_lettura"], 3)
        self.feed = with_news(school_feed(), grades=["cvv:g1", "cvv:g2", "cvv:g3"],
                              comms=[("classeviva", "notice", "n1"), ("classroom", "announcement", "a1")])
        self.run_at(on, hour="15:00")
        self.assertEqual(sorted(self.texts()), ["C'è una comunicazione nuova", "È arrivato un voto nuovo"])
        self.run_at(on, hour="19:00")
        self.assertEqual(self.texts(), [])
        self.run_at({"sch_comunicazioni": True}, hour="19:30")                           # voti spenti: prima lettura tolta
        self.feed = with_news(school_feed(), grades=["cvv:g1", "cvv:g2", "cvv:g3", "cvv:g4"])
        self.run_at(on, date=TODAY + dt.timedelta(days=1))
        self.assertEqual(self.texts(), [])                                               # riacceso: nessun arretrato
        self.feed = with_news(school_feed(), grades=["cvv:g1", "cvv:g2", "cvv:g3", "cvv:g4", "cvv:g5", "cvv:g6"])
        self.run_at(on, date=TODAY + dt.timedelta(days=1), hour="15:00")
        self.assertEqual(self.texts(), ["Sono arrivati 2 voti nuovi"])

    def test_voti_fonte_non_letta_nessuna_prima_lettura(self):
        self.run_at({"sch_voti": True})                                                  # feed senza pagina dei voti
        self.assertFalse(os.path.exists(self.state))
        self.feed = with_news(school_feed(), grades=["cvv:g1"])
        self.run_at({"sch_voti": True}, hour="15:00")
        self.assertEqual(self.texts(), [])

    def test_impegni_personali_di_domani_la_sera(self):
        SP.aggiungi(self.pers, {"tipo": "impegno", "testo": "Ripetizioni SEGRETE", "data": day(1)})
        gone = SP.aggiungi(self.pers, {"tipo": "impegno", "testo": "annullato", "data": day(1)})
        SP.aggiungi(self.pers, {"tipo": "annulla", "id": gone["id"]})
        SP.aggiungi(self.pers, {"tipo": "impegno", "testo": "dopodomani", "data": day(2)})
        self.run_at({"sch_impegni": True})
        self.assertEqual(self.texts(), [])
        self.run_at({"sch_impegni": True}, hour="19:00")
        self.assertEqual(self.texts(), ["Domani hai un impegno personale"])              # riservato: senza il testo

    def test_appunti_con_promemoria_arrivato(self):
        now = "%sT11:00" % TODAY.isoformat()
        FA.aggiungi(self.notes, {"tipo": "crea", "testo": "Comprare il libro SEGRETO", "quando": now})
        FA.aggiungi(self.notes, {"tipo": "crea", "testo": "senza promemoria", "quando": now, "promemoria": False})
        FA.aggiungi(self.notes, {"tipo": "crea", "testo": "piu' tardi", "quando": "%sT16:00" % TODAY.isoformat()})
        FA.aggiungi(self.notes, {"tipo": "crea", "testo": "solo appunto"})
        done = FA.aggiungi(self.notes, {"tipo": "crea", "testo": "fatto", "quando": TODAY.isoformat()})
        FA.aggiungi(self.notes, {"tipo": "completa", "id": done["id"]})
        self.run_at({"fin_appunti": True})
        self.assertEqual(self.texts(), ["Hai un appunto da ricordare"])
        self.run_at({"fin_appunti": True}, hour="19:00")
        self.assertEqual(self.texts(), ["Hai un appunto da ricordare"])                  # quello delle 16:00
        self.run_at({"fin_appunti": True}, hour="20:00")
        self.assertEqual(self.texts(), [])

    def test_proposte_spente_e_domenica_sera(self):
        on = {"fin_settimana": True, "sch_compiti_sera": True}
        SP.aggiungi(self.pers, {"tipo": "compito", "uid": "cvv:11", "fatto": True})
        self.run_at(on, hour="19:00")                                                    # mercoledi'
        self.assertEqual(self.texts(), ["Hai ancora un compito per domani da segnare fatto"])
        self.run_at(on, date=dt.date(2026, 11, 8), hour="19:00")                         # domenica
        self.assertEqual(self.texts(), ["Dai un'occhiata alle spese della settimana"])

    def test_prova_dice_la_prima_lettura(self):
        self.feed = with_news(school_feed(), grades=["cvv:g1"])
        self.run_at({"sch_voti": True}, cmd="prova")
        self.assertIn("Prima lettura di voti/comunicazioni: 1", "\n".join(self.lines))
        self.assertFalse(os.path.exists(self.state))


class Impostazioni(unittest.TestCase):
    """Le impostazioni scritte dal ponte: un solo file, scrittura atomica 0600, stato per l'interfaccia."""

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="cuore-promemoria-test-"))
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "CUORE", "promemoria.json")

    def test_senza_file_modello_spento(self):
        doc = P.settings_status(self.path, NOW)
        self.assertEqual((doc["file"], doc["attivo"], doc["momenti"]["sera"]), ("assente", False, "19:00"))
        rows = {r["chiave"]: r for r in doc["promemoria"]}
        self.assertEqual(set(rows), set(P.KINDS))
        self.assertFalse(any(r["acceso"] for r in rows.values()))                  # tutti spenti, anche uno per uno
        self.assertTrue(rows["fin_settimana"]["proposta"] and not rows["sch_voti"]["proposta"])
        self.assertTrue(all(r["prossimo_controllo"] is None for r in rows.values()))
        self.assertFalse(os.path.exists(self.path))

    def test_modifica_parziale_e_prossimo_controllo(self):
        doc = P.update_settings(self.path, {"attivo": True, "promemoria": {"sch_voti": {"attivo": True, "momenti": ["sera"]},
                                                                           "fin_settimana": True, "fin_dati": False}}, NOW)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode), 0o700)
        rows = {r["chiave"]: r for r in doc["promemoria"]}
        self.assertEqual(rows["sch_voti"]["prossimo_controllo"], "2026-11-04T19:00:00+01:00")
        self.assertIsNone(rows["sch_verifica"]["prossimo_controllo"])            # mai acceso: resta spento
        self.assertEqual(rows["fin_settimana"]["prossimo_controllo"], "2026-11-08T19:00:00+01:00")
        self.assertIsNone(rows["fin_dati"]["prossimo_controllo"])
        cfg, why = P.load_config(self.path)                          # il motore legge lo stesso file
        self.assertIsNone(why)
        self.assertEqual((cfg["on"]["fin_settimana"], cfg["on"]["fin_dati"], cfg["momenti"]["sch_voti"]), (True, False, ["sera"]))
        P.update_settings(self.path, {"attivo": False}, NOW)
        self.assertEqual(P.load_config(self.path)[0], None)
        self.assertTrue(P.read_settings(self.path)[0]["promemoria"]["fin_settimana"]["attivo"])   # resta salvato da spento

    def test_modifiche_non_valide_rifiutate_file_invariato(self):
        P.update_settings(self.path, {"attivo": True}, NOW)
        before = read(self.path)
        for bad in ({}, {"altro": 1}, {"promemoria": {"ignoto": True}}, {"promemoria": {"fin_contanti": {"momenti": ["mattina"]}}},
                    {"soglie": {"budget_percento": 10}}, {"attivo": "si"}, {"promemoria": {"sch_voti": {}}}):
            with self.assertRaises(P.ConfigNonValida, msg=bad):
                P.update_settings(self.path, bad, NOW)
        self.assertEqual(read(self.path), before)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{rotto")
        self.assertEqual(P.settings_status(self.path, NOW)["file"], "non_valido")
        with self.assertRaises(P.ConfigNonValida):
            P.update_settings(self.path, {"attivo": True}, NOW)

    def test_invio_previsto_di_un_appunto(self):
        s = P.normalize({"attivo": True, "promemoria": {"fin_appunti": True}})
        self.assertEqual(P.appunto_send(s, "2026-11-05T10:00", NOW).isoformat(), "2026-11-05T15:00:00+01:00")
        self.assertEqual(P.appunto_send(s, "2026-11-05", NOW).isoformat(), "2026-11-05T07:15:00+01:00")
        self.assertEqual(P.appunto_send(s, "2026-11-05T20:00", NOW).isoformat(), "2026-11-06T07:15:00+01:00")
        self.assertIsNone(P.appunto_send(P.normalize({"attivo": True}), "2026-11-05", NOW))


class FeedSintetici(unittest.TestCase):
    """I promemoria sui feed veri del ponte, costruiti da dati SINTETICI (forma dei dati controllata dal contratto)."""

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="cuore-promemoria-test-"))
        self.addCleanup(shutil.rmtree, self.dir)
        self.cfg = P.parse_config(ALL_ON)[0]

    def test_feed_school_sintetico(self):
        import prova
        feed = prova.school_feed(self.dir)
        now = dt.datetime.fromisoformat(feed["pagine"]["verifiche"]["riferimento_temporale"]["now"])
        rems = P.school_reminders(feed, self.cfg, now.date(), set(), now)
        self.assertTrue(any(r["chiave"].startswith("school:verifica:") for r in rems))

    @unittest.skipUnless(NODE, "node non disponibile")
    def test_feed_money_sintetico(self):
        import prova
        import prova_guard as G
        code, doc = prova.run_node(G.create_command(NODE, os.path.join(self.dir, "s")))
        self.assertEqual(code, 0, doc)
        feed = prova.money_feed(os.path.join(self.dir, "s"), NODE, today=TODAY.isoformat())
        self.assertTrue(feed["sola_lettura_verificata"], feed["errori"])
        kinds = {r["tipo"] for r in P.finance_reminders(feed, self.cfg, TODAY)}
        self.assertTrue({"fin_dati", "fin_classificare"} <= kinds, kinds)


if __name__ == "__main__":
    unittest.main()
