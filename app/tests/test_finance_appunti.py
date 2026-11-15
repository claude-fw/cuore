"""Appunti personali di Finance (`app/bridge/finance_appunti.py`): solo file sintetici in cartelle temporanee.
Esecuzione: cd app && /usr/bin/python3 -B -m unittest tests.test_finance_appunti"""
import os, shutil, stat, sys, tempfile, unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bridge"))
import finance_appunti as FA  # noqa: E402


class Appunti(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cuore-appunti-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "appunti.jsonl")

    def add(self, ev, **kw):
        return FA.aggiungi(self.path, ev, **kw)

    def items(self):
        return {a["id"]: a for a in FA.stato(self.path)["appunti"]}

    def test_ciclo_completo_senza_cancellazioni(self):
        a = self.add({"tipo": "crea", "testo": " Comprare le scarpe ", "categoria": "da_comprare", "quando": "2026-11-10T18:00"})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        b = self.add({"tipo": "crea", "testo": "Il budget svago sta finendo", "categoria": "budget"})
        it = self.items()
        self.assertEqual((it[a["id"]]["testo"], it[a["id"]]["promemoria"], it[b["id"]]["promemoria"]),
                         ("Comprare le scarpe", True, False))
        self.add({"tipo": "modifica", "id": a["id"], "quando": "2026-11-11", "acquisto": "acq-1"})
        self.add({"tipo": "promemoria", "id": a["id"], "attivo": False})
        self.assertEqual((self.items()[a["id"]]["quando"], self.items()[a["id"]]["promemoria"]), ("2026-11-11", False))
        self.add({"tipo": "promemoria", "id": a["id"], "attivo": True})
        self.add({"tipo": "completa", "id": a["id"]})
        self.add({"tipo": "annulla", "id": b["id"], "motivo": "non serve piu'"})
        st = FA.stato(self.path)
        self.assertEqual((st["aperti"], st["eventi"], len(st["appunti"])), (0, 7, 2))              # niente sparisce
        self.assertEqual({a_["stato"] for a_ in st["appunti"]}, {"completato", "annullato"})
        for ev in ({"tipo": "modifica", "id": a["id"], "testo": "x"}, {"tipo": "completa", "id": b["id"]}):
            with self.assertRaises(FA.NonValido):                                                 # chiusi: non si modificano
                self.add(ev)

    def test_togliere_la_data_spegne_il_promemoria(self):
        a = self.add({"tipo": "crea", "testo": "Pensare alla bici", "quando": "2026-11-10"})
        self.add({"tipo": "modifica", "id": a["id"], "quando": None})
        self.assertEqual((self.items()[a["id"]]["quando"], self.items()[a["id"]]["promemoria"]), (None, False))
        with self.assertRaises(FA.NonValido):
            self.add({"tipo": "promemoria", "id": a["id"], "attivo": True})                       # senza data: no

    def test_ingressi_non_validi(self):
        for bad in ({"tipo": "crea", "testo": ""}, {"tipo": "crea", "testo": "x" * 301}, {"tipo": "crea", "testo": "x", "quando": "domani"},
                    {"tipo": "crea", "testo": "x", "quando": "2026-02-30"}, {"tipo": "crea", "testo": "x", "promemoria": True},
                    {"tipo": "crea", "testo": "x", "categoria": "Con Spazi"}, {"tipo": "crea", "testo": "x", "importo": 5},
                    {"tipo": "modifica", "id": "manca", "testo": "x"}, {"tipo": "modifica", "id": "manca"},
                    {"tipo": "promemoria", "id": "manca", "attivo": "si"}, {"tipo": "cancella", "id": "x"}, "testo"):
            with self.assertRaises(FA.NonValido, msg=bad):
                self.add(bad)
        self.assertFalse(os.path.exists(self.path))

    def test_richiesta_ripetuta_nessun_doppione(self):
        ev = {"tipo": "crea", "testo": "Occhio alle spese"}
        first = self.add(ev, richiesta="req-00000001")
        self.assertEqual(self.add(ev, richiesta="req-00000001"), first)
        with self.assertRaises(FA.NonValido):
            self.add({"tipo": "crea", "testo": "altro"}, richiesta="req-00000001")
        self.assertEqual(FA.stato(self.path)["eventi"], 1)


if __name__ == "__main__":
    unittest.main()
