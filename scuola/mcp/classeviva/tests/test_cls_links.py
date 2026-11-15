"""LINK Classroom in risposta DEGRADATA (verificato dal vivo: un link con titolo valido registrato e' tornato
prima senza titolo, poi con il titolo anti-bot "Just a moment..."). Niente versioni fantasma, niente titoli inventati,
le modifiche reali restano modifiche; la protezione vale nel dry-run e nella sincronizzazione (anche nella verifica)."""
import copy
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "..", "_common"))
import cls_registry as CL   # noqa: E402
import cvv_dryrun as D      # noqa: E402
import cvv_sync as S        # noqa: E402
import registry as R        # noqa: E402

URL = "https://example.org/pagina"
TITLE = "Pagina del corso con le istruzioni (SINT.)"
K = "announcement"
UID = "1001/a9"


def ann(link=None, extra_links=(), text="Leggete la pagina (SINT.)"):
    lk = {"url": URL, "title": TITLE, "thumbnailUrl": "https://thumb.example/1"} if link is None else link
    return {"courseId": "1001", "id": "a9", "text": text, "state": "PUBLISHED",
            "creationTime": "2026-10-29T10:00:00Z", "updateTime": "2026-10-29T10:00:00Z",
            "materials": [{"link": lk}] + [{"link": x} for x in extra_links]}


NO_TITLE = {"url": URL}                                               # forma osservata: niente title ne' thumbnailUrl
ANTIBOT = {"url": URL, "title": "Just a moment...", "thumbnailUrl": "https://thumb.example/cf"}


class Base(unittest.TestCase):
    def setUp(self):
        self.reg = R.Registry(":memory:")
        self.addCleanup(self.reg.close)

    def sync(self, raw):
        return CL.sync_kind(self.reg, K, lambda: [copy.deepcopy(raw)])

    def rec(self):
        return self.reg.get_record(CL.SOURCE, K, UID)

    def link(self):
        return CL._links(self.rec()["raw"])[0]


class RiconoscimentoAntiBot(unittest.TestCase):
    def test_known_interstitial_titles(self):
        for t in ("Just a moment...", "Just a moment…", "  just  a moment...  ", "Attention Required! | Cloudflare",
                  "Please Wait... | Cloudflare", "Checking your browser before accessing example.org", "DDoS-Guard"):
            self.assertTrue(CL.antibot_title(t), t)

    def test_valid_titles_are_not_rejected(self):
        for t in ("Just a moment in history", "Un momento, prego: istruzioni", "Moment of inertia", "Cloudflare Workers docs",
                  "Verifica di matematica", "Please wait for the bell", None, 7):
            self.assertFalse(CL.antibot_title(t), t)


class LinkDegradati(Base):
    def test_bug_reproduced_without_repair(self):
        """Senza riparazione (ingest diretto) un titolo anti-bot crea una versione fantasma: e' il bug osservato."""
        self.reg.ingest(CL.SOURCE, K, CL.to_items(K, [ann()]), volatile=CL.VOLATILE[K])
        res = self.reg.ingest(CL.SOURCE, K, CL.to_items(K, [ann(ANTIBOT)]), volatile=CL.VOLATILE[K])
        self.assertEqual(res["counts"]["update"], 1)

    def test_missing_title_with_valid_history_is_restored(self):
        self.sync(ann())
        res = self.sync(ann(NO_TITLE))
        self.assertEqual(res["counts"].get("update", 0), 0)
        self.assertEqual((self.rec()["version"], self.link()["title"]), (1, TITLE))
        self.assertTrue(any("titolo ripristinato" in w for w in res["warnings"]))

    def test_antibot_title_with_valid_history_is_restored(self):
        self.sync(ann())
        res = self.sync(ann(ANTIBOT))
        self.assertEqual(res["counts"].get("update", 0), 0)
        self.assertEqual((self.rec()["version"], self.link()["title"]), (1, TITLE))

    def test_transient_sequence_has_no_ghost_versions(self):
        self.sync(ann())
        for lk in (NO_TITLE, ANTIBOT, None, ANTIBOT, NO_TITLE, None):
            res = self.sync(ann(lk))
            self.assertEqual(res["counts"].get("update", 0), 0)
        self.assertEqual(self.rec()["version"], 1)
        self.assertEqual([e["event"] for e in self.reg.events(source_uid=UID)], ["create"])

    def test_first_read_antibot_invents_nothing_and_is_flagged(self):
        res = self.sync(ann(ANTIBOT))
        self.assertEqual(res["counts"]["create"], 1)
        self.assertEqual(self.link()["title"], "Just a moment...")            # RAW come letto, nulla inventato
        m = CL.normalized(K, self.rec()["raw"])["materials"][0]
        self.assertEqual((m["title"], m["titolo_non_determinabile"]), (None, "titolo_antibot_della_fonte"))
        self.assertTrue(any("NON DETERMINABILE" in w for w in res["warnings"]))

    def test_degraded_history_degraded_read_no_ghost_version(self):
        self.sync(ann(NO_TITLE))                       # prima lettura gia' degradata
        res = self.sync(ann(ANTIBOT))                  # degradato -> degradato: non e' un cambiamento
        self.assertEqual(res["counts"].get("update", 0), 0)
        self.assertNotIn("title", self.link())
        res = self.sync(ann(NO_TITLE))
        self.assertEqual(res["counts"].get("update", 0), 0)

    def test_degraded_history_then_valid_title_is_a_real_update(self):
        self.sync(ann(NO_TITLE))
        res = self.sync(ann())
        self.assertEqual(res["counts"]["update"], 1)
        self.assertEqual(self.link()["title"], TITLE)

    def test_legit_title_change_is_a_change(self):
        self.sync(ann())
        res = self.sync(ann({"url": URL, "title": "Nuovo titolo (SINT.)", "thumbnailUrl": "https://thumb.example/1"}))
        self.assertEqual(res["counts"]["update"], 1)
        self.assertEqual(self.link()["title"], "Nuovo titolo (SINT.)")

    def test_real_link_change_is_a_change_and_not_repaired(self):
        self.sync(ann())
        res = self.sync(ann({"url": "https://example.org/altra", "title": "Just a moment..."}))
        self.assertEqual(res["counts"]["update"], 1)
        self.assertEqual(self.link()["url"], "https://example.org/altra")
        self.assertEqual(self.link()["title"], "Just a moment...")            # url nuovo: nessun titolo preso altrove

    def test_other_fields_change_with_degraded_link_keeps_valid_title(self):
        self.sync(ann())
        res = self.sync(ann(ANTIBOT, text="Testo cambiato (SINT.)"))
        self.assertEqual(res["counts"]["update"], 1)
        self.assertEqual((self.rec()["raw"]["text"], self.link()["title"]), ("Testo cambiato (SINT.)", TITLE))

    def test_duplicate_urls_are_not_guessed(self):
        same = {"url": URL, "title": TITLE}
        self.sync(ann(same, extra_links=[dict(same)]))
        out, fixed, undetermined = CL.repair_links(K, ann(NO_TITLE, extra_links=[dict(same)]), self.rec()["raw"])
        self.assertEqual((fixed, undetermined), (0, 1))

    def test_input_not_mutated(self):
        prev, live = ann(), ann(ANTIBOT)
        snap = copy.deepcopy(live)
        out, fixed, _ = CL.repair_links(K, live, prev)
        self.assertEqual(live, snap)
        self.assertEqual((fixed, CL._links(out)[0]["title"]), (1, TITLE))
        same, f2, u2 = CL.repair_links(K, prev, None)
        self.assertIs(same, prev)
        self.assertEqual((f2, u2), (0, 0))

    def test_course_work_and_submission_attachment_links(self):
        work = copy.deepcopy(__import__("cls_fixtures").WORK[0])
        CL.sync_kind(self.reg, "course_work", lambda: [copy.deepcopy(work)])
        deg = copy.deepcopy(work)
        deg["materials"][1]["link"] = {"url": "https://example.org/x"}
        res = CL.sync_kind(self.reg, "course_work", lambda: [deg])
        self.assertEqual(res["counts"].get("update", 0), 0)
        sub = {"courseId": "1001", "courseWorkId": "w1", "id": "s9", "state": "TURNED_IN",
               "assignmentSubmission": {"attachments": [{"link": {"url": URL, "title": TITLE}}]}}
        CL.sync_kind(self.reg, "submission", lambda: [copy.deepcopy(sub)])
        sub2 = copy.deepcopy(sub)
        sub2["assignmentSubmission"]["attachments"][0]["link"]["title"] = "Just a moment..."
        self.assertEqual(CL.sync_kind(self.reg, "submission", lambda: [sub2])["counts"].get("update", 0), 0)

    def test_drive_repair_unchanged(self):
        work = copy.deepcopy(__import__("cls_fixtures").WORK[0])
        CL.sync_kind(self.reg, "course_work", lambda: [copy.deepcopy(work)])
        deg = copy.deepcopy(work)
        df = deg["materials"][0]["driveFile"]["driveFile"]
        df.pop("title")
        df["alternateLink"] = "https://drive.google.com/open?id=d1"
        res = CL.sync_kind(self.reg, "course_work", lambda: [deg])
        self.assertEqual(res["counts"].get("update", 0), 0)
        self.assertTrue(any("file Drive in risposta degradata" in w for w in res["warnings"]))


class DryRunESincronizzazione(unittest.TestCase):
    """La protezione opera nel dry-run e nel commit su registro TEMPORANEO; la verifica post-scrittura non da' falsi negativi."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "Cloud", "school", "registry.sqlite3")

    def f(self, raw):
        return {K: (lambda: [copy.deepcopy(raw)], None)}

    def test_dry_run_and_commit(self):
        out = S.run(self.f(ann()), kinds=[K], commit=True, expect_create=1, db_path=self.path, adapter=CL)
        self.assertEqual(out["esito"], "SCRITTO E VERIFICATO")
        for lk in (ANTIBOT, NO_TITLE):
            dry = D.run(self.f(ann(lk)), kinds=[K], simulate=False, baseline_path=self.path, adapter=CL)
            lett = dry["RISULTATO_REALE"]["letture"][K]
            self.assertEqual((lett["conteggi"]["update"], lett["conteggi"]["unchanged"]), (0, 1))
            out = S.run(self.f(ann(lk)), kinds=[K], commit=True, expect_create=0, db_path=self.path, adapter=CL)
            self.assertEqual(out["esito"], "SCRITTO E VERIFICATO", json.dumps(out.get("verifica_post_scrittura"), indent=1))
            self.assertEqual(out["esito_scrittura"][K]["conteggi"], {"unchanged": 1})
        reg = R.Registry.from_snapshot(self.path)
        try:
            rec = reg.get_record(CL.SOURCE, K, UID)
            self.assertEqual((rec["version"], CL._links(rec["raw"])[0]["title"]), (1, TITLE))
        finally:
            reg.close()


if __name__ == "__main__":
    unittest.main()
