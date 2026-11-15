import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import cls_fixtures as F        # noqa: E402
import cls_registry as CL       # noqa: E402
import cvv_registry as CR       # noqa: E402
import cvv_subject as subj      # noqa: E402
import registry as R            # noqa: E402
import school_context as SC     # noqa: E402

SUBJECTS = subj.build_subjects([
    {"id": 1, "description": "MATEMATICA", "teachers": [{"teacherName": "ROSSI MARIO"}]},
    {"id": 2, "description": "STORIA", "teachers": [{"teacherName": "BIANCHI ANNA"}]},
    {"id": 3, "description": "FISICA", "teachers": [{"teacherName": "ROSSI MARIO"}]},
    {"id": 4, "description": "DISEGNO E STORIA DELL'ARTE", "teachers": [{"teacherName": "VERDI LUCA"}]},
])
NOW = "2026-11-01T12:00:00+02:00"
CONF_MAT = {"subjects": {"matematica": {"classroom_course_ids": ["1001"], "nota": "dichiarato"}}}


def ev(eid, text, author="ROSSI MARIO", code="AGNT", day="2026-11-04"):
    return {"evtId": eid, "evtCode": code, "notes": text, "evtDatetimeBegin": day + "T10:00:00",
            "evtDatetimeEnd": day + "T11:00:00", "isFullDay": False, "authorName": author, "classDesc": "4X"}


def old_work(wid, cid="1001", year=2023, title="Esercizi vecchi (SINT.)"):
    """Compito STORICO (anno scolastico 2023-24)."""
    return {"courseId": cid, "id": wid, "title": title, "state": "PUBLISHED", "workType": "ASSIGNMENT",
            "creationTime": "%d-10-10T08:00:00Z" % year, "updateTime": "%d-10-10T08:00:00Z" % year,
            "dueDate": {"year": year, "month": 10, "day": 20}, "dueTime": {"hours": 21, "minutes": 59}, "topicId": "t-old"}


def old_sub(sid, wid, cid="1001"):
    return {"courseId": cid, "courseWorkId": wid, "id": sid, "userId": "5001", "state": "TURNED_IN",
            "creationTime": "2023-10-11T08:00:00Z", "updateTime": "2023-10-11T08:00:00Z"}


def make_registry(extra_works=(), extra_subs=(), extra_courses=()):
    reg = R.Registry(":memory:")
    CR.sync_kind(reg, "agenda_event", lambda: [
        ev(1, "Verifica di matematica: limiti", day="2026-11-17"),
        ev(2, "Verifica di matematica: algebra", day="2026-10-18"),
        ev(3, "Esercizi di matematica pag. 10", code="AGHW", day="2026-11-07"),
        ev(4, "Verifica di storia", author="BIANCHI ANNA", day="2026-11-18"),
        ev(5, "Verifica scritta", author="ROSSI MARIO", day="2026-11-19"),          # matematica o fisica: ambigua
        ev(6, "Assemblea di classe", author="BIANCHI ANNA", day="2026-11-09")])
    CR.sync_kind(reg, "grade", lambda: [{"evtId": 90, "subjectDesc": "MATEMATICA", "displayValue": "7", "evtDate": "2026-10-29",
                                         "componentDesc": "Scritto", "weightFactor": 1}])
    CR.sync_kind(reg, "absence", lambda: [])
    CL.sync_kind(reg, "course", lambda: [dict(F.COURSE_A), dict(F.COURSE_B)] + [dict(c) for c in extra_courses])
    CL.sync_kind(reg, "course_work", lambda: [dict(w) for w in F.WORK] + [dict(w) for w in extra_works])
    CL.sync_kind(reg, "submission", lambda: [dict(s) for s in F.SUBMISSIONS] + [dict(s) for s in extra_subs])
    CL.sync_kind(reg, "announcement", lambda: [dict(a) for a in F.ANNOUNCEMENTS] + [
        {"courseId": "1001", "id": "a-old", "text": "Avviso vecchio (SINT.)", "state": "PUBLISHED",
         "creationTime": "2023-10-01T10:00:00Z", "updateTime": "2023-10-01T10:00:00Z"}])
    CL.sync_kind(reg, "material", lambda: [dict(m) for m in F.MATERIALS] + [
        {"courseId": "1001", "id": "m-new", "title": "Dispensa limiti (SINT.)", "state": "PUBLISHED", "topicId": "t1",
         "creationTime": "2026-10-23T09:00:00Z", "updateTime": "2026-10-23T09:00:00Z"}])
    CL.sync_kind(reg, "topic", lambda: [dict(t) for t in F.TOPICS] + [
        {"courseId": "1001", "topicId": "t-old", "name": "Argomento vecchio (SINT.)", "updateTime": "2023-09-20T09:00:00Z"},
        {"courseId": "1001", "topicId": "t-unused", "name": "Mai usato (SINT.)", "updateTime": "2026-10-18T09:00:00Z"}])
    return reg


def uids(rows):
    return [r["uid"] for r in rows]


class Base(unittest.TestCase):
    def setUp(self):
        self.reg = make_registry(extra_works=[old_work("w-old")], extra_subs=[old_sub("s-old", "w-old")])

    def ctx(self, subject="matematica", manual=None, now=NOW, **kw):
        return SC.build(self.reg, subject, SUBJECTS, now, manual=manual, **kw)

    def internal(self, subject="matematica", manual=None, now=NOW, **kw):
        """Vista INTERNA (storico/tutto/anno): non esposta agli agenti."""
        return SC._build(self.reg, subject, SUBJECTS, now, manual=manual, **kw)


class ClasseVivaPart(Base):
    """Parte ClasseViva: invariata rispetto alla v1."""
    def test_agenda_filtered_by_subject_with_citations(self):
        c = self.ctx()
        fut = c["agenda_classeviva"]["verifiche_future"]
        self.assertEqual(uids(fut), ["cvv:1"])
        self.assertEqual(uids(c["agenda_classeviva"]["verifiche_passate"]), ["cvv:2"])
        self.assertEqual(uids(c["agenda_classeviva"]["compiti"]), ["cvv:3"])
        self.assertEqual(fut[0]["fonte"]["registry_version"], 1)
        self.assertTrue(fut[0]["fonte"]["content_hash"])
        self.assertNotIn("cvv:4", json.dumps(c["agenda_classeviva"]))

    def test_ambiguous_events_not_assigned_but_reported(self):
        c = self.ctx()
        self.assertIn("cvv:5", uids(c["possibili_da_verificare"]))
        self.assertNotIn("cvv:5", json.dumps(c["agenda_classeviva"]))
        self.assertTrue(c["da_verificare"])

    def test_other_subject_sees_its_own_only(self):
        c = self.ctx("storia")
        self.assertEqual(uids(c["agenda_classeviva"]["verifiche_future"]), ["cvv:4"])
        self.assertEqual(uids(c["agenda_classeviva"]["eventi"]), ["cvv:6"])

    def test_grades_by_subject_and_empty_state_declared(self):
        self.assertEqual(len(self.ctx()["voti_classeviva"]["materia"]), 1)
        c = self.ctx("storia")
        self.assertEqual(c["voti_classeviva"]["materia"], [])
        self.assertIn("stato vuoto valido", c["voti_classeviva"]["nota"])

    def test_missing_records_excluded_and_counted(self):
        CR.sync_kind(self.reg, "agenda_event", lambda: [
            ev(1, "Verifica di matematica: limiti", day="2026-11-17"), ev(2, "Verifica di matematica: algebra", day="2026-10-18"),
            ev(3, "Esercizi di matematica pag. 10", code="AGHW", day="2026-11-07"),
            ev(4, "Verifica di storia", author="BIANCHI ANNA", day="2026-11-18"),
            ev(5, "Verifica scritta", day="2026-11-19")])      # cvv:6 sparito (1 su 6: sopra soglia)
        c = self.ctx("storia")
        self.assertEqual(c["provenienza"]["agenda_scomparsa_dalla_fonte"], 1)
        self.assertEqual(c["agenda_classeviva"]["eventi"], [])

    def test_raw_only_on_request(self):
        self.assertNotIn("raw", self.ctx()["agenda_classeviva"]["verifiche_future"][0])
        self.assertIn("raw", self.ctx(include_raw=True)["agenda_classeviva"]["verifiche_future"][0])
        t = self.ctx(manual=CONF_MAT, include_raw=True)["classroom"]["confermato"]["compiti"][0]
        self.assertIn("raw", t)


class Subjects(Base):
    def test_unknown_subject(self):
        with self.assertRaises(ValueError):
            self.ctx("astronomia")

    def test_excluded_subjects_refused(self):
        for s in ("arte", "motorie", "civica", "religione", "studio", "DISEGNO E STORIA DELL'ARTE"):
            with self.assertRaises(ValueError) as cm:
                self.ctx(s)
            self.assertIn("esclusa", str(cm.exception), s)

    def test_agent_subjects_accepted_and_separate(self):
        for s in SC.AGENT_SUBJECTS:
            c = self.ctx(s)
            self.assertEqual(c["materia"]["chiave"], s)

    def test_subject_by_official_name_part(self):
        self.assertEqual(self.ctx("MATEMATICA")["materia"]["chiave"], "matematica")
        self.assertEqual(self.ctx("matematica")["materia"]["docenti_registro"], ["ROSSI MARIO"])

    def test_school_subjects_catalog(self):
        cat = SC.subjects_catalog(SUBJECTS)
        self.assertEqual([m["chiave"] for m in cat["materie"]],
                         ["italiano", "latino", "inglese", "storia", "filosofia", "matematica", "fisica", "scienze"])
        self.assertEqual([m["chiave"] for m in cat["escluse"]], ["arte", "motorie", "civica", "religione", "studio"])
        self.assertEqual(next(m for m in cat["materie"] if m["chiave"] == "matematica")["nome_ufficiale"], "MATEMATICA")
        self.assertEqual(len(SC.subjects_catalog()["materie"]), 8)          # anche senza nomi ClasseViva


class ClassroomL3(Base):
    def test_uses_l3_task_with_linked_submission(self):
        c = self.ctx(manual=CONF_MAT)
        t = {x["uid"]: x for x in c["classroom"]["confermato"]["compiti"]}["cls:1001/w1"]
        self.assertEqual(t["consegna"]["uid"], "cls:1001/s1")
        self.assertEqual(t["consegna"]["stato_fonte"], "TURNED_IN")
        self.assertEqual(t["stato_operativo"], "consegnato")
        self.assertEqual(t["perimetro"], "operativo")
        self.assertEqual(t["anno_compito"]["valore"], "2026-27")
        self.assertEqual(t["fonte"]["course_work"]["registry_version"], 1)
        self.assertTrue(t["fonte"]["course_work"]["content_hash"])
        self.assertEqual(t["fonte"]["submission"]["uid"], "cls:1001/s1")
        self.assertEqual(t["derive_version"], c["provenienza"]["derive_version"])

    def test_no_separate_submission_list(self):
        c = self.ctx(manual=CONF_MAT)
        self.assertNotIn("consegne", c["classroom"]["confermato"])
        self.assertNotIn("consegne", c["classroom"])

    def test_task_without_submission_is_da_verificare(self):
        t = {x["uid"]: x for x in self.ctx(manual=CONF_MAT)["classroom"]["confermato"]["compiti"]}["cls:1001/w2"]
        self.assertFalse(t["consegna"]["presente"])
        self.assertEqual(t["stato_operativo"], "da_verificare")
        self.assertTrue(t["da_verificare"])

    def test_orphan_submission_reported(self):
        self.reg = make_registry(extra_subs=[old_sub("s-orfana", "w-404")])
        c = self.ctx(manual=CONF_MAT)
        self.assertEqual(uids(c["classroom"]["confermato"]["consegne_orfane"]), ["cls:1001/s-orfana"])
        self.assertTrue(any("consegne senza compito" in x for x in c["da_verificare"]))


class Scope(Base):
    def test_default_operational_excludes_history(self):
        cr = self.ctx(manual=CONF_MAT)["classroom"]["confermato"]
        self.assertNotIn("cls:1001/w-old", uids(cr["compiti"]))
        self.assertEqual(cr["compiti_per_perimetro_nei_corsi"].get("storico"), 1)
        self.assertTrue(all(t["perimetro"] == "operativo" for t in cr["compiti"]))
        self.assertNotIn("cls:1001/a-old", uids(cr["annunci"]))
        self.assertEqual(cr["annunci_conteggio"]["esclusi_per_perimetro"], {"storico": 1})

    def test_internal_history_view(self):
        c = self.internal(manual=CONF_MAT, ambito="storico")
        cr = c["classroom"]["confermato"]
        self.assertEqual(uids(cr["compiti"]), ["cls:1001/w-old"])
        self.assertEqual(uids(cr["annunci"]), ["cls:1001/a-old"])
        self.assertEqual(c["ambito"]["valore"], "storico")
        self.assertIn("INTERNA", c["ambito"]["nota"])

    def test_internal_everything(self):
        cr = self.internal(manual=CONF_MAT, ambito="tutto")["classroom"]["confermato"]
        self.assertEqual(set(uids(cr["compiti"])), {"cls:1001/w1", "cls:1001/w2", "cls:1001/w3", "cls:1001/w-old"})

    def test_internal_single_year(self):
        cr = self.internal(manual=CONF_MAT, anno="2023-24")["classroom"]["confermato"]
        self.assertEqual(uids(cr["compiti"]), ["cls:1001/w-old"])
        with self.assertRaises(ValueError):
            self.internal(anno="2023")
        with self.assertRaises(ValueError):
            self.internal(ambito="recenti")

    def test_agent_facing_build_has_no_history_parameters(self):
        for kw in ({"ambito": "storico"}, {"ambito": "tutto"}, {"anno": "2023-24"}):
            with self.assertRaises(TypeError):
                self.ctx(manual=CONF_MAT, **kw)
        import inspect
        self.assertNotIn("ambito", inspect.signature(SC.build).parameters)
        self.assertNotIn("ambito", inspect.signature(SC.build_from_path).parameters)
        c = self.ctx(manual=CONF_MAT)
        self.assertNotIn("w-old", json.dumps(c))                       # nessun compito/annuncio/argomento storico
        self.assertNotIn("a-old", json.dumps(c))
        self.assertNotIn("t-old", json.dumps(c))

    def test_topics_only_when_linked_to_included_items(self):
        op = self.ctx(manual=CONF_MAT)["classroom"]["confermato"]["argomenti"]
        self.assertEqual([t["topic_id"] for t in op], ["t1"])                  # t1 (w1 + materiale); mai t-unused/t-old
        st = self.internal(manual=CONF_MAT, ambito="storico")["classroom"]["confermato"]["argomenti"]
        self.assertEqual([t["topic_id"] for t in st], ["t-old"])

    def test_operational_scope_declared_without_history_invitation(self):
        a = self.ctx()["ambito"]
        self.assertEqual((a["valore"], a["anno"]), ("operativo", None))
        self.assertNotIn("come_richiedere_storico", a)
        self.assertIn("non e' esposto agli agenti", a["nota"])
        self.assertEqual(a["perimetro_operativo"], {"2025-26": "3a classe", "2026-27": "4a classe"})


class ConfirmedVsProposed(Base):
    def test_proposed_is_separate_and_marked(self):
        c = self.ctx()                                                        # nessuna conferma
        self.assertEqual(c["classroom"]["confermato"]["corsi"], [])
        self.assertEqual(c["classroom"]["confermato"]["compiti"], [])
        p = c["classroom_proposto"]
        self.assertEqual([x["course_id"] for x in p["corsi"]], ["1001"])
        self.assertEqual(p["stato"], "PROPOSTA NON CONFERMATA")
        self.assertIn("NON sono fatti", p["avviso"])
        self.assertIn("cls:1001/w1", uids(p["compiti"]))
        self.assertTrue(all(t["materia"]["decisione"] is None for t in p["compiti"]))

    def test_confirmation_moves_course_out_of_proposed(self):
        c = self.ctx(manual=CONF_MAT)
        self.assertEqual([x["course_id"] for x in c["classroom"]["confermato"]["corsi"]], ["1001"])
        self.assertEqual(c["classroom_proposto"]["corsi"], [])
        self.assertEqual(c["livello_manuale"]["dichiarato_dall_utente"]["nota"], "dichiarato")

    def test_proposals_follow_l3_explicit_rule(self):
        self.reg = make_registry(extra_courses=[{"id": "1009", "name": "4^ZC Storia dell'arte (prova)",
                                                 "courseState": "ACTIVE", "creationTime": "2026-09-29T08:00:00Z"}])
        self.assertNotIn("1009", [x["course_id"] for x in self.ctx("storia")["classroom_proposto"]["corsi"]])

    def test_confirmed_but_unknown_course_flagged(self):
        c = self.ctx(manual={"subjects": {"matematica": {"classroom_course_ids": ["9999"]}}})
        self.assertTrue(any("9999" in x for x in c["da_verificare"]))

    def test_no_classroom_data_declared(self):
        c = SC.build(R.Registry(":memory:"), "matematica", SUBJECTS, NOW)
        self.assertIn("nessun dato Classroom", c["classroom"]["stato"])


class Isolation(Base):
    def test_subject_sees_only_its_courses(self):
        man = {"subjects": {"matematica": {"classroom_course_ids": ["1001"]}, "storia": {"classroom_course_ids": ["1002"]}}}
        mat, sto = self.ctx("matematica", manual=man), self.ctx("storia", manual=man)
        self.assertNotIn("cls:1002/", json.dumps(mat["classroom"]))
        self.assertNotIn("cls:1001/", json.dumps(sto["classroom"]))
        self.assertEqual(uids(sto["classroom"]["confermato"]["compiti"]), ["cls:1002/w9"])
        self.assertNotIn("cls:1001/", json.dumps(mat["classroom_proposto"]) + json.dumps(sto["classroom_proposto"]))

    def test_shared_course_condiviso_con(self):
        man = {"subjects": {"matematica": {"classroom_course_ids": ["1001"]}, "fisica": {"classroom_course_ids": ["1001"]}}}
        mat, fis = self.ctx("matematica", manual=man), self.ctx("fisica", manual=man)
        self.assertEqual(uids(mat["classroom"]["confermato"]["compiti"]), uids(fis["classroom"]["confermato"]["compiti"]))
        tm, tf = mat["classroom"]["confermato"]["compiti"][0], fis["classroom"]["confermato"]["compiti"][0]
        self.assertEqual((tm["corso"]["condiviso_con"], tf["corso"]["condiviso_con"]), (["fisica"], ["matematica"]))
        self.assertEqual(mat["classroom"]["confermato"]["corsi"][0]["condiviso_con"], ["fisica"])
        self.assertEqual(tm["materia"]["stato"], "ambigua_confermata")      # nessuna scelta automatica
        self.assertIsNone(tm["materia"]["decisione"])
        self.assertEqual(mat["classroom"]["confermato"]["annunci"][0]["condiviso_con"], ["fisica"])

    def test_excluded_subject_confirmation_ignored(self):
        man = {"subjects": {"arte": {"classroom_course_ids": ["1001"]}, "matematica": {"classroom_course_ids": ["1001"]}}}
        self.assertEqual(self.ctx(manual=man)["classroom"]["confermato"]["corsi"][0]["condiviso_con"], [])


class Truncation(unittest.TestCase):
    def test_safe_truncation_newest_first_open_first(self):
        works = [{"courseId": "1001", "id": "x%03d" % i, "title": "T%d" % i, "state": "PUBLISHED", "workType": "ASSIGNMENT",
                  "creationTime": "2026-10-%02dT08:00:00Z" % (1 + i % 28), "updateTime": "2026-09-29T08:00:00Z",
                  "dueDate": {"year": 2026, "month": 10, "day": 1 + i % 28}, "dueTime": {"hours": 8, "minutes": 0}}
                 for i in range(230)]
        subs = [{"courseId": "1001", "courseWorkId": w["id"], "id": "s" + w["id"], "userId": "5001", "state": "TURNED_IN",
                 "updateTime": "2026-09-29T08:00:00Z"} for w in works]
        works.append({"courseId": "1001", "id": "zzz-open", "title": "Aperto", "state": "PUBLISHED", "workType": "ASSIGNMENT",
                      "creationTime": "2025-09-02T08:00:00Z", "updateTime": "2025-09-02T08:00:00Z",
                      "dueDate": {"year": 2026, "month": 11, "day": 27}, "dueTime": {"hours": 8, "minutes": 0}})
        subs.append({"courseId": "1001", "courseWorkId": "zzz-open", "id": "s-open", "userId": "5001", "state": "CREATED"})
        reg = make_registry(extra_works=works, extra_subs=subs)
        cr = SC.build(reg, "matematica", SUBJECTS, NOW, manual=CONF_MAT)["classroom"]["confermato"]
        meta = cr["compiti_conteggio"]
        self.assertEqual(meta["totale"], 234)                          # 230 + w1,w2,w3 + aperto
        self.assertEqual((meta["inclusi"], meta["omessi"], meta["troncato"]), (200, 34, True))
        self.assertIn("restano nel registro", meta["come_richiedere_il_resto"])
        self.assertNotIn("anno", meta["come_richiedere_il_resto"])         # nessun invito allo storico per gli agenti
        self.assertEqual(cr["compiti"][0]["uid"], "cls:1001/zzz-open")  # gli aperti prima
        dates = [t["scadenza"]["data_locale"] or t["creato"][:10] for t in cr["compiti"][1:]]
        self.assertEqual(dates, sorted(dates, reverse=True))           # poi i piu' recenti
        kept = set(uids(cr["compiti"]))
        self.assertIn("cls:1001/w1", kept)                             # scadenza 30/10: tra i piu' recenti
        self.assertNotIn("cls:1001/x000", kept)                        # scadenza 1/9: tra i piu' vecchi, omesso

    def test_not_truncated_declares_it(self):
        meta = SC.build(make_registry(), "matematica", SUBJECTS, NOW, manual=CONF_MAT)["classroom"]["confermato"]["compiti_conteggio"]
        self.assertEqual((meta["troncato"], meta["omessi"]), (False, 0))
        self.assertNotIn("come_richiedere_il_resto", meta)


class Properties(Base):
    def test_deterministic_same_now(self):
        a, b = self.ctx(manual=CONF_MAT), self.ctx(manual=CONF_MAT)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(a["schema"], "cloud.school.context/2")
        self.assertEqual(a["riferimento_temporale"], "2026-11-01T12:00:00+02:00")
        self.assertTrue(any("non inventare" in r for r in a["regole_per_gli_agenti"]))
        self.assertTrue(any("classroom_proposto" in r for r in a["regole_per_gli_agenti"]))

    def test_now_required_with_timezone(self):
        with self.assertRaises(ValueError):
            self.ctx(now="2026-11-01")
        with self.assertRaises(ValueError):
            self.ctx(now="2026-11-01T12:00:00")

    def test_now_changes_state_deterministically(self):
        before = {t["uid"]: t for t in self.ctx(manual=CONF_MAT)["classroom"]["confermato"]["compiti"]}
        after = {t["uid"]: t for t in self.ctx(manual=CONF_MAT, now="2026-12-13T12:00:00+01:00")["classroom"]["confermato"]["compiti"]}
        self.assertEqual(before["cls:1001/w1"]["scadenza"]["giorni_mancanti"], 26)
        self.assertEqual(before["cls:1001/w3"]["stato_operativo"], "restituito")
        self.assertEqual(after["cls:1001/w1"]["scadenza"]["giorni_mancanti"], -16)

    def test_building_context_changes_nothing(self):
        before = (len(self.reg.list_records()), len(self.reg.events()), self.reg.verify_integrity())
        self.internal(manual=CONF_MAT, ambito="tutto")
        self.assertEqual(before, (len(self.reg.list_records()), len(self.reg.events()), self.reg.verify_integrity()))

    def test_manual_input_not_mutated(self):
        man = copy.deepcopy(CONF_MAT)
        self.ctx(manual=man)
        self.assertEqual(man, CONF_MAT)


class Propagation(unittest.TestCase):
    """Classroom (sintetico) -> registro -> L3 -> school_context: elemento nuovo e aggiornato."""
    def test_new_and_updated_assignment_reach_context(self):
        reg = make_registry()
        first = SC.build(reg, "matematica", SUBJECTS, NOW, manual=CONF_MAT)["classroom"]["confermato"]
        self.assertNotIn("cls:1001/w-new", uids(first["compiti"]))
        works = [copy.deepcopy(w) for w in F.WORK]
        works[0]["title"] = "Esercizi cap. 3 (SINT.) AGGIORNATO"
        works.append({"courseId": "1001", "id": "w-new", "title": "Nuovo (SINT.)", "state": "PUBLISHED",
                      "workType": "ASSIGNMENT", "creationTime": "2026-11-01T08:00:00Z", "updateTime": "2026-11-01T08:00:00Z",
                      "dueDate": {"year": 2026, "month": 11, "day": 9}, "dueTime": {"hours": 21, "minutes": 59}})
        subs = [copy.deepcopy(s) for s in F.SUBMISSIONS] + [
            {"courseId": "1001", "courseWorkId": "w-new", "id": "s-new", "userId": "5001", "state": "CREATED"}]
        CL.sync_kind(reg, "course_work", lambda: works)
        CL.sync_kind(reg, "submission", lambda: subs)
        cr = SC.build(reg, "matematica", SUBJECTS, NOW, manual=CONF_MAT)["classroom"]["confermato"]
        t = {x["uid"]: x for x in cr["compiti"]}
        self.assertEqual(t["cls:1001/w-new"]["stato_operativo"], "aperto")
        self.assertEqual(t["cls:1001/w-new"]["consegna"]["uid"], "cls:1001/s-new")
        self.assertEqual(t["cls:1001/w-new"]["perimetro"], "operativo")
        self.assertEqual(cr["compiti"][0]["uid"], "cls:1001/w-new")          # aperto -> in testa
        self.assertEqual(t["cls:1001/w1"]["titolo"], "Esercizi cap. 3 (SINT.) AGGIORNATO")
        self.assertEqual(t["cls:1001/w1"]["fonte"]["course_work"]["registry_version"], 2)
        sto = SC.build(reg, "storia", SUBJECTS, NOW, manual=dict(CONF_MAT, subjects={**CONF_MAT["subjects"],
                                                                                        "storia": {"classroom_course_ids": ["1002"]}}))
        self.assertNotIn("w-new", json.dumps(sto["classroom"]))                 # isolamento per materia


class OnDisk(unittest.TestCase):
    def test_build_from_path_is_read_only_and_manual_never_created(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "registry.sqlite3")
        reg = R.Registry(path)
        CR.sync_kind(reg, "agenda_event", lambda: [ev(1, "Verifica di matematica", day="2026-11-17")])
        CL.sync_kind(reg, "course", lambda: [dict(F.COURSE_A)])
        CL.sync_kind(reg, "course_work", lambda: [dict(F.WORK[0])])
        reg.close()
        import sqlite3
        c = sqlite3.connect(path); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.close()

        def h():
            with open(path, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        before, listing = h(), sorted(os.listdir(d))
        manual = os.path.join(d, "manual", "school_manual.json")
        out = SC.build_from_path(path, "matematica", SUBJECTS, NOW, manual_path=manual)
        self.assertEqual(uids(out["agenda_classeviva"]["verifiche_future"]), ["cvv:1"])
        self.assertEqual(uids(out["classroom_proposto"]["compiti"]), ["cls:1001/w1"])
        self.assertEqual(h(), before)
        self.assertEqual(out["provenienza"]["registro"], os.path.abspath(path))
        self.assertTrue(out["provenienza"]["copia_in_sola_lettura"])
        self.assertEqual(sorted(os.listdir(d)), listing)
        self.assertFalse(os.path.exists(manual))
        self.assertFalse(SC.load_manual(manual)["presente"])

    def test_manual_layer_is_read_not_written(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "m.json")
        with open(p, "w") as fh:
            json.dump({"subjects": {"fisica": {"nota": "x"}}}, fh)
        m = SC.load_manual(p)
        self.assertTrue(m["presente"])
        self.assertEqual(m["dati"]["subjects"]["fisica"]["nota"], "x")


class ServerTool(unittest.TestCase):
    def setUp(self):
        import server
        from unittest import mock
        self.server, self.mock = server, mock
        self.d = tempfile.mkdtemp()
        self.db = os.path.join(self.d, "registry.sqlite3")
        p = mock.patch.dict(os.environ, {"CLOUD_SCHOOL_DB": self.db})
        p.start(); self.addCleanup(p.stop)
        q = mock.patch.object(server, "subjects", lambda: SUBJECTS)
        q.start(); self.addCleanup(q.stop)

    def test_missing_registry_is_clear_error_and_not_created(self):
        with self.assertRaises(ValueError) as cm:
            self.server.school_context("matematica")
        self.assertIn("registro locale assente", str(cm.exception))
        self.assertFalse(os.path.exists(self.db))

    def test_reads_registry_without_writing(self):
        reg = R.Registry(self.db)
        CR.sync_kind(reg, "agenda_event", lambda: [ev(1, "Verifica di matematica", day="2999-10-20")])
        reg.close()
        import sqlite3
        c = sqlite3.connect(self.db); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.close()
        before = sorted(os.listdir(self.d))
        out = self.server.school_context("matematica")
        self.assertEqual(uids(out["agenda_classeviva"]["verifiche_future"]), ["cvv:1"])
        self.assertEqual(out["ambito"]["valore"], "operativo")
        self.assertEqual(sorted(os.listdir(self.d)), before)
        with self.assertRaises(TypeError):
            self.server.school_context("matematica", ambito="storico")      # storico non esposto agli agenti
        with self.assertRaises(ValueError):
            self.server.school_context("religione")

    def test_schema_requires_subject_and_is_read_only_described(self):
        d = self.server.srv._tools["school_context"]["def"]
        self.assertEqual(d["inputSchema"]["required"], ["subject"])
        self.assertIn("sola lettura", d["description"])
        self.assertEqual(set(d["inputSchema"]["properties"]), {"subject", "include_raw"})   # niente ambito/anno
        self.assertIn("SOLO perimetro operativo", d["description"])

    def test_cls_course_subjects_tool_read_only(self):
        reg = R.Registry(self.db)
        CL.sync_kind(reg, "course", lambda: [dict(F.COURSE_A), {"id": "9", "name": "Scienze Motorie", "courseState": "ACTIVE"}])
        reg.close()
        import sqlite3
        c = sqlite3.connect(self.db); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.close()
        before = sorted(os.listdir(self.d))
        with self.mock.patch.object(SC, "manual_default_path", lambda: os.path.join(self.d, "manual", "school_manual.json")):
            out = self.server.cls_course_subjects()
        self.assertEqual(sorted(os.listdir(self.d)), before)                  # nulla creato (neanche manual/)
        self.assertFalse(out["school_manual"]["presente"])
        cat = {r["course_id"]: r["categoria"] for r in out["corsi"]}
        self.assertEqual(cat, {"1001": "proposed", "9": "excluded"})
        self.assertEqual(out["conteggi"]["confirmed"], 0)
        d = self.server.srv._tools["cls_course_subjects"]["def"]
        self.assertIn("sola lettura", d["description"])

    def test_school_subjects_tool(self):
        out = self.server.school_subjects()
        self.assertEqual(len(out["materie"]), 8)
        self.assertEqual([m["chiave"] for m in out["escluse"]], ["arte", "motorie", "civica", "religione", "studio"])
        self.assertIn("sola lettura", self.server.srv._tools["school_subjects"]["def"]["description"])


class SingleProposalSource(Base):
    """Contratto: le proposte corso -> materia del contesto vengono SOLO da cls_subjects; il campo legacy di L3
    `derivato.materia_proposte` (cls_derive.subject_proposals, deprecato) non e' esposto ne' usato."""
    MF = {"id": "1010", "name": "4Z Mate & Fisica (prova) 2026/27", "courseState": "ACTIVE", "creationTime": "2026-09-29T08:00:00Z"}
    MF_WORK = {"courseId": "1010", "id": "mf1", "title": "Esercizi (SINT.)", "state": "PUBLISHED", "workType": "ASSIGNMENT",
               "creationTime": "2026-10-18T08:00:00Z", "updateTime": "2026-10-18T08:00:00Z",
               "dueDate": {"year": 2026, "month": 11, "day": 17}, "dueTime": {"hours": 21, "minutes": 59}}
    MF_SUB = {"courseId": "1010", "courseWorkId": "mf1", "id": "mfs1", "userId": "5001", "state": "CREATED"}

    def setUp(self):
        self.reg = make_registry(extra_courses=[self.MF], extra_works=[self.MF_WORK], extra_subs=[self.MF_SUB])

    def all_tasks(self, c):
        return c["classroom"]["confermato"]["compiti"] + c["classroom_proposto"]["compiti"]

    def test_no_task_exposes_materia_proposte_or_l3_derivato(self):
        man = {"subjects": {"matematica": {"classroom_course_ids": ["1001"]}}}
        for s in SC.AGENT_SUBJECTS:
            for m in (None, man):
                tasks = self.all_tasks(self.ctx(s, manual=m))
                for t in tasks:
                    self.assertNotIn("materia_proposte", t, (s, t["uid"]))
                    self.assertNotIn("derivato", t, (s, t["uid"]))
        self.assertTrue(self.all_tasks(self.ctx("matematica", manual=man)))   # il controllo non e' vuoto

    def test_mate_fisica_proposed_by_cls_subjects_for_both(self):
        for s in ("matematica", "fisica"):
            corsi = {c["course_id"]: c for c in self.ctx(s)["classroom_proposto"]["corsi"]}
            self.assertIn("1010", corsi, s)
            self.assertEqual(corsi["1010"]["materie_proposte"], ["fisica", "matematica"])
            self.assertEqual(corsi["1010"]["categoria"], "proposed")
            self.assertEqual([e["regola"] for e in corsi["1010"]["evidenze"]], ["E2"])
            self.assertIn("cls:1010/mf1", uids(self.ctx(s)["classroom_proposto"]["compiti"]))
        self.assertNotIn("1010", [c["course_id"] for c in self.ctx("storia")["classroom_proposto"]["corsi"]])

    def test_context_does_not_depend_on_legacy_l3_field(self):
        import cls_derive as D
        legacy = {v["uid"]: v for v in D.from_registry(self.reg, NOW)["work_views"]}["cls:1010/mf1"]
        self.assertEqual([p["subject"] for p in legacy["derivato"]["materia_proposte"]], ["fisica"])  # legacy: niente mate
        self.assertIn("1010", [c["course_id"] for c in self.ctx("matematica")["classroom_proposto"]["corsi"]])
        from unittest import mock
        with mock.patch.object(D, "subject_proposals", lambda raw: [{"subject": "storia", "stato": "proposto"}]):
            self.assertEqual([c["course_id"] for c in self.ctx("storia")["classroom_proposto"]["corsi"]], ["1002"])
            self.assertIn("1010", [c["course_id"] for c in self.ctx("matematica")["classroom_proposto"]["corsi"]])

    def test_dead_v1_proposal_function_removed(self):
        self.assertFalse(hasattr(SC, "propose_links"))


class CourseSubjects(Base):
    """Integrazione cls_subjects -> school_context: solo le conferme manuali entrano nel contesto."""
    def test_without_manual_only_proposed_never_confirmed(self):
        c = self.ctx()
        self.assertEqual(c["classroom"]["confermato"]["corsi"], [])
        self.assertEqual(c["classroom"]["confermato"]["compiti"], [])
        p = c["classroom_proposto"]["corsi"][0]
        self.assertEqual((p["course_id"], p["categoria"]), ("1001", "proposed"))
        self.assertEqual(p["materie_proposte"], ["matematica"])
        self.assertEqual(p["evidenze"][0]["regola"], "E2")

    def test_shared_course_proposed_to_both_subjects(self):
        self.reg = make_registry(extra_courses=[{"id": "1010", "name": "4Z Mate & Fisica (prova) 2026/27", "courseState": "ACTIVE",
                                                 "creationTime": "2026-09-29T08:00:00Z"}])
        for s in ("matematica", "fisica"):
            self.assertIn("1010", [x["course_id"] for x in self.ctx(s)["classroom_proposto"]["corsi"]], s)
        self.assertNotIn("1010", [x["course_id"] for x in self.ctx("storia")["classroom_proposto"]["corsi"]])

    def test_excluded_and_non_attributed_never_in_any_context(self):
        self.reg = make_registry(extra_courses=[
            {"id": "2001", "name": "Scienze Motorie", "courseState": "ACTIVE"},
            {"id": "2002", "name": "4^ ZZ - ABC", "courseState": "ACTIVE"},
            {"id": "2003", "name": "RELIGIONE 2026-2027", "courseState": "ACTIVE", "subject": "RELIGIONE CATTOLICA"}])
        for s in SC.AGENT_SUBJECTS:
            c = self.ctx(s)
            txt = json.dumps(c["classroom_proposto"]) + json.dumps(c["classroom"])
            for cid in ("2001", "2002", "2003"):
                self.assertNotIn('"%s"' % cid, txt, (s, cid))

    def test_manual_confirmation_for_excluded_subject_is_ignored_and_reported(self):
        c = self.ctx(manual={"subjects": {"religione": {"classroom_course_ids": ["1001"]}}})
        self.assertEqual(c["classroom"]["confermato"]["corsi"], [])
        self.assertTrue(any("religione" in x and "non ammessa" in x for x in c["da_verificare"]))

    def test_report_matches_context(self):
        rep = SC.course_subjects_report(self.reg, SUBJECTS, CONF_MAT)
        row = {r["course_id"]: r for r in rep["corsi"]}
        self.assertEqual((row["1001"]["categoria"], row["1001"]["materie_confermate"]), ("confirmed", ["matematica"]))
        self.assertEqual(row["1002"]["categoria"], "proposed")


if __name__ == "__main__":
    unittest.main()
