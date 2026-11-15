import copy
import itertools
import json
import os
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import cls_client as C      # noqa: E402
import cls_fixtures as F    # noqa: E402
import cls_registry as CL   # noqa: E402
import cvv_dryrun as D      # noqa: E402
import cvv_sync as S        # noqa: E402
import registry as R        # noqa: E402


RealClient = C.ClassroomClient


def client(fc, **kw):
    return C.ClassroomClient(fc.token_provider, transport=fc.transport, sleep=fc.sleep, page_size=2, **kw)


class Normalizers(unittest.TestCase):
    def test_uid_scheme_and_uniqueness(self):
        self.assertEqual(CL.normalized("course", F.COURSE_A)["uid"], "cls:1001")
        self.assertEqual(CL.normalized("course_work", F.WORK[0])["uid"], "cls:1001/w1")
        self.assertEqual(CL.normalized("topic", F.TOPICS[0])["uid"], "cls:1001/t1")
        self.assertEqual(CL.source_uid_of("cls:1001/w1"), "1001/w1")
        # stesso id in corsi diversi => uid diversi
        a = dict(F.WORK[0]); b = dict(F.WORK[0], courseId="1002")
        self.assertNotEqual(CL.raw_source_uid("course_work", a), CL.raw_source_uid("course_work", b))

    def test_missing_ids_rejected(self):
        for kind, raw in (("course", {"name": "x"}), ("course_work", {"id": "w"}), ("topic", {"courseId": "1"})):
            with self.assertRaises(ValueError):
                CL.to_items(kind, [raw])

    def test_normalizers_do_not_mutate_raw(self):
        for kind, raws in (("course", [F.COURSE_A]), ("course_work", F.WORK), ("material", F.MATERIALS),
                           ("announcement", F.ANNOUNCEMENTS), ("topic", F.TOPICS), ("submission", F.SUBMISSIONS)):
            for r in raws:
                before = copy.deepcopy(r)
                CL.normalized(kind, r)
                self.assertEqual(r, before)

    def test_due_winter_time_converted_to_rome(self):
        due = CL.normalized("course_work", F.WORK[0])["due"]        # 2026-11-27 22:59 UTC -> CET (+1)
        self.assertEqual(due["at_utc"], "2026-11-27T22:59:00Z")
        self.assertEqual(due["at_local"], "2026-11-27T23:59")
        self.assertEqual(due["date_local"], "2026-11-27")

    def test_due_summer_time_converted_to_rome(self):
        due = CL.normalized("course_work", F.WORK[1])["due"]        # 2026-06-05 21:59 UTC -> CEST (+2)
        self.assertEqual(due["at_local"], "2026-06-05T23:59")

    def test_due_crossing_midnight_changes_local_date(self):
        raw = dict(F.WORK[0], dueDate={"year": 2026, "month": 12, "day": 29}, dueTime={"hours": 23, "minutes": 30})
        due = CL.normalized("course_work", raw)["due"]
        self.assertEqual(due["date_utc"], "2026-12-29")
        self.assertEqual(due["date_local"], "2026-12-30")

    def test_due_date_only_and_none(self):
        d = CL.normalized("course_work", F.WORK[3])["due"]
        self.assertEqual((d["date_local"], d["at_utc"], d["at_local"]), ("2026-03-20", None, None))
        self.assertIsNone(CL.normalized("course_work", F.WORK[2])["due"])

    def test_due_empty_time_means_midnight_utc(self):
        raw = dict(F.WORK[0], dueDate={"year": 2026, "month": 12, "day": 1}, dueTime={})
        self.assertEqual(CL.normalized("course_work", raw)["due"]["at_utc"], "2026-12-01T00:00:00Z")

    def test_item_date_prefers_due_local_then_creation(self):
        self.assertEqual(CL.normalized("course_work", F.WORK[0])["date"], "2026-11-27")
        self.assertEqual(CL.normalized("course_work", F.WORK[2])["date"], "2026-10-31")

    def test_materials_summary_and_unknown_flag(self):
        mats = CL.normalized("course_work", F.WORK[0])["materials"]
        self.assertEqual([m["type"] for m in mats], ["drive_file", "link"])
        self.assertEqual(mats[0]["url"], "https://drive.example/d1")
        raw = dict(F.WORK[0], materials=[{"novitaGoogle": {"x": 1}}])
        n = CL.normalized("course_work", raw)
        self.assertEqual(n["materials"][0]["type"], "unknown")
        self.assertIn("materiale di tipo sconosciuto", n["interpreted"]["da_verificare"])

    def test_unknown_enum_values_flagged_not_dropped(self):
        n = CL.normalized("course_work", dict(F.WORK[0], state="NUOVO_STATO", workType="TIPO_X"))
        self.assertEqual(n["state"], "NUOVO_STATO")
        self.assertEqual(len(n["interpreted"]["da_verificare"]), 2)

    def test_enrollment_code_not_in_normalized(self):
        self.assertNotIn("synth1", json.dumps(CL.normalized("course", F.COURSE_A)))

    def test_submission_points_are_not_a_school_grade(self):
        n = CL.normalized("submission", F.SUBMISSIONS[1])
        self.assertEqual(n["assigned_points"], 7)
        self.assertIn("NON un voto", n["interpreted"]["nota"])

    def test_interp_version_stable(self):
        self.assertEqual(CL.interp_version(), CL.interp_version())
        self.assertTrue(CL.interp_version().startswith("cls-r1-"))


class RegistryIntegration(unittest.TestCase):
    def setUp(self):
        self.reg = R.Registry(":memory:")

    def test_create_unchanged_update_missing_and_raw_exact(self):
        raws = [copy.deepcopy(w) for w in F.WORK[:3]]
        r = CL.sync_kind(self.reg, "course_work", lambda: raws)
        self.assertEqual(r["counts"]["create"], 3)
        rec = self.reg.get_record("classroom", "course_work", "1001/w1")
        self.assertEqual(rec["raw_json"], json.dumps(F.WORK[0], ensure_ascii=False))
        self.assertIsNone(rec["legacy_uid"])
        self.assertEqual(CL.sync_kind(self.reg, "course_work", lambda: raws)["counts"]["unchanged"], 3)
        raws[0]["title"] = "Titolo cambiato (SINT.)"
        r = CL.sync_kind(self.reg, "course_work", lambda: raws)
        self.assertEqual(r["counts"]["update"], 1)
        vs = self.reg.get_versions("classroom", "course_work", "1001/w1")
        self.assertEqual([v["version"] for v in vs], [1, 2])
        self.assertEqual(vs[0]["raw"], F.WORK[0])          # versione 1 intatta
        r = CL.sync_kind(self.reg, "course_work", lambda: raws[:2])  # 1 su 3 sparito: sopra la soglia 0.5
        self.assertEqual(r["counts"]["missing"], 1)
        self.assertEqual(self.reg.get_record("classroom", "course_work", "1001/w3")["source_status"], "missing_in_source")
        self.assertIsNotNone(self.reg.get_record("classroom", "course_work", "1001/w3")["raw"])
        self.assertEqual(self.reg.verify_integrity(), [])

    def test_empty_read_does_not_mark_missing(self):
        CL.sync_kind(self.reg, "announcement", lambda: [dict(a) for a in F.ANNOUNCEMENTS])
        r = CL.sync_kind(self.reg, "announcement", lambda: [])
        self.assertEqual(r["counts"]["missing"], 0)
        self.assertTrue(r["warnings"])
        self.assertTrue(all(x["source_status"] == "present"
                            for x in self.reg.list_records(source="classroom", kind="announcement")))

    def test_fetch_error_marks_nothing(self):
        CL.sync_kind(self.reg, "announcement", lambda: [dict(a) for a in F.ANNOUNCEMENTS])

        def boom():
            raise C.ClassroomApiError(403, "scope_insufficient", "scope")
        r = CL.sync_kind(self.reg, "announcement", boom)
        self.assertEqual(r["status"], "error")
        self.assertEqual(len(self.reg.list_records(source="classroom", kind="announcement", status="present")), 3)

    def test_sources_stay_separate_in_same_registry(self):
        self.reg.ingest("classeviva", "notice", [{"source_uid": "1001", "legacy_uid": "cvv:1001", "raw": {"a": 1}}])
        CL.sync_kind(self.reg, "course", lambda: [dict(F.COURSE_A)])
        self.assertEqual(len(self.reg.list_records(source="classeviva")), 1)
        self.assertEqual(len(self.reg.list_records(source="classroom")), 1)
        self.assertIsNone(self.reg.find_by_legacy_uid("classroom", "course", "cvv:1001"))
        # stesso source_uid '1001' in due fonti: record distinti, nessuna fusione
        self.assertEqual(self.reg.get_record("classeviva", "notice", "1001")["raw"], {"a": 1})
        self.assertEqual(self.reg.get_record("classroom", "course", "1001")["raw"]["name"], F.COURSE_A["name"])



class Client(unittest.TestCase):
    def test_pagination_and_only_get(self):
        fc = F.FakeClassroom()
        cl = client(fc)
        self.assertEqual(len(cl.announcements("1001")), 3)
        self.assertEqual(len(cl.courses()), 3)
        self.assertTrue(fc.requests and all(r[0] == "GET" for r in fc.requests))
        self.assertTrue(all(r[3] == "Bearer " + F.TOKEN for r in fc.requests))

    def test_returned_records_are_exact_json(self):
        fc = F.FakeClassroom()
        self.assertEqual(client(fc).course_work("1001"), F.WORK[:3])

    def test_submissions_request_shape(self):
        fc = F.FakeClassroom()
        client(fc).submissions("1001")
        paths = [r[1] for r in fc.requests]
        self.assertTrue(all(p == "/v1/courses/1001/courseWork/-/studentSubmissions" for p in paths))
        self.assertTrue(all(r[2]["userId"] == ["me"] for r in fc.requests))

    def test_401_refreshes_once_then_succeeds(self):
        fc = F.FakeClassroom()
        fc.fail.append(["/courses", 401, {"error": {"code": 401, "status": "UNAUTHENTICATED", "message": "x"}}, 1])
        self.assertEqual(len(client(fc).courses()), 3)
        self.assertEqual(len(fc.refreshes), 1)

    def test_401_twice_is_auth_error(self):
        fc = F.FakeClassroom()
        fc.fail.append(["/courses", 401, {"error": {"code": 401, "message": "x"}}, 5])
        with self.assertRaises(C.ClassroomApiError) as cm:
            client(fc).courses()
        self.assertEqual(cm.exception.kind, "auth")
        self.assertEqual(len(fc.refreshes), 1)

    def test_429_retry_after_honoured(self):
        fc = F.FakeClassroom()
        fc.fail.append(["/courses", 429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "q"}}, 2,
                        {"Retry-After": "3"}])
        self.assertEqual(len(client(fc).courses()), 3)
        self.assertEqual(fc.sleeps, [3.0, 3.0])

    def test_5xx_exhausts_retries(self):
        fc = F.FakeClassroom()
        fc.fail.append(["/courses", 503, {"error": {"code": 503, "message": "down"}}, 99])
        with self.assertRaises(C.ClassroomApiError) as cm:
            client(fc, max_retries=2).courses()
        self.assertEqual(cm.exception.kind, "server")
        self.assertEqual(len(fc.sleeps), 2)

    def test_error_classification(self):
        cases = [
            (403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Google Classroom API has not been used in project 1 before or it is disabled.", "details": [{"reason": "SERVICE_DISABLED"}]}}, "api_disabled"),
            (403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Request had insufficient authentication scopes.", "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}, "scope_insufficient"),
            (403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "The caller does not have permission"}}, "permission"),
            (404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "Requested entity was not found."}}, "not_found"),
            (400, {"error": {"code": 400, "message": "bad"}}, "other"),
        ]
        for status, payload, kind in cases:
            self.assertEqual(C.classify(status, payload)[0], kind, payload)

    def test_no_non_get_method_exists_in_module(self):
        src = open(os.path.join(os.path.dirname(HERE), "cls_client.py")).read()
        for m in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"'):
            self.assertNotIn(m, src)


class Fetchers(unittest.TestCase):
    def test_content_only_for_active_and_archived_courses(self):
        fc = F.FakeClassroom()
        fetchers = C.make_fetchers(client(fc))
        self.assertEqual(sorted(fetchers), sorted(CL.ALL_KINDS))
        work = fetchers["course_work"][0]()
        self.assertEqual({w["courseId"] for w in work}, {"1001", "1002"})       # non '1003' (DECLINED)
        self.assertFalse([r for r in fc.requests if "/1003/" in r[1]])

    def test_course_list_read_once_for_content_kinds(self):
        fc = F.FakeClassroom()
        fetchers = C.make_fetchers(client(fc))
        fetchers["course_work"][0](); fetchers["announcement"][0](); fetchers["topic"][0]()
        self.assertEqual(len([r for r in fc.requests if r[1] == "/v1/courses"]), 2)   # 2 pagine, una lettura

    def test_course_kind_always_rereads(self):
        fc = F.FakeClassroom()
        fetchers = C.make_fetchers(client(fc))
        fetchers["course"][0](); n1 = len(fc.requests)
        fetchers["course"][0]()
        self.assertEqual(len(fc.requests), 2 * n1)

    def test_one_failing_course_fails_whole_kind_and_marks_nothing(self):
        fc = F.FakeClassroom()
        reg = R.Registry(":memory:")
        f = C.make_fetchers(client(fc))
        CL.sync_kind(reg, "announcement", f["announcement"][0])
        fc.fail.append(["/1001/announcements", 403, {"error": {"code": 403, "message": "no"}}, 99])
        r = CL.sync_kind(reg, "announcement", f["announcement"][0])
        self.assertEqual(r["status"], "error")
        self.assertEqual(len(reg.list_records(source="classroom", kind="announcement", status="present")), 3)

    def test_scope_missing_on_one_kind_does_not_block_others(self):
        fc = F.FakeClassroom()
        fc.fail.append(["/announcements", 403, {"error": {"code": 403, "message": "Request had insufficient authentication scopes.", "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}, 99])
        res = D.run(C.make_fetchers(client(fc)), adapter=CL, baseline_path=os.path.join(tempfile.mkdtemp(), "x.sqlite3"))
        letture = res["RISULTATO_REALE"]["letture"]
        self.assertEqual(letture["announcement"]["stato"], "errore")
        self.assertEqual(letture["course"]["stato"], "ok")
        self.assertEqual(letture["course_work"]["stato"], "ok")


def with_thumbs(data):
    """Copia dei dati finti con file Drive (e thumbnailUrl) anche in materiali, annunci e consegne."""
    d = copy.deepcopy(data)
    drive = {"driveFile": {"driveFile": {"id": "dx", "title": "f.pdf", "alternateLink": "https://drive.example/dx",
                                         "thumbnailUrl": "https://thumb.example/dx"}, "shareMode": "VIEW"}}
    d["courseWorkMaterials"][0]["materials"].append(copy.deepcopy(drive))
    d["announcements"][0]["materials"] = [copy.deepcopy(drive)]
    d["studentSubmissions"][0]["assignmentSubmission"] = {"attachments": [
        {"driveFile": {"id": "dy", "title": "mio.pdf", "alternateLink": "https://drive.example/dy",
                       "thumbnailUrl": "https://thumb.example/dy"}}]}
    return d


class RotatingThumbs(F.FakeClassroom):
    """Come Google dal vivo: ogni GET restituisce un thumbnailUrl NUOVO (unico campo che cambia tra due letture)."""

    SEQ = itertools.count(1)        # globale: istanze diverse (= letture diverse) non ripetono mai un URL

    def __init__(self, **kw):
        super().__init__(**kw)
        self.data = with_thumbs(self.data)
        self.n = 0

    def transport(self, method, url, headers):
        st, body, h = super().transport(method, url, headers)

        def rot(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == "thumbnailUrl":
                        self.n += 1
                        o[k] = "https://thumb.example/rotated-%d" % next(self.SEQ)
                    else:
                        rot(v)
            elif isinstance(o, list):
                for x in o:
                    rot(x)
        rot(body)
        return st, body, h


class VolatileThumbnail(unittest.TestCase):
    def setUp(self):
        self.reg = R.Registry(":memory:")

    def test_all_classroom_kinds_declare_deep_thumbnail_volatile(self):
        self.assertEqual(set(CL.VOLATILE), set(CL.ALL_KINDS))
        for k in CL.ALL_KINDS:
            self.assertEqual(CL.VOLATILE[k], ("**.thumbnailUrl",))

    def test_rotated_thumbnail_is_unchanged_and_raw_kept_exact(self):
        first = copy.deepcopy(F.WORK[0])
        CL.sync_kind(self.reg, "course_work", lambda: [first])
        second = copy.deepcopy(F.WORK[0])
        second["materials"][0]["driveFile"]["driveFile"]["thumbnailUrl"] = "https://thumb.example/NUOVO"
        r = CL.sync_kind(self.reg, "course_work", lambda: [second])
        self.assertEqual(r["counts"]["unchanged"], 1)
        rec = self.reg.get_record("classroom", "course_work", "1001/w1")
        self.assertEqual(rec["version"], 1)
        self.assertEqual(rec["raw_json"], json.dumps(F.WORK[0], ensure_ascii=False))   # RAW esatto, thumbnail incluso
        self.assertIn("https://thumb.example/d1", rec["raw_json"])
        self.assertEqual(rec["hash_excludes"], ["**.thumbnailUrl"])
        self.assertEqual(self.reg.verify_integrity(), [])

    def test_real_change_still_creates_version(self):
        CL.sync_kind(self.reg, "course_work", lambda: [copy.deepcopy(F.WORK[0])])
        w = copy.deepcopy(F.WORK[0])
        w["materials"][0]["driveFile"]["driveFile"]["title"] = "dispensa-v2.pdf"
        self.assertEqual(CL.sync_kind(self.reg, "course_work", lambda: [w])["counts"]["update"], 1)

    def test_submission_attachment_thumbnail_ignored(self):
        d = with_thumbs(F.FakeClassroom().data)
        s1 = d["studentSubmissions"][0]
        CL.sync_kind(self.reg, "submission", lambda: [copy.deepcopy(s1)])
        s2 = copy.deepcopy(s1)
        s2["assignmentSubmission"]["attachments"][0]["driveFile"]["thumbnailUrl"] = "https://thumb.example/altro"
        self.assertEqual(CL.sync_kind(self.reg, "submission", lambda: [s2])["counts"]["unchanged"], 1)

    def test_normalized_and_projection_ignore_thumbnail(self):
        a, b = copy.deepcopy(F.WORK[0]), copy.deepcopy(F.WORK[0])
        b["materials"][0]["driveFile"]["driveFile"]["thumbnailUrl"] = "https://thumb.example/altro"
        self.assertEqual(CL.normalize_course_work(a), CL.normalize_course_work(b))


class DryRunAndSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "school", "registry.sqlite3")

    def fetchers(self, fc=None):
        self.fc = fc or F.FakeClassroom()
        return C.make_fetchers(client(self.fc))

    def test_dryrun_classroom_writes_nothing_and_labels_source(self):
        res = D.run(self.fetchers(), adapter=CL, baseline_path=self.db, second_read=True)
        self.assertEqual(res["sorgente"], "classroom")
        self.assertFalse(os.path.exists(self.db))
        self.assertTrue(res["verifica_nessuna_scrittura"]["stato_prima_uguale_a_dopo"])
        l = res["RISULTATO_REALE"]["letture"]
        self.assertEqual(l["course"]["conteggi"]["create"], 3)
        self.assertEqual(l["course_work"]["conteggi"]["create"], 4)
        self.assertEqual(l["topic"]["conteggi"]["create"], 1)
        self.assertTrue(all(not v["cambiamenti_reali"] for k, v in res["RISULTATO_REALE"]["secondo_giro"].items() if k in CL.ALL_KINDS))
        self.assertIn("cls:1001/w1", json.dumps(l["course_work"]["uid"]))
        sim = res["SIMULAZIONE_NON_ACCADUTA"]
        self.assertIn("SCENARIO NON ACCADUTO", sim["AVVISO"])
        self.assertFalse(sim["per_kind"]["topic"]["eseguita"])           # 1 record: nulla inventato
        self.assertTrue(sim["per_kind"]["course_work"]["eseguita"])
        self.assertNotIn(F.TOKEN, json.dumps(res))

    def test_dryrun_second_read_with_rotating_thumbnails_reports_no_changes(self):
        res = D.run(self.fetchers(RotatingThumbs()), adapter=CL, baseline_path=self.db, second_read=True)
        self.assertGreater(self.fc.n, 4)                                      # i thumbnail sono davvero ruotati
        sg = res["RISULTATO_REALE"]["secondo_giro"]
        for k in CL.ALL_KINDS:
            self.assertFalse(sg[k]["cambiamenti_reali"], (k, sg[k]))
        self.assertEqual(res["RISULTATO_REALE"]["integrita"], "OK")
        self.assertFalse(os.path.exists(self.db))

    def test_commit_then_rotated_thumbnails_unchanged_and_verified(self):
        out = S.run(self.fetchers(RotatingThumbs()), adapter=CL, db_path=self.db, commit=True, expect_create=15)
        self.assertEqual(out["esito"], "SCRITTO E VERIFICATO")
        out2 = S.run(self.fetchers(RotatingThumbs()), adapter=CL, db_path=self.db, commit=True, expect_create=0)
        self.assertEqual(out2["riepilogo"]["record_da_aggiornare"], 0, json.dumps(out2["riepilogo"]))
        self.assertEqual(out2["riepilogo"]["invariati"], 15)
        reg = R.Registry.from_snapshot(self.db)
        try:
            self.assertEqual(reg.verify_integrity(), [])
            self.assertTrue(all(r["current_version"] == 1 for r in reg.list_records(source="classroom")))
            raw = reg.get_record("classroom", "material", "1001/m1")["raw"]
            self.assertIn("thumbnailUrl", json.dumps(raw))                    # RAW conservato con il thumbnail
        finally:
            reg.close()

    def test_real_change_among_rotated_thumbnails_writes_one_version_and_verifies(self):
        S.run(self.fetchers(RotatingThumbs()), adapter=CL, db_path=self.db, commit=True, expect_create=15)
        fc = RotatingThumbs()
        fc.data["courseWork"][1]["title"] = "Verifica spostata (SINT.)"     # w2 cambia davvero; w1 solo thumbnail
        out = S.run(self.fetchers(fc), adapter=CL, db_path=self.db, commit=True, expect_create=0)
        self.assertEqual(out["riepilogo"]["record_da_aggiornare"], 1, json.dumps(out["riepilogo"]))
        self.assertEqual(out["esito"], "SCRITTO E VERIFICATO",
                         json.dumps(out.get("verifica_post_scrittura"), indent=1, default=str)[:1500])

    def test_sync_default_is_simulation(self):
        out = S.run(self.fetchers(), adapter=CL, db_path=self.db)
        self.assertEqual(out["mode"], "SYNC SIMULATO (commit=false)")
        self.assertEqual(out["sorgente"], "classroom")
        self.assertFalse(os.path.exists(os.path.dirname(self.db)))
        self.assertEqual(out["riepilogo"]["record_da_creare"], 3 + 4 + 2 + 3 + 1 + 2)

    def test_sync_commit_writes_verifies_and_is_idempotent(self):
        out = S.run(self.fetchers(), adapter=CL, db_path=self.db, commit=True, expect_create=15)
        self.assertEqual(out["esito"], "SCRITTO E VERIFICATO", json.dumps(out["verifica_post_scrittura"], indent=1)[:1500])
        self.assertEqual(stat.S_IMODE(os.stat(self.db).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.db)).st_mode), 0o700)
        out2 = S.run(self.fetchers(), adapter=CL, db_path=self.db, commit=True, expect_create=0)
        self.assertEqual(out2["riepilogo"]["record_da_creare"], 0)
        self.assertEqual(out2["riepilogo"]["invariati"], 15)
        self.assertIsNone(out2["backup_prima_della_scrittura"])
        reg = R.Registry.from_snapshot(self.db)
        try:
            self.assertEqual(reg.verify_integrity(), [])
            self.assertEqual(len(reg.list_records(source="classroom")), 15)
            self.assertEqual(reg.list_records(source="classeviva"), [])
        finally:
            reg.close()
        self.assertNotIn(F.TOKEN.encode(), open(self.db, "rb").read())

    def test_sync_expect_create_guard(self):
        out = S.run(self.fetchers(), adapter=CL, db_path=self.db, commit=True, expect_create=99)
        self.assertEqual(out["mode"], "COMMIT RIFIUTATO")
        self.assertFalse(os.path.exists(self.db))

    def test_change_in_classroom_creates_version_and_backup(self):
        S.run(self.fetchers(), adapter=CL, db_path=self.db, commit=True, expect_create=15)
        fc = F.FakeClassroom()
        fc.data = copy.deepcopy(fc.data)
        fc.data["courseWork"][0]["title"] = "Titolo modificato (SINT.)"
        out = S.run(self.fetchers(fc), adapter=CL, db_path=self.db, commit=True, expect_create=0)
        self.assertEqual(out["riepilogo"]["record_da_aggiornare"], 1)
        self.assertTrue(out["backup_prima_della_scrittura"])
        self.assertEqual(out["esito"], "SCRITTO E VERIFICATO")

    def test_classeviva_default_unaffected(self):
        import cvv_registry as CR
        self.assertIs(D.CR, CR)
        self.assertEqual(D.ALL_KINDS, ("agenda_event", "grade", "absence", "notice"))


class BridgeTools(unittest.TestCase):
    """Strumenti cls_* del server MCP: mai rete reale, mai Keychain reale (patch)."""

    def setUp(self):
        import server
        from unittest import mock
        self.server, self.mock = server, mock
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "school", "registry.sqlite3")
        p = mock.patch.dict(os.environ, {"CLOUD_SCHOOL_DB": self.db})
        p.start(); self.addCleanup(p.stop)

    def patched(self, fc):
        factory = lambda tp: RealClient(tp, transport=fc.transport, sleep=fc.sleep, page_size=2)   # noqa: E731
        return (self.mock.patch.object(self.server.cauth, "token_provider", lambda *a, **k: fc.token_provider),
                self.mock.patch.object(self.server.ccl, "ClassroomClient", factory))

    def test_unauthorized_dryrun_reports_per_kind_error_and_writes_nothing(self):
        import cls_auth

        def deny(*a, **k):
            def p(force=False):
                raise cls_auth.AuthError("no_refresh_token", "Classroom non autorizzato: esegui authorize")
            return p
        with self.mock.patch.object(self.server.cauth, "token_provider", deny):
            res = self.server.cls_registry_dryrun(kinds=["course", "course_work"], simulate=False)
        letture = res["RISULTATO_REALE"]["letture"]
        self.assertEqual({k: v["stato"] for k, v in letture.items()}, {"course": "errore", "course_work": "errore"})
        self.assertIn("non autorizzato", letture["course"]["errore"])
        self.assertFalse(os.path.exists(self.db))

    def test_dryrun_and_default_sync_through_server_functions_write_nothing(self):
        fc = F.FakeClassroom()
        a, b = self.patched(fc)
        with a, b:
            res = self.server.cls_registry_dryrun(kinds=["course"], simulate=False)
            self.assertEqual(res["sorgente"], "classroom")
            self.assertEqual(res["RISULTATO_REALE"]["letture"]["course"]["conteggi"]["create"], 3)
            out = self.server.cls_registry_sync(kinds=["course"])
            self.assertEqual(out["mode"], "SYNC SIMULATO (commit=false)")
        self.assertFalse(os.path.exists(self.db))
        self.assertTrue(all(r[0] == "GET" for r in fc.requests))

    def test_commit_through_server_function_writes_classroom_source_only(self):
        fc = F.FakeClassroom()
        a, b = self.patched(fc)
        with a, b:
            out = self.server.cls_registry_sync(kinds=["course", "topic"], commit=True, expect_create=4)
        self.assertEqual(out["esito"], "SCRITTO E VERIFICATO")
        reg = R.Registry.from_snapshot(self.db)
        try:
            self.assertEqual({r["source"] for r in reg.list_records()}, {"classroom"})
        finally:
            reg.close()

    def test_status_offline_has_no_secrets(self):
        kc = type("K", (), {"get": lambda s, a, b: None})()
        with self.mock.patch.object(self.server.cauth, "MacKeychain", lambda: kc):
            res = self.server.cls_status(online=False)
        self.assertEqual(res["keychain"], {"client_id": False, "client_secret": False, "refresh_token": False})

    def test_tool_schemas(self):
        d = self.server.srv._tools["cls_registry_sync"]["def"]
        self.assertEqual(sorted(d["inputSchema"]["properties"]["kinds"]["items"]["enum"]), sorted(CL.ALL_KINDS))
        self.assertIn("SOLO con commit=true", d["description"])
        import inspect
        self.assertIs(inspect.signature(self.server.cls_registry_sync).parameters["commit"].default, False)
        self.assertIs(inspect.signature(self.server.cls_registry_dryrun).parameters["second_read"].default, False)


if __name__ == "__main__":
    unittest.main()
