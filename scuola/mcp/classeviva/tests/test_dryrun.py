"""Dry-run live: nessuna scrittura, risultato reale separato dalla simulazione, baseline in sola lettura."""
import copy, hashlib, inspect, json, os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(ROOT, "..", "_common"))
import cvv_client as cv
import cvv_dryrun as D
import cvv_registry as CR
import registry as R
import test_bridge as TB

NOTICE = {"pubId": 1, "pubDT": "2026-10-29T12:00:00+02:00", "readStatus": False, "evtCode": "CF", "cntId": 9,
          "cntValidFrom": "2026-10-29", "cntValidTo": "2027-06-01", "cntValidInRange": True, "cntStatus": "active",
          "cntTitle": "Circolare", "cntNum": "1", "cntCategory": "", "cntHasChanged": False, "cntHasAttach": True,
          "needJoin": False, "needReply": False, "needFile": False, "needSign": False, "evento_id": "9",
          "dinsert_allegato": "2026-10-29 12:00:00", "attachments": []}
ABSENCE = {"evtId": 1, "evtCode": "ABA0", "evtDate": "2026-10-27", "evtHPos": 0, "evtValue": 0, "hoursAbsence": [],
           "isJustified": False, "justifReasonCode": None, "justifReasonDesc": None, "webJustifStatus": 0}


def notices(n=3):
    out = []
    for i in range(n):
        r = copy.deepcopy(NOTICE); r["pubId"] += i; r["pubDT"] = "2026-10-0%dT12:00:00+02:00" % (i + 1)
        r["cntTitle"] = "Circolare %d" % i
        out.append(r)
    return out


def absences(n=3):
    out = []
    for i in range(n):
        r = dict(ABSENCE); r["evtId"] += i; r["evtDate"] = "2026-09-2%d" % (i + 1)
        out.append(r)
    return out


def fetchers(**over):
    f = {"agenda_event": (lambda: [], ("2026-09-29", "2027-08-31")), "grade": (lambda: [], None),
         "absence": (lambda: absences(), None), "notice": (lambda: notices(), None)}
    f.update(over)
    return f


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "reg", "registry.sqlite3")      # NON esiste

    def run_dry(self, f=None, **kw):
        return D.run(f or fetchers(), baseline_path=self.path, **kw)


class NessunaScrittura(Base):
    def test_without_real_registry_nothing_is_created(self):
        res = self.run_dry()
        self.assertFalse(os.path.exists(os.path.dirname(self.path)))             # nemmeno la cartella
        self.assertEqual(os.listdir(self.tmp.name), [])
        self.assertTrue(res["verifica_nessuna_scrittura"]["stato_prima_uguale_a_dopo"])
        self.assertFalse(any(res["scritture"].values()))
        self.assertFalse(res["baseline"]["esiste"])
        self.assertIn("VUOTA", res["baseline"]["nota"])
        self.assertEqual(res["registro_di_lavoro"], "SOLO IN MEMORIA")

    def test_existing_real_registry_is_not_touched(self):
        real = R.Registry(self.path)
        CR.sync_kind(real, "notice", lambda: notices(3))
        CR.sync_kind(real, "absence", lambda: absences(3))
        rec = real.get_record(CR.SOURCE, "notice", "1")
        real.record_projection(CR.SOURCE, "notice", "1", "storico", "p", rec["content_hash"], "r0-vecchia")
        real.close()
        before_hash, before_ls = sha(self.path), sorted(os.listdir(os.path.dirname(self.path)))
        changed = notices(3)[:2]; changed[0]["cntTitle"] = "Circolare rettificata"        # 0 cambia, 2 sparisce
        res = self.run_dry(fetchers(notice=(lambda: changed, None), absence=(lambda: absences(3), None)))
        self.assertEqual(sha(self.path), before_hash)                            # nemmeno un byte
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.path))), before_ls)   # nessun -wal/-shm/-journal
        self.assertTrue(res["verifica_nessuna_scrittura"]["stato_prima_uguale_a_dopo"])
        self.assertTrue(res["baseline"]["esiste"])
        self.assertEqual(res["baseline"]["record_caricati"], 6)
        n = res["RISULTATO_REALE"]["letture"]["notice"]
        self.assertEqual((n["conteggi"]["update"], n["conteggi"]["unchanged"], n["conteggi"]["missing"]), (1, 1, 1))
        self.assertNotIn("piano_proiezione", n)                                   # nessun piano verso superfici esterne
        a = res["RISULTATO_REALE"]["letture"]["absence"]
        self.assertEqual(a["conteggi"]["unchanged"], 3)
        # il registro reale e' ancora quello di prima (riletto)
        again = R.Registry.from_snapshot(self.path)
        self.assertEqual(again.get_record(CR.SOURCE, "notice", "1")["version"], 1)
        self.assertEqual(len(again.list_records()), 6)                           # stessi 6 record di prima
        self.assertEqual(len(again.list_records(status="missing_in_source")), 0)

    def test_nonempty_wal_is_refused_and_unreadable_baseline_is_not_replaced_by_empty(self):
        os.makedirs(os.path.dirname(self.path))
        R.Registry(self.path).close()
        with open(self.path + "-wal", "wb") as fh:
            fh.write(b"x" * 64)
        res = self.run_dry()
        self.assertIn("errore_baseline", res)
        self.assertNotIn("RISULTATO_REALE", res)
        os.remove(self.path + "-wal")
        with open(self.path, "wb") as fh:
            fh.write(b"questo non e' un database sqlite" * 10)
        res = self.run_dry()
        self.assertIn("errore_baseline", res)
        self.assertNotIn("RISULTATO_REALE", res)


class SeparazioneRealeSimulazione(Base):
    def test_two_sections_and_banner(self):
        res = self.run_dry()
        self.assertIn("RISULTATO_REALE", res)
        self.assertIn("SIMULAZIONE_NON_ACCADUTA", res)
        sim = res["SIMULAZIONE_NON_ACCADUTA"]
        self.assertIn("NON ACCADUTO", sim["AVVISO"])
        steps = [s for k in sim["per_kind"].values() if k["eseguita"] for s in k["passi"]]
        self.assertTrue(steps)
        self.assertTrue(all(s["passo"].startswith("[SIMULATO]") for s in steps))

    def test_real_section_never_contains_simulated_data(self):
        res = self.run_dry()
        text = json.dumps(res["RISULTATO_REALE"], ensure_ascii=False)
        self.assertNotIn("SIMULAT", text)
        self.assertNotIn("(SIMULATO)", text)

    def test_simulation_does_not_change_real_result(self):
        a = self.run_dry(simulate=True)["RISULTATO_REALE"]
        b = self.run_dry(simulate=False)["RISULTATO_REALE"]
        self.assertEqual(a, b)
        self.assertEqual(self.run_dry(simulate=False)["SIMULAZIONE_NON_ACCADUTA"], {"eseguita": False})

    def test_simulation_covers_all_paths(self):
        sim = self.run_dry()["SIMULAZIONE_NON_ACCADUTA"]["per_kind"]["notice"]
        self.assertTrue(sim["eseguita"])
        p = {s["passo"].replace("[SIMULATO] ", ""): s for s in sim["passi"]}
        self.assertEqual(p["lettura identica"]["conteggi"], {"unchanged": 3})
        self.assertEqual(p["dato cambiato / stato letto"]["conteggi"], {"update": 1, "state_change": 1, "unchanged": 1})
        self.assertEqual(p["dato cambiato / stato letto"]["versioni_primo_record"], [1, 2])
        self.assertEqual(p["record sparito"]["conteggi"]["missing"], 1)
        self.assertEqual(p["record sparito"]["stato_record_sparito"], "missing_in_source")
        self.assertEqual(p["lettura vuota"]["conteggi"], {"missing_refused": 2})
        self.assertTrue(p["lettura vuota"]["avvisi"])
        self.assertNotIn("logica interpretativa cambiata", p)
        self.assertEqual(sim["integrita_copia"], "OK")

    def test_small_or_empty_sample_is_not_padded_with_invented_data(self):
        res = self.run_dry(fetchers(absence=(lambda: absences(1), None)))
        s = res["SIMULAZIONE_NON_ACCADUTA"]["per_kind"]
        self.assertFalse(s["absence"]["eseguita"])
        self.assertIn("campione insufficiente", s["absence"]["motivo"])
        self.assertFalse(s["grade"]["eseguita"])                                    # 0 voti: nessun voto inventato
        g = res["RISULTATO_REALE"]["letture"]["grade"]
        self.assertEqual((g["stato"], g["letti"], g["registro_dopo"]["present"]), ("ok", 0, 0))


class ErroriEOpzioni(Base):
    def test_one_source_failing_does_not_block_the_others(self):
        def down():
            raise ConnectionError("rete assente")
        res = self.run_dry(fetchers(notice=(down, None)))
        r = res["RISULTATO_REALE"]["letture"]
        self.assertEqual(r["notice"]["stato"], "errore")
        self.assertIn("nessun record marcato", r["notice"]["nota"])
        self.assertEqual((r["absence"]["stato"], r["absence"]["conteggi"]["create"]), ("ok", 3))
        self.assertFalse(res["SIMULAZIONE_NON_ACCADUTA"]["per_kind"]["notice"]["eseguita"])

    def test_second_read_is_off_by_default_and_reports_only_real_changes(self):
        self.assertIs(inspect.signature(D.run).parameters["second_read"].default, False)
        self.assertNotIn("secondo_giro", self.run_dry()["RISULTATO_REALE"])
        calls = {"n": 0}
        def flip():
            calls["n"] += 1
            r = notices(3)
            if calls["n"] > 1:
                r[1]["readStatus"] = True                                           # qualcuno ha letto la circolare
            return r
        res = self.run_dry(fetchers(notice=(flip, None)), second_read=True)
        g = res["RISULTATO_REALE"]["secondo_giro"]
        self.assertEqual(g["notice"]["conteggi"], {"state_change": 1, "unchanged": 2})
        self.assertTrue(g["notice"]["cambiamenti_reali"])
        self.assertFalse(g["absence"]["cambiamenti_reali"])

    def test_invalid_kind_and_kind_selection(self):
        with self.assertRaises(ValueError):
            self.run_dry(kinds=["voti"])
        res = self.run_dry(kinds=["notice"])
        self.assertEqual(list(res["RISULTATO_REALE"]["letture"]), ["notice"])

    def test_da_verificare_is_grouped_and_agenda_classification_is_flagged_inferred(self):
        res = self.run_dry()
        dv = res["RISULTATO_REALE"]["letture"]["notice"]["da_verificare"]
        self.assertEqual(dv["categoria non fornita da ClasseViva"]["n"], 3)
        self.assertEqual(len(dv["categoria non fornita da ClasseViva"]["esempi"]), 3)
        ev = {"evtId": 7, "evtCode": "AGNT", "subjectDesc": None, "evtDatetimeBegin": "2026-10-31T08:00:00+02:00",
              "evtDatetimeEnd": "2026-10-31T09:00:00+02:00", "isFullDay": False, "notes": "Verifica di storia",
              "authorName": "X", "classDesc": "3A"}
        res = self.run_dry(fetchers(agenda_event=(lambda: [ev], ("2026-09-29", "2027-08-31"))))
        a = res["RISULTATO_REALE"]["letture"]["agenda_event"]
        self.assertEqual(a["classificazione_dedotta"], {"verifica": 1})
        self.assertIn("DEDOTTI", a["nota"])


class EndToEndTool(unittest.TestCase):
    """Il tool MCP reale (server.py) contro il ClasseViva FINTO, con il registro dirottato su una cartella temporanea."""
    ENV = ("CLASSEVIVA_BASE", "CLASSEVIVA_USER", "CLASSEVIVA_PASS", "CLOUD_SCHOOL_DB")

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in self.ENV}
        _, base = TB.start_mock()
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.dbpath = os.path.join(self.tmp.name, "mai-creato", "registry.sqlite3")
        os.environ.update(CLASSEVIVA_BASE=base, CLASSEVIVA_USER="S1234567X", CLASSEVIVA_PASS="ok", CLOUD_SCHOOL_DB=self.dbpath)
        TB.STATE.clear(); TB.STATE.update(TB.fresh_state())
        TB.STATE["notices"] = [{"pubId": 5, "cntTitle": "Circolare n. 1", "pubDT": TB.d(-3) + "T08:00:00+02:00", "cntCategory": "", "readStatus": True},
                               {"pubId": 6, "cntTitle": "Circolare n. 2", "pubDT": TB.d(-2) + "T08:00:00+02:00", "cntCategory": "", "readStatus": False}]
        self.s = TB.srv()

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_tool_registered_with_safe_defaults(self):
        t = self.s.srv._tools["cv_registry_dryrun"]
        self.assertEqual(sorted(t["def"]["inputSchema"]["properties"]), ["kinds", "second_read", "simulate"])
        self.assertNotIn("path", json.dumps(t["def"]["inputSchema"]))               # nessun modo di scegliere dove scrivere
        d = inspect.signature(self.s.cv_registry_dryrun).parameters
        self.assertIs(d["second_read"].default, False)
        self.assertIs(d["simulate"].default, True)
        self.assertGreaterEqual(tuple(int(x) for x in self.s.srv.version.split(".")), (0, 3, 0))

    def test_live_dry_run_through_the_tool(self):
        out = self.s.srv.call("cv_registry_dryrun", {})
        json.dumps(out, ensure_ascii=False)                                         # serializzabile come risposta MCP
        r = out["RISULTATO_REALE"]["letture"]
        self.assertEqual(r["agenda_event"]["letti"], 6)
        self.assertEqual(r["agenda_event"]["conteggi"]["create"], 6)
        self.assertEqual(r["notice"]["letti"], 2)
        self.assertEqual((r["grade"]["stato"], r["grade"]["letti"]), ("ok", 0))
        self.assertEqual(r["absence"]["letti"], 1)
        self.assertIn("nuovoCampo", json.dumps(self.s.cv.Client("S1234567X", "ok").absences()))   # il campo non previsto esiste nella fonte...
        self.assertTrue(out["verifica_nessuna_scrittura"]["stato_prima_uguale_a_dopo"])
        self.assertFalse(os.path.exists(os.path.dirname(self.dbpath)))              # il registro reale NON e' stato creato
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_tool_with_options(self):
        out = self.s.srv.call("cv_registry_dryrun", {"kinds": ["notice", "grade"], "simulate": False, "second_read": True})
        self.assertEqual(sorted(out["RISULTATO_REALE"]["letture"]), ["grade", "notice"])
        self.assertEqual(out["SIMULAZIONE_NON_ACCADUTA"], {"eseguita": False})
        self.assertFalse(out["RISULTATO_REALE"]["secondo_giro"]["notice"]["cambiamenti_reali"])


if __name__ == "__main__":
    unittest.main()
