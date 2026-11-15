"""Kind 'lesson' (Fase 1): UID, idempotenza, aggiornamento dell'argomento, anomalie. Valori SINTETICI; campi come da API."""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cvv_client as cv
import cvv_registry as CR
import cvv_dryrun as D
import cvv_sync as S
import registry as R

L1 = {"evtId": 9001, "evtDate": "2026-10-29", "evtCode": "LSF0", "evtHPos": 2, "evtDuration": 1, "classDesc": "4A",
      "authorName": "ROSSI MARIO", "subjectId": 11, "subjectCode": "", "subjectDesc": "FISICA",
      "lessonType": "Lezione", "lessonArg": "Calore e temperatura"}
L2 = dict(L1, evtId=9002, evtHPos=3, subjectDesc="MATEMATICA", lessonArg="Equazioni")
L3 = dict(L1, evtId=9003, evtDate="2026-10-30", evtHPos=1, lessonArg="")           # argomento non ancora compilato
WIN = ("2026-09-29", "2026-11-01")


def fresh():
    return R.Registry(":memory:")


def sync(reg, raws, **kw):
    return CR.sync_kind(reg, "lesson", lambda: [dict(r) for r in raws], window=WIN, **kw)


class Uid(unittest.TestCase):
    def test_uid_from_evtid(self):
        n = CR.normalized("lesson", L1)
        self.assertEqual(n["uid"], "cvv:lesson:9001")
        self.assertEqual(CR.source_uid_of(n["uid"]), "lesson:9001")
        self.assertEqual(CR.legacy_uid_of("lesson:9001"), "cvv:lesson:9001")

    def test_no_collision_with_agenda_ids(self):
        self.assertNotEqual(CR.normalized("lesson", dict(L1, evtId=777))["uid"],
                            CR.normalized("agenda_event", {"evtId": 777, "notes": "", "evtCode": "AGNT"})["uid"])

    def test_fallback_key_excludes_topic(self):
        a = {k: v for k, v in L1.items() if k != "evtId"}
        b = dict(a, lessonArg="Altro argomento")
        ua, ub = CR.normalized("lesson", a)["uid"], CR.normalized("lesson", b)["uid"]
        self.assertTrue(ua.startswith("sec:lesson:"))
        self.assertEqual(ua, ub)
        self.assertNotEqual(CR.normalized("lesson", a)["hash"], CR.normalized("lesson", b)["hash"])
        self.assertNotEqual(ua, CR.normalized("lesson", dict(a, evtHPos=5))["uid"])


class Registro(unittest.TestCase):
    def test_create_then_idempotent(self):
        reg = fresh()
        r1 = sync(reg, [L1, L2, L3])
        self.assertEqual(r1["counts"]["create"], 3)
        r2 = sync(reg, [L1, L2, L3])
        self.assertEqual((r2["counts"]["unchanged"], r2["counts"]["create"], r2["counts"]["update"]), (3, 0, 0))
        self.assertEqual(reg.verify_integrity(), [])

    def test_raw_exact(self):
        reg = fresh(); sync(reg, [dict(L1, campoNuovo="x")])
        self.assertEqual(reg.get_record(CR.SOURCE, "lesson", "lesson:9001")["raw"], dict(L1, campoNuovo="x"))

    def test_late_topic_is_update_not_duplicate(self):
        reg = fresh(); sync(reg, [L1, L3])
        res = sync(reg, [L1, dict(L3, lessonArg="Principio zero")])
        self.assertEqual((res["counts"]["update"], res["counts"]["create"]), (1, 0))
        self.assertEqual([v["version"] for v in reg.get_versions(CR.SOURCE, "lesson", "lesson:9003")], [1, 2])
        self.assertEqual(len(reg.list_records(kind="lesson")), 3 - 1)

    def test_topic_late_with_fallback_key(self):
        a, b = [{k: v for k, v in x.items() if k != "evtId"} for x in (L3, dict(L3, lessonArg="Nuovo"))]
        reg = fresh(); sync(reg, [a]); res = sync(reg, [b])
        self.assertEqual((res["counts"]["update"], res["counts"]["create"]), (1, 0))

    def test_duplicate_evtid_in_one_read_is_refused(self):
        reg = fresh()
        res = sync(reg, [L1, dict(L1, lessonArg="doppione")])
        self.assertEqual(res["status"], "error")
        self.assertEqual(reg.list_records(kind="lesson"), [])

    def test_vanished_lesson_is_missing_not_deleted(self):
        reg = fresh(); sync(reg, [L1, L2, L3, dict(L1, evtId=9004, evtHPos=4)])
        res = sync(reg, [L1, L2, L3])
        self.assertEqual(res["counts"]["missing"], 1)
        self.assertEqual(len(reg.list_records(kind="lesson")), 4)

    def test_not_in_default_kinds(self):
        self.assertNotIn("lesson", D.ALL_KINDS)
        self.assertIn("lesson", CR.KINDS)


class DryRunESync(unittest.TestCase):
    def test_dryrun_lesson_only(self):
        res = D.run({"lesson": (lambda: [dict(L1), dict(L2), dict(L3)], WIN)}, kinds=["lesson"], simulate=True,
                    baseline_path=os.path.join(tempfile.mkdtemp(), "nope.sqlite3"))
        e = res["RISULTATO_REALE"]["letture"]["lesson"]
        self.assertEqual(e["conteggi"]["create"], 3)
        self.assertEqual(res["RISULTATO_REALE"]["integrita"], "OK")
        self.assertFalse(res["verifica_nessuna_scrittura"]["file_reale_o_ausiliari_creati_o_modificati"])
        self.assertTrue(res["SIMULAZIONE_NON_ACCADUTA"]["per_kind"]["lesson"]["eseguita"])
        self.assertIn("argomento non (ancora) compilato", e["da_verificare"])

    def test_default_kinds_unchanged_even_if_lesson_fetcher_present(self):
        f = {"lesson": (lambda: [dict(L1)], WIN), "notice": (lambda: [], None)}
        res = D.run(f, simulate=False, baseline_path=os.path.join(tempfile.mkdtemp(), "x.sqlite3"))
        self.assertNotIn("lesson", res["RISULTATO_REALE"]["letture"])

    def test_commit_twice_is_idempotent(self):
        db = os.path.join(tempfile.mkdtemp(), "school", "registry.sqlite3")
        f = {"lesson": (lambda: [dict(L1), dict(L2), dict(L3)], WIN)}
        r1 = S.run(f, kinds=["lesson"], commit=True, expect_create=3, db_path=db)
        self.assertEqual(r1["esito"], "SCRITTO E VERIFICATO")
        r2 = S.run(f, kinds=["lesson"], commit=True, expect_create=0, db_path=db)
        self.assertEqual(r2["riepilogo"]["invariati"], 3)
        self.assertEqual(r2["riepilogo"]["nuove_versioni_raw"], 0)
        r3 = S.run({"lesson": (lambda: [dict(L1), dict(L2), dict(L3, lessonArg="X")], WIN)}, kinds=["lesson"],
                   commit=True, expect_create=0, db_path=db)
        self.assertEqual(r3["riepilogo"]["record_da_aggiornare"], 1)
        self.assertIsNotNone(r3["backup_prima_della_scrittura"])


if __name__ == "__main__":
    unittest.main()
