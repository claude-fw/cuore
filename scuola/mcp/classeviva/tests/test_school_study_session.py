"""M13 v1 - school_study_session/1 (cloud.school.study_session/1): orchestratore/guardrail della sessione di studio sopra M12. Le 6
modalita', contenuto vuoto/mancante, obiettivo valido/inesistente/senza argomenti, separazione contenuto scolastico / utente /
conoscenza generale, provenienza (sha256, uid/hash M12), guardrail e divieti, nessun programma inferito, nessun giudizio, contenuto
modificato -> provenienza coerente, input non mutato, determinismo, ordine stabile, nessuna scrittura, clock una volta, M1-M12 invariati."""
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
import school_study_session as SS
from test_school_context4 import fresh_fake, pretty, counts

SUBJ, NOW, MAN = F.SUBJECTS, F.NOW, F.MANUAL
A = lambda c, a: "cls:%s/%s" % (c, a)            # noqa: E731
TEXT = "Le funzioni irrazionali hanno la forma y = a^x con a > 0 e a diverso da 1."
_SA_BUILD = SA.build


def build(reg, materia, modalita, contenuto, **kw):
    kw.setdefault("manual", MAN)
    return SS.build(reg, materia, modalita, contenuto, kw.pop("now", NOW), subjects=kw.pop("subjects", SUBJ), **kw)


def codes(d):
    return [f["codice"] for f in d["sessione"]["frasi_obbligatorie"]]


def insuff(d):
    return [i["codice"] for i in d["esito"]["informazione_insufficiente"]]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = F.make_registry()
        cls.p = mock.patch.object(R.Registry, "freshness", fresh_fake())
        cls.p.start()

    @classmethod
    def tearDownClass(cls):
        cls.p.stop()
        cls.reg.close()


# ------------------------------------------------------------------------------------------------ struttura e input
class Struttura(Base):
    def test_schema_keys_and_constants(self):
        d = build(self.reg, "matematica", "spiega", TEXT)
        self.assertEqual(SS.SCHEMA, "cloud.school.study_session/1")
        self.assertEqual(list(d), ["schema", "natura", "materia", "obiettivo", "modalita", "esito", "contenuto", "contesto_scolastico",
                                   "sessione", "regole_agente", "provenance"])
        self.assertEqual(SS.MODES, ("spiega", "interroga", "esercitati", "correggi", "ripassa", "prepara_obiettivo"))
        self.assertEqual(SS.PROVENANCE, ("contenuto_scolastico", "contenuto_fornito_utente", "conoscenza_generale"))
        self.assertIn("non genera la risposta didattica", d["natura"])
        self.assertTrue(SS.study_session_version().startswith("r1-"))
        self.assertEqual(d["provenance"]["study_assistant_version"], SA.study_assistant_version())

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            build(self.reg, "", "spiega", TEXT)
        with self.assertRaises(ValueError):
            build(self.reg, "matematica", "riassumi", TEXT)                               # modalita' fuori elenco
        with self.assertRaises(ValueError):
            build(self.reg, "matematica", "spiega", 42)
        with self.assertRaises(ValueError):
            build(self.reg, "matematica", "spiega", {"testo": TEXT, "url": "http://x"})     # nessuna acquisizione esterna
        with self.assertRaises(ValueError):
            build(self.reg, "matematica", "spiega", {"riferimenti_m12": "cvv:lesson:1"})
        with self.assertRaises(ValueError):
            build(self.reg, "matematica", "spiega", TEXT, obiettivo="  ")
        with self.assertRaises(ValueError):
            build(self.reg, "chimica", "spiega", TEXT)

    def test_closed_vocabularies(self):
        for mode in SS.MODES:
            for content in (TEXT, None, {"testo": TEXT, "risposta_utente": "y = 2^x"}):
                d = build(self.reg, "matematica", mode, content, obiettivo="cvv:1000101" if mode == "prepara_obiettivo" else None)
                self.assertIn(d["esito"]["stato"], SS.OUTCOMES)
                allowed_all = set().union(*SS.MODE_ALLOWED.values(), SS.INSUFFICIENT_ALLOWED, SS.CONTEXT_ONLY_ALLOWED)
                self.assertTrue(set(d["sessione"]["azioni_ammesse"]) <= allowed_all)
                self.assertEqual(d["sessione"]["azioni_vietate"], list(SS.COMMON_FORBIDDEN) + list(SS.MODE_FORBIDDEN[mode]))
                for f in d["sessione"]["frasi_obbligatorie"]:
                    self.assertEqual(f["frase"], SS.PHRASES[f["codice"]])
                for p in d["contenuto"]["parti"]:
                    self.assertIn(p["provenienza"], SS.PROVENANCE)

    def test_no_judgment_words_in_generated_text(self):
        rx = re.compile(r"\b(%s)\b" % "|".join(re.escape(w) for w in SS.FORBIDDEN_WORDS), re.I)
        for txt in list(SS.PHRASES.values()) + list(SS.INSUFFICIENT.values()) + SS.RULES_FOR_AGENTS + [SS.NATURA] + list(SS.PROVENANCE_DEF.values()):
            self.assertIsNone(rx.search(txt), txt)


# ------------------------------------------------------------------------------------------------ le 6 modalita'
class Modalita(Base):
    def test_spiega(self):
        d = build(self.reg, "matematica", "spiega", TEXT)
        self.assertEqual(d["esito"]["stato"], "ammessa")
        self.assertIn("chiarire_con_conoscenza_generale_etichettata", d["sessione"]["azioni_ammesse"])
        self.assertIn("attribuire_conoscenza_generale_alla_scuola", d["sessione"]["azioni_vietate"])
        self.assertIn("conoscenza_generale", codes(d))
        self.assertIn("contenuto_utente", codes(d))

    def test_interroga(self):
        d = build(self.reg, "matematica", "interroga", TEXT)
        self.assertIn("generare_domande_sul_contenuto", d["sessione"]["azioni_ammesse"])
        self.assertIn("presentare_domande_come_programma", d["sessione"]["azioni_vietate"])
        self.assertIn("interroga", codes(d))

    def test_esercitati(self):
        d = build(self.reg, "fisica", "esercitati", TEXT)
        self.assertIn("generare_esercizi_sul_contenuto", d["sessione"]["azioni_ammesse"])
        self.assertIn("presentare_esercizi_come_richiesti_dalla_scuola", d["sessione"]["azioni_vietate"])
        self.assertIn("esercitati", codes(d))

    def test_correggi(self):
        d = build(self.reg, "matematica", "correggi", {"testo": TEXT, "risposta_utente": "y = (-2)^x e' irrazionale"})
        self.assertEqual(d["esito"]["stato"], "ammessa")
        self.assertEqual(d["contenuto"]["risposta_utente"]["ruolo"], "risposta_da_correggere")
        self.assertIn("giudizio_globale_dalla_correzione", d["sessione"]["azioni_vietate"])
        self.assertIn("correggi", codes(d))
        missing = build(self.reg, "matematica", "correggi", TEXT)                         # nessuna risposta
        self.assertEqual(missing["esito"]["stato"], "contenuto_insufficiente")
        self.assertIn("risposta_mancante", insuff(missing))
        noref = build(self.reg, "matematica", "correggi", {"risposta_utente": "2^3 = 6"})
        self.assertEqual(noref["esito"]["stato"], "ammessa")
        self.assertIn("correggi_senza_riferimento", codes(noref))

    def test_ripassa(self):
        d = build(self.reg, "storia", "ripassa", TEXT)
        self.assertIn("organizzare_concetti", d["sessione"]["azioni_ammesse"])
        self.assertIn("chiamare_programma_completo", d["sessione"]["azioni_vietate"])
        self.assertIn("ripassa", codes(d))

    def test_prepara_obiettivo_with_content(self):
        d = build(self.reg, "matematica", "prepara_obiettivo", TEXT, obiettivo="cvv:1000101")
        self.assertEqual(d["esito"]["stato"], "ammessa")
        ctx = d["contesto_scolastico"]["obiettivo"]
        self.assertEqual([c["uid"] for c in ctx["quadro_obiettivo"]["contenuti"]], ["cvv:1000101"])
        self.assertIn("argomenti_obiettivo", codes(d))
        self.assertIn("non_coincide_programma", codes(d))
        self.assertIn({"codice": "argomenti_obiettivo_non_determinati", "fonte": "M11/M12 (U4 sempre non determinabile)"},
                      d["sessione"]["limiti"])
        self.assertIn("dichiarare_coincidenza_contenuto_programma", d["sessione"]["azioni_vietate"])

    def test_prepara_obiettivo_without_content_only_context(self):
        d = build(self.reg, "matematica", "prepara_obiettivo", None, obiettivo="cvv:1000101")
        self.assertEqual(d["esito"]["stato"], "solo_contesto")
        self.assertEqual(d["sessione"]["azioni_ammesse"], list(SS.CONTEXT_ONLY_ALLOWED))
        self.assertNotIn("generare_domande_sul_contenuto", d["sessione"]["azioni_ammesse"])
        self.assertEqual(d["contenuto"]["parti"], [])
        self.assertIn("argomenti_obiettivo", codes(d))

    def test_prepara_obiettivo_requires_explicit_objective(self):
        d = build(self.reg, "matematica", "prepara_obiettivo", TEXT)
        self.assertEqual(d["esito"]["stato"], "obiettivo_richiesto")
        self.assertEqual(d["sessione"]["azioni_ammesse"], list(SS.INSUFFICIENT_ALLOWED))
        info = next(i for i in d["esito"]["informazione_insufficiente"] if i["codice"] == "obiettivo_richiesto")
        self.assertIn("cvv:1000101", info["obiettivi_esistenti"])                       # elencati, mai scelti


# ------------------------------------------------------------------------------------------------ contenuto e obiettivo
class ContenutoEObiettivo(Base):
    def test_empty_or_missing_content(self):
        for content in (None, "", "   ", {"testo": ""}, {}):
            for mode in ("spiega", "interroga", "esercitati", "ripassa"):
                d = build(self.reg, "matematica", mode, content)
                self.assertEqual(d["esito"]["stato"], "contenuto_insufficiente")
                self.assertEqual(d["sessione"]["azioni_ammesse"], list(SS.INSUFFICIENT_ALLOWED))
                self.assertIn("contenuto_mancante", insuff(d))
                self.assertNotIn("conoscenza_generale", codes(d))

    def test_objective_not_found_blocks_didactics(self):
        d = build(self.reg, "matematica", "spiega", TEXT, obiettivo="cvv:265450")      # nessuna ricerca approssimata
        self.assertEqual(d["esito"]["stato"], "obiettivo_non_trovato")
        self.assertEqual(d["sessione"]["azioni_ammesse"], list(SS.INSUFFICIENT_ALLOWED))
        info = next(i for i in d["esito"]["informazione_insufficiente"] if i["codice"] == "obiettivo_non_trovato")
        self.assertIn("cvv:1000101", info["obiettivi_esistenti"])

    def test_objective_without_determinable_topics_no_invention(self):
        d = build(self.reg, "italiano", "prepara_obiettivo", None, obiettivo="cvv:1000104")    # Ariosto
        self.assertEqual(d["esito"]["stato"], "solo_contesto")
        q = d["contesto_scolastico"]["obiettivo"]["quadro_obiettivo"]
        self.assertEqual(q["contenuti"][0]["campi"]["testo_dichiarato"], "Verifica su Ariosto")
        self.assertEqual(d["contesto_scolastico"]["obiettivo"]["materiale_collegato"]["disponibilita"], "non_disponibile")
        self.assertNotIn("argomenti", pretty(d["contenuto"]))
        self.assertIn("inventare_argomenti_obiettivo", d["sessione"]["azioni_vietate"])

    def test_school_reference_resolution(self):
        d = build(self.reg, "matematica", "ripassa", {"riferimenti_m12": ["cvv:lesson:1", "cvv:1000101"]})
        self.assertEqual(d["esito"]["stato"], "ammessa")
        parts = d["contenuto"]["parti"]
        self.assertEqual([p["provenienza"] for p in parts], ["contenuto_scolastico", "contenuto_scolastico"])
        m12 = SA.build(self.reg, "matematica", NOW, manual=MAN, subjects=SUBJ)
        les = next(c for m in m12["modalita"] for c in m["contenuti"] if c["uid"] == "cvv:lesson:1")
        self.assertEqual(parts[0]["riferimento_m12"]["hash"], les["hash"])
        self.assertEqual(parts[0]["anteprima"], les["campi"]["argomento"])
        self.assertEqual(parts[0]["sha256"], hashlib.sha256(les["campi"]["argomento"].encode()).hexdigest())
        self.assertIn("contenuto_scolastico", codes(d))
        self.assertNotIn("contenuto_utente", codes(d))

    def test_school_reference_not_found_or_without_text(self):
        d = build(self.reg, "matematica", "spiega", {"riferimenti_m12": ["cvv:lesson:999", A("C-MF", "w-sim")]})
        self.assertEqual(d["esito"]["stato"], "contenuto_insufficiente")
        self.assertIn("riferimento_non_trovato", insuff(d))
        self.assertIn("riferimento_senza_testo", insuff(d))                            # materiale: titolo non disponibile
        self.assertEqual(d["contenuto"]["parti"], [])

    def test_user_declared_origin_never_school(self):
        d = build(self.reg, "matematica", "spiega", {"testo": TEXT, "origine_dichiarata": "slide della professoressa"})
        p = d["contenuto"]["parti"][0]
        self.assertEqual((p["provenienza"], p["verifica"], p["origine_dichiarata"]),
                         ("contenuto_fornito_utente", "non_verificato_dalle_fonti", "slide della professoressa"))

    def test_three_sources_separated(self):
        d = build(self.reg, "matematica", "spiega", {"testo": TEXT, "riferimenti_m12": ["cvv:lesson:1"]})
        prov = [p["provenienza"] for p in d["contenuto"]["parti"]]
        self.assertEqual(prov, ["contenuto_fornito_utente", "contenuto_scolastico"])     # ordine stabile: utente, poi riferimenti
        self.assertNotIn("conoscenza_generale", prov)                                    # mai nel contenuto: solo etichetta per Claude
        self.assertEqual(set(d["sessione"]["etichette_provenienza"]), set(SS.PROVENANCE))
        self.assertIn("mai presentato come proveniente", d["sessione"]["etichette_provenienza"]["conoscenza_generale"])

    def test_modified_content_changes_provenance(self):
        a = build(self.reg, "matematica", "spiega", TEXT)
        b = build(self.reg, "matematica", "spiega", TEXT + " ")
        self.assertNotEqual(a["contenuto"]["parti"][0]["sha256"], b["contenuto"]["parti"][0]["sha256"])
        self.assertNotEqual(a["contenuto"]["sha256_complessivo"], b["contenuto"]["sha256_complessivo"])
        self.assertEqual(a["contenuto"]["parti"][0]["sha256"], hashlib.sha256(TEXT.encode()).hexdigest())

    def test_preview_and_detail(self):
        long = "x" * 500
        c = build(self.reg, "matematica", "spiega", long)["contenuto"]["parti"][0]
        self.assertEqual((len(c["anteprima"]), c["anteprima_troncata"], c["lunghezza"]), (160, True, 500))
        self.assertNotIn("testo", c)
        d = build(self.reg, "matematica", "spiega", long, dettaglio=True)["contenuto"]["parti"][0]
        self.assertEqual(d["testo"], long)

    def test_no_program_inference_and_no_judgment(self):
        for mode in SS.MODES:
            d = build(self.reg, "matematica", mode, {"testo": TEXT, "risposta_utente": "a = 1"},
                      obiettivo="cvv:1000101" if mode == "prepara_obiettivo" else None)
            for a in ("dichiarare_programma_verifica", "valutare_preparazione_globale", "assegnare_voti", "stimare_tempo",
                      "proporre_piano_automatico", "scegliere_cosa_studiare", "acquisire_materiali", "inferire_dati_scolastici"):
                self.assertIn(a, d["sessione"]["azioni_vietate"])
            self.assertNotIn("programma", pretty(d["contenuto"]))

    def test_m12_guardrails_kept_for_school_context(self):
        d = build(self.reg, "matematica", "spiega", TEXT)
        cs = d["contesto_scolastico"]
        self.assertEqual(cs["azioni_vietate_m12"], list(SA.FORBIDDEN_ACTIONS))
        self.assertIn("contesto scolastico", cs["ambito_divieti_m12"])
        self.assertEqual(cs["disponibilita_m12"], SA.build(self.reg, "matematica", NOW, manual=MAN, subjects=SUBJ)["disponibilita"])


# ------------------------------------------------------------------------------------------------ determinismo, scritture, tool
class DeterminismoESicurezza(unittest.TestCase):
    def test_byte_identical_and_order_independent(self):
        a, b = F.make_registry(), F.make_registry(reverse=True)
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            for k in SC.AGENT_SUBJECTS:
                for mode in SS.MODES:
                    kw = {"obiettivo": "cvv:1000101"} if (mode == "prepara_obiettivo" and k == "matematica") else {}
                    content = {"testo": TEXT, "riferimenti_m12": ["cvv:lesson:1"], "risposta_utente": "a = 1"}
                    x1, x2, y = build(a, k, mode, content, **kw), build(a, k, mode, content, **kw), build(b, k, mode, content, **kw)
                    self.assertEqual(pretty(x1), pretty(x2))
                    self.assertEqual(pretty(x1), pretty(y))
        a.close(); b.close()

    def test_builder_never_reads_clock_and_only_m12(self):
        src = open(SS.__file__, encoding="utf-8").read()
        for bad in ("datetime.now", "time.time", "date.today", "utcnow"):
            self.assertNotIn(bad, src)
        body = src.split('"""', 2)[2]
        for mod in ("school_study_context", "school_action", "school_priority", "school_status", "school_context4", "school_context3",
                    "cvv_cls_links", "cvv_comms", "school_agent", "cls_registry", "cvv_registry"):
            self.assertNotIn("import %s" % mod, body)

    def test_inputs_not_mutated(self):
        reg = F.make_registry()
        content = {"testo": TEXT, "riferimenti_m12": ["cvv:lesson:1"], "risposta_utente": "a = 1"}
        before = copy.deepcopy(content)
        seen = []

        def spy(*a, **kw):
            out = _SA_BUILD(*a, **kw)
            seen.append((out, pretty(out)))
            return out
        with mock.patch.object(R.Registry, "freshness", fresh_fake()), mock.patch.object(SA, "build", spy):
            build(reg, "matematica", "correggi", content, dettaglio=True)
        self.assertEqual(content, before)
        self.assertEqual(pretty(seen[0][0]), seen[0][1])
        reg.close()

    def test_no_writes_and_m1_m12_unchanged(self):
        reg = F.make_registry()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            snap = lambda: [pretty(C3.build(reg, "matematica", SUBJ, NOW, manual=MAN)), pretty(C3.overview(reg, SUBJ, NOW, manual=MAN)),  # noqa: E731
                            pretty(L.build(reg, SUBJ, NOW, manual=MAN)), pretty(M.build(reg, SUBJ, NOW, manual=MAN)),
                            pretty(C4.build(reg, "inglese", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(ST.build(reg, "scienze", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(PR.build(reg, NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(AC.build(reg, NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(SX.build(reg, "matematica", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(_SA_BUILD(reg, "matematica", NOW, manual=MAN, subjects=SUBJ, dettaglio=True))]
            before, c0 = snap(), counts(reg)
            for k in SC.AGENT_SUBJECTS:
                for mode in SS.MODES:
                    build(reg, k, mode, TEXT, dettaglio=True)
            self.assertEqual((before, c0), (snap(), counts(reg)))
        reg.close()

    def test_build_from_path_writes_nothing(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "registry.sqlite3")
        F.make_registry(path=p).close()
        h = lambda q: hashlib.sha256(open(q, "rb").read()).hexdigest()                 # noqa: E731
        s0, f0, h0 = dry._fs_state(p), sorted(os.listdir(d)), h(p)
        out = SS.build_from_path(p, "fisica", "spiega", TEXT, NOW, subjects=SUBJ)
        self.assertEqual(out["schema"], SS.SCHEMA)
        self.assertEqual((s0, f0, h0), (dry._fs_state(p), sorted(os.listdir(d)), h(p)))


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
        t = self.server.srv._tools["school_study_session_v1"]["def"]
        self.assertEqual(set(t["inputSchema"]["properties"]), {"materia", "modalita", "contenuto", "obiettivo", "dettaglio"})
        self.assertEqual(t["inputSchema"]["required"], ["materia", "modalita", "contenuto"])
        self.assertIn("SOLA LETTURA", t["description"])

    def test_missing_registry(self):
        with self.assertRaises(ValueError):
            self.server.school_study_session_v1("matematica", "spiega", TEXT)
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
            out = self.server.school_study_session_v1("matematica", "prepara_obiettivo", TEXT, obiettivo="cvv:1000101")
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["provenance"]["riferimento_temporale"]["now"], "2026-11-01T12:00:00+02:00")
        self.assertFalse(out["verifica_nessuna_scrittura"]["file_modificati"])
        self.assertEqual(h0, hashlib.sha256(open(self.db, "rb").read()).hexdigest())


if __name__ == "__main__":
    unittest.main()
