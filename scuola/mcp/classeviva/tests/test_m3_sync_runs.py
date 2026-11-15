"""M3 - robustezza della sync: run con fine/durata/modalita'/esito, immutabilita' di runs, migrazione ESPLICITA v1->v2
(su copie sintetiche), snapshot verificati, retention, freschezza. Nessun dato reale: la fixture v1 usa lo schema vecchio."""
import hashlib, json, os, sqlite3, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import cvv_dryrun as D
import cvv_registry as CR
import cvv_sync as S
import registry as R
from test_sync import fx, EVENT, WIN

# --- fixture v1: DDL del registro PRIMA di M3 (runs senza colonne v2 e senza trigger), con dati e run storici ---------------
V1_RUNS = """CREATE TABLE runs(run_id TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL,
  started_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ok','error')),
  items_seen INTEGER, counts_json TEXT, warnings_json TEXT, error TEXT);"""


def make_v1(path):
    """Crea un file v1 realistico: parte da un registro v2 popolato, poi lo riporta allo schema vecchio."""
    reg = R.Registry(path)
    CR.sync_kind(reg, "notice", lambda: [{"pubId": 1, "pubDT": "2026-10-29T12:00:00+02:00", "cntTitle": "A", "readStatus": False}])
    CR.sync_kind(reg, "absence", lambda: [{"evtId": 5, "evtCode": "ABA0", "evtDate": "2026-10-26", "evtHPos": 1, "evtValue": None,
                                          "hoursAbsence": [], "isJustified": False, "webJustifStatus": 0}])
    reg.record_failed_run("classeviva", "grade", "rete: timeout")
    reg.close()
    c = sqlite3.connect(path, isolation_level=None)
    rows = c.execute("SELECT run_id,source,kind,started_at,status,items_seen,counts_json,warnings_json,error FROM runs").fetchall()
    c.execute("DROP TRIGGER IF EXISTS runs_no_update"); c.execute("DROP TRIGGER IF EXISTS runs_no_delete")
    c.execute("DROP TABLE runs"); c.execute(V1_RUNS)
    c.executemany("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)", rows)
    c.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    c.execute("DELETE FROM meta WHERE key='migrated_v1_to_v2_at'")
    c.close()
    return path


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def cols(path, table="runs"):
    c = sqlite3.connect("file:%s?immutable=1" % path, uri=True)
    try:
        return [r[1] for r in c.execute("PRAGMA table_info(%s)" % table)]
    finally:
        c.close()


def triggers(path):
    c = sqlite3.connect("file:%s?immutable=1" % path, uri=True)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    finally:
        c.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "school")
        os.makedirs(self.root)
        os.chmod(self.root, 0o700)  # come la cartella reale: il check post-scrittura non va indebolito
        self.path = os.path.join(self.root, "registry.sqlite3")


class RunV2(Base):
    def test_commit_run_has_end_duration_mode_outcome(self):
        reg = R.Registry(self.path)
        res = CR.sync_kind(reg, "absence", lambda: [{"evtId": 5, "evtCode": "ABA0", "evtDate": "2026-10-26", "evtHPos": 1,
                                                    "evtValue": None, "hoursAbsence": [], "isJustified": False, "webJustifStatus": 0}])
        run = [r for r in reg.runs() if r["run_id"] == res["run_id"]][0]
        self.assertEqual((run["mode"], run["outcome"], run["status"]), ("commit", "ok", "ok"))
        self.assertIsInstance(run["duration_ms"], int); self.assertGreaterEqual(run["duration_ms"], 0)
        self.assertIsNotNone(run["finished_at"]); self.assertGreaterEqual(run["finished_at"], run["started_at"])
        self.assertEqual(res["outcome"], "ok")
        reg.close()

    def test_memory_registry_runs_are_dry_run(self):
        reg = R.Registry(":memory:")
        CR.sync_kind(reg, "notice", lambda: [])
        self.assertEqual(reg.runs()[0]["mode"], "dry_run")

    def test_failed_run_is_error_with_mode_and_duration(self):
        reg = R.Registry(self.path)
        def boom():
            time.sleep(0.01); raise RuntimeError("rete")
        res = reg.run_source("classeviva", "grade", boom)
        run = [r for r in reg.runs() if r["run_id"] == res["run_id"]][0]
        self.assertEqual((res["outcome"], run["outcome"], run["status"], run["mode"]), ("error", "error", "error", "commit"))
        self.assertGreaterEqual(run["duration_ms"], 5)
        reg.close()

    def test_empty_read_is_refused_run_and_nothing_marked_missing(self):
        reg = R.Registry(":memory:")
        raws = [{"pubId": i, "pubDT": "2026-10-29T12:00:00+02:00", "cntTitle": "t%d" % i} for i in range(4)]
        CR.sync_kind(reg, "notice", lambda: raws, window=("2026-09-29", "2027-08-31"))
        res = CR.sync_kind(reg, "notice", lambda: [], window=("2026-09-29", "2027-08-31"))
        self.assertEqual((res["outcome"], res["counts"]["missing_refused"], res["counts"]["missing"]), ("rifiutato", 4, 0))
        self.assertEqual(reg.runs()[-1]["outcome"], "rifiutato")
        self.assertEqual({r["source_status"] for r in reg.list_records(kind="notice")}, {"present"})

    def test_partial_drop_is_ok_with_rejections(self):
        reg = R.Registry(":memory:")
        raws = [{"pubId": i, "pubDT": "2026-10-29T12:00:00+02:00", "cntTitle": "t%d" % i} for i in range(6)]
        CR.sync_kind(reg, "notice", lambda: raws, window=("2026-09-29", "2027-08-31"))
        res = CR.sync_kind(reg, "notice", lambda: raws[:1] + [{"pubId": 99, "pubDT": "2026-10-30T12:00:00+02:00", "cntTitle": "n"}],
                           window=("2026-09-29", "2027-08-31"))
        self.assertEqual(res["outcome"], "ok_con_rifiuti")
        self.assertEqual((res["counts"]["create"], res["counts"]["missing"]), (1, 0))

    def test_runs_are_immutable(self):
        reg = R.Registry(self.path)
        CR.sync_kind(reg, "notice", lambda: [])
        for sql in ("UPDATE runs SET status='error'", "DELETE FROM runs"):
            with self.assertRaises(sqlite3.DatabaseError):
                reg.db.execute(sql)
        self.assertEqual(len(reg.runs()), 1)
        reg.close()

    def test_other_immutability_unchanged(self):
        reg = R.Registry(self.path)
        CR.sync_kind(reg, "notice", lambda: [{"pubId": 1, "pubDT": "2026-10-29T12:00:00+02:00", "cntTitle": "A"}])
        for sql in ("DELETE FROM versions", "UPDATE versions SET raw_json='x'", "DELETE FROM events", "DELETE FROM records"):
            with self.assertRaises(sqlite3.DatabaseError):
                reg.db.execute(sql)
        reg.close()


class MigrazioneEsplicita(Base):
    def test_v1_file_is_refused_not_migrated_implicitly(self):
        make_v1(self.path); h = sha(self.path)
        self.assertEqual(R.stored_version(self.path), 1)
        with self.assertRaises(R.RegistryError):
            R.Registry(self.path)
        self.assertEqual(sha(self.path), h)                                    # nemmeno un byte
        self.assertNotIn("runs_no_update", triggers(self.path))
        self.assertNotIn("mode", cols(self.path))

    def test_snapshot_of_v1_is_migrated_in_memory_only(self):
        make_v1(self.path); h = sha(self.path)
        reg = R.Registry.from_snapshot(self.path)
        self.assertTrue(reg.migrated_in_memory)
        self.assertEqual(reg.verify_integrity(), [])
        runs = reg.runs()
        self.assertTrue(all(r["mode"] == "commit" and r.get("legacy") for r in runs))      # derivato in lettura
        self.assertEqual(sorted(r["outcome"] for r in runs), ["error", "ok", "ok"])
        self.assertTrue(all(r["finished_at"] is None for r in runs))                       # non inventati
        reg.close()
        self.assertEqual(sha(self.path), h)
        self.assertEqual(R.stored_version(self.path), 1)

    def test_migrate_file_on_a_copy_is_additive_and_preserves_history(self):
        make_v1(self.path)
        before = sqlite3.connect(self.path)
        old_runs = before.execute("SELECT run_id,source,kind,started_at,status,items_seen,counts_json,error FROM runs ORDER BY run_id").fetchall()
        n_versions = before.execute("SELECT count(*) FROM versions").fetchone()[0]
        before.close()
        res = R.migrate_file(self.path)
        self.assertEqual((res["da"], res["a"], res["integrity_check"], res["versione_dopo"]), (1, 2, "ok", 2))
        self.assertEqual(R.stored_version(self.path), 2)
        self.assertTrue({"finished_at", "duration_ms", "mode", "outcome"} <= set(cols(self.path)))
        self.assertTrue({"runs_no_update", "runs_no_delete"} <= triggers(self.path))
        c = sqlite3.connect(self.path)
        self.assertEqual(old_runs, c.execute("SELECT run_id,source,kind,started_at,status,items_seen,counts_json,error FROM runs ORDER BY run_id").fetchall())
        self.assertEqual(n_versions, c.execute("SELECT count(*) FROM versions").fetchone()[0])
        self.assertTrue(all(r[0] is None for r in c.execute("SELECT mode FROM runs")))      # storia NON riscritta
        c.close()
        reg = R.Registry(self.path)                                                         # ora si apre e si usa
        res = CR.sync_kind(reg, "notice", lambda: [{"pubId": 7, "pubDT": "2026-10-31T12:00:00+02:00", "cntTitle": "B"}])
        self.assertEqual(res["counts"]["create"], 1)
        self.assertEqual(reg.runs()[-1]["mode"], "commit")
        self.assertEqual(reg.verify_integrity(), [])
        with self.assertRaises(sqlite3.DatabaseError):
            reg.db.execute("DELETE FROM runs")
        reg.close()
        with self.assertRaises(R.RegistryError):                                            # non e' ripetibile
            R.migrate_file(self.path)

    def test_future_schema_still_refused(self):
        R.Registry(self.path).close()
        c = sqlite3.connect(self.path); c.execute("UPDATE meta SET value='99' WHERE key='schema_version'"); c.commit(); c.close()
        with self.assertRaises(R.RegistryError):
            R.Registry(self.path)

    def test_dryrun_reports_pending_migration_and_does_not_touch_the_file(self):
        make_v1(self.path); h = sha(self.path)
        res = D.run({"notice": (lambda: [], None)}, kinds=["notice"], simulate=False, baseline_path=self.path)
        self.assertEqual(res["baseline"]["schema"]["su_disco"], 1)
        self.assertTrue(res["baseline"]["schema"]["migrazione_necessaria"])
        self.assertEqual(sha(self.path), h)
        self.assertFalse(res["verifica_nessuna_scrittura"]["file_reale_o_ausiliari_creati_o_modificati"])


class CommitConMigrazione(Base):
    def test_commit_on_v1_without_migrate_is_refused_with_zero_writes(self):
        make_v1(self.path); h = sha(self.path)
        out = S.run(fx(), db_path=self.path, commit=True)
        self.assertEqual(out["mode"], "COMMIT RIFIUTATO")
        self.assertIn("migrate=true", out["esito"])
        self.assertTrue(out["schema"]["migrazione_necessaria"])
        self.assertEqual(sha(self.path), h)
        self.assertFalse(os.path.isdir(os.path.join(self.root, "backups")))

    def test_commit_with_migrate_takes_verified_premigration_snapshot_first(self):
        make_v1(self.path)
        c = sqlite3.connect(self.path); before = S._table_counts(c); c.close()
        out = S.run(fx(), db_path=self.path, commit=True, migrate=True)
        self.assertEqual(out["esito"], "SCRITTO E VERIFICATO", out)
        m = out["migrazione_schema"]
        self.assertIn("premigrazione", os.path.basename(m["snapshot_premigrazione"]))
        self.assertTrue(m["verifica_snapshot"]["ok"])
        self.assertEqual(m["risultato"]["versione_dopo"], 2)
        self.assertEqual(R.stored_version(self.path), 2)
        self.assertEqual(R.stored_version(m["snapshot_premigrazione"]), 1)                    # lo snapshot e' lo stato v1
        c = sqlite3.connect("file:%s?immutable=1" % m["snapshot_premigrazione"], uri=True)
        self.assertEqual(S._table_counts(c), before); c.close()
        names = [x["controllo"] for x in out["verifica_post_scrittura"]["controlli"]]
        self.assertTrue(any("run registrato" in n for n in names))
        self.assertIn("storico dei run protetto da trigger", names)
        self.assertEqual(os.stat(m["snapshot_premigrazione"]).st_mode & 0o777, 0o600)

    def test_after_migration_next_commit_needs_no_migrate_and_is_idempotent(self):
        make_v1(self.path)
        S.run(fx(), db_path=self.path, commit=True, migrate=True)
        out = S.run(fx(), db_path=self.path, commit=True, expect_create=0)
        self.assertEqual(out["esito"], "SCRITTO E VERIFICATO")
        self.assertEqual(out["riepilogo"]["invariati"], 7)
        self.assertIsNone(out["migrazione_schema"])
        self.assertFalse(out["schema"]["migrazione_necessaria"])

    def test_fresh_registry_needs_no_migration(self):
        out = S.run(fx(), db_path=self.path, commit=True, expect_create=7)
        self.assertEqual(out["esito"], "SCRITTO E VERIFICATO")
        self.assertEqual(out["schema"], {"su_disco": None, "atteso": 2, "migrazione_necessaria": False})
        self.assertEqual(R.stored_version(self.path), 2)

    def test_migrate_flag_must_be_a_real_boolean(self):
        with self.assertRaises(ValueError):
            S.run(fx(), db_path=self.path, commit=True, migrate="true")

    def test_guardrails_still_in_place(self):
        out = S.run(fx(), db_path=self.path, commit=True, expect_create=3)               # sbagliato
        self.assertEqual(out["mode"], "COMMIT RIFIUTATO")
        self.assertFalse(os.path.exists(self.path))
        out = S.run(fx(), db_path=self.path, commit=True, expect_create=7)
        self.assertEqual(out["esito"], "SCRITTO E VERIFICATO")
        # lettura fallita su un kind: gli altri proseguono, nessun missing, run in errore tracciato
        def boom(): raise RuntimeError("rete")
        f = fx(notice=(boom, None))
        out = S.run(f, db_path=self.path, commit=True, expect_create=0)
        self.assertEqual(out["esito_scrittura"]["notice"]["stato"], "errore")
        reg = R.Registry.from_snapshot(self.path)
        self.assertEqual({r["source_status"] for r in reg.list_records(kind="notice")}, {"present"})
        self.assertIn("error", [r["outcome"] for r in reg.runs()])
        reg.close()


class SnapshotVerificati(Base):
    def test_failed_verification_aborts_before_any_write(self):
        S.run(fx(), db_path=self.path, commit=True, expect_create=7)
        h = sha(self.path)
        changed = dict(fx())
        changed["notice"] = (lambda: [dict(r, cntTitle=r["cntTitle"] + "!") for r in fx()["notice"][0]()], None)   # forza un update
        calls = {"n": 0}
        real = S._table_counts
        def fake(conn):
            calls["n"] += 1
            c = real(conn)
            if calls["n"] == 2:
                c["records"] += 1
            return c
        S._table_counts = fake
        try:
            out = S.run(changed, db_path=self.path, commit=True, expect_create=0)
        finally:
            S._table_counts = real
        self.assertEqual(out["mode"], "COMMIT FALLITO")
        self.assertIn("snapshot non verificato", out["esito"])
        reg = R.Registry.from_snapshot(self.path)
        self.assertEqual(len(reg.list_records(kind="notice")), 3)
        self.assertTrue(all(r["current_version"] == 1 for r in reg.list_records(kind="notice")))          # nessun update scritto
        reg.close()

    def test_update_commit_makes_verified_backup(self):
        S.run(fx(), db_path=self.path, commit=True, expect_create=7)
        f = fx(); f["notice"] = (lambda: [dict(r, cntTitle=r["cntTitle"] + "!") for r in fx()["notice"][0]()], None)
        out = S.run(f, db_path=self.path, commit=True, expect_create=0)
        self.assertEqual(out["esito"], "SCRITTO E VERIFICATO")
        self.assertTrue(os.path.isfile(out["backup_prima_della_scrittura"]))
        self.assertNotIn("premigrazione", out["backup_prima_della_scrittura"])


class Retention(Base):
    def mk(self, names_ages):
        b = os.path.join(self.root, "backups"); os.makedirs(b)
        now = time.time()
        for name, age_days in names_ages:
            p = os.path.join(b, name); open(p, "w").write("x")
            os.utime(p, (now - age_days * 86400, now - age_days * 86400))
        return b

    def test_plan_keeps_recent_last_n_and_premigration_and_prunes_only_old_extra(self):
        files = [("registry-2026%02d.sqlite3" % i, 100 - i) for i in range(1, 9)]                      # 8 vecchi (92..99 gg)
        files += [("registry-premigrazione-old.sqlite3", 200), ("registry-recent.sqlite3", 1), ("altro.txt", 500)]
        self.mk(files)
        plan = S.retention_plan(self.root, keep_last=3, keep_days=30)
        self.assertEqual(plan["totale"], 10)                                                       # altro.txt ignorato
        self.assertNotIn("registry-premigrazione-old.sqlite3", plan["da_potare"])
        self.assertNotIn("registry-recent.sqlite3", plan["da_potare"])
        self.assertEqual(len(plan["da_potare"]), 10 - plan["da_conservare"])
        self.assertTrue(plan["da_potare"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "backups")))[0], "altro.txt")     # il piano non cancella

    def test_prune_deletes_only_listed_snapshots(self):
        self.mk([("registry-a.sqlite3", 90), ("registry-b.sqlite3", 91), ("registry-premigrazione-x.sqlite3", 300), ("note.txt", 400)])
        plan = S.retention_plan(self.root, keep_last=1, keep_days=30)
        done = S.prune_backups(self.root, plan)
        left = sorted(os.listdir(os.path.join(self.root, "backups")))
        self.assertEqual(sorted(done), sorted(plan["da_potare"]))
        self.assertIn("registry-premigrazione-x.sqlite3", left); self.assertIn("note.txt", left)

    def test_commit_reports_plan_and_prunes_only_on_request(self):
        S.run(fx(), db_path=self.path, commit=True, expect_create=7)
        b = os.path.join(self.root, "backups"); os.makedirs(b, exist_ok=True)
        old = os.path.join(b, "registry-20200101T000000Z.sqlite3"); open(old, "w").write("x")
        os.utime(old, (1, 1))
        for i in range(12):
            p = os.path.join(b, "registry-2026%02d.sqlite3" % i); open(p, "w").write("x")
        f = fx(); f["notice"] = (lambda: [dict(r, cntTitle=r["cntTitle"] + "?") for r in fx()["notice"][0]()], None)
        out = S.run(f, db_path=self.path, commit=True, expect_create=0)
        self.assertIn("registry-20200101T000000Z.sqlite3", out["retention_snapshot"]["da_potare"])
        self.assertIsNone(out["retention_snapshot"]["eseguita"])
        self.assertTrue(os.path.exists(old))                                                        # solo un piano
        f2 = fx(); f2["notice"] = (lambda: [dict(r, cntTitle=r["cntTitle"] + "??") for r in fx()["notice"][0]()], None)
        out = S.run(f2, db_path=self.path, commit=True, expect_create=0, prune_backups_=True)
        self.assertIn("registry-20200101T000000Z.sqlite3", out["retention_snapshot"]["eseguita"])
        self.assertFalse(os.path.exists(old))


class Freschezza(unittest.TestCase):
    def mk(self):
        self.t = ["2026-10-29T08:00:00+00:00"]
        return R.Registry(":memory:", clock=lambda: self.t[0])

    def at(self, reg, iso, fn):
        self.t[0] = iso
        fn(reg)

    def commit_rows(self, reg):
        # simula run di commit: in memoria sono 'dry_run', quindi li rietichetto tramite un registro su disco
        raise NotImplementedError

    def disk(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.t = ["2026-10-29T08:00:00+00:00"]
        return R.Registry(os.path.join(tmp.name, "r.sqlite3"), clock=lambda: self.t[0])

    N = {"pubId": 1, "pubDT": "2026-10-29T12:00:00+02:00", "cntTitle": "A"}

    def test_fresh_then_stale_by_age(self):
        reg = self.disk()
        CR.sync_kind(reg, "notice", lambda: [self.N])
        f = reg.freshness("2026-10-29T18:00:00+00:00")["per_kind"]["classeviva/notice"]
        self.assertEqual((f["stale"], f["motivo"], f["eta_ore"], f["ultimo_run"]["esito"]), (False, "ok", 10.0, "ok"))
        self.assertIsNotNone(f["ultimo_run"]["fine"]); self.assertIsNotNone(f["ultimo_run"]["durata_ms"])
        f = reg.freshness("2026-11-01T08:00:00+00:00")["per_kind"]["classeviva/notice"]
        self.assertEqual((f["stale"], f["motivo"]), (True, "oltre_soglia"))
        f = reg.freshness("2026-11-01T08:00:00+00:00", stale_after_hours=100)["per_kind"]["classeviva/notice"]
        self.assertFalse(f["stale"])

    def test_last_run_error_is_stale_and_counts_consecutive_errors(self):
        reg = self.disk()
        CR.sync_kind(reg, "notice", lambda: [self.N])
        for i in (1, 2):
            self.t[0] = "2026-10-29T1%d:00:00+00:00" % i
            reg.run_source("classeviva", "notice", lambda: (_ for _ in ()).throw(RuntimeError("rete")))
        f = reg.freshness("2026-10-29T13:00:00+00:00")["per_kind"]["classeviva/notice"]
        self.assertEqual((f["stale"], f["motivo"], f["errori_consecutivi"]), (True, "ultimo_run_in_errore", 2))
        self.assertEqual(f["ultima_lettura_riuscita"], "2026-10-29T08:00:00+00:00")
        self.assertIn("rete", f["ultimo_errore"]["errore"])
        self.t[0] = "2026-10-29T14:00:00+00:00"
        CR.sync_kind(reg, "notice", lambda: [self.N])
        f = reg.freshness("2026-10-29T15:00:00+00:00")["per_kind"]["classeviva/notice"]
        self.assertEqual((f["stale"], f["errori_consecutivi"]), (False, 0))

    def test_never_synced_and_dry_runs_do_not_count(self):
        reg = self.mk()                                    # in memoria: run 'dry_run'
        CR.sync_kind(reg, "notice", lambda: [self.N])
        res = reg.freshness("2026-10-29T09:00:00+00:00")
        f = res["per_kind"]["classeviva/notice"]
        self.assertEqual((f["stale"], f["motivo"], f["ultimo_run"]), (True, "mai_sincronizzato", None))
        self.assertTrue(res["qualcosa_stale"])

    def test_refused_reading_is_flagged_but_not_stale(self):
        reg = self.disk()
        raws = [dict(self.N, pubId=i) for i in range(4)]
        W = ("2026-09-29", "2027-08-31")
        CR.sync_kind(reg, "notice", lambda: raws, window=W)
        self.t[0] = "2026-10-29T09:00:00+00:00"
        CR.sync_kind(reg, "notice", lambda: [], window=W)
        f = reg.freshness("2026-10-29T10:00:00+00:00")["per_kind"]["classeviva/notice"]
        self.assertEqual(f["ultimo_run"]["esito"], "rifiutato")
        self.assertEqual((f["stale"], f["motivo"]), (False, "ultima_lettura_con_rifiuti"))

    def test_freshness_is_read_only(self):
        reg = self.disk()
        CR.sync_kind(reg, "notice", lambda: [self.N])
        n = len(reg.runs()); reg.freshness("2026-10-30T00:00:00+00:00")
        self.assertEqual(len(reg.runs()), n)

    def test_legacy_runs_count_for_freshness_on_v1_snapshot(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        p = os.path.join(tmp.name, "r.sqlite3"); make_v1(p)
        reg = R.Registry.from_snapshot(p)
        res = reg.freshness("2099-01-01T00:00:00+00:00")
        f = res["per_kind"]["classeviva/notice"]
        self.assertTrue(f["ultimo_run"]["legacy"])
        self.assertEqual(f["motivo"], "oltre_soglia")
        self.assertIsNone(f["ultimo_run"]["fine"])
        reg.close()


if __name__ == "__main__":
    unittest.main()
