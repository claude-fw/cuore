"""STATO DI REGISTRO degli eventi di agenda. Il registro usa 'missing_in_source' (registry.MISSING): un evento sparito da
ClasseViva deve risultare mancante, uno ricomparso di nuovo presente, mai cancellato. Solo dati SINTETICI e registro in
memoria: nessun dato scolastico reale. (La proiezione verso Notion che usava questi stati e' stata rimossa: Notion non fa
parte di CUORE.)"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "..", "_common"))
import cvv_registry as CR        # noqa: E402
import links_fixtures as LF      # noqa: E402
import registry as R             # noqa: E402

VER_A = LF.ev(91001, "Verifica di matematica (SINT.)", "PROF SINT", "2026-12-01")
VER_B = LF.ev(91002, "Verifica di storia (SINT.)", "PROF SINT", "2026-12-02")


class Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(R, "default_path", side_effect=AssertionError("il registro reale non va mai aperto"))
        p.start()
        self.addCleanup(p.stop)
        self.reg = R.Registry(":memory:")
        self.addCleanup(self.reg.close)

    def sync(self, *events):
        return CR.sync_kind(self.reg, "agenda_event", lambda: [dict(e) for e in events])

    def status(self):
        return {r["source_uid"]: r["source_status"] for r in self.reg.list_records(source=CR.SOURCE, kind="agenda_event")}


class StatoRegistro(Base):
    def test_event_missing_from_source_is_missing_in_source(self):
        self.sync(VER_A, VER_B)
        res = self.sync(VER_B)
        self.assertEqual(res["counts"]["missing"], 1)
        self.assertEqual(self.status()["91001"], R.MISSING)

    def test_present_event_stays_present(self):
        self.sync(VER_A, VER_B)
        self.sync(VER_B)
        self.assertEqual(self.status()["91002"], R.PRESENT)

    def test_reappeared_event_is_present_again(self):
        self.sync(VER_A, VER_B)
        self.sync(VER_B)
        res = self.sync(VER_A, VER_B)
        self.assertEqual(res["counts"]["reappeared"], 1)
        self.assertEqual(self.status()["91001"], R.PRESENT)

    def test_registry_has_two_states_and_never_deletes(self):
        self.assertEqual((R.PRESENT, R.MISSING), ("present", "missing_in_source"))
        self.sync(VER_A, VER_B)
        self.sync(VER_B)
        self.assertEqual(set(self.status()), {"91001", "91002"})                # l'evento sparito resta nel registro
        self.assertEqual(set(self.status().values()), {R.PRESENT, R.MISSING})


if __name__ == "__main__":
    unittest.main()
