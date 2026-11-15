"""M2: attribuzione materia con evidenza. Struttura di /subjects e delle lezioni come nei dati reali; nomi di fantasia."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cvv_attribution as A
import cvv_client as cv
import cvv_registry as CR
import cvv_subject as S
import registry as R

T = lambda *n: [{"teacherName": x} for x in n]
RAW_SUBJECTS = [
    {"id": 1, "description": "LINGUA E LETTERATURA ITALIANA", "teachers": T("BIANCHI ANNA")},
    {"id": 2, "description": "LINGUA E CULTURA LATINA", "teachers": T("BIANCHI ANNA")},
    {"id": 3, "description": "STORIA", "teachers": T("VERDI LUCA")},
    {"id": 4, "description": "FILOSOFIA", "teachers": T("VERDI LUCA")},
    {"id": 5, "description": "MATEMATICA", "teachers": T("NERI SARA")},
    {"id": 6, "description": "FISICA", "teachers": T("NERI SARA")},
    {"id": 7, "description": "LINGUA E CULTURA STRANIERA 1", "teachers": T("GRIGI EMMA")},
    {"id": 8, "description": "SCIENZE NATURALI (BIOLOGIA, CHIMICA, SCIENZE DELLA TERRA)", "teachers": T("ROSSI PAOLA", "ROSSI GIO")},
    {"id": 9, "description": "EDUCAZIONE CIVICA SUP", "teachers": T("BIANCHI ANNA", "VERDI LUCA", "NERI SARA", "GRIGI EMMA", "ROSSI PAOLA", "ROSSI GIO")},
]
SUBJ = S.build_subjects(RAW_SUBJECTS)
ITA, LAT, STO, FIL = (s["desc"] for s in SUBJ[:4])
MAT, FIS, ING, SCI, CIV = SUBJ[4]["desc"], SUBJ[5]["desc"], SUBJ[6]["desc"], SUBJ[7]["desc"], SUBJ[8]["desc"]


def L(eid, date, ora, author, subject, arg="", tipo="Lezione"):
    return {"evtId": eid, "evtDate": date, "evtCode": "LSF0", "evtHPos": ora, "evtDuration": 1, "classDesc": "4A",
            "authorName": author, "subjectId": 1, "subjectCode": "", "subjectDesc": subject, "lessonType": tipo, "lessonArg": arg}


def E(eid, date, hour, author, text):
    return {"evtId": eid, "evtCode": "AGNT", "subjectDesc": None, "evtDatetimeBegin": "%sT%s:00:00+02:00" % (date, hour),
            "evtDatetimeEnd": "%sT%s:55:00+02:00" % (date, hour), "isFullDay": False, "notes": text, "authorName": author, "classDesc": "4^Z"}


def lesson(raw):
    return A.attribute_lesson(raw, SUBJ)


class LezioniDueMaterie(unittest.TestCase):
    def test_each_two_subject_teacher_is_determined_by_api_subject(self):
        for au, sub, arg in (("BIANCHI ANNA", ITA, "Ariosto"), ("BIANCHI ANNA", LAT, "Catullo: carme 5"),
                             ("VERDI LUCA", STO, "La Controriforma"), ("VERDI LUCA", FIL, "Stoicismo"),
                             ("NERI SARA", MAT, "Funzione irrazionale"), ("NERI SARA", FIS, "Statica dei fluidi")):
            a = lesson(L(1, "2026-10-20", 2, au, sub, arg))
            self.assertEqual((a["stato"], a["materia_derivata"], a["materia_api"], a["confidenza"]), ("determinata", sub, sub, "alta"), (au, sub))
            self.assertEqual(a["conflitti"], [], (au, sub))
            self.assertEqual(a["criterio"], "api:subjectDesc ufficiale")
            self.assertTrue(any(e["regola"] == "L2" for e in a["evidenze"]))   # coppia docente-materia in /subjects

    def test_empty_topic_no_conflict(self):
        a = lesson(L(1, "2026-10-20", 2, "NERI SARA", FIS, ""))
        self.assertEqual((a["stato"], a["conflitti"]), ("determinata", []))


class Conflitti(unittest.TestCase):
    def test_math_lesson_about_gravitation_is_conflict_not_override(self):
        a = lesson(L(1, "2026-10-26", 4, "NERI SARA", MAT, "Problemi sulla gravitazione e l'energia potenziale."))
        self.assertEqual((a["stato"], a["materia_derivata"], a["materia_api"]), ("determinata", MAT, MAT))
        self.assertEqual(len(a["conflitti"]), 1)
        self.assertEqual((a["conflitti"][0]["materia_api"], a["conflitti"][0]["materia_testo"]), ("matematica", "fisica"))

    def test_philosophy_lesson_with_history_topic_is_conflict(self):
        a = lesson(L(1, "2026-10-20", 2, "VERDI LUCA", FIL, "Luigi XIV e la Riforma"))
        self.assertEqual((a["stato"], a["materia_derivata"]), ("determinata", FIL))
        self.assertEqual(a["conflitti"][0]["materia_testo"], "storia")

    def test_both_subjects_in_text_is_not_a_conflict(self):
        a = lesson(L(1, "2026-10-20", 2, "NERI SARA", MAT, "Equazioni e pressione dei fluidi"))
        self.assertEqual(a["conflitti"], [])

    def test_civics_is_never_checked_against_topics(self):
        a = lesson(L(1, "2026-10-20", 2, "NERI SARA", CIV, "Dibattito su calore e temperatura del pianeta"))
        self.assertEqual((a["stato"], a["materia_derivata"], a["conflitti"]), ("determinata", CIV, []))


class EducazioneCivica(unittest.TestCase):
    def test_civics_by_any_teacher_is_determined(self):
        for au in ("BIANCHI ANNA", "NERI SARA", "VERDI LUCA"):
            a = lesson(L(1, "2026-10-21", 1, au, CIV, "Percorso Orientamento", "Orientamento"))
            self.assertEqual((a["stato"], a["materia_derivata"]), ("determinata", CIV), au)

    def test_civics_does_not_enter_teacher_candidates(self):
        a = lesson(L(1, "2026-10-21", 1, "NERI SARA", None))
        self.assertEqual(a["candidati"], sorted([MAT, FIS]))


class DaVerificare(unittest.TestCase):
    def test_substitution_not_official_subject(self):
        a = lesson(L(1, "2026-10-16", 2, "ALTRI MARIO", "SUPPLENZA", "Studio autonomo"))
        self.assertEqual((a["stato"], a["materia_derivata"], a["materia_api"]), ("da_verificare", None, "SUPPLENZA"))
        self.assertIn("non e' una materia ufficiale", a["criterio"])

    def test_subject_missing(self):
        a = lesson(L(1, "2026-10-16", 2, "VERDI LUCA", None))
        self.assertEqual((a["stato"], a["materia_derivata"], a["materia_api"], a["criterio"]), ("da_verificare", None, None, "subjectDesc mancante"))
        self.assertEqual(sorted(a["candidati"]), sorted([STO, FIL]))                                   # candidati, NON una scelta

    def test_subject_missing_empty_string_same(self):
        self.assertEqual(lesson(L(1, "2026-10-16", 2, "VERDI LUCA", "  "))["stato"], "da_verificare")

    def test_unknown_teacher_with_official_subject(self):
        a = lesson(L(1, "2026-10-16", 2, "SCONOSCIUTO ZZ", MAT, "Equazioni"))
        self.assertEqual((a["stato"], a["materia_derivata"], a["materia_api"]), ("da_verificare", None, MAT))
        self.assertEqual(a["criterio"], "docente non presente in /subjects")

    def test_known_teacher_but_wrong_subject_pair(self):
        a = lesson(L(1, "2026-10-16", 2, "GRIGI EMMA", MAT, "Equazioni"))
        self.assertEqual((a["stato"], a["materia_derivata"]), ("da_verificare", None))
        self.assertIn("non risulta tra i docenti", a["criterio"])

    def test_substitution_lesson_type_is_warning_only(self):
        a = lesson(L(1, "2026-10-16", 1, "ROSSI PAOLA", SCI, "correzione compiti", "Supplenza"))
        self.assertEqual((a["stato"], a["materia_derivata"]), ("determinata", SCI))
        self.assertTrue(a["avvisi"])

    def test_two_teachers_one_subject(self):
        for au in ("ROSSI PAOLA", "ROSSI GIO"):
            self.assertEqual(lesson(L(1, "2026-10-16", 1, au, SCI, "minerali"))["stato"], "determinata")


class Separazione(unittest.TestCase):
    def test_original_subject_is_untouched_and_normalized_is_additive(self):
        raw = L(1, "2026-10-26", 4, "NERI SARA", MAT, "gravitazione")
        before = cv.normalize_lesson(raw)
        wa = A.with_attribution(before, lesson(raw))
        self.assertEqual(wa["raw"]["subjectDesc"], MAT)
        for k in ("uid", "hash", "interp_version", "raw", "date"):
            self.assertEqual(wa[k], before[k], k)
        self.assertIn("attribuzione", wa["interpreted"])
        self.assertNotIn("attribuzione", before["interpreted"])                              # l'originale non e' stato alterato

    def test_lesson_identity_and_hash_do_not_depend_on_attribution(self):
        raw = L(1, "2026-10-26", 4, "NERI SARA", MAT, "x")
        n1 = cv.normalize_lesson(raw); lesson(raw); n2 = cv.normalize_lesson(raw)
        self.assertEqual((n1["uid"], n1["hash"], n1["interp_version"]), (n2["uid"], n2["hash"], n2["interp_version"]))
        self.assertEqual(n1["uid"], "cvv:lesson:1")

    def test_attr_version_is_separate_and_tracks_tables(self):
        v = A.attr_version()
        self.assertTrue(v.startswith("r%d-" % A.ATTR_RULES_REV))
        self.assertNotEqual(v, cv.lesson_interp_version())
        old = S.ALLOW_UNIQUE_TEACHER
        try:
            S.ALLOW_UNIQUE_TEACHER = not old
            self.assertNotEqual(A.attr_version(), v)
        finally:
            S.ALLOW_UNIQUE_TEACHER = old


def attr_ev(raw, lessons=()):
    return A.attribute_event(cv.normalize_event(raw), SUBJ, A.lessons_index(list(lessons)))


class Eventi(unittest.TestCase):
    def test_explicit_name_with_concordant_lesson(self):
        a = attr_ev(E(10, "2026-10-20", "09", "VERDI LUCA", "Verifica di filosofia: Aristotele"),
                    [L(1, "2026-10-20", 3, "VERDI LUCA", FIL, "Verifica")])
        self.assertEqual((a["stato"], a["materia_derivata"], a["materia_api"]), ("determinata", FIL, None))
        self.assertEqual(a["criterio"], "testo:nome-materia")
        self.assertEqual([e["esito"] for e in a["evidenze"] if e["regola"] == "A6"], ["concorde"])
        self.assertEqual(a["conflitti"], [])

    def test_discordant_lesson_is_conflict_without_changing_subject(self):
        a = attr_ev(E(10, "2026-10-20", "09", "VERDI LUCA", "Verifica di filosofia"),
                    [L(1, "2026-10-20", 3, "VERDI LUCA", STO, "Riforma")])
        self.assertEqual(a["materia_derivata"], FIL)
        self.assertEqual(a["conflitti"][0]["tipo"], "evento_vs_lezioni")
        self.assertEqual(a["conflitti"][0]["materie_lezioni"], [STO])

    def test_lesson_alone_never_assigns_a_subject(self):
        a = attr_ev(E(10, "2026-10-20", "09", "VERDI LUCA", "Verifica"), [L(1, "2026-10-20", 3, "VERDI LUCA", FIL, "x")])
        self.assertEqual((a["stato"], a["materia_derivata"]), ("da_verificare", None))
        self.assertEqual(sorted(a["candidati"]), sorted([STO, FIL]))
        self.assertTrue(any(e["regola"] == "A6" and "non usata" in e["esito"] for e in a["evidenze"]))

    def test_no_lesson_that_day(self):
        a = attr_ev(E(10, "2026-11-04", "10", "NERI SARA", "Verifica di fisica"))
        self.assertEqual(a["materia_derivata"], FIS)
        self.assertEqual([e["esito"] for e in a["evidenze"] if e["regola"] == "A6"], ["nessuna corroborazione"])

    def test_math_physics_teacher_ambiguous_text(self):
        a = attr_ev(E(10, "2026-11-04", "10", "NERI SARA", "Verifica"))
        self.assertEqual((a["stato"], a["materia_derivata"]), ("da_verificare", None))
        self.assertEqual(sorted(a["candidati"]), sorted([MAT, FIS]))

    def test_math_and_physics_both_cited_is_unknown(self):
        a = attr_ev(E(10, "2026-11-04", "10", "NERI SARA", "Verifica di matematica e fisica"))
        self.assertEqual(a["materia_derivata"], None)

    def test_italian_latin_topic_medium_confidence(self):
        a = attr_ev(E(10, "2026-11-09", "10", "BIANCHI ANNA", "Verifica su Ariosto"))
        self.assertEqual((a["materia_derivata"], a["confidenza"]), (ITA, "media"))
        a = attr_ev(E(11, "2026-11-09", "10", "BIANCHI ANNA", "Verifica di scritto"))   # niente argomento ne' nome materia
        self.assertEqual(a["materia_derivata"], None)

    def test_unique_teacher_medium_and_unknown_teacher(self):
        a = attr_ev(E(10, "2026-10-29", "11", "GRIGI EMMA", "Interrogazioni programmate"))
        self.assertEqual((a["materia_derivata"], a["confidenza"], a["criterio"].split(":")[0]), (ING, "media", "docente-unico"))
        a = attr_ev(E(11, "2026-10-29", "11", "SCONOSCIUTO ZZ", "Verifica"))
        self.assertEqual((a["stato"], a["materia_derivata"]), ("da_verificare", None))
        self.assertTrue(a["evidenze"][0]["esito"].endswith("NO"))

    def test_civics_only_if_cited(self):
        self.assertEqual(attr_ev(E(10, "2026-10-29", "11", "NERI SARA", "Verifica di educazione civica"))["materia_derivata"], CIV)

    def test_text_contradicting_teacher_is_conflict(self):
        a = attr_ev(E(10, "2026-10-29", "11", "GRIGI EMMA", "Verifica di matematica"))
        self.assertEqual((a["stato"], a["materia_derivata"]), ("da_verificare", None))
        self.assertEqual(a["conflitti"][0]["tipo"], "testo_vs_docente")

    def test_event_resolution_matches_existing_resolver(self):
        for raw in (E(1, "2026-10-20", "09", "VERDI LUCA", "Verifica di storia"), E(2, "2026-11-04", "10", "NERI SARA", "Verifica"),
                    E(3, "2026-11-09", "10", "BIANCHI ANNA", "Tema in classe")):
            n = cv.normalize_event(raw)
            r = S.resolve_subject(n["author"], n["text"], SUBJ, n["subject_api"])
            a = A.attribute_event(n, SUBJ)
            self.assertEqual((a["materia_derivata"], a["criterio"], a["confidenza"]), (r["subject"], r["subject_criterion"], r["subject_confidence"]))


class Report(unittest.TestCase):
    def test_report_counts_and_no_registry_change(self):
        lessons = [L(1, "2026-10-26", 4, "NERI SARA", MAT, "gravitazione"), L(2, "2026-10-16", 2, "ALTRI MARIO", "SUPPLENZA"),
                   L(3, "2026-10-20", 2, "VERDI LUCA", FIL, "Stoicismo")]
        events = [E(10, "2026-10-20", "09", "VERDI LUCA", "Verifica di filosofia")]
        rep = A.build_report(lessons, events, SUBJ)
        self.assertEqual(rep["lesson"]["totale"], 3)
        self.assertEqual(rep["lesson"]["per_stato"], {"determinata": 2, "da_verificare": 1})
        self.assertEqual(len(rep["lesson"]["conflitti"]), 1)
        self.assertEqual(rep["lesson"]["da_verificare"][0]["materia_api"], "SUPPLENZA")
        self.assertEqual(rep["agenda_event"]["per_stato"], {"determinata": 1})

    def test_attribution_never_writes_registry(self):
        reg = R.Registry(":memory:")
        CR.sync_kind(reg, "lesson", lambda: [L(1, "2026-10-26", 4, "NERI SARA", MAT, "gravitazione")])
        v = reg.get_versions(CR.SOURCE, "lesson", "lesson:1")
        raw = reg.get_record(CR.SOURCE, "lesson", "lesson:1")["raw"]
        A.build_report([raw], [], SUBJ)
        self.assertEqual(reg.get_versions(CR.SOURCE, "lesson", "lesson:1"), v)
        self.assertEqual(reg.list_records(kind="lesson")[0]["current_version"], 1)


class Dettaglio(unittest.TestCase):
    LES = [L(101, "2026-10-20", 3, "VERDI LUCA", FIL, "Verifica"), L(102, "2026-10-20", 5, "VERDI LUCA", STO, "Riforma"),
           L(103, "2026-10-16", 2, "ALTRI MARIO", "SUPPLENZA", "Studio autonomo"),
           L(104, "2026-10-26", 4, "NERI SARA", MAT, "gravitazione universale"),
           L(105, "2026-10-16", 1, "ROSSI PAOLA", SCI, "correzione", "Supplenza"), L(106, "2026-10-20", 6, "GRIGI EMMA", ING, "x")]
    EVS = [E(10, "2026-10-20", "09", "VERDI LUCA", "Verifica di filosofia"),            # concorde
           E(11, "2026-10-20", "10", "VERDI LUCA", "Verifica di storia"),               # concorde (altra lezione dello stesso giorno)
           E(12, "2026-10-20", "11", "GRIGI EMMA", "Verifica di matematica"),            # testo contro docente: da_verificare, lezione informativa
           E(13, "2026-11-04", "10", "NERI SARA", "Verifica di fisica"),                 # nessuna lezione quel giorno
           E(14, "2026-10-26", "10", "NERI SARA", "Verifica di fisica"),                 # DISCORDE: quel giorno solo lezione di matematica
           E(15, "2026-10-20", "12", "VERDI LUCA", "Verifica")]                          # da_verificare: la lezione e' solo informativa

    def rep(self, d=True):
        return A.build_report([dict(x) for x in self.LES], [dict(x) for x in self.EVS], SUBJ, dettaglio=d)

    def a6(self, rep, evid):
        rec = [r for r in rep["agenda_event"]["dettaglio_record"] if r["uid"] == "cvv:%s" % evid][0]
        return rec, [e for e in rec["evidenze"] if e["regola"] == "A6"]

    def test_default_has_no_detail(self):
        r = self.rep(False)
        self.assertFalse(r["dettaglio"])
        self.assertNotIn("dettaglio_record", r["agenda_event"])
        self.assertNotIn("dettaglio_record_rilevanti", r["lesson"])

    def test_detail_does_not_change_decisions(self):
        a, b = self.rep(False), self.rep(True)
        for k in ("lesson", "agenda_event"):
            for f in ("totale", "per_stato", "per_criterio", "per_confidenza", "da_verificare", "conflitti", "avvisi"):
                self.assertEqual(a[k][f], b[k][f], (k, f))

    def test_a6_concordant_names_the_lesson_used(self):
        rec, a6 = self.a6(self.rep(), 10)
        self.assertEqual(len(a6), 1)
        e = a6[0]
        self.assertEqual((e["esito_codice"], e["usata_per_assegnare"], e["materie_lezioni"]), ("concorde", False, sorted([FIL, STO])))
        self.assertEqual([(x["uid"], x["ora"], x["materia"]) for x in e["lezioni_usate"] if x["materia"] == FIL],
                         [("cvv:lesson:101", 3, FIL)])
        self.assertEqual(rec["materia_derivata"], FIL)

    def test_a6_same_day_lessons_of_two_subjects_still_concordant(self):
        _, a6 = self.a6(self.rep(), 11)
        self.assertEqual(a6[0]["esito_codice"], "concorde")
        self.assertEqual(a6[0]["materie_lezioni"], sorted([FIL, STO]))

    def test_a6_discordant_is_conflict_and_does_not_change_subject(self):
        rec, a6 = self.a6(self.rep(), 14)
        self.assertEqual((a6[0]["esito_codice"], a6[0]["materie_lezioni"]), ("discordante", [MAT]))
        self.assertEqual(rec["materia_derivata"], FIS)
        self.assertEqual(rec["conflitti"][0]["tipo"], "evento_vs_lezioni")
        self.assertEqual(a6[0]["lezioni_usate"][0]["uid"], "cvv:lesson:104")

    def test_a6_absent(self):
        rec, a6 = self.a6(self.rep(), 13)
        self.assertEqual((a6[0]["esito_codice"], a6[0]["lezioni_usate"], a6[0]["usata_per_assegnare"]), ("assente", [], False))
        self.assertEqual(rec["materia_derivata"], FIS)

    def test_a6_informative_never_assigns(self):
        rec, a6 = self.a6(self.rep(), 15)
        self.assertEqual(a6[0]["esito_codice"], "informativa")
        self.assertEqual((rec["stato"], rec["materia_derivata"]), ("da_verificare", None))
        self.assertFalse(a6[0]["usata_per_assegnare"])
        rec, a6 = self.a6(self.rep(), 12)                          # lezione di inglese del docente, testo matematica
        self.assertEqual((rec["materia_derivata"], a6[0]["esito_codice"]), (None, "informativa"))

    def test_a6_never_used_for_assignment_anywhere(self):
        r = self.rep()
        for rec in r["agenda_event"]["dettaglio_record"]:
            for e in rec["evidenze"]:
                if e["regola"] == "A6":
                    self.assertFalse(e["usata_per_assegnare"])
        self.assertEqual(sum(r["agenda_event"]["evidenza_A6_riepilogo"].values()), len(self.EVS))
        self.assertEqual(r["agenda_event"]["evidenza_A6_riepilogo"],
                         {"concorde": 2, "informativa": 2, "assente": 1, "discordante": 1})

    def test_lesson_detail_only_for_relevant_records_with_evidence(self):
        l = self.rep()["lesson"]
        uids = {x["uid"] for x in l["dettaglio_record_rilevanti"]}
        self.assertEqual(uids, {"cvv:lesson:103", "cvv:lesson:104", "cvv:lesson:105"})
        self.assertEqual(l["record_senza_rilievi"], len(self.LES) - 3)
        for x in l["dettaglio_record_rilevanti"]:
            self.assertTrue(x["evidenze"])
            self.assertTrue(all({"regola", "fonte", "testo"} <= set(e) for e in x["evidenze"]))
        self.assertTrue(any(c["n"] and c["docente"] == "NERI SARA" for c in l["per_docente_materia"]))

    def test_detail_does_not_touch_registry(self):
        reg = R.Registry(":memory:")
        CR.sync_kind(reg, "lesson", lambda: [dict(x) for x in self.LES])
        raws = [reg.get_record(CR.SOURCE, "lesson", r["source_uid"])["raw"] for r in reg.list_records(kind="lesson")]
        before = [(r["source_uid"], r["current_version"], r["content_hash"]) for r in reg.list_records(kind="lesson")]
        A.build_report(raws, [], SUBJ, dettaglio=True)
        self.assertEqual(before, [(r["source_uid"], r["current_version"], r["content_hash"]) for r in reg.list_records(kind="lesson")])
        self.assertEqual(reg.verify_integrity(), [])


if __name__ == "__main__":
    unittest.main()
