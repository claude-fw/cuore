"""Registro unico degli eventi (`cuore_events.py`, `cuore.events/1`): vista calcolata in sola lettura. Solo fonti SINTETICHE in
cartelle temporanee (log, calendario, state.json, Registry, feed Money): nessun file reale viene letto."""
import datetime as dt, hashlib, json, os, plistlib, shutil, sqlite3, sys, tempfile, unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
ENGINE = os.path.normpath(os.path.join(APP, "..", "scuola", "mcp", "classeviva"))
sys.path[:0] = [os.path.join(APP, "bridge"), os.path.join(ENGINE, "tests"), ENGINE, os.path.join(ENGINE, "..", "_common"),
                HERE]                                       # test_money_bridge anche quando questo file si lancia da solo
import cuore_events as E  # noqa: E402
import test_school_records as TR  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC).replace(microsecond=0)


def line(t, evento, **kw):
    return json.dumps(dict(ts=t.isoformat(), evento=evento, **kw), sort_keys=True)


def run_lines(t0, esito="OK", minutes=2, anomalie=()):
    t1 = t0 + dt.timedelta(minutes=minutes)
    return [line(t0, "avvio", modo="commit", fonti=["classroom", "classeviva"]),
            line(t1 - dt.timedelta(seconds=5), "fonte", fonte="classroom", esito="ok"),
            line(t1 - dt.timedelta(seconds=4), "fonte", fonte="classeviva", esito="parziale" if esito == "PARZIALE" else "ok"),
            line(t1, "fine", esito=esito, exit_code={"OK": 0, "PARZIALE": 1}[esito], durata_s=42.0, anomalie=list(anomalie),
                 oltre_soglia=[])]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.log = os.path.join(self.dir, "school-job.jsonl")
        self.plist = os.path.join(self.dir, "cloud.scuola.job.plist")
        self.state = os.path.join(self.dir, "job")
        self.reg = os.path.join(self.dir, "registry.sqlite3")
        with open(self.plist, "wb") as f:
            plistlib.dump({"Label": "cloud.scuola.job", "StartCalendarInterval": [{"Hour": 6, "Minute": 45},
                                                                                {"Hour": 14, "Minute": 30}, {"Hour": 20, "Minute": 0}]}, f)
        # log che inizia 29 ore fa: gli orari prima sono «non verificabili», quelli dopo senza avvio «mancati»
        self.write_log(run_lines(NOW - dt.timedelta(hours=29)) +
                       run_lines(NOW - dt.timedelta(hours=3), esito="PARZIALE", anomalie=["titoli_non_determinabili:classroom/x"]))
        os.makedirs(self.state)
        self.write_state({"problemi": {
            "parziale_temporaneo:classroom": {"dal": (NOW - dt.timedelta(hours=3)).isoformat(), "ultima_notifica": None,
                                              "titolo": "Cloud Scuola: sincronizzazione parziale ripetuta", "giri": 1,
                                              "attesa_conferma": 2},
            "anomalia:assenze_ambigue:cvv:9999999": {"dal": (NOW - dt.timedelta(hours=5)).isoformat(),
                                                    "ultima_notifica": (NOW - dt.timedelta(hours=5)).isoformat(),
                                                    "titolo": "Cloud Scuola: controllo richiesto", "giri": 2},
            "freschezza": {"titolo": "Cloud Scuola: dati non aggiornati"},          # senza data: incompleto
            "rotto": "non un oggetto"}, "ultimo": {}})
        reg = TR.make(path=self.reg)                                              # run reali del motore (grade, absence)
        reg.record_failed_run("classeviva", "notice", "timed out con SEGRETO-SINTETICO-XYZ")
        reg.close()

    def write_log(self, lines, mode="w"):
        with open(self.log, mode, encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def write_state(self, obj):
        with open(os.path.join(self.state, "state.json"), "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def build(self, **kw):
        args = dict(now=NOW, log_path=self.log, plist_path=self.plist, state_dir=self.state, registry_path=self.reg)
        args.update(kw)
        return E.build(**args)

    def files(self):
        def sha(path):
            with open(path, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        return {f: sha(os.path.join(dp, f)) for dp, _, fs in os.walk(self.dir) for f in fs}


class Contratto(Base):
    def test_eventi_normalizzati_con_tutte_le_nature(self):
        doc = self.build()
        self.assertEqual(E.validate(doc), [])
        nat = {e["natura"] for e in doc["eventi"]}
        self.assertEqual(nat, set(E.NATURE))                       # fatto, stato corrente, inferenza, anomalia, non verificato
        tipi = {e["tipo"] for e in doc["eventi"]}
        for t in ("giro_job", "anomalia_giro", "orario_mancato", "orario_non_verificabile", "problema_aperto",
                  "notifica_inviata", "run_registro"):
            self.assertIn(t, tipi, t)
        giri = [e for e in doc["eventi"] if e["tipo"] == "giro_job"]
        self.assertEqual([g["esito"] for g in giri], ["parziale", "ok"])          # piu' recenti prima
        self.assertEqual(giri[0]["gravita"], "avviso")
        self.assertIsNotNone(giri[0]["acquisito"])                               # fine registrata
        runs = [e for e in doc["eventi"] if e["tipo"] == "run_registro"]
        self.assertEqual({r["esito"] for r in runs}, {"ok", "error"})
        self.assertTrue(all(r["acquisito"] for r in runs))                       # finished_at del Registry
        prob = {e["esito"]: e for e in doc["eventi"] if e["tipo"] == "problema_aperto"}
        self.assertEqual(prob["in_osservazione"]["gravita"], "info")
        self.assertEqual(prob["aperto"]["qualita"], "incompleto")                # senza 'dal': mai una data inventata
        self.assertIsNone(prob["aperto"]["quando"])
        self.assertEqual(doc["fonti"]["school.stato_job"], {"stato": "ok", "letti": 3, "scartati": 1, "eventi_prodotti": 4})

    def test_validatore_rifiuta_ogni_violazione_del_contratto(self):
        doc = self.build()
        self.assertEqual(E.validate(doc), [])
        i = next(n for n, e in enumerate(doc["eventi"]) if e["tipo"] == "problema_aperto" and e["quando"])
        j = next(n for n, e in enumerate(doc["eventi"]) if e["tipo"] == "giro_job")
        cases = {
            "tipo non ammesso": lambda d: d["eventi"][i].update(tipo="inventato"),
            "area o natura non ammesse": lambda d: d["eventi"][i].update(natura="fatto_osservato"),
            "chiavi del dettaglio fuori vocabolario": lambda d: d["eventi"][i]["dettaglio"].update(messaggio="x"),
            "valori non ammessi": lambda d: d["eventi"][j]["dettaglio"].update(modo="cvv:9999999"),
            "valori non ammessi ": lambda d: d["eventi"][j]["dettaglio"].update(modo="persona" + "@" + "esempio.invalid"),   # costruito: nessun indirizzo nel sorgente
            "valori non ammessi  ": lambda d: d["eventi"][j]["dettaglio"].update(modo="https://classroom.google.com/c/x"),
            "valori non ammessi   ": lambda d: d["eventi"][j]["dettaglio"].update(fonti={"classroom": {"profondo": {"x": 1}}}),
            "fonte o riferimento": lambda d: d["eventi"][i]["fonte"].update(riferimento="state.json#problemi/anomalia:cvv:9999999"),
            "non e' un istante UTC": lambda d: d["eventi"][i].update(quando=E._utc(d["eventi"][i]["quando"]).astimezone(
                dt.timezone(dt.timedelta(hours=1))).isoformat()),
            "ordine non rispettato": lambda d: d["eventi"].reverse(),
            "conteggi non coerenti": lambda d: d["conteggi"].update({"school/inferenza": 99}),
            "diagnostica": lambda d: d["diagnostica"].pop("duplicati_scartati"),
            "campi diversi": lambda d: d["eventi"][i].update(testo="libero"),
            "id non nel formato": lambda d: d["eventi"][i].update(id="evt:XYZ"),
            "fonti: stato": lambda d: d["fonti"]["school.log_job"].update(stato="forse"),
        }
        for want, mutate in cases.items():
            bad = json.loads(json.dumps(doc))
            mutate(bad)
            got = E.validate(bad)
            self.assertTrue(any(want.strip() in x for x in got), (want, got))

    def test_chiusure_in_attesa_senza_chiave_completa(self):
        fine = NOW - dt.timedelta(hours=1)
        self.write_state({"problemi": {}, "ultimo": {"fine": fine.astimezone(dt.timezone(dt.timedelta(hours=1))).isoformat()},
                          "risolti_in_attesa": {"temporaneo:classroom": "Cloud Scuola: Classroom non aggiornato da piu' giri",
                                                "anomalia:assenze_ambigue:cvv:9999999": "Cloud Scuola: controllo richiesto",
                                                "freschezza": 3}})
        doc = self.build()
        ch = [e for e in doc["eventi"] if e["tipo"] == "chiusura_in_attesa"]
        self.assertEqual(sorted((e["dettaglio"]["tipo_problema"], e["dettaglio"]["fonte"]) for e in ch),
                         [("anomalia", None), ("freschezza", None), ("temporaneo", "classroom")])
        self.assertTrue(all((e["natura"], e["esito"], e["quando"], e["acquisito"], e["qualita"]) ==
                            ("stato_corrente", "in_attesa", None, fine.isoformat(), "valido") for e in ch))
        text = json.dumps(doc)
        for bad in ("cvv:9999999", "assenze_ambigue", "controllo richiesto", "non aggiornato"):
            self.assertNotIn(bad, text, bad)
        self.assertEqual(E.validate(doc), [])

    def test_problema_temporaneo_del_job_riconosciuto(self):
        self.write_state({"problemi": {"temporaneo:classroom": {"dal": (NOW - dt.timedelta(hours=2)).isoformat(),
                                                                "ultima_notifica": None, "giri": 1, "attesa_conferma": 2,
                                                                "titolo": "Cloud Scuola: Classroom non aggiornato da piu' giri"}}})
        e = next(x for x in self.build()["eventi"] if x["tipo"] == "problema_aperto")
        self.assertEqual((e["dettaglio"]["tipo_problema"], e["dettaglio"]["fonte"], e["esito"], e["gravita"]),
                         ("temporaneo", "classroom", "in_osservazione", "info"))

    def test_nessun_testo_libero_ne_chiave_completa(self):
        text = json.dumps(self.build())
        for bad in ("SEGRETO-SINTETICO-XYZ", "timed out", "cvv:9999999", "assenze_ambigue", "classroom/x"):
            self.assertNotIn(bad, text, bad)

    def test_ordine_e_finestra(self):
        doc = self.build(days=1)
        whens = [e["quando"] for e in doc["eventi"] if e["quando"]]
        self.assertEqual(whens, sorted(whens, reverse=True))
        self.assertFalse([w for w in whens if E._utc(w) < NOW - dt.timedelta(days=1)])   # il giro di 29 ore fa e' escluso
        self.assertNotIn("ok", [e["esito"] for e in doc["eventi"] if e["tipo"] == "giro_job"])


class Riproducibilita(Base):
    def test_sola_lettura_e_seconda_lettura_identica(self):
        before = self.files()
        a, b = self.build(), self.build()
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(self.files(), before)                                     # nessun file nuovo (-wal/-shm) o cambiato

    def test_duplicati_e_righe_fuori_ordine(self):
        ref = self.build()
        with open(self.log, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.write_log(lines[::-1] + lines[:4])                                   # invertite + un giro ripetuto
        doc = self.build()
        self.assertEqual([e["id"] for e in doc["eventi"]], [e["id"] for e in ref["eventi"]])
        self.assertGreater(doc["diagnostica"]["duplicati_scartati"], 0)
        self.assertEqual(E.validate(doc), [])
        wide = self.build(days=3650)                                              # nessun evento fuori finestra
        self.assertEqual(sum(f["eventi_prodotti"] for f in wide["fonti"].values()),
                         len(wide["eventi"]) + wide["diagnostica"]["duplicati_scartati"])

    def test_istante_incoerente_contato_una_volta_anche_se_duplicato(self):
        future = run_lines(NOW + dt.timedelta(hours=2))
        self.write_log(future + future, mode="a")                                 # lo stesso giro futuro letto due volte
        doc = self.build()
        incoherent = [e for e in doc["eventi"] if e["qualita"] == "timestamp_incoerente"]
        self.assertEqual(len(incoherent), 1)
        self.assertEqual(doc["diagnostica"]["timestamp_incoerenti"], 1)
        self.assertGreater(doc["diagnostica"]["duplicati_scartati"], 0)


class RecordImperfetti(Base):
    def test_righe_illeggibili_istanti_incoerenti_run_non_validi(self):
        self.write_log(["{rotta", json.dumps({"ts": "non una data", "evento": "avvio"})], mode="a")
        self.write_log(run_lines(NOW + dt.timedelta(hours=2)), mode="a")             # nel futuro: incoerente
        conn = sqlite3.connect(self.reg)
        conn.execute("INSERT INTO runs(run_id,source,kind,started_at,status,finished_at,mode,outcome) VALUES(?,?,?,?,?,?,?,?)",
                     ("incoerente", "classeviva", "lesson", (NOW - dt.timedelta(hours=1)).isoformat(), "ok",
                      (NOW - dt.timedelta(hours=2)).isoformat(), "commit", "ok"))
        conn.commit()
        conn.close()
        doc = self.build()
        self.assertEqual(doc["diagnostica"]["record_non_validi"], 2)
        self.assertGreaterEqual(doc["diagnostica"]["timestamp_incoerenti"], 2)
        q = {e["fonte"]["riferimento"]: e["qualita"] for e in doc["eventi"]}
        self.assertEqual(q["run_id incoerente"], "timestamp_incoerente")
        self.assertIn("timestamp_incoerente", [e["qualita"] for e in doc["eventi"] if e["tipo"] == "giro_job"])
        self.assertEqual(doc["fonti"]["school.log_job"]["stato"], "con_righe_illeggibili")
        self.assertEqual(E.validate(doc), [])

    def test_fonti_assenti_o_illeggibili(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty)
        doc = E.build(now=NOW, log_path=os.path.join(empty, "x.jsonl"), plist_path=None, state_dir=empty,
                      registry_path=os.path.join(empty, "r.sqlite3"))
        self.assertEqual(doc["eventi"], [])
        self.assertEqual({k: v["stato"] for k, v in doc["fonti"].items()},
                         {"school.log_job": "assente", "school.calendario": "non_configurata", "school.stato_job": "assente",
                          "school.registry_runs": "assente", "finance.data_status": "non_collegata",
                          "core.azioni": "non_configurata", "finance.sincronizzazione": "non_configurata"})
        self.write_state("{non json")
        with open(self.reg + "-wal", "wb") as f:
            f.write(b"x")                                                         # scrittore attivo: copia rifiutata
        doc = self.build()
        self.assertEqual(doc["fonti"]["school.stato_job"]["stato"], "illeggibile")
        self.assertEqual(doc["fonti"]["school.registry_runs"]["stato"], "illeggibile")
        self.assertIn("giro_job", {e["tipo"] for e in doc["eventi"]})            # le altre fonti restano utilizzabili


class AdattatoreFinance(Base):
    FEED = {"operazioni": {"data_status": {"ok": True, "data": {
        "accounts": [{"accountId": "conto-demo", "lastImportAt": (NOW - dt.timedelta(days=2)).isoformat(),
                      "lastImportStatus": "committed", "lastBookedOn": "2026-01-01", "pendingCount": 0},
                     {"accountId": "mai-importato", "lastImportAt": None}, "non un oggetto"],
        "anomalies": [{"code": "stale_data", "severity": "warning", "accountId": "conto-demo"},
                      {"code": 3, "severity": "warning"}]}}}}

    def test_solo_metadati_di_importazione_e_anomalie(self):
        doc = self.build(money_feed=self.FEED)
        fin = [e for e in doc["eventi"] if e["area"] == "finance"]
        self.assertEqual(sorted((e["tipo"], e["natura"]) for e in fin),
                         [("anomalia_dati", "anomalia"), ("importazione_riuscita", "fatto_osservato")])
        self.assertEqual(doc["fonti"]["finance.data_status"]["scartati"], 2)
        an = next(e for e in fin if e["tipo"] == "anomalia_dati")
        self.assertEqual((an["gravita"], an["quando"], an["qualita"]), ("avviso", None, "incompleto"))
        tail = [e["quando"] for e in doc["eventi"]][-2:]
        self.assertEqual(tail, [None, None])                                      # senza istante: in coda
        self.assertIn(an["id"], [e["id"] for e in doc["eventi"][-2:]])
        self.assertNotIn("lastBookedOn", json.dumps(fin))                         # nessun dato oltre i metadati
        self.assertEqual(E.validate(doc), [])

    def test_stato_importazione_dati_mancanti_anomalie_freschezza(self):
        """Codici reali del motore (consultation.ts): importazione rifiutata (con istante), nessuna importazione, importazione
        vecchia, saldo mancante. Messaggi e dettagli liberi non entrano mai nel registro."""
        rej = (NOW - dt.timedelta(hours=5)).isoformat()
        feed = {"operazioni": {"data_status": {"ok": True, "data": {
            "accounts": [{"accountId": "conto-a", "lastImportAt": (NOW - dt.timedelta(days=40)).isoformat(), "lastImportStatus": "rejected"},
                         {"accountId": "conto-b", "lastImportAt": None, "lastImportStatus": None}],
            "anomalies": [
                {"code": "last_import_rejected", "severity": "warning", "accountId": "conto-a", "message": "MESSAGGIO LIBERO",
                 "detail": {"at": rej}},
                {"code": "stale_import", "severity": "warning", "accountId": "conto-a", "message": "x", "detail": {"giorni": 40}},
                {"code": "no_imports", "severity": "warning", "accountId": "conto-b", "message": "x", "detail": {}},
                {"code": "balance_missing", "severity": "error", "accountId": "conto-b", "message": "x"},
                {"code": "stale_data", "severity": "warning", "accountId": None, "message": "x", "detail": {"at": "non una data"}}]}}}}
        doc = self.build(money_feed=feed, days=60)
        fin = {(e["tipo"], e["esito"]): e for e in doc["eventi"] if e["area"] == "finance"}
        self.assertEqual(fin[("importazione_riuscita", "committed")]["quando"], E._utc(feed["operazioni"]["data_status"]["data"]
                                                                                    ["accounts"][0]["lastImportAt"]).isoformat())
        self.assertNotIn(("importazione_riuscita", "rejected"), fin)                    # l'ultima esecuzione rifiutata non e'
                                                                                         # l'esito dell'importazione riuscita
        r = fin[("anomalia_dati", "last_import_rejected")]
        self.assertEqual((r["quando"], r["qualita"]), (E._utc(rej).isoformat(), "valido"))   # istante dichiarato dal motore
        self.assertEqual(fin[("anomalia_dati", "no_imports")]["qualita"], "incompleto")       # nessun istante: mai inventato
        self.assertEqual(fin[("anomalia_dati", "balance_missing")]["gravita"], "errore")
        self.assertEqual((fin[("anomalia_dati", "stale_data")]["quando"], fin[("anomalia_dati", "stale_data")]["fonte"]["riferimento"]),
                         (None, "generale"))
        self.assertFalse([e for e in doc["eventi"] if e["area"] == "finance" and e["tipo"] == "importazione_riuscita"
                          and e["dettaglio"]["conto"] == "conto-b"])                           # mai importato: nessun fatto
        text = json.dumps(doc)
        self.assertNotIn("MESSAGGIO LIBERO", text)
        self.assertNotIn('"giorni": 40', text)                                         # dettaglio libero del motore
        self.assertEqual(E.validate(doc), [])

    def test_periodi_e_date_distinguono_le_anomalie_senza_importi(self):
        """Il motore emette una `reconciliation_difference` per periodo, senza istante: due periodi dello stesso conto
        sono due anomalie, non un duplicato. Importi, esito e messaggio non entrano nel registro."""
        def rec(period, diff):
            return {"code": "reconciliation_difference", "severity": "error", "accountId": "carta",
                    "message": "riconciliazione non quadrata nel periodo %s" % period,
                    "detail": {"period": period, "outcome": "difference", "differenceMinor": diff}}
        feed = {"operazioni": {"data_status": {"ok": True, "data": {"accounts": [], "anomalies": [
            rec("2026-01-01..2026-01-15", -60465), rec("2026-01-15..2026-01-31", -19534),
            rec("periodo libero 123456", -1),                                     # forma inattesa: nessun periodo
            {"code": "balance_conflict", "severity": "error", "accountId": "carta", "message": "x",
             "detail": {"date": "2026-01-31"}}]}}}}
        doc = self.build(money_feed=feed)
        fin = [e for e in doc["eventi"] if e["area"] == "finance"]
        self.assertEqual(doc["diagnostica"]["duplicati_scartati"], 0)
        self.assertEqual(sorted((e["esito"], e["dettaglio"].get("periodo", ""), e["dettaglio"].get("data", "")) for e in fin),
                         [("balance_conflict", "", "2026-01-31"), ("reconciliation_difference", "", ""),
                          ("reconciliation_difference", "2026-01-01..2026-01-15", ""),
                          ("reconciliation_difference", "2026-01-15..2026-01-31", "")])
        text = json.dumps(doc)
        for bad in ("60465", "19534", "differenceMinor", "outcome", "periodo libero", "non quadrata"):
            self.assertNotIn(bad, text, bad)
        self.assertEqual(E.validate(doc), [])

    @unittest.skipUnless(shutil.which("node") or os.path.isfile("/usr/local/bin/node"), "node non disponibile")
    def test_dal_motore_finance_reale_su_database_sintetico(self):
        """Catena completa su un database SINTETICO in una cartella temporanea: motore Finance (consultazione v1) ->
        ponte Money (cuore_money.build_feed, contratto validato) -> adattatore degli eventi -> validatore."""
        import subprocess
        import cuore_money as CM
        import test_money_bridge as TM
        db = os.path.join(self.dir, "finance-sintetico.sqlite3")
        TM.build_demo(db)
        for date, amount in (("2026-01-01", "0.00"), ("2026-01-15", "777.00"), ("2026-01-31", "999.99")):
            subprocess.run([TM.NODE, os.path.join(TM.FINANCE, "src", "cli", "main.ts"), "--db", db, "balance:add",
                            "--account", "carta", "--date", date, "--amount", amount], cwd=TM.FINANCE, check=True,
                           capture_output=True, timeout=120)
        subprocess.run([TM.NODE, os.path.join(TM.FINANCE, "src", "cli", "main.ts"), "--db", db, "reconcile", "--account",
                        "carta", "--observed"], cwd=TM.FINANCE, check=True, capture_output=True, timeout=120)
        before = TM.sha(db)
        feed = CM.build_feed(db, today="2026-06-30")                              # cinque mesi dopo: dati fermi
        self.assertEqual(feed["errori"], [])
        doc = E.build(now=NOW, days=3650, money_feed=feed, log_path=None, plist_path=None, state_dir=None, registry_path=None)
        self.assertEqual(E.validate(doc), [])
        self.assertEqual(TM.sha(db), before)                                      # sola lettura
        fin = [e for e in doc["eventi"] if e["area"] == "finance"]
        got = sorted((e["tipo"], e["esito"], e["dettaglio"]["conto"], e["dettaglio"].get("periodo", "")) for e in fin)
        self.assertEqual(got, [("anomalia_dati", "reconciliation_difference", "carta", "2026-01-01..2026-01-15"),
                               ("anomalia_dati", "reconciliation_difference", "carta", "2026-01-15..2026-01-31"),
                               ("anomalia_dati", "stale_data", "carta", ""), ("anomalia_dati", "stale_data", "conto", ""),
                               ("importazione_riuscita", "committed", "carta", ""),
                               ("importazione_riuscita", "committed", "conto", "")])
        self.assertEqual((doc["diagnostica"]["duplicati_scartati"], doc["fonti"]["finance.data_status"]["scartati"]), (0, 0))
        text = json.dumps(doc)
        for bad in ("Conto DEMO", "Carta DEMO", "movimenti disponibili", "differenceMinor", "lastMovementOn", "amountMinor"):
            self.assertNotIn(bad, text, bad)

    def test_sincronizzazione_bancaria_e_consensi_ricalcolati(self):
        """File di stato di finance/scripts/bank-sync.ts: ultimo tentativo per conto e consenso, ricalcolato a `now`
        (il valore scritto nel file puo' essere vecchio). Nessun importo, descrizione o identificativo del fornitore."""
        path = os.path.join(self.dir, "banca-stato.json")
        today = NOW.date()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"schema": "cuore.bank.sync_status/1", "generato": (NOW - dt.timedelta(hours=1)).isoformat(), "database": "copia",
                       "connessioni": [
                           {"conto": "rev-eur", "banca": {"nome": "Revolut", "paese": "LT"},
                            "consenso": {"stato": "valido", "giorni_rimasti": 100, "scade_il": (today + dt.timedelta(days=5)).isoformat()},
                            "ultimo_tentativo": {"at": (NOW - dt.timedelta(hours=1)).isoformat(), "esito": "importata", "errore": None,
                                                 "riprova_dopo_ore": None, "nuovi": 3, "aggiornati": 1, "saldi": 1,
                                                 "importo": "250.00", "descrizione": "SEGRETO"}},
                           {"conto": "mps-eur", "banca": {"nome": "MPS", "paese": "IT"},
                            "consenso": {"scade_il": (today - dt.timedelta(days=1)).isoformat()},
                            "ultimo_tentativo": {"at": (NOW - dt.timedelta(hours=2)).isoformat(), "esito": "interrotta",
                                                 "errore": "consent_expired", "riprova_dopo_ore": None}},
                           {"conto": "cvv:9999999 con spazi"}]}, f)
        doc = self.build(bank_status_path=path)
        self.assertEqual(E.validate(doc), [])
        ev = {(e["tipo"], e["dettaglio"]["conto"]): e for e in doc["eventi"] if e["area"] == "finance"}
        sync = ev[("sincronizzazione_bancaria", "rev-eur")]
        self.assertEqual((sync["esito"], sync["gravita"], sync["dettaglio"]["nuovi"], sync["dettaglio"]["database"]),
                         ("importata", "info", 3, "copia"))
        self.assertEqual(ev[("consenso_bancario", "rev-eur")]["esito"], "in_scadenza")          # 5 giorni: ricalcolato, non «valido»
        self.assertEqual((ev[("consenso_bancario", "mps-eur")]["esito"], ev[("consenso_bancario", "mps-eur")]["gravita"]),
                         ("scaduto", "errore"))
        self.assertEqual(ev[("sincronizzazione_bancaria", "mps-eur")]["gravita"], "errore")      # consenso scaduto
        self.assertEqual(doc["fonti"]["finance.sincronizzazione"]["scartati"], 1)
        text = json.dumps(doc)
        for bad in ("250.00", "SEGRETO", "cvv:", "importo"):
            self.assertNotIn(bad, text, bad)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{rotto")
        self.assertEqual(self.build(bank_status_path=path)["fonti"]["finance.sincronizzazione"]["stato"], "illeggibile")

    def test_feed_non_disponibile(self):
        doc = self.build(money_feed={"operazioni": {"data_status": {"ok": False, "error": {"code": "x"}}}})
        self.assertEqual(doc["fonti"]["finance.data_status"]["stato"], "illeggibile")


class Endpoint(Base):
    def server(self, events_fn):
        import threading
        import serve
        httpd = serve.ThreadingHTTPServer((serve.BIND, 0), None)
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = serve.make_handler(port, feed_fn=lambda: {"schema": "x"}, events_fn=events_fn)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return port

    def req(self, port, method="GET", host=None, headers=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request(method, "/api/events", headers=dict({"Host": host or "127.0.0.1:%d" % port}, **(headers or {})))
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def test_endpoint_in_sola_lettura_con_le_protezioni_del_ponte(self):
        before = self.files()
        port = self.server(lambda: self.build())
        code, body = self.req(port)
        doc = json.loads(body)
        self.assertEqual((code, doc["schema"], E.validate(doc)), (200, "cuore.events/1", []))
        self.assertEqual(self.files(), before)
        for m in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.req(port, m)[0], 405, m)
        self.assertEqual(self.req(port, host="evil.example:%d" % port)[0], 403)
        self.assertEqual(self.req(port, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_non_collegato_non_legge_nulla(self):
        code, body = self.req(self.server(None))
        self.assertEqual((code, json.loads(body)["eventi"]), (503, []))

    def test_registro_non_calcolabile_o_non_conforme_dichiarato(self):
        def boom():
            raise OSError("/percorso/privato/segreto")
        code, body = self.req(self.server(boom))
        doc = json.loads(body)
        self.assertEqual((code, doc["eventi"], doc["errori"][0]["codice"]), (503, [], "non_disponibile"))
        self.assertNotIn("segreto", body.decode())                                # solo il tipo d'errore
        bad = self.build()
        bad["eventi"][0]["dettaglio"]["messaggio"] = "testo libero"
        code, body = self.req(self.server(lambda: bad))
        doc = json.loads(body)
        self.assertEqual((code, doc["eventi"], doc["errori"][0]["codice"]), (503, [], "contratto_non_valido"))
        self.assertNotIn("testo libero", body.decode())


HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const el = () => ({ innerHTML: "", textContent: "", dataset: {}, setAttribute() {}, scrollIntoView() {} });
const els = {};
const ctx = { console, Intl, Math, Date, String, Number, Object, Array, JSON, RegExp, Set, location: { hash: "", protocol: "file:" },
  history: { replaceState() {}, pushState() {} }, matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {}, scrollTo() {},
  document: { documentElement: { dataset: {} }, getElementById: id => (els[id] = els[id] || el()), querySelectorAll: () => [], querySelector: () => null, addEventListener() {} } };
ctx.window = ctx; vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const C = ctx.CUORE, out = {};
out.senza = C.viewHtml(C.parse("cuore/diagnostica")); out.home_senza = C.viewHtml(C.parse(""));
C.EVENTS_STATE.doc = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
out.con = C.viewHtml(C.parse("cuore/diagnostica")); out.home_con = C.viewHtml(C.parse(""));
(async () => {
  // ponte raggiungibile, registro dichiarato non disponibile (503 con errori): scheda con il motivo, mai «nessun evento»
  ctx.location.protocol = "http:";
  ctx.fetch = async () => ({ status: 503, json: async () => ({ schema: "cuore.events/1", eventi: [], fonti: {},
    errori: [{ ambito: "registro", codice: "contratto_non_valido", messaggio: "registro non conforme al contratto (2 problemi)" }] }) });
  await C.loadEvents();
  out.errore = C.viewHtml(C.parse("cuore/diagnostica"));
  console.log(JSON.stringify(out));
})();
"""


class InSistema(Base):
    def test_eventi_rilevanti_in_sistema_letture_riuscite_contate(self):
        import re, subprocess
        node = shutil.which("node") or ("/usr/local/bin/node" if os.path.isfile("/usr/local/bin/node") else None)
        if not node:
            self.skipTest("node non disponibile")
        doc = self.build()
        noon = dt.datetime(2026, 11, 2, 12, 0, tzinfo=UTC)                           # ora solare: 13:00 a Roma
        doc["eventi"].insert(0, E.event("school", "giro_job", "fatto_osservato", ["t"], noon, "school-job.jsonl", "t", esito="ok"))
        with open(os.path.join(APP, "index.html"), encoding="utf-8") as fh:
            script = re.search(r"<script>(.*?)</script>", fh.read(), re.S).group(1)
        p = {n: os.path.join(self.dir, n) for n in ("page.js", "h.js", "doc.json")}
        for n, text in (("page.js", script), ("h.js", HARNESS), ("doc.json", json.dumps(doc))):
            with open(p[n], "w", encoding="utf-8") as fh:
                fh.write(text)
        r = subprocess.run([node, p["h.js"], p["page.js"], p["doc.json"]], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        v = json.loads(r.stdout)
        self.assertNotIn("Eventi recenti", v["senza"])                              # registro assente: nessuna scheda
        self.assertNotIn("Eventi recenti", v["home_con"])                           # la Home resta un punto di ingresso
        h = v["con"]
        for w in ('data-id="Y.S.9"', "giro del job", "problema aperto", "orario senza avvio", "notifica inviata",
                  "lettura nel Registry", "dedotto", "stato attuale", "letture riuscite del Registry non elencate", "13:00"):
            self.assertIn(w, h, w)
        self.assertIn('chip warn">error', h)                                       # lettura del Registry non riuscita: avviso
        for bad in ("undefined", "NaN", "[object", "SEGRETO", "cvv:"):
            self.assertNotIn(bad, h, bad)
        err = v["errore"]
        self.assertIn("Registro degli eventi non disponibile", err)
        self.assertIn("registro non conforme al contratto (2 problemi)", err)
        self.assertNotIn("Nessun evento da segnalare", err)

    def test_chiusure_e_anomalie_finance_leggibili_in_sistema(self):
        import re, subprocess
        node = shutil.which("node") or ("/usr/local/bin/node" if os.path.isfile("/usr/local/bin/node") else None)
        if not node:
            self.skipTest("node non disponibile")
        self.write_state({"problemi": {}, "risolti_in_attesa": {"temporaneo:classroom": "x"}})
        feed = {"operazioni": {"data_status": {"ok": True, "data": {"accounts": [], "anomalies": [
            {"code": "reconciliation_difference", "severity": "error", "accountId": "carta", "message": "x",
             "detail": {"period": "2026-01-01..2026-01-15", "differenceMinor": -1}}]}}}}
        doc = self.build(money_feed=feed, log_path=None, plist_path=None, registry_path=None)
        with open(os.path.join(APP, "index.html"), encoding="utf-8") as fh:
            script = re.search(r"<script>(.*?)</script>", fh.read(), re.S).group(1)
        p = {n: os.path.join(self.dir, n) for n in ("page.js", "h.js", "doc.json")}
        for n, text in (("page.js", script), ("h.js", HARNESS), ("doc.json", json.dumps(doc))):
            with open(p[n], "w", encoding="utf-8") as fh:
                fh.write(text)
        r = subprocess.run([node, p["h.js"], p["page.js"], p["doc.json"]], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        h = json.loads(r.stdout)["con"]
        for w in ("chiusura in attesa", "errori temporanei (rete o Mac in stop) · Classroom", "riconciliazione non quadrata",
                  "conto carta", "2026-01-01 → 2026-01-15"):
            self.assertIn(w, h, w)
        self.assertNotIn("undefined", h)


if __name__ == "__main__":
    unittest.main()
