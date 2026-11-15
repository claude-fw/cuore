"""Sync manuale ClasseViva -> registro reale: default simulazione, scrittura solo con commit=True, verifica, sicurezza."""
import copy, hashlib, inspect, json, os, stat, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(ROOT, "..", "_common"))
import cvv_registry as CR
import cvv_sync as S
import registry as R
import test_bridge as TB
from test_dryrun import notices, absences, fetchers

EVENT = {"evtId": 7, "evtCode": "AGNT", "subjectDesc": None, "evtDatetimeBegin": "2026-11-07T08:00:00+02:00",
         "evtDatetimeEnd": "2026-11-07T09:00:00+02:00", "isFullDay": False, "notes": "Verifica di storia",
         "authorName": "X", "classDesc": "3A"}
WIN = ("2026-09-29", "2027-08-31")


def fx(**over):
    f = fetchers(agenda_event=(lambda: [dict(EVENT)], WIN))
    f.update(over)
    return f                    # 1 evento + 0 voti + 3 assenze + 3 comunicazioni = 7 record


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "Library", "Cloud", "school")
        self.path = os.path.join(self.root, "registry.sqlite3")

    def go(self, f=None, **kw):
        return S.run(f or fx(), db_path=self.path, **kw)

    def mode(self, p):
        return stat.S_IMODE(os.stat(p).st_mode)


class SimulazioneDiDefault(Base):
    def test_default_writes_nothing(self):
        out = S.run(fx(), db_path=self.path)
        self.assertFalse(out["commit"])
        self.assertIn("SIMULATO", out["mode"])
        self.assertFalse(any(out["scritture"].values()))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "Library")))     # nemmeno le cartelle
        self.assertEqual(os.listdir(self.tmp.name), [])
        self.assertEqual(out["riepilogo"]["record_da_creare"], 7)
        self.assertIn("expect_create=7", out["per_scrivere"])
        self.assertIs(inspect.signature(S.run).parameters["commit"].default, False)

    def test_commit_false_never_opens_a_real_database(self):
        real_init = R.Registry.__init__
        def guarded(self_, path=":memory:", clock=None):
            if path != ":memory:":
                raise AssertionError("con commit=false e' stato aperto un database su disco: %s" % path)
            real_init(self_, path, clock)
        R.Registry.__init__ = guarded
        try:
            out = S.run(fx(), commit=False, db_path=self.path)
            self.assertEqual(out["riepilogo"]["record_da_creare"], 7)
        finally:
            R.Registry.__init__ = real_init
        self.assertFalse(os.path.exists(self.root))

    def test_commit_must_be_a_real_boolean(self):
        for bad in ("true", "True", 1, "yes", None.__class__):
            with self.assertRaises(ValueError):
                S.run(fx(), commit=bad, db_path=self.path)
        self.assertFalse(os.path.exists(self.root))

    def test_expect_create_must_be_integer(self):
        for bad in ("7", 7.0, True):
            with self.assertRaises(ValueError):
                S.run(fx(), commit=True, expect_create=bad, db_path=self.path)
        self.assertFalse(os.path.exists(self.root))

    def test_simulation_uses_existing_baseline_read_only(self):
        self.go(commit=True)
        before = sha(self.path)
        out = self.go()                                                  # simulazione sul registro esistente
        self.assertEqual(out["riepilogo"]["invariati"], 7)
        self.assertEqual(out["riepilogo"]["record_da_creare"], 0)
        self.assertEqual(sha(self.path), before)


class Commit(Base):
    def test_first_commit_creates_private_registry_and_verifies(self):
        out = self.go(commit=True, expect_create=7)
        self.assertEqual(out["esito"], "SCRITTO E VERIFICATO")
        self.assertTrue(out["scritture"]["sqlite_reale"])
        self.assertFalse(out["scritture"]["classeviva"] or out["scritture"]["classroom"])
        self.assertNotIn("notion", out["scritture"])
        self.assertEqual(self.mode(self.path), 0o600)
        self.assertEqual(self.mode(self.root), 0o700)
        self.assertEqual(self.mode(os.path.dirname(self.root)), 0o700)         # 'Cloud' creata qui
        self.assertTrue(out["file_creato_ora"])
        self.assertIsNone(out["backup_prima_della_scrittura"])
        v = out["verifica_post_scrittura"]
        self.assertTrue(v["tutto_ok"], json.dumps(v, indent=1))
        self.assertEqual(v["stato_registro"]["totale_record"], 7)
        self.assertEqual(v["stato_registro"]["eventi"], {"create": 7})
        files = set(v["stato_registro"]["file"])
        self.assertTrue({"registry.lock", "registry.sqlite3"} <= files, files)
        # la SQLite di sistema (macOS, /usr/lib/libsqlite3.dylib) lascia dopo una chiusura pulita un -wal VUOTO e il -shm:
        # ammessi solo questi due file ausiliari; l'invariante vera resta "nessun -wal non vuoto"
        self.assertTrue(files <= {"registry.lock", "registry.sqlite3", "registry.sqlite3-wal", "registry.sqlite3-shm"}, files)
        wal = self.path + "-wal"
        self.assertFalse(os.path.exists(wal) and os.path.getsize(wal) > 0, "-wal non vuoto dopo la scrittura")
        self.assertTrue(out["coerenza_piano_scrittura"])

    def test_raw_persisted_exactly_and_readstatus_excluded(self):
        ns = notices(3); ns[1]["readStatus"] = True
        self.go(fx(notice=(lambda: ns, None)), commit=True)
        reg = R.Registry.from_snapshot(self.path)
        for n in ns:
            rec = reg.get_record(CR.SOURCE, "notice", str(n["pubId"]))
            self.assertEqual(rec["raw"], n)
            self.assertEqual(rec["hash_excludes"], ["readStatus"])
            self.assertEqual(rec["volatile"], {"readStatus": n["readStatus"]})
            self.assertEqual(rec["legacy_uid"], "cvv:%d" % n["pubId"])
        ab = reg.get_record(CR.SOURCE, "absence", "1")
        self.assertEqual(ab["raw"], absences(3)[0])
        self.assertEqual(reg.verify_integrity(), [])

    def test_expect_create_mismatch_refuses_and_writes_nothing(self):
        out = self.go(commit=True, expect_create=26)
        self.assertEqual(out["mode"], "COMMIT RIFIUTATO")
        self.assertFalse(out["scritture"]["sqlite_reale"])
        self.assertFalse(os.path.exists(self.root))

    def test_second_commit_is_idempotent_for_raw_and_versions(self):
        self.go(commit=True)
        reg = R.Registry.from_snapshot(self.path)
        v1 = {(r["kind"], r["source_uid"]): reg.get_versions(CR.SOURCE, r["kind"], r["source_uid"]) for r in reg.list_records()}
        reg.close()
        out = self.go(commit=True, expect_create=0)
        self.assertEqual(out["esito"], "SCRITTO E VERIFICATO")
        self.assertEqual(out["riepilogo"]["invariati"], 7)
        self.assertIsNone(out["backup_prima_della_scrittura"])           # nessuna riga modificata: nessun backup
        reg = R.Registry.from_snapshot(self.path)
        v2 = {(r["kind"], r["source_uid"]): reg.get_versions(CR.SOURCE, r["kind"], r["source_uid"]) for r in reg.list_records()}
        self.assertEqual(v1, v2)
        self.assertEqual(reg.db.execute("SELECT count(*) FROM events").fetchone()[0], 7)   # nessun evento nuovo

    def test_backup_only_when_rows_are_modified_and_never_deleted(self):
        self.go(commit=True)
        changed = notices(3); changed[0]["cntTitle"] = "Circolare rettificata"
        out = self.go(fx(notice=(lambda: changed, None)), commit=True)
        self.assertIsNotNone(out["backup_prima_della_scrittura"])
        b = out["backup_prima_della_scrittura"]
        self.assertEqual(self.mode(b), 0o600)
        self.assertEqual(self.mode(os.path.dirname(b)), 0o700)
        old = R.Registry.from_snapshot(b)
        self.assertEqual(old.get_record(CR.SOURCE, "notice", "1")["raw"]["cntTitle"], "Circolare 0")   # il backup e' lo stato PRIMA
        cur = R.Registry.from_snapshot(self.path)
        self.assertEqual([v["version"] for v in cur.get_versions(CR.SOURCE, "notice", "1")], [1, 2])
        self.go(commit=True)                                              # una terza sync non cancella il backup
        self.assertTrue(os.path.exists(b))

    def test_record_gone_is_marked_never_deleted(self):
        self.go(commit=True)
        out = self.go(fx(notice=(lambda: notices(3)[:2], None)), commit=True)
        self.assertEqual(out["riepilogo"]["da_marcare_missing"], 1)
        reg = R.Registry.from_snapshot(self.path)
        self.assertEqual(len(reg.list_records()), 7)
        self.assertEqual(len(reg.list_records(status="missing_in_source")), 1)

    def test_failing_kind_does_not_block_others_and_marks_nothing_missing(self):
        self.go(commit=True)
        def down():
            raise ConnectionError("rete assente")
        out = self.go(fx(notice=(down, None)), commit=True)
        self.assertEqual(out["esito_scrittura"]["notice"]["stato"], "errore")
        self.assertEqual(out["esito_scrittura"]["absence"]["stato"], "ok")
        reg = R.Registry.from_snapshot(self.path)
        self.assertEqual(len(reg.list_records(kind="notice", status="present")), 3)
        self.assertEqual(len(reg.list_records(status="missing_in_source")), 0)
        self.assertEqual(reg.db.execute("SELECT count(*) FROM runs WHERE status='error'").fetchone()[0], 1)

    def test_concurrent_sync_is_refused(self):
        os.makedirs(self.root)
        with S._Lock(os.path.join(self.root, "registry.lock")):
            out = self.go(commit=True)
        self.assertEqual(out["mode"], "COMMIT RIFIUTATO")
        self.assertIn("in corso", out["esito"])
        self.assertFalse(os.path.exists(self.path))

    def test_existing_directories_are_not_chmodded(self):
        os.makedirs(os.path.dirname(self.root), mode=0o755)
        os.chmod(os.path.dirname(self.root), 0o755)
        self.go(commit=True)
        self.assertEqual(self.mode(os.path.dirname(self.root)), 0o755)    # pre-esistente: non toccata
        self.assertEqual(self.mode(self.root), 0o700)

    def test_unreadable_baseline_stops_everything(self):
        os.makedirs(self.root)
        with open(self.path, "wb") as fh:
            fh.write(b"non e' sqlite" * 20)
        before = sha(self.path)
        out = self.go(commit=True)
        self.assertIn("errore_baseline", out)
        self.assertFalse(out["scritture"]["sqlite_reale"])
        self.assertEqual(sha(self.path), before)

    def test_single_fetch_plan_and_write_use_same_records(self):
        calls = {"n": 0}
        def once():
            calls["n"] += 1
            return notices(3)
        self.go(fx(notice=(once, None)), commit=True)
        self.assertEqual(calls["n"], 1)


class EndToEndTool(unittest.TestCase):
    ENV = ("CLASSEVIVA_BASE", "CLASSEVIVA_USER", "CLASSEVIVA_PASS", "CLOUD_SCHOOL_DB")

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in self.ENV}
        _, base = TB.start_mock()
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "Cloud", "school", "registry.sqlite3")
        os.environ.update(CLASSEVIVA_BASE=base, CLASSEVIVA_USER="S1234567X", CLASSEVIVA_PASS="ok", CLOUD_SCHOOL_DB=self.db)
        TB.STATE.clear(); TB.STATE.update(TB.fresh_state())
        TB.STATE["notices"] = [{"pubId": 5, "cntTitle": "Circolare n. 1", "pubDT": TB.d(-3) + "T08:00:00+02:00", "cntCategory": "", "readStatus": True},
                               {"pubId": 6, "cntTitle": "Circolare n. 2", "pubDT": TB.d(-2) + "T08:00:00+02:00", "cntCategory": "", "readStatus": False}]
        self.s = TB.srv()

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_tool_defaults_are_safe_and_there_is_no_path_parameter(self):
        t = self.s.srv._tools["cv_registry_sync"]["def"]["inputSchema"]
        self.assertEqual(sorted(t["properties"]), ["commit", "expect_create", "kinds", "migrate", "prune_backups"])
        self.assertEqual(t["properties"]["commit"]["type"], "boolean")
        d = inspect.signature(self.s.cv_registry_sync).parameters
        self.assertIs(d["commit"].default, False)
        self.assertNotIn("path", json.dumps(t).lower())

    def test_shared_helper_feeds_both_tools(self):
        f = self.s._registry_fetchers()
        self.assertEqual(sorted(f), ["absence", "agenda_event", "grade", "lesson", "notice"])

    def test_default_call_does_not_create_the_database(self):
        out = self.s.srv.call("cv_registry_sync", {})
        json.dumps(out, ensure_ascii=False)
        self.assertFalse(out["commit"])
        self.assertEqual(out["riepilogo"]["record_da_creare"], 6 + 2 + 1)          # 6 eventi + 2 comunicazioni + 1 assenza
        self.assertFalse(os.path.exists(os.path.dirname(os.path.dirname(self.db))))
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_string_true_is_rejected_by_the_tool(self):
        with self.assertRaises(ValueError):
            self.s.srv.call("cv_registry_sync", {"commit": "true"})
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_commit_through_the_tool_then_dry_run_sees_everything_unchanged(self):
        out = self.s.srv.call("cv_registry_sync", {"commit": True, "expect_create": 9})
        self.assertEqual(out["esito"], "SCRITTO E VERIFICATO", json.dumps(out.get("verifica_post_scrittura"), indent=1))
        dry = self.s.srv.call("cv_registry_dryrun", {"simulate": False})
        self.assertEqual(dry["baseline"]["record_caricati"], 9)
        r = dry["RISULTATO_REALE"]["letture"]
        self.assertEqual(sum(r[k]["conteggi"]["unchanged"] for k in r), 9)
        self.assertEqual(sum(r[k]["conteggi"]["create"] for k in r), 0)
        self.assertTrue(dry["verifica_nessuna_scrittura"]["stato_prima_uguale_a_dopo"])


if __name__ == "__main__":
    unittest.main()
