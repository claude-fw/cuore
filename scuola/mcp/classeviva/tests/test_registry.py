"""Registro locale: creazione, aggiornamento, invariato, scomparsa, hash/versioni, RAW conservato e immutabile."""
import itertools, json, os, sqlite3, stat, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import registry as R

S, K = "src", "thing"


def clock():
    c = itertools.count(1)
    return lambda: "2026-10-31T10:00:%02d+00:00" % next(c)


def it(uid, date="2026-10-29", **raw):
    raw.setdefault("title", "t-" + uid)
    return {"source_uid": uid, "date": date, "raw": raw}


class Base(unittest.TestCase):
    def setUp(self):
        self.r = R.Registry(":memory:", clock=clock())

    def ing(self, items, **kw):
        return self.r.ingest(S, K, items, **kw)


class CreazioneAggiornamento(Base):
    def test_create_then_unchanged(self):
        a = self.ing([it("1"), it("2")])
        self.assertEqual(a["counts"]["create"], 2)
        b = self.ing([it("1"), it("2")])
        self.assertEqual((b["counts"]["create"], b["counts"]["update"], b["counts"]["unchanged"]), (0, 0, 2))
        self.assertEqual(self.r.get_record(S, K, "1")["version"], 1)        # invariato = nessuna nuova versione

    def test_update_creates_new_version_and_keeps_old_raw(self):
        self.ing([it("1", title="vecchio")])
        res = self.ing([it("1", title="nuovo")])
        self.assertEqual(res["counts"]["update"], 1)
        rec = self.r.get_record(S, K, "1")
        self.assertEqual((rec["version"], rec["raw"]["title"]), (2, "nuovo"))
        vs = self.r.get_versions(S, K, "1")
        self.assertEqual([v["version"] for v in vs], [1, 2])
        self.assertEqual(vs[0]["raw"]["title"], "vecchio")                  # la versione 1 e' intatta
        self.assertNotEqual(vs[0]["content_hash"], vs[1]["content_hash"])

    def test_same_uid_in_different_kinds_does_not_collide(self):
        self.r.ingest(S, "grade", [it("7", title="voto")])
        self.r.ingest(S, "notice", [it("7", title="avviso")])
        self.assertEqual(self.r.get_record(S, "grade", "7")["raw"]["title"], "voto")
        self.assertEqual(self.r.get_record(S, "notice", "7")["raw"]["title"], "avviso")

    def test_legacy_uid_lookup(self):
        x = it("55"); x["legacy_uid"] = "cvv:55"
        self.ing([x])
        self.assertEqual(self.r.find_by_legacy_uid(S, K, "cvv:55")["source_uid"], "55")
        self.assertIsNone(self.r.find_by_legacy_uid(S, K, "cvv:56"))


class RawConservato(Base):
    def test_raw_roundtrip_is_exact(self):
        raw = {"z": 1, "a": [1, {"k": None}, True], "testo": "perché è così è", "n": 12345678901234567890,
               "f": 7.25, "vuoto": "", "extra_non_previsto": {"x": []}}
        self.ing([{"source_uid": "9", "raw": raw}])
        rec = self.r.get_record(S, K, "9")
        self.assertEqual(rec["raw"], raw)
        self.assertEqual(list(rec["raw"]), ["z", "a", "testo", "n", "f", "vuoto", "extra_non_previsto"])   # ordine originale
        self.assertEqual(rec["raw_json"], json.dumps(raw, ensure_ascii=False))

    def test_versions_and_events_are_immutable(self):
        self.ing([it("1")])
        for sql in ("UPDATE versions SET raw_json='{}'", "DELETE FROM versions",
                    "UPDATE events SET event='x'", "DELETE FROM events", "DELETE FROM records"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.r.db.execute(sql)
        self.assertEqual(self.r.get_record(S, K, "1")["raw"]["title"], "t-1")

    def test_non_json_raw_is_rejected_without_writing(self):
        with self.assertRaises(TypeError):
            self.ing([it("1"), {"source_uid": "2", "raw": {"d": {1, 2}}}])
        self.assertEqual(self.r.list_records(), [])                          # niente scritto

    def test_invalid_batches_write_nothing(self):
        for bad in ([it("1"), it("1")], [{"source_uid": "", "raw": {}}], [{"source_uid": "1", "raw": [1]}]):
            with self.assertRaises(ValueError):
                self.ing(bad)
        self.assertEqual(self.r.list_records(), [])
        self.assertEqual(self.r.runs(), [])


class HashEVersioni(Base):
    def test_hash_is_order_independent_and_covers_all_fields(self):
        self.assertEqual(R.content_hash({"a": 1, "b": 2}), R.content_hash({"b": 2, "a": 1}))
        self.assertNotEqual(R.content_hash({"a": 1}), R.content_hash({"a": 1, "campo_nuovo": 0}))
        self.assertEqual(len(R.content_hash({"a": 1})), 64)

    def test_volatile_fields_excluded_from_hash_but_tracked_as_state(self):
        self.ing([it("1", readStatus=False)], volatile=("readStatus",))
        res = self.ing([it("1", readStatus=True)], volatile=("readStatus",))
        self.assertEqual((res["counts"]["state_change"], res["counts"]["update"]), (1, 0))
        rec = self.r.get_record(S, K, "1")
        self.assertEqual(rec["version"], 1)                                  # nessuna nuova versione per il solo stato
        self.assertEqual(rec["volatile"], {"readStatus": True})
        self.assertEqual(rec["raw"]["readStatus"], False)                    # il RAW catturato non e' stato riscritto
        self.assertEqual(rec["hash_excludes"], ["readStatus"])
        again = self.ing([it("1", readStatus=True)], volatile=("readStatus",))
        self.assertEqual(again["counts"]["unchanged"], 1)

    def test_volatile_plus_real_change_makes_new_version_with_current_state(self):
        self.ing([it("1", readStatus=False)], volatile=("readStatus",))
        self.ing([it("1", readStatus=True, title="cambiato")], volatile=("readStatus",))
        rec = self.r.get_record(S, K, "1")
        self.assertEqual((rec["version"], rec["raw"]["readStatus"], rec["volatile"]), (2, True, {"readStatus": True}))

    # ---- campi volatili annidati ("**.nome") -------------------------------------------------------
    @staticmethod
    def nested(thumb="u1", title="doc"):
        return {"title": "t", "thumbnailUrl": "top-" + thumb,
                "materials": [{"driveFile": {"driveFile": {"id": "d1", "title": title, "thumbnailUrl": thumb}}},
                              {"link": {"url": "x"}}],
                "assignmentSubmission": {"attachments": [{"driveFile": {"id": "d2", "thumbnailUrl": thumb}}]}}

    def test_deep_volatile_excluded_at_any_depth(self):
        ex = ("**.thumbnailUrl",)
        self.assertEqual(R.content_hash(self.nested("u1"), ex), R.content_hash(self.nested("u2"), ex))
        self.assertNotEqual(R.content_hash(self.nested("u1")), R.content_hash(self.nested("u2")))
        self.assertNotEqual(R.content_hash(self.nested(title="a"), ex), R.content_hash(self.nested(title="b"), ex))
        stripped = R.strip_volatile(self.nested(), ex)
        self.assertNotIn("thumbnailUrl", json.dumps(stripped))
        self.assertEqual(stripped["materials"][1], {"link": {"url": "x"}})

    def test_strip_volatile_does_not_mutate_raw(self):
        raw = self.nested()
        before = json.dumps(raw)
        R.strip_volatile(raw, ("**.thumbnailUrl", "title"))
        self.assertEqual(json.dumps(raw), before)

    def test_deep_and_top_level_excludes_combine(self):
        s = R.strip_volatile({"a": 1, "readStatus": 1, "x": {"readStatus": 2, "k": [{"t": 1, "z": 2}]}}, ("readStatus", "**.t"))
        self.assertEqual(s, {"a": 1, "x": {"readStatus": 2, "k": [{"z": 2}]}})   # "readStatus" resta solo di 1o livello

    def test_hash_without_deep_excludes_is_unchanged(self):
        import hashlib
        raw = {"b": [1, {"c": "è"}], "a": 1, "readStatus": True}
        for ex in ((), ("readStatus",)):
            body = {k: v for k, v in raw.items() if k not in ex}
            self.assertEqual(R.content_hash(raw, ex), hashlib.sha256(R.canonical_json(body).encode("utf-8")).hexdigest())

    def test_deep_volatile_without_name_rejected(self):
        with self.assertRaises(ValueError):
            R.content_hash({"a": 1}, ("**.",))

    def test_ingest_deep_volatile_no_new_version_and_raw_kept(self):
        ex = ("**.thumbnailUrl",)
        self.ing([{"source_uid": "1", "raw": self.nested("u1")}], volatile=ex)
        res = self.ing([{"source_uid": "1", "raw": self.nested("u2")}], volatile=ex)
        self.assertEqual(res["counts"]["unchanged"], 1)
        self.assertEqual((res["counts"]["update"], res["counts"]["state_change"]), (0, 0))
        rec = self.r.get_record(S, K, "1")
        self.assertEqual(rec["version"], 1)
        self.assertEqual(rec["raw"], self.nested("u1"))                      # RAW originale, thumbnailUrl compreso
        self.assertIsNone(rec["volatile"])                                   # i campi annidati non sono "stato"
        self.assertEqual(rec["hash_excludes"], ["**.thumbnailUrl"])
        self.assertEqual(self.r.verify_integrity(), [])

    def test_ingest_deep_volatile_plus_real_change_new_version_with_new_raw(self):
        ex = ("**.thumbnailUrl",)
        self.ing([{"source_uid": "1", "raw": self.nested("u1")}], volatile=ex)
        res = self.ing([{"source_uid": "1", "raw": self.nested("u2", title="nuovo")}], volatile=ex)
        self.assertEqual(res["counts"]["update"], 1)
        rec = self.r.get_record(S, K, "1")
        self.assertEqual((rec["version"], rec["raw"]), (2, self.nested("u2", title="nuovo")))
        self.assertEqual(self.r.verify_integrity(), [])

    def test_integrity_check_detects_tampering(self):
        self.ing([it("1")])
        self.assertEqual(self.r.verify_integrity(), [])
        self.r.db.execute("DROP TRIGGER versions_no_update")                 # simula corruzione/manomissione esterna
        self.r.db.execute("UPDATE versions SET raw_json='{\"title\":\"alterato\"}'")
        self.assertEqual(len(self.r.verify_integrity()), 1)


class Scomparsa(Base):
    def test_missing_is_marked_never_deleted_and_can_reappear(self):
        self.ing([it("1"), it("2"), it("3")])
        res = self.ing([it("1"), it("2")])
        self.assertEqual(res["keys"]["missing"], ["3"])
        rec = self.r.get_record(S, K, "3")
        self.assertEqual(rec["source_status"], "missing_in_source")
        self.assertEqual(rec["raw"]["title"], "t-3")                         # il RAW resta
        self.assertEqual(len(self.r.list_records()), 3)                      # nessuna cancellazione
        back = self.ing([it("1"), it("2"), it("3")])
        self.assertEqual(back["keys"]["reappeared"], ["3"])
        self.assertEqual(self.r.get_record(S, K, "3")["source_status"], "present")
        self.assertEqual([e["event"] for e in self.r.events(source_uid="3")], ["create", "missing", "reappeared"])

    def test_empty_fetch_does_not_mark_everything_missing(self):
        self.ing([it("1"), it("2")])
        res = self.ing([])
        self.assertEqual(res["counts"]["missing"], 0)
        self.assertEqual(res["counts"]["missing_refused"], 2)
        self.assertTrue(res["warnings"])
        self.assertEqual(len(self.r.list_records(status="present")), 2)
        forced = self.ing([], allow_empty=True)                              # solo se il chiamante lo dichiara
        self.assertEqual(forced["counts"]["missing"], 2)

    def test_suspicious_drop_is_refused(self):
        self.ing([it(str(i)) for i in range(10)])
        res = self.ing([it("0"), it("1")])                                   # 2 su 10: sotto la soglia
        self.assertEqual((res["counts"]["missing"], res["counts"]["missing_refused"]), (0, 8))

    def test_partial_read_never_marks_missing(self):
        self.ing([it("1"), it("2")])
        res = self.ing([it("1")], complete=False)
        self.assertEqual(res["counts"]["missing"], 0)

    def test_window_limits_what_can_be_missing(self):
        self.ing([it("old", date="2026-09-29"), it("a", date="2026-10-30"), it("b", date="2026-10-31"),
                  it("c", date="2026-11-01")])
        res = self.ing([it("a", date="2026-10-30"), it("c", date="2026-11-01")], window=("2026-10-29", "2026-11-28"))
        self.assertEqual(res["keys"]["missing"], ["b"])                      # 'old' e' fuori finestra: non si tocca
        self.assertEqual(self.r.get_record(S, K, "old")["source_status"], "present")


class IsolamentoEErrori(Base):
    def test_failure_of_one_source_does_not_touch_others(self):
        self.r.run_source("A", K, lambda: [it("1")])
        self.r.run_source("B", K, lambda: [it("9")])
        def boom():
            raise ConnectionError("rete assente")
        res = self.r.run_source("A", K, boom)
        self.assertEqual(res["status"], "error")
        self.assertIn("ConnectionError", res["error"])
        self.assertEqual(self.r.get_record("A", K, "1")["source_status"], "present")   # errore != scomparsa
        ok = self.r.run_source("B", K, lambda: [it("9"), it("10")])
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(ok["counts"]["create"], 1)
        statuses = [(x["source"], x["status"]) for x in self.r.runs()]
        self.assertEqual(statuses, [("A", "ok"), ("B", "ok"), ("A", "error"), ("B", "ok")])

    def test_ingest_is_atomic(self):
        self.ing([it("1")])
        class Boom(dict):
            def items(self):
                raise RuntimeError("guasto a meta' scrittura")
        with self.assertRaises(RuntimeError):
            self.ing([it("2"), {"source_uid": "3", "raw": Boom(a=1)}])
        self.assertEqual([x["source_uid"] for x in self.r.list_records()], ["1"])      # nemmeno la "2"
        res = self.r.run_source(S, K, lambda: [{"source_uid": "3", "raw": Boom(a=1)}])
        self.assertEqual(res["status"], "error")

    def test_run_log_records_counts(self):
        self.ing([it("1")])
        run = self.r.runs()[0]
        self.assertEqual(run["status"], "ok")
        self.assertEqual(json.loads(run["counts_json"])["create"], 1)


class Persistenza(unittest.TestCase):
    def test_file_db_survives_reopen_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "reg.sqlite3")
            a = R.Registry(p)
            a.ingest(S, K, [it("1")])
            a.close()
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            b = R.Registry(p)
            self.assertEqual(b.get_record(S, K, "1")["raw"]["title"], "t-1")
            b.close()

    def test_future_schema_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "reg.sqlite3")
            R.Registry(p).close()
            c = sqlite3.connect(p)
            c.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
            c.commit(); c.close()
            with self.assertRaises(R.RegistryError):
                R.Registry(p)

    def test_default_path_is_outside_icloud_and_creates_nothing(self):
        p = R.default_path()
        self.assertNotIn("Mobile Documents", p)
        self.assertNotIn("iCloud", p)


class Proiezioni(Base):
    def test_plan_create_update_reinterpret_unchanged_missing(self):
        self.ing([it("1"), it("2"), it("3")])
        p = self.r.projection_plan(S, K, "destinazione", "r1-aaa")
        self.assertEqual(p["create"], ["1", "2", "3"])
        for uid in ("1", "2", "3"):
            rec = self.r.get_record(S, K, uid)
            self.r.record_projection(S, K, uid, "destinazione", "page-" + uid, rec["content_hash"], "r1-aaa")
        self.assertEqual(self.r.projection_plan(S, K, "destinazione", "r1-aaa")["unchanged"], ["1", "2", "3"])
        self.assertEqual(self.r.projection_plan(S, K, "destinazione", "r2-bbb")["reinterpret"], ["1", "2", "3"])
        self.ing([it("1", title="cambiato"), it("2")])                       # 1 cambia, 3 sparisce
        p = self.r.projection_plan(S, K, "destinazione", "r1-aaa")
        self.assertEqual((p["update"], p["unchanged"], p["mark_missing"]), (["1"], ["2"], ["3"]))
        self.assertNotIn("delete", " ".join(p))                              # il piano non contiene cancellazioni

    def test_projection_state_is_separate_from_raw(self):
        self.ing([it("1")])
        h = self.r.get_record(S, K, "1")["content_hash"]
        self.r.record_projection(S, K, "1", "destinazione", "p1", h, "r1")
        self.assertEqual(self.r.get_record(S, K, "1")["version"], 1)
        self.assertEqual(self.r.verify_integrity(), [])


if __name__ == "__main__":
    unittest.main()
