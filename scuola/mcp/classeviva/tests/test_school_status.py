"""M8 v1 - school_status/1 (cloud.school.status/1): stato oggettivo per materia sopra M7.1. Fatti -> segnali -> valutazioni
non_valutabile; evidenze tracciabili; affidabilita' monotona; legacy solo provenance (D1); anno operativo separato dallo storico (D3);
mancante != vuoto; possibili/generali mai nei segnali; nessun giudizio; determinismo; nessuna scrittura; tool MCP."""
import datetime as dt
import hashlib, json, os, re, sys, tempfile, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import context4_fixtures as F
import cvv_dryrun as dry
import cvv_registry as CR
import registry as R
import school_context as SC
import school_context4 as C4
import school_status as ST
from links_fixtures import ann, lesson
from test_school_context4 import fresh_fake, pretty, counts

SUBJ, NOW, MAN = F.SUBJECTS, F.NOW, F.MANUAL
A = lambda c, a: "cls:%s/%s" % (c, a)            # noqa: E731
GRADE = {"evtId": 1, "subjectDesc": "MATEMATICA", "evtDate": "2026-10-30", "displayValue": "7+", "decimalValue": 7.25,
         "componentDesc": "Scritto", "periodDesc": "Trimestre", "weightFactor": None, "canceled": False, "notesForFamily": ""}


def build(reg, materia, **kw):
    kw.setdefault("manual", MAN)
    return ST.build(reg, materia, kw.pop("now", NOW), subjects=kw.pop("subjects", SUBJ), **kw)


def sig(d, sid):
    return next(s for s in d["segnali"] if s["id"] == sid)


def ev_uids(d, sid=None):
    return {e["uid"] for s in d["segnali"] if sid in (None, s["id"]) for e in s["evidenze"] if "uid" in e}


def with_fresh(fn, **fk):
    with mock.patch.object(R.Registry, "freshness", fresh_fake(**fk)):
        return fn()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = F.make_registry()
        cls.p = mock.patch.object(R.Registry, "freshness", fresh_fake())
        cls.p.start()
        cls.docs = {k: build(cls.reg, k) for k in SC.AGENT_SUBJECTS}

    @classmethod
    def tearDownClass(cls):
        cls.p.stop()
        cls.reg.close()


# ------------------------------------------------------------------------------------------------ struttura
class Struttura(Base):
    def test_schema_and_stable_keys(self):
        d = self.docs["matematica"]
        self.assertEqual(ST.SCHEMA, "cloud.school.status/1")
        self.assertEqual(list(d), ["schema", "natura", "materia", "riferimento_temporale", "livelli", "copertura", "fatti", "segnali",
                                   "valutazioni", "storico", "freshness", "gap", "gap_m7", "ambiguita", "provenance", "regole_agente"])
        self.assertEqual([s["id"] for s in d["segnali"]], list(ST.SIGNALS))
        self.assertTrue(ST.status_version().startswith("r1-"))
        self.assertEqual(d["provenance"]["context_version"], C4.context_version())

    def test_all_eight_subjects(self):
        self.assertEqual(sorted(self.docs), sorted(SC.AGENT_SUBJECTS))
        for k, d in self.docs.items():
            self.assertEqual(d["materia"]["chiave"], k)
            self.assertEqual(d["riferimento_temporale"]["anno_operativo"], "2026-27")

    def test_invalid_inputs(self):
        for bad in ("chimica", "", None):
            with self.assertRaises(ValueError):
                build(self.reg, bad)
        for bad in ("true", ["lezioni"], 1):
            with self.assertRaises(ValueError):
                build(self.reg, "matematica", dettaglio=bad)
        with self.assertRaises(ValueError):
            build(self.reg, "matematica", now="2026-11-01T12:00:00")

    def test_signal_shape(self):
        for d in self.docs.values():
            for s in d["segnali"]:
                self.assertEqual(set(s) - {"note", "evidenze_dettaglio"},
                                 {"id", "nome", "valore", "natura", "regola", "evidenze", "affidabilita", "motivi_affidabilita", "freschezza"})
                self.assertIn(s["natura"], ST.NATURE)
                self.assertIn(s["affidabilita"], ST.RELIABILITY)
                self.assertEqual(s["motivi_affidabilita"] == [], s["affidabilita"] == "alta")


# ------------------------------------------------------------------------------------------------ valutazioni e lessico
class Valutazioni(Base):
    def test_always_non_valutabile(self):
        for d in self.docs.values():
            v = d["valutazioni"]
            self.assertEqual((v["rendimento"]["stato"], v["puntualita"]["stato"]), ("non_valutabile", "non_valutabile"))

    def test_grades_present_still_non_valutabile(self):
        reg = F.make_registry()
        CR.sync_kind(reg, "grade", lambda: [dict(GRADE)])
        d = with_fresh(lambda: build(reg, "matematica"))
        self.assertEqual(sig(d, "S9")["valore"], {"stato": "voti_presenti", "n_materia": 1, "n_registro": 1})
        self.assertEqual(d["valutazioni"]["rendimento"]["stato"], "non_valutabile")
        self.assertIn("nessun modello", d["valutazioni"]["rendimento"]["motivo"])
        self.assertNotIn("7.25", pretty(d))                                   # nessun voto riportato o stimato
        self.assertEqual(sig(with_fresh(lambda: build(reg, "fisica")), "S9")["valore"]["n_materia"], 0)
        reg.close()

    def test_no_judgment_words(self):
        rx = re.compile(r"\b(%s)\b" % "|".join(re.escape(w) for w in ST.FORBIDDEN_WORDS), re.I)
        for d in self.docs.values():
            self.assertIsNone(rx.search(pretty(d)))
        for txt in list(ST.RULES.values()) + ST.RULES_FOR_AGENTS + [ST.NATURA] + list(ST.SIGNALS.values()):
            self.assertIsNone(rx.search(txt), txt)

    def test_no_lesson_ratio(self):
        s4 = sig(self.docs["matematica"], "S4")
        self.assertEqual(set(s4["valore"]), {"n", "prima", "ultima", "giorni_da_ultima", "con_discordanza_m2"})
        self.assertEqual(self.docs["matematica"]["copertura"]["orario"]["stato"], "non_disponibile")


# ------------------------------------------------------------------------------------------------ segnali
class Segnali(Base):
    def test_s1_present_agenda(self):
        s = sig(self.docs["matematica"], "S1")
        self.assertEqual((s["valore"]["stato"], s["valore"]["data"], s["valore"]["giorni_mancanti"], s["valore"]["uid"]),
                         ("presente", "2026-11-02", 1, "cvv:1000101"))
        self.assertEqual(s["affidabilita"], "alta")

    def test_s1_m6_probabile_is_media(self):
        s = sig(self.docs["inglese"], "S1")
        self.assertEqual((s["valore"]["uid"], s["natura"], s["affidabilita"]), (A("C-EN", "a-en-ver"), "dipende_da_interpretazione", "media"))
        self.assertTrue(any(m.startswith("A1") for m in s["motivi_affidabilita"]))

    def test_s1_absent_and_hints(self):
        self.assertEqual(sig(self.docs["storia"], "S1")["valore"], {"stato": "assente"})
        sc = sig(self.docs["scienze"], "S1")                                    # a-sci-ver: verifica senza data, 2026-27
        self.assertEqual(sc["valore"], {"stato": "assente_con_indizi_non_datati", "n_indizi": 1})
        self.assertEqual({e["uid"] for e in sc["evidenze"]}, {A("C-SC", "a-sci-ver")})
        reg = F.make_registry(announcements=[a for a in F.ANNOUNCEMENTS if a["id"] != "a-en-ver"])
        s = sig(with_fresh(lambda: build(reg, "inglese")), "S1")
        self.assertEqual(s["valore"]["stato"], "assente_con_indizi_non_datati")
        self.assertEqual(s["affidabilita"], "bassa")
        self.assertIn(A("C-EN", "a-en-rel"), {e["uid"] for e in s["evidenze"]})
        reg.close()

    def test_s2_s3_compiti(self):
        d = self.docs["inglese"]
        self.assertEqual(sig(d, "S2")["valore"], {"n": 1, "prossima_scadenza": "2026-11-07", "uid": A("C-EN", "w-en-open")})
        self.assertEqual(sig(d, "S3")["valore"]["n"], 1)
        self.assertEqual({e["uid"] for e in sig(d, "S3")["evidenze"]}, {A("C-EN", "w-en-late")})
        self.assertEqual(sig(self.docs["matematica"], "S2")["valore"]["n"], 4)

    def test_s4_s5_lessons(self):
        d = self.docs["matematica"]
        self.assertEqual(sig(d, "S4")["valore"], {"n": 4, "prima": "2026-10-19", "ultima": "2026-10-24", "giorni_da_ultima": 8,
                                                  "con_discordanza_m2": 0})
        self.assertEqual(len(sig(d, "S5")["valore"]["argomenti"]), 4)
        self.assertEqual(sig(self.docs["inglese"], "S4")["valore"]["n"], 0)          # vuoto valido, non mancante
        self.assertEqual(sig(self.docs["inglese"], "S4")["affidabilita"], "alta")

    def test_s5_discordance_flags_item_and_media(self):
        reg = F.make_registry(lessons=F.LF.LESSONS + [lesson(9, "MATEMATICA", "ROSSI MARIO", "2026-10-30", "Leggi di Keplero e gravitazione")])
        d = with_fresh(lambda: build(reg, "matematica"))
        s5, s4 = sig(d, "S5"), sig(d, "S4")
        self.assertEqual(s5["affidabilita"], "media")
        self.assertTrue(next(a for a in s5["valore"]["argomenti"] if a["uid"] == "cvv:lesson:9")["discordanza_m2"])
        self.assertEqual(s4["affidabilita"], "bassa")                                # la lezione discordante e' l'ULTIMA (primaria)
        self.assertEqual(s4["valore"]["con_discordanza_m2"], 1)
        reg.close()

    def test_s6_links_keep_m5_state(self):
        s = sig(self.docs["matematica"], "S6")
        links = s["valore"][0]["collegamenti"]
        self.assertTrue(any(l["relazione"] == "prepara" and l["stato"] == "probabile" for l in links))
        self.assertIn(s["affidabilita"], ("media", "bassa"))
        self.assertEqual(sig(self.docs["scienze"], "S6")["valore"]["stato"], "non_applicabile")

    def test_s7_counts_operational_year_only(self):
        s = sig(self.docs["scienze"], "S7")
        self.assertEqual(s["valore"]["n"], 6)
        self.assertNotIn(A("C-SC", "a-old"), {e["uid"] for e in s["evidenze"]})        # 2025-26: solo storico
        self.assertEqual(s["affidabilita"], "alta")                                     # riferimenti relativi non pertinenti a S7

    def test_s8_attribution(self):
        self.assertEqual(sig(self.docs["italiano"], "S8")["valore"]["stato"], "solo_possibili")
        self.assertEqual(sig(self.docs["inglese"], "S8")["valore"]["stato"], "attribuiti")
        d = with_fresh(lambda: build(self.reg, "matematica", manual={}))
        self.assertEqual(sig(d, "S8")["valore"]["stato"], "nessun_corso_confermato")

    def test_s9_empty_vs_missing(self):
        self.assertEqual(sig(self.docs["storia"], "S9")["valore"]["stato"], "nessun_voto")
        self.assertIn("dati_valutativi_assenti", [g["codice"] for g in self.docs["storia"]["gap"]])
        d = with_fresh(lambda: build(self.reg, "storia"), none_kinds=("classeviva/grade",))
        s = sig(d, "S9")
        self.assertEqual((s["valore"]["stato"], s["valore"]["n_materia"], s["affidabilita"]), ("non_disponibile", None, "non_determinabile"))
        self.assertIn("fonte_non_letta", [g["codice"] for g in d["gap"]])


# ------------------------------------------------------------------------------------------------ evidenze, perimetro, affidabilita'
class EvidenzeEAffidabilita(Base):
    def test_every_signal_has_traceable_evidence(self):
        for k, d in self.docs.items():
            c = C4.collect(self.reg, k, NOW, manual=MAN, subjects=SUBJ)
            known = {r["_mk"](False)["uid"]: r["_mk"](False)["hash"] for s in C4.SECTIONS for r in c["candidati"][s]}
            for s in d["segnali"]:
                self.assertTrue(s["evidenze"], (k, s["id"]))
                for e in s["evidenze"]:
                    if "uid" in e:
                        self.assertEqual(known[e["uid"]], e["hash"])
                    else:
                        self.assertIn("contatore", e)

    def test_possibili_and_generali_never_evidence(self):
        for k, d in self.docs.items():
            c = C4.collect(self.reg, k, NOW, manual=MAN, subjects=SUBJ)
            banned = {r["uid"] for s in ("possibili_da_verificare", "generali") for r in c["candidati"][s]}
            self.assertFalse(ev_uids(d) & banned, k)

    def test_storico_separated(self):
        d = self.docs["scienze"]
        self.assertEqual(d["storico"]["anni"].get("2025-26", {}).get("comunicazioni"), 1)
        c = C4.collect(self.reg, "scienze", NOW, manual=MAN, subjects=SUBJ)
        old = {r["uid"] for s in C4.SECTIONS for r in c["candidati"][s] if ST._year(r, s) not in (None, "2026-27")}
        self.assertTrue(old)
        for dd in self.docs.values():
            self.assertFalse(ev_uids(dd) & old)

    def test_monotone_with_stale_and_values_unchanged(self):
        order = {r: i for i, r in enumerate(ST.RELIABILITY)}
        for k in ("matematica", "inglese", "scienze"):
            a = self.docs[k]
            b = with_fresh(lambda: build(self.reg, k), stale_kinds=("classeviva/lesson", "classroom/announcement", "classeviva/agenda_event"))
            for sa, sb in zip(a["segnali"], b["segnali"]):
                self.assertEqual(pretty(sa["valore"]), pretty(sb["valore"]))
                self.assertGreaterEqual(order[sb["affidabilita"]], order[sa["affidabilita"]])
            self.assertEqual(sig(b, "S4")["affidabilita"], "bassa")

    def test_never_read_is_null_and_non_determinabile(self):
        d = with_fresh(lambda: build(self.reg, "matematica"), none_kinds=("classeviva/lesson",))
        for sid in ("S4", "S5"):
            self.assertIsNone(sig(d, sid)["valore"])
            self.assertEqual(sig(d, sid)["affidabilita"], "non_determinabile")
        self.assertEqual(d["copertura"]["lezioni"]["stato"], "non_letto")

    def test_empty_registry_kind_is_zero_not_null(self):
        reg = F.make_registry(lessons=[])
        d = with_fresh(lambda: build(reg, "matematica"))
        self.assertEqual(sig(d, "S4")["valore"]["n"], 0)
        self.assertEqual(d["copertura"]["lezioni"]["stato"], "vuoto_valido")
        reg.close()

    def test_legacy_does_not_downgrade(self):
        def legacy(self_, now, stale_after_hours=None):
            out = fresh_fake()(self_, now, stale_after_hours)
            for e in out["per_kind"].values():
                e["ultimo_run"]["legacy"] = True
            return out
        with mock.patch.object(R.Registry, "freshness", legacy):
            d = build(self.reg, "matematica")
        a = self.docs["matematica"]
        self.assertEqual([s["affidabilita"] for s in a["segnali"]], [s["affidabilita"] for s in d["segnali"]])
        self.assertTrue(d["provenance"]["kind_legacy"])

    def test_ambiguity_relevance_table(self):
        self.assertEqual(set(ST.AMBIGUITY_RELEVANCE), set(ST.SIGNALS))
        for codes in ST.AMBIGUITY_RELEVANCE.values():
            self.assertTrue(set(codes) <= set(C4.AMBIGUITY_CODES))
        a = next(x for x in self.docs["inglese"]["ambiguita"] if x["codice"] == "possibile_stessa_verifica")
        self.assertEqual(a["tocca_segnali"], [])                          # le due verifiche sono passate: non sono evidenze di S1


# ------------------------------------------------------------------------------------------------ determinismo, scritture, tool
class DeterminismoESicurezza(unittest.TestCase):
    def test_byte_identical_and_order_independent(self):
        a, b = F.make_registry(), F.make_registry(reverse=True)
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            for k in SC.AGENT_SUBJECTS:
                for kw in ({}, {"dettaglio": True}):
                    x1, x2, y = build(a, k, **kw), build(a, k, **kw), build(b, k, **kw)
                    self.assertEqual(pretty(x1), pretty(x2))
                    self.assertEqual(pretty(x1), pretty(y))
        a.close(); b.close()

    def test_builder_never_reads_clock(self):
        src = open(ST.__file__, encoding="utf-8").read()
        for bad in ("datetime.now", "time.time", "date.today", "utcnow"):
            self.assertNotIn(bad, src)

    def test_no_writes_and_m7_unchanged(self):
        reg = F.make_registry()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            before, m7 = counts(reg), pretty(C4.build(reg, "matematica", NOW, manual=MAN, subjects=SUBJ, dettaglio=True))
            for k in SC.AGENT_SUBJECTS:
                build(reg, k, dettaglio=True)
            self.assertEqual(before, counts(reg))
            self.assertEqual(m7, pretty(C4.build(reg, "matematica", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)))
        reg.close()

    def test_build_from_path_writes_nothing(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "registry.sqlite3")
        F.make_registry(path=p).close()
        h = lambda q: hashlib.sha256(open(q, "rb").read()).hexdigest()                 # noqa: E731
        s0, f0, h0 = dry._fs_state(p), sorted(os.listdir(d)), h(p)
        out = ST.build_from_path(p, "fisica", NOW, subjects=SUBJ)
        self.assertEqual(out["schema"], ST.SCHEMA)
        self.assertEqual((s0, f0, h0), (dry._fs_state(p), sorted(os.listdir(d)), h(p)))

    def test_detail_adds_m7_elements(self):
        reg = F.make_registry()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            d = build(reg, "matematica", dettaglio=True)
        s = sig(d, "S4")
        self.assertEqual([e["uid"] for e in s["evidenze_dettaglio"]], [e["uid"] for e in s["evidenze"]])
        reg.close()


class ServerTool(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.d = tempfile.mkdtemp()
        os.chmod(self.d, 0o700)
        self.db = os.path.join(self.d, "registry.sqlite3")
        self.manual = os.path.join(self.d, "manual", "school_manual.json")
        for p in (mock.patch.dict(os.environ, {"CLOUD_SCHOOL_DB": self.db}), mock.patch.object(server, "subjects", lambda: SUBJ),
                  mock.patch.object(SC, "manual_default_path", lambda: self.manual)):
            p.start(); self.addCleanup(p.stop)

    def test_tool_registered(self):
        t = self.server.srv._tools["school_status_v1"]["def"]
        self.assertEqual(set(t["inputSchema"]["properties"]), {"materia", "dettaglio", "soglia_ore"})
        self.assertEqual(t["inputSchema"]["required"], ["materia"])
        self.assertIn("SOLA LETTURA", t["description"])

    def test_missing_registry(self):
        with self.assertRaises(ValueError):
            self.server.school_status_v1("matematica")
        self.assertFalse(os.path.exists(self.db))

    def test_clock_once_no_writes(self):
        F.make_registry(path=self.db).close()
        os.makedirs(os.path.dirname(self.manual), exist_ok=True)
        with open(self.manual, "w") as f:
            json.dump(MAN, f)
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
            out = self.server.school_status_v1("matematica")
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["riferimento_temporale"]["now"], "2026-11-01T12:00:00+02:00")
        self.assertFalse(out["verifica_nessuna_scrittura"]["file_modificati"])
        self.assertEqual(h0, hashlib.sha256(open(self.db, "rb").read()).hexdigest())


if __name__ == "__main__":
    unittest.main()
