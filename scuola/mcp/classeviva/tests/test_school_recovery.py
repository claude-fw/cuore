"""Recupero all'uso del job scuola (E1): decisione pura su giri, orari e memoria SINTETICI; la riga di comando solo su
file temporanei. Nessun job lanciato, nessun file reale (log, plist, Registry, stato) letto o scritto."""
import datetime as dt, io, json, os, plistlib, sys, tempfile, unittest
from contextlib import redirect_stdout

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "..", "_common"))
import school_recovery as R  # noqa: E402

UTC = dt.timezone.utc
TIMES = [(6, 45), (14, 30), (20, 0)]           # ora locale (Europe/Rome; a novembre UTC+1)


def t(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=UTC)


def run(avvio, esito):
    return {"avvio": t(avvio), "esito": esito}


EVENING_OK = run("2026-11-29T19:01:00", "ok")          # giro delle 20:00 del 01/11
MORNING_PARTIAL = run("2026-11-30T05:51:00", "parziale")  # giro delle 06:45, Mac in stop: parziale
NOW = t("2026-11-30T07:00:00")                          # 08:00 locali, utente al Mac


class Decisione(unittest.TestCase):
    def d(self, runs, now=NOW, idle=10, state=None):
        return R.decide(runs, TIMES, now, idle, state)

    def test_recupera_dopo_un_parziale_quando_il_mac_e_in_uso(self):
        out = self.d([EVENING_OK, MORNING_PARTIAL])
        self.assertEqual((out["recupera"], out["motivo"]), (True, "recupero"))
        self.assertEqual(out["orario"], "2026-11-30T05:45:00+00:00")
        self.assertEqual(out["orari_scoperti"], 1)

    def test_orario_coperto_da_un_giro_ok_anche_in_ritardo(self):
        late_ok = run("2026-11-30T06:30:00", "ok")
        self.assertEqual(self.d([EVENING_OK, MORNING_PARTIAL, late_ok])["motivo"], "ultimo_orario_coperto")

    def test_attende_dopo_l_orario_che_launchd_lo_esegua(self):
        out = self.d([EVENING_OK], now=t("2026-11-30T05:55:00"))     # 10 minuti dopo le 06:45
        self.assertEqual(out["motivo"], "ultimo_orario_coperto")      # conta ancora l'orario delle 20:00, coperto
        self.assertFalse(out["recupera"])

    def test_giro_in_corso(self):
        self.assertEqual(self.d([EVENING_OK, run("2026-11-30T05:51:00", "in_corso")])["motivo"], "giro_in_corso")

    def test_prossimo_orario_vicino(self):
        out = self.d([EVENING_OK, MORNING_PARTIAL], now=t("2026-11-30T13:10:00"))   # 14:10 locali
        self.assertEqual(out["motivo"], "prossimo_orario_vicino")

    def test_mac_non_in_uso_o_inattivita_ignota(self):
        self.assertEqual(self.d([EVENING_OK, MORNING_PARTIAL], idle=3600)["motivo"], "mac_non_in_uso")
        self.assertEqual(self.d([EVENING_OK, MORNING_PARTIAL], idle=None)["motivo"], "mac_non_in_uso")

    def test_autorizzazione_non_si_ripara_con_un_altro_giro(self):
        self.assertEqual(self.d([EVENING_OK, run("2026-11-30T05:46:00", "autorizzazione")])["motivo"],
                         "serve_intervento")

    def test_un_solo_recupero_per_orario_e_intervallo_minimo(self):
        first = self.d([EVENING_OK, MORNING_PARTIAL])
        state = R.record({}, first, NOW)
        later = NOW + dt.timedelta(hours=2)
        self.assertEqual(self.d([EVENING_OK, MORNING_PARTIAL], now=later, state=state)["motivo"], "gia_recuperato")
        # un recupero per un ALTRO orario 10 minuti fa: troppo recente; dopo 40 minuti si'
        other = {"recuperi": [{"orario": "2026-11-29T19:00:00+00:00", "avviato": "2026-11-30T06:50:00+00:00"}]}
        self.assertEqual(self.d([EVENING_OK, MORNING_PARTIAL], state=other)["motivo"], "recupero_recente")
        other["recuperi"][0]["avviato"] = "2026-11-30T06:20:00+00:00"
        self.assertTrue(self.d([EVENING_OK, MORNING_PARTIAL], state=other)["recupera"])

    def test_dopo_un_lungo_periodo_spento_un_solo_giro_che_copre_tutto(self):
        # Mac ibernato dal 31/10 sera: nessun giro per 6 orari; al risveglio un recupero, poi basta
        runs = [run("2026-11-28T19:01:00", "ok")]
        out = self.d(runs, now=t("2026-11-30T10:00:00"))
        self.assertTrue(out["recupera"])
        self.assertEqual(out["orari_scoperti"], 4)                   # 01/11 06:45, 14:30, 20:00; 02/11 06:45 (31/10 coperti)
        state = R.record({}, out, t("2026-11-30T10:00:00"))
        again = self.d(runs, now=t("2026-11-30T10:45:00"), state=state)
        self.assertEqual(again["motivo"], "gia_recuperato")
        # il giro di recupero e' riuscito: l'orario risulta coperto
        recovered = runs + [run("2026-11-30T10:00:30", "ok")]
        self.assertEqual(self.d(recovered, now=t("2026-11-30T11:00:00"), state=state)["motivo"], "ultimo_orario_coperto")

    def test_memoria_dei_recuperi_limitata_e_nessuna_scrittura_se_non_si_recupera(self):
        state = {}
        for i in range(25):
            state = R.record(state, {"recupera": True, "orario": "o%d" % i}, NOW)
        self.assertEqual(len(state["recuperi"]), 20)
        self.assertEqual(state["recuperi"][0]["orario"], "o5")
        self.assertEqual(R.record(state, {"recupera": False}, NOW), state)

    def test_inattivita_da_ioreg(self):
        text = '    | |   "HIDIdleTime" = 4200000000\n    | |   "HIDIdleTimeUnits" = 1\n'
        self.assertAlmostEqual(R.idle_seconds_from_ioreg(text), 4.2)
        self.assertIsNone(R.idle_seconds_from_ioreg("niente"))


class RigaDiComando(unittest.TestCase):
    def test_stampa_la_decisione_e_non_scrive_nulla(self):
        with tempfile.TemporaryDirectory() as d:
            log, plist = os.path.join(d, "school-job.jsonl"), os.path.join(d, "job.plist")
            now = dt.datetime.now(UTC)
            with open(log, "w", encoding="utf-8") as f:
                old = (now - dt.timedelta(days=3)).isoformat()
                f.write(json.dumps({"ts": old, "evento": "avvio", "fonti": ["classeviva"], "modo": "commit"}) + "\n")
                f.write(json.dumps({"ts": old, "evento": "fine", "esito": "OK", "exit_code": 0, "durata_s": 80}) + "\n")
            with open(plist, "wb") as f:
                plistlib.dump({"Label": "prova", "StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in TIMES]}, f)
            before = {p: os.path.getmtime(os.path.join(d, p)) for p in os.listdir(d)}
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(R.main(["--log", log, "--plist", plist, "--inattivita-s", "5"]), 0)
            out = json.loads(buf.getvalue())
            self.assertIn(out["motivo"], ("recupero", "prossimo_orario_vicino"))   # dipende dall'ora della prova
            self.assertEqual(out["righe_di_log_illeggibili"], 0)
            self.assertEqual({p: os.path.getmtime(os.path.join(d, p)) for p in os.listdir(d)}, before)

    def test_esegui_salva_la_memoria_poi_avvia_il_job_una_volta_per_orario(self):
        with tempfile.TemporaryDirectory() as d:
            log, plist, stato = (os.path.join(d, n) for n in ("school-job.jsonl", "job.plist", "recupero.json"))
            old = (dt.datetime.now(UTC) - dt.timedelta(days=3)).isoformat()
            with open(log, "w", encoding="utf-8") as f:
                f.write(json.dumps({"ts": old, "evento": "avvio", "fonti": ["classeviva"], "modo": "commit"}) + "\n")
                f.write(json.dumps({"ts": old, "evento": "fine", "esito": "OK", "exit_code": 0, "durata_s": 80}) + "\n")
            with open(plist, "wb") as f:
                plistlib.dump({"Label": "prova", "StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in TIMES]}, f)
            calls, saved = [], dict(R.POLICY)
            R.POLICY["margine_prossimo_orario_min"] = 0           # decisione indipendente dall'ora della prova
            try:
                args = ["--log", log, "--plist", plist, "--inattivita-s", "5", "--stato", stato, "--esegui"]
                for _ in range(2):
                    with redirect_stdout(io.StringIO()):
                        R.main(args, execv=lambda exe, argv: calls.append(argv))
            finally:
                R.POLICY.clear(); R.POLICY.update(saved)
            self.assertEqual(len(calls), 1)                       # il secondo passaggio trova «gia_recuperato»
            self.assertEqual(calls[0][1:], [os.path.join(os.path.dirname(HERE), "school_job.py"), "run", "--commit", "--notify"])
            with open(stato, encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)["recuperi"]), 1)

    def test_esegui_senza_stato_rifiutato(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            import contextlib
            with contextlib.redirect_stderr(io.StringIO()):
                R.main(["--esegui"])

    def test_log_assente(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with redirect_stdout(buf):
                R.main(["--log", os.path.join(d, "manca.jsonl"), "--plist", os.path.join(d, "manca.plist"),
                        "--inattivita-s", "5"])
            self.assertEqual(json.loads(buf.getvalue())["motivo"], "log_o_calendario_non_leggibili")


if __name__ == "__main__":
    unittest.main()
