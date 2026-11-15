"""M16 v1 - school_dashboard/1 (cloud.school.dashboard/1): modello di vista della dashboard. Pagine chiuse, blocchi con stato chiuso
(dati / vuoto_valido / non_letto / non_disponibile / non_attivo) e provenienza, valori IDENTICI ai contratti sorgente (nessuna logica
nuova), medie come politiche non verificate, null mai 0 nei grafici, grafici solo con punti reali, storico separato, novita' del
Registry senza la prima acquisizione, layer futuri non_attivo, nessun giudizio, determinismo, ordine invertito, nessuna scrittura,
input immutati, clock una volta, M1-M15 invariati."""
import copy, datetime as dt
import hashlib, json, os, re, sys, tempfile, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import context4_fixtures as F
import cvv_cls_links as L
import cvv_comms as M
import cvv_dryrun as dry
import registry as R
import school_action as AC
import school_context as SC
import school_context3 as C3
import school_context4 as C4
import school_dashboard as DB
import school_materials_catalog as MC
import school_priority as PR
import school_records as RC
import school_status as ST
import school_study_assistant as SA
import school_study_context as SX
import school_study_session as SS
from test_school_context4 import pretty, counts
from test_school_records import ABSENCES, GRADES, SUBJECTS, fake, make

NOW, MAN = F.NOW, F.MANUAL


def build(reg, pagina="home", **kw):
    kw.setdefault("manual", MAN)
    return DB.build(reg, kw.pop("now", NOW), pagina=pagina, subjects=kw.pop("subjects", SUBJECTS), **kw)


def all_pages(reg, **kw):
    out = {p: build(reg, p, **kw) for p in DB.PAGES if p != "materia"}
    out.update({"materia_" + k: build(reg, "materia", materia=k, **kw) for k in SC.AGENT_SUBJECTS})
    return out


def blk(d, bid):
    return next(b for b in d["blocchi"] if b["id"] == bid)


def chart(d, cid):
    return next((c for c in d["grafici"] if c["id"] == cid), None)


def _null_s1(d):
    d = copy.deepcopy(d)
    for s in d["segnali"]:
        if s["id"] == "S1":
            s["valore"], s["evidenze_dettaglio"] = None, []
    return d


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = make()
        cls.p = mock.patch.object(R.Registry, "freshness", fake())
        cls.p.start()
        cls.pages = all_pages(cls.reg)

    @classmethod
    def tearDownClass(cls):
        cls.p.stop()
        cls.reg.close()


# ------------------------------------------------------------------------------------------------ struttura
class Struttura(Base):
    def test_schema_keys_and_navigation(self):
        for name, d in self.pages.items():
            self.assertEqual(list(d), ["schema", "natura", "pagina", "materia", "riferimento_temporale", "navigazione", "blocchi", "grafici",
                                       "grafici_assenti", "stati_blocco", "provenance", "regole_agente"], name)
            self.assertEqual(d["schema"], "cloud.school.dashboard/1")
            self.assertEqual([n["pagina"] for n in d["navigazione"]], list(DB.PAGES))
        self.assertTrue(DB.dashboard_version().startswith("r1-"))
        self.assertEqual(set(DB.PAGES), {"home", "attenzione", "materie", "materia", "verifiche", "compiti", "voti", "assenze", "studio",
                                         "materiali", "storico", "qualita_dati"})

    def test_blocks_closed_states_and_provenance(self):
        for name, d in self.pages.items():
            ids = [b["id"] for b in d["blocchi"]]
            self.assertEqual(len(ids), len(set(ids)), name)
            for b in d["blocchi"]:
                self.assertIn(b["stato"], DB.BLOCK_STATES)
                self.assertEqual(list(b)[:6], ["id", "titolo", "stato", "affidabilita", "provenienza", "dati"])
                if b["stato"] in ("dati", "vuoto_valido"):
                    self.assertTrue(b["provenienza"].get("contratto"), (name, b["id"]))
                    self.assertIsNotNone(b["provenienza"].get("versione"), (name, b["id"]))
                if b["stato"] == "non_attivo":
                    self.assertIsNone(b["dati"])
                    self.assertTrue(b["nota"])
            for c in d["grafici"]:
                self.assertIn(c["tipo"], DB.CHART_TYPES)
                self.assertTrue(c["provenienza"]["contratto"])

    def test_invalid_inputs(self):
        for kw in ({"pagina": "notion"}, {"pagina": "materia"}, {"pagina": "materia", "materia": " "},
                   {"pagina": "materia", "materia": "astrologia"}, {"pagina": "voti", "materia": "matematica"}, {"soglia_ore": 0},
                   {"soglia_ore": True}):
            with self.assertRaises(ValueError):
                DB.build(self.reg, NOW, manual=MAN, subjects=SUBJECTS, **kw)
        with self.assertRaises(ValueError):
            build(self.reg, now="2026-11-01T12:00:00")

    def test_no_judgment_words(self):
        rx = re.compile(r"\b(%s)\b" % "|".join(re.escape(w) for w in DB.FORBIDDEN_WORDS), re.I)
        texts = [DB.NATURA] + DB.RULES_FOR_AGENTS + list(DB.INACTIVE.values())
        for d in self.pages.values():
            texts += [b["titolo"] for b in d["blocchi"]] + [b.get("nota") or "" for b in d["blocchi"]] + [c["titolo"] for c in d["grafici"]]
        for t in texts:
            self.assertIsNone(rx.search(t), t)

    def test_future_layers_inactive(self):
        st = self.pages["studio"]
        self.assertEqual(blk(st, "non_attivo_store_materiali")["stato"], "non_attivo")
        self.assertEqual(blk(self.pages["materiali"], "disponibili")["stato"], "non_attivo")
        m = self.pages["materia_matematica"]
        for k in ("pianificazione", "profilo", "dichiarazioni"):
            self.assertEqual(blk(m, "non_attivo_" + k)["stato"], "non_attivo")
        self.assertNotRegex(pretty(self.pages).lower(), r"\bacquisito\"")


# ------------------------------------------------------------------------------------------------ fedelta' ai contratti
class Fedelta(Base):
    def test_home_and_attention_equal_m9_m10(self):
        m9 = PR.build(self.reg, NOW, manual=MAN, subjects=SUBJECTS, dettaglio=True)
        m10 = AC.build(self.reg, NOW, manual=MAN, subjects=SUBJECTS)
        h = self.pages["home"]
        self.assertEqual(blk(h, "attenzione")["dati"]["materie_per_priorita"], m9["sintesi"])
        self.assertEqual(blk(h, "azioni_disponibili")["dati"]["totale"], m10["sintesi"]["azioni"])
        a = self.pages["attenzione"]
        for m in m9["materie"]:
            b = blk(a, "materia_" + m["materia"])
            self.assertEqual((b["dati"]["priorita"], b["affidabilita"]), (m["priorita"], m["affidabilita"]))
            self.assertEqual([v["oggetto"] for v in b["dati"]["voci"]], [v["oggetto"] for v in m["voci"]])
        imp = blk(h, "prossimi_impegni")
        self.assertEqual(imp["provenienza"]["uid"], sorted(x["oggetto"] for x in imp["dati"]))
        self.assertEqual([x["data"] for x in imp["dati"]], sorted(x["data"] for x in imp["dati"]))

    def test_subject_cards_equal_sources(self):
        cards = {c["materia"]: c for c in blk(self.pages["materie"], "materie")["dati"]}
        self.assertEqual(list(cards), list(SC.AGENT_SUBJECTS))
        m14 = RC.build(self.reg, NOW, manual=MAN, subjects=SUBJECTS)
        for k in SC.AGENT_SUBJECTS:
            d8 = ST.build(self.reg, k, NOW, manual=MAN, subjects=SUBJECTS, dettaglio=True)
            v = next(m for m in m14["voti"]["per_materia"] if m["materia"] == k)
            c = cards[k]
            self.assertEqual(c["lezioni_anno"], d8["fatti"]["lezioni"]["n"])
            self.assertEqual(c["materiali_identificati"], MC.build(self.reg, NOW, manual=MAN, materia=k, subjects=SUBJECTS)["sintesi"]["totale"])
            self.assertEqual((c["voti"]["stato"], c["voti"]["n"]), (v["stato"], v["n_totali"]))
            self.assertEqual(c["voti"]["corrispondenza_classeviva"], "non_verificata")
            self.assertEqual(blk(self.pages["materia_" + k], "scheda")["dati"], c)

    def test_verifiche_and_subject_page_match_m8(self):
        rows = blk(self.pages["verifiche"], "verifiche_future")["dati"]
        for k in SC.AGENT_SUBJECTS:
            s1 = next(s for s in ST.build(self.reg, k, NOW, manual=MAN, subjects=SUBJECTS, dettaglio=True)["segnali"] if s["id"] == "S1")
            if (s1["valore"] or {}).get("stato") == "presente":
                exp = sorted(e["uid"] for e in s1["evidenze_dettaglio"])
                self.assertEqual(sorted(r["uid"] for r in rows if r["materia"] == k), exp)
                self.assertEqual(sorted(x["uid"] for x in blk(self.pages["materia_" + k], "verifiche")["dati"]), exp)
        cal = chart(self.pages["verifiche"], "calendario_verifiche")
        if rows:
            self.assertEqual(sorted(p["uid"] for s in cal["serie"] for p in s["punti"]), sorted(r["uid"] for r in rows))


# ------------------------------------------------------------------------------------------------ voti, null, grafici
class VotiEGrafici(Base):
    def test_means_are_named_unverified_policies(self):
        v = self.pages["voti"]
        self.assertIn("non verificata", blk(v, "complessivo")["nota"])
        self.assertEqual(blk(v, "politiche")["dati"]["primaria"], RC.PRIMARY_POLICY)
        for c in v["grafici"]:
            if c["id"].startswith("media") or c["id"] == "andamento_media":
                self.assertIs(c["valore_verificato"], False)
                self.assertTrue(c["formula"])
        self.assertNotRegex(pretty(self.pages).lower(), r"la tua media")

    def test_null_never_zero_in_charts(self):
        bars = chart(self.pages["voti"], "media_per_materia")
        xs = {p["x"]: p["y"] for p in bars["serie"][0]["punti"]}
        self.assertNotIn("storia", xs)                                  # somma pesi 0 -> media null -> nessun punto (non 0)
        self.assertNotIn("latino", xs)                                  # nessun voto -> nessun punto
        self.assertIn("matematica", xs)
        per = {m["materia"]: m for m in blk(self.pages["voti"], "per_materia")["dati"]}
        self.assertIsNone(per["storia"]["medie"][RC.PRIMARY_POLICY])
        for d in self.pages.values():
            for c in d["grafici"]:
                for s in c["serie"]:
                    self.assertTrue(s["punti"])
                    for p in s["punti"]:
                        self.assertIsNotNone(p["y"])

    def test_series_points_carry_uid(self):
        line = chart(self.pages["voti"], "andamento_voti")
        uids = {g["uid"] for g in blk(self.pages["voti"], "per_materia")["provenienza"]["uid"] and
                [{"uid": u} for u in blk(self.pages["voti"], "per_materia")["provenienza"]["uid"]]}
        for s in line["serie"]:
            for p in s["punti"]:
                self.assertIn(p["uid"], uids)
        self.assertNotIn("cvv:8", pretty(self.pages["voti"]))           # voto 2025-26: solo nello storico

    def test_no_grades_no_charts_and_states(self):
        empty = make(grades=[])
        with mock.patch.object(R.Registry, "freshness", fake()):
            v = build(empty, "voti")
        self.assertEqual(v["grafici"], [])
        self.assertTrue(v["grafici_assenti"])
        self.assertEqual(blk(v, "complessivo")["stato"], "vuoto_valido")
        self.assertEqual(blk(v, "per_materia")["stato"], "vuoto_valido")
        with mock.patch.object(R.Registry, "freshness", fake(none=("classeviva/grade",))):
            u = build(self.reg, "voti")
        self.assertEqual(blk(u, "complessivo")["stato"], "non_letto")
        self.assertEqual(blk(u, "per_materia")["stato"], "non_letto")
        self.assertEqual(u["grafici"], [])
        empty.close()

    def test_absences_chart_and_states(self):
        a = self.pages["assenze"]
        self.assertEqual(blk(a, "conteggi")["stato"], "dati")
        c = chart(a, "assenze_per_mese")
        tot = sum(p["y"] for s in c["serie"] for p in s["punti"])
        self.assertEqual(tot, blk(a, "conteggi")["dati"]["totale"])
        self.assertEqual(blk(self.pages["materia_matematica"], "assenze")["stato"], "non_disponibile")
        with mock.patch.object(R.Registry, "freshness", fake(none=("classeviva/absence",))):
            u = build(self.reg, "assenze")
        self.assertEqual(blk(u, "conteggi")["stato"], "non_letto")
        self.assertEqual(u["grafici"], [])


# ------------------------------------------------------------------------------------------------ storico, qualita', novita'
class StoricoQualitaNovita(Base):
    def test_history_page_separate(self):
        s = self.pages["storico"]
        self.assertEqual(blk(s, "voti_assenze")["dati"], {"assenze_2025-26": 1, "voti_2025-26": 1})
        for name, d in self.pages.items():
            if name != "storico":
                self.assertNotIn("2025-10-01", pretty(d["blocchi"]), name)

    def test_quality_page(self):
        q = self.pages["qualita_dati"]
        self.assertEqual([p["materia"] for p in blk(q, "per_materia")["dati"]], list(SC.AGENT_SUBJECTS))
        self.assertIn("media_classeviva_non_verificata", blk(q, "voti_assenze")["dati"]["gap"])
        with mock.patch.object(R.Registry, "freshness", fake(none=("classeviva/agenda_event",))):
            d = build(self.reg, "qualita_dati")
            h = build(self.reg, "home")
        self.assertEqual(blk(d, "freschezza")["dati"]["per_kind"]["classeviva/agenda_event"]["motivo"], "mai_sincronizzato")
        self.assertIn("classeviva/agenda_event", blk(h, "qualita_sintesi")["dati"]["kind_stale_o_non_letti"])

    def test_unread_source_not_empty(self):
        with mock.patch.object(R.Registry, "freshness", fake(none=("classeviva/agenda_event",))):
            v = build(self.reg, "verifiche")
        b = blk(v, "verifiche_future")
        self.assertIn("non letta", b["nota"])
        self.assertIn("latino", b["nota"])
        self.assertEqual({r["affidabilita"] for r in b["dati"]}, {"non_determinabile"})      # affidabilita' propagata da M8
        self.assertNotIn("scienze", [r["materia"] for r in b["dati"]])                      # S1 null: nessuna riga, non una verifica
        orig = ST.build
        with mock.patch.object(DB.ST, "build", side_effect=lambda *a, **k: _null_s1(orig(*a, **k))):
            u = build(self.reg, "verifiche")
        self.assertEqual((blk(u, "verifiche_future")["stato"], blk(u, "indizi_non_datati")["stato"]), ("non_letto", "non_letto"))

    def test_news_excludes_first_acquisition(self):
        now = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))

        class Fake:
            def runs(self):
                return [{"run_id": "r1", "source": "classeviva", "kind": "grade", "mode": "commit"},
                        {"run_id": "r0", "source": "classeviva", "kind": "grade", "mode": "dry_run"},
                        {"run_id": "r2", "source": "classeviva", "kind": "grade", "mode": "commit"}]

            def events(self):
                ev = lambda r, u, e, at: {"run_id": r, "source": "classeviva", "kind": "grade", "source_uid": u, "event": e, "at": at}  # noqa: E731
                return [ev("r1", "1", "create", "2026-10-29T08:00:00+00:00"), ev("r2", "2", "create", "2026-10-31T08:00:00+00:00"),
                        ev("r2", "1", "update", "2026-10-31T09:00:00+00:00"), ev("r2", "3", "create", "2026-10-18T08:00:00+00:00"),
                        ev("r2", "4", "create", "2026-11-01T11:00:00+00:00")]      # 13:00 locali: futuro rispetto a now
        rows, initial, cnt = DB._news(Fake(), now)
        self.assertEqual(initial, 1)
        self.assertEqual([(r["uid_fonte"], r["evento"]) for r in rows], [("1", "update"), ("2", "create")])
        self.assertEqual(rows[0]["ora_locale"], "2026-10-31T10:00:00+01:00")
        self.assertEqual(cnt, {"classeviva/grade create": 1, "classeviva/grade update": 1})


# ------------------------------------------------------------------------------------------------ determinismo e sicurezza
class DeterminismoESicurezza(unittest.TestCase):
    def test_byte_identical_and_reversed(self):
        a, b = make(), make(reverse=True)
        with mock.patch.object(R.Registry, "freshness", fake()):
            x1, x2, y = all_pages(a), all_pages(a), all_pages(b)
        for k in x1:
            self.assertEqual(pretty(x1[k]), pretty(x2[k]), k)
            self.assertEqual(pretty(x1[k]["blocchi"]), pretty(y[k]["blocchi"]), k)
            self.assertEqual(pretty(x1[k]["grafici"]), pretty(y[k]["grafici"]), k)
        a.close(); b.close()

    def test_builder_never_reads_clock(self):
        src = open(DB.__file__, encoding="utf-8").read()
        for bad in ("datetime.now", "time.time", "date.today", "utcnow"):
            self.assertNotIn(bad, src)

    def test_inputs_not_mutated(self):
        reg = make()
        subs, man = copy.deepcopy(SUBJECTS), copy.deepcopy(MAN)
        with mock.patch.object(R.Registry, "freshness", fake()):
            all_pages(reg, manual=man, subjects=subs)
        self.assertEqual((subs, man), (SUBJECTS, MAN))
        reg.close()

    def test_no_writes_and_m1_m15_unchanged(self):
        reg = make()
        with mock.patch.object(R.Registry, "freshness", fake()):
            snap = lambda: [pretty(C3.build(reg, "matematica", F.SUBJECTS, NOW, manual=MAN)), pretty(C3.overview(reg, F.SUBJECTS, NOW, manual=MAN)),  # noqa: E731
                            pretty(L.build(reg, F.SUBJECTS, NOW, manual=MAN)), pretty(M.build(reg, F.SUBJECTS, NOW, manual=MAN)),
                            pretty(C4.build(reg, "inglese", NOW, manual=MAN, subjects=SUBJECTS, dettaglio=True)),
                            pretty(ST.build(reg, "matematica", NOW, manual=MAN, subjects=SUBJECTS, dettaglio=True)),
                            pretty(PR.build(reg, NOW, manual=MAN, subjects=SUBJECTS, dettaglio=True)),
                            pretty(AC.build(reg, NOW, manual=MAN, subjects=SUBJECTS, dettaglio=True)),
                            pretty(SX.build(reg, "matematica", NOW, manual=MAN, subjects=SUBJECTS, dettaglio=True)),
                            pretty(SA.build(reg, "matematica", NOW, manual=MAN, subjects=SUBJECTS, dettaglio=True)),
                            pretty(SS.build(reg, "matematica", "spiega", "testo", NOW, manual=MAN, subjects=SUBJECTS)),
                            pretty(RC.build(reg, NOW, manual=MAN, subjects=SUBJECTS, dettaglio=True)),
                            pretty(MC.build(reg, NOW, manual=MAN, subjects=SUBJECTS, dettaglio=True))]
            before, c0 = snap(), counts(reg)
            all_pages(reg)
            self.assertEqual((before, c0), (snap(), counts(reg)))
        reg.close()

    def test_build_from_path_writes_nothing(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "registry.sqlite3")
        make(path=p).close()
        h = lambda q: hashlib.sha256(open(q, "rb").read()).hexdigest()                 # noqa: E731
        s0, f0, h0 = dry._fs_state(p), sorted(os.listdir(d)), h(p)
        for pg in ("home", "voti", "materiali"):
            out = DB.build_from_path(p, NOW, subjects=SUBJECTS, pagina=pg)
            self.assertEqual(out["schema"], DB.SCHEMA)
        self.assertEqual((s0, f0, h0), (dry._fs_state(p), sorted(os.listdir(d)), h(p)))


class ServerTool(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.d = tempfile.mkdtemp()
        os.chmod(self.d, 0o700)
        self.db = os.path.join(self.d, "registry.sqlite3")
        self.manual = os.path.join(self.d, "manual", "school_manual.json")
        for p in (mock.patch.dict(os.environ, {"CLOUD_SCHOOL_DB": self.db}), mock.patch.object(server, "subjects", lambda: SUBJECTS),
                  mock.patch.object(SC, "manual_default_path", lambda: self.manual)):
            p.start(); self.addCleanup(p.stop)

    def test_tool_registered(self):
        t = self.server.srv._tools["school_dashboard_v1"]["def"]
        self.assertEqual(set(t["inputSchema"]["properties"]), {"pagina", "materia", "soglia_ore"})
        self.assertFalse(t["inputSchema"].get("required"))
        self.assertIn("SOLA LETTURA", t["description"])

    def test_missing_registry(self):
        with self.assertRaises(ValueError):
            self.server.school_dashboard_v1()
        self.assertFalse(os.path.exists(self.db))

    def test_clock_once_no_writes(self):
        make(path=self.db).close()
        calls = []
        fixed = dt.datetime(2026, 11, 1, 12, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))

        class FakeDT:
            class datetime:
                @staticmethod
                def now(tz=None):
                    calls.append(tz)
                    return fixed
        h0 = hashlib.sha256(open(self.db, "rb").read()).hexdigest()
        with mock.patch.object(self.server, "dt", FakeDT):
            out = self.server.school_dashboard_v1(pagina="materia", materia="matematica")
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["riferimento_temporale"]["now"], "2026-11-01T12:00:00+02:00")
        self.assertFalse(out["verifica_nessuna_scrittura"]["file_modificati"])
        self.assertNotIn("notion", out["scritture"])
        self.assertEqual(h0, hashlib.sha256(open(self.db, "rb").read()).hexdigest())


if __name__ == "__main__":
    unittest.main()
