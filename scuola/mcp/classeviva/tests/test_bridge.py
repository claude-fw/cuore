"""Test del bridge con un registro FINTO (mock locale, dati fittizi). Non sostituiscono i test sul registro reale."""
import datetime as dt, json, os, subprocess, sys, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "..", "_common"))

TODAY = dt.date.today()
def d(n): return (TODAY + dt.timedelta(days=n)).isoformat()
def ev(i, code, text, who, n, subj=None, full=False):
    return {"evtId": i, "evtCode": code, "notes": text, "subjectDesc": subj, "authorName": who, "classDesc": "4^X",
            "evtDatetimeBegin": d(n) + "T08:00:00+02:00", "evtDatetimeEnd": d(n) + "T09:00:00+02:00", "isFullDay": full}

SUBJECTS = [
    {"id": 1, "description": "LINGUA E LETTERATURA ITALIANA", "teachers": [{"teacherName": "ROSSI ANNA"}]},
    {"id": 2, "description": "LINGUA E CULTURA LATINA", "teachers": [{"teacherName": "ROSSI ANNA"}]},
    {"id": 3, "description": "STORIA", "teachers": [{"teacherName": "VERDI PAOLO"}]},
    {"id": 4, "description": "FILOSOFIA", "teachers": [{"teacherName": "VERDI PAOLO"}]},
    {"id": 5, "description": "MATEMATICA", "teachers": [{"teacherName": "NERI SARA"}]},
    {"id": 6, "description": "FISICA", "teachers": [{"teacherName": "NERI SARA"}]},
    {"id": 7, "description": "LINGUA E CULTURA STRANIERA 1", "teachers": [{"teacherName": "BIANCHI LUCA"}]},
]
def fresh_state():
    return {"agenda": [
        ev(1001, "AGNT", "Verifica di filosofia: Platone", "VERDI PAOLO", 5),
        ev(1002, "AGHW", "Esercizi pag. 40  n. 1-5", "NERI SARA", 1, subj="MATEMATICA", full=True),
        ev(1003, "AGEV", "Gita", "BIANCHI LUCA", 9, full=True),
        ev(1004, "AGNT", "Interrogazione", "ROSSI ANNA", 40),
        ev(1005, "AGNT", "Verifica su Ariosto", "ROSSI ANNA", 12),
        ev(1006, "AGHW", "Studiare pag. 10", "VERDI PAOLO", 2),
    ], "code_endpoint": True, "grades": [],
       "lessons": [{"evtId": 1, "evtDate": d(-1), "evtCode": "LSF0", "evtHPos": 2, "evtDuration": 1, "authorName": "NERI SARA",
                    "subjectDesc": "FISICA", "lessonType": "Lezione", "lessonArg": "Calore"},
                   {"evtId": 2, "evtDate": d(-1), "evtCode": "LSF0", "evtHPos": 3, "evtDuration": 1, "authorName": "NERI SARA",
                    "subjectDesc": "MATEMATICA", "lessonType": "Lezione", "lessonArg": "Equazioni"}]}
STATE = fresh_state()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = json.loads(self.rfile.read(n))
        if body.get("pass") == "ok" and self.headers.get("Z-Dev-Apikey"):
            self._send(200, {"ident": "S1234567X", "firstName": "MOCK", "token": "T", "expire": "2099"})
        else:
            self._send(422, {"info": "auth fallita"})
    def do_GET(self):
        if self.headers.get("Z-Auth-Token") != "T": return self._send(401, {})
        p = self.path.split("/students/1234567/")[-1].split("/")
        if p[0] == "agenda":
            code, b, e = p[1], p[2], p[3]
            vr = STATE.get("valid_range")
            if vr and not (vr[0] <= b <= e <= vr[1] and b <= e):
                return self._send(404, {"statusCode": 404, "error": "122:CvvRestApi/invalid date range",
                                        "message": "dates must be beween %s and %s; first date be NOT greater than second" % vr})
            if code != "all" and not STATE["code_endpoint"]: return self._send(404, {"info": "no"})
            sel = [x for x in STATE["agenda"] if b <= x["evtDatetimeBegin"][:10].replace("-", "") <= e and code in ("all", x["evtCode"])]
            return self._send(200, {"agenda": sel})
        if p[0] == "subjects": return self._send(200, {"subjects": SUBJECTS})
        if p[0] == "grades":
            g = STATE["grades"]; return self._send(200, {} if g is None else {"grades": g})
        if p[0] == "absences": return self._send(200, {"events": [{"evtId": 555, "evtCode": "ABR0", "evtDate": d(-2), "evtHPos": 2, "evtValue": 1, "hoursAbsence": 0, "isJustified": False, "justifReasonCode": None, "justifReasonDesc": None, "webJustifStatus": "N", "nuovoCampo": 7}]})
        if p[0] == "noticeboard": return self._send(200, {"items": STATE.get("notices", [])})
        if p[0] == "periods": return self._send(200, {"periods": [{"periodDesc": "Primo periodo", "dateStart": d(-30), "dateEnd": d(60), "isFinal": False}]})
        if p[0] == "lessons": return self._send(200, {"lessons": STATE["lessons"]})
        self._send(404, {})

def start_mock():
    s = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, "http://127.0.0.1:%d" % s.server_port

BASE = None
def setUpModule():
    global BASE
    _, BASE = start_mock()
    os.environ.update(CLASSEVIVA_BASE=BASE, CLASSEVIVA_USER="S1234567X", CLASSEVIVA_PASS="ok")

def srv():
    import server
    server._client = None; server._subjects = None
    return server

class Client(unittest.TestCase):
    def setUp(self):
        global STATE; STATE.clear(); STATE.update(fresh_state())
        import cvv_client as cv; self.cv = cv
    def test_login_and_student_id(self):
        c = self.cv.Client("S1234567X", "ok"); c.login()
        self.assertEqual(c.student_id, "1234567"); self.assertEqual(c.first_name, "MOCK")
    def test_login_failure_hides_password(self):
        with self.assertRaises(self.cv.CVError) as cm: self.cv.Client("S1234567X", "segretissima").login()
        self.assertNotIn("segretissima", str(cm.exception))
    def test_classify(self):
        c = self.cv.Client("S1234567X", "ok")
        raw = c.agenda(TODAY, TODAY + dt.timedelta(days=60))
        items = [self.cv.normalize_event(e) for e in raw]
        kinds = {i["uid"]: i["kind"] for i in items}
        self.assertEqual((kinds["cvv:1001"], kinds["cvv:1002"], kinds["cvv:1003"], kinds["cvv:1004"]),
                         ("verifica", "compito", "evento", "verifica"))
        self.assertEqual(len(items), 6)   # finestre da 31 gg unite senza duplicati
        # normalize_event non assume mai una materia: riporta solo quella data dall'API
        self.assertEqual([i["subject_api"] for i in items],
                         [((e.get("subjectDesc") or "").strip() or None) for e in raw])
    def test_strict_list_missing_key_is_error_not_zero(self):
        c = self.cv.Client("S1234567X", "ok")
        STATE["grades"] = None   # risposta SENZA la chiave 'grades'
        with self.assertRaises(self.cv.CVError) as cm: c.grades()
        self.assertIn("chiave 'grades' assente", str(cm.exception))
        STATE["grades"] = []     # risposta valida ma vuota
        self.assertEqual(c.grades(), [])
    def test_agenda_by_code_endpoint(self):
        c = self.cv.Client("S1234567X", "ok")
        hw = c.agenda(TODAY, TODAY + dt.timedelta(days=10), code="AGHW")
        self.assertEqual(sorted(x["evtId"] for x in hw), [1002, 1006])
    def test_hash_changes_when_subject_resolution_changes(self):
        subs = srv().subjects()
        it = self.cv.normalize_event(STATE["agenda"][0]); h0 = it["hash"]
        self.cv.enrich_event(it, subs)
        self.assertEqual(it["subject"], "FILOSOFIA"); self.assertNotEqual(it["hash"], h0)

class Tools(unittest.TestCase):
    def setUp(self):
        global STATE; STATE.clear(); STATE.update(fresh_state()); self.s = srv()
    def test_subject_resolution_exposed_with_criterion(self):
        items = {i["uid"]: i for i in self.s.cv_agenda(d(0), d(60))["items"]}
        self.assertEqual(items["cvv:1001"]["subject"], "FILOSOFIA"); self.assertEqual(items["cvv:1001"]["subject_criterion"], "testo:nome-materia")
        self.assertEqual(items["cvv:1005"]["subject"], "LINGUA E LETTERATURA ITALIANA"); self.assertEqual(items["cvv:1005"]["subject_confidence"], "media")
        self.assertIsNone(items["cvv:1004"]["subject"]); self.assertEqual(items["cvv:1004"]["subject_label"], "Materia da verificare")
        self.assertEqual(items["cvv:1002"]["subject_criterion"], "api:subjectDesc")
        self.assertEqual(items["cvv:1003"]["subject"], "LINGUA E CULTURA STRANIERA 1")   # docente unico, media
    def test_tests_filter_does_not_guess(self):
        r = self.s.cv_tests(d(0), d(60), "latino")
        self.assertEqual(r["count"], 0); self.assertEqual([i["uid"] for i in r["da_verificare"]], ["cvv:1004"])
        r = self.s.cv_tests(d(0), d(60), "italiano")
        self.assertEqual([i["uid"] for i in r["items"]], ["cvv:1005"]); self.assertEqual([i["uid"] for i in r["da_verificare"]], ["cvv:1004"])
    def test_next_test(self):
        self.assertEqual(self.s.cv_next_test("filosofia")["next"]["uid"], "cvv:1001")
        self.assertIsNone(self.s.cv_next_test("fisica")["next"])
    def test_homework_double_source(self):
        r = self.s.cv_homework(d(0), d(10))
        self.assertEqual(r["count"], 2); self.assertEqual(r["fonti"], {"agenda_completa_AGHW": 2, "endpoint_per_codice_AGHW": 2})
        hw = {i["uid"]: i for i in r["items"]}
        self.assertEqual(hw["cvv:1006"]["subject_label"], "Materia da verificare")   # Verdi: storia/filosofia, nessun indizio
        self.assertEqual([i["uid"] for i in self.s.cv_homework(d(0), d(10), "matematica")["items"]], ["cvv:1002"])
    def test_homework_survives_code_endpoint_failure(self):
        STATE["code_endpoint"] = False
        r = self.s.cv_homework(d(0), d(10))
        self.assertEqual(r["count"], 2); self.assertTrue(r["note"]); self.assertEqual(r["fonti"]["endpoint_per_codice_AGHW"], 0)
    def test_grades_empty_vs_missing(self):
        self.assertEqual(self.s.cv_grades()["count"], 0)
        STATE["grades"] = None
        with self.assertRaises(Exception): self.s.cv_grades()
        STATE["grades"] = [{"subjectDesc": "FISICA", "evtDate": d(-1), "displayValue": "7+", "decimalValue": 7.25,
                            "componentDesc": "Scritto", "periodDesc": "Primo periodo", "weightFactor": 1, "canceled": False}]
        g = self.s.cv_grades("fisica")["grades"]; self.assertEqual((len(g), g[0]["raw"]["displayValue"]), (1, "7+"))
        self.assertEqual(g[0]["uid"][:4], "sec:")                      # il mock non ha evtId: chiave di riserva
        self.assertEqual(self.s.cv_grades("matematica")["count"], 0)   # filtro per materia senza supposizioni
    def test_diagnostics_clamps_to_valid_range(self):
        # il registro reale rifiuta date fuori anno scolastico: la diagnostica non deve fallire
        ys, ye = self.s.cv.school_year_range(TODAY)
        STATE["valid_range"] = (ys.strftime("%Y%m%d"), ye.strftime("%Y%m%d"))
        try:
            r = self.s.cv_diagnostics()
            self.assertNotIn("errore", r["agenda"], r["agenda"])
            self.assertGreaterEqual(r["agenda"]["finestra"][0], str(ys))
            self.assertLessEqual(r["agenda"]["finestra"][1], str(ye))
            self.assertIn("coerenti", r["compiti"])
            # intervallo piu' stretto di quello atteso: usa quello indicato da ClasseViva e riprova
            lo, hi = TODAY - dt.timedelta(days=10), TODAY + dt.timedelta(days=100)
            STATE["valid_range"] = (lo.strftime("%Y%m%d"), hi.strftime("%Y%m%d"))
            r = self.s.cv_diagnostics()
            self.assertNotIn("errore", r["agenda"], r["agenda"])
            self.assertEqual(r["agenda"]["finestra"][0], str(max(lo, ys)))
        finally:
            STATE["valid_range"] = None

    def test_absences_keep_raw_code_and_fields(self):
        r = self.s.cv_absences()
        e = r["events"][0]
        self.assertEqual((r["count"], e["uid"], e["date"]), (1, "cvv:555", d(-2)))
        self.assertEqual(e["raw"], {"evtId": 555, "evtCode": "ABR0", "evtDate": d(-2), "evtHPos": 2, "evtValue": 1,
                                    "hoursAbsence": 0, "isJustified": False, "justifReasonCode": None,
                                    "justifReasonDesc": None, "webJustifStatus": "N"})   # valori esatti, 0 e False restano tali
        self.assertEqual(e["extra_fields_present"], ["nuovoCampo"])
        self.assertEqual(e["interpreted"]["tipo"], "Entrata in ritardo")   # ABR0: tabella verificata
        self.assertEqual(e["interpreted"]["ore_perse"], 1)
        self.assertEqual(e["derived"]["nome"], "Entrata in ritardo \u00b7 %s \u00b7 2\u00aa ora" % ("%s/%s/%s" % (d(-2)[8:], d(-2)[5:7], d(-2)[:4])))

    def test_absence_hash_changes_only_with_raw_data(self):
        base = {"evtId": 9, "evtCode": "ABA0", "evtDate": "2026-10-27", "evtHPos": None, "evtValue": None,
                "hoursAbsence": None, "isJustified": False, "justifReasonCode": None,
                "justifReasonDesc": None, "webJustifStatus": None}
        n = self.s.cv.normalize_absence
        h0 = n(base)["hash"]
        self.assertEqual(h0, n(dict(base, campoNuovo=1))["hash"])            # campo extra: hash invariato
        self.assertNotEqual(h0, n(dict(base, isJustified=True))["hash"])     # giustificata: hash cambia
        self.assertEqual(n(base)["uid"], "cvv:9")
        self.assertTrue(n({k: v for k, v in base.items() if k != "evtId"})["uid"].startswith("sec:"))

    def test_grades_and_notices_normalized_from_full_lists(self):
        STATE["grades"] = [{"evtId": 77, "subjectDesc": "MATEMATICA", "evtDate": d(-1), "displayValue": "8", "decimalValue": 8.0,
                            "componentDesc": "Scritto", "periodDesc": "Primo periodo", "canceled": False, "campoNuovo": "x"}]
        STATE["notices"] = [{"pubId": 5, "cntTitle": "Circolare n. 1", "pubDT": d(-3) + "T08:00:00+02:00", "cntCategory": ""}]
        try:
            cv, c = self.s.cv, self.s.client()
            g = cv.normalize_grade(c.grades()[0])
            self.assertEqual(g["uid"], "cvv:77")
            self.assertEqual(g["raw"]["campoNuovo"], "x")              # campo non previsto conservato
            self.assertEqual(g["interpreted"]["tipo_valutazione"], "Da verificare")
            self.assertTrue(g["interpreted"]["da_verificare"])
            STATE["grades"][0]["displayValue"] = "9"                    # il voto cambia nel registro
            self.assertNotEqual(cv.normalize_grade(c.grades()[0])["hash"], g["hash"])
            n = cv.normalize_notice(c.noticeboard()[0])
            self.assertEqual((n["uid"], n["interpreted"]["categoria"], n["date"]), ("cvv:5", "Da classificare", d(-3)))
            self.assertEqual(self.s.cv_noticeboard()["count"], 1)
        finally:
            STATE["grades"] = []; STATE["notices"] = []

    def test_school_year_helpers(self):
        f = self.s.cv.school_year_range
        self.assertEqual(f(dt.date(2026, 10, 3)), (dt.date(2026, 9, 1), dt.date(2027, 8, 31)))
        self.assertEqual(f(dt.date(2027, 8, 31)), (dt.date(2026, 9, 1), dt.date(2027, 8, 31)))
        self.assertEqual(f(dt.date(2027, 9, 1)), (dt.date(2027, 9, 1), dt.date(2028, 8, 31)))
        msg = "HTTP 404 dates must be beween 20260901 and 20270831; first date"
        self.assertEqual(self.s.cv.parse_valid_range(msg), (dt.date(2026, 9, 1), dt.date(2027, 8, 31)))
        self.assertIsNone(self.s.cv.parse_valid_range("altro errore"))

    def test_diagnostics_distinguishes_zero_from_unavailable(self):
        r = self.s.cv_diagnostics()
        self.assertTrue(r["endpoint"]["grades"]["ok"]); self.assertEqual(r["endpoint"]["grades"]["elementi"], 0)
        self.assertEqual(r["compiti"], {"agenda_completa_AGHW": 2, "endpoint_per_codice_AGHW": 2, "coerenti": True})
        self.assertEqual(r["agenda"]["per_codice"]["AGNT"], 3)
        STATE["grades"] = None; STATE["code_endpoint"] = False
        r = self.s.cv_diagnostics()
        self.assertFalse(r["endpoint"]["grades"]["ok"]); self.assertIn("errore", r["compiti"])
        self.assertEqual(len(r["lezioni"]["coppie_docente_materia"]), 2)
    def test_lessons(self):
        r = self.s.cv_lessons(d(-3), d(0)); self.assertEqual(r["count"], 2); self.assertEqual(r["lessons"][0]["materia"], "FISICA")
class Proto(unittest.TestCase):
    def test_stdio_roundtrip(self):
        env = dict(os.environ, CLASSEVIVA_BASE=BASE)
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env)
        def rpc(i, m, par=None):
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": par or {}}) + "\n"); p.stdin.flush()
            return json.loads(p.stdout.readline())
        init = rpc(1, "initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(init["serverInfo"]["name"], "classeviva")
        # le istruzioni arrivano al client una volta, all'avvio: nessun rimando a Notion o a strumenti rimossi
        self.assertTrue(init["instructions"])
        self.assertNotIn("notion", init["instructions"].lower())
        self.assertNotIn("cv_sync_plan", init["instructions"])
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"); p.stdin.flush()
        names = {t["name"] for t in rpc(2, "tools/list")["result"]["tools"]}
        self.assertEqual(names, {"cv_status", "cv_agenda", "cv_tests", "cv_homework", "cv_next_test", "cv_subjects", "cv_lessons",
                                 "cv_grades", "cv_noticeboard", "cv_absences", "cv_diagnostics",
                                 "cv_registry_dryrun", "cv_registry_sync", "cv_attribution_report", "school_freshness", "school_registry_migrate", "school_overview", "school_context_v3", "school_links", "school_link_explain", "school_communications", "school_communication_explain", "school_context_v4", "school_status_v1", "school_priority_v1", "school_action_v1", "school_study_context_v1", "school_study_assistant_v1", "school_study_session_v1", "school_records_v1", "school_materials_catalog_v1", "school_dashboard_v1", "school_material_acquire_v1", "school_materials_status_v1", "school_study_material_v1",
                                 "cls_status", "cls_registry_dryrun", "cls_registry_sync", "school_context",
                                 "school_subjects", "cls_course_subjects",
                                 "school_brief", "school_tasks", "school_task", "school_announcements",
                                 "school_materials"})
        self.assertFalse([n for n in names if "notion" in n or n == "cv_sync_plan"])   # nessuno strumento verso Notion
        r = rpc(3, "tools/call", {"name": "cv_next_test", "arguments": {"subject": "filosofia"}})["result"]
        self.assertFalse(r["isError"]); self.assertEqual(json.loads(r["content"][0]["text"])["next"]["uid"], "cvv:1001")
        self.assertTrue(rpc(4, "tools/call", {"name": "cv_agenda", "arguments": {"from_date": "xx"}})["result"]["isError"])
        self.assertIn("result", rpc(5, "ping")); p.stdin.close(); p.wait(timeout=10); p.stdout.close(); p.stderr.close()

if __name__ == "__main__":
    unittest.main(verbosity=2)
