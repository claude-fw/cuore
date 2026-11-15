"""M3 - migrazione ESPLICITA solo-schema (cvv_sync.migrate_only): snapshot verificato prima, nessuna lettura live, nessun run,
contenuti identici dopo. Solo su copie sintetiche v1."""
import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import cvv_sync as S
import registry as R
from test_m3_sync_runs import Base, make_v1, sha, cols, triggers


class MigrateOnly(Base):
    def test_default_is_preview_and_writes_nothing(self):
        make_v1(self.path); h = sha(self.path); files = sorted(os.listdir(self.root))
        out = S.migrate_only(db_path=self.path)
        self.assertTrue(out["mode"].startswith("ANTEPRIMA")); self.assertFalse(out["scritture"]["sqlite_reale"])
        self.assertEqual(h, sha(self.path)); self.assertEqual(files, sorted(os.listdir(self.root)))
        self.assertEqual(R.stored_version(self.path), 1)

    def test_confirm_must_be_real_bool(self):
        make_v1(self.path); h = sha(self.path)
        for bad in ("true", 1, 0):
            with self.assertRaises(ValueError):
                S.migrate_only(db_path=self.path, confirm=bad)
        self.assertEqual(h, sha(self.path))

    def test_migration_snapshot_first_content_identical_no_new_runs(self):
        make_v1(self.path)
        out = S.migrate_only(db_path=self.path, confirm=True)
        self.assertEqual(out["esito"], "MIGRATO E VERIFICATO", out)
        self.assertTrue(out["verifica_snapshot"]["ok"])
        snap = out["snapshot_premigrazione"]
        self.assertIn("premigrazione", os.path.basename(snap)); self.assertEqual(R.stored_version(snap), 1)
        self.assertEqual(R.stored_version(self.path), 2)
        self.assertEqual(out["prima"], out["dopo"])
        self.assertEqual(out["prima"]["runs"]["righe"], 3)           # nessun run aggiunto
        for c in ("finished_at", "duration_ms", "mode", "outcome"):
            self.assertIn(c, cols(self.path))
        self.assertTrue({"runs_no_update", "runs_no_delete"} <= triggers(self.path))
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")

    def test_legacy_runs_still_derived_after_migration(self):
        make_v1(self.path); S.migrate_only(db_path=self.path, confirm=True)
        reg = R.Registry.from_snapshot(self.path)
        try:
            rs = reg.runs()
            self.assertTrue(rs and all(r.get("legacy") for r in rs))
        finally:
            reg.close()

    def test_second_call_is_noop(self):
        make_v1(self.path); S.migrate_only(db_path=self.path, confirm=True); h = sha(self.path)
        out = S.migrate_only(db_path=self.path, confirm=True)
        self.assertIn("nessuna migrazione necessaria", out["esito"]); self.assertEqual(h, sha(self.path))

    def test_runs_immutable_after_migration(self):
        import sqlite3
        make_v1(self.path); S.migrate_only(db_path=self.path, confirm=True)
        c = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("UPDATE runs SET status='ok'")
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("DELETE FROM runs")
        c.close()

    def test_missing_registry_and_future_schema_untouched(self):
        out = S.migrate_only(db_path=self.path, confirm=True)
        self.assertIn("assente", out["esito"]); self.assertFalse(os.path.exists(self.path))

    def test_lock_busy_refuses_without_writing(self):
        make_v1(self.path); h = sha(self.path)
        with S._Lock(os.path.join(self.root, "registry.lock")):
            out = S.migrate_only(db_path=self.path, confirm=True)
        self.assertIn("in corso", out["esito"]); self.assertEqual(h, sha(self.path))
        self.assertFalse(os.path.isdir(os.path.join(self.root, "backups")))

    def test_failed_snapshot_verification_aborts_migration(self):
        make_v1(self.path); h = sha(self.path)
        orig = S._backup
        def bad(*a, **k): raise RuntimeError("snapshot non verificato: finto")
        S._backup = bad
        try:
            out = S.migrate_only(db_path=self.path, confirm=True)
        finally:
            S._backup = orig
        self.assertIn("FALLITA", out["esito"]); self.assertEqual(h, sha(self.path)); self.assertEqual(R.stored_version(self.path), 1)


if __name__ == "__main__":
    unittest.main()
