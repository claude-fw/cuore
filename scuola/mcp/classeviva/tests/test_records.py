"""Voti e comunicazioni: record grezzi completi, hash, chiavi di riserva, nessuna interpretazione inventata."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cvv_client as cv

G = {"evtId": 1, "subjectDesc": "MATEMATICA", "evtDate": "2026-11-17", "displayValue": "7+", "decimalValue": 7.25,
     "componentDesc": "Scritto", "periodDesc": "Trimestre", "weightFactor": None, "canceled": False, "notesForFamily": ""}


class Voti(unittest.TestCase):
    def test_raw_is_exact_and_complete(self):
        g = cv.normalize_grade(dict(G, campoNuovo=[1, 2]))
        self.assertEqual(g["raw"], dict(G, campoNuovo=[1, 2]))          # nessun campo scartato o convertito
        self.assertEqual(g["uid"], "cvv:1")
        self.assertIsNone(g["raw"]["weightFactor"])                      # il peso non viene inventato
        self.assertNotIn("peso", str(g["interpreted"]))

    def test_unknown_component_is_to_verify(self):
        g = cv.normalize_grade(G)
        self.assertEqual(g["interpreted"]["tipo_valutazione"], "Da verificare")
        self.assertEqual(len(g["interpreted"]["da_verificare"]), 1)
        self.assertEqual(g["interpreted"]["materia_chiave"], "matematica")
        old = dict(cv.GRADE_TYPE_MAP)
        try:
            cv.GRADE_TYPE_MAP["Scritto"] = "Scritto"                    # tabella verificata (ipotetica, solo nel test)
            self.assertEqual(cv.normalize_grade(G)["interpreted"]["tipo_valutazione"], "Scritto")
        finally:
            cv.GRADE_TYPE_MAP.clear(); cv.GRADE_TYPE_MAP.update(old)

    def test_hash_changes_with_any_field_and_not_with_order(self):
        h = cv.normalize_grade(G)["hash"]
        self.assertEqual(h, cv.normalize_grade(dict(reversed(list(G.items()))))["hash"])
        for k, v in (("displayValue", "8"), ("canceled", True), ("notesForFamily", "x"), ("campoNuovo", 1)):
            self.assertNotEqual(h, cv.normalize_grade(dict(G, **{k: v}))["hash"], k)

    def test_fallback_key_without_id_is_stable(self):
        a = {k: v for k, v in G.items() if k != "evtId"}
        self.assertEqual(cv.normalize_grade(a)["uid"], cv.normalize_grade(dict(a))["uid"])
        self.assertTrue(cv.normalize_grade(a)["uid"].startswith("sec:"))

    def test_partial_record_does_not_crash(self):
        g = cv.normalize_grade({"evtId": 9})
        self.assertEqual((g["uid"], g["date"]), ("cvv:9", None))
        self.assertTrue(g["derived"]["nome"])


class Bacheca(unittest.TestCase):
    N = {"pubId": 10000001, "cntTitle": "Circolare n. 99 Avviso di prova", "pubDT": "2026-10-29T10:15:00+02:00", "cntCategory": ""}

    def test_notice_fields(self):
        n = cv.normalize_notice(self.N)
        self.assertEqual((n["uid"], n["date"], n["derived"]["nome"]), ("cvv:10000001", "2026-10-29", "Circolare n. 99 Avviso di prova"))
        self.assertEqual(n["interpreted"]["categoria"], "Da classificare")      # categoria mai inventata
        self.assertEqual(n["raw"], self.N)

    def test_given_category_is_kept(self):
        n = cv.normalize_notice(dict(self.N, cntCategory="Circolari"))
        self.assertEqual((n["interpreted"]["categoria"], n["interpreted"]["da_verificare"]), ("Circolari", []))

    def test_hash_and_versions(self):
        h = cv.normalize_notice(self.N)["hash"]
        self.assertNotEqual(h, cv.normalize_notice(dict(self.N, cntTitle="altro"))["hash"])
        self.assertRegex(cv.normalize_notice(self.N)["interp_version"], r"^r\d+-[0-9a-f]{6}$")


if __name__ == "__main__":
    unittest.main()
