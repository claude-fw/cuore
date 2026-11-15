"""M12 v1 - school_study_assistant/1 (cloud.school.study_assistant/1): orchestratore/guardrail sopra M11. Struttura, richiesta
(materia obbligatoria, obiettivo esatto o non trovato, modalita' valida/non disponibile/sconosciuta/assente), le 5 modalita' con
disponibile/limitata/non_disponibile, chiarimento sempre disponibile, guardrail chiusi, contenuti solo da M11 (uid/hash), nessuna fusione
lezioni<->obiettivi, mancante/vuoto/ambiguo, affidabilita' monotona, storico escluso, determinismo, nessuna scrittura, clock una volta,
input M11 non mutato, 8 materie (casi modellati sui dati reali), M1-M11 invariati."""
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
import school_study_assistant as SA
import school_study_context as SX
from links_fixtures import ann, lesson
from test_school_context4 import fresh_fake, pretty, counts

SUBJ, NOW, MAN = F.SUBJECTS, F.NOW, F.MANUAL
A = lambda c, a: "cls:%s/%s" % (c, a)            # noqa: E731
LV = {k: i for i, k in enumerate(ST.RELIABILITY)}
_SX_BUILD = SX.build


def build(reg, materia, **kw):
    kw.setdefault("manual", MAN)
    return SA.build(reg, materia, kw.pop("now", NOW), subjects=kw.pop("subjects", SUBJ), **kw)


def mode(d, name):
    return next(m for m in d["modalita"] if m["modalita"] == name)


def patched_m11(mutate):
    def fake(*a, **kw):
        out = copy.deepcopy(_SX_BUILD(*a, **kw))
        mutate(out)
        return out
    return mock.patch.object(SX, "build", fake)


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


# ------------------------------------------------------------------------------------------------ struttura e richiesta
class StrutturaERichiesta(Base):
    def test_schema_keys_and_constants(self):
        d = self.docs["matematica"]
        self.assertEqual(SA.SCHEMA, "cloud.school.study_assistant/1")
        self.assertEqual(list(d), ["schema", "natura", "richiesta", "esito_richiesta", "riferimento_temporale", "stato_input_m11", "modalita",
                                   "disponibilita", "non_determinabile", "informazione_insufficiente", "politica", "provenance",
                                   "regole_agente"])
        self.assertEqual(SA.MODES, ("quadro_obiettivo", "materiale_collegato", "rassegna_lezioni", "comunicazioni_da_leggere",
                                    "chiarimento_limiti"))
        self.assertIn("nessun testo didattico", d["natura"])
        self.assertTrue(SA.study_assistant_version().startswith("r1-"))
        for m in d["modalita"]:
            self.assertEqual(list(m), ["modalita", "disponibilita", "motivo", "requisiti", "affidabilita", "contenuti", "limiti",
                                       "azioni_ammesse", "azioni_vietate", "frasi_obbligatorie"])

    def test_materia_required_and_invalid_inputs(self):
        for bad in (None, "", "  "):
            with self.assertRaises(ValueError):
                build(self.reg, bad)
        with self.assertRaises(ValueError):
            build(self.reg, "chimica")
        with self.assertRaises(ValueError):
            build(self.reg, "matematica", modalita="spiegazione")                      # modalita' sconosciuta
        with self.assertRaises(ValueError):
            build(self.reg, "matematica", dettaglio="si")
        with self.assertRaises(ValueError):
            build(self.reg, "matematica", obiettivo="  ")

    def test_no_mode_requested_lists_all_five_in_fixed_order(self):
        d = self.docs["matematica"]
        self.assertEqual(d["esito_richiesta"]["stato"], "nessuna_modalita_richiesta")
        self.assertEqual([m["modalita"] for m in d["modalita"]], list(SA.MODES))
        self.assertEqual(list(d["disponibilita"]), list(SA.MODES))
        self.assertNotIn("consigliata", pretty(d))

    def test_requested_available_mode(self):
        d = build(self.reg, "matematica", modalita="quadro_obiettivo")
        self.assertEqual(d["esito_richiesta"]["stato"], "ammessa")
        self.assertEqual([m["modalita"] for m in d["modalita"]], ["quadro_obiettivo"])
        self.assertTrue(d["modalita"][0]["contenuti"])

    def test_requested_unavailable_mode_no_fallback(self):
        d = build(self.reg, "italiano", modalita="materiale_collegato")
        e = d["esito_richiesta"]
        self.assertEqual(e["stato"], "non_disponibile")
        self.assertTrue(e["motivo"])
        self.assertEqual(d["modalita"][0]["contenuti"], [])
        for alt in e["alternative"]:
            self.assertNotEqual(d["disponibilita"][alt], "non_disponibile")
        self.assertNotIn("materiale_collegato", e["alternative"])
        self.assertIn("modalita_non_disponibile", [i["codice"] for i in d["informazione_insufficiente"]])

    def test_objective_exact_match_and_not_found(self):
        d = build(self.reg, "matematica", obiettivo="cvv:1000101")
        q = mode(d, "quadro_obiettivo")
        self.assertEqual([c["uid"] for c in q["contenuti"]], ["cvv:1000101"])
        nf = build(self.reg, "matematica", obiettivo="cvv:265450")                   # prefisso: NESSUNA ricerca approssimata
        self.assertEqual(nf["esito_richiesta"]["stato"], "obiettivo_non_trovato")
        self.assertIn("cvv:1000101", nf["esito_richiesta"]["obiettivi_esistenti"])
        self.assertEqual(nf["disponibilita"]["quadro_obiettivo"], "non_disponibile")
        self.assertEqual(nf["disponibilita"]["materiale_collegato"], "non_disponibile")
        self.assertIn("obiettivo_non_trovato", [i["codice"] for i in nf["informazione_insufficiente"]])

    def test_closed_vocabularies_and_guardrails(self):
        for d in self.docs.values():
            for m in d["modalita"]:
                self.assertIn(m["disponibilita"], SA.AVAILABILITY)
                self.assertTrue(set(m["azioni_ammesse"]) <= set(SA.ALLOWED))
                self.assertEqual(m["azioni_vietate"], list(SA.FORBIDDEN_ACTIONS))
                for f in m["frasi_obbligatorie"]:
                    self.assertEqual(f["frase"], SA.PHRASES[f["codice"]])
                for c in m["contenuti"]:
                    self.assertIn(c["stato_dato"], SA.ADMITTED_STATES)
            self.assertIn(d["esito_richiesta"]["stato"], SA.REQUEST_OUTCOMES)
        for a in ("spiegare_argomento", "stimare_tempo", "proporre_piano", "valutare_preparazione", "collegare_lezione_obiettivo"):
            self.assertIn(a, SA.FORBIDDEN_ACTIONS)

    def test_no_forbidden_words_in_generated_text(self):
        rx = re.compile(r"\b(%s)\b" % "|".join(re.escape(w) for w in SA.FORBIDDEN_WORDS), re.I)
        for txt in list(SA.PHRASES.values()) + list(SA.INSUFFICIENT_M12.values()) + list(SA.REASONS.values()) + SA.RULES_FOR_AGENTS + [SA.NATURA]:
            self.assertIsNone(rx.search(txt), txt)


# ------------------------------------------------------------------------------------------------ le 5 modalita'
class Modalita(Base):
    def test_quadro_obiettivo(self):
        q = mode(self.docs["matematica"], "quadro_obiettivo")
        self.assertEqual(q["disponibilita"], "limitata")                                 # compito di agenda: azione M10 parziale
        c = next(c for c in q["contenuti"] if c["uid"] == "cvv:1000101")
        self.assertEqual((c["campi"]["data"], c["campi"]["priorita_m9"], c["campi"]["testo_dichiarato"]), ("2026-11-02", "alta", "Verifica Matematica"))
        self.assertIn("argomenti_non_determinati", [f["codice"] for f in q["frasi_obbligatorie"]])
        q2 = mode(build(self.reg, "matematica", obiettivo="cvv:1000101"), "quadro_obiettivo")
        self.assertEqual(q2["disponibilita"], "disponibile")
        self.assertEqual(mode(self.docs["storia"], "quadro_obiettivo")["disponibilita"], "non_disponibile")

    def test_materiale_collegato_limited_without_title(self):
        m = mode(self.docs["matematica"], "materiale_collegato")
        self.assertEqual(m["disponibilita"], "limitata")
        c = m["contenuti"][0]
        self.assertEqual((c["uid"], c["campi"]["stato_collegamento"], c["campi"]["titolo"]), (A("C-MF", "w-sim"), "probabile", None))
        self.assertEqual((c["hash"], c["hash_esposto_da_m11"]), (None, False))
        self.assertEqual(c["provenienza"]["obiettivo"], "cvv:1000101")
        self.assertEqual(set(c["campi"]), {"tipo_materiale", "data", "stato_collegamento", "relazione", "titolo"})
        codes = [f["codice"] for f in m["frasi_obbligatorie"]]
        self.assertIn("titolo_non_disponibile", codes)
        self.assertIn("collegamento_non_confermato", codes)
        self.assertIn("materiale_senza_titolo", [i["codice"] for i in self.docs["matematica"]["informazione_insufficiente"]])

    def test_materiale_ariosto_non_disponibile(self):
        m = mode(self.docs["italiano"], "materiale_collegato")
        self.assertEqual((m["disponibilita"], m["contenuti"]), ("non_disponibile", []))
        self.assertEqual(m["requisiti"][0]["esito"], "non_determinabile")

    def test_rassegna_lezioni_states(self):
        r = mode(self.docs["matematica"], "rassegna_lezioni")
        self.assertEqual((r["disponibilita"], len(r["contenuti"])), ("disponibile", 4))
        self.assertIn("lezioni_non_collegate", [f["codice"] for f in r["frasi_obbligatorie"]])
        self.assertEqual(mode(self.docs["latino"], "rassegna_lezioni")["disponibilita"], "non_disponibile")    # nessuna lezione
        reg = F.make_registry(lessons=F.LF.LESSONS + [lesson(7, "MATEMATICA", "ROSSI MARIO", "2026-10-30", "")])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            r2 = mode(build(reg, "matematica"), "rassegna_lezioni")
        self.assertEqual((r2["disponibilita"], len(r2["contenuti"])), ("limitata", 4))   # una lezione senza argomento: esclusa, dichiarata
        self.assertIn({"codice": "lezioni_senza_argomento_n", "ambito": "materia", "n": 1}, r2["limiti"])
        reg.close()
        reg = F.make_registry(lessons=[lesson(7, "MATEMATICA", "ROSSI MARIO", "2026-10-30", "")])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            r3 = mode(build(reg, "matematica"), "rassegna_lezioni")
        self.assertEqual((r3["disponibilita"], r3["contenuti"]), ("non_disponibile", []))
        reg.close()

    def test_rassegna_never_derives_topics(self):
        d = self.docs["matematica"]
        r = mode(d, "rassegna_lezioni")
        m11 = SX.build(self.reg, "matematica", NOW, manual=MAN, subjects=SUBJ)
        recorded = {l["argomento"]["valore"] for l in m11["lezioni"]["elenco"] if l["argomento"]["stato"] == "dato_disponibile"}
        self.assertEqual({c["campi"]["argomento"] for c in r["contenuti"]}, recorded)
        self.assertNotIn("Verifica Matematica", pretty(r["contenuti"]))

    def test_comunicazioni(self):
        c = mode(self.docs["inglese"], "comunicazioni_da_leggere")
        self.assertIn(c["disponibilita"], ("disponibile", "limitata"))
        self.assertTrue(all(x["hash"] is None and x["hash_esposto_da_m11"] is False for x in c["contenuti"]))
        self.assertEqual(mode(self.docs["matematica"], "comunicazioni_da_leggere")["disponibilita"], "non_disponibile")
        long = "Per domani leggete " + "pagina " * 40 + "fine"
        reg = F.make_registry(announcements=F.ANNOUNCEMENTS + [ann("C-SC", "a-long", long, "2026-10-31")])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            c2 = mode(build(reg, "scienze"), "comunicazioni_da_leggere")
        self.assertEqual(c2["disponibilita"], "limitata")
        self.assertIn("comunicazione_troncata", [f["codice"] for f in c2["frasi_obbligatorie"]])
        reg.close()

    def test_chiarimento_always_available(self):
        for d in self.docs.values():
            ch = mode(d, "chiarimento_limiti")
            self.assertEqual((ch["disponibilita"], ch["contenuti"]), ("disponibile", []))
        ch = mode(self.docs["scienze"], "chiarimento_limiti")
        self.assertIn("da_verificare", [l["codice"] for l in ch["limiti"]])           # indizio P1b
        with mock.patch.object(R.Registry, "freshness", fresh_fake(none_kinds=("classeviva/agenda_event", "classroom/course_work",
                                                                                "classroom/announcement", "classeviva/lesson"))):
            d = build(self.reg, "storia")
        self.assertEqual(mode(d, "chiarimento_limiti")["disponibilita"], "disponibile")
        self.assertEqual([m for m in SA.MODES if d["disponibilita"][m] != "non_disponibile"], ["chiarimento_limiti"])
        self.assertIn("obiettivi_non_determinabili", [i["codice"] for i in d["informazione_insufficiente"]])

    def test_unavailable_modes_have_no_content(self):
        for d in self.docs.values():
            for m in d["modalita"]:
                if m["disponibilita"] == "non_disponibile":
                    self.assertEqual((m["contenuti"], m["affidabilita"]), ([], None))
                    self.assertTrue(m["motivo"])

    def test_lessons_and_objectives_never_mixed(self):
        for d in self.docs.values():
            for name in ("quadro_obiettivo", "materiale_collegato"):
                self.assertFalse([c for c in mode(d, name)["contenuti"] if c["tipo"] == "lezione"])
                self.assertNotIn("cvv:lesson:", pretty(mode(d, name)["contenuti"]))
            obj_uids = {c["uid"] for c in mode(d, "quadro_obiettivo")["contenuti"]}
            r = mode(d, "rassegna_lezioni")
            self.assertFalse([c for c in r["contenuti"] if c["tipo"] != "lezione"])
            self.assertFalse(obj_uids & set(re.findall(r'"(cvv:\d+|cls:[^"]+)"', pretty(r["contenuti"]))))


# ------------------------------------------------------------------------------------------------ casi per materia (fixture modellate sui dati reali)
class CasiPerMateria(Base):
    def test_eight_subjects_availability(self):
        exp = {"matematica": ("limitata", "limitata", "disponibile", "non_disponibile"),
               "fisica": ("disponibile", "limitata", "non_disponibile", "non_disponibile"),
               "italiano": ("disponibile", "non_disponibile", "non_disponibile", "non_disponibile"),
               "inglese": None, "scienze": ("non_disponibile", "non_disponibile", "non_disponibile", None),
               "latino": ("non_disponibile", "non_disponibile", "non_disponibile", "non_disponibile"),
               "storia": ("non_disponibile", "non_disponibile", "disponibile", "non_disponibile"),
               "filosofia": ("non_disponibile", "non_disponibile", "non_disponibile", "non_disponibile")}
        for k, e in exp.items():
            d = self.docs[k]
            self.assertEqual(d["disponibilita"]["chiarimento_limiti"], "disponibile", k)
            if e is None:
                continue
            got = tuple(d["disponibilita"][m] for m in SA.MODES[:4])
            for g, x in zip(got, e):
                if x is not None:
                    self.assertEqual(g, x, (k, got))
        ing = self.docs["inglese"]
        self.assertNotEqual(ing["disponibilita"]["quadro_obiettivo"], "non_disponibile")
        self.assertNotEqual(ing["disponibilita"]["comunicazioni_da_leggere"], "non_disponibile")
        self.assertNotEqual(self.docs["scienze"]["disponibilita"]["comunicazioni_da_leggere"], "non_disponibile")

    def test_no_objective_distinction(self):
        self.assertIn("nessun_obiettivo_nel_perimetro", [i["codice"] for i in self.docs["latino"]["informazione_insufficiente"]])


# ------------------------------------------------------------------------------------------------ provenienza, affidabilita', storico
class ProvenienzaEAffidabilita(Base):
    def test_contents_trace_to_m11(self):
        for k, d in self.docs.items():
            m11 = SX.build(self.reg, k, NOW, manual=MAN, subjects=SUBJ)
            objs = {o["uid"]: o for o in m11["obiettivi"]}
            les = {l["uid"]: l for l in m11["lezioni"]["elenco"]}
            preps = {p["materiale"]["uid"] for o in m11["obiettivi"] for p in o["preparazione"]}
            comms = {c["uid"] for c in m11["comunicazioni"]["da_leggere"]}
            for m in d["modalita"]:
                for c in m["contenuti"]:
                    if c["tipo"] == "obiettivo":
                        self.assertEqual(objs[c["uid"]]["hash"], c["hash"])
                    elif c["tipo"] == "lezione":
                        self.assertEqual(les[c["uid"]]["hash"], c["hash"])
                    elif c["tipo"] == "materiale":
                        self.assertIn(c["uid"], preps)
                        self.assertEqual(objs[c["provenienza"]["obiettivo"]]["hash"], c["provenienza"]["obiettivo_hash"])
                    else:
                        self.assertIn(c["uid"], comms)

    def test_reliability_never_above_m11(self):
        for k, d in self.docs.items():
            for m in d["modalita"]:
                for c in m["contenuti"]:
                    self.assertGreaterEqual(LV[m["affidabilita"]], LV[c["affidabilita"]])

    def test_monotone_property(self):
        for lvl in ("media", "bassa", "non_determinabile"):
            def degrade(out, lvl=lvl):
                for o in out["obiettivi"]:
                    o["affidabilita"] = ST.RELIABILITY[max(LV[o["affidabilita"]], LV[lvl])]
                    for u in o["usi"].values():
                        u["affidabilita"] = ST.RELIABILITY[max(LV[u["affidabilita"]], LV[lvl])]
                    for p in o["preparazione"]:
                        p["affidabilita"] = ST.RELIABILITY[max(LV[p["affidabilita"]], LV[lvl])]
                for l in out["lezioni"]["elenco"]:
                    l["affidabilita"] = ST.RELIABILITY[max(LV[l["affidabilita"]], LV[lvl])]
                for c in out["comunicazioni"]["da_leggere"]:
                    c["affidabilita"] = ST.RELIABILITY[max(LV[c["affidabilita"]], LV[lvl])]
            for k in ("matematica", "inglese", "italiano", "scienze"):
                with patched_m11(degrade):
                    d = build(self.reg, k)
                ref = self.docs[k]
                for mr, md in zip(ref["modalita"], d["modalita"]):
                    if mr["affidabilita"] and md["affidabilita"]:
                        self.assertGreaterEqual(LV[md["affidabilita"]], LV[mr["affidabilita"]])
                        self.assertGreaterEqual(LV[md["affidabilita"]], LV[lvl])

    def test_monotone_end_to_end_stale(self):
        with mock.patch.object(R.Registry, "freshness", fresh_fake(stale_kinds=("classeviva/agenda_event", "classeviva/lesson",
                                                                                "classroom/announcement"))):
            d = build(self.reg, "matematica")
        for mr, md in zip(self.docs["matematica"]["modalita"], d["modalita"]):
            if mr["affidabilita"] and md["affidabilita"]:
                self.assertGreaterEqual(LV[md["affidabilita"]], LV[mr["affidabilita"]])

    def test_history_excluded(self):
        reg = F.make_registry(announcements=F.ANNOUNCEMENTS + [ann("C-SC", "a-old-ver", "Verifica del 6 novembre 2025", "2025-10-01")],
                              lessons=F.LF.LESSONS + [lesson(8, "MATEMATICA", "ROSSI MARIO", "2025-10-01", "Limiti notevoli")])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            for k in ("matematica", "scienze"):
                d = build(reg, k)
                txt = pretty(d["modalita"])
                self.assertNotIn("cvv:lesson:8", txt)
                self.assertNotIn("Limiti notevoli", txt)
                self.assertNotIn(A("C-SC", "a-old-ver"), txt)
        reg.close()

    def test_missing_vs_empty(self):
        reg = F.make_registry(lessons=[])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            r = mode(build(reg, "matematica"), "rassegna_lezioni")
        self.assertIn("vuoto_valido", r["motivo"])
        reg.close()
        with mock.patch.object(R.Registry, "freshness", fresh_fake(none_kinds=("classeviva/lesson",))):
            r = mode(build(self.reg, "matematica"), "rassegna_lezioni")
        self.assertIn("fonte_non_letta", r["motivo"])


# ------------------------------------------------------------------------------------------------ determinismo, scritture, tool
class DeterminismoESicurezza(unittest.TestCase):
    def test_byte_identical_and_order_independent(self):
        a, b = F.make_registry(), F.make_registry(reverse=True)
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            for k in SC.AGENT_SUBJECTS:
                for kw in ({}, {"dettaglio": True}, {"modalita": "rassegna_lezioni"}):
                    x1, x2, y = build(a, k, **kw), build(a, k, **kw), build(b, k, **kw)
                    self.assertEqual(pretty(x1), pretty(x2))
                    self.assertEqual(pretty(x1), pretty(y))
        a.close(); b.close()

    def test_builder_never_reads_clock(self):
        src = open(SA.__file__, encoding="utf-8").read()
        for bad in ("datetime.now", "time.time", "date.today", "utcnow"):
            self.assertNotIn(bad, src)

    def test_only_m11_contract_consumed(self):
        body = open(SA.__file__, encoding="utf-8").read().split('"""', 2)[2]
        for mod in ("school_action", "school_priority", "school_context4", "school_context3", "cvv_cls_links", "cvv_comms", "school_agent",
                    "cvv_attribution"):
            self.assertNotIn("import %s" % mod, body)

    def test_m11_input_not_mutated(self):
        reg = F.make_registry()
        seen = []

        def spy(*a, **kw):
            out = _SX_BUILD(*a, **kw)
            seen.append((out, pretty(out)))
            return out
        with mock.patch.object(R.Registry, "freshness", fresh_fake()), mock.patch.object(SX, "build", spy):
            build(reg, "matematica", dettaglio=True)
        self.assertEqual(pretty(seen[0][0]), seen[0][1])
        reg.close()

    def test_no_writes_and_m1_m11_unchanged(self):
        reg = F.make_registry()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            snap = lambda: [pretty(C3.build(reg, "matematica", SUBJ, NOW, manual=MAN)), pretty(C3.overview(reg, SUBJ, NOW, manual=MAN)),  # noqa: E731
                            pretty(L.build(reg, SUBJ, NOW, manual=MAN)), pretty(M.build(reg, SUBJ, NOW, manual=MAN)),
                            pretty(C4.build(reg, "inglese", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(ST.build(reg, "scienze", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(PR.build(reg, NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(AC.build(reg, NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(_SX_BUILD(reg, "matematica", NOW, manual=MAN, subjects=SUBJ, dettaglio=True))]
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
        out = SA.build_from_path(p, "fisica", NOW, subjects=SUBJ)
        self.assertEqual(out["schema"], SA.SCHEMA)
        self.assertEqual((s0, f0, h0), (dry._fs_state(p), sorted(os.listdir(d)), h(p)))

    def test_detail_adds_m11_elements(self):
        reg = F.make_registry()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            d = build(reg, "matematica", dettaglio=True)
        for m in d["modalita"]:
            for c in m["contenuti"]:
                self.assertIn("elemento_m11", c)
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
        t = self.server.srv._tools["school_study_assistant_v1"]["def"]
        self.assertEqual(set(t["inputSchema"]["properties"]), {"materia", "obiettivo", "modalita", "dettaglio", "soglia_ore"})
        self.assertEqual(t["inputSchema"]["required"], ["materia"])
        self.assertIn("SOLA LETTURA", t["description"])

    def test_missing_registry(self):
        with self.assertRaises(ValueError):
            self.server.school_study_assistant_v1("matematica")
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
            out = self.server.school_study_assistant_v1("matematica", modalita="quadro_obiettivo")
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["riferimento_temporale"]["now"], "2026-11-01T12:00:00+02:00")
        self.assertFalse(out["verifica_nessuna_scrittura"]["file_modificati"])
        self.assertEqual(h0, hashlib.sha256(open(self.db, "rb").read()).hexdigest())


if __name__ == "__main__":
    unittest.main()
