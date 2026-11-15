"""Test della tabella di corrispondenza delle assenze (interpretazioni VERIFICATE sulla UI web)."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cvv_client as cv


def ev(**kw):
    base = {"evtId": 1, "evtCode": "ABR0", "evtDate": "2026-09-10", "evtHPos": 1, "evtValue": None, "hoursAbsence": [],
            "isJustified": False, "justifReasonCode": None, "justifReasonDesc": None, "webJustifStatus": 1}
    base.update(kw)
    return base


class T(unittest.TestCase):
    def test_verified_mappings(self):
        a = cv.normalize_absence(ev())
        self.assertEqual((a["interpreted"]["tipo"], a["interpreted"]["stato_giustificazione"]), ("Entrata in ritardo", "In approvazione"))
        self.assertEqual(a["derived"]["nome"], "Entrata in ritardo \u00b7 10/09/2026 \u00b7 1\u00aa ora")
        b = cv.normalize_absence(ev(evtCode="ABA0", evtHPos=None, evtDate="2026-10-27", webJustifStatus=0))
        self.assertEqual((b["interpreted"]["tipo"], b["interpreted"]["stato_giustificazione"]), ("Assenza", "Da giustificare"))
        self.assertEqual(b["derived"]["nome"], "Assenza \u00b7 27/10/2026")
        self.assertEqual(b["interpreted"]["da_verificare"], [])
        self.assertIn("UI web", b["interpreted"]["fonte_interpretazione"])

    def test_unknown_code_and_status_stay_to_verify(self):
        a = cv.normalize_absence(ev(evtCode="ABU0", webJustifStatus=2))
        self.assertEqual(a["raw"]["evtCode"], "ABU0")                      # codice grezzo conservato
        self.assertEqual((a["interpreted"]["tipo"], a["interpreted"]["stato_giustificazione"]), ("Da verificare", "Da verificare"))
        self.assertEqual(len(a["interpreted"]["da_verificare"]), 2)
        self.assertIn("da verificare", a["derived"]["nome"])
        for bad in (None, True, "1", 1.5):                                  # True non deve valere come 1
            self.assertEqual(cv.normalize_absence(ev(webJustifStatus=bad))["interpreted"]["stato_giustificazione"], "Da verificare")

    def test_hours_lost_only_for_delays_with_numeric_value(self):
        self.assertEqual(cv.normalize_absence(ev(evtValue=1))["interpreted"]["ore_perse"], 1)
        self.assertIsNone(cv.normalize_absence(ev(evtValue=None))["interpreted"]["ore_perse"])   # null resta vuoto, non 0
        x = cv.normalize_absence(ev(evtCode="ABA0", evtValue=1))
        self.assertIsNone(x["interpreted"]["ore_perse"])                    # non generalizzato ad altri codici
        self.assertTrue(any("evtValue" in m for m in x["interpreted"]["da_verificare"]))
        self.assertEqual(x["raw"]["evtValue"], 1)                           # il grezzo resta evtValue

    def test_raw_untouched_and_hash_only_on_raw(self):
        e = ev(evtValue=1)
        a = cv.normalize_absence(e)
        self.assertEqual(a["raw"], e)
        h = a["hash"]
        self.assertEqual(h, cv.normalize_absence(dict(e, nuovo=1))["hash"])
        self.assertNotEqual(h, cv.normalize_absence(dict(e, webJustifStatus=0))["hash"])
        self.assertEqual(a["uid"], "cvv:1")

    def test_real_cases_from_ui(self):
        # le 7 corrispondenze verificate sulla UI web ufficiale (codice, webJustifStatus, evtValue) -> attese
        cases = [("ABR0", 1, None, "Entrata in ritardo", "In approvazione", None), ("ABR0", 1, None, "Entrata in ritardo", "In approvazione", None),
                 ("ABR0", 1, None, "Entrata in ritardo", "In approvazione", None), ("ABR0", 0, None, "Entrata in ritardo", "Da giustificare", None),
                 ("ABA0", 0, None, "Assenza", "Da giustificare", None), ("ABR0", 0, 1, "Entrata in ritardo", "Da giustificare", 1),
                 ("ABR0", 0, None, "Entrata in ritardo", "Da giustificare", None)]
        for code, ws, val, tipo, stato, ore in cases:
            i = cv.normalize_absence(ev(evtCode=code, webJustifStatus=ws, evtValue=val))["interpreted"]
            self.assertEqual((i["tipo"], i["stato_giustificazione"], i["ore_perse"]), (tipo, stato, ore))



class Versione(unittest.TestCase):
    def test_version_auto_changes_with_tables_and_rev(self):
        v0 = cv.absence_interp_version()
        self.assertRegex(v0, r"^r\d+-[0-9a-f]{6}$")
        self.assertEqual(v0, cv.absence_interp_version())                  # stabile
        old = dict(cv.ABSENCE_CODE_MAP)
        try:
            cv.ABSENCE_CODE_MAP["ABU0"] = "Uscita anticipata"               # nuova interpretazione verificata
            self.assertNotEqual(v0, cv.absence_interp_version())
        finally:
            cv.ABSENCE_CODE_MAP.clear(); cv.ABSENCE_CODE_MAP.update(old)
        self.assertEqual(v0, cv.absence_interp_version())
        rev = cv.ABSENCE_RULES_REV
        try:
            cv.ABSENCE_RULES_REV = rev + 1                                  # cambio di logica
            self.assertNotEqual(v0, cv.absence_interp_version())
        finally:
            cv.ABSENCE_RULES_REV = rev

    def test_version_is_not_in_hash(self):
        e = ev()
        h = cv.normalize_absence(e)["hash"]
        old = dict(cv.ABSENCE_CODE_MAP)
        try:
            cv.ABSENCE_CODE_MAP["ABU0"] = "Uscita anticipata"
            self.assertEqual(h, cv.normalize_absence(e)["hash"])           # l'hash non dipende dalle interpretazioni
        finally:
            cv.ABSENCE_CODE_MAP.clear(); cv.ABSENCE_CODE_MAP.update(old)



if __name__ == "__main__":
    unittest.main()
