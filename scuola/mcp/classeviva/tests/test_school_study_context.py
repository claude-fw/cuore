"""M11 v1 - school_study_context/1 (cloud.school.study_context/1): contesto di studio per UNA materia sopra M10 (+M9) e M8. Schema,
materia obbligatoria, 8 materie, obiettivi 1:1 con M10, 5 stati del dato, 3 motivi di non_disponibile, usi U1-U4 (U4 sempre
non_determinabile), Ariosto/matematica, lezioni mai rilevanti per un obiettivo, storico escluso, nessun materiale M4, nessuna invenzione,
affidabilita' monotona, determinismo, nessuna scrittura, clock una volta, input non mutati, vocabolari chiusi, M2-M10 invariati."""
import copy, datetime as dt
import hashlib, json, os, re, sys, tempfile, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import context4_fixtures as F
import cvv_cls_links as L
import cvv_comms as M
import cvv_dryrun as dry
import registry as R
import school_action as AC
import school_context as SC
import school_context3 as C3
import school_context4 as C4
import school_priority as PR
import school_status as ST
import school_study_context as SX
from links_fixtures import ann, lesson
from test_school_context4 import fresh_fake, pretty, counts

SUBJ, NOW, MAN = F.SUBJECTS, F.NOW, F.MANUAL
A = lambda c, a: "cls:%s/%s" % (c, a)            # noqa: E731
LV = {k: i for i, k in enumerate(ST.RELIABILITY)}
_AC_BUILD = AC.build


def build(reg, materia, **kw):
    kw.setdefault("manual", MAN)
    return SX.build(reg, materia, kw.pop("now", NOW), subjects=kw.pop("subjects", SUBJ), **kw)


def obj(d, uid):
    return next(o for o in d["obiettivi"] if o["uid"] == uid)


def patched_m10(mutate):
    """Sostituisce il contratto M10 con una sua copia modificata (per i casi sintetici): M11 resta invariato."""
    def fake(*a, **kw):
        out = copy.deepcopy(_AC_BUILD(*a, **kw))
        mutate(out)
        return out
    return mock.patch.object(AC, "build", fake)


def walk(o, key):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                yield v
            yield from walk(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v, key)


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
    def test_schema_and_keys(self):
        d = self.docs["matematica"]
        self.assertEqual(SX.SCHEMA, "cloud.school.study_context/1")
        self.assertEqual(list(d), ["schema", "natura", "materia", "riferimento_temporale", "stato_input", "obiettivi", "lezioni",
                                   "comunicazioni", "da_verificare", "materiali", "lacune_informative", "ambiguita",
                                   "informazione_insufficiente", "affidabilita_minima", "storico", "politica", "provenance", "regole_agente"])
        self.assertIn("NON e' uno Study Assistant", d["natura"])
        self.assertTrue(SX.study_context_version().startswith("r1-"))

    def test_materia_required_no_overview(self):
        for bad in (None, "", "   ", 3):
            with self.assertRaises(ValueError):
                build(self.reg, bad)
        with self.assertRaises(ValueError):
            build(self.reg, "chimica")
        with self.assertRaises(ValueError):
            build(self.reg, "matematica", dettaglio="si")

    def test_all_eight_subjects(self):
        self.assertEqual(sorted(self.docs), sorted(SC.AGENT_SUBJECTS))
        for k, d in self.docs.items():
            self.assertEqual(d["materia"]["chiave"], k)
            self.assertEqual(d["riferimento_temporale"]["anno_operativo"], "2026-27")
            self.assertIn(d["stato_input"]["obiettivi"], SX.OBJECTIVES_STATE)

    def test_objectives_one_to_one_with_m10(self):
        for k, d in self.docs.items():
            m10 = AC.build(self.reg, NOW, manual=MAN, materia=k, subjects=SUBJ, dettaglio=True)["materie"][0]
            exp = [(x["target"], x["id"]) for x in m10["azioni"] if x["azione"] in SX.OBJECTIVE_ACTIONS]
            self.assertEqual([(o["uid"], o["provenienza"]["azione_m10"]) for o in d["obiettivi"]], exp, k)
            for o, x in zip(d["obiettivi"], [x for x in m10["azioni"] if x["azione"] in SX.OBJECTIVE_ACTIONS]):
                self.assertEqual((o["priorita_m9"], o["eseguibilita_m10"], o["affidabilita"]),
                                 (x["priorita_m9"], x["eseguibilita"], x["affidabilita"]))     # riportati come ricevuti

    def test_data_and_priority_as_received_no_days(self):
        o = obj(self.docs["matematica"], "cvv:1000101")
        self.assertEqual((o["data"], o["priorita_m9"]), ("2026-11-02", "alta"))
        self.assertNotRegex(pretty(self.docs["matematica"]), r'"giorni')

    def test_closed_vocabularies(self):
        for d in self.docs.values():
            for v in walk(d, "stato_dato"):
                self.assertIn(v, SX.DATA_STATES)
            for v in walk(d, "esito"):
                self.assertIn(v, SX.OUTCOMES)
            for v in walk(d, "motivo_non_disponibile"):
                self.assertIn(v, SX.UNAVAILABLE_REASONS)
            for i in d["informazione_insufficiente"]:
                self.assertEqual(i["messaggio"], SX.INSUFFICIENT[i["codice"]])
            for o in d["obiettivi"]:
                self.assertEqual(list(o["usi"]), list(SX.USES))
                for blk in (o["testo_dichiarato"], o["collegamenti_m5"]):
                    self.assertIn(blk["stato"], SX.DATA_STATES)

    def test_no_forbidden_words_in_generated_text(self):
        rx = re.compile(r"\b(%s)\b" % "|".join(re.escape(w) for w in SX.FORBIDDEN_WORDS), re.I)
        for txt in list(SX.RULES.values()) + list(SX.INSUFFICIENT.values()) + SX.RULES_FOR_AGENTS + [SX.NATURA]:
            self.assertIsNone(rx.search(txt), txt)
        self.assertNotIn("sufficiente per studiare", pretty(self.docs).replace("Non usare", ""))


# ------------------------------------------------------------------------------------------------ stati del dato e usi
class StatiEUsi(Base):
    def test_five_states_present(self):
        d = self.docs["matematica"]
        o = obj(d, "cvv:1000101")
        self.assertEqual(d["lezioni"]["elenco"][0]["stato"], "dato_disponibile")
        self.assertEqual(o["testo_dichiarato"]["stato"], "dato_rilevante")
        self.assertEqual(o["preparazione"][0]["stato"], "dato_rilevante")
        self.assertEqual(o["usi"]["identificare_obiettivo"]["stato_dato"], "dato_sufficiente")
        self.assertEqual(o["usi"]["argomenti_dell_obiettivo"]["stato_dato"], "non_determinabile")
        comp = obj(d, A("C-MF", "w-mat-open"))
        self.assertEqual(comp["collegamenti_m5"]["stato"], "non_determinabile")      # compito: collegamenti non esposti

    def test_three_unavailable_reasons(self):
        # fonte_non_letta
        with mock.patch.object(R.Registry, "freshness", fresh_fake(none_kinds=("classeviva/lesson",))):
            d = build(self.reg, "matematica")
        self.assertEqual((d["lezioni"]["stato"], d["lezioni"]["motivo"], d["lezioni"]["n"]), ("non_disponibile", "fonte_non_letta", None))
        self.assertIn("fonte_non_letta", [i["codice"] for i in d["informazione_insufficiente"]])
        # vuoto_valido
        reg = F.make_registry(lessons=[])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            e = build(reg, "matematica")
        self.assertEqual((e["lezioni"]["stato"], e["lezioni"]["motivo"], e["lezioni"]["n"]), ("non_disponibile", "vuoto_valido", 0))
        reg.close()
        # non_presente_nella_fonte (lezione senza argomento)
        reg = F.make_registry(lessons=F.LF.LESSONS + [lesson(7, "MATEMATICA", "ROSSI MARIO", "2026-10-30", "")])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            f = build(reg, "matematica")
        les = next(l for l in f["lezioni"]["elenco"] if l["uid"] == "cvv:lesson:7")
        self.assertEqual((les["argomento"]["stato"], les["argomento"]["motivo"]), ("non_disponibile", "non_presente_nella_fonte"))
        self.assertEqual(f["lezioni"]["senza_argomento"], 1)
        self.assertIn("lezioni_senza_argomento", [i["codice"] for i in f["informazione_insufficiente"]])
        reg.close()

    def test_u4_always_non_determinabile(self):
        n = 0
        for d in self.docs.values():
            for o in d["obiettivi"]:
                u = o["usi"]["argomenti_dell_obiettivo"]
                self.assertEqual((u["esito"], u["affidabilita"]), ("non_determinabile", "non_determinabile"))
                self.assertIn({"codice": "argomenti_obiettivo", "ambito": "obiettivo", "uid": o["uid"],
                               "messaggio": SX.INSUFFICIENT["argomenti_obiettivo"]}, d["informazione_insufficiente"])
                n += 1
        self.assertGreater(n, 0)

    def test_ariosto_u3_non_determinabile(self):
        o = obj(self.docs["italiano"], "cvv:1000104")
        self.assertEqual(o["collegamenti_m5"]["copertura"], "non_esposti_da_m9")
        self.assertEqual(o["usi"]["materiale_collegato_identificabile"]["esito"], "non_determinabile")
        self.assertEqual(o["preparazione"], [])
        self.assertIn("collegamenti_non_esposti", [i["codice"] for i in self.docs["italiano"]["informazione_insufficiente"]])

    def test_matematica_u3_satisfied_media(self):
        o = obj(self.docs["matematica"], "cvv:1000101")
        u = o["usi"]["materiale_collegato_identificabile"]
        p = o["preparazione"][0]
        # affidabilita' = minimo tra obiettivo e preparazione (sui dati reali: media; nelle fixture S6 e' piu' bassa)
        self.assertEqual((u["esito"], u["affidabilita"]),
                         ("soddisfatto", ST.RELIABILITY[max(LV[o["affidabilita"]], LV[p["affidabilita"]])]))
        self.assertNotEqual(u["affidabilita"], "alta")
        self.assertEqual((p["stato_collegamento"], p["materiale"]["uid"]), ("probabile", A("C-MF", "w-sim")))
        self.assertIn("non_confermato", p["certezza"])

    def test_u1_u2_u3_other_outcomes(self):
        def empty_text(out):
            for x in out["materie"][0]["azioni"]:
                if x["target"] == "cvv:1000101" and x["azione"] == "controllare_verifica":
                    x["contesto"]["testo_verifica"] = ""
                    x["contesto"]["collegamenti_m5"] = []
                if x["azione"] == "aprire_preparazione":
                    x["voce_m9"] = "altro"
        with patched_m10(empty_text):
            o = obj(build(self.reg, "matematica"), "cvv:1000101")
        self.assertEqual(o["usi"]["testo_dichiarato_disponibile"]["esito"], "non_soddisfatto")
        self.assertEqual((o["testo_dichiarato"]["stato"], o["testo_dichiarato"]["motivo"]), ("non_disponibile", "non_presente_nella_fonte"))
        self.assertEqual(o["usi"]["materiale_collegato_identificabile"]["esito"], "non_soddisfatto")   # esposti e nessuno
        self.assertEqual((o["collegamenti_m5"]["stato"], o["collegamenti_m5"]["motivo"]), ("non_disponibile", "vuoto_valido"))

        def unresolved(out):
            for x in out["materie"][0]["azioni"]:
                if x["target"] == "cvv:1000101" and x["azione"] == "controllare_verifica":
                    x["contesto"], x["eseguibilita"], x["affidabilita"] = {}, "non_eseguibile", "non_determinabile"
                    x["provenienza"]["m7"] = None
        with patched_m10(unresolved):
            d = build(self.reg, "matematica")
        o = obj(d, "cvv:1000101")
        self.assertEqual(o["usi"]["identificare_obiettivo"]["esito"], "non_determinabile")
        self.assertEqual(o["usi"]["testo_dichiarato_disponibile"]["esito"], "non_determinabile")
        self.assertEqual(o["testo_dichiarato"]["stato"], "non_determinabile")
        codes = [i["codice"] for i in d["informazione_insufficiente"]]
        self.assertIn("obiettivo_non_identificabile", codes)
        self.assertIn("azione_non_completa", codes)

    def test_partial_action_agenda_task(self):
        o = obj(self.docs["matematica"], "cvv:3000001")
        self.assertEqual((o["tipo"], o["eseguibilita_m10"]), ("compito", "parzialmente_eseguibile"))
        self.assertEqual(o["usi"]["identificare_obiettivo"]["esito"], "soddisfatto")
        self.assertIn({"codice": "stato_consegna_non_disponibile", "effetto": "parzialmente_eseguibile"}, o["lacune_m10"])

    def test_overdue_task_prudent_wording(self):
        o = obj(self.docs["inglese"], A("C-EN", "w-en-late"))
        self.assertIn("non consegnato secondo Classroom", o["compito"]["formulazione"])

    def test_objectives_state(self):
        self.assertEqual(self.docs["matematica"]["stato_input"]["obiettivi"], "presenti")
        self.assertEqual(self.docs["storia"]["stato_input"]["obiettivi"], "nessuno_nel_perimetro")
        with mock.patch.object(R.Registry, "freshness", fresh_fake(none_kinds=("classeviva/agenda_event", "classroom/course_work",
                                                                                "classroom/announcement"))):
            d = build(self.reg, "storia")
        self.assertEqual(d["stato_input"]["obiettivi"], "non_determinabile")
        self.assertIn("obiettivi_non_determinabili", [i["codice"] for i in d["informazione_insufficiente"]])


# ------------------------------------------------------------------------------------------------ nessuna inferenza
class NessunaInferenza(Base):
    def test_lessons_never_relevant_to_objectives(self):
        for d in self.docs.values():
            les = {l["uid"] for l in d["lezioni"]["elenco"]}
            for o in d["obiettivi"]:
                self.assertFalse(les & set(re.findall(r'"(cvv:lesson:\d+)"', pretty(o))))
            for l in d["lezioni"]["elenco"]:
                self.assertEqual(l["stato"], "dato_disponibile")                          # mai dato_rilevante

    def test_no_invented_topics_or_materials(self):
        d = self.docs["matematica"]
        lesson_args = {l["argomento"]["valore"] for l in d["lezioni"]["elenco"] if l["argomento"]["stato"] == "dato_disponibile"}
        for o in d["obiettivi"]:
            txt = pretty(o)
            for a in lesson_args:
                self.assertNotIn(a, txt)
            self.assertNotIn("argomenti", o)                                                 # nessun campo di argomenti estratti
        m10 = AC.build(self.reg, NOW, manual=MAN, materia="matematica", subjects=SUBJ, dettaglio=True)["materie"][0]
        allowed = {(x["contesto"].get("materiale") or {}).get("uid") for x in m10["azioni"] if x["azione"] == "aprire_preparazione"}
        self.assertTrue({m["uid"] for m in d["materiali"]["collegati"]} <= allowed)
        self.assertIn("non stabilisce se un materiale serve o basta", d["materiali"]["nota"])

    def test_no_m4_materials(self):
        self.assertNotIn("school_agent", open(SX.__file__, encoding="utf-8").read().split('"""', 2)[2])
        import school_agent as SA
        with mock.patch.object(SA, "materials", side_effect=AssertionError("M4 materials usati")), \
                mock.patch.object(C3.SC, "_classroom_section", side_effect=AssertionError("M4 classroom usato")):
            d = build(self.reg, "matematica")
        self.assertEqual(len(d["materiali"]["collegati"]), 1)
        self.assertEqual(d["materiali"]["altri_materiali"]["stato"], "non_determinabile")

    def test_history_excluded(self):
        reg = F.make_registry(announcements=F.ANNOUNCEMENTS + [ann("C-MF", "a-old-ver", "Verifica del 6 novembre 2025", "2025-10-01")],
                              lessons=F.LF.LESSONS + [lesson(8, "MATEMATICA", "ROSSI MARIO", "2025-10-01", "Limiti notevoli")])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            d = build(reg, "matematica")
        self.assertNotIn("cvv:lesson:8", [l["uid"] for l in d["lezioni"]["elenco"]])
        self.assertNotIn("Limiti notevoli", pretty({k: v for k, v in d.items() if k != "storico"}))
        self.assertTrue(all(o["data"] >= "2026-09-29" for o in d["obiettivi"]))
        self.assertIn("2025-26", d["storico"]["anni"])
        reg.close()


# ------------------------------------------------------------------------------------------------ affidabilita'
class Affidabilita(Base):
    def test_never_above_sources(self):
        for d in self.docs.values():
            for o in d["obiettivi"]:
                for u in o["usi"].values():
                    self.assertGreaterEqual(LV[u["affidabilita"]], LV[o["affidabilita"]] if u["esito"] != "non_determinabile" else 0)
                for p in o["preparazione"]:
                    self.assertGreaterEqual(LV[o["usi"]["materiale_collegato_identificabile"]["affidabilita"]], LV[p["affidabilita"]])
            s4 = next(s for s in ST.build(self.reg, d["materia"]["chiave"], NOW, manual=MAN, subjects=SUBJ, dettaglio=True)["segnali"]
                      if s["id"] == "S4")
            for l in d["lezioni"]["elenco"]:
                self.assertGreaterEqual(LV[l["affidabilita"]], LV[s4["affidabilita"]])

    def test_lesson_m2_discordance_degrades(self):
        reg = F.make_registry(lessons=F.LF.LESSONS + [lesson(9, "MATEMATICA", "ROSSI MARIO", "2026-10-30", "Leggi di Keplero e gravitazione")])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            d = build(reg, "matematica")
        l = next(l for l in d["lezioni"]["elenco"] if l["uid"] == "cvv:lesson:9")
        self.assertEqual((l["affidabilita"], l.get("discordanza_m2")), ("bassa", True))
        self.assertEqual(d["lezioni"]["con_discordanza_m2"], 1)
        reg.close()

    def test_monotone_property_on_m10(self):
        base = {k: self.docs[k] for k in ("matematica", "inglese", "italiano")}
        for lvl in ("media", "bassa", "non_determinabile"):
            def degrade(out, lvl=lvl):
                for x in out["materie"][0]["azioni"]:
                    x["affidabilita"] = ST.RELIABILITY[max(LV[x["affidabilita"]], LV[lvl])]
            for k, ref in base.items():
                with patched_m10(degrade):
                    d = build(self.reg, k)
                for o_ref, o in zip(ref["obiettivi"], d["obiettivi"]):
                    self.assertGreaterEqual(LV[o["affidabilita"]], LV[o_ref["affidabilita"]])
                    for u in SX.USES:
                        self.assertGreaterEqual(LV[o["usi"][u]["affidabilita"]], LV[o_ref["usi"][u]["affidabilita"]])
                if d["affidabilita_minima"] and ref["affidabilita_minima"]:
                    self.assertGreaterEqual(LV[d["affidabilita_minima"]], LV[ref["affidabilita_minima"]])

    def test_monotone_end_to_end_stale(self):
        with mock.patch.object(R.Registry, "freshness", fresh_fake(stale_kinds=("classeviva/agenda_event", "classeviva/lesson"))):
            d = build(self.reg, "matematica")
        ref = self.docs["matematica"]
        for o_ref, o in zip(ref["obiettivi"], d["obiettivi"]):
            self.assertGreaterEqual(LV[o["affidabilita"]], LV[o_ref["affidabilita"]])
        self.assertGreaterEqual(LV[d["lezioni"]["affidabilita"]], LV[ref["lezioni"]["affidabilita"]])


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
        src = open(SX.__file__, encoding="utf-8").read()
        for bad in ("datetime.now", "time.time", "date.today", "utcnow"):
            self.assertNotIn(bad, src)

    def test_inputs_not_mutated(self):
        reg = F.make_registry()
        seen = {}
        orig8 = ST.build

        def a10(*a, **kw):
            out = _AC_BUILD(*a, **kw)
            seen["m10"] = (out, pretty(out))
            return out

        def a8(*a, **kw):
            out = orig8(*a, **kw)
            seen.setdefault("m8", []).append((out, pretty(out)))
            return out
        with mock.patch.object(R.Registry, "freshness", fresh_fake()), mock.patch.object(AC, "build", a10), \
                mock.patch.object(ST, "build", a8):
            build(reg, "matematica", dettaglio=True)
        self.assertEqual(pretty(seen["m10"][0]), seen["m10"][1])
        for out, s in seen["m8"]:
            self.assertEqual(pretty(out), s)
        reg.close()

    def test_no_writes_and_m2_m10_unchanged(self):
        reg = F.make_registry()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            snap = lambda: [pretty(C3.build(reg, "matematica", SUBJ, NOW, manual=MAN)), pretty(C3.overview(reg, SUBJ, NOW, manual=MAN)),  # noqa: E731
                            pretty(L.build(reg, SUBJ, NOW, manual=MAN)), pretty(M.build(reg, SUBJ, NOW, manual=MAN)),
                            pretty(C4.build(reg, "inglese", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(ST.build(reg, "scienze", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(PR.build(reg, NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(AC.build(reg, NOW, manual=MAN, subjects=SUBJ, dettaglio=True))]
            before, c0 = snap(), counts(reg)
            for k in SC.AGENT_SUBJECTS:
                build(reg, k, dettaglio=True)
            self.assertEqual((before, c0), (snap(), counts(reg)))
        reg.close()

    def test_build_from_path_writes_nothing(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "registry.sqlite3")
        F.make_registry(path=p).close()
        h = lambda q: hashlib.sha256(open(q, "rb").read()).hexdigest()                 # noqa: E731
        s0, f0, h0 = dry._fs_state(p), sorted(os.listdir(d)), h(p)
        out = SX.build_from_path(p, "fisica", NOW, subjects=SUBJ)
        self.assertEqual(out["schema"], SX.SCHEMA)
        self.assertEqual((s0, f0, h0), (dry._fs_state(p), sorted(os.listdir(d)), h(p)))

    def test_detail_adds_full_m10_action(self):
        reg = F.make_registry()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            d = build(reg, "matematica", dettaglio=True)
        for o in d["obiettivi"]:
            self.assertEqual(o["azione_m10_completa"]["id"], o["provenienza"]["azione_m10"])
        reg.close()

    def test_provenance_chain(self):
        reg = F.make_registry()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            d = build(reg, "matematica")
            d8 = ST.build(reg, "matematica", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)
        m8ev = {(s["id"], e["uid"]): e["hash"] for s in d8["segnali"] for e in s["evidenze"] if "uid" in e}
        for o in d["obiettivi"]:
            self.assertEqual(o["hash"], o["provenienza"]["m7"]["hash"])
            for e in o["provenienza"]["m8"]:
                self.assertEqual(m8ev[(e["segnale"], e["uid"])], e["hash"])
        for l in d["lezioni"]["elenco"]:
            self.assertEqual(m8ev[("S4", l["uid"])], l["hash"])
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
        t = self.server.srv._tools["school_study_context_v1"]["def"]
        self.assertEqual(set(t["inputSchema"]["properties"]), {"materia", "dettaglio", "soglia_ore"})
        self.assertEqual(t["inputSchema"]["required"], ["materia"])
        self.assertIn("SOLA LETTURA", t["description"])

    def test_missing_registry(self):
        with self.assertRaises(ValueError):
            self.server.school_study_context_v1("matematica")
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
            out = self.server.school_study_context_v1("matematica")
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["riferimento_temporale"]["now"], "2026-11-01T12:00:00+02:00")
        self.assertFalse(out["verifica_nessuna_scrittura"]["file_modificati"])
        self.assertEqual(h0, hashlib.sha256(open(self.db, "rb").read()).hexdigest())


if __name__ == "__main__":
    unittest.main()
