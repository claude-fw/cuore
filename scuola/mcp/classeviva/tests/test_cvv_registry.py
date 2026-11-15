"""Adattatore ClasseViva -> registro: ID cvv:<id> compatibili, RAW completo, volatili, proiezione a livelli.
I valori sono SINTETICI; la struttura dei campi e' quella osservata sui dati reali."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cvv_client as cv
import cvv_registry as CR
import registry as R

NOTICE = {"pubId": 111, "pubDT": "2026-10-29T12:00:00+02:00", "readStatus": False, "evtCode": "CF", "cntId": 9,
          "cntValidFrom": "2026-10-29", "cntValidTo": "2027-06-01", "cntValidInRange": True, "cntStatus": "active",
          "cntTitle": "Circolare di prova", "cntNum": "1", "cntCategory": "", "cntHasChanged": False,
          "cntHasAttach": True, "needJoin": False, "needReply": False, "needFile": False, "needSign": False,
          "evento_id": "9", "dinsert_allegato": "2026-10-29 12:00:00",
          "attachments": [{"fileName": "a.pdf", "attachNum": 1}]}
ABSENCE = {"evtId": 555, "evtCode": "ABR0", "evtDate": "2026-10-14", "evtHPos": 2, "evtValue": 1, "hoursAbsence": [],
           "isJustified": False, "justifReasonCode": None, "justifReasonDesc": None, "webJustifStatus": 0,
           "campoExtraNonPrevisto": "x"}
EVENT = {"evtId": 777, "evtCode": "AGNT", "subjectDesc": "STORIA", "evtDatetimeBegin": "2026-10-31T08:00:00+02:00",
         "evtDatetimeEnd": "2026-10-31T09:00:00+02:00", "isFullDay": False, "notes": "Verifica di prova",
         "authorName": "ROSSI MARIO", "classDesc": "3A"}


def fresh():
    return R.Registry(":memory:")


class IdCompatibili(unittest.TestCase):
    def test_cvv_ids_are_preserved(self):
        self.assertEqual(CR.source_uid_of("cvv:555"), "555")
        self.assertEqual(CR.legacy_uid_of("555"), "cvv:555")
        self.assertEqual(CR.legacy_uid_of("sec:abc"), "sec:abc")
        for kind, raw, expected in (("absence", ABSENCE, "cvv:555"), ("notice", NOTICE, "cvv:111"),
                                    ("agenda_event", EVENT, "cvv:777")):
            reg = fresh()
            CR.sync_kind(reg, kind, lambda raw=raw: [raw])
            rec = reg.find_by_legacy_uid(CR.SOURCE, kind, expected)
            self.assertIsNotNone(rec, kind)
            self.assertEqual(rec["legacy_uid"], CR.normalized(kind, raw)["uid"])    # identico al normalizzatore esistente

    def test_fallback_key_without_id(self):
        raw = dict(ABSENCE); del raw["evtId"]
        reg = fresh()
        CR.sync_kind(reg, "absence", lambda: [raw])
        rec = reg.list_records()[0]
        self.assertTrue(rec["source_uid"].startswith("sec:"))
        self.assertEqual(rec["legacy_uid"], rec["source_uid"])


class RawCompleto(unittest.TestCase):
    def test_absence_keeps_fields_the_normalizer_drops(self):
        reg = fresh()
        CR.sync_kind(reg, "absence", lambda: [ABSENCE])
        rec = reg.get_record(CR.SOURCE, "absence", "555")
        self.assertEqual(rec["raw"], ABSENCE)                                       # incluso il campo non previsto
        self.assertNotIn("campoExtraNonPrevisto", CR.normalized("absence", ABSENCE)["raw"])   # il normalizzato lo scarta: il registro no

    def test_all_kinds_roundtrip_exact(self):
        for kind, raw in (("notice", NOTICE), ("absence", ABSENCE), ("agenda_event", EVENT)):
            reg = fresh()
            CR.sync_kind(reg, kind, lambda raw=raw: [raw])
            self.assertEqual(reg.list_records(kind=kind)[0]["current_version"], 1)
            self.assertEqual(reg.get_record(CR.SOURCE, kind, CR.source_uid_of(CR.normalized(kind, raw)["uid"]))["raw"], raw)


class Volatili(unittest.TestCase):
    def test_reading_a_notice_is_state_not_a_new_version(self):
        reg = fresh()
        CR.sync_kind(reg, "notice", lambda: [NOTICE])
        res = CR.sync_kind(reg, "notice", lambda: [dict(NOTICE, readStatus=True)])
        self.assertEqual((res["counts"]["state_change"], res["counts"]["update"]), (1, 0))
        rec = reg.get_record(CR.SOURCE, "notice", "111")
        self.assertEqual((rec["version"], rec["volatile"]), (1, {"readStatus": True}))

    def test_title_change_is_a_real_update(self):
        reg = fresh()
        CR.sync_kind(reg, "notice", lambda: [NOTICE])
        res = CR.sync_kind(reg, "notice", lambda: [dict(NOTICE, cntTitle="Circolare rettificata")])
        self.assertEqual(res["counts"]["update"], 1)
        self.assertEqual(len(reg.get_versions(CR.SOURCE, "notice", "111")), 2)


class Livelli(unittest.TestCase):
    def test_layers_are_separated(self):
        reg = fresh()
        CR.sync_kind(reg, "absence", lambda: [ABSENCE])
        rec = reg.get_record(CR.SOURCE, "absence", "555")
        n = CR.normalized("absence", rec["raw"])
        self.assertEqual(rec["legacy_uid"], "cvv:555")
        self.assertEqual(rec["raw"], ABSENCE)                                        # RAW esatto
        self.assertEqual(n["interpreted"]["tipo"], "Entrata in ritardo")
        self.assertEqual(n["interp_version"], cv.absence_interp_version())
        self.assertEqual(len(rec["content_hash"]), 64)


class ErroriDiFonte(unittest.TestCase):
    def test_fetch_error_marks_nothing_missing(self):
        reg = fresh()
        CR.sync_kind(reg, "notice", lambda: [NOTICE])
        def down():
            raise TimeoutError("ClasseViva non risponde")
        res = CR.sync_kind(reg, "notice", down)
        self.assertEqual(res["status"], "error")
        self.assertEqual(reg.get_record(CR.SOURCE, "notice", "111")["source_status"], "present")

    def test_unparseable_payload_is_an_error_run_not_a_crash(self):
        reg = fresh()
        res = CR.sync_kind(reg, "absence", lambda: ["non un oggetto"])
        self.assertEqual(res["status"], "error")
        self.assertEqual(reg.list_records(), [])

    def test_one_kind_failing_does_not_block_the_other(self):
        reg = fresh()
        def down():
            raise ConnectionError("x")
        a = CR.sync_kind(reg, "grade", down)
        b = CR.sync_kind(reg, "notice", lambda: [NOTICE])
        self.assertEqual((a["status"], b["status"]), ("error", "ok"))
        self.assertEqual(len(reg.list_records(kind="notice")), 1)

    def test_zero_grades_is_a_valid_empty_state(self):
        reg = fresh()
        res = CR.sync_kind(reg, "grade", lambda: [])
        self.assertEqual((res["status"], res["counts"]["create"]), ("ok", 0))        # nessun voto inventato
        self.assertEqual(reg.list_records(kind="grade"), [])


if __name__ == "__main__":
    unittest.main()
