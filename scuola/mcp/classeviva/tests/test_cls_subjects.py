"""cls_subjects: corso Classroom -> materia. Proposte (E1/E2/E3) mai confermate in automatico. Dati SINTETICI
(nomi dei corsi nella forma di quelli reali; docenti inventati)."""
import copy
import inspect
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import cls_subjects as CS       # noqa: E402
import cvv_subject as subj      # noqa: E402

SUBJECTS = subj.build_subjects([
    {"id": 1, "description": "LINGUA E LETTERATURA ITALIANA", "teachers": [{"teacherName": "ROSSI' MARIA"}]},
    {"id": 2, "description": "LINGUA E CULTURA LATINA", "teachers": [{"teacherName": "ROSSI' MARIA"}]},
    {"id": 3, "description": "MATEMATICA", "teachers": [{"teacherName": "BIANCHI LUCA"}]},
    {"id": 4, "description": "FISICA", "teachers": [{"teacherName": "BIANCHI LUCA"}]},
    {"id": 5, "description": "EDUCAZIONE CIVICA SUP", "teachers": [{"teacherName": "ROSSI' MARIA"}, {"teacherName": "BIANCHI LUCA"}]},
    {"id": 6, "description": "STUDIO ASSISTITO", "teachers": [{"teacherName": "ROSSI' MARIA"}]},
    {"id": 7, "description": "DISEGNO E STORIA DELL'ARTE", "teachers": [{"teacherName": "VERDI LIA"}]},
])


def course(cid, name, subject=None, state="ACTIVE"):
    c = {"id": cid, "name": name, "courseState": state}
    if subject is not None:
        c["subject"] = subject
    return c


REAL_LIKE = [
    course("c1", "4Z Mate & Fisica (prova) 2026/27"),
    course("c2", "ITALIANO, LATINO", subject="ITALIANO, LATINO"),
    course("c3", "Lettere - Rossì", subject="Italiano, Latino"),
    course("c4", "Storia e Filosofia (prova) - 4Z"),
    course("c5", "3^ Z (prova) - Scienze naturali", subject="SCIENZE NATURALI"),
    course("c6", "SCIENZE 4 (prova)", subject="Scienze"),
    course("c7", "4X -  Inglese"),
    course("c8", "4^ZC Storia dell’arte (prova)"),
    course("c9", "RELIGIONE 2026-2027", subject="RELIGIONE CATTOLICA"),
    course("c10", "Scienze Motorie"),
    course("c11", "4^ ZZ - ABC"),
    course("c12", "Laboratorio Teatro (prova) - 2026/27"),
    course("c13", "GEOSTORIA II ZZ", state="ARCHIVED"),
]


def rows(courses=REAL_LIKE, manual=None, subjects=SUBJECTS):
    return {r["course_id"]: r for r in CS.classify(courses, subjects, manual)["corsi"]}


class Rules(unittest.TestCase):
    def test_e1_subject_field(self):
        r = rows([course("x", "Corso X", subject="Italiano, Latino")])["x"]
        self.assertEqual((r["categoria"], r["materie_proposte"]), ("proposed", ["italiano", "latino"]))
        self.assertEqual([e["regola"] for e in r["evidenze"]], ["E1"])
        self.assertEqual(rows([course("x", "Y", subject="SCIENZE NATURALI")])["x"]["materie_proposte"], ["scienze"])

    def test_e2_course_name_whole_words(self):
        r = rows()
        self.assertEqual(r["c1"]["materie_proposte"], ["fisica", "matematica"])
        self.assertEqual(r["c4"]["materie_proposte"], ["filosofia", "storia"])
        self.assertEqual(r["c7"]["materie_proposte"], ["inglese"])
        for name in ("Matematerie", "Fisicamente", "Storiella", "Mateus"):     # niente parole parziali
            self.assertEqual(rows([course("x", name)])["x"]["categoria"], "non_attributed", name)

    def test_e2_lettere_alone_is_weak(self):
        r = rows([course("x", "Lettere 4S")])["x"]
        self.assertEqual((r["categoria"], r["materie_proposte"]), ("proposed", ["italiano", "latino"]))
        self.assertEqual([e["forza"] for e in r["evidenze"]], ["debole"])
        self.assertIn("debole", r["motivazione"])
        r = rows([course("y", "Corso Y", subject="Lettere")])["y"]                  # anche nel campo subject (E1)
        self.assertEqual(([e["regola"] for e in r["evidenze"]], [e["forza"] for e in r["evidenze"]]), (["E1"], ["debole"]))
        self.assertIn("debole", r["motivazione"])

    def test_e3_teacher_surname(self):
        r = rows()["c3"]
        self.assertEqual((r["categoria"], r["materie_proposte"]), ("proposed", ["italiano", "latino"]))
        e3 = [e for e in r["evidenze"] if e["regola"] == "E3"]
        self.assertEqual(len(e3), 1)
        self.assertEqual(e3[0]["materie"], ["italiano", "latino"])                 # civica/studio mai dal docente
        self.assertIn("ROSSI' MARIA", e3[0]["testo"])
        self.assertEqual({e["regola"] for e in r["evidenze"]}, {"E1", "E2", "E3"})

    def test_e3_only_whole_word_and_min_length(self):
        self.assertEqual(rows([course("x", "Rossiniana")])["x"]["categoria"], "non_attributed")
        idx = CS.teacher_index(subj.build_subjects([{"id": 1, "description": "MATEMATICA",
                                                     "teachers": [{"teacherName": "LI MEI"}]}]))
        self.assertEqual(idx, {})                                                   # cognome troppo corto: ignorato

    def test_e3_alone_produces_proposal(self):
        r = rows([course("x", "Corso Bianchi")])["x"]
        self.assertEqual((r["categoria"], r["materie_proposte"]), ("proposed", ["fisica", "matematica"]))


class Categories(unittest.TestCase):
    def test_real_like_table(self):
        r = rows()
        exp = {"c1": "proposed", "c2": "proposed", "c3": "proposed", "c4": "proposed", "c5": "proposed",
               "c6": "proposed", "c7": "proposed", "c8": "excluded", "c9": "excluded", "c10": "excluded",
               "c11": "non_attributed", "c12": "non_attributed", "c13": "non_attributed"}
        self.assertEqual({k: v["categoria"] for k, v in r.items()}, exp)

    def test_art_is_never_history(self):
        r = rows()["c8"]
        self.assertEqual((r["categoria"], r["materie_proposte"]), ("excluded", []))
        self.assertNotIn("storia", json.dumps(r["evidenze"][0]["materie"]))
        self.assertEqual(rows([course("x", "Disegno e storia dell'arte")])["x"]["categoria"], "excluded")

    def test_sport_is_never_science_or_physics(self):
        self.assertEqual(rows()["c10"]["categoria"], "excluded")
        self.assertEqual(rows([course("x", "Educazione fisica")])["x"]["categoria"], "excluded")

    def test_both_science_courses_stay_proposed(self):
        r = rows()
        self.assertEqual((r["c5"]["categoria"], r["c6"]["categoria"]), ("proposed", "proposed"))

    def test_ambiguous_when_e1_and_e2_disagree(self):
        r = rows([course("x", "Inglese", subject="Matematica")])["x"]
        self.assertEqual(r["categoria"], "ambiguous")
        self.assertEqual(r["materie_proposte"], ["inglese", "matematica"])

    def test_ambiguous_when_agent_and_excluded_mixed(self):
        self.assertEqual(rows([course("x", "Religione e Storia")])["x"]["categoria"], "ambiguous")

    def test_shared_course_flag(self):
        self.assertTrue(rows()["c1"]["condiviso"])
        self.assertFalse(rows()["c7"]["condiviso"])


class Manual(unittest.TestCase):
    def test_no_manual_means_no_confirmation(self):
        rep = CS.classify(REAL_LIKE, SUBJECTS, None)
        self.assertEqual(rep["conteggi"]["confirmed"], 0)
        self.assertTrue(all(r["materie_confermate"] == [] for r in rep["corsi"]))
        self.assertFalse(rep["manuale_presente"])

    def test_minimal_empty_manual(self):
        empty = {"schema": CS.MANUAL_SCHEMA, "subjects": {k: {"classroom_course_ids": []} for k in CS.AGENT_SUBJECTS}}
        rep = CS.classify(REAL_LIKE, SUBJECTS, empty)
        self.assertEqual((rep["conteggi"]["confirmed"], rep["avvisi"]), (0, []))

    def test_confirmation_only_from_manual_and_shared(self):
        man = {"subjects": {"matematica": {"classroom_course_ids": ["c1"]}, "fisica": {"classroom_course_ids": ["c1"]}}}
        r = rows(manual=man)["c1"]
        self.assertEqual((r["categoria"], r["materie_confermate"], r["condiviso"]), ("confirmed", ["fisica", "matematica"], True))
        self.assertEqual(rows(manual=man)["c4"]["categoria"], "proposed")          # le altre restano proposte

    def test_manual_override_beats_automatic_category(self):
        man = {"subjects": {"italiano": {"classroom_course_ids": ["c11"]}}}       # FSL confermato a mano
        r = rows(manual=man)["c11"]
        self.assertEqual((r["categoria"], r["materie_confermate"], r["materie_proposte"]), ("confirmed", ["italiano"], []))

    def test_manual_validation(self):
        man = {"subjects": {"religione": {"classroom_course_ids": ["c9"]}, "matematica": {"classroom_course_ids": ["c1", 7, ""]},
                            "storia": {"classroom_course_ids": "c4", "nota": "x"}}}
        by, warn = CS.validate_manual(man)
        self.assertEqual(by, {"matematica": ["7", "c1"]})
        self.assertTrue(any("religione" in w for w in warn))
        self.assertTrue(any("storia" in w for w in warn))
        rep = CS.classify(REAL_LIKE, SUBJECTS, {"subjects": {"latino": {"classroom_course_ids": ["c404"]}}})
        self.assertTrue(any("c404" in w for w in rep["avvisi"]))

    def test_optional_extra_fields_accepted(self):
        man = {"schema": CS.MANUAL_SCHEMA, "subjects": {"inglese": {"classroom_course_ids": ["c7"], "nota": "ok",
                                                                    "docente_dichiarato": "x"}}}
        self.assertEqual(rows(manual=man)["c7"]["categoria"], "confirmed")

    def test_proposed_for(self):
        rep = CS.classify(REAL_LIKE, SUBJECTS, {"subjects": {"matematica": {"classroom_course_ids": ["c1"]}}})
        self.assertEqual(CS.proposed_for(rep, "matematica"), [])
        self.assertEqual(CS.proposed_for(rep, "fisica"), ["c1"])                 # confermato per mate, proposto per fisica
        self.assertEqual(CS.proposed_for(rep, "scienze"), ["c5", "c6"])
        self.assertEqual(CS.proposed_for(rep, "storia"), ["c4"])                 # mai il corso di arte


class Properties(unittest.TestCase):
    def test_pure_deterministic_input_untouched(self):
        courses, man = copy.deepcopy(REAL_LIKE), {"subjects": {"fisica": {"classroom_course_ids": ["c1"]}}}
        before = json.dumps([courses, man], sort_keys=True)
        a = CS.classify(courses, SUBJECTS, man)
        b = CS.classify(list(reversed(courses)), SUBJECTS, man)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(json.dumps([courses, man], sort_keys=True), before)

    def test_no_io_in_source(self):
        src = inspect.getsource(CS)
        for bad in ("open(", "urllib", "socket", "sqlite", "subprocess", ".now(", "os.path"):
            self.assertNotIn(bad, src, bad)

    def test_accepts_registry_records(self):
        recs = [{"raw": course("c7", "4X - Inglese"), "version": 1}]
        self.assertEqual(rows(recs)["c7"]["categoria"], "proposed")


if __name__ == "__main__":
    unittest.main()
