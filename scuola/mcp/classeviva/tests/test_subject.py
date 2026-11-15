"""Test della risoluzione materia. Dati FITTIZI (nomi e materie di fantasia strutturalmente simili)."""
import os, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import cvv_subject as S

RAW = [
    {"id": 1, "description": "LINGUA E LETTERATURA ITALIANA", "teachers": [{"teacherName": "ROSSI ANNA"}]},
    {"id": 2, "description": "LINGUA E CULTURA LATINA", "teachers": [{"teacherName": "ROSSI ANNA"}]},
    {"id": 3, "description": "LINGUA E CULTURA STRANIERA 1", "teachers": [{"teacherName": "BIANCHI LUCA"}]},
    {"id": 4, "description": "STORIA", "teachers": [{"teacherName": "VERDI PAOLO"}]},
    {"id": 5, "description": "FILOSOFIA", "teachers": [{"teacherName": "VERDI PAOLO"}]},
    {"id": 6, "description": "MATEMATICA", "teachers": [{"teacherName": "NERI SARA"}]},
    {"id": 7, "description": "FISICA", "teachers": [{"teacherName": "NERI SARA"}]},
    {"id": 8, "description": "SCIENZE NATURALI (BIOLOGIA, CHIMICA, SCIENZE DELLA TERRA)",
     "teachers": [{"teacherName": "GIALLI EVA"}, {"teacherName": "BRUNI ELIA"}]},
    {"id": 9, "description": "DISEGNO E STORIA DELL'ARTE", "teachers": [{"teacherName": "MORO ITA"}]},
    {"id": 10, "description": "SCIENZE MOTORIE SUP", "teachers": [{"teacherName": "FERRI NICO"}]},
    {"id": 11, "description": "EDUCAZIONE CIVICA SUP", "teachers": [{"teacherName": n} for n in
        ("ROSSI ANNA", "BIANCHI LUCA", "VERDI PAOLO", "NERI SARA", "GIALLI EVA", "BRUNI ELIA", "MORO ITA", "FERRI NICO")]},
    {"id": 12, "description": "STUDIO ASSISTITO", "teachers": [{"teacherName": "STUDIO GUIDATO"}]},
]
SUBS = S.build_subjects(RAW)

def R(author, text, api=None):
    return S.resolve_subject(author, text, SUBS, api)

class T(unittest.TestCase):
    def ok(self, r, subject, crit, conf):
        self.assertEqual(r["subject"], subject, r); self.assertEqual(r["subject_status"], "determinata")
        self.assertIn(crit, r["subject_criterion"]); self.assertEqual(r["subject_confidence"], conf)
    def unk(self, r):
        self.assertIsNone(r["subject"], r); self.assertEqual(r["subject_label"], "Materia da verificare")
        self.assertEqual(r["subject_status"], "da_verificare"); self.assertTrue(r["subject_criterion"])

    def test_api_subject_wins(self):
        self.ok(R("VERDI PAOLO", "boh", api="STORIA"), "STORIA", "api:subjectDesc", "alta")
    def test_two_subject_teacher_explicit_name(self):
        self.ok(R("VERDI PAOLO", "Verifica di filosofia: Kant e illuminismo"), "FILOSOFIA", "testo:nome-materia", "alta")
        self.ok(R("VERDI PAOLO", "Verifica di storia: Luigi XIV, Colbert e Riforma"), "STORIA", "testo:nome-materia", "alta")
    def test_two_subject_teacher_no_hint_is_unknown(self):
        r = R("VERDI PAOLO", "Verifica"); self.unk(r); self.assertEqual(len(r["subject_candidates"]), 2)
        self.unk(R("NERI SARA", "Verifica")); self.unk(R("ROSSI ANNA", "Verifica"))
    def test_topic_only_is_medium_confidence(self):
        self.ok(R("VERDI PAOLO", "Verifica su Aristotele"), "FILOSOFIA", "testo:argomento", "media")
        self.ok(R("ROSSI ANNA", "Verifica su Ariosto"), "LINGUA E LETTERATURA ITALIANA", "testo:argomento", "media")
        self.ok(R("ROSSI ANNA", "Tema in classe"), "LINGUA E LETTERATURA ITALIANA", "testo:argomento", "media")
    def test_both_subjects_cited_is_unknown(self):
        self.unk(R("VERDI PAOLO", "Verifica di storia della filosofia"))
        self.unk(R("ROSSI ANNA", "Verifica su Ariosto e versione dal latino"))
        self.unk(R("ROSSI ANNA", "Verifica su Ariosto e traduzione"))   # argomenti di due materie
    def test_math_physics(self):
        self.ok(R("NERI SARA", "Verifica Matematica"), "MATEMATICA", "testo:nome-materia", "alta")
        self.ok(R("NERI SARA", "Verifica di fisica"), "FISICA", "testo:nome-materia", "alta")
        self.ok(R("NERI SARA", "Verifica su equazioni di secondo grado"), "MATEMATICA", "testo:argomento", "media")
    def test_single_subject_teacher_unique_rule(self):
        self.ok(R("BIANCHI LUCA", "Reading and vocabulary test"), "LINGUA E CULTURA STRANIERA 1", "docente-unico", "media")
        S.ALLOW_UNIQUE_TEACHER = False
        try: self.unk(R("BIANCHI LUCA", "Reading and vocabulary test"))
        finally: S.ALLOW_UNIQUE_TEACHER = True
    def test_text_conflicts_with_teacher(self):
        r = R("MORO ITA", "Verifica di storia"); self.unk(r); self.assertIn("conflitto", r["subject_criterion"])
        self.ok(R("MORO ITA", "Verifica di storia dell'arte: Alberti"), "DISEGNO E STORIA DELL'ARTE", "testo:nome-materia", "alta")
    def test_two_teachers_one_subject(self):
        self.ok(R("BRUNI ELIA", "VERIFICA DI SCIENZE: stechiometria"), "SCIENZE NATURALI (BIOLOGIA, CHIMICA, SCIENZE DELLA TERRA)", "testo:nome-materia", "alta")
        self.ok(R("GIALLI EVA", "Verifica di chimica"), "SCIENZE NATURALI (BIOLOGIA, CHIMICA, SCIENZE DELLA TERRA)", "testo:nome-materia", "alta")
    def test_civics_only_if_cited(self):
        self.ok(R("NERI SARA", "Verifica di educazione civica"), "EDUCAZIONE CIVICA SUP", "testo:nome-materia", "alta")
        r = R("NERI SARA", "Verifica"); self.assertNotIn("CIVICA", str(r["subject"]))
    def test_educazione_fisica_is_not_fisica(self):
        self.ok(R("FERRI NICO", "Test di educazione fisica"), "SCIENZE MOTORIE SUP", "testo:nome-materia", "alta")
        r = R("NERI SARA", "Test di educazione fisica"); self.unk(r)   # il docente di fisica non insegna motorie
    def test_unknown_teacher(self):
        self.ok(R("SUPPLENTE X", "Verifica di fisica"), "FISICA", "docente non presente", "media")
        self.unk(R("SUPPLENTE X", "Verifica"))
    def test_filter_never_matches_by_guess(self):
        a = {"subject": "FISICA", "subject_candidates": ["FISICA"]}
        b = {"subject": None, "subject_candidates": ["MATEMATICA", "FISICA"]}
        c = {"subject": None, "subject_candidates": ["STORIA", "FILOSOFIA"]}
        ok, maybe = S.subject_filter([a, b, c], "fisica")
        self.assertEqual(ok, [a]); self.assertEqual(maybe, [b])
        ok, maybe = S.subject_filter([{"subject": "LINGUA E CULTURA STRANIERA 1", "subject_candidates": []}], "inglese")
        self.assertEqual(len(ok), 1)

    def test_filter_aliases(self):
        def one(name, wanted):
            ok, _ = S.subject_filter([{"subject": name, "subject_candidates": []}], wanted)
            return len(ok) == 1
        self.assertTrue(one("LINGUA E CULTURA LATINA", "latino"))
        self.assertTrue(one("LINGUA E LETTERATURA ITALIANA", "italiano"))
        self.assertTrue(one("STORIA", "storia"))
        self.assertFalse(one("DISEGNO E STORIA DELL'ARTE", "storia"))
        self.assertTrue(one("DISEGNO E STORIA DELL'ARTE", "arte"))
        self.assertFalse(one("SCIENZE MOTORIE SUP", "fisica"))
        self.assertFalse(one("LINGUA E CULTURA LATINA", "italiano"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
