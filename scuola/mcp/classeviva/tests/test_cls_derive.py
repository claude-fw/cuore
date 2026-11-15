"""L3 Classroom (cls_derive): work_view pura, deterministica, read-only. Solo dati SINTETICI."""
import copy
import hashlib
import inspect
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import cls_derive as D      # noqa: E402
import cls_registry as CL   # noqa: E402
import registry as R        # noqa: E402

NOW = "2026-10-03T21:00:00+02:00"          # sabato sera, ora legale (CEST)


def course(cid="C1", name="Matematica 4S", state="ACTIVE", created="2026-09-01T08:00:00Z"):
    c = {"id": cid, "name": name, "courseState": state}
    if created is not None:
        c["creationTime"] = created
    return c


def work(wid="W1", cid="C1", due=(2026, 10, 10), time=(21, 59), state="PUBLISHED", title="Esercizi (SINT.)",
         created="2026-09-20T08:00:00Z"):
    w = {"courseId": cid, "id": wid, "title": title, "state": state, "workType": "ASSIGNMENT",
         "updateTime": "2026-09-20T08:00:00Z"}
    if created is not None:
        w["creationTime"] = created
    if due:
        w["dueDate"] = {"year": due[0], "month": due[1], "day": due[2]}
        if time is not None:
            w["dueTime"] = {"hours": time[0], "minutes": time[1]} if time else {}
    return w


def sub(sid="S1", cid="C1", wid="W1", state="CREATED", late=False, grade=None):
    s = {"courseId": cid, "courseWorkId": wid, "id": sid, "userId": "me", "state": state, "late": late,
         "updateTime": "2026-09-21T08:00:00Z"}
    if grade is not None:
        s["assignedGrade"] = grade
    return s


def views(courses=None, works=None, subs=None, now=NOW, manual=None):
    return D.build_work_views([course()] if courses is None else courses, [work()] if works is None else works,
                              [sub()] if subs is None else subs, now, manual=manual)


def one(**kw):
    return views(**kw)["work_views"][0]


class Join(unittest.TestCase):
    def test_single_submission_joined(self):
        v = one()
        self.assertEqual(v["fonte"]["consegna"]["numero_trovate"], 1)
        self.assertEqual(v["fonte"]["consegna"]["uid"], "cls:C1/S1")
        self.assertEqual(v["provenienza"]["submission"]["uid"], "cls:C1/S1")

    def test_no_submission_is_da_verificare(self):
        v = one(subs=[])
        self.assertEqual(v["derivato"]["stato_operativo"], "da_verificare")
        self.assertFalse(v["fonte"]["consegna"]["presente"])
        self.assertIn("nessuna consegna", " ".join(v["da_verificare"]))

    def test_multiple_submissions_reported_not_resolved(self):
        v = one(subs=[sub("S2", state="TURNED_IN"), sub("S1")])
        c = v["fonte"]["consegna"]
        self.assertEqual((c["numero_trovate"], c["presente"], c["uid"], c["stato_fonte"]), (2, False, None, None))
        self.assertEqual(c["candidati"], ["cls:C1/S1", "cls:C1/S2"])
        self.assertEqual(v["derivato"]["stato_operativo"], "da_verificare")
        self.assertIn("consegne multiple", v["derivato"]["motivo_stato"])
        self.assertIsNone(v["provenienza"]["submission"])

    def test_orphan_submission_reported(self):
        r = views(subs=[sub(), sub("S9", wid="W404")])
        self.assertEqual([o["uid"] for o in r["consegne_orfane"]], ["cls:C1/S9"])
        self.assertEqual(r["conteggi"]["consegne_orfane"], 1)

    def test_same_work_id_other_course_not_joined(self):
        r = views(courses=[course(), course("C2", "Fisica")], subs=[sub(cid="C2")])
        v = r["work_views"][0]
        self.assertEqual(v["fonte"]["consegna"]["numero_trovate"], 0)
        self.assertEqual([o["uid"] for o in r["consegne_orfane"]], ["cls:C2/S1"])

    def test_late_is_api_value_not_recomputed(self):
        v = one(works=[work(due=(2026, 9, 1))], subs=[sub(state="TURNED_IN", late=False)])
        self.assertIs(v["fonte"]["consegna"]["late"], False)          # scadenza passata ma l'API dice late=False
        v = one(subs=[sub(late=True)])                                # scadenza futura ma l'API dice late=True
        self.assertIs(v["fonte"]["consegna"]["late"], True)

    def test_points_are_not_a_grade(self):
        c = one(subs=[sub(state="RETURNED", grade=7)])["fonte"]["consegna"]
        self.assertEqual(c["assigned_points"], 7)
        self.assertIn("NON un voto", c["nota_punti"])


class SubmissionStates(unittest.TestCase):
    def state(self, st, due=(2026, 10, 10), time=(21, 59)):
        v = one(works=[work(due=due, time=time)], subs=[sub(state=st)])
        return v["derivato"]["stato_operativo"], v

    def test_turned_in_and_returned(self):
        self.assertEqual(self.state("TURNED_IN")[0], "consegnato")
        self.assertEqual(self.state("RETURNED")[0], "restituito")
        self.assertEqual(self.state("TURNED_IN", due=(2026, 9, 1))[0], "consegnato")

    def test_edited_after_turn_in(self):
        st, v = self.state("STUDENT_EDITED_AFTER_TURN_IN")
        self.assertEqual(st, "consegnato")
        self.assertIn("modificata dopo", " ".join(v["derivato"]["note_stato"]))

    def test_created_new_reclaimed_future_past_none(self):
        for s in ("NEW", "CREATED", "RECLAIMED_BY_STUDENT"):
            self.assertEqual(self.state(s)[0], "aperto", s)
            self.assertEqual(self.state(s, due=(2026, 9, 1))[0], "scaduto_non_consegnato", s)
            self.assertEqual(self.state(s, due=None)[0], "senza_scadenza", s)
        self.assertIn("ritirata", " ".join(self.state("RECLAIMED_BY_STUDENT")[1]["derivato"]["note_stato"]))

    def test_semantic_notes_no_todo_meaning(self):
        _, v = self.state("CREATED", due=None)
        self.assertIn("NON significa 'da fare'", " ".join(v["derivato"]["note_stato"]))
        _, v = self.state("CREATED", due=(2026, 9, 1))
        self.assertIn("classificazione tecnica", " ".join(v["derivato"]["note_stato"]))

    def test_unknown_or_missing_state(self):
        for s in ("SUBMISSION_STATE_UNSPECIFIED", None, "QUALCOSA"):
            st, v = self.state(s)
            self.assertEqual(st, "da_verificare", s)
            self.assertTrue(v["da_verificare"])


class WorkStates(unittest.TestCase):
    def test_draft_deleted_published(self):
        self.assertEqual(one(works=[work(state="DRAFT")])["derivato"]["stato_operativo"], "bozza")
        self.assertEqual(one(works=[work(state="DELETED")])["derivato"]["stato_operativo"], "rimosso")
        self.assertEqual(one(works=[work(state="PUBLISHED")])["derivato"]["stato_operativo"], "aperto")

    def test_unknown_work_state_flagged(self):
        v = one(works=[work(state="ALTRO")])
        self.assertTrue(any("ALTRO" in x or "state" in x for x in v["da_verificare"]))


class Deadlines(unittest.TestCase):
    def due(self, due=(2026, 10, 10), time=(21, 59), now=NOW):
        return one(works=[work(due=due, time=time)], now=now)["derivato"]["scadenza"]

    def test_none(self):
        s = self.due(due=None)
        self.assertEqual((s["presente"], s["precisione"], s["giorni_mancanti"], s["passata"]), (False, None, None, None))

    def test_date_and_time_utc_to_rome_summer(self):
        s = self.due(time=(21, 59))                                  # 21:59Z = 23:59 CEST
        self.assertEqual((s["precisione"], s["momento_locale"], s["data_locale"]), ("data_e_ora", "2026-10-10T23:59", "2026-10-10"))
        self.assertEqual(s["giorni_mancanti"], 7)

    def test_utc_crossing_midnight_changes_local_day(self):
        s = self.due(due=(2026, 10, 9), time=(22, 30))               # 22:30Z = 00:30 CEST del 10
        self.assertEqual((s["momento_locale"], s["data_locale"]), ("2026-10-10T00:30", "2026-10-10"))

    def test_dst_winter(self):
        s = self.due(due=(2026, 12, 10), time=(22, 59))              # inverno: 22:59Z = 23:59 CET
        self.assertEqual(s["momento_locale"], "2026-12-10T23:59")

    def test_dst_change_day(self):
        s = self.due(due=(2026, 10, 25), time=(1, 30))               # 25/10: alle 01:00Z si torna a CET
        self.assertEqual(s["momento_locale"], "2026-10-25T02:30")
        s = self.due(due=(2026, 10, 25), time=(0, 30))               # prima del cambio: CEST
        self.assertEqual(s["momento_locale"], "2026-10-25T02:30")    # stesso orario locale, istanti diversi

    def test_empty_time_means_midnight_utc(self):
        s = self.due(time=())
        self.assertEqual((s["precisione"], s["momento_locale"]), ("data_e_ora", "2026-10-10T02:00"))

    def test_date_only(self):
        s = self.due(time=None)
        self.assertEqual((s["precisione"], s["momento_locale"], s["data_locale"]), ("solo_data", None, "2026-10-10"))
        self.assertIn("fine giornata", s["nota"])

    def test_date_only_today_is_still_open(self):
        v = one(works=[work(due=(2026, 10, 3), time=None)], now=NOW)
        self.assertEqual(v["derivato"]["stato_operativo"], "aperto")
        self.assertEqual(v["derivato"]["scadenza"]["giorni_mancanti"], 0)
        v = one(works=[work(due=(2026, 10, 3), time=None)], now="2026-10-04T00:00:00+02:00")
        self.assertEqual(v["derivato"]["stato_operativo"], "scaduto_non_consegnato")

    def test_exact_boundary(self):
        at = "2026-10-10T21:59:00+00:00"
        self.assertEqual(one(now=at)["derivato"]["stato_operativo"], "scaduto_non_consegnato")   # now == scadenza
        self.assertEqual(one(now="2026-10-10T21:58:59+00:00")["derivato"]["stato_operativo"], "aperto")

    def test_now_must_have_timezone(self):
        for bad in ("2026-10-03T21:00:00", None, 12345):
            with self.assertRaises(ValueError):
                views(now=bad)
        self.assertEqual(views(now="2026-10-03T19:00:00Z")["riferimento_temporale"], "2026-10-03T19:00:00+00:00")


class Courses(unittest.TestCase):
    def test_active_archived_other_unknown(self):
        for st, exp in (("ACTIVE", True), ("ARCHIVED", False), ("PROVISIONED", False)):
            self.assertIs(one(courses=[course(state=st)])["derivato"]["corso_attivo"], exp, st)
        v = one(courses=[course(state="BOH")])
        self.assertIsNone(v["derivato"]["corso_attivo"])
        self.assertIn("stato del corso sconosciuto", " ".join(v["da_verificare"]))

    def test_archived_course_does_not_change_operational_state(self):
        self.assertEqual(one(courses=[course(state="ARCHIVED")])["derivato"]["stato_operativo"], "aperto")

    def test_missing_course(self):
        v = one(courses=[])
        self.assertFalse(v["fonte"]["corso"]["presente"])
        self.assertIsNone(v["derivato"]["corso_attivo"])
        self.assertEqual(v["derivato"]["anno_corso"]["certezza"], "sconosciuta")
        self.assertEqual(v["derivato"]["anno_compito"]["valore"], "2026-27")   # il compito non dipende dal corso
        self.assertIn("non presente nel registro", " ".join(v["da_verificare"]))


class CourseYear(unittest.TestCase):
    def cy(self, name, state="ACTIVE", created="2026-09-01T08:00:00Z"):
        return D.course_year(course(name=name, state=state, created=created))

    def test_real_name_patterns(self):
        for name, exp in (("2 Z Mate & Fisica (prova) 24/25", "2024-25"), ("4Z Mate & Fisica (prova) 2026/27", "2026-27"),
                          ("Laboratorio Teatro (prova) 2025-26", "2025-26"), ("RELIGIONE 2026-2027", "2026-27")):
            y = self.cy(name, created=None)
            self.assertEqual((y["valore"], y["certezza"], y["fonte"]), (exp, "alta", "nome_corso"), name)

    def test_name_wins_creation_is_only_a_candidate(self):
        y = self.cy("RELIGIONE 2026-2027", created="2023-09-10T08:00:00Z")       # corso riusato: caso reale
        self.assertEqual((y["valore"], y["certezza"]), ("2026-27", "alta"))
        self.assertEqual({c["valore"] for c in y["candidati"]}, {"2026-27", "2023-24"})

    def test_creation_only_is_low_certainty(self):
        for st in ("ACTIVE", "ARCHIVED"):
            y = self.cy("Scienze Motorie", state=st)
            self.assertEqual((y["valore"], y["certezza"], y["fonte"]), ("2026-27", "bassa", "creazione_corso"), st)
        self.assertIn("riusato", self.cy("Scienze Motorie")["nota"])

    def test_creation_boundary_local_time(self):
        self.assertEqual(self.cy("X", created="2026-08-31T20:00:00Z")["valore"], "2025-26")   # 22:00 del 31/8
        self.assertEqual(self.cy("X", created="2026-08-31T22:30:00Z")["valore"], "2026-27")   # 00:30 del 1/9

    def test_non_year_numbers_ignored(self):
        for name in ("Aula 2024", "Classe 10/11", "Gruppo 3-4", "Corso 2024/2026", "4^ ZZ - ABC"):
            y = self.cy(name, created=None)
            self.assertEqual((y["valore"], y["certezza"]), (None, "sconosciuta"), name)

    def test_two_different_years_in_name(self):
        v = one(courses=[course(name="Corso 24/25 e 25/26")])
        self.assertEqual(v["derivato"]["anno_corso"]["certezza"], "conflitto")
        self.assertIn("anno del corso in conflitto", " ".join(v["da_verificare"]))

    def test_labelled_as_context_deduction(self):
        self.assertIn("non anno dei compiti", self.cy("RELIGIONE 2026-2027")["nota"])


class WorkYear(unittest.TestCase):
    def wy(self, created="2026-09-20T08:00:00Z", due=(2026, 10, 10), time=(21, 59), cname="Corso 2022/23",
           ccreated="2022-09-01T08:00:00Z"):
        return one(courses=[course(name=cname, created=ccreated)],
                   works=[work(created=created, due=due, time=time)])["derivato"]

    def test_creation_and_due_agree_high(self):
        y = self.wy()["anno_compito"]
        self.assertEqual((y["valore"], y["certezza"], y["fonte"]), ("2026-27", "alta", "creazione_compito+scadenza_compito"))

    def test_course_year_never_used_for_work(self):
        d = self.wy()                                  # corso "2022/23" creato nel 2022, compito del 2026
        self.assertEqual(d["anno_corso"]["valore"], "2022-23")
        self.assertEqual(d["anno_compito"]["valore"], "2026-27")
        self.assertNotIn("corso", json.dumps(d["anno_compito"]["candidati"]))

    def test_single_evidence_is_medium(self):
        y = self.wy(due=None)["anno_compito"]
        self.assertEqual((y["valore"], y["certezza"], y["fonte"]), ("2026-27", "media", "creazione_compito"))
        y = self.wy(created=None)["anno_compito"]
        self.assertEqual((y["valore"], y["certezza"], y["fonte"]), ("2026-27", "media", "scadenza_compito"))

    def test_conflict_summer_homework(self):
        v = one(works=[work(created="2026-06-05T08:00:00Z", due=(2026, 9, 15))])
        y = v["derivato"]["anno_compito"]
        self.assertEqual((y["valore"], y["certezza"]), (None, "conflitto"))
        self.assertEqual({c["valore"] for c in y["candidati"]}, {"2025-26", "2026-27"})
        self.assertIn("anno del compito conflitto", " ".join(v["da_verificare"]))

    def test_no_dates_unknown(self):
        v = one(works=[work(created=None, due=None)])
        self.assertEqual(v["derivato"]["anno_compito"]["certezza"], "sconosciuta")
        self.assertEqual(v["derivato"]["perimetro"]["valore"], "da_verificare")
        self.assertTrue(any("perimetro" in x for x in v["da_verificare"]))

    def test_due_uses_local_date(self):                  # 31/8 22:30Z = 1/9 00:30 a Roma -> nuovo anno
        y = self.wy(created=None, due=(2026, 8, 31), time=(22, 30))["anno_compito"]
        self.assertEqual(y["valore"], "2026-27")

    def test_manual_override_kept_separate(self):
        v = one(manual={"course_school_year": {"C1": "2025-26"}, "work_school_year": {"cls:C1/W1": "2024-25"}})
        self.assertEqual((v["manuale"]["anno_corso"], v["manuale"]["anno_compito"]), ("2025-26", "2024-25"))
        self.assertEqual(v["derivato"]["anno_compito"]["valore"], "2026-27")       # la deduzione non viene riscritta
        self.assertEqual(v["derivato"]["perimetro"]["valore"], "storico")           # ma il perimetro segue la decisione


class Perimeter(unittest.TestCase):
    def per(self, created, due):
        return one(works=[work(created=created, due=due)])["derivato"]["perimetro"]

    def test_classes_declared(self):
        self.assertEqual(D.CLASSI, {"2025-26": "3a classe", "2026-27": "4a classe"})

    def test_operational_and_historical(self):
        p = self.per("2026-09-20T08:00:00Z", (2026, 10, 10))
        self.assertEqual((p["valore"], p["anno"], p["classe"]), ("operativo", "2026-27", "4a classe"))
        p = self.per("2025-11-20T08:00:00Z", (2025, 12, 1))
        self.assertEqual((p["valore"], p["classe"]), ("operativo", "3a classe"))
        p = self.per("2024-11-20T08:00:00Z", (2024, 12, 1))
        self.assertEqual((p["valore"], p["classe"]), ("storico", None))

    def test_conflict_inside_outside_mixed(self):
        self.assertEqual(self.per("2026-06-05T08:00:00Z", (2026, 9, 15))["valore"], "operativo")   # 3a -> 4a
        self.assertEqual(self.per("2023-06-05T08:00:00Z", (2023, 9, 15))["valore"], "storico")     # tutto fuori
        self.assertEqual(self.per("2025-06-05T08:00:00Z", (2025, 9, 15))["valore"], "da_verificare")  # 2a -> 3a

    def test_label_only_nothing_removed(self):
        ws = [work("W1", created="2026-09-20T08:00:00Z"), work("W2", created="2022-10-01T08:00:00Z", due=(2022, 11, 1))]
        r = views(works=ws, subs=[sub(), sub("S2", wid="W2")])
        self.assertEqual(len(r["work_views"]), 2)                                      # lo storico resta
        self.assertEqual(r["conteggi"]["perimetro"], {"operativo": 1, "storico": 1})

    def test_operational_views_non_destructive(self):
        ws = [work("W1"), work("W2", created="2022-10-01T08:00:00Z", due=(2022, 11, 1)),
              work("W3", created=None, due=None)]
        r = views(works=ws, subs=[sub(), sub("S2", wid="W2"), sub("S3", wid="W3")])
        before = json.dumps(r, sort_keys=True)
        o = D.operational_views(r)
        self.assertEqual([v["uid"] for v in o["work_views"]], ["cls:C1/W1"])
        self.assertEqual((o["vista"]["esclusi_storico"], o["vista"]["esclusi_da_verificare"]), (1, 1))
        self.assertEqual(json.dumps(r, sort_keys=True), before)                       # risultato completo intatto
        o2 = D.operational_views(r, include_da_verificare=True)
        self.assertEqual([v["uid"] for v in o2["work_views"]], ["cls:C1/W1", "cls:C1/W3"])

    def test_current_year_outside_perimeter_is_reported(self):
        self.assertTrue(views()["perimetro_operativo"]["anno_corrente_nel_perimetro"])
        r = views(now="2027-10-01T10:00:00+02:00")
        self.assertEqual(r["perimetro_operativo"]["anno_corrente"], "2027-28")
        self.assertFalse(r["perimetro_operativo"]["anno_corrente_nel_perimetro"])


class Subjects(unittest.TestCase):
    def test_no_proposal(self):
        v = one(courses=[course(name="Laboratorio Teatro (prova) - 2026/27")])
        self.assertEqual(v["derivato"]["materia_proposte"], [])
        self.assertEqual(v["manuale"]["materia"]["stato"], "non_assegnata")

    def test_single_proposal_is_never_a_decision(self):
        v = one(courses=[course(name="RELIGIONE 2026-2027")])
        self.assertEqual([p["subject"] for p in v["derivato"]["materia_proposte"]], ["religione"])
        self.assertEqual(v["derivato"]["materia_proposte"][0]["stato"], "proposto")
        m = v["manuale"]["materia"]
        self.assertEqual((m["stato"], m["decisione"], m["confermate"]), ("non_assegnata", None, []))

    def test_multiple_proposals(self):
        v = one(courses=[course(name="ITALIANO, LATINO")])
        self.assertEqual([p["subject"] for p in v["derivato"]["materia_proposte"]], ["italiano", "latino"])
        self.assertIsNone(v["manuale"]["materia"]["decisione"])

    def test_art_course_is_not_history(self):
        v = one(courses=[course(name="4^ZC Storia dell’arte (prova)")])
        self.assertEqual([p["subject"] for p in v["derivato"]["materia_proposte"]], ["arte"])

    def test_confirmed_single(self):
        m = one(manual={"course_subjects": {"C1": [{"subject": "matematica", "ruolo": "primary"}]}})["manuale"]["materia"]
        self.assertEqual((m["stato"], m["decisione"]), ("confermata", "matematica"))
        self.assertEqual(m["confermate"][0]["origine"], "manuale")

    def test_confirmed_multiple_is_ambiguous(self):
        man = {"course_subjects": {"C1": [{"subject": "matematica", "ruolo": "primary"},
                                          {"subject": "fisica", "ruolo": "primary"}]}}
        m = one(manual=man)["manuale"]["materia"]
        self.assertEqual((m["stato"], m["decisione"]), ("ambigua_confermata", None))

    def test_work_override_wins(self):
        man = {"course_subjects": {"C1": [{"subject": "matematica"}, {"subject": "fisica"}]},
               "work_subject": {"cls:C1/W1": "fisica"}}
        m = one(manual=man)["manuale"]["materia"]
        self.assertEqual((m["stato"], m["decisione"]), ("override", "fisica"))

    def test_manual_reference_to_missing_course_is_reported(self):
        r = views(manual={"course_subjects": {"C404": [{"subject": "latino"}]}, "work_subject": {"cls:X/Y": "latino"}})
        self.assertEqual([x["riferimento"] for x in r["manuale_non_risolto"]], ["C404", "cls:X/Y"])


class Properties(unittest.TestCase):
    def data(self):
        return ([course(), course("C2", "ITALIANO, LATINO 2026/27", "ARCHIVED")],
                [work("W2", "C2", due=None), work(), work("W3", time=None)],
                [sub(), sub("S2", "C2", "W2", "TURNED_IN"), sub("S3", wid="W3"), sub("S4", wid="W404")])

    def test_deterministic_and_sorted(self):
        c, w, s = self.data()
        a = json.dumps(D.build_work_views(c, w, s, NOW), sort_keys=True)
        b = json.dumps(D.build_work_views(list(reversed(c)), list(reversed(w)), list(reversed(s)), NOW), sort_keys=True)
        self.assertEqual(a, b)
        uids = [v["uid"] for v in D.build_work_views(c, w, s, NOW)["work_views"]]
        self.assertEqual(uids, sorted(uids))

    def test_input_not_mutated(self):
        c, w, s = self.data()
        man = {"course_subjects": {"C1": [{"subject": "matematica"}]}}
        before = json.dumps([c, w, s, man], sort_keys=True)
        D.build_work_views(c, w, s, NOW, manual=man)
        self.assertEqual(json.dumps([c, w, s, man], sort_keys=True), before)

    def test_four_groups_always_present(self):
        for v in views()["work_views"]:
            self.assertEqual(set(v), {"uid", "derive_version", "riferimento_temporale", "fonte", "derivato", "manuale",
                                      "da_verificare", "provenienza"})

    def test_no_notion_decision_in_l3(self):
        txt = json.dumps(views(), ensure_ascii=False).lower()
        for word in ("notion", "visibile", "proietta"):
            self.assertNotIn(word, txt)

    def test_derive_version_stable_and_sensitive_to_rules(self):
        self.assertTrue(D.DERIVE_VERSION.startswith("cls-d2-"))
        self.assertEqual(views()["derive_version"], D.DERIVE_VERSION)
        rules = dict(D._RULES, sy_start=8)
        other = hashlib.sha256((json.dumps(rules, sort_keys=True, ensure_ascii=False) + CL.interp_version())
                               .encode("utf-8")).hexdigest()[:8]
        self.assertNotEqual(D.DERIVE_VERSION.split("-")[-1], other)

    def test_no_network_no_clock_in_source(self):
        src = inspect.getsource(D)
        for bad in ("urllib", "socket", "http", ".now(", "utcnow", "today(", "time.time", "subprocess", "open("):
            self.assertNotIn(bad, src, bad)

    def test_registry_metadata_and_missing_in_source(self):
        recs = [{"raw": course(), "version": 2, "content_hash": "h1", "source_status": "present"}]
        w = [{"raw": work(), "version": 1, "content_hash": "h2", "source_status": "missing_in_source"}]
        s = [{"raw": sub(), "version": 1, "content_hash": "h3", "source_status": "present"}]
        v = D.build_work_views(recs, w, s, NOW)["work_views"][0]
        self.assertEqual(v["provenienza"]["course"]["version"], 2)
        self.assertEqual(v["provenienza"]["course_work"]["stato_registro"], "missing_in_source")
        self.assertIn("compito non piu' presente nella fonte", " ".join(v["da_verificare"]))


class FromRegistryReadOnly(unittest.TestCase):
    def test_reads_snapshot_without_touching_file(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "registry.sqlite3")
        reg = R.Registry(path)
        CL.sync_kind(reg, "course", lambda: [course(), course("C2", "Fisica 2026/27", "ARCHIVED")])
        CL.sync_kind(reg, "course_work", lambda: [work(), work("W2", "C2", due=None)])
        CL.sync_kind(reg, "submission", lambda: [sub(), sub("S2", "C2", "W2", "TURNED_IN")])
        reg.close()
        def read(f):
            with open(os.path.join(tmp, f), "rb") as fh:
                return fh.read(), os.stat(os.path.join(tmp, f)).st_mtime_ns
        snap = lambda: {f: read(f) for f in sorted(os.listdir(tmp))}
        before = snap()
        r = D.from_registry(path, NOW)
        self.assertEqual(snap(), before)                                       # byte per byte, nessun file nuovo
        self.assertEqual([v["uid"] for v in r["work_views"]], ["cls:C1/W1", "cls:C2/W2"])
        v = r["work_views"][0]
        self.assertEqual(v["provenienza"]["course_work"]["version"], 1)
        self.assertEqual(v["provenienza"]["course_work"]["stato_registro"], "present")
        self.assertEqual(r["conteggi"]["stato_operativo"], {"aperto": 1, "consegnato": 1})

    def test_accepts_open_registry_and_does_not_close_it(self):
        reg = R.Registry(":memory:")
        CL.sync_kind(reg, "course", lambda: [course()])
        D.from_registry(reg, NOW)
        self.assertEqual(len(reg.list_records()), 1)                            # ancora utilizzabile
        reg.close()


if __name__ == "__main__":
    unittest.main()
