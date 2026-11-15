"""M14 v1 - school_records/1 (cloud.school.records/1): registro valutativo e presenze, statistiche DESCRITTIVE. Inclusione dichiarata,
politiche di media nominate (mai 'la tua media'), pesi assunti, peso 0, annullati, non numerici, stati distinti (fonte non letta /
nessun voto / solo esclusi / media calcolabile / non calcolabile), null mai zero, periodi, distribuzione, serie, storico separato,
assenze da tabelle verificate, provenance, determinismo, ordine invertito, nessuna scrittura, input immutati, clock una volta,
M1-M13 invariati."""
import copy, datetime as dt
import hashlib, json, os, re, sys, tempfile, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import context4_fixtures as F
import cvv_cls_links as L
import cvv_comms as M
import cvv_dryrun as dry
import cvv_registry as CR
import registry as R
import school_action as AC
import school_context as SC
import school_context3 as C3
import school_context4 as C4
import school_priority as PR
import school_records as RC
import school_status as ST
import school_study_assistant as SA
import school_study_context as SX
import school_study_session as SS
from test_school_context4 import fresh_fake, pretty, counts

SUBJ, NOW, MAN = F.SUBJECTS, F.NOW, F.MANUAL


def grade(eid, subj, day, dec, w=1, comp="Scritto", per="Primo periodo", pos=1, canceled=False, disp=None):
    return {"evtId": eid, "subjectDesc": subj, "evtDate": day, "displayValue": disp if disp is not None else str(dec), "decimalValue": dec,
            "componentDesc": comp, "periodDesc": per, "periodPos": pos, "weightFactor": w, "canceled": canceled, "notesForFamily": ""}


def absence(eid, code, day, hpos=1, val=None, ws=1):
    return {"evtId": eid, "evtCode": code, "evtDate": day, "evtHPos": hpos, "evtValue": val, "hoursAbsence": [],
            "isJustified": False, "justifReasonCode": None, "justifReasonDesc": None, "webJustifStatus": ws}


GRADES = [
    grade(1, "MATEMATICA", "2026-10-29", 7.25, w=1, disp="7+"),
    grade(2, "MATEMATICA", "2026-10-31", 5.0, w=2),
    grade(3, "MATEMATICA", "2026-10-30", 8.0, canceled=True),                     # annullato
    grade(4, "MATEMATICA", "2026-10-29", None, disp="+"),                         # non numerico
    grade(5, "FISICA", "2026-10-29", 6.0, w=None),                                # peso non fornito -> assunto 1
    grade(6, "STORIA", "2026-10-29", 6.5, w=0),                                   # somma pesi 0
    grade(7, "RELIGIONE CATTOLICA/ATTIVITA' ALTERNATIVA", "2026-10-29", None, disp="ottimo"),   # solo non numerico
    grade(8, "MATEMATICA", "2025-10-01", 4.0),                                    # storico 2025-26
    grade(9, "CHIMICA ORGANICA", "2026-10-30", 7.0),                              # materia non attribuita
    grade(10, "MATEMATICA", "2026-10-31", 9.5, per="Secondo periodo", pos=2),
]
ABSENCES = [absence(101, "ABA0", "2026-10-12"), absence(102, "ABR0", "2026-10-14", val=1), absence(103, "ABR0", "2026-10-22"),
            absence(104, "ABU0", "2026-10-23"), absence(105, "ABA0", "2025-10-27")]
SUBJECTS = SUBJ + [s for s in __import__("cvv_subject").build_subjects([
    {"id": 9, "description": "RELIGIONE CATTOLICA/ATTIVITA' ALTERNATIVA", "teachers": [{"teacherName": "X Y"}]}])]


def fake(none=(), grade_ok=True, absence_ok=True):
    base = fresh_fake(none_kinds=tuple(none))

    def freshness(self, now, stale_after_hours=None):
        out = base(self, now, stale_after_hours)
        for k, ok in (("classeviva/absence", absence_ok), ("classeviva/grade", grade_ok)):
            if k not in none:
                out["per_kind"][k] = {"ultimo_run": {"legacy": False}, "stale": False, "motivo": "ok", "eta_ore": 1,
                                      "ultima_lettura_riuscita": "2026-11-01T10:00:00+02:00"}
        return out
    return freshness


def make(grades=GRADES, absences=ABSENCES, reverse=False, path=":memory:"):
    reg = F.make_registry(path=path, reverse=reverse)
    o = (lambda xs: list(reversed(xs))) if reverse else (lambda xs: list(xs))
    CR.sync_kind(reg, "grade", lambda: [dict(g) for g in o(grades)])
    CR.sync_kind(reg, "absence", lambda: [dict(a) for a in o(absences)])
    return reg


def build(reg, **kw):
    kw.setdefault("manual", MAN)
    return RC.build(reg, kw.pop("now", NOW), subjects=kw.pop("subjects", SUBJECTS), **kw)


def subj(d, k):
    return next(m for m in d["voti"]["per_materia"] if m["materia"] == k)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = make()
        cls.p = mock.patch.object(R.Registry, "freshness", fake())
        cls.p.start()
        cls.doc = build(cls.reg)

    @classmethod
    def tearDownClass(cls):
        cls.p.stop()
        cls.reg.close()


# ------------------------------------------------------------------------------------------------ struttura
class Struttura(Base):
    def test_schema_and_keys(self):
        d = self.doc
        self.assertEqual(RC.SCHEMA, "cloud.school.records/1")
        self.assertEqual(list(d), ["schema", "natura", "riferimento_temporale", "ambito", "politiche", "voti", "assenze", "storico", "gap",
                                   "freshness", "provenance", "regole_agente"])
        self.assertEqual(d["riferimento_temporale"]["anno_operativo"], "2026-27")
        self.assertTrue(RC.records_version().startswith("r1-"))

    def test_policies_named_never_your_average(self):
        p = self.doc["politiche"]
        self.assertEqual(p["primaria"], "media_pesata_weightfactor")
        for pol in p["elenco"].values():
            self.assertEqual(pol["corrispondenza_classeviva"], "non_verificata")
            self.assertTrue(pol["formula"])
        self.assertIn("media_classeviva_non_verificata", [g["codice"] for g in self.doc["gap"]])
        self.assertNotRegex(pretty(self.doc).lower(), r"la tua media")

    def test_no_judgment_words(self):
        rx = re.compile(r"\b(%s)\b" % "|".join(re.escape(w) for w in RC.FORBIDDEN_WORDS), re.I)
        for txt in [RC.NATURA] + RC.RULES_FOR_AGENTS + [p["formula"] for p in RC.POLICIES.values()] + [g["messaggio"] for g in self.doc["gap"]]:
            self.assertIsNone(rx.search(txt), txt)

    def test_invalid_inputs(self):
        for kw in ({"materia": ""}, {"materia": "astrologia"}, {"periodo": " "}, {"dettaglio": "si"}, {"soglia_ore": 0}):
            with self.assertRaises(ValueError):
                build(self.reg, **kw)
        with self.assertRaises(ValueError):
            build(self.reg, now="2026-11-01T12:00:00")


# ------------------------------------------------------------------------------------------------ voti
class Voti(Base):
    def test_inclusion_declared_verbatim(self):
        el = {g["uid"]: g for g in self.doc["voti"]["elenco"]}
        self.assertEqual(el["cvv:3"]["inclusione"], "escluso_annullato")
        self.assertEqual(el["cvv:4"]["inclusione"], "escluso_non_numerico")
        self.assertEqual((el["cvv:4"]["displayValue"], el["cvv:4"]["decimalValue"], el["cvv:4"]["valore"]), ("+", None, None))
        self.assertEqual((el["cvv:1"]["displayValue"], el["cvv:1"]["decimalValue"], el["cvv:1"]["peso_raw"]), ("7+", 7.25, 1))
        self.assertIsNone(el["cvv:5"]["peso_raw"])                                     # peso NON inventato nel dato

    def test_weighted_and_arithmetic_means(self):
        m = subj(self.doc, "matematica")
        self.assertEqual(m["stato"], "media_calcolabile")
        w = m["medie"]["media_pesata_weightfactor"]
        a = m["medie"]["media_aritmetica_semplice"]
        self.assertAlmostEqual(w["valore_esatto"], (7.25 * 1 + 5.0 * 2 + 9.5 * 1) / 4)
        self.assertAlmostEqual(a["valore_esatto"], (7.25 + 5.0 + 9.5) / 3)
        self.assertEqual(sorted(w["uid_usati"]), ["cvv:1", "cvv:10", "cvv:2"])
        self.assertEqual(w["valore"], round(w["valore_esatto"], RC.ROUND))
        self.assertEqual(m["esclusi"], {"escluso_annullato": ["cvv:3"], "escluso_non_numerico": ["cvv:4"]})

    def test_assumed_weight_declared(self):
        w = subj(self.doc, "fisica")["medie"]["media_pesata_weightfactor"]
        self.assertEqual((w["valore_esatto"], w["pesi_assunti"]["uid"], w["pesi_assunti"]["valore_assunto"]), (6.0, ["cvv:5"], 1))

    def test_zero_weight_sum_not_computable(self):
        s = subj(self.doc, "storia")
        self.assertEqual(s["stato"], "media_non_calcolabile")
        self.assertFalse(s["medie"]["media_pesata_weightfactor"]["calcolabile"])
        self.assertIsNone(s["medie"]["media_pesata_weightfactor"]["valore"])          # null, mai 0
        self.assertEqual(s["medie"]["media_aritmetica_semplice"]["valore_esatto"], 6.5)

    def test_states_distinct(self):
        # 15/11: un giudizio di Religione («ottimo») diventa 10, entra nella materia ma mai nella media complessiva
        rel = subj(self.doc, "religione")
        self.assertEqual((rel["stato"], rel["n_totali"], rel["n_inclusi"]), ("media_calcolabile", 1, 1))
        self.assertEqual([p["valore"] for p in rel["serie"]], [10])
        el = {g["uid"]: g for g in self.doc["voti"]["elenco"]}
        self.assertEqual((el["cvv:7"]["valore"], el["cvv:7"]["valore_da_giudizio"], el["cvv:7"]["fuori_media"]), (10, True, True))
        self.assertNotIn("cvv:7", self.doc["voti"]["complessivo"]["medie"]["media_aritmetica_semplice"]["uid_usati"])
        lat = subj(self.doc, "latino")
        self.assertEqual((lat["stato"], lat["n_totali"]), ("nessun_voto", 0))
        with mock.patch.object(R.Registry, "freshness", fake(none=("classeviva/grade",))):
            d = build(self.reg)
        self.assertEqual(d["voti"]["stato_fonte"]["stato"], "non_letto")
        self.assertEqual(subj(d, "matematica")["stato"], "fonte_non_letta")
        self.assertIsNone(subj(d, "matematica")["n_totali"])                          # null, non 0
        self.assertIsNone(d["voti"]["elenco"])
        empty = make(grades=[])
        with mock.patch.object(R.Registry, "freshness", fake()):
            e = build(empty)
        self.assertEqual((e["voti"]["stato_fonte"]["stato"], e["voti"]["complessivo"]["stato"], e["voti"]["complessivo"]["n_totali"]),
                         ("vuoto_valido", "nessun_voto", 0))
        self.assertIn("nessun_voto_anno_operativo", [g["codice"] for g in e["gap"]])
        empty.close()

    def test_eight_subjects_always_listed(self):
        keys = [m["materia"] for m in self.doc["voti"]["per_materia"]]
        self.assertTrue(set(SC.AGENT_SUBJECTS) <= set(keys))
        self.assertEqual(keys, sorted(keys))

    def test_unattributed_and_overall(self):
        self.assertEqual(self.doc["voti"]["non_attribuiti"], ["cvv:9"])
        c = self.doc["voti"]["complessivo"]
        self.assertIn("cvv:9", c["medie"]["media_aritmetica_semplice"]["uid_usati"])  # complessiva: tutti i voti inclusi
        self.assertNotIn("cvv:8", pretty(c))                                         # storico mai nel complessivo

    def test_no_average_di_classeviva_fuori_dalla_media_complessiva(self):
        raw = dict(grade(20, "STORIA", "2026-11-18", 9.0), noAverage=True)
        reg = make(grades=GRADES + [raw])
        try:
            d = build(reg)
        finally:
            reg.close()
        self.assertIn("cvv:20", [p["uid"] for p in subj(d, "storia")["serie"]])          # resta nello storico della materia
        self.assertNotIn("cvv:20", d["voti"]["complessivo"]["medie"]["media_aritmetica_semplice"]["uid_usati"])

    def test_periods(self):
        per = {p["periodo"]: p for p in self.doc["voti"]["per_periodo"]}
        self.assertEqual(list(per), ["Primo periodo", "Secondo periodo"])
        self.assertEqual(per["Secondo periodo"]["medie"]["media_aritmetica_semplice"]["uid_usati"], ["cvv:10"])
        d = build(self.reg, periodo="Primo periodo")
        self.assertNotIn("cvv:10", [g["uid"] for g in d["voti"]["elenco"]])

    def test_distribution_and_series(self):
        m = subj(self.doc, "matematica")
        self.assertEqual(m["distribuzione"]["conteggi"]["[7,8)"], 1)
        self.assertEqual(m["distribuzione"]["conteggi"]["[9,10]"], 1)
        self.assertEqual(sum(m["distribuzione"]["conteggi"].values()), m["n_inclusi"])
        dates = [p["data"] for p in m["serie"]]
        self.assertEqual(dates, sorted(dates))
        last = m["serie"][-1]["progressiva"]["media_pesata_weightfactor"]
        self.assertEqual(last, m["medie"]["media_pesata_weightfactor"]["valore"])

    def test_materia_filter_including_non_agent_subject(self):
        d = build(self.reg, materia="religione")
        self.assertEqual([m["materia"] for m in d["voti"]["per_materia"]], ["religione"])
        self.assertEqual([g["uid"] for g in d["voti"]["elenco"]], ["cvv:7"])

    def test_history_separated(self):
        self.assertEqual(self.doc["storico"]["conteggi"], {"assenze_2025-26": 1, "voti_2025-26": 1})
        self.assertNotIn("cvv:8", [g["uid"] for g in self.doc["voti"]["elenco"]])

    def test_provenance_hash_is_registry(self):
        recs = {r["source_uid"]: r["content_hash"] for r in self.reg.list_records(source=CR.SOURCE, kind="grade")}
        for g in self.doc["voti"]["elenco"]:
            self.assertEqual(g["hash"], recs[g["uid"].split(":")[1]])
        all_uids = {g["uid"] for g in self.doc["voti"]["elenco"]}
        for m in self.doc["voti"]["per_materia"]:
            for pol in (m["medie"] or {}).values():
                self.assertTrue(set(pol["uid_usati"]) <= all_uids)

    def test_detail_adds_raw(self):
        d = build(self.reg, dettaglio=True)
        self.assertEqual(d["voti"]["elenco"][0]["raw"]["evtId"], int(d["voti"]["elenco"][0]["uid"].split(":")[1]))
        self.assertNotIn("raw", self.doc["voti"]["elenco"][0])


# ------------------------------------------------------------------------------------------------ assenze
class Assenze(Base):
    def test_absence_counts_and_types(self):
        a = self.doc["assenze"]
        self.assertEqual(a["conteggi"]["totale"], 4)
        self.assertEqual(a["conteggi"]["per_tipo"], {"Assenza": 1, "Da verificare": 1, "Entrata in ritardo": 2})
        self.assertEqual(a["conteggi"]["per_mese"], {"2026-10": {"Assenza": 1, "Da verificare": 1, "Entrata in ritardo": 2}})
        self.assertEqual(a["conteggi"]["da_verificare"], 1)
        ev = {e["uid"]: e for e in a["eventi"]}
        self.assertEqual(ev["cvv:102"]["ore_perse"], 1)
        self.assertIsNone(ev["cvv:103"]["ore_perse"])                                   # mai inventate
        self.assertIn("assenze_da_verificare", [g["codice"] for g in self.doc["gap"]])

    def test_absence_source_unread(self):
        with mock.patch.object(R.Registry, "freshness", fake(none=("classeviva/absence",))):
            d = build(self.reg)
        self.assertEqual((d["assenze"]["stato_fonte"]["stato"], d["assenze"]["eventi"], d["assenze"]["conteggi"]), ("non_letto", None, None))

    def test_absence_empty_valid(self):
        reg = make(absences=[])
        with mock.patch.object(R.Registry, "freshness", fake()):
            d = build(reg)
        self.assertEqual((d["assenze"]["stato_fonte"]["stato"], d["assenze"]["conteggi"]["totale"]), ("vuoto_valido", 0))
        reg.close()


# ------------------------------------------------------------------------------------------------ determinismo, scritture, tool
class DeterminismoESicurezza(unittest.TestCase):
    def test_byte_identical_and_reversed(self):
        a, b = make(), make(reverse=True)
        with mock.patch.object(R.Registry, "freshness", fake()):
            for kw in ({}, {"dettaglio": True}, {"materia": "matematica"}, {"periodo": "Primo periodo"}):
                x1, x2, y = build(a, **kw), build(a, **kw), build(b, **kw)
                self.assertEqual(pretty(x1), pretty(x2))
                self.assertEqual(pretty(x1), pretty(y))
        a.close(); b.close()

    def test_builder_never_reads_clock(self):
        src = open(RC.__file__, encoding="utf-8").read()
        for bad in ("datetime.now", "time.time", "date.today", "utcnow"):
            self.assertNotIn(bad, src)

    def test_inputs_not_mutated(self):
        reg = make()
        subs = copy.deepcopy(SUBJECTS)
        with mock.patch.object(R.Registry, "freshness", fake()):
            build(reg, subjects=subs, dettaglio=True)
        self.assertEqual(subs, SUBJECTS)
        reg.close()

    def test_no_writes_and_m1_m13_unchanged(self):
        reg = make()
        with mock.patch.object(R.Registry, "freshness", fake()):
            snap = lambda: [pretty(C3.build(reg, "matematica", SUBJ, NOW, manual=MAN)), pretty(C3.overview(reg, SUBJ, NOW, manual=MAN)),  # noqa: E731
                            pretty(L.build(reg, SUBJ, NOW, manual=MAN)), pretty(M.build(reg, SUBJ, NOW, manual=MAN)),
                            pretty(C4.build(reg, "inglese", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(ST.build(reg, "matematica", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(PR.build(reg, NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(AC.build(reg, NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(SX.build(reg, "matematica", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(SA.build(reg, "matematica", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(SS.build(reg, "matematica", "spiega", "testo", NOW, manual=MAN, subjects=SUBJ))]
            before, c0 = snap(), counts(reg)
            build(reg, dettaglio=True)
            self.assertEqual((before, c0), (snap(), counts(reg)))
        reg.close()

    def test_build_from_path_writes_nothing(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "registry.sqlite3")
        make(path=p).close()
        h = lambda q: hashlib.sha256(open(q, "rb").read()).hexdigest()                 # noqa: E731
        s0, f0, h0 = dry._fs_state(p), sorted(os.listdir(d)), h(p)
        out = RC.build_from_path(p, NOW, subjects=SUBJECTS)
        self.assertEqual(out["schema"], RC.SCHEMA)
        self.assertEqual((s0, f0, h0), (dry._fs_state(p), sorted(os.listdir(d)), h(p)))


class ServerTool(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.d = tempfile.mkdtemp()
        os.chmod(self.d, 0o700)
        self.db = os.path.join(self.d, "registry.sqlite3")
        self.manual = os.path.join(self.d, "manual", "school_manual.json")
        for p in (mock.patch.dict(os.environ, {"CLOUD_SCHOOL_DB": self.db}), mock.patch.object(server, "subjects", lambda: SUBJECTS),
                  mock.patch.object(SC, "manual_default_path", lambda: self.manual)):
            p.start(); self.addCleanup(p.stop)

    def test_tool_registered(self):
        t = self.server.srv._tools["school_records_v1"]["def"]
        self.assertEqual(set(t["inputSchema"]["properties"]), {"materia", "periodo", "dettaglio", "soglia_ore"})
        self.assertFalse(t["inputSchema"].get("required"))
        self.assertIn("SOLA LETTURA", t["description"])

    def test_missing_registry(self):
        with self.assertRaises(ValueError):
            self.server.school_records_v1()
        self.assertFalse(os.path.exists(self.db))

    def test_clock_once_no_writes(self):
        make(path=self.db).close()
        calls = []
        fixed = dt.datetime(2026, 11, 1, 12, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))

        class FakeDT:
            class datetime:
                @staticmethod
                def now(tz=None):
                    calls.append(tz)
                    return fixed
        h0 = hashlib.sha256(open(self.db, "rb").read()).hexdigest()
        with mock.patch.object(self.server, "dt", FakeDT):
            out = self.server.school_records_v1()
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["riferimento_temporale"]["now"], "2026-11-01T12:00:00+02:00")
        self.assertFalse(out["verifica_nessuna_scrittura"]["file_modificati"])
        self.assertEqual(h0, hashlib.sha256(open(self.db, "rb").read()).hexdigest())


if __name__ == "__main__":
    unittest.main()
