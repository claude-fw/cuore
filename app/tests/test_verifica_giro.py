"""verifica_giro.py: verifica in sola lettura di un giro reale del job School. Solo file SINTETICI in cartelle temporanee
(log, stato, riepilogo, calendario; Registry e database Finance solo come impronte): nessun file reale viene letto."""
import datetime as dt, json, os, plistlib, shutil, stat, sys, tempfile, unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bridge"))
import verifica_giro as V  # noqa: E402

UTC = dt.timezone.utc
CET = dt.timezone(dt.timedelta(hours=1))
T0 = dt.datetime(2026, 10, 25, 5, 57, 22, tzinfo=UTC)                  # 06:57 a Roma (ora solare)


def put(path, data, mode="wb"):
    with open(path, mode) as f:
        f.write(data)


def line(t, evento, **kw):
    return json.dumps(dict(ts=t.isoformat(), evento=evento, **kw))


def run(t0, minutes, esito, kinds_cls, kinds_cv, extra=None):
    t1 = t0 + dt.timedelta(minutes=minutes)
    return [line(t0, "avvio", modo="commit"),
            line(t1 - dt.timedelta(seconds=3), "fonte", fonte="classroom", esito="fallita", kinds=kinds_cls),
            line(t1 - dt.timedelta(seconds=2), "fonte", fonte="classeviva", esito="fallita", kinds=kinds_cv),
            line(t1, "fine", esito=esito, exit_code={"OK": 0, "PARZIALE": 1, "FALLITO": 2}[esito], durata_s=64.0,
                 anomalie=["titoli_non_determinabili:classroom/x"], **(extra or {}))]


TMP = {"stato": "errore", "categoria": "temporaneo", "motivo": "scadenza_tempo_reale"}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        j = lambda n: os.path.join(self.dir, n)      # noqa: E731
        self.paths = {"log": j("school-job.jsonl"), "plist": j("cloud.scuola.job.plist"), "registry": j("registry.sqlite3"),
                      "state": j("state.json"), "summary": j("last_summary.json"), "finance_db": j("finance.sqlite3")}
        with open(self.paths["plist"], "wb") as f:
            plistlib.dump({"StartCalendarInterval": [{"Hour": 6, "Minute": 45}, {"Hour": 14, "Minute": 30},
                                                     {"Hour": 20, "Minute": 0}]}, f)
        for k in ("registry", "finance_db"):
            put(self.paths[k], b"sintetico-" + k.encode())
        prev = T0 - dt.timedelta(hours=10)
        self.write_log(run(prev, 2, "OK", {}, {}))
        self.state({"problemi": {}, "ultimo": {"inizio": prev.astimezone(CET).isoformat(), "esito": "OK"}})

    def write_log(self, lines, mode="w"):
        with open(self.paths["log"], mode, encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def state(self, obj):
        with open(self.paths["state"], "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def summary(self, inizio, notifiche=()):
        with open(self.paths["summary"], "w", encoding="utf-8") as f:
            json.dump({"inizio": inizio.astimezone(CET).isoformat(), "notifiche": [{"titolo": t, "inviata": True} for t in notifiche],
                       "problemi_in_osservazione": ["temporaneo:classroom"],
                       "automazione": {"questo_giro": {"orario_previsto": "2026-10-25T06:45:00+01:00"}, "stop_durante_il_giro_s": 6830,
                                       "giri_mancati_consecutivi": []}}, f)

    def the_run(self):
        self.write_log(run(T0, 115, "FALLITO", {k: TMP for k in ("course", "material")}, {"grade": TMP, "notice": dict(TMP, motivo=None)},
                           extra={"durata_reale_s": 6893.7, "stop_durante_il_giro_s": 6830, "orari_mancati": 0}), mode="a")
        self.state({"problemi": {"temporaneo:classroom": {"dal": T0.isoformat(), "giri": 1, "attesa_conferma": 2,
                                                          "ultima_notifica": None, "titolo": "Cloud Scuola: Classroom non aggiornato"},
                                 "anomalia:assenze_ambigue:cvv:1234567": {"dal": T0.isoformat(), "giri": 1,
                                                                         "ultima_notifica": T0.isoformat(),
                                                                         "titolo": "Cloud Scuola: controllo richiesto"}},
                    "ultimo": {"inizio": T0.astimezone(CET).isoformat(), "esito": "FALLITO", "exit_code": 2},
                    "risolti_in_attesa": {"freschezza": "Cloud Scuola: dati non aggiornati"}})
        put(self.paths["registry"], b"+scrittura del job", "ab")

    def after(self, ref, now=None):
        return V.after(ref, self.paths, "06:45", now=now or T0 + dt.timedelta(hours=3), feed_fn=lambda: {"schema": "x"})


class Verifica(Base):
    def test_nessun_giro_nuovo(self):
        ref = V.before(self.paths, now=T0)
        code, rep = self.after(ref)
        self.assertEqual(code, 2)
        self.assertIn("nessun nuovo", rep["giro"])

    def test_giro_esaminato_con_il_suo_riepilogo(self):
        ref = V.before(self.paths, now=T0)
        self.the_run()
        self.summary(T0 + dt.timedelta(seconds=1), notifiche=["Cloud Scuola: problema risolto"])
        code, rep = self.after(ref)
        self.assertEqual(code, 0)
        self.assertEqual((rep["1_orario"]["avvio"], rep["1_orario"]["fine"]), ("2026-10-25 06:57:22", "2026-10-25 08:52:22"))
        self.assertEqual(rep["1_orario"]["stato_orario"]["stato"], "eseguito")
        self.assertEqual((rep["2_esito"]["esito"], rep["2_esito"]["exit_code"], rep["2_esito"]["stop_durante_il_giro_s"]),
                         ("FALLITO", 2, 6830))
        self.assertTrue(rep["2_esito"]["codice_con_diagnostica_dell_automazione"])
        self.assertEqual(rep["3_fonti"]["classroom"]["tipi_in_errore"], {"temporaneo/scadenza_tempo_reale": 2})
        self.assertEqual(rep["3_fonti"]["classeviva"]["tipi_in_errore"],
                         {"temporaneo/scadenza_tempo_reale": 1, "temporaneo/-": 1})
        job = rep["8_job"]
        self.assertTrue(job["riepilogo_del_giro_esaminato"])
        self.assertEqual(job["notifiche_del_riepilogo"], [("Cloud Scuola: problema risolto", True)])
        self.assertEqual(job["stato"]["chiusure_in_attesa"], 1)
        self.assertEqual(sorted((p["tipo"], p["fonte"]) for p in job["stato"]["problemi"]),
                         [("anomalia", None), ("temporaneo", "classroom")])
        imp = rep["7_impronte"]
        self.assertEqual((imp["finance_db"]["uguale"], imp["plist"]["uguale"], imp["registry"]["uguale"]), (True, True, False))
        self.assertEqual(rep["5_feed"]["problemi_contratto"], 1)                         # feed finto: non conforme, contato
        for bad in ("cvv:", "assenze_ambigue", "classroom/x"):
            self.assertNotIn(bad, json.dumps(rep), bad)

    def test_riepilogo_di_un_giro_successivo_dichiarato(self):
        ref = V.before(self.paths, now=T0)
        self.the_run()
        later = T0 + dt.timedelta(hours=7, minutes=44)
        self.write_log(run(later, 70, "PARZIALE", {"material": TMP}, {}), mode="a")
        self.summary(later)
        code, rep = self.after(ref, now=later + dt.timedelta(hours=2))
        self.assertEqual(rep["2_esito"]["esito"], "FALLITO")                             # sempre il PRIMO giro dopo il riferimento
        self.assertNotIn("8_job", rep)
        self.assertFalse(rep["8_job_di_un_giro_successivo"]["riepilogo_del_giro_esaminato"])
        self.assertIn("non sono piu' osservabili", rep["8_job_di_un_giro_successivo"]["nota"])

    def test_feed_non_leggibile_e_stato_assente(self):
        ref = V.before(self.paths, now=T0)
        self.the_run()
        os.remove(self.paths["state"])

        def boom():
            raise RuntimeError("motore")
        code, rep = V.after(ref, self.paths, "06:45", now=T0 + dt.timedelta(hours=3), feed_fn=boom)
        self.assertEqual(rep["5_feed"], {"non_disponibile": "RuntimeError"})
        self.assertEqual(rep["8_job_di_un_giro_successivo"]["stato"], {"leggibile": False})


class RigaDiComando(Base):
    def test_prima_e_dopo_con_riferimento_privato(self):
        ref_file = os.path.join(self.dir, "rif.json")
        with mock.patch.object(V, "real_paths", return_value=self.paths), mock.patch("sys.stdout"):
            self.assertEqual(V.main(["dopo", "06:45", "--riferimento", ref_file]), 64)      # senza riferimento
            self.assertEqual(V.main(["prima", "--riferimento", ref_file]), 0)
            self.assertEqual(stat.S_IMODE(os.stat(ref_file).st_mode), 0o600)
            self.assertEqual(V.main(["dopo", "06:45", "--riferimento", ref_file]), 2)       # nessun giro nuovo
        self.assertTrue(V.DEFAULT_REF.startswith(tempfile.gettempdir()))                    # mai accanto ai dati reali


if __name__ == "__main__":
    unittest.main()
