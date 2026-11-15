"""Risposte Classroom DEGRADATE (verificato dal vivo: pochi file Drive su centinaia in due letture ravvicinate):
file Drive senza `title` e con `alternateLink` `open?id=`. Non devono produrre versioni fantasma ne' sovrascrivere i dati completi
gia' registrati; le modifiche reali restano modifiche; nessun valore inventato."""
import copy
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import cls_fixtures as F    # noqa: E402
import cls_registry as CL   # noqa: E402
import registry as R        # noqa: E402

UID = "1001/w1"


def degrade(raw, file_id="d1"):
    """Come la risposta reale degradata: niente titolo, link open?id, il resto identico."""
    r = copy.deepcopy(raw)
    for df in CL._drive_files(r):
        if df.get("id") == file_id:
            df.pop("title", None)
            df["alternateLink"] = "https://drive.google.com/open?id=%s" % file_id
    return r


def submission_with_file(title=True):
    s = copy.deepcopy(F.SUBMISSIONS[0])
    df = {"id": "d7", "alternateLink": "https://drive.google.com/file/d/d7/view?usp=drive_web", "thumbnailUrl": "https://t/x"}
    if title:
        df["title"] = "consegna.pdf"
    s["assignmentSubmission"] = {"attachments": [{"driveFile": df}]}
    return s


class Degraded(unittest.TestCase):
    def setUp(self):
        self.reg = R.Registry(":memory:")
        self.work = copy.deepcopy(F.WORK)

    def tearDown(self):
        self.reg.close()

    def sync(self, kind, raws):
        return CL.sync_kind(self.reg, kind, lambda: copy.deepcopy(raws))

    def rec(self, kind="course_work", uid=UID):
        return self.reg.get_record(CL.SOURCE, kind, uid)

    def test_real_case_degraded_is_not_a_new_version(self):
        self.assertEqual(self.sync("course_work", self.work)["counts"]["create"], 4)
        first = self.rec()
        res = self.sync("course_work", [degrade(self.work[0])] + self.work[1:])
        self.assertEqual(res["counts"].get("update", 0), 0)
        self.assertEqual(self.rec()["version"], first["version"])
        self.assertEqual(self.rec()["raw_json"], first["raw_json"])             # dati completi intatti
        self.assertTrue(any("ricostruiti" in w for w in res["warnings"]))

    def test_back_to_complete_after_degraded_no_ghost(self):
        self.sync("course_work", self.work)
        self.sync("course_work", [degrade(self.work[0])] + self.work[1:])
        res = self.sync("course_work", self.work)
        self.assertEqual(res["counts"].get("update", 0), 0)
        self.assertEqual(self.rec()["version"], 1)

    def test_real_change_with_degraded_file_keeps_full_file_data(self):
        self.sync("course_work", self.work)
        changed = degrade(self.work[0])
        changed["title"] = "Esercizi cap. 4 (SINT.)"                            # modifica reale del compito
        res = self.sync("course_work", [changed] + self.work[1:])
        self.assertEqual(res["counts"]["update"], 1)
        rec = self.rec()
        self.assertEqual(rec["raw"]["title"], "Esercizi cap. 4 (SINT.)")
        df = CL._drive_files(rec["raw"])[0]
        self.assertEqual((df["title"], df["alternateLink"]), ("dispensa.pdf", "https://drive.example/d1"))

    def test_real_file_rename_is_a_change(self):
        self.sync("course_work", self.work)
        renamed = copy.deepcopy(self.work[0])
        CL._drive_files(renamed)[0]["title"] = "dispensa-v2.pdf"
        res = self.sync("course_work", [renamed] + self.work[1:])
        self.assertEqual(res["counts"]["update"], 1)
        self.assertEqual(CL._drive_files(self.rec()["raw"])[0]["title"], "dispensa-v2.pdf")

    def test_real_link_change_with_missing_title_is_a_change(self):
        self.sync("course_work", self.work)
        moved = degrade(self.work[0])
        CL._drive_files(moved)[0]["alternateLink"] = "https://drive.example/altro"   # non e' la forma degradata
        res = self.sync("course_work", [moved] + self.work[1:])
        self.assertEqual(res["counts"]["update"], 1)
        df = CL._drive_files(self.rec()["raw"])[0]
        self.assertEqual((df["title"], df["alternateLink"]), ("dispensa.pdf", "https://drive.example/altro"))

    def test_first_read_degraded_nothing_invented(self):
        res = self.sync("course_work", [degrade(self.work[0])])
        self.assertEqual(res["counts"]["create"], 1)
        self.assertNotIn("title", CL._drive_files(self.rec()["raw"])[0])
        self.assertTrue(any("nessun valore inventato" in w for w in res["warnings"]))

    def test_different_file_id_is_not_repaired(self):
        self.sync("course_work", self.work)
        other = copy.deepcopy(self.work[0])
        CL._drive_files(other)[0].update(id="d2", alternateLink="https://drive.google.com/open?id=d2")
        CL._drive_files(other)[0].pop("title")
        res = self.sync("course_work", [other] + self.work[1:])
        self.assertEqual(res["counts"]["update"], 1)                            # file diverso: modifica reale
        self.assertNotIn("title", CL._drive_files(self.rec()["raw"])[0])

    def test_submission_attachments(self):
        full = submission_with_file()
        self.sync("submission", [full])
        uid = "1001/s1"
        deg = copy.deepcopy(full)
        df = deg["assignmentSubmission"]["attachments"][0]["driveFile"]
        df.pop("title")
        df["alternateLink"] = "https://drive.google.com/open?id=d7"
        res = self.sync("submission", [deg])
        self.assertEqual(res["counts"].get("update", 0), 0)
        self.assertEqual(self.rec("submission", uid)["version"], 1)

    def test_input_not_mutated_and_no_degraded_untouched(self):
        raw = degrade(self.work[0])
        snap = copy.deepcopy(raw)
        out, fixed, missing = CL.repair_degraded("course_work", raw, self.work[0])
        self.assertEqual(raw, snap)
        self.assertEqual((fixed, missing), (1, 0))
        same, f2, m2 = CL.repair_degraded("course_work", self.work[0], None)
        self.assertIs(same, self.work[0])
        self.assertEqual((f2, m2), (0, 0))

    def test_announcements_and_materials_kinds(self):
        ann = dict(copy.deepcopy(F.ANNOUNCEMENTS[0]), materials=copy.deepcopy(self.work[0]["materials"]))
        mat = dict(copy.deepcopy(F.MATERIALS[1]), materials=copy.deepcopy(self.work[0]["materials"]))
        for kind, raw in (("announcement", ann), ("material", mat)):
            self.sync(kind, [raw])
            res = self.sync(kind, [degrade(raw)])
            self.assertEqual(res["counts"].get("update", 0), 0, kind)


if __name__ == "__main__":
    unittest.main()
