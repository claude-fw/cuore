"""M5 - parser di date italiane ESPLICITE (it_dates): formati, anno scolastico, giorno della settimana, date impossibili,
nessuna data relativa."""
import datetime as dt, os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import it_dates as ID


def one(text, y=2026):
    r = ID.parse_dates(text, y)
    assert len(r) == 1, r
    return r[0]


class Formats(unittest.TestCase):
    def test_numeric_and_textual(self):
        for text, iso in [("verifica 5/10", "2026-10-05"), ("verifica 05/10/2026", "2026-10-05"), ("il 5/10/26", "2026-10-05"),
                          ("5 ottobre", "2026-10-05"), ("5 Ottobre 2026", "2026-10-05"), ("1° ottobre", "2026-10-01"),
                          ("lunedì 5 ottobre", "2026-10-05"), ("giovedì 1/10", "2026-10-01")]:
            r = one(text)
            self.assertEqual((r["data"], r["stato"]), (iso, "valida"), text)

    def test_year_from_school_year_and_declared(self):
        r = one("3 ottobre", 2025)
        self.assertEqual((r["data"], r["anno_esplicito"]), ("2025-10-03", False))
        self.assertEqual(one("3 ottobre", 2026)["data"], "2026-10-03")
        # gennaio-agosto cadono nell'anno SUCCESSIVO a quello di inizio
        self.assertEqual(one("15 gennaio", 2026)["data"], "2027-01-15")
        self.assertEqual(one("9 aprile", 2025)["data"], "2026-04-09")
        self.assertEqual(one("15 settembre", 2026)["data"], "2026-09-15")
        r = one("5 ottobre 2026", 2030)
        self.assertEqual((r["data"], r["anno_esplicito"]), ("2026-10-05", True))        # l'anno esplicito prevale

    def test_impossible_dates_never_corrected(self):
        for text in ("31/9", "30 febbraio", "25/26", "0/10", "12/13", "29/2"):
            r = one(text)
            self.assertEqual((r["data"], r["stato"]), (None, "impossibile"), text)
        self.assertEqual(one("29/2/2028")["data"], "2028-02-29")

    def test_weekday_incoherent_is_flagged_not_fixed(self):
        r = one("giovedì 2/10")                     # il 2/10/2026 e' venerdi
        self.assertEqual((r["data"], r["stato"], r["weekday"]), ("2026-10-02", "weekday_incoerente", "giovedi"))
        self.assertEqual(one("venerdì 2/10")["stato"], "valida")

    def test_no_relative_dates_and_no_year_pairs(self):
        for text in ("per lunedì", "domani", "tra una settimana", "anno 2025/26", "capitolo 9", "pag. 40 n. 1-5", "alle 10:30"):
            self.assertEqual(ID.parse_dates(text, 2026), [], text)

    def test_several_dates_in_order_and_determinism(self):
        t = "dal 1/10 al 7 ottobre 2026 e poi 12/10"
        a = ID.parse_dates(t, 2026)
        self.assertEqual([x["data"] for x in a], ["2026-10-01", "2026-10-07", "2026-10-12"])
        self.assertEqual([x["inizio"] for x in a], sorted(x["inizio"] for x in a))
        self.assertEqual(a, ID.parse_dates(t, 2026))

    def test_school_year_helpers(self):
        self.assertEqual(ID.school_year_start("2026-08-31"), 2025)
        self.assertEqual(ID.school_year_start("2026-09-01"), 2026)
        self.assertEqual(ID.school_year_start(dt.date(2027, 1, 5)), 2026)
        self.assertEqual(ID.school_year_label("2026-10-04"), "2026-27")

    def test_pure_no_input_mutation(self):
        t = "Verifica del 5 ottobre"
        ID.parse_dates(t, 2026)
        self.assertEqual(t, "Verifica del 5 ottobre")


if __name__ == "__main__":
    unittest.main()
