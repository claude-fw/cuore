"""registry_restore: verifica, ripristino SU COPIA e confronto append-only degli snapshot del Registry (dati SINTETICI)."""
import datetime as dt, hashlib, json, os, shutil, sqlite3, sys, tempfile, unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, ROOT, os.path.join(ROOT, "..", "_common")]
import cvv_registry as CR  # noqa: E402
import cvv_sync as S  # noqa: E402
import registry as R  # noqa: E402
import registry_restore as RR  # noqa: E402
import school_dashboard as SD  # noqa: E402
import test_school_records as TR  # noqa: E402


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "registry.sqlite3")
        self.reg = TR.make(grades=TR.GRADES[:-1], path=self.path)          # stato "vecchio": manca l'ultimo voto
        self.snap, chk = S._backup(self.reg, self.dir)
        self.assertTrue(chk["ok"])
        CR.sync_kind(self.reg, "grade", lambda: [dict(g) for g in TR.GRADES])  # poi il Registry cresce
        self.reg.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")              # stato consolidato nel file principale
        self.addCleanup(self.reg.close)

    def new(self, name="ripristinato.sqlite3"):
        return os.path.join(self.dir, name)


class Verifica(Base):
    def test_snapshot_valido(self):
        v = RR.verify(self.snap)
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["integrity_check"], "ok")
        self.assertEqual(v["schema_version"], R.SCHEMA_VERSION)
        self.assertGreater(v["righe"]["versions"], 0)
        self.assertFalse(os.path.exists(self.snap + "-wal") or os.path.exists(self.snap + "-shm"))   # sola lettura

    def test_snapshot_corrotto_non_valido(self):
        bad = self.new("corrotto.sqlite3")
        shutil.copyfile(self.snap, bad)
        with open(bad, "r+b") as f:
            f.seek(4096)
            f.write(b"\x00" * 8192)                                             # pagine azzerate
        v = RR.verify(bad)
        self.assertFalse(v["ok"], v)
        dest = self.new()
        with self.assertRaises(R.RegistryError):
            RR.restore_copy(bad, dest)
        self.assertFalse(os.path.exists(dest))

    def test_file_assente_o_wal_non_vuoto(self):
        self.assertEqual(RR.verify(self.new("assente.sqlite3"))["errore"], "FileNotFoundError")
        with open(self.snap + "-wal", "wb") as f:
            f.write(b"x")
        self.assertEqual(RR.verify(self.snap)["errore"], "RegistryError")


class Ripristino(Base):
    def test_copia_identica_e_originali_invariati(self):
        before = (sha(self.snap), sha(self.path))
        dest = self.new()
        r = RR.restore_copy(self.snap, dest)
        self.assertTrue(r["contenuto_identico"])
        self.assertEqual(r["copia"]["righe"], r["snapshot"]["righe"])
        self.assertEqual(r["copia"]["impronta"], r["snapshot"]["impronta"])
        self.assertEqual(os.stat(dest).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.exists(dest + ".in-corso") or os.path.exists(dest + "-wal"))
        self.assertEqual((sha(self.snap), sha(self.path)), before)                # snapshot e Registry non toccati
        reg = R.Registry.from_snapshot(dest)                                       # utilizzabile dal motore
        reg.close()

    def test_mai_sovrascrivere(self):
        dest = self.new()
        open(dest, "w").close()
        with self.assertRaises(FileExistsError):
            RR.restore_copy(self.snap, dest)
        self.assertEqual(os.path.getsize(dest), 0)
        with self.assertRaises((ValueError, FileExistsError)):
            RR.restore_copy(self.snap, self.snap)
        with mock.patch.dict(os.environ, {"CLOUD_SCHOOL_DB": self.new("reale.sqlite3")}):
            with self.assertRaises(ValueError):                                    # mai sul percorso del Registry reale
                RR.restore_copy(self.snap, self.new("reale.sqlite3"))
        self.assertFalse(os.path.exists(self.new("reale.sqlite3")))
        with self.assertRaises(FileNotFoundError):
            RR.restore_copy(self.snap, os.path.join(self.dir, "manca", "x.sqlite3"))

    def test_pagine_identiche_dal_ripristinato(self):
        dest = self.new()
        RR.restore_copy(self.snap, dest)
        now = dt.datetime.fromisoformat(TR.F.NOW)
        man = os.path.join(self.dir, "manuale.json")

        def page(p):
            d = SD.build_from_path(p, now, manual_path=man, subjects=TR.SUBJECTS, pagina="voti")
            d["provenance"].pop("sorgente", None)                                  # percorso del file: unica differenza
            return json.dumps(d, sort_keys=True, ensure_ascii=False)
        self.assertEqual(page(dest), page(self.snap))


class Confronto(Base):
    def test_snapshot_contenuto_nel_registro_piu_recente(self):
        c = RR.compare(self.snap, self.path)
        self.assertTrue(c["coerente"], c)
        self.assertGreater(c["tabelle"]["versions"]["solo_nel_recente"], 0)       # l'ultimo voto c'e' solo dopo
        self.assertEqual(c["tabelle"]["versions"]["mancanti_nel_recente"], 0)

    def test_riga_immutabile_mancante_rilevata(self):
        tampered = self.new("manomesso.sqlite3")
        shutil.copyfile(self.path, tampered)
        conn = sqlite3.connect(tampered)
        conn.execute("DROP TRIGGER versions_no_delete")                            # solo sulla copia di prova
        conn.execute("DELETE FROM versions WHERE rowid = (SELECT min(rowid) FROM versions)")
        conn.commit()
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.close()
        c = RR.compare(self.snap, tampered)
        self.assertFalse(c["coerente"])
        self.assertEqual(c["tabelle"]["versions"]["mancanti_nel_recente"], 1)

    def test_ordine_invertito_non_coerente(self):
        self.assertFalse(RR.compare(self.path, self.snap)["coerente"])            # il piu' recente ha righe in piu'


if __name__ == "__main__":
    unittest.main()
