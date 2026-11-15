"""school_agent: brief di avvio + recupero su richiesta per gli agenti di materia. Dati SINTETICI.
Isolamento per materia, solo corsi confermati, solo perimetro operativo, sola lettura."""
import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import cls_fixtures as F                    # noqa: E402
import cls_registry as CL                   # noqa: E402
import registry as R                        # noqa: E402
import school_agent as A                    # noqa: E402
import school_context as SC                 # noqa: E402
from test_school_context import (NOW, SUBJECTS, make_registry, old_sub, old_work)   # noqa: E402

ITA_COURSE = {"id": "3001", "name": "Lettere - Neri", "courseState": "ACTIVE", "subject": "Italiano, Latino",
              "creationTime": "2025-09-05T08:00:00Z"}
ITA_WORK = {"courseId": "3001", "id": "iw1", "title": "Tema argomentativo (SINT.)", "state": "PUBLISHED",
            "workType": "ASSIGNMENT", "creationTime": "2026-10-23T08:00:00Z", "updateTime": "2026-10-23T08:00:00Z",
            "description": "Consegna su carta (SINT.)", "dueDate": {"year": 2026, "month": 11, "day": 6},
            "dueTime": {"hours": 21, "minutes": 59}}
ITA_SUB = {"courseId": "3001", "courseWorkId": "iw1", "id": "is1", "userId": "5001", "state": "CREATED"}
MANUAL = {"schema": "cloud.school.manual/1",
          "subjects": {"matematica": {"classroom_course_ids": ["1001"]}, "storia": {"classroom_course_ids": ["1002"]},
                       "italiano": {"classroom_course_ids": ["3001"]}, "latino": {"classroom_course_ids": ["3001"]}}}


def reg_all():
    return make_registry(extra_works=[old_work("w-old"), ITA_WORK], extra_subs=[old_sub("s-old", "w-old"), ITA_SUB],
                         extra_courses=[ITA_COURSE])


def dump(o):
    return json.dumps(o, ensure_ascii=False, sort_keys=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.reg = reg_all()

    def brief(self, s="matematica", manual=MANUAL, now=NOW):
        return A.brief(self.reg, s, SUBJECTS, now, manual=manual)


class Brief(Base):
    def test_shape_and_confirmed_only(self):
        b = self.brief()
        self.assertEqual(b["schema"], "cloud.school.brief/2")
        self.assertEqual(b["materia"]["chiave"], "matematica")
        self.assertEqual([c["course_id"] for c in b["corsi_confermati"]], ["1001"])
        refs = [t["ref"] for t in b["compiti"]["attivi"] + b["compiti"]["recenti"]]
        self.assertTrue(refs and all(r.startswith("cls:1001/") and "@v" in r for r in refs))
        self.assertEqual(b["compiti"]["totale"], 3)
        self.assertTrue(b["regole"] and b["recupero_su_richiesta"])

    def test_no_history_no_internal_l3_no_hashes(self):
        txt = dump(self.brief())
        for bad in ("w-old", "a-old", "t-old", '"derivato"', "materia_proposte", "content_hash", '"raw"'):
            self.assertNotIn(bad, txt, bad)

    def test_contamination_other_subjects_absent(self):
        for s, foreign in (("matematica", ("cls:1002/", "cls:3001/")), ("storia", ("cls:1001/", "cls:3001/")),
                           ("italiano", ("cls:1001/", "cls:1002/"))):
            txt = dump(self.brief(s))
            for f in foreign:
                self.assertNotIn(f, txt, (s, f))

    def test_shared_course_marked(self):
        b = self.brief("italiano")
        self.assertEqual(b["corsi_confermati"][0]["condiviso_con"], ["latino"])
        self.assertEqual(b["compiti"]["attivi"][0]["condiviso_con"], ["latino"])
        refs = lambda s: [t["ref"] for t in self.brief(s)["compiti"]["attivi"] + self.brief(s)["compiti"]["recenti"]]
        self.assertEqual(refs("italiano"), refs("latino"))                 # stesso corso, stessi compiti
        self.assertEqual(self.brief("latino")["compiti"]["attivi"][0]["condiviso_con"], ["italiano"])

    def test_proposed_only_names_and_counts(self):
        b = self.brief(manual=None)                                    # nessuna conferma
        self.assertEqual(b["corsi_confermati"], [])
        self.assertEqual(b["compiti"]["totale"], 0)
        p = b["corsi_proposti"]
        self.assertEqual([c["course_id"] for c in p], ["1001"])
        self.assertEqual(set(p[0]), {"course_id", "nome", "materie_proposte", "compiti_operativi", "annunci_operativi",
                                     "materiali_operativi"})
        self.assertNotIn("cls:1001/w1", dump(b))                        # nessun contenuto dei proposti

    def test_proposed_without_operational_content_hidden(self):
        self.reg = make_registry(extra_courses=[{"id": "4001", "name": "2 Z Mate & Fisica (prova) 24/25", "courseState": "ARCHIVED"}],
                                 extra_works=[old_work("ow", cid="4001")], extra_subs=[old_sub("os", "ow", cid="4001")])
        b = self.brief(manual={"subjects": {"matematica": {"classroom_course_ids": ["1001"]}}})
        self.assertNotIn("4001", [c["course_id"] for c in b["corsi_proposti"]])
        self.assertEqual(b["corsi_proposti_nascosti_senza_contenuto_operativo"], 1)

    def test_caps_and_clipping(self):
        works = [dict(copy.deepcopy(F.WORK[0]), id="x%02d" % i, title="T%d" % i) for i in range(15)]
        subs = [{"courseId": "1001", "courseWorkId": "x%02d" % i, "id": "sx%02d" % i, "userId": "5001", "state": "TURNED_IN"}
                for i in range(15)]
        self.reg = make_registry(extra_works=works, extra_subs=subs)
        long_ann = {"courseId": "1001", "id": "a-long", "text": "parola " * 200, "state": "PUBLISHED",
                    "creationTime": "2026-10-31T12:00:00Z", "updateTime": "2026-10-31T12:00:00Z"}
        CL.sync_kind(self.reg, "announcement", lambda: [dict(a) for a in F.ANNOUNCEMENTS] + [long_ann], complete=False)
        b = self.brief()
        self.assertEqual(len(b["compiti"]["recenti"]), A.BRIEF["recent_tasks"])
        self.assertEqual(b["compiti"]["altri_non_mostrati"], b["compiti"]["totale"] - len(b["compiti"]["attivi"]) - 8)
        first = b["annunci"]["recenti"][0]
        self.assertEqual(first["ref"].split("@")[0], "cls:1001/a-long")
        self.assertLessEqual(len(first["testo"]), A.BRIEF["announcement_chars"] + 2)
        self.assertTrue(first["testo"].endswith("…"))

    def test_much_smaller_than_full_context(self):
        full = SC.build(self.reg, "matematica", SUBJECTS, NOW, manual=MANUAL)
        self.assertLess(len(dump(self.brief())), len(dump(full)))

    def test_deterministic_and_input_untouched(self):
        man = copy.deepcopy(MANUAL)
        self.assertEqual(dump(self.brief(manual=man)), dump(self.brief(manual=man)))
        self.assertEqual(man, MANUAL)

    def test_excluded_subjects_refused_everywhere(self):
        for s in SC.EXCLUDED_SUBJECTS:
            for fn, kw in ((A.brief, {}), (A.tasks, {}), (A.task, {"uid": "cls:1001/w1"}), (A.announcements, {}),
                           (A.materials, {})):
                with self.assertRaises(ValueError):
                    fn(self.reg, s, SUBJECTS, NOW, manual=MANUAL, **kw)

    def test_classeviva_compact(self):
        cv = self.brief()["classeviva"]
        self.assertEqual([e["uid"] for e in cv["verifiche_future"]], ["cvv:1"])
        self.assertEqual(cv["voti"][0]["voto"], "7")
        self.assertIn("cvv:5", [p["uid"] for p in cv["possibili_da_verificare"]])
        self.assertNotIn("cvv:4", dump(cv))                             # verifica di storia


class Retrieval(Base):
    def call(self, fn, s="matematica", **kw):
        return fn(self.reg, s, SUBJECTS, NOW, manual=MANUAL, **kw)

    def test_tasks_and_filter(self):
        out = self.call(A.tasks)
        self.assertEqual({t["ref"].split("@")[0] for t in out["compiti"]}, {"cls:1001/w1", "cls:1001/w2", "cls:1001/w3"})
        only = self.call(A.tasks, stato="consegnato")
        self.assertEqual([t["ref"].split("@")[0] for t in only["compiti"]], ["cls:1001/w1"])

    def test_task_detail_with_full_provenance(self):
        t = self.call(A.task, uid="cls:1001/w1")["compito"]
        self.assertEqual(t["descrizione"], "Fare 1-10")
        self.assertEqual(len(t["fonte"]["course_work"]["content_hash"]), 64)
        self.assertEqual(t["consegna"]["uid"], "cls:1001/s1")
        self.assertEqual(t["ref"], "cls:1001/w1@v1")
        self.assertEqual(self.call(A.task, uid="cls:1001/w1@v1")["compito"]["uid"], "cls:1001/w1")   # accetta il ref

    def test_task_refuses_other_subject_unconfirmed_and_history(self):
        for uid in ("cls:3001/iw1", "cls:1002/w9", "cls:1001/w-old", "cls:9999/x", ""):
            with self.assertRaises(ValueError):
                self.call(A.task, uid=uid)
        with self.assertRaises(ValueError):                             # proposto ma non confermato
            A.task(self.reg, "matematica", SUBJECTS, NOW, manual=None, uid="cls:1001/w1")

    def test_announcements_search_limit_and_isolation(self):
        out = self.call(A.announcements, cerca="VERIFICA")
        self.assertEqual([a["ref"].split("@")[0] for a in out["annunci"]], ["cls:1001/a2"])
        self.assertEqual(out["annunci"][0]["testo"], "Ricordo la verifica (SINT.)")
        lim = self.call(A.announcements, limite=1)
        self.assertEqual((len(lim["annunci"]), lim["totale"], lim["troncato"]), (1, 3, True))
        self.assertNotIn("a-old", dump(self.call(A.announcements)))
        self.assertEqual(self.call(A.announcements, s="italiano")["totale"], 0)
        for bad in (0, -1, "5"):
            with self.assertRaises(ValueError):
                self.call(A.announcements, limite=bad)
        self.assertEqual(len(self.call(A.announcements, limite=999)["annunci"]), 3)          # massimo applicato

    def test_retrieval_ignores_proposed_courses(self):
        man = {"subjects": {"storia": {"classroom_course_ids": ["1002"]}}}       # 1001 solo PROPOSTO per matematica
        for fn in (A.tasks, A.announcements, A.materials):
            out = fn(self.reg, "matematica", SUBJECTS, NOW, manual=man)
            self.assertEqual(out["totale"], 0, fn.__name__)
            self.assertNotIn("cls:1001/", dump(out), fn.__name__)

    def test_retrieval_max_enforced(self):
        with mock.patch.dict(A.RETRIEVAL_MAX, {"announcements": 2, "materials": 1}):
            self.assertEqual(len(self.call(A.announcements, limite=999)["annunci"]), 2)
            self.assertEqual(len(self.call(A.materials, limite=999)["materiali"]), 1)

    def test_materials_filters(self):
        out = self.call(A.materials)
        self.assertIn("cls:1001/m-new", [m["ref"].split("@")[0] for m in out["materiali"]])
        by_topic = self.call(A.materials, argomento="limiti")
        self.assertEqual([m["ref"].split("@")[0] for m in by_topic["materiali"]], ["cls:1001/m-new"])
        self.assertEqual(self.call(A.materials, cerca="nessuna-corrispondenza")["totale"], 0)
        self.assertNotIn("cls:1002/", dump(out))

    def test_retrieval_never_exposes_l3_internals_or_history(self):
        for fn, kw in ((A.tasks, {}), (A.announcements, {}), (A.materials, {})):
            txt = dump(self.call(fn, **kw))
            for bad in ('"derivato"', "materia_proposte", "w-old", "a-old"):
                self.assertNotIn(bad, txt, (fn.__name__, bad))

    def test_new_and_updated_assignment_reach_brief(self):
        works = [copy.deepcopy(w) for w in F.WORK]
        works[0]["title"] = "AGGIORNATO (SINT.)"
        works.append({"courseId": "1001", "id": "w-new", "title": "Nuovo (SINT.)", "state": "PUBLISHED",
                      "workType": "ASSIGNMENT", "creationTime": "2026-11-01T08:00:00Z", "updateTime": "2026-11-01T08:00:00Z",
                      "dueDate": {"year": 2026, "month": 11, "day": 9}, "dueTime": {"hours": 21, "minutes": 59}})
        CL.sync_kind(self.reg, "course_work", lambda: works, complete=False)
        CL.sync_kind(self.reg, "submission", lambda: [{"courseId": "1001", "courseWorkId": "w-new", "id": "s-new",
                                                       "userId": "5001", "state": "CREATED"}], complete=False)
        b = self.brief()
        self.assertEqual(b["compiti"]["attivi"][0]["ref"], "cls:1001/w-new@v1")
        self.assertEqual(b["compiti"]["attivi"][0]["consegna"], "CREATED")
        self.assertIn("cls:1001/w1@v2", [t["ref"] for t in b["compiti"]["recenti"]])
        self.assertNotIn("w-new", dump(self.brief("storia")) + dump(self.brief("italiano")))


class ServerTools(unittest.TestCase):
    TOOLS = ("school_brief", "school_tasks", "school_task", "school_announcements", "school_materials")

    def setUp(self):
        import server
        self.server = server
        self.d = tempfile.mkdtemp()
        self.db = os.path.join(self.d, "registry.sqlite3")
        reg = R.Registry(self.db)
        CL.sync_kind(reg, "course", lambda: [dict(F.COURSE_A), dict(F.COURSE_B)])
        CL.sync_kind(reg, "course_work", lambda: [dict(w) for w in F.WORK])
        CL.sync_kind(reg, "submission", lambda: [dict(s) for s in F.SUBMISSIONS])
        reg.close()
        c = sqlite3.connect(self.db); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.close()
        self.manual = os.path.join(self.d, "manual", "school_manual.json")
        for p in (mock.patch.dict(os.environ, {"CLOUD_SCHOOL_DB": self.db}),
                  mock.patch.object(server, "subjects", lambda: SUBJECTS),
                  mock.patch.object(SC, "manual_default_path", lambda: self.manual)):
            p.start(); self.addCleanup(p.stop)

    def test_tools_registered_read_only_with_subject(self):
        for name in self.TOOLS:
            d = self.server.srv._tools[name]["def"]
            self.assertIn("subject", d["inputSchema"]["required"], name)
            self.assertIn("sola lettura", d["description"], name)
        self.assertIn("DIAGNOSTICA", self.server.srv._tools["school_context"]["def"]["description"])

    def test_no_manual_means_empty_but_valid_and_nothing_written(self):
        before = sorted(os.listdir(self.d))
        b = self.server.school_brief("matematica")
        self.assertEqual(b["corsi_confermati"], [])
        self.assertEqual([c["course_id"] for c in b["corsi_proposti"]], ["1001"])
        self.assertEqual(sorted(os.listdir(self.d)), before)
        self.assertFalse(os.path.exists(self.manual))

    def test_with_manual_tools_answer_and_isolate(self):
        os.makedirs(os.path.dirname(self.manual))
        with open(self.manual, "w") as fh:
            json.dump({"subjects": {"matematica": {"classroom_course_ids": ["1001"]}}}, fh)
        with open(self.db, "rb") as fh:
            h = fh.read()
        self.assertEqual(self.server.school_brief("matematica")["corsi_confermati"][0]["course_id"], "1001")
        self.assertEqual(self.server.school_task("matematica", "cls:1001/w1")["compito"]["uid"], "cls:1001/w1")
        with self.assertRaises(ValueError):
            self.server.school_task("storia", "cls:1001/w1")
        self.assertEqual(self.server.school_tasks("matematica", stato="consegnato")["totale"], 1)
        self.assertIn("annunci", self.server.school_announcements("matematica"))
        self.assertIn("materiali", self.server.school_materials("matematica"))
        with self.assertRaises(ValueError):
            self.server.school_brief("religione")
        with open(self.db, "rb") as fh:
            self.assertEqual(fh.read(), h)                            # registro identico


if __name__ == "__main__":
    unittest.main()
