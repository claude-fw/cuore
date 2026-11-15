"""Invio del feed School al cloud (E3): spento senza configurazione, minimizzazione, invio a un server FINTO locale
(127.0.0.1), errori di rete e risposte del cloud che non toccano mai il job. Solo dati sintetici e cartelle temporanee:
nessuna rete reale, nessun file reale (Registry, log, calendario, Portachiavi)."""
import datetime as dt, json, os, shutil, socket, sys, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, ROOT, os.path.join(ROOT, "..", "_common")]
import school_cloud_push as CP  # noqa: E402
import school_job as J  # noqa: E402
import test_school_records as TR  # noqa: E402   (registro sintetico del motore)

CS, K = CP._bridge()
import school_dashboard as SD  # noqa: E402

NOW = dt.datetime.fromisoformat(TR.F.NOW)
CLIENT = ("0123456789abcdef0123456789abcdef.access", "segreto-di-prova-solo-test-0001")


class FakeCloud:
    """Server HTTP finto su 127.0.0.1: registra le richieste e risponde con lo stato scelto."""

    def __init__(self, status=200, reply=None, delay=0, location=None):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": self.rfile.read(n)})
                time.sleep(delay)
                b = json.dumps(reply if reply is not None else {"ok": status == 200}).encode()
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            do_GET = do_POST                                  # lettura della richiesta di giro: stesso registro

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/api/school" % self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "job")
        os.makedirs(self.state)
        self.reg = os.path.join(self.tmp, "registry.sqlite3")
        TR.make(path=self.reg).close()
        self.subjects = os.path.join(self.tmp, "materie.json")
        with open(self.subjects, "w", encoding="utf-8") as f:
            json.dump(TR.SUBJECTS, f)
        self.secrets = os.path.join(self.tmp, "segreti")
        os.makedirs(self.secrets, mode=0o700)
        self.token = os.path.join(self.secrets, "mac-school.json")
        self.write_private(self.token, {"client_id": CLIENT[0], "client_secret": CLIENT[1]})

    def write_private(self, path, obj, mode=0o600):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.chmod(path, mode)

    def configure(self, url, **extra):
        self.write_private(CP.config_path(self.state), dict({"url": url, "token_file": self.token}, **extra))

    def raw_feed(self):
        return CS.build_feed(self.reg, NOW, subjects_file=self.subjects, manual_path=os.path.join(self.tmp, "manca.json"))

    def build(self):
        return lambda: CP.minimize(self.raw_feed())

    def server(self, **kw):
        s = FakeCloud(**kw)
        self.addCleanup(s.close)
        return s


class Spento(Base):
    def test_senza_configurazione_non_fa_nulla(self):
        calls = []
        log = lambda *a, **k: calls.append(("log", a))                       # noqa: E731
        build = lambda: calls.append("build")                                 # noqa: E731
        sender = lambda *a, **k: calls.append("send")                         # noqa: E731
        self.assertIsNone(CP.job_step(self.state, self.reg, log=log, build=build, sender=sender))
        self.assertEqual(calls, [])                                            # niente feed, rete o log
        self.assertIsNone(J.cloud_push_step(self.state, self.reg, log))
        self.assertEqual(calls, [])

    def test_sospeso_con_attivo_false(self):
        self.configure("https://cuore.esempio.invalid/api/school", attivo=False)
        self.assertIsNone(CP.job_step(self.state, self.reg, build=lambda: self.fail("mai costruito")))

    def test_main_senza_configurazione_esito_e_log_invariati(self):
        summary = {"esito": "PARZIALE", "exit_code": 1, "modo": "commit", "fonti": {}, "anomalie": []}
        logf = os.path.join(self.tmp, "l.jsonl")
        argv = ["run", "--commit", "--db", self.reg, "--state-dir", self.state, "--log-file", logf]
        with mock.patch.object(J, "run_job", return_value=dict(summary)) as rj, mock.patch("sys.stdout"), \
                mock.patch.object(CP, "build_minimized", side_effect=AssertionError("mai chiamato")):
            self.assertEqual(J.main(argv), 1)
        rj.assert_called_once()
        with open(logf, encoding="utf-8") as f:
            self.assertNotIn("invio_cloud", f.read())


class Minimizzazione(Base):
    def test_solo_campi_dell_interfaccia_e_contratto_valido(self):
        raw = self.raw_feed()
        raw["errori"].append(K.error("voti", "errore_motore", "KeyError"))
        m = CP.minimize(raw)
        rules = CP.load_rules()
        self.assertTrue(set(m) <= set(rules["campi"]["feed"]))
        for k in ("fonte", "motore"):
            self.assertNotIn(k, m)
        self.assertEqual(m["errori"][-1], {k: v for k, v in raw["errori"][-1].items() if k != "dettaglio"})
        self.assertEqual(set(m["pagine"]), set(rules["pagine"]))
        for name, p in m["pagine"].items():
            self.assertTrue(set(p) <= set(rules["campi"]["pagina"]), name)
            self.assertNotIn("sorgente", p["provenance"])
            for b in p["blocchi"]:
                self.assertTrue(set(b["provenienza"]) <= {"contratto", "versione"})        # niente elenchi di uid
            rb = {b["id"]: b for b in raw["pagine"][name]["blocchi"]}
            for b in p["blocchi"]:
                self.assertEqual(b.get("dati"), rb[b["id"]].get("dati"))                  # cio' che si mostra: invariato
            self.assertEqual([g["serie"] for g in p["grafici"]], [g["serie"] for g in raw["pagine"][name]["grafici"]])
        m["esito"] = "completo" if not m["errori"] else "parziale"
        _, probs = CP.check(m)
        self.assertEqual(probs, [])
        self.assertLess(len(json.dumps(m)), len(json.dumps(raw)))

    def test_regole_uguali_a_motore_e_contratto(self):
        r = CP.load_rules()
        self.assertEqual(r["schema"], K.SCHEMA)
        self.assertEqual(r["schema_pagina"], SD.SCHEMA)
        self.assertEqual(tuple(r["pagine"]), CS.PAGES)
        self.assertEqual(tuple(r["stati_blocco"]), SD.BLOCK_STATES)
        self.assertEqual(tuple(r["tipi_grafico"]), SD.CHART_TYPES)
        self.assertEqual(tuple(r["esiti"]), K.ESITI)
        self.assertEqual(tuple(r["stati_freschezza"]), K.FRESHNESS_STATES)
        self.assertEqual(r["messaggi_errore"], K.PUBLIC_MESSAGES)

    def test_chiave_sensibile_blocca_l_invio(self):
        srv = self.server()
        self.configure(srv.url)
        def build():
            m = CP.minimize(self.raw_feed())
            m["pagine"]["home"]["blocchi"][0]["dati"] = {"password": "x"}
            return m
        r = CP.job_step(self.state, self.reg, build=build)
        self.assertEqual((r["esito"], r["motivo"]), ("non_inviato", "feed_non_conforme"))
        self.assertEqual(srv.requests, [])

    def test_feed_non_disponibile_o_troppo_grande_non_inviati(self):
        srv = self.server()
        self.configure(srv.url)
        missing = lambda: CP.minimize(CS.build_feed(os.path.join(self.tmp, "assente.sqlite3"), NOW,      # noqa: E731
                                                    subjects_file=self.subjects))
        self.assertEqual(CP.job_step(self.state, self.reg, build=missing)["motivo"], "feed_non_disponibile")
        rules = dict(CP.load_rules(), max_byte=1000)
        r = CP.job_step(self.state, self.reg, build=self.build(), rules=rules)
        self.assertEqual((r["esito"], r["motivo"]), ("non_inviato", "troppo_grande"))
        self.assertEqual(srv.requests, [])


class Invio(Base):
    def test_invia_il_json_atteso_con_il_token_di_servizio(self):
        srv = self.server(reply={"ok": True, "generato": "x"})
        self.configure(srv.url)
        lines = []
        r = CP.job_step(self.state, self.reg, log=lambda ev, **kw: lines.append((ev, kw)), build=self.build())
        self.assertEqual(r["esito"], "inviato", r)
        self.assertEqual(len(srv.requests), 1)
        req = srv.requests[0]
        self.assertEqual(req["path"], "/api/school")
        self.assertEqual(req["headers"]["content-type"], "application/json")
        self.assertEqual((req["headers"]["cf-access-client-id"], req["headers"]["cf-access-client-secret"]), CLIENT)
        expected = CP.minimize(self.raw_feed())
        sent = json.loads(req["body"])
        for f in (sent, expected):                                # l'unico campo che dipende dall'ora di lettura
            f["automazione"]["storico"].pop("letto_il", None)
        self.assertEqual(sent, expected)
        self.assertEqual(r["byte"], len(req["body"]))
        self.assertEqual([ev for ev, _ in lines], ["invio_cloud"])
        self.assertNotIn(CLIENT[1], json.dumps(lines))                       # il segreto mai nel log

    def test_risposte_del_cloud(self):
        for status, reply, esito, motivo in [
            (200, {"ok": True, "ripetuto": True}, "inviato", None),
            (403, {"errore": "accesso_negato"}, "errore", "non_autorizzato"),
            (409, {"errore": "feed_meno_recente"}, "rifiutato", "feed_meno_recente"),
            (422, {"errore": "Testo Libero <script>"}, "rifiutato", "rifiutato"),
            (503, {"errore": "invio_school_spento"}, "errore", "invio_school_spento"),
            (302, {}, "errore", "reindirizzato"),
        ]:
            srv = self.server(status=status, reply=reply, location="http://127.0.0.1:1/login" if status == 302 else None)
            self.configure(srv.url)
            r = CP.job_step(self.state, self.reg, build=self.build())
            self.assertEqual((r["esito"], r.get("motivo")), (esito, motivo), status)
            self.assertEqual(len(srv.requests), 1, "nessun reindirizzamento seguito, nessun nuovo tentativo")

    def test_errori_di_rete_e_lentezza_non_sollevano(self):
        self.configure("http://127.0.0.1:%d/api/school" % closed_port())
        r = CP.job_step(self.state, self.reg, build=self.build())
        self.assertEqual((r["esito"], r["motivo"]), ("errore", "rete"))
        srv = self.server(delay=1.5)
        self.configure(srv.url)
        t0 = time.monotonic()
        r = CP.job_step(self.state, self.reg, build=self.build(), sender=lambda *a: CP.send(*a, timeout=0.3))
        self.assertLess(time.monotonic() - t0, 1.4)                           # il timeout interrompe l'attesa
        self.assertEqual((r["esito"], r["motivo"]), ("errore", "rete"))
        boom = lambda: (_ for _ in ()).throw(RuntimeError("difetto"))          # noqa: E731
        self.assertEqual(CP.job_step(self.state, self.reg, build=boom)["motivo"], "imprevisto")

    def test_main_con_cloud_irraggiungibile_esito_invariato(self):
        self.configure("http://127.0.0.1:%d/api/school" % closed_port())
        logf = os.path.join(self.tmp, "l.jsonl")
        argv = ["run", "--commit", "--db", self.reg, "--state-dir", self.state, "--log-file", logf]
        summary = {"esito": "OK", "exit_code": 0, "modo": "commit", "fonti": {}, "anomalie": []}
        with mock.patch.object(J, "run_job", return_value=dict(summary)), mock.patch("sys.stdout"), \
                mock.patch.object(CP, "build_minimized", lambda *a, **k: CP.minimize(self.raw_feed())):
            self.assertEqual(J.main(argv), 0)
            self.assertEqual(J.main(argv[:1] + argv[2:]), 0)                  # dry-run: nessun invio
        with open(logf, encoding="utf-8") as f:
            evs = [json.loads(x) for x in f.read().splitlines()]
        sent = [e for e in evs if e["evento"] == "invio_cloud"]
        self.assertEqual([(e["esito"], e["motivo"]) for e in sent], [("errore", "rete")])
        with mock.patch.object(CP, "job_step", side_effect=KeyboardInterrupt), mock.patch("sys.stdout"), \
                mock.patch.object(J, "run_job", return_value=dict(summary)):
            self.assertEqual(J.main(argv), 0)                                 # interruzione dell'invio: esito del job invariato


class Configurazione(Base):
    def test_configurazioni_non_valide_niente_invio(self):
        srv = self.server()
        cases = [
            ({"url": "http://cuore.esempio.invalid/api/school", "token_file": self.token}, "url_non_valido"),
            ({"url": "https://cuore.esempio.invalid/altro", "token_file": self.token}, "url_non_valido"),
            ({"url": "https://u:p@cuore.esempio.invalid/api/school", "token_file": self.token}, "url_non_valido"),
            ({"url": srv.url, "token_file": "relativo.json"}, "configurazione_non_valida"),
            ({"url": srv.url, "token_file": self.token, "altro": 1}, "configurazione_non_valida"),
            ({"url": srv.url, "token_file": os.path.join(self.tmp, "manca.json")}, "token_assente"),
        ]
        for cfg, motivo in cases:
            self.write_private(CP.config_path(self.state), cfg)
            r = CP.job_step(self.state, self.reg, build=self.build())
            self.assertEqual((r["esito"], r["motivo"]), ("errore", motivo), cfg)
        with open(CP.config_path(self.state), "w") as f:
            f.write("{non json")
        self.assertEqual(CP.job_step(self.state, self.reg, build=self.build())["motivo"], "configurazione_illeggibile")
        self.assertEqual(srv.requests, [])

    def test_token_protetto(self):
        srv = self.server()
        self.configure(srv.url)
        os.chmod(self.token, 0o644)
        self.assertEqual(CP.job_step(self.state, self.reg, build=self.build())["motivo"], "permessi_token")
        os.chmod(self.token, 0o600)
        link = os.path.join(self.secrets, "collegamento.json")
        os.symlink(self.token, link)
        self.configure(srv.url)
        self.write_private(CP.config_path(self.state), {"url": srv.url, "token_file": link})
        self.assertEqual(CP.job_step(self.state, self.reg, build=self.build())["motivo"], "permessi_token")
        self.write_private(self.token, {"client_id": "x", "client_secret": "y"})
        self.configure(srv.url)
        self.assertEqual(CP.job_step(self.state, self.reg, build=self.build())["motivo"], "token_non_valido")
        with mock.patch.object(CP, "_unsafe_place", return_value=True):
            self.assertEqual(CP.job_step(self.state, self.reg, build=self.build())["motivo"], "token_in_posto_non_sicuro")
        self.assertTrue(CP._unsafe_place(os.path.join(CP.REPO, "cloud", "segreto.json")))
        self.assertEqual(srv.requests, [])


class RichiestaDalTelefono(Base):
    """«Sincronizza tutto» dal telefono: il Mac legge la richiesta con il suo token e il giro parte solo se serve."""

    def test_legge_la_richiesta_con_il_token_del_mac(self):
        s = self.server(reply={"ok": True, "risultato": {"richiesta": "2026-12-12T19:30:00.000Z"}})
        self.configure(s.url)
        self.assertEqual(CP.pending_request(self.state), "2026-12-12T19:30:00.000Z")
        r = s.requests[0]
        self.assertEqual(r["path"], "/api/school/richiesta")
        self.assertEqual(r["body"], b"")
        self.assertEqual(r["headers"]["cf-access-client-id"], CLIENT[0])

    def test_spento_rifiutato_o_irraggiungibile_vuol_dire_nessuna_richiesta(self):
        self.assertIsNone(CP.pending_request(self.state))                       # nessuna configurazione
        s = self.server(status=403, reply={"ok": False, "errore": "accesso_negato"})
        self.configure(s.url)
        self.assertIsNone(CP.pending_request(self.state))
        self.configure("http://127.0.0.1:%d/api/school" % closed_port())
        self.assertIsNone(CP.pending_request(self.state))

    def test_quando_parte_il_giro(self):
        now = dt.datetime(2026, 12, 12, 20, 0, tzinfo=dt.timezone.utc)
        st = {"ultimo": {"inizio": "2026-12-12T19:00:00+00:00"}, "ultimo_commit_ok": "2026-12-12T19:05:00+00:00"}
        self.assertIsNone(J.check_due(st, now))                                 # dati di un'ora fa
        self.assertEqual(J.check_due(st, now, richiesta="2026-12-12T19:30:00Z"), "richiesta_dal_telefono")
        self.assertIsNone(J.check_due(st, now, richiesta="2026-12-12T18:30:00Z"))  # richiesta gia' servita
        later = now + dt.timedelta(hours=4)
        self.assertEqual(J.check_due(st, later), "dati_vecchi")
        fallito = dict(st, ultimo={"inizio": "2026-12-12T23:30:00+00:00"})       # tentativo fallito mezz'ora fa
        self.assertIsNone(J.check_due(fallito, later))
        self.assertEqual(J.check_due({}, now), "mai_riuscito")
        # Mac non in uso (risveglio a coperchio chiuso, o inattivita' non leggibile): solo le richieste dal telefono
        self.assertIsNone(J.check_due(st, later, idle_s=301))
        self.assertIsNone(J.check_due(st, later, idle_s=None))
        self.assertEqual(J.check_due(st, later, idle_s=30), "dati_vecchi")
        self.assertEqual(J.check_due(st, now, richiesta="2026-12-12T19:30:00Z", idle_s=None), "richiesta_dal_telefono")


if __name__ == "__main__":
    unittest.main()
