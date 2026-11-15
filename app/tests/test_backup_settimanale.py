"""Backup settimanale verificato (`app/bridge/backup_settimanale.py` + `finance/scripts/backup-settimanale.ts`).
Solo copie SINTETICHE in cartelle temporanee disposte come i dati veri: database e archivio reali mai aperti.
Esecuzione: cd app && /usr/bin/python3 -B -m unittest tests.test_backup_settimanale"""
import datetime as dt, json, os, shutil, stat, subprocess, sys, tempfile, unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(APP, "bridge"))
sys.path.insert(0, APP)
import backup_settimanale as B  # noqa: E402
import cuore_money as CM  # noqa: E402
import school_personale as SP  # noqa: E402

NODE = CM._node()[0]
NOW = dt.datetime(2026, 11, 8, 10, 30, tzinfo=dt.timezone.utc)


def read(p):
    with open(p, "rb") as f:
        return f.read()


class School(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="cuore-settimanale-test-"))
        self.addCleanup(shutil.rmtree, self.base)
        d = os.path.join(self.base, "CUORE", "school")
        os.makedirs(d, mode=0o700)
        self.src = os.path.join(d, "personale.jsonl")
        SP.aggiungi(self.src, {"tipo": "nota", "testo": "Nota SINTETICA"})
        self.dest = os.path.join(d, "backup")

    def test_copia_verificata_con_impronta_una_al_giorno(self):
        before, mtime = read(self.src), os.stat(self.src).st_mtime_ns
        r = B.school_backup(self.src, now=NOW)
        self.assertEqual((r["ok"], r["stato"]), (True, "creato"))
        self.assertEqual(r["file"], "personale-prima-di-settimanale-2026-11-08T10-30-00-000Z.jsonl")
        copy = os.path.join(self.dest, r["file"])
        self.assertEqual(read(copy), before)
        self.assertEqual(stat.S_IMODE(os.stat(copy).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.dest).st_mode), 0o700)
        chk = subprocess.run(["/usr/bin/shasum", "-a", "256", "-c", r["file"] + ".sha256"], cwd=self.dest,
                             capture_output=True, text=True)
        self.assertEqual(chk.returncode, 0, chk.stdout + chk.stderr)
        again = B.school_backup(self.src, now=NOW + dt.timedelta(hours=2))
        self.assertEqual((again["stato"], again["file"]), ("presente", r["file"]))
        self.assertEqual((read(self.src), os.stat(self.src).st_mtime_ns), (before, mtime))   # originale intatto

    def test_copia_non_verificata_e_archivio_assente(self):
        r = B.school_backup(self.src, now=NOW, hash_copy=lambda p: "0" * 64)
        self.assertFalse(r["ok"])
        self.assertEqual([n.endswith(".non-verificato") for n in os.listdir(self.dest)], [True])
        missing = os.path.join(self.base, "CUORE", "school", "altro.jsonl")
        self.assertEqual(B.school_backup(missing, now=NOW), {"ok": True, "stato": "assente"})

    def test_percorsi_rifiutati(self):
        self.assertFalse(B.school_backup(self.src, dest=os.path.join(B.scritture.REPO_ROOT, "x"), now=NOW)["ok"])
        self.assertFalse(B.school_backup(self.src, dest="relativo", now=NOW)["ok"])
        os.chmod(self.src, 0o644)                               # archivio leggibile da altri: rifiutato
        self.assertFalse(B.school_backup(self.src, now=NOW)["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.base, "x")))


@unittest.skipUnless(NODE, "node non disponibile")
class Completo(unittest.TestCase):
    def test_finance_e_school_su_copie_sintetiche(self):
        from tests.test_scritture_reali import real_like
        base = os.path.realpath(tempfile.mkdtemp(prefix="cuore-settimanale-test-"))
        self.addCleanup(shutil.rmtree, base)
        db = real_like(base)
        before = read(db)
        d = os.path.join(base, "CUORE", "school")
        os.makedirs(d, mode=0o700)
        pers = os.path.join(d, "personale.jsonl")
        SP.aggiungi(pers, {"tipo": "nota", "testo": "Nota SINTETICA"})
        lines = []
        self.assertEqual(B.main(["--db", db, "--school-personale", pers], out=lines.append), 0, lines)
        res = json.loads(lines[0])
        self.assertEqual((res["finance"]["stato"], res["school"]["stato"]), ("creato", "creato"))
        files = os.listdir(os.path.join(base, "finance", "backup"))
        self.assertEqual(files, [res["finance"]["file"]])
        self.assertIn("-prima-di-settimanale-", files[0])
        self.assertEqual(read(db), before)
        self.assertEqual(os.listdir(os.path.dirname(db)), ["cloud-finance.sqlite3"])   # nessun -wal/-shm creato
        self.assertNotIn("SINTETICA", lines[0])
        lines.clear()
        self.assertEqual(B.main(["--db", db, "--school-personale", pers], out=lines.append), 0)
        self.assertEqual({v["stato"] for v in json.loads(lines[0]).values()}, {"presente"})

    def test_database_assente(self):
        lines = []
        self.assertEqual(B.main(["--db", "/nessuna/cartella/x.sqlite3", "--solo", "finance"], out=lines.append), 1)
        self.assertIn("assente", lines[0])


if __name__ == "__main__":
    unittest.main()
