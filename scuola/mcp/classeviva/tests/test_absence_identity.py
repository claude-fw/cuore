"""Assenze ClasseViva RI-EMESSE con un evtId nuovo dopo la giustificazione (verificato sui dati reali: alcune assenze,
stessa data/codice/ora, cambiati solo evtId, isJustified, justifReasonCode/Desc). Prima: nuove + scomparse (doppioni,
stati obsoleti). Ora: identita' riconciliata in modo deterministico, ambigui mai fusi, nessun doppione.
I record sono FITTIZI con la forma reale dei campi (nessun dato reale)."""
import copy
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "..", "_common"))
import cvv_dryrun as D      # noqa: E402
import cvv_registry as CR   # noqa: E402
import cvv_sync as S        # noqa: E402
import registry as R        # noqa: E402

K = "absence"


def ab(eid, date, code="ABR0", hpos=1, justified=False, reason=None, desc=None, web=0):
    """Assenza con i campi reali di ClasseViva (valori fittizi)."""
    return {"evtId": eid, "evtCode": code, "evtDate": date, "evtHPos": hpos, "evtValue": None if code == "ABA0" else 1,
            "isJustified": justified, "justifReasonCode": reason, "justifReasonDesc": desc, "hoursAbsence": [],
            "webJustifStatus": web}


def justified(raw, new_id, reason="C"):
    """La stessa assenza come la restituisce ClasseViva dopo la giustificazione: evtId nuovo, giustificata."""
    r = dict(raw, evtId=new_id, isJustified=True, justifReasonCode=reason, justifReasonDesc="Motivo %s" % reason)
    return r


OLD = [ab(100, "2026-10-12"), ab(101, "2026-10-14"), ab(102, "2026-10-27", code="ABA0", hpos=None), ab(103, "2026-10-29")]


class Base(unittest.TestCase):
    def setUp(self):
        self.reg = R.Registry(":memory:")
        self.addCleanup(self.reg.close)

    def sync(self, raws):
        return CR.sync_kind(self.reg, K, lambda: copy.deepcopy(raws))

    def uids(self, status=None):
        return sorted(r["source_uid"] for r in self.reg.list_records(source=CR.SOURCE, kind=K, status=status))


class RiemissioneDopoGiustificazione(Base):
    def test_bug_reproduced_without_reconciliation(self):
        """Il vecchio percorso (identita' = evtId) produce doppioni e scomparsi: e' il bug osservato."""
        self.reg.ingest(CR.SOURCE, K, CR.to_items(K, OLD))
        new = [justified(OLD[0], 200), justified(OLD[1], 201)] + OLD[2:]
        res = self.reg.ingest(CR.SOURCE, K, CR.to_items(K, new))
        self.assertEqual((res["counts"]["create"], res["counts"]["missing"]), (2, 2))

    def test_changed_id_after_justification_is_reconciled(self):
        self.sync(OLD)
        new = [justified(OLD[0], 200), justified(OLD[1], 201), justified(OLD[2], 202, "A")] + OLD[3:]
        res = self.sync(new)
        c = res["counts"]
        self.assertEqual((c["create"], c["update"], c["missing"], c["unchanged"]), (0, 3, 0, 1))
        self.assertEqual(self.uids(), ["100", "101", "102", "103"])                 # nessun doppione
        self.assertEqual(self.uids(R.PRESENT), ["100", "101", "102", "103"])        # nessuno scomparso
        rec = self.reg.get_record(CR.SOURCE, K, "100")
        self.assertEqual((rec["legacy_uid"], rec["raw"]["evtId"], rec["raw"]["isJustified"], rec["version"]),
                         ("cvv:100", 200, True, 2))
        ev = [e for e in self.reg.events(source=CR.SOURCE, kind=K, source_uid="100") if e["event"] == "reidentified"]
        self.assertEqual(len(ev), 1)
        prov = json.loads(ev[0]["detail_json"])
        self.assertEqual((prov["regola"], prov["evtId_precedente"], prov["evtId_nuovo"], prov["versione_precedente"]),
                         ("assenza_rinumerata", 100, 200, 1))
        self.assertEqual(prov["campi_cambiati"], ["isJustified", "justifReasonCode", "justifReasonDesc"])
        self.assertEqual(sorted(r["record"] for r in res["riconciliazione"]["riconciliate"]), ["cvv:100", "cvv:101", "cvv:102"])
        self.assertTrue(any("ri-emesse" in w for w in res["warnings"]))
        self.assertEqual(self.reg.verify_integrity(), [])
        self.assertEqual([v["raw"]["evtId"] for v in self.reg.get_versions(CR.SOURCE, K, "100")], [100, 200])  # storia intatta

    def test_repeated_sync_creates_nothing_new(self):
        self.sync(OLD)
        new = [justified(r, r["evtId"] + 100) for r in OLD]
        self.sync(new)
        n_events = len(self.reg.events())
        for _ in range(3):
            res = self.sync(new)
            c = res["counts"]
            self.assertEqual((c["create"], c["update"], c["missing"], c["unchanged"]), (0, 0, 0, 4))
            self.assertNotIn("riconciliazione", res)
        self.assertEqual(len(self.reg.events()), n_events)                         # nessun evento aggiunto
        self.assertEqual(self.uids(), ["100", "101", "102", "103"])

    def test_status_update_with_same_id_is_a_plain_update(self):
        self.sync(OLD)
        res = self.sync([justified(OLD[0], 100)] + OLD[1:])                         # stesso evtId, ora giustificata
        self.assertEqual((res["counts"]["update"], res["counts"]["create"]), (1, 0))
        self.assertNotIn("riconciliazione", res)
        self.assertEqual(self.reg.events(source_uid="100")[-1]["event"], "update")

    def test_true_new_absence_is_created(self):
        self.sync(OLD)
        res = self.sync(OLD + [ab(300, "2026-11-03")])
        self.assertEqual((res["counts"]["create"], res["counts"]["update"]), (1, 0))
        self.assertIn("300", self.uids(R.PRESENT))
        self.assertNotIn("riconciliazione", res)

    def test_new_absence_same_key_while_old_still_present_is_new(self):
        """Se il vecchio evtId e' ancora restituito, uno nuovo con la stessa chiave NON e' una rinumerazione."""
        self.sync(OLD)
        res = self.sync(OLD + [ab(300, "2026-10-12")])
        self.assertEqual(res["counts"]["create"], 1)
        self.assertNotIn("riconciliazione", res)

    def test_two_similar_absences_same_day_reconciled_separately(self):
        day = [ab(110, "2026-10-22", hpos=1), ab(111, "2026-10-22", code="ABU0", hpos=5)]
        self.sync(day)
        res = self.sync([justified(day[0], 210), justified(day[1], 211)])
        self.assertEqual((res["counts"]["create"], res["counts"]["update"], res["counts"]["missing"]), (0, 2, 0))
        self.assertEqual(self.reg.get_record(CR.SOURCE, K, "110")["raw"]["evtId"], 210)
        self.assertEqual(self.reg.get_record(CR.SOURCE, K, "111")["raw"]["evtId"], 211)

    def test_true_absence_and_reissue_on_other_day_together(self):
        self.sync(OLD)
        res = self.sync([justified(OLD[0], 200)] + OLD[1:] + [ab(300, "2026-11-05")])
        self.assertEqual((res["counts"]["create"], res["counts"]["update"], res["counts"]["missing"]), (1, 1, 0))


class Ambigui(Base):
    def test_two_registered_candidates_same_key_are_never_merged(self):
        same = [ab(120, "2026-10-23"), ab(121, "2026-10-23")]           # stessa chiave nel registro (anomalia della fonte)
        self.sync(same)
        res = self.sync([justified(same[0], 220)])
        self.assertEqual((res["counts"]["create"], res["counts"]["update"], res["counts"]["missing"]), (0, 0, 0))
        self.assertEqual(self.uids(R.PRESENT), ["120", "121"])           # nessuno marcato scomparso
        self.assertIsNone(self.reg.get_record(CR.SOURCE, K, "220"))      # nessun doppione creato
        amb = res["riconciliazione"]["ambigue"]
        self.assertEqual((len(amb), amb[0]["candidati"]), (1, ["cvv:120", "cvv:121"]))
        self.assertTrue(any("AMBIGUA" in w for w in res["warnings"]))

    def test_two_read_absences_same_key_are_never_merged(self):
        self.sync([ab(130, "2026-10-24")])
        res = self.sync([justified(ab(130, "2026-10-24"), 230), justified(ab(130, "2026-10-24"), 231)])
        self.assertEqual((res["counts"]["create"], res["counts"]["update"], res["counts"]["missing"]), (0, 0, 0))
        self.assertEqual(self.uids(), ["130"])
        self.assertEqual(self.reg.get_record(CR.SOURCE, K, "130")["source_status"], R.PRESENT)
        self.assertEqual(len(res["riconciliazione"]["ambigue"]), 2)

    def test_non_increasing_new_id_is_ambiguous(self):
        self.sync([ab(140, "2026-10-25")])
        res = self.sync([justified(ab(140, "2026-10-25"), 90)])
        self.assertEqual((res["counts"]["create"], res["counts"]["update"]), (0, 0))
        self.assertIn("non successivo", res["riconciliazione"]["ambigue"][0]["motivo"])
        self.assertEqual(self.reg.get_record(CR.SOURCE, K, "140")["source_status"], R.PRESENT)

    def test_incomplete_absence_is_not_registered(self):
        self.sync(OLD)
        res = self.sync(OLD + [dict(ab(301, "2026-10-26"), evtDate=None)])
        self.assertEqual(res["counts"]["create"], 0)
        self.assertEqual(len(res["riconciliazione"]["incomplete"]), 1)
        self.assertIsNone(self.reg.get_record(CR.SOURCE, K, "301"))

    def test_ambiguity_resolved_later_is_reconciled_without_duplicates(self):
        self.sync([ab(150, "2026-10-27")])
        self.sync([justified(ab(150, "2026-10-27"), 250), justified(ab(150, "2026-10-27"), 251)])   # ambiguo: nulla
        res = self.sync([justified(ab(150, "2026-10-27"), 250)])                                    # poi univoco
        self.assertEqual((res["counts"]["create"], res["counts"]["update"]), (0, 1))
        self.assertEqual(self.uids(), ["150"])


class IdentitaNelRegistry(Base):
    def test_key_is_stable_and_missing_status_is_recorded(self):
        self.sync(OLD)
        self.sync([justified(OLD[0], 200)] + OLD[1:3])               # OLD[3] davvero sparito (nessun candidato)
        self.assertEqual(self.uids(), ["100", "101", "102", "103"])   # stessa chiave, nessun record nuovo
        self.assertEqual(self.uids(status=R.MISSING), ["103"])         # prima restava 'present'
        self.assertEqual(self.uids(status=R.PRESENT), ["100", "101", "102"])


class VerificaESincronizzazioneReale(unittest.TestCase):
    """cvv_sync su un registro TEMPORANEO (mai quello reale): la verifica post-scrittura usa la stessa identita'."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "Cloud", "school", "registry.sqlite3")

    def run_sync(self, raws, **kw):
        return S.run({K: (lambda: copy.deepcopy(raws), None)}, kinds=[K], db_path=self.path, **kw)

    def test_dry_run_and_commit_reconcile_and_verify(self):
        self.assertEqual(self.run_sync(OLD, commit=True, expect_create=4)["esito"], "SCRITTO E VERIFICATO")
        new = [justified(r, r["evtId"] + 100) for r in OLD]
        dry = D.run({K: (lambda: copy.deepcopy(new), None)}, kinds=[K], simulate=False, baseline_path=self.path)
        lett = dry["RISULTATO_REALE"]["letture"][K]
        self.assertEqual((lett["conteggi"]["create"], lett["conteggi"]["update"], lett["conteggi"]["missing"]), (0, 4, 0))
        self.assertEqual(len(lett["riconciliazione"]["riconciliate"]), 4)
        self.assertTrue(dry["verifica_nessuna_scrittura"]["stato_prima_uguale_a_dopo"])
        out = self.run_sync(new, commit=True, expect_create=0)
        self.assertEqual(out["esito"], "SCRITTO E VERIFICATO", json.dumps(out.get("verifica_post_scrittura"), indent=1))
        self.assertTrue(out["verifica_post_scrittura"]["tutto_ok"])
        self.assertEqual(out["verifica_post_scrittura"]["stato_registro"]["totale_record"], 4)
        again = self.run_sync(new, commit=True, expect_create=0)
        self.assertEqual(again["esito"], "SCRITTO E VERIFICATO")
        self.assertEqual(again["esito_scrittura"][K]["conteggi"], {"unchanged": 4})


if __name__ == "__main__":
    unittest.main()
