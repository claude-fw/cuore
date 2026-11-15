"""M6 - classificazione derivata delle comunicazioni (cloud.school.communications/1): casi sintetici con la forma dei casi reali,
principi (determinato solo da struttura nativa, nessun falso positivo da nomi soli, M5 solo corroborazione, conflitti senza scelta),
date, perimetro, filtri, explain, freschezza, determinismo, non-mutazione, compatibilita' M1-M5, tool MCP."""
import hashlib, json, os, sys, tempfile, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import comms_fixtures as F
import cvv_cls_links as L
import cvv_comms as M
import registry as R
import school_context as SC
import school_context3 as C3
import school_common as CM

SUBJ, NOW, MAN = F.SUBJECTS, F.NOW, F.MANUAL
N = lambda i: "cvv:%d" % i                       # noqa: E731
A = lambda c, a: "cls:%s/%s" % (c, a)            # noqa: E731


def dump(o):
    return json.dumps(o, ensure_ascii=False, sort_keys=True, default=str)


def counts(reg):
    return {t: reg.db.execute("SELECT count(*) FROM %s" % t).fetchone()[0] for t in ("records", "versions", "events", "runs", "projections")}


def build(reg, **kw):
    kw.setdefault("perimetro", "tutti")
    kw.setdefault("limits", {"comunicazioni": 0})
    return M.build(reg, SUBJ, NOW, manual=kw.pop("manual", MAN), **kw)


def by(doc):
    return {x["uid"]: x for x in doc["comunicazioni"]}


def fn(x, name):
    r = [f for f in x["interpretazione"]["funzioni"] if f["funzione"] == name]
    return r[0] if r else None


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = F.make_registry()
        cls.doc = build(cls.reg, dettaglio=True)
        cls.c = by(cls.doc)

    @classmethod
    def tearDownClass(cls):
        cls.reg.close()


# ------------------------------------------------------------------------------------------------ circolari ClasseViva
class Circolari(Base):
    def test_schema_levels_and_sections(self):
        d = self.doc
        self.assertEqual(d["schema"], "cloud.school.communications/1")
        self.assertEqual(set(d["livelli"]), {"dati", "interpretazione", "freschezza"})
        self.assertIn("SOLA LETTURA", d["natura"].upper())
        self.assertEqual(d["comm_version"], M.comm_version())
        self.assertTrue(d["comm_version"].startswith("r2-"))                # r2: ASSENZA_DOCENTE (M7)
        for k in ("riepilogo", "freschezza", "warning", "informazioni_mancanti", "provenienza", "regole_per_gli_agenti", "criteri", "filtri"):
            self.assertIn(k, d)

    def test_notice_is_general_and_never_has_a_subject(self):
        for i in range(1001, 1013):
            x = self.c[N(i)]
            self.assertEqual(x["interpretazione"]["ambito"], {"valore": "generale", "stato": "determinato", "regola": "A1",
                                                              "criterio": M.RULES["A1"]})
            self.assertIsNone(x["interpretazione"]["materia"]["key"])
            self.assertEqual(x["interpretazione"]["materia"]["stato"], "da_verificare")

    def test_real_like_circulars(self):
        exp = {1001: ("attivita", "probabile"), 1002: ("attivita", "probabile"), 1003: ("attivita", "proposto"),
               1004: ("organizzativa", "probabile"), 1006: ("avviso", "proposto"), 1011: ("organizzativa", "proposto")}
        for i, (cls, st) in exp.items():
            p = self.c[N(i)]["interpretazione"]["classe_principale"]
            self.assertEqual((p["valore"], p["stato"]), (cls, st), i)
            self.assertEqual(self.c[N(i)]["stato"], st)

    def test_circular_without_evidence_is_not_determinable(self):
        x = self.c[N(1005)]                               # 'Associazione culturale'
        self.assertEqual(x["stato"], "non_determinabile")
        self.assertEqual(x["interpretazione"]["regola"], "RN1")
        self.assertEqual(x["interpretazione"]["funzioni"], [])
        self.assertIsNone(x["interpretazione"]["classe_principale"]["valore"])
        self.assertIn("nessun indizio", dump(x["interpretazione"]["informazioni_mancanti"]))

    def test_notice_attachment_is_not_a_clue(self):
        for i in (1003, 1012):                                  # l'allegato di una circolare E' la circolare
            self.assertIsNone(fn(self.c[N(i)], "materiale"))
            self.assertEqual(self.c[N(i)]["dati"]["materiali"], 1)
        x = self.c[N(1012)]                                      # solo 'Documento' + allegato: nessun indizio
        self.assertEqual((x["stato"], x["interpretazione"]["regola"]), ("non_determinabile", "RN1"))
        self.assertEqual(self.c[N(1003)]["interpretazione"]["classe_principale"]["valore"], "attivita")

    def test_announcement_native_materials_are_determined(self):
        x = self.c[A("C-EN", "a-nat")]
        self.assertEqual((fn(x, "materiale")["stato"], fn(x, "materiale")["regola"]), ("determinato", "M1"))
        self.assertEqual((x["stato"], x["interpretazione"]["classe_principale"]["valore"]), ("determinato", "materiale"))
        self.assertTrue(any(p["codice"] == "S6" for p in fn(x, "materiale")["prove"]))

    def test_determinato_only_from_native_structure(self):
        for x in self.doc["comunicazioni"]:
            for f in x["interpretazione"]["funzioni"]:
                if f["stato"] == "determinato":
                    self.assertEqual(f["regola"], "M1", x["uid"])
                    self.assertTrue(any(p["codice"] == "S6" for p in f["prove"]))
            if x["stato"] == "determinato":
                self.assertEqual(x["interpretazione"]["classe_principale"]["valore"], "materiale")

    def test_native_validity_expired(self):
        v = self.c[N(1006)]["dati"]["validita_nativa"]
        self.assertEqual((v["stato"], v["al"], v["cntValidInRange"]), ("scaduta", "2026-10-28", False))
        self.assertIn("circolari_scadute", {w["codice"] for w in self.doc["warning"]})
        self.assertEqual(self.c[N(1001)]["dati"]["validita_nativa"]["stato"], "valida")      # in_range True, nessuna data
        self.assertEqual(self.c[N(1001)]["dati"]["validita_nativa"]["dal"], None)

    def test_native_category_is_reported_not_mapped(self):
        self.assertEqual(self.c[N(1011)]["dati"]["categoria_scuola"], "Organizzazione")
        self.assertIsNone(self.c[N(1001)]["dati"]["categoria_scuola"])
        self.assertEqual(self.c[N(1011)]["interpretazione"]["classe_principale"]["valore"], "organizzativa")

    def test_verification_with_date_and_variations(self):
        self.assertEqual(fn(self.c[N(1007)], "verifica")["regola"], "V2")                   # 'di recupero' rompe l'attribuzione
        self.assertEqual(fn(self.c[N(1008)], "verifica")["regola"], "V3")                   # annullata
        self.assertEqual(fn(self.c[N(1008)], "verifica")["stato"], "proposto")
        self.assertIn("S9", {p["codice"] for p in fn(self.c[N(1008)], "verifica")["prove"]})

    def test_two_verification_dates_are_a_conflict_without_choice(self):
        x = self.c[N(1009)]
        self.assertEqual(x["stato"], "conflitto")
        c = x["interpretazione"]["conflitti"]
        self.assertEqual(len(c), 1)
        self.assertEqual((c[0]["tipo"], c[0]["regola"], c[0]["valori"]), ("date_discordanti", "RC2", ["2026-11-11", "2026-11-12"]))
        self.assertEqual(fn(x, "verifica")["stato"], "conflitto")

    def test_history_is_historic_perimeter(self):
        self.assertEqual(self.c[N(1010)]["interpretazione"]["perimetro"]["valore"], "storico")
        self.assertEqual(self.c[N(1010)]["stato"], "non_determinabile")                      # 'gita' non e' nel lessico
        self.assertEqual(self.c[N(1001)]["interpretazione"]["perimetro"], {"valore": "operativo", "anno": "2026-27", "classe": "4a classe"})


# ------------------------------------------------------------------------------------------------ annunci Classroom
class Annunci(Base):
    def test_announcement_scope_and_subject(self):
        i = lambda a, c: self.c[A(c, a)]["interpretazione"]                                 # noqa: E731
        self.assertEqual((i("a-ing", "C-EN")["ambito"]["valore"], i("a-ing", "C-EN")["materia"]["key"]), ("materia", "inglese"))
        self.assertEqual((i("a-ita", "C-LE")["ambito"]["valore"], i("a-ita", "C-LE")["materia"]["key"],
                          i("a-ita", "C-LE")["materia"]["stato"]), ("corso", None, "da_verificare"))
        self.assertEqual(i("a-ita", "C-LE")["materia"]["candidati"], ["italiano", "latino"])
        x = self.c[A("C-X", "a-x")]                                                          # corso NON confermato: classificato, materia no
        self.assertEqual((x["interpretazione"]["ambito"]["valore"], x["interpretazione"]["materia"]["stato"]), ("corso", "da_verificare"))
        self.assertFalse(x["dati"]["corso"]["confermato"])

    def test_homework_with_date_and_verification_mention(self):
        x = self.c[A("C-EN", "a-ing")]                           # 'Esercizi per giovedi 29/10 ripasso generale per verifica'
        self.assertEqual((fn(x, "compito")["stato"], fn(x, "compito")["regola"]), ("probabile", "C1"))
        self.assertEqual((fn(x, "verifica")["stato"], fn(x, "verifica")["regola"]), ("proposto", "V2"))     # data NON attribuita
        self.assertEqual(x["interpretazione"]["classe_principale"], {"valore": "compito", "stato": "probabile", "pari_livello": None})
        self.assertEqual(x["stato"], "probabile")

    def test_relative_dates_are_detected_never_resolved(self):
        x = self.c[A("C-SC", "a-sci2")]                          # 'Per lunedi studiate la stechiometria'
        i = x["interpretazione"]
        self.assertEqual(i["date"], [])
        self.assertTrue(i["riferimenti_relativi_non_risolti"])
        self.assertEqual(fn(x, "compito")["regola"], "C1")
        self.assertIn("NON risolti", dump(i["informazioni_mancanti"]))
        for y in self.doc["comunicazioni"]:                       # nessuna data inventata: ogni data ha un testo esplicito
            for d in y["interpretazione"]["date"]:
                self.assertTrue(d["testo"])

    def test_short_name_alone_is_not_determinable(self):
        x = self.c[A("C-LE", "a-nome")]                          # 'Ariosto'
        self.assertEqual((x["stato"], x["interpretazione"]["regola"]), ("non_determinabile", "RN1"))
        self.assertEqual(x["interpretazione"]["funzioni"], [])
        # 'Ariosto: leggete il canto primo' ha un verbo di studio: solo proposto
        self.assertEqual((self.c[A("C-LE", "a-ita")]["stato"], fn(self.c[A("C-LE", "a-ita")], "compito")["regola"]), ("proposto", "C2"))

    def test_substitution_notice_is_organizational(self):
        x = self.c[A("C-SF", "a-sost")]
        self.assertEqual((x["interpretazione"]["classe_principale"]["valore"], x["stato"]), ("organizzativa", "probabile"))
        self.assertIsNone(x["interpretazione"]["materia"]["key"])

    def test_url_only_is_material(self):
        x = self.c[A("C-EN", "a-url")]
        self.assertEqual((fn(x, "materiale")["regola"], x["interpretazione"]["classe_principale"]["valore"]), ("M2", "materiale"))
        self.assertEqual(x["stato"], "probabile")                                             # sintattico, mai 'determinato'

    def test_verification_with_attributed_date(self):
        x = self.c[A("C-MF", "a-ver")]
        self.assertEqual((fn(x, "verifica")["regola"], x["stato"]), ("V1", "probabile"))
        self.assertEqual(x["interpretazione"]["date"][0]["ruolo"], "verifica")
        self.assertEqual(x["interpretazione"]["date"][0]["data"], "2026-11-04")
        self.assertTrue(x["interpretazione"]["date"][0]["anno_esplicito"])
        self.assertEqual(x["interpretazione"]["materia"]["stato"], "da_verificare")           # corso condiviso senza topic

    def test_conflicts_do_not_choose(self):
        x = self.c[A("C-MF", "a-ver2")]                          # due date diverse attribuite a verifiche
        self.assertEqual((x["stato"], x["interpretazione"]["conflitti"][0]["tipo"]), ("conflitto", "date_discordanti"))
        y = self.c[A("C-EN", "a-disc")]                          # 'verifica di storia' in un corso di inglese
        c = y["interpretazione"]["conflitti"][0]
        self.assertEqual((y["stato"], c["tipo"], c["regola"]), ("conflitto", "materia_discordante", "RC1"))
        self.assertEqual(c["valori"], {"testo": ["storia"], "corso": ["inglese"]})
        self.assertEqual(y["interpretazione"]["materia"]["stato"], "conflitto")
        self.assertEqual(self.c[A("C-EN", "a-disc")]["dati"]["testo"], "Verifica di storia sul Rinascimento")     # dato originale intatto
        self.assertTrue(fn(y, "verifica"))                                                    # le funzioni restano visibili
        self.assertIn("nessuna scelta", dump(c))

    def test_negation_caps_verification(self):
        x = self.c[A("C-EN", "a-neg")]                           # 'Nessuna verifica domani'
        self.assertEqual((fn(x, "verifica")["regola"], x["stato"]), ("V3", "proposto"))

    def test_equal_classes_are_not_chosen(self):
        x = self.c[A("C-SC", "a-two")]
        p = x["interpretazione"]["classe_principale"]
        self.assertEqual((p["valore"], sorted(p["pari_livello"]), x["interpretazione"]["regola"]), (None, ["attivita", "organizzativa"], "RN2"))
        self.assertEqual(x["stato"], "probabile")

    def test_material_without_native_structure_is_only_proposed(self):
        x = self.c[A("C-SC", "a-sci1")]
        self.assertEqual((fn(x, "materiale")["regola"], x["stato"]), ("M3", "proposto"))

    def test_native_materials_make_material_determined(self):
        a = F.ann("C-EN", "a-mat", "Ecco la dispensa", "2026-10-29")
        a["materials"] = [{"driveFile": {"driveFile": {"id": "x1", "title": "dispensa.pdf"}}}]
        d = build(F.make_registry(announcements=[a]), dettaglio=True)
        x = by(d)[A("C-EN", "a-mat")]
        self.assertEqual((fn(x, "materiale")["regola"], x["stato"]), ("M1", "determinato"))

    def test_draft_and_missing_date_are_excluded_and_counted(self):
        a = F.ann("C-EN", "a-d", "Verifica del 4 novembre", "2026-10-29")
        a["state"] = "DRAFT"
        d = build(F.make_registry(announcements=[a]))
        self.assertNotIn(A("C-EN", "a-d"), by(d))
        self.assertEqual(d["riepilogo"]["esclusi_dalla_raccolta"]["annunci_non_pubblicati"], 1)

    def test_test_word_in_compiti_in_classe_is_a_verification_not_homework(self):
        a = F.ann("C-EN", "a-cc", "Compito in classe martedi", "2026-10-29")
        x = by(build(F.make_registry(announcements=[a])))[A("C-EN", "a-cc")]
        self.assertEqual(fn(x, "verifica")["stato"], "proposto")
        self.assertIsNone(fn(x, "compito"))


# ------------------------------------------------------------------------------------------------ M5 come corroborazione
class UsoDiM5(Base):
    def test_m5_corroborates_existing_textual_evidence(self):
        x = self.c[A("C-SC", "a-sci-ver")]                       # 'Ripasso per la verifica sulla stechiometria delle reazioni'
        v = fn(x, "verifica")
        self.assertEqual((v["stato"], v["regola"]), ("probabile", "V4"))
        s8 = [p for p in v["prove"] if p["codice"] == "S8"]
        self.assertEqual((len(s8), s8[0]["peso"]), (1, "corroborativa"))
        m5 = x["interpretazione"]["collegamenti_m5"]
        self.assertTrue(m5["usato_come_evidenza"])
        self.assertTrue(any(l["relazione"] == "prepara" for l in m5["collegamenti"]))
        # il collegamento citato esiste davvero in M5
        links = L.build(self.reg, SUBJ, NOW, manual=MAN)["collegamenti"]
        self.assertTrue({l["link_id"] for l in m5["collegamenti"]} <= {l["link_id"] for l in links})
        self.assertEqual(x["stato"], "probabile")

    def test_m5_link_alone_never_creates_a_classification(self):
        x = self.c[A("C-SC", "a-sci-nome")]                      # 'Stechiometria delle reazioni chimiche': M5 collega, il testo no
        m5 = x["interpretazione"]["collegamenti_m5"]
        self.assertTrue(m5["collegamenti"])
        self.assertFalse(m5["usato_come_evidenza"])
        self.assertEqual((x["stato"], x["interpretazione"]["funzioni"]), ("non_determinabile", []))
        self.assertIn("M5 e' solo corroborazione", dump(x["interpretazione"]["informazioni_mancanti"]))

    def test_m5_never_yields_determinato_nor_changes_without_text(self):
        for x in self.doc["comunicazioni"]:
            if x["interpretazione"]["collegamenti_m5"]["usato_come_evidenza"]:
                self.assertNotEqual(x["stato"], "determinato")
                self.assertTrue(any(p["codice"] == "S4" for f in x["interpretazione"]["funzioni"] for p in f["prove"]))

    def test_strong_fake_m5_link_changes_nothing_without_text(self):
        # M5 che 'vede' collegamenti forti ovunque: senza testo l'annuncio resta non_determinabile
        idx = {A("C-LE", "a-nome"): {"collegamenti": [{"link_id": "x", "relazione": "prepara", "stato": "probabile", "regola": "R1",
                                                       "con": {"sistema": "classeviva", "tipo": "agenda_event", "uid": "cvv:1"}}],
                                     "conflitti": [], "non_determinabili": 0, "nota": "t"}}
        with mock.patch.object(M, "_m5_index", lambda *a, **k: (idx, "fake")):
            d = by(build(self.reg))
        self.assertEqual(d[A("C-LE", "a-nome")]["stato"], "non_determinabile")

    def test_m5_does_not_raise_a_capped_verification(self):
        a = F.ann("C-SC", "a-cap", "Verifica stechiometria rinviata", "2026-10-23")
        x = by(build(F.make_registry(announcements=[a])))[A("C-SC", "a-cap")]
        self.assertEqual(fn(x, "verifica")["regola"], "V3")

    def test_notices_have_no_m5(self):
        for i in range(1001, 1013):
            m5 = self.c[N(i)]["interpretazione"]["collegamenti_m5"]
            self.assertEqual((m5["collegamenti"], m5["usato_come_evidenza"]), ([], False))

    def test_m5_conflicts_are_reported_not_used(self):
        seen = [x for x in self.doc["comunicazioni"] if x["interpretazione"]["collegamenti_m5"]["conflitti"]]
        links = L.build(self.reg, SUBJ, NOW, manual=MAN)
        self.assertEqual(len(seen), len({c["elemento"]["uid"] for c in links["conflitti"] if c["elemento"]["tipo"] == "announcement"}))
        for x in seen:                                           # un conflitto M5 non rende conflittuale la classificazione M6
            self.assertNotIn("m5", dump(x["interpretazione"]["conflitti"]).lower())


# ------------------------------------------------------------------------------------------------ principi trasversali
class Principi(Base):
    def test_every_classification_is_explained_and_cited(self):
        reg = self.reg
        for x in self.doc["comunicazioni"]:
            f = x["fonte_record"]
            rec = reg.get_record(f["uid"].split(":")[0] == "cvv" and "classeviva" or "classroom",
                                 "notice" if x["tipo"] == "notice" else "announcement",
                                 f["uid"][4:] if x["tipo"] == "notice" else f["uid"][len("cls:"):])
            self.assertEqual((f["registry_version"], f["content_hash"]), (rec["version"], rec["content_hash"]), x["uid"])
            self.assertEqual(set(x), {"comm_id", "fonte", "tipo", "uid", "data", "stato", "dati", "interpretazione", "fonte_record", "freschezza"})
            self.assertIn(x["stato"], M.STATES)
            for fu in x["interpretazione"]["funzioni"]:
                self.assertTrue(fu["prove"], (x["uid"], fu["funzione"]))
                self.assertIn(fu["regola"], M.RULES)
                for p in fu["prove"]:
                    self.assertEqual(set(p), {"codice", "segnale", "esito", "peso", "dettaglio"})

    def test_overall_state_follows_conflicts_then_main_class(self):
        for x in self.doc["comunicazioni"]:
            i = x["interpretazione"]
            if i["conflitti"]:
                self.assertEqual(x["stato"], "conflitto")
            elif i["classe_principale"]["valore"] or i["classe_principale"]["pari_livello"]:
                self.assertEqual(x["stato"], i["classe_principale"]["stato"])
            else:
                self.assertEqual(x["stato"], "non_determinabile")

    def test_data_layer_is_the_original_text(self):
        raw = {n["pubId"]: n for n in F.NOTICES}
        for i, n in raw.items():
            self.assertEqual(self.c[N(i)]["dati"]["titolo"], n["cntTitle"])
        for a in F.ANNOUNCEMENTS:
            self.assertEqual(self.c[A(a["courseId"], a["id"])]["dati"]["testo"], a["text"])

    def test_registry_is_not_modified(self):
        reg = F.make_registry()
        before = counts(reg)
        build(reg, dettaglio=True)
        M.explain(reg, SUBJ, NOW, N(1001), manual=MAN)
        self.assertEqual(before, counts(reg))

    def test_collect_does_not_mutate_and_is_repeatable(self):
        d1, d2 = build(self.reg, dettaglio=True), build(self.reg, dettaglio=True)
        self.assertEqual(dump(d1), dump(d2))

    def test_deterministic_and_order_independent(self):
        a = build(F.make_registry(), dettaglio=True)
        b = build(F.make_registry(reverse=True), dettaglio=True)
        strip = lambda d: [{k: v for k, v in x.items() if k not in ("fonte_record",)} for x in d["comunicazioni"]]    # noqa: E731
        self.assertEqual(dump(strip(a)), dump(strip(b)))
        self.assertEqual([x["comm_id"] for x in a["comunicazioni"]], [x["comm_id"] for x in b["comunicazioni"]])

    def test_comm_id_follows_source_content_and_rules_version(self):
        base = by(build(F.make_registry()))
        ann2 = [dict(a) for a in F.ANNOUNCEMENTS]
        for a in ann2:
            if a["id"] == "a-ing":
                a["text"] += " e leggete pag. 3"
        changed = by(build(F.make_registry(announcements=ann2)))
        self.assertNotEqual(base[A("C-EN", "a-ing")]["comm_id"], changed[A("C-EN", "a-ing")]["comm_id"])
        self.assertEqual(base[N(1001)]["comm_id"], changed[N(1001)]["comm_id"])
        with mock.patch.object(M, "COMM_RULES_REV", 3):
            self.assertNotEqual(M.comm_version(), self.doc["comm_version"])
        with mock.patch.dict(M.LEX, {"avviso": M.LEX["avviso"] + "|zzz"}):
            self.assertNotEqual(M.comm_version(), self.doc["comm_version"])

    def test_comm_version_tracks_m5_version(self):
        with mock.patch.object(L, "link_version", lambda: "r9-zzzzzz"):
            self.assertNotEqual(M.comm_version(), self.doc["comm_version"])

    def test_no_invented_dates_subjects_or_teachers(self):
        for x in self.doc["comunicazioni"]:
            if x["fonte"] == "classeviva":
                self.assertIsNone(x["interpretazione"]["materia"]["key"])
            self.assertNotIn("docente", x["dati"])
            for d in x["interpretazione"]["date"]:
                self.assertTrue(d["testo"])
                self.assertIn(d["ruolo"], ("verifica", "scadenza", "non_attribuita", "non_utilizzabile"))

    def test_impossible_or_incoherent_dates_are_not_used(self):
        n = F.notice(2001, "Verifica del 31/9 e verifica lunedi 5/10", "2026-10-29")
        x = by(build(F.make_registry(notices=[n]), dettaglio=True))[N(2001)]
        roles = {d["testo"]: d["ruolo"] for d in x["interpretazione"]["date"]}
        self.assertTrue(roles)
        self.assertTrue(all(r == "non_utilizzabile" for t, r in roles.items() if "31" in t))
        self.assertEqual(fn(x, "verifica")["stato"] in ("proposto", "probabile"), True)

    def test_deadline_date_role(self):
        a = F.ann("C-EN", "a-dl", "Compiti da consegnare entro il 6 novembre", "2026-10-29")
        x = by(build(F.make_registry(announcements=[a])))[A("C-EN", "a-dl")]
        d = x["interpretazione"]["date"][0]
        self.assertEqual((d["data"], d["ruolo"]), ("2026-11-06", "scadenza"))
        self.assertEqual(fn(x, "compito")["regola"], "C1")
        self.assertIsNone(fn(x, "verifica"))

    def test_empty_registry_and_no_confirmed_courses(self):
        d = build(R.Registry(":memory:"))
        self.assertEqual(d["comunicazioni"], [])
        self.assertIn("nessun corso Classroom CONFERMATO", dump(M.build(R.Registry(":memory:"), SUBJ, NOW, manual={})["informazioni_mancanti"]))
        d2 = build(F.make_registry(), manual={})
        self.assertEqual({x["interpretazione"]["materia"]["stato"] for x in d2["comunicazioni"] if x["fonte"] == "classroom"}, {"da_verificare"})
        self.assertEqual(len(d2["comunicazioni"]), len(self.doc["comunicazioni"]))

    def test_counts_match_registry_published(self):
        n_n = len(self.reg.list_records(source="classeviva", kind="notice", status=R.PRESENT))
        n_a = len([a for a in F.ANNOUNCEMENTS if a.get("state") == "PUBLISHED"])
        self.assertEqual(self.doc["riepilogo"]["per_fonte"], {"classeviva": n_n, "classroom": n_a})
        self.assertEqual(sum(self.doc["riepilogo"]["per_stato"].values()), n_n + n_a)


# ------------------------------------------------------------------------------------------------ perimetro, filtri, explain
class FiltriEExplain(Base):
    def test_default_perimeter_is_operational(self):
        d = M.build(self.reg, SUBJ, NOW, manual=MAN, limits={"comunicazioni": 0})
        uids = set(by(d))
        self.assertNotIn(N(1010), uids)
        self.assertNotIn(A("C-SC", "a-hist"), uids)
        self.assertIn(A("C-SC", "a-old"), uids)                    # 2025-26 = 3a: operativo
        self.assertEqual(d["riepilogo"]["esclusi_dai_filtri"], {"fuori_perimetro_operativo": 2})
        self.assertEqual(d["filtri"]["perimetro"], "operativo")
        self.assertEqual(len(self.doc["comunicazioni"]), len(uids) + 2)

    def test_perimeter_values(self):
        h = build(self.reg, perimetro="storico")
        self.assertEqual(set(by(h)), {N(1010), A("C-SC", "a-hist")})
        with self.assertRaises(ValueError):
            build(self.reg, perimetro="boh")

    def test_subject_filter_excludes_general_and_possible(self):
        d = build(self.reg, materia="scienze")
        self.assertTrue(d["comunicazioni"])
        self.assertTrue(all(x["interpretazione"]["materia"]["key"] == "scienze" for x in d["comunicazioni"]))
        self.assertEqual(d["riepilogo"]["esclusi_dai_filtri"].get("materia_generali"), 12)
        self.assertIn("includi_generali", dump(d["informazioni_mancanti"]))
        e = build(self.reg, materia="scienze", includi_generali=True)
        self.assertEqual(len(e["comunicazioni"]), len(d["comunicazioni"]) + 12)
        m = build(self.reg, materia="matematica")                  # corso condiviso senza topic: solo 'possibile', non della materia
        self.assertEqual(m["comunicazioni"], [])
        self.assertGreater(m["riepilogo"]["esclusi_dai_filtri"].get("materia_possibile_da_verificare", 0), 0)

    def test_class_state_source_and_date_filters(self):
        v = build(self.reg, classe="verifica")
        self.assertTrue(all(fn(x, "verifica") for x in v["comunicazioni"]))
        self.assertIn(A("C-EN", "a-ing"), by(v))                   # funzione presente anche se non principale
        self.assertTrue(all(x["stato"] == "conflitto" for x in build(self.reg, stato="conflitto")["comunicazioni"]))
        self.assertEqual({x["fonte"] for x in build(self.reg, fonte="classeviva")["comunicazioni"]}, {"classeviva"})
        r = build(self.reg, dal="2026-10-29", al="2026-10-30")
        self.assertTrue(r["comunicazioni"] and all("2026-10-29" <= x["data"] <= "2026-10-30" for x in r["comunicazioni"]))
        self.assertGreater(r["riepilogo"]["esclusi_dai_filtri"]["fuori_intervallo_date"], 0)

    def test_invalid_filters(self):
        for kw in ({"classe": "boh"}, {"stato": "certo"}, {"stato": "confermato"}, {"fonte": "notion"}, {"materia": "religione"}, {"materia": ""},
                   {"dal": "ieri"}, {"dal": "2026-11-02", "al": "2026-10-29"}):
            with self.assertRaises(ValueError, msg=str(kw)):
                build(self.reg, **kw)

    def test_compact_vs_detail(self):
        c = build(self.reg)
        d = self.doc
        self.assertEqual([x["comm_id"] for x in c["comunicazioni"]], [x["comm_id"] for x in d["comunicazioni"]])
        for x in c["comunicazioni"]:
            for f in x["interpretazione"]["funzioni"]:
                self.assertNotIn("prove", f)
        self.assertLess(len(dump(c)), len(dump(d)) * 0.8)
        self.assertEqual({x["stato"] for x in c["comunicazioni"]}, {x["stato"] for x in d["comunicazioni"]})
        self.assertEqual({x["comm_id"] for x in c["comunicazioni"] if x["stato"] == "conflitto"}, {x["comm_id"] for x in d["comunicazioni"] if x["stato"] == "conflitto"})

    def test_limits_declare_truncation(self):
        d = M.build(self.reg, SUBJ, NOW, manual=MAN, perimetro="tutti", limits={"comunicazioni": 5})
        self.assertEqual(len(d["comunicazioni"]), 5)
        self.assertIn("contenuto_parziale", {w["codice"] for w in d["warning"]})
        self.assertEqual(d["riepilogo"]["comunicazioni_selezionate"], len(self.doc["comunicazioni"]))
        self.assertEqual([x["data"] for x in d["comunicazioni"]], sorted([x["data"] for x in self.doc["comunicazioni"]], reverse=True)[:5])

    def test_summary_counts(self):
        r = self.doc["riepilogo"]
        self.assertEqual(sum(r["per_stato"].values()), r["comunicazioni_selezionate"])
        self.assertEqual(sum(r["per_ambito"].values()), r["comunicazioni_selezionate"])
        self.assertEqual(sum(r["per_classe_principale"].values()), r["comunicazioni_selezionate"])
        self.assertEqual(r["per_stato"]["conflitto"], len([x for x in self.doc["comunicazioni"] if x["stato"] == "conflitto"]))
        self.assertIn("conflitti_presenti", {w["codice"] for w in self.doc["warning"]})

    def test_explain_by_uid_and_comm_id(self):
        for t in (A("C-SC", "a-sci-ver"), self.c[A("C-SC", "a-sci-ver")]["comm_id"]):
            ex = M.explain(self.reg, SUBJ, NOW, t, manual=MAN)
            self.assertEqual(ex["schema"], "cloud.school.communications.explain/1")
            self.assertEqual(len(ex["comunicazioni"]), 1)
            x = ex["comunicazioni"][0]
            self.assertTrue(fn(x, "verifica")["prove"])
            self.assertIn("motivazione", fn(x, "verifica"))
            self.assertEqual(x["freschezza"]["nota"], C3.FRESH_NOTE)
        self.assertEqual(M.explain(self.reg, SUBJ, NOW, A("C-SC", "a-sci-ver"), manual=MAN)["tipo_richiesta"], "uid")
        # explain considera TUTTI i perimetri
        self.assertEqual(len(M.explain(self.reg, SUBJ, NOW, N(1010), manual=MAN)["comunicazioni"]), 1)

    def test_explain_unknown_and_invalid(self):
        ex = M.explain(self.reg, SUBJ, NOW, "cvv:999999", manual=MAN)
        self.assertEqual((ex["tipo_richiesta"], ex["comunicazioni"]), ("sconosciuto", []))
        self.assertIn("non trovato", ex["esito"])
        for t in ("", "  ", None, 5):
            with self.assertRaises(ValueError):
                M.explain(self.reg, SUBJ, NOW, t, manual=MAN)


# ------------------------------------------------------------------------------------------------ freschezza
class Freschezza(Base):
    @staticmethod
    def fake(notice_stale=False, ann_state="aggiornato"):
        def per(stale, motivo="ok"):
            return {"ultimo_run": {"legacy": False}, "stale": stale, "motivo": motivo, "eta_ore": 99 if stale else 1,
                    "ultima_lettura_riuscita": "2026-11-01T10:00:00+02:00"}
        def freshness(self, now, stale_after_hours=None):
            pk = {}
            for s, kinds in (("classeviva", ("agenda_event", "lesson", "notice", "grade")),
                             ("classroom", ("course", "course_work", "announcement", "material", "topic", "submission"))):
                for k in kinds:
                    pk["%s/%s" % (s, k)] = per(stale=(notice_stale and k == "notice") or (ann_state == "stale" and k == "announcement"))
            return {"calcolato_a": now, "soglia_ore": 48, "per_kind": pk}
        return freshness

    def test_freshness_is_operational_and_separate(self):
        d = build(self.reg)
        self.assertTrue(set(d["freschezza"]["per_sezione"]) == {"circolari", "annunci", "agenda", "lezioni"})
        self.assertTrue(set(d["freschezza"]["per_sezione"].values()) <= {"aggiornato", "stale", "senza_lettura"})
        for x in d["comunicazioni"]:
            self.assertIn(x["freschezza"]["stato"], ("aggiornato", "stale", "senza_lettura"))
        # la freschezza non cambia le classificazioni
        with mock.patch.object(R.Registry, "freshness", self.fake(notice_stale=True)):
            e = build(self.reg)
        self.assertEqual([(x["comm_id"], x["stato"]) for x in d["comunicazioni"]], [(x["comm_id"], x["stato"]) for x in e["comunicazioni"]])

    def test_stale_notice_section_marks_only_notices(self):
        with mock.patch.object(R.Registry, "freshness", self.fake(notice_stale=True)):
            d = build(self.reg)
        self.assertEqual(d["freschezza"]["per_sezione"]["circolari"], "stale")
        self.assertEqual(d["freschezza"]["per_sezione"]["annunci"], "aggiornato")
        for x in d["comunicazioni"]:
            self.assertEqual(x["freschezza"]["stato"], "stale" if x["tipo"] == "notice" else "aggiornato", x["uid"])
        codes = {w["codice"] for w in d["warning"]}
        self.assertTrue({"sezione_stale", "classificazioni_con_fonte_non_aggiornata"} <= codes)

    def test_m5_corroboration_inherits_freshness_of_event_sections(self):
        with mock.patch.object(R.Registry, "freshness", self.fake(ann_state="aggiornato")):
            d = build(self.reg)
        self.assertEqual(by(d)[A("C-SC", "a-sci-ver")]["freschezza"]["stato"], "aggiornato")
        def stale_agenda(self, now, stale_after_hours=None):
            r = TestFresh.fake()(self, now, stale_after_hours)
            r["per_kind"]["classeviva/agenda_event"]["stale"] = True
            r["per_kind"]["classeviva/agenda_event"]["motivo"] = "oltre_soglia"
            return r
        TestFresh = Freschezza
        with mock.patch.object(R.Registry, "freshness", stale_agenda):
            e = build(self.reg)
        self.assertEqual(by(e)[A("C-SC", "a-sci-ver")]["freschezza"]["stato"], "stale")      # usa M5 -> dipende dall'agenda
        self.assertEqual(by(e)[A("C-SC", "a-sci-nome")]["freschezza"]["stato"], "aggiornato")  # nessuna corroborazione usata
        self.assertEqual(by(e)[N(1001)]["freschezza"]["stato"], "aggiornato")


# ------------------------------------------------------------------------------------------------ primitive condivise
class Primitive(unittest.TestCase):
    def test_common_module_is_pure_and_shared(self):
        self.assertIs(M.CM, CM)
        self.assertIs(L.CM, CM)
        self.assertEqual(CM.short_hash("a", 1), CM.short_hash("a", "1"))
        self.assertEqual(CM.cap_text("  abcdef ", 3), ("abc", True))
        self.assertEqual(CM.worst_freshness("aggiornato", "stale"), "stale")
        self.assertEqual(CM.prove("K", "s", "a_favore", "forte", "d"), {"codice": "K", "segnale": "s", "esito": "a_favore", "peso": "forte", "dettaglio": "d"})

    def test_m5_aliases_keep_working(self):
        self.assertIs(L._p, CM.prove)
        self.assertIs(L._worst, CM.worst_freshness)
        self.assertEqual(L._TEST.pattern, CM.TEST_RE.pattern)
        self.assertEqual(L.link_version(), "r1-15cce5")                                        # M5 invariata dopo la rifattorizzazione

    def test_m5_output_unaffected_by_m6_layer(self):
        reg = F.make_registry()
        a = L.build(reg, SUBJ, NOW, manual=MAN)
        build(reg)
        b = L.build(reg, SUBJ, NOW, manual=MAN)
        self.assertEqual(dump(a), dump(b))
        self.assertEqual(L.SCHEMA, "cloud.school.links/1")
        self.assertEqual(C3.SCHEMA, "cloud.school.context/3")


# ------------------------------------------------------------------------------------------------ tool MCP
class ServerTools(unittest.TestCase):
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

    def sha(self, p):
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def populate(self):
        reg = F.make_registry(path=self.db)
        reg.close()
        os.makedirs(os.path.dirname(self.manual), exist_ok=True)
        with open(self.manual, "w") as f:
            json.dump(MAN, f)

    def test_missing_registry_clear_error_nothing_created(self):
        for f, args in ((self.server.school_communications, ()), (self.server.school_communication_explain, ("cvv:1",))):
            with self.assertRaises(ValueError) as cm:
                f(*args)
            self.assertIn("registro locale assente", str(cm.exception))
        self.assertFalse(os.path.exists(self.db))

    def test_tool_schemas_and_previous_tools_unchanged(self):
        t = self.server.srv._tools
        d = t["school_communications"]["def"]
        self.assertEqual(set(d["inputSchema"]["properties"]), {"materia", "classe", "stato", "fonte", "perimetro", "dal", "al",
                                                               "includi_generali", "dettaglio", "soglia_ore"})
        self.assertFalse(d["inputSchema"].get("required"))
        self.assertIn("SOLA LETTURA", d["description"])
        e = t["school_communication_explain"]["def"]
        self.assertEqual(e["inputSchema"]["required"], ["target"])
        self.assertIn("SOLA LETTURA", e["description"])
        self.assertEqual(set(t["school_links"]["def"]["inputSchema"]["properties"]), {"materia", "stato", "relazione", "soglia_ore"})
        self.assertEqual(set(t["school_context_v3"]["def"]["inputSchema"]["properties"]), {"subject", "include_raw", "docente"})
        self.assertEqual(set(t["school_overview"]["def"]["inputSchema"]["properties"]), {"soglia_ore"})

    def test_tools_read_registry_without_writing(self):
        self.populate()
        h0, files0, mh = self.sha(self.db), sorted(os.listdir(self.d)), self.sha(self.manual)
        out = self.server.school_communications(perimetro="tutti")
        self.assertEqual(out["schema"], "cloud.school.communications/1")
        self.assertTrue(out["comunicazioni"])
        self.assertFalse(out["verifica_nessuna_scrittura"]["file_modificati"])
        self.assertFalse(any(out["scritture"].values()))
        ex = self.server.school_communication_explain("cvv:1001")
        self.assertEqual(ex["schema"], "cloud.school.communications.explain/1")
        self.assertEqual(len(ex["comunicazioni"]), 1)
        self.assertEqual(self.server.school_communications(materia="scienze", stato="proposto")["filtri"],
                         {"materia": "scienze", "classe": None, "stato": "proposto", "fonte": None, "perimetro": "operativo", "dal": None,
                          "al": None, "includi_generali": False, "dettaglio": False})
        self.assertFalse(any(self.server.school_links()["verifica_nessuna_scrittura"].values()))
        self.assertEqual((h0, files0, mh), (self.sha(self.db), sorted(os.listdir(self.d)), self.sha(self.manual)))

    def test_tools_leave_m4_m5_outputs_unchanged(self):
        self.populate()
        before = {k: dump({a: b for a, b in getattr(self.server, k)().items() if a not in ("riferimento_temporale", "freschezza", "provenienza")})
                  for k in ("school_links", "school_overview")}
        self.server.school_communications(); self.server.school_communication_explain("cvv:1001")
        after = {k: dump({a: b for a, b in getattr(self.server, k)().items() if a not in ("riferimento_temporale", "freschezza", "provenienza")})
                 for k in ("school_links", "school_overview")}
        self.assertEqual(before, after)

    def test_tool_errors(self):
        self.populate()
        for f, kw in ((self.server.school_communications, {"stato": "certo"}), (self.server.school_communications, {"classe": "boh"}),
                      (self.server.school_communications, {"materia": "religione"}), (self.server.school_communications, {"perimetro": "x"}),
                      (self.server.school_communications, {"dal": "oggi"}), (self.server.school_communication_explain, {"target": ""})):
            with self.assertRaises(ValueError):
                f(**kw)


if __name__ == "__main__":
    unittest.main()
