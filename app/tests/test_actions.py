"""Registro delle azioni (`cuore_actions.py`, `cuore.actions/1`) e server MCP di CUORE (`cuore_mcp.py`). Solo file
SINTETICI in cartelle temporanee: nessun registro reale, nessun server registrato, nessuna azione eseguita."""
import datetime as dt, io, json, os, shutil, stat, subprocess, sys, tempfile, unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(APP, "bridge"))
import cuore_actions as A  # noqa: E402
import cuore_events as E  # noqa: E402
import cuore_mcp as M  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime.now(UTC).replace(microsecond=0) - dt.timedelta(hours=2)


class Tty(io.StringIO):
    def isatty(self):
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "azioni.jsonl")

    def lines(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read().splitlines()

    def rewrite(self, lines):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


class Registro(Base):
    def test_ciclo_richiesta_approvazione_esito(self):
        a = A.propose(self.path, "importa_estratto", "estratto sintetico di ottobre", now=T0)["azione"]
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertFalse(A.approved_and_valid(self.path, a, now=T0))                 # richiesta: non eseguibile
        A.decide(self.path, a, True, now=T0 + dt.timedelta(minutes=5))
        self.assertTrue(A.approved_and_valid(self.path, a, now=T0 + dt.timedelta(minutes=6)))
        A.record_outcome(self.path, a, True, riferimento="sha256:abc123", now=T0 + dt.timedelta(minutes=30))
        self.assertFalse(A.approved_and_valid(self.path, a, now=T0 + dt.timedelta(minutes=31)))   # una sola esecuzione
        doc = A.read(self.path)
        self.assertTrue(doc["integro"], doc["problemi"])
        (act,) = doc["azioni"]
        self.assertEqual((act["stato"], act["area"], act["richiesta_da"], [p["passo"] for p in act["passi"]]),
                         ("eseguita", "finance", "agente", ["richiesta", "approvata", "eseguita"]))
        self.assertEqual([p["da"] for p in act["passi"]], ["agente", "titolare", "esecutore"])

    def test_passaggi_non_ammessi_non_scrivono_nulla(self):
        a = A.propose(self.path, "sincronizza_school", "sincronizzazione dopo il ripristino", now=T0)["azione"]
        n = len(self.lines())
        bad = [lambda: A.record_outcome(self.path, a, True, now=T0),                          # esito senza approvazione
               lambda: A._append(self.path, a, "approvata", "sincronizza_school", "agente", now=T0),   # l'agente non decide
               lambda: A.propose(self.path, "cancella_tutto", "x", now=T0),                   # tipo fuori vocabolario
               lambda: A.propose(self.path, "commit_locale", "due\nrighe", now=T0),
               lambda: A.propose(self.path, "commit_locale", "x" * 201, now=T0),
               lambda: A.propose(self.path, "commit_locale", "ok", riferimento="con spazi", now=T0),
               lambda: A.decide(self.path, "act-000000000000", True, now=T0)]                 # azione sconosciuta
        for f in bad:
            with self.assertRaises(A.ActionError):
                f()
        A.decide(self.path, a, False, now=T0)
        with self.assertRaises(A.ActionError):
            A.decide(self.path, a, True, now=T0)                                              # rifiutata e' definitiva
        with self.assertRaises(A.ActionError):
            A.record_outcome(self.path, a, True, now=T0)
        self.assertEqual(len(self.lines()), n + 1)                                            # solo il rifiuto
        self.assertTrue(A.read(self.path)["integro"])

    def test_approvazione_scaduta(self):
        a = A.propose(self.path, "modifica_automazione", "alimentatore e stop impedito", now=T0)["azione"]
        A.decide(self.path, a, True, now=T0)
        late = T0 + A.APPROVAL_TTL + dt.timedelta(minutes=1)
        self.assertFalse(A.approved_and_valid(self.path, a, now=late))
        with self.assertRaises(A.ActionError):
            A.record_outcome(self.path, a, True, now=late)
        A.cancel(self.path, a, now=late)                                                      # chiusa, non riusabile
        self.assertEqual(A.read(self.path)["azioni"][0]["stato"], "annullata")

    def test_catena_manomessa_dichiarata_e_bloccata(self):
        for i in range(3):
            A.propose(self.path, "commit_locale", "commit numero %d" % i, now=T0 + dt.timedelta(minutes=i))
        orig = self.lines()
        edited = json.loads(orig[1])
        edited["tipo"] = "importa_estratto"
        for name, lines in (("riga modificata", [orig[0], json.dumps(edited), orig[2]]), ("riga tolta", [orig[0], orig[2]]),
                            ("righe riordinate", [orig[1], orig[0], orig[2]]), ("riga illeggibile", orig + ["{rotta"])):
            self.rewrite(lines)
            doc = A.read(self.path)
            self.assertFalse(doc["integro"], name)
            self.assertTrue(doc["problemi"], name)
            with self.assertRaises(A.ActionError):                                            # nessuna nuova riga
                A.propose(self.path, "commit_locale", "dopo la manomissione", now=T0)
            ev = E.build(now=T0 + dt.timedelta(hours=1), actions_path=self.path)
            self.assertEqual((ev["fonti"]["core.azioni"]["stato"], [e for e in ev["eventi"] if e["area"] == "core"]),
                             ("illeggibile", []), name)

    def test_scritture_concorrenti_da_processi_diversi(self):
        script = ("import sys; sys.path.insert(0, %r); import cuore_actions as A\n"
                  "for i in range(15): A.propose(%r, 'commit_locale', 'processo %%s richiesta %%d' %% (sys.argv[1], i))"
                  % (os.path.join(APP, "bridge"), self.path))
        procs = [subprocess.Popen([sys.executable, "-B", "-c", script, str(n)]) for n in range(4)]
        self.assertEqual([p.wait(timeout=120) for p in procs], [0, 0, 0, 0])
        doc = A.read(self.path)
        self.assertTrue(doc["integro"], doc["problemi"][:3])
        self.assertEqual(len(doc["azioni"]), 60)


class RigaDiComando(Base):
    def run_cli(self, args, stdin):
        out = io.StringIO()
        code = A.main(["--registro", self.path] + args, stdin=stdin, out=out)
        return code, out.getvalue()

    def test_decisione_solo_da_terminale_interattivo_con_codice(self):
        code, out = self.run_cli(["proponi", "ripristina_registry", "snapshot verificato del 24/10"], io.StringIO())
        a = json.loads(out)["azione"]
        self.assertEqual((code, json.loads(out)["da"]), (0, "agente"))                       # senza terminale: agente
        n = len(self.lines())
        self.assertEqual(self.run_cli(["approva", a], io.StringIO(a[-6:] + "\n"))[0], 77)    # nessun terminale
        self.assertEqual(self.run_cli(["approva", a], Tty("sbagliato\n"))[0], 1)             # codice diverso
        self.assertEqual(len(self.lines()), n)
        code, out = self.run_cli(["approva", a], Tty(a[-6:] + "\n"))
        self.assertEqual(code, 0)
        self.assertIn("ripristina_registry", out)
        self.assertTrue(A.approved_and_valid(self.path, a))
        code, _ = self.run_cli(["esito", a, "eseguita", "--riferimento", "rapporto-ripristino.json"], io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(self.run_cli(["esito", a, "eseguita"], io.StringIO())[0], 2)         # seconda esecuzione rifiutata

    def test_elenco_e_verifica(self):
        self.run_cli(["proponi", "commit_locale", "commit del blocco eventi"], io.StringIO())
        code, out = self.run_cli(["verifica"], io.StringIO())
        self.assertEqual((code, json.loads(out)), (0, {"integro": True, "problemi": [], "azioni": 1}))
        self.rewrite([self.lines()[0].replace("commit del blocco", "commit DIVERSO")])
        self.assertEqual(self.run_cli(["verifica"], io.StringIO())[0], 3)


class NelRegistroDegliEventi(Base):
    def test_azioni_come_eventi_senza_motivo(self):
        a = A.propose(self.path, "importa_estratto", "MOTIVO PRIVATO estratto", now=T0)["azione"]
        b = A.propose(self.path, "commit_locale", "commit", now=T0 + dt.timedelta(minutes=1))["azione"]
        A.decide(self.path, b, True, now=T0 + dt.timedelta(minutes=2))
        A.record_outcome(self.path, b, False, now=T0 + dt.timedelta(minutes=3))
        doc = E.build(now=T0 + dt.timedelta(hours=1), actions_path=self.path)
        self.assertEqual(E.validate(doc), [])
        ev = {e["fonte"]["riferimento"]: e for e in doc["eventi"] if e["tipo"] == "azione"}
        self.assertEqual((ev["azioni.jsonl#" + a]["esito"], ev["azioni.jsonl#" + a]["gravita"]), ("richiesta", "avviso"))
        self.assertEqual((ev["azioni.jsonl#" + b]["esito"], ev["azioni.jsonl#" + b]["gravita"],
                          ev["azioni.jsonl#" + b]["dettaglio"]["passi"]), ("fallita", "errore", 3))
        self.assertEqual(doc["fonti"]["core.azioni"], {"stato": "ok", "letti": 2, "scartati": 0, "eventi_prodotti": 2})
        self.assertNotIn("MOTIVO PRIVATO", json.dumps(doc))
        self.assertEqual(E.build(now=T0, actions_path=os.path.join(self.dir, "assente.jsonl"))["fonti"]["core.azioni"]["stato"],
                         "assente")


class ServerMcp(Base):
    def call(self, srv, name, args=None, mid=1):
        r = srv._handle({"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": {"name": name, "arguments": args or {}}})
        res = r["result"]
        return res["isError"], (json.loads(res["content"][0]["text"]) if not res["isError"] else res["content"][0]["text"])

    def test_strumenti_proposta_senza_approvazione(self):
        srv = M.build_server(lambda: E.build(now=T0 + dt.timedelta(hours=1), actions_path=self.path), self.path)
        init = srv._handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})["result"]
        self.assertIn("titolare", init["instructions"])
        tools = {t["name"] for t in srv._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
        self.assertEqual(tools, {"cuore_eventi", "cuore_cerca", "cuore_azioni", "cuore_proponi_azione"})   # nessuno per decidere
        err, out = self.call(srv, "cuore_proponi_azione", {"tipo": "collega_fonte", "motivo": "Finance nel registro eventi"})
        self.assertFalse(err)
        self.assertEqual(out["stato"], "richiesta")
        err, acts = self.call(srv, "cuore_azioni")
        self.assertEqual((acts["integro"], acts["azioni"][0]["stato"], acts["azioni"][0]["eseguibile"]), (True, "richiesta", False))
        self.assertTrue(self.call(srv, "cuore_approva", {"azione": out["azione"]})[0])        # strumento inesistente
        self.assertTrue(self.call(srv, "cuore_proponi_azione", {"tipo": "cancella_tutto", "motivo": "x"})[0])
        err, ev = self.call(srv, "cuore_eventi", {"area": "core"})
        self.assertEqual([e["esito"] for e in ev["eventi"]], ["richiesta"])

    def test_non_configurato_non_crea_file(self):
        srv = M.build_server(lambda: E.build(now=T0), None)
        self.assertEqual(self.call(srv, "cuore_azioni")[1]["configurato"], False)
        self.assertEqual(self.call(srv, "cuore_proponi_azione", {"tipo": "commit_locale", "motivo": "x"})[1]["configurato"], False)
        self.assertEqual(os.listdir(self.dir), [])

    def test_eventi_validati_e_filtrati(self):
        feed = {"operazioni": {"data_status": {"ok": True, "data": {"accounts": [], "anomalies": [
            {"code": "stale_data", "severity": "warning", "accountId": "conto", "message": "x"}]}}}}
        srv = M.build_server(lambda: E.build(now=T0, money_feed=feed), None)
        self.assertEqual([e["tipo"] for e in self.call(srv, "cuore_eventi", {"area": "finance"})[1]["eventi"]], ["anomalia_dati"])
        self.assertEqual(self.call(srv, "cuore_eventi", {"area": "school"})[1]["eventi"], [])
        bad = E.build(now=T0, money_feed=feed)
        bad["eventi"][0]["dettaglio"]["messaggio"] = "testo libero"
        srv = M.build_server(lambda: bad, None)
        self.assertEqual(self.call(srv, "cuore_eventi")[1], {"schema": E.SCHEMA, "errore": "contratto_non_valido", "problemi": 1})

    def test_protocollo_stdio(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "cuore_proponi_azione", "arguments": {"tipo": "commit_locale", "motivo": "prova stdio"}}}]
        r = subprocess.run(["/usr/bin/python3", "-B", os.path.join(APP, "bridge", "cuore_mcp.py"), "--azioni", self.path],
                           input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True, timeout=120)
        out = [json.loads(line) for line in r.stdout.splitlines()]                           # stdout: solo protocollo
        self.assertEqual([o["id"] for o in out], [1, 2, 3])
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "cuore")
        self.assertFalse(out[2]["result"]["isError"])
        self.assertEqual(A.read(self.path)["azioni"][0]["richiesta_da"], "agente")


if __name__ == "__main__":
    unittest.main()
