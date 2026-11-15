"""M5 - collegamenti e conflitti derivati ClasseViva <-> Classroom (cloud.school.links/1): casi sintetici con la forma dei casi reali,
principi (mai 'confermato', falsi positivi, provenienza, determinismo, non-mutazione), finestre/date, explain, filtri, tool MCP."""
import copy, hashlib, json, os, sys, tempfile, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import links_fixtures as F
import cls_registry as CL
import cvv_cls_links as L
import cvv_registry as CR
import registry as R
import school_context as SC

SUBJ, NOW, MAN = F.SUBJECTS, F.NOW, F.MANUAL


def dump(o):
    return json.dumps(o, ensure_ascii=False, sort_keys=True, default=str)


def counts(reg):
    return {t: reg.db.execute("SELECT count(*) FROM %s" % t).fetchone()[0] for t in ("records", "versions", "events", "runs", "projections")}


def build(reg, **kw):
    return L.build(reg, SUBJ, NOW, manual=kw.pop("manual", MAN), **kw)


def pair(doc, a, b, key="collegamenti"):
    return [x for x in doc[key] if x["a"]["uid"] == a and x["b"]["uid"] == b]


def all_uids(doc):
    out = set()
    for x in doc["collegamenti"] + doc["non_determinabili"]:
        out |= {x["a"]["uid"], x["b"]["uid"]}
    for x in doc["conflitti"]:
        out.add(x["elemento"]["uid"])
        out |= {c["lato"]["uid"] for c in x["candidati"]}
    return out


def codes(link):
    return {p["codice"] for p in link["interpretazione"]["prove"]}


def mini(events=(), works=(), materials=(), anns=(), lessons=()):
    return F.make_registry(events=list(events), lessons=list(lessons), works=list(works), materials=list(materials),
                           announcements=list(anns))


class Base(unittest.TestCase):
    def setUp(self):
        self.reg = F.make_registry()
        self.doc = build(self.reg)


# ------------------------------------------------------------------------------------------------ forma
class Shape(Base):
    def test_schema_levels_and_sections(self):
        d = self.doc
        self.assertEqual(d["schema"], "cloud.school.links/1")
        self.assertEqual(set(d["livelli"]), {"dati", "interpretazione", "freschezza"})
        for k in ("collegamenti", "conflitti", "non_determinabili", "riepilogo", "freschezza", "warning", "informazioni_mancanti",
                  "provenienza", "criteri", "regole_per_gli_agenti", "link_version", "natura"):
            self.assertIn(k, d)
        self.assertEqual(set(d["riepilogo"]["per_stato"]), set(L.STATES))
        self.assertEqual(d["riepilogo"]["per_stato"]["confermato"], 0)
        self.assertEqual(set(d["freschezza"]["per_sezione"]), {"agenda", "lezioni", "classroom"})
        self.assertTrue(d["link_version"].startswith("r%d-" % L.LINK_RULES_REV))

    def test_every_link_has_levels_provenance_and_evidence(self):
        for x in self.doc["collegamenti"]:
            for side in ("a", "b"):
                s = x[side]
                self.assertEqual(set(s), {"sistema", "tipo", "uid", "dati", "interpretazione", "fonte"})
                self.assertEqual(set(s["fonte"]) >= {"uid", "registry_version", "content_hash"}, True)
            self.assertEqual((x["a"]["sistema"], x["b"]["sistema"]), ("classeviva", "classroom"))
            i = x["interpretazione"]
            self.assertTrue(i["prove"] and i["regola"] and i["motivazione"] and i["link_version"] == self.doc["link_version"])
            for p in i["prove"]:
                self.assertEqual(set(p), {"codice", "segnale", "esito", "peso", "dettaglio"})
                self.assertIn(p["esito"], ("a_favore", "contro", "neutro"))
            self.assertIn("freschezza", x)
            self.assertNotIn("_a_section", x)

    def test_missing_info_declares_limits(self):
        txt = dump(self.doc["informazioni_mancanti"])
        for needle in ("conferme manuali", "date relative", "corsi NON confermati", "annunci di corsi condivisi"):
            self.assertIn(needle.split()[0], txt)
        self.assertIn("NON confermati", txt)


# ------------------------------------------------------------------------------------------------ casi reali (sintetici)
class Cases(Base):
    def test_math_simulation_is_probable_prepara_not_identity(self):
        (x,) = pair(self.doc, "cvv:1000101", "cls:C-MF/w-sim")
        self.assertEqual((x["relazione"], x["stato"], x["interpretazione"]["regola"]), ("prepara", "probabile", "R1"))
        self.assertTrue({"K1", "K2", "K4", "K7"} <= codes(x))
        k1 = [p for p in x["interpretazione"]["prove"] if p["codice"] == "K1"][0]
        self.assertEqual((k1["esito"], k1["peso"]), ("a_favore", "forte"))
        self.assertNotEqual(x["relazione"], "stessa_attivita")

    def test_physics_probable(self):
        (x,) = pair(self.doc, "cvv:1000102", "cls:C-MF/w-fis")
        self.assertEqual((x["relazione"], x["stato"]), ("prepara", "probabile"))

    def test_trap_previous_school_year_never_appears(self):
        for uid in ("cls:C-MF/w-fis-old", "cls:C-SF/m-fil-old", "cls:C-MF/w-pasqua"):
            self.assertNotIn(uid, all_uids(self.doc), uid)
        self.assertGreater(self.doc["riepilogo"]["veti_su_coppie"]["veto_anno_scolastico_diverso"], 0)

    def test_cross_subject_and_contradictory_date_not_linked(self):
        # w-cross: topic Fisica, "verifica del 2 novembre 2026" (data della verifica di MATEMATICA)
        self.assertNotIn("cls:C-MF/w-cross", all_uids(self.doc))

    def test_history_topic_links_are_proposed_and_short_name_is_not_enough(self):
        for uid in ("cls:C-SF/m-sto1", "cls:C-SF/m-sto2"):
            (x,) = pair(self.doc, "cvv:1000106", uid)
            self.assertEqual((x["relazione"], x["stato"], x["interpretazione"]["regola"]), ("stesso_argomento", "proposto", "R3"))
        self.assertNotIn("cls:C-SF/m-sto3", all_uids(self.doc))              # "Luigi XIV": 1 parola corta -> evidenza debole

    def test_philosophy_material_proposed_old_year_rejected(self):
        (x,) = pair(self.doc, "cvv:1000105", "cls:C-SF/m-fil")
        self.assertEqual(x["stato"], "proposto")

    def test_sciences_announcements_proposed_via_monosubject_course(self):
        for uid in ("cls:C-SC/a-sci1", "cls:C-SC/a-sci2"):
            (x,) = pair(self.doc, "cvv:1000103", uid)
            self.assertEqual((x["stato"], x["b"]["interpretazione"]["stato_materia"]), ("proposto", "determinata"))
        # "per lunedi" e' una data relativa: non interpretata
        a2 = [x for x in self.doc["collegamenti"] if x["b"]["uid"] == "cls:C-SC/a-sci2"][0]
        self.assertNotIn("K1", codes(a2))

    def test_english_conflict_between_two_events_no_choice(self):
        c = [x for x in self.doc["conflitti"] if x["elemento"]["uid"] == "cls:C-EN/a-ing"]
        self.assertEqual(len(c), 1)
        c = c[0]
        self.assertEqual((c["tipo"], c["stato"], c["interpretazione"]["regola"]), ("segnali_discordanti", "conflitto", "RC2"))
        self.assertEqual({y["lato"]["uid"] for y in c["candidati"]}, {"cvv:1000108", "cvv:1000107"})
        self.assertFalse([x for x in self.doc["collegamenti"] if x["b"]["uid"] == "cls:C-EN/a-ing"])
        self.assertIn("NESSUNA fonte e' stata modificata", c["interpretazione"]["nota"])

    def test_shared_course_announcement_is_not_determinable(self):
        self.assertFalse([x for x in self.doc["collegamenti"] if x["b"]["uid"] == "cls:C-LE/a-ita"])
        (x,) = [x for x in self.doc["non_determinabili"] if x["b"]["uid"] == "cls:C-LE/a-ita"]
        self.assertEqual((x["a"]["uid"], x["stato"], x["interpretazione"]["regola"]), ("cvv:1000104", "non_determinabile", "RN1"))
        self.assertEqual(x["b"]["interpretazione"]["stato_materia"], "da_verificare")
        self.assertEqual(x["b"]["interpretazione"]["candidati"], ["italiano", "latino"])

    def test_shared_course_work_without_topic_is_not_determinable(self):
        (x,) = [x for x in self.doc["non_determinabili"] if x["b"]["uid"] == "cls:C-MF/w-nosubj"]
        self.assertEqual((x["a"]["uid"], x["interpretazione"]["evidenza_residua"]), ("cvv:1000102", "forte"))
        self.assertFalse([x for x in self.doc["collegamenti"] if x["b"]["uid"] == "cls:C-MF/w-nosubj"])

    def test_event_without_evidence_has_nothing_and_says_so(self):
        self.assertNotIn("cvv:1000109", all_uids(self.doc))                  # "Tema in classe"
        e = L.explain(self.reg, SUBJ, NOW, "cvv:1000109", manual=MAN)
        self.assertEqual(e["tipo_richiesta"], "uid")
        self.assertIn("NESSUN collegamento", e["esito"])

    def test_unconfirmed_course_is_excluded_and_counted(self):
        self.assertNotIn("cls:C-X/a-x", all_uids(self.doc))
        self.assertEqual(self.doc["riepilogo"]["esclusi"]["classroom_corsi_non_confermati"], 1)

    def test_lesson_topic_best_match_and_empty_title(self):
        (x,) = pair(self.doc, "cvv:lesson:1", "cls:C-MF/m-mat1")
        self.assertEqual((x["relazione"], x["stato"], x["interpretazione"]["regola"]), ("stesso_argomento", "proposto", "R5"))
        self.assertFalse(pair(self.doc, "cvv:lesson:2", "cls:C-MF/m-mat1"))   # corrispondenza peggiore: non collegata
        self.assertNotIn("cls:C-MF/m-mat2", all_uids(self.doc))              # nessuna parola specifica

    def test_lesson_tie_is_a_conflict(self):
        (c,) = [x for x in self.doc["conflitti"] if x["elemento"]["uid"] == "cls:C-MF/m-int"]
        self.assertEqual((c["tipo"], c["interpretazione"]["regola"]), ("candidati_multipli", "RC1"))
        self.assertEqual({y["lato"]["uid"] for y in c["candidati"]}, {"cvv:lesson:4", "cvv:lesson:5"})
        self.assertFalse([x for x in self.doc["collegamenti"] if x["b"]["uid"] == "cls:C-MF/m-int"])

    def test_deadline_links_and_deadline_conflict(self):
        (x,) = pair(self.doc, "cvv:3000001", "cls:C-MF/w-hw1")
        self.assertEqual((x["relazione"], x["stato"], x["interpretazione"]["regola"]), ("stessa_scadenza", "probabile", "R6"))
        (c,) = [x for x in self.doc["conflitti"] if x["elemento"]["uid"] == "cls:C-MF/w-hw2"]
        self.assertEqual((c["tipo"], c["valori"]), ("data_discordante", {"classeviva": "2026-11-11", "classroom": ["2026-11-13"]}))
        self.assertFalse([x for x in self.doc["collegamenti"] if x["b"]["uid"] == "cls:C-MF/w-hw2"])

    def test_teacher_signal_is_only_corroborative(self):
        (x,) = [x for x in self.doc["non_determinabili"] if x["b"]["uid"] == "cls:C-LE/a-ita"]
        k6 = [p for p in x["interpretazione"]["prove"] if p["codice"] == "K6"]
        self.assertEqual(len(k6), 1)
        self.assertEqual(k6[0]["peso"], "corroborativa")
        self.assertEqual(x["interpretazione"]["evidenza_residua"], "media")      # K6 non alza il livello


# ------------------------------------------------------------------------------------------------ principi
class Principles(Base):
    def test_engine_never_produces_confirmed(self):
        self.assertIn("confermato", L.STATES)
        self.assertEqual(L.RESERVED_STATES, ("confermato",))
        self.assertTrue(all(x["stato"] in ("probabile", "proposto") for x in self.doc["collegamenti"]))
        self.assertEqual(build(self.reg, stato="confermato")["collegamenti"], [])
        # nemmeno una "conferma" dichiarata in school_manual.json cambia qualcosa
        man = copy.deepcopy(MAN)
        man["links"] = {"confirmed": [["cvv:1000101", "cls:C-MF/w-sim"]]}
        man["subjects"]["matematica"]["links_confermati"] = ["cls:C-MF/w-sim"]
        self.assertEqual(dump(build(self.reg, manual=man)), dump(self.doc))

    def test_provenance_matches_registry(self):
        reg = self.reg
        rows = self.doc["collegamenti"]
        self.assertTrue(rows)
        for x in rows:
            for s in (x["a"], x["b"]):
                if s["sistema"] == "classroom":
                    src, kind, su = "classroom", s["tipo"], s["uid"][4:]
                else:
                    src, kind, su = "classeviva", ("lesson" if s["tipo"] == "lesson" else "agenda_event"), CR.source_uid_of(s["uid"])
                rec = reg.get_record(src, kind, su)
                self.assertEqual((s["fonte"]["registry_version"], s["fonte"]["content_hash"]), (rec["version"], rec["content_hash"]))

    def test_registry_is_not_modified(self):
        before = counts(self.reg)
        build(self.reg); L.explain(self.reg, SUBJ, NOW, "cvv:1000101", manual=MAN); build(self.reg, materia="fisica")
        self.assertEqual(before, counts(self.reg))

    def test_compute_does_not_mutate_input(self):
        prep = L.collect(self.reg, SUBJ, MAN)
        snap = copy.deepcopy(prep)
        a = L.compute(prep)
        self.assertEqual(prep, snap)
        self.assertEqual(dump(a), dump(L.compute(prep)))

    def test_deterministic_and_order_independent(self):
        clock = lambda: "2026-11-01T10:00:00+00:00"          # noqa: E731
        d1 = build(F.make_registry(clock=clock))
        d2 = build(F.make_registry(clock=clock, reverse=True))
        self.assertEqual(dump(d1), dump(d2))
        self.assertEqual(dump(build(self.reg)), dump(build(self.reg)))

    def test_link_id_follows_source_versions_and_rules_version(self):
        (x,) = pair(self.doc, "cvv:1000101", "cls:C-MF/w-sim")
        # un record Classroom cambia -> nuova versione/hash -> nuovo link_id
        changed = [dict(w) for w in F.WORKS]
        for w in changed:
            if w["id"] == "w-sim":
                w["description"] = "ora con una descrizione"
        d2 = build(F.make_registry(works=changed))
        (y,) = pair(d2, "cvv:1000101", "cls:C-MF/w-sim")
        self.assertNotEqual(x["link_id"], y["link_id"])
        self.assertNotEqual(x["b"]["fonte"]["content_hash"], y["b"]["fonte"]["content_hash"])
        # le regole cambiano -> link_version e link_id cambiano
        with mock.patch.object(L, "WINDOW_PREPARA", 10):
            d3 = build(self.reg)
        self.assertNotEqual(d3["link_version"], self.doc["link_version"])
        self.assertNotEqual(pair(d3, "cvv:1000101", "cls:C-MF/w-sim")[0]["link_id"], x["link_id"])

    def test_property_no_cross_subject_no_cross_year_no_duplicates(self):
        seen = set()
        for x in self.doc["collegamenti"]:
            self.assertEqual(x["a"]["interpretazione"]["materia"], x["b"]["interpretazione"]["materia"])
            self.assertIsNotNone(x["a"]["interpretazione"]["materia"])
            self.assertEqual((x["a"]["interpretazione"]["stato_materia"], x["b"]["interpretazione"]["stato_materia"]),
                             ("determinata", "determinata"))
            from it_dates import school_year_start
            self.assertEqual(school_year_start(x["a"]["dati"]["data"]), school_year_start(x["b"]["dati"]["data_creazione"]))
            k = (x["a"]["uid"], x["b"]["uid"], x["relazione"])
            self.assertNotIn(k, seen)
            seen.add(k)

    def test_probable_links_are_fully_explained(self):
        for x in self.doc["collegamenti"]:
            if x["stato"] == "probabile":
                self.assertTrue({"K1", "K2", "K7"} <= codes(x))
                self.assertTrue(any(p["codice"] == "K1" and p["peso"] == "forte" for p in x["interpretazione"]["prove"]))

    def test_conflicts_do_not_choose(self):
        for c in self.doc["conflitti"]:
            self.assertEqual(c["stato"], "conflitto")
            self.assertIn("NESSUN candidato", c["interpretazione"]["nota"])
            self.assertTrue(c["candidati"])

    def test_non_determinable_are_not_links(self):
        linked = {(x["a"]["uid"], x["b"]["uid"]) for x in self.doc["collegamenti"]}
        for x in self.doc["non_determinabili"]:
            self.assertNotIn((x["a"]["uid"], x["b"]["uid"]), linked)

    def test_warnings_for_conflicts_and_non_determinable(self):
        w = {x["codice"] for x in self.doc["warning"]}
        self.assertTrue({"conflitti_presenti", "collegamenti_non_determinabili"} <= w)


# ------------------------------------------------------------------------------------------------ unita'
class Units(unittest.TestCase):
    def test_stem_and_specific_stems(self):
        self.assertEqual(L.specific_stems("Equazioni e disequazioni irrazionali"), {"equazion", "disequazion", "irrazional"})
        self.assertEqual(L.specific_stems("Soluzione disequazione irrazionale"), {"disequazion", "irrazional"})
        self.assertEqual(L.specific_stems("Verifica Matematica: compiti ed esercizi per domani"), set())
        self.assertEqual(L.specific_stems("Luigi XIV"), {"luigi"})
        self.assertEqual(L.specific_stems("Ariosto"), {"ariost"})
        self.assertEqual(L.specific_stems("la di e il"), set())
        self.assertEqual(L.stem("carlo"), "carlo")
        self.assertEqual(L.stem("riforma"), "riform")

    def test_media_threshold(self):
        self.assertTrue(L._media({"ariost"}))
        self.assertFalse(L._media({"carlo"}))
        self.assertFalse(L._media({"luter"}))
        self.assertTrue(L._media({"carlo", "luter"}))
        self.assertFalse(L._media(set()))

    def test_item_subject(self):
        self.assertEqual(L.item_subject(["inglese"], None)["stato"], "determinata")
        self.assertEqual(L.item_subject(["matematica", "fisica"], "Matematica")["key"], "matematica")
        self.assertEqual(L.item_subject(["matematica", "fisica"], "Fisica")["key"], "fisica")
        for topic in (None, "Laboratorio", "Progetto Robotica", "Matematica e Fisica", "Latino", "Scrittura"):
            r = L.item_subject(["matematica", "fisica"], topic)
            self.assertEqual((r["key"], r["stato"], r["candidati"]), (None, "da_verificare", ["fisica", "matematica"]), topic)

    def test_compat(self):
        k = lambda key, cand=(): {"key": key, "candidati": list(cand)}          # noqa: E731
        self.assertEqual(L.compat(k("a"), k("a")), "uguale")
        self.assertEqual(L.compat(k("a"), k("b")), "incompatibile")
        self.assertEqual(L.compat(k("a"), k(None, ["a", "b"])), "indeterminata")
        self.assertEqual(L.compat(k("c"), k(None, ["a", "b"])), "incompatibile")
        self.assertEqual(L.compat(k("a"), k(None, [])), "indeterminata")


# ------------------------------------------------------------------------------------------------ finestre e date (registri minimi)
class Windows(unittest.TestCase):
    EV = [F.ev(1, "Verifica Matematica", "ROSSI MARIO", "2026-11-02")]

    def doc(self, works=(), events=None, **kw):
        return build(mini(events=self.EV if events is None else events, works=works, **kw))

    def test_window_boundary_21_days(self):
        ok = self.doc([F.work("C-MF", "w", "Prova di allenamento per la verifica del 2 novembre 2026", "2026-10-12", "tM")])
        self.assertTrue(pair(ok, "cvv:1", "cls:C-MF/w"))
        late = self.doc([F.work("C-MF", "w", "Prova di allenamento per la verifica del 2 novembre 2026", "2026-10-11", "tM")])
        self.assertEqual(all_uids(late), set())
        self.assertEqual(late["riepilogo"]["veti_su_coppie"]["veto_fuori_finestra"], 1)

    def test_item_after_event_never_prepares_it(self):
        d = self.doc([F.work("C-MF", "w", "Prova di allenamento per la verifica del 2 novembre 2026", "2026-11-03", "tM")])
        self.assertEqual(all_uids(d), set())
        self.assertEqual(d["riepilogo"]["veti_su_coppie"]["veto_elemento_posteriore"], 1)
        same_day = self.doc([F.work("C-MF", "w", "Prova di allenamento per la verifica del 2 novembre 2026", "2026-11-02", "tM")])
        self.assertTrue(pair(same_day, "cvv:1", "cls:C-MF/w"))

    def test_unattributed_date_is_only_proposed(self):
        d = self.doc([F.work("C-MF", "w", "Ripasso per verifica, esercizi per il 2 novembre", "2026-10-29", "tM")])
        (x,) = pair(d, "cvv:1", "cls:C-MF/w")
        self.assertEqual((x["stato"], x["interpretazione"]["regola"]), ("proposto", "R2"))
        k1 = [p for p in x["interpretazione"]["prove"] if p["codice"] == "K1"][0]
        self.assertEqual(k1["peso"], "media")

    def test_date_alone_is_not_enough(self):
        d = self.doc([F.work("C-MF", "w", "Esercizi per il 2 novembre", "2026-10-29", "tM")])
        self.assertEqual(all_uids(d), set())

    def test_incoherent_weekday_is_not_used(self):
        d = self.doc([F.work("C-MF", "w", "Verifica di giovedi 2 novembre 2026", "2026-10-29", "tM")])      # il 5/10/2026 e' lunedi
        (x,) = pair(d, "cvv:1", "cls:C-MF/w")
        self.assertEqual((x["stato"], x["interpretazione"]["regola"]), ("proposto", "R4"))
        self.assertTrue(any(p["codice"] == "K1" and "NON usata" in p["dettaglio"] for p in x["interpretazione"]["prove"]))

    def test_mention_attributed_to_another_date_is_not_linked(self):
        d = self.doc([F.work("C-MF", "w", "Verifica del 9 novembre", "2026-10-29", "tM")])
        self.assertEqual(all_uids(d), set())

    def test_same_topic_different_date_is_a_conflict(self):
        ev = [F.ev(1, "Verifica Matematica: disequazioni irrazionali", "ROSSI MARIO", "2026-11-02")]
        d = self.doc([F.work("C-MF", "w", "Verifica del 5 novembre: disequazioni irrazionali", "2026-10-29", "tM")], events=ev)
        (c,) = d["conflitti"]
        self.assertEqual((c["tipo"], c["interpretazione"]["regola"], c["valori"]["classeviva"], c["valori"]["classroom"]),
                         ("data_discordante", "RC3", "2026-11-02", ["2026-11-05"]))
        self.assertEqual(d["collegamenti"], [])

    def test_two_equal_candidates_are_a_conflict(self):
        ev = [F.ev(1, "Verifica Matematica", "ROSSI MARIO", "2026-11-02"), F.ev(2, "Verifica Matematica gruppo B", "ROSSI MARIO", "2026-11-02")]
        d = self.doc([F.work("C-MF", "w", "Prova di allenamento per la verifica del 2 novembre 2026", "2026-10-29", "tM")], events=ev)
        (c,) = d["conflitti"]
        self.assertEqual((c["tipo"], len(c["candidati"])), ("candidati_multipli", 2))
        self.assertEqual(d["collegamenti"], [])

    def test_year_less_date_resolves_in_second_part_of_school_year(self):
        ev = [F.ev(1, "Verifica Matematica", "ROSSI MARIO", "2027-01-12")]
        d = self.doc([F.work("C-MF", "w", "Simulazione verifica del 12 gennaio", "2027-01-05", "tM")], events=ev)
        (x,) = pair(d, "cvv:1", "cls:C-MF/w")
        self.assertEqual(x["stato"], "probabile")
        self.assertTrue(any("anno dedotto" in p["dettaglio"] for p in x["interpretazione"]["prove"]))

    def test_lone_mention_with_two_verifiche_is_a_conflict(self):
        ev = [F.ev(1, "Verifica Matematica", "ROSSI MARIO", "2026-11-02"), F.ev(2, "Verifica Matematica 2", "ROSSI MARIO", "2026-11-06")]
        d = self.doc([F.work("C-MF", "w", "Ricordo la verifica", "2026-10-31", "tM")], events=ev)
        self.assertEqual([c["tipo"] for c in d["conflitti"]], ["candidati_multipli"])
        self.assertEqual(d["collegamenti"], [])
        d1 = self.doc([F.work("C-MF", "w", "Ricordo la verifica", "2026-10-31", "tM")])         # una sola verifica: proposto R4
        (x,) = pair(d1, "cvv:1", "cls:C-MF/w")
        self.assertEqual((x["stato"], x["interpretazione"]["regola"]), ("proposto", "R4"))

    def test_draft_and_unpublished_items_are_ignored(self):
        w = F.work("C-MF", "w", "Prova di allenamento per la verifica del 2 novembre 2026", "2026-10-29", "tM")
        w["state"] = "DRAFT"
        d = self.doc([w])
        self.assertEqual(all_uids(d), set())
        self.assertEqual(d["riepilogo"]["esclusi"]["classroom_non_pubblicati"], 1)


# ------------------------------------------------------------------------------------------------ explain, filtri, bordi
class ExplainAndFilters(Base):
    def test_explain_by_uid_link_and_conflict(self):
        e = L.explain(self.reg, SUBJ, NOW, "cvv:1000101", manual=MAN)
        self.assertEqual((e["schema"], e["tipo_richiesta"], len(e["collegamenti"])), ("cloud.school.links.explain/1", "uid", 1))
        lid = e["collegamenti"][0]["link_id"]
        e2 = L.explain(self.reg, SUBJ, NOW, lid, manual=MAN)
        self.assertEqual((e2["tipo_richiesta"], [x["link_id"] for x in e2["collegamenti"]]), ("link_id", [lid]))
        cid = [c for c in self.doc["conflitti"] if c["tipo"] == "segnali_discordanti"][0]["conflitto_id"]
        e3 = L.explain(self.reg, SUBJ, NOW, cid, manual=MAN)
        self.assertEqual((e3["tipo_richiesta"], len(e3["conflitti"])), ("conflitto_id", 1))
        e4 = L.explain(self.reg, SUBJ, NOW, "cvv:1000107", manual=MAN)          # candidato di un conflitto
        self.assertEqual(len(e4["conflitti"]), 1)

    def test_explain_unknown_and_excluded(self):
        e = L.explain(self.reg, SUBJ, NOW, "cvv:99999", manual=MAN)
        self.assertEqual(e["tipo_richiesta"], "sconosciuto")
        self.assertIn("non trovato", e["esito"])
        self.assertEqual(L.explain(self.reg, SUBJ, NOW, "cls:C-X/a-x", manual=MAN)["tipo_richiesta"], "sconosciuto")
        for bad in ("", "  ", None, 5):
            with self.assertRaises(ValueError):
                L.explain(self.reg, SUBJ, NOW, bad, manual=MAN)

    def test_filters(self):
        f = build(self.reg, materia="fisica")
        self.assertTrue(f["collegamenti"])
        self.assertTrue(all("fisica" in x["interpretazione"]["materie_coinvolte"] for x in f["collegamenti"] + f["conflitti"] + f["non_determinabili"]))
        self.assertTrue(all(x["stato"] == "probabile" for x in build(self.reg, stato="probabile")["collegamenti"]))
        self.assertTrue(all(x["relazione"] == "stessa_scadenza" for x in build(self.reg, relazione="stessa_scadenza")["collegamenti"]))
        self.assertEqual(build(self.reg, stato="conflitto")["collegamenti"], [])
        self.assertTrue(build(self.reg, stato="conflitto")["conflitti"])
        self.assertEqual(f["filtri"], {"materia": "fisica", "stato": None, "relazione": None})

    def test_invalid_filters(self):
        for kw in ({"stato": "certo"}, {"relazione": "identica"}, {"materia": "religione"}, {"materia": "astrologia"}, {"materia": ""}):
            with self.assertRaises(ValueError, msg=str(kw)):
                build(self.reg, **kw)

    def test_limits_declare_truncation(self):
        d = L.build(self.reg, SUBJ, NOW, manual=MAN, limits={"collegamenti": 2})
        self.assertEqual(len(d["collegamenti"]), 2)
        w = [x for x in d["warning"] if x["codice"] == "contenuto_parziale"]
        self.assertEqual(len(w), 1)
        self.assertGreater(w[0]["omessi"], 0)


class Edges(unittest.TestCase):
    def test_empty_registry(self):
        d = build(R.Registry(":memory:"))
        self.assertEqual((d["collegamenti"], d["conflitti"], d["non_determinabili"]), ([], [], []))
        self.assertIn("nessun evento", dump(d["informazioni_mancanti"]))

    def test_no_confirmed_courses_means_no_classroom_items(self):
        d = build(F.make_registry(), manual={})
        self.assertEqual((d["collegamenti"], d["conflitti"], d["non_determinabili"]), ([], [], []))
        self.assertEqual(d["riepilogo"]["elementi_considerati"]["elementi_classroom"], 0)
        self.assertIn("nessun corso Classroom CONFERMATO", dump(d["informazioni_mancanti"]))

    def test_freshness_is_operational_and_separate(self):
        d = build(F.make_registry())
        self.assertTrue(set(d["freschezza"]["per_sezione"].values()) <= {"aggiornato", "stale", "senza_lettura"})
        for x in d["collegamenti"]:
            self.assertIn(x["freschezza"]["stato"], ("aggiornato", "stale", "senza_lettura"))
        if any(v != "aggiornato" for v in d["freschezza"]["per_sezione"].values()):
            self.assertIn("collegamenti_con_fonte_non_aggiornata", {w["codice"] for w in d["warning"]})
        # la freschezza e' operativa: non cambia i collegamenti calcolati
        self.assertEqual([x["link_id"] for x in d["collegamenti"]], [x["link_id"] for x in build(F.make_registry())["collegamenti"]])


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
        for fn, args in ((self.server.school_links, ()), (self.server.school_link_explain, ("cvv:1",))):
            with self.assertRaises(ValueError) as cm:
                fn(*args)
            self.assertIn("registro locale assente", str(cm.exception))
        self.assertFalse(os.path.exists(self.db))

    def test_tool_schemas(self):
        t = self.server.srv._tools
        d = t["school_links"]["def"]
        self.assertEqual(set(d["inputSchema"]["properties"]), {"materia", "stato", "relazione", "soglia_ore"})
        self.assertFalse(d["inputSchema"].get("required"))
        self.assertIn("SOLA LETTURA", d["description"])
        e = t["school_link_explain"]["def"]
        self.assertEqual(e["inputSchema"]["required"], ["target"])
        self.assertIn("SOLA LETTURA", e["description"])
        # M4 invariato
        self.assertEqual(set(t["school_context_v3"]["def"]["inputSchema"]["properties"]), {"subject", "include_raw", "docente"})

    def test_tools_read_registry_without_writing(self):
        self.populate()
        h0, files0, mh = self.sha(self.db), sorted(os.listdir(self.d)), self.sha(self.manual)
        out = self.server.school_links()
        self.assertEqual(out["schema"], "cloud.school.links/1")
        self.assertTrue(out["collegamenti"])
        self.assertFalse(out["verifica_nessuna_scrittura"]["file_modificati"])
        self.assertFalse(any(out["scritture"].values()))
        ex = self.server.school_link_explain("cvv:1000101")
        self.assertEqual(ex["schema"], "cloud.school.links.explain/1")
        self.assertEqual(len(ex["collegamenti"]), 1)
        self.assertEqual(self.server.school_links(materia="fisica", stato="probabile")["filtri"],
                         {"materia": "fisica", "stato": "probabile", "relazione": None})
        self.assertEqual((h0, files0, mh), (self.sha(self.db), sorted(os.listdir(self.d)), self.sha(self.manual)))

    def test_tool_errors(self):
        self.populate()
        for fn, kw in ((self.server.school_links, {"stato": "certo"}), (self.server.school_links, {"materia": "religione"}),
                       (self.server.school_link_explain, {"target": ""})):
            with self.assertRaises(ValueError):
                fn(**kw)


if __name__ == "__main__":
    unittest.main()
