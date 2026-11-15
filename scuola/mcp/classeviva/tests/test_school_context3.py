"""M4 - school_context v3 (cloud.school.context/3) e school_overview: sezioni dati/interpretazione/freschezza, materia solo se M2
la determina, conflitti e da_verificare visibili, freschezza (stale/senza lettura/legacy), compatibilita' v2, sola lettura."""
import copy, datetime as dt, hashlib, json, os, sqlite3, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import cls_fixtures as F
import cls_registry as CL
import cvv_registry as CR
import registry as R
import school_agent as A
import school_context as SC
import school_context3 as C3
from test_m3_sync_runs import V1_RUNS
from test_school_context import CONF_MAT, NOW, SUBJECTS, ev, make_registry


def lesson(eid, subj_desc, who="ROSSI MARIO", day="2026-10-29", hpos=2, arg="Limiti di funzione", typ="Lezione"):
    r = {"evtId": eid, "evtDate": day, "evtHPos": hpos, "authorName": who, "subjectDesc": subj_desc,
         "lessonType": typ, "lessonArg": arg, "evtDuration": 1}
    return {k: v for k, v in r.items() if v is not None}


LESSONS = [
    lesson(1, "MATEMATICA", day="2026-10-29", hpos=2, arg="Limiti di funzione"),
    lesson(2, "MATEMATICA", day="2026-10-30", hpos=3, arg=""),                                    # senza argomento
    lesson(3, "FISICA", day="2026-10-30", hpos=4, arg="Moto circolare"),
    lesson(4, "MATEMATICA", day="2026-10-31", hpos=1, arg="La gravitazione universale"),          # conflitto con fisica
    lesson(5, None, day="2026-10-31", hpos=2, arg="Ripasso"),                                      # M2 non decide: da_verificare
    lesson(6, "STORIA", who="BIANCHI ANNA", day="2026-10-31", hpos=3, arg="Il Seicento"),
    lesson(7, "MATEMATICA", who="NERI PAOLO", day="2026-10-31", hpos=5, arg="Derivate"),          # docente fuori da /subjects
    lesson(8, "MATEMATICA", day="2026-11-01", hpos=1, arg="Derivate", typ="Supplenza"),           # avviso supplenza
]


def populate_lessons(reg):
    CR.sync_kind(reg, "lesson", lambda: [dict(r) for r in LESSONS])


def make_reg(lessons=True):
    reg = make_registry()
    if lessons:
        populate_lessons(reg)
    CR.sync_kind(reg, "notice", lambda: [{"pubId": 1, "pubDT": "2026-10-29T12:00:00+02:00", "cntTitle": "Circolare"}])
    return reg


def counts(reg):
    return {t: reg.db.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
            for t in ("records", "versions", "events", "runs", "projections")}


def dump(o):
    return json.dumps(o, ensure_ascii=False, sort_keys=True, default=str)


def disk_registry(root):
    """Registro su disco (run in modalita' 'commit'), popolato come make_reg."""
    path = os.path.join(root, "registry.sqlite3")
    reg = R.Registry(path)
    CR.sync_kind(reg, "agenda_event", lambda: [ev(1, "Verifica di matematica: limiti", day="2026-11-17"),
                                               ev(2, "Verifica di storia", author="BIANCHI ANNA", day="2026-11-18")])
    CR.sync_kind(reg, "grade", lambda: [])
    populate_lessons(reg)
    CL.sync_kind(reg, "course", lambda: [dict(F.COURSE_A), dict(F.COURSE_B)])
    CL.sync_kind(reg, "course_work", lambda: [dict(w) for w in F.WORK])
    CL.sync_kind(reg, "submission", lambda: [dict(s) for s in F.SUBMISSIONS])
    CL.sync_kind(reg, "announcement", lambda: [dict(a) for a in F.ANNOUNCEMENTS])
    CL.sync_kind(reg, "material", lambda: [dict(m) for m in F.MATERIALS])
    CL.sync_kind(reg, "topic", lambda: [dict(t) for t in F.TOPICS])
    reg.close()
    return path


def downgrade_to_v1(path):
    c = sqlite3.connect(path, isolation_level=None)
    rows = c.execute("SELECT run_id,source,kind,started_at,status,items_seen,counts_json,warnings_json,error FROM runs").fetchall()
    c.execute("DROP TRIGGER IF EXISTS runs_no_update"); c.execute("DROP TRIGGER IF EXISTS runs_no_delete")
    c.execute("DROP TABLE runs"); c.execute(V1_RUNS)
    c.executemany("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)", rows)
    c.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    c.execute("DELETE FROM meta WHERE key='migrated_v1_to_v2_at'")
    c.close()


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.reg = make_reg()

    def v3(self, subject="matematica", manual=CONF_MAT, now=NOW, **kw):
        return C3.build(self.reg, subject, SUBJECTS, now, manual=manual, **kw)


class Shape(Base):
    def test_schema_levels_and_sections(self):
        c = self.v3()
        self.assertEqual(c["schema"], "cloud.school.context/3")
        self.assertEqual(set(c["sezioni"]), {"lezioni", "agenda", "voti", "classroom"})
        self.assertEqual(set(c["livelli"]), {"dati", "interpretazione", "freschezza"})
        for name in ("lezioni", "agenda", "voti", "classroom"):
            self.assertIn("freschezza", c["sezioni"][name])
        self.assertEqual(c["identita"]["materia"], "matematica")
        self.assertIn("ROSSI MARIO", c["identita"]["docenti"]["registro_subjects"])
        self.assertEqual(c["identita"]["classe"]["anno_scolastico"], "2026-27")
        self.assertEqual(c["identita"]["classe"]["classi_osservate_in_agenda"], ["4X"])

    def test_records_separate_data_interpretation_and_citation(self):
        c = self.v3()
        for r in c["sezioni"]["lezioni"]["record"] + c["sezioni"]["agenda"]["verifiche_future"]:
            self.assertTrue({"uid", "dati", "interpretazione", "fonte"} <= set(r), r)
            self.assertTrue({"uid", "registry_version", "content_hash", "source_status"} <= set(r["fonte"]))
            self.assertNotIn("stato", r["dati"])                     # nessuna interpretazione dentro i dati canonici
            self.assertIn("stato", r["interpretazione"])

    def test_deterministic_same_now(self):
        self.assertEqual(dump(self.v3()), dump(self.v3()))


class SubjectDetermined(Base):
    def test_determined_lessons_only_of_subject(self):
        c = self.v3()
        uids = [r["uid"] for r in c["sezioni"]["lezioni"]["record"]]
        self.assertEqual(set(uids), {"cvv:lesson:1", "cvv:lesson:2", "cvv:lesson:4", "cvv:lesson:8"})
        for r in c["sezioni"]["lezioni"]["record"]:
            self.assertEqual((r["interpretazione"]["stato"], r["interpretazione"]["materia_derivata"]), ("determinata", "MATEMATICA"))
        self.assertTrue(all("fisica" not in u for u in uids))

    def test_other_subjects_see_their_own(self):
        st = self.v3("storia", manual={})
        self.assertEqual([r["uid"] for r in st["sezioni"]["lezioni"]["record"]], ["cvv:lesson:6"])
        fi = self.v3("fisica", manual={})
        self.assertEqual([r["uid"] for r in fi["sezioni"]["lezioni"]["record"]], ["cvv:lesson:3"])

    def test_lesson_data_is_exact_raw(self):
        c = self.v3()
        r = {x["uid"]: x for x in c["sezioni"]["lezioni"]["record"]}["cvv:lesson:1"]
        self.assertEqual(r["dati"], {"data": "2026-10-29", "ora": 2, "docente": "ROSSI MARIO", "materia_api": "MATEMATICA",
                                     "tipo": "Lezione", "argomento": "Limiti di funzione"})
        rec = self.reg.get_record("classeviva", "lesson", "lesson:1") or self.reg.get_record("classeviva", "lesson", "cvv:lesson:1")
        self.assertIsNotNone(rec)
        self.assertEqual(r["fonte"]["content_hash"], rec["content_hash"])
        self.assertEqual(r["fonte"]["registry_version"], rec["version"])

    def test_agenda_events_via_m2_and_type_in_interpretation(self):
        a = self.v3()["sezioni"]["agenda"]
        fut = a["verifiche_future"]
        self.assertEqual([t["dati"]["data"] for t in fut], ["2026-11-17"])
        self.assertEqual(fut[0]["interpretazione"]["tipo_dedotto"], "verifica")
        self.assertEqual(fut[0]["interpretazione"]["stato"], "determinata")
        self.assertTrue(fut[0]["interpretazione"]["evidenze"])
        self.assertEqual(len(a["compiti"]), 1)
        self.assertNotIn("tipo_dedotto", fut[0]["dati"])


class ToVerify(Base):
    def test_proposed_courses_stay_proposed(self):
        c = self.v3("matematica", manual={})
        self.assertEqual(c["sezioni"]["classroom"]["confermato"]["corsi"], [])
        self.assertEqual(c["sezioni"]["classroom"]["proposto"]["stato"], "PROPOSTA NON CONFERMATA")

    def test_uncertain_lesson_not_assigned_but_visible(self):
        c = self.v3()
        lez = c["sezioni"]["lezioni"]
        self.assertNotIn("cvv:lesson:5", [r["uid"] for r in lez["record"]])
        self.assertIn("cvv:lesson:5", [r["uid"] for r in lez["possibili_da_verificare"]])
        p = [r for r in lez["possibili_da_verificare"] if r["uid"] == "cvv:lesson:5"][0]
        self.assertEqual(p["interpretazione"]["stato"], "da_verificare")
        self.assertIsNone(p["interpretazione"]["materia_derivata"])
        self.assertIn("MATEMATICA", p["interpretazione"]["candidati"])
        self.assertTrue(p["interpretazione"]["evidenze"])
        w = [x for x in c["warning"] if x["codice"] == "attribuzione_da_verificare" and x["sezione"] == "lezioni"][0]
        self.assertIn("cvv:lesson:5", w["uid"])

    def test_unknown_teacher_lesson_is_to_verify_even_if_subjectdesc_matches(self):
        lez = self.v3()["sezioni"]["lezioni"]
        self.assertNotIn("cvv:lesson:7", [r["uid"] for r in lez["record"]])
        self.assertIn("cvv:lesson:7", [r["uid"] for r in lez["possibili_da_verificare"]])

    def test_ambiguous_event_not_in_subject(self):
        a = self.v3()["sezioni"]["agenda"]
        allu = [r["uid"] for k in ("verifiche_future", "verifiche_passate", "compiti", "eventi") for r in a[k]]
        self.assertNotIn("cvv:5", allu)
        self.assertIn("cvv:5", [r["uid"] for r in a["possibili_da_verificare"]])

    def test_to_verify_stays_visible_in_every_candidate_subject_only(self):
        self.assertIn("cvv:lesson:5", [r["uid"] for r in self.v3("fisica", manual={})["sezioni"]["lezioni"]["possibili_da_verificare"]])
        self.assertEqual(self.v3("storia", manual={})["sezioni"]["lezioni"]["possibili_da_verificare"], [])

    def test_text_similarity_never_assigns_without_m2(self):
        """La lezione senza subjectDesc cita 'Ripasso': nessun testo la fa entrare in una materia."""
        for k in ("matematica", "fisica", "storia", "italiano"):
            c = self.v3(k, manual={})
            self.assertNotIn("cvv:lesson:5", [r["uid"] for r in c["sezioni"]["lezioni"]["record"]])


class Conflicts(Base):
    def test_conflict_visible_and_subject_unchanged(self):
        c = self.v3()
        r = {x["uid"]: x for x in c["sezioni"]["lezioni"]["record"]}["cvv:lesson:4"]
        self.assertEqual(r["interpretazione"]["materia_derivata"], "MATEMATICA")
        self.assertEqual(r["interpretazione"]["stato"], "determinata")
        self.assertEqual(r["interpretazione"]["conflitti"][0]["tipo"], "subjectDesc_vs_testo")
        self.assertTrue(r["interpretazione"]["evidenze"])
        w = [x for x in c["warning"] if x["codice"] == "conflitto_attribuzione" and x["sezione"] == "lezioni"]
        self.assertEqual(w[0]["uid"], ["cvv:lesson:4"])
        self.assertIn("NON modificata", w[0]["messaggio"])

    def test_substitution_notice_is_a_warning(self):
        c = self.v3()
        w = [x for x in c["warning"] if x["codice"] == "avviso_attribuzione"][0]
        self.assertIn("cvv:lesson:8", w["uid"])


class Teacher(Base):
    def test_filter_by_teacher(self):
        c = self.v3(docente="rossi")
        self.assertTrue(c["sezioni"]["lezioni"]["record"])
        self.assertTrue(all(r["dati"]["docente"] == "ROSSI MARIO" for r in c["sezioni"]["lezioni"]["record"]))
        self.assertEqual(c["identita"]["docenti"]["filtro_docente"], "rossi")

    def test_filter_by_other_teacher_is_empty_and_declared(self):
        c = self.v3(docente="BIANCHI")
        self.assertEqual(c["sezioni"]["lezioni"]["record"], [])
        self.assertTrue(any(m["sezione"] == "lezioni" and "docente indicato" in m["messaggio"] for m in c["informazioni_mancanti"]))

    def test_invalid_teacher_rejected(self):
        with self.assertRaises(ValueError):
            self.v3(docente="  ")


class Missing(unittest.TestCase):
    def test_empty_registry_declares_what_is_missing(self):
        reg = R.Registry(":memory:")
        c = C3.build(reg, "matematica", SUBJECTS, NOW, manual={})
        msgs = " | ".join("%s:%s" % (m["sezione"], m["messaggio"]) for m in c["informazioni_mancanti"])
        for s in ("lezioni:", "agenda:", "voti:", "classroom:", "livello_manuale:", "materiali_e_appunti:"):
            self.assertIn(s, msgs)
        self.assertEqual(c["sezioni"]["lezioni"]["record"], [])
        self.assertEqual(c["sezioni"]["voti"]["record"], [])
        self.assertEqual(counts(reg)["records"], 0)

    def test_lessons_kind_absent_is_declared_not_empty_valid(self):
        reg = make_reg(lessons=False)
        c = C3.build(reg, "matematica", SUBJECTS, NOW, manual=CONF_MAT)
        self.assertTrue(any("kind 'lesson'" in m["messaggio"] for m in c["informazioni_mancanti"]))
        self.assertEqual(c["sezioni"]["lezioni"]["freschezza"]["stato"], "senza_lettura")

    def test_subject_without_confirmed_course(self):
        c = C3.build(make_reg(), "storia", SUBJECTS, NOW, manual=CONF_MAT)
        self.assertTrue(any(m["sezione"] == "classroom" and "CONFERMATO" in m["messaggio"] for m in c["informazioni_mancanti"]))

    def test_unknown_and_excluded_subjects_rejected(self):
        for s in ("arte", "astrologia"):
            with self.assertRaises(ValueError):
                C3.build(make_reg(), s, SUBJECTS, NOW, manual={})

    def test_notices_are_declared_unattributable(self):
        c = C3.build(make_reg(), "matematica", SUBJECTS, NOW, manual={})
        self.assertTrue(any(w["codice"] == "comunicazioni_non_attribuibili" for w in c["warning"]))


class Freshness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "school"); os.makedirs(self.root); os.chmod(self.root, 0o700)
        self.path = disk_registry(self.root)
        self.now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    def ctx(self, now=None, **kw):
        reg = R.Registry.from_snapshot(self.path)
        try:
            return C3.build(reg, "matematica", SUBJECTS, now or self.now, manual=CONF_MAT, **kw)
        finally:
            reg.close()

    def test_recent_commit_runs_are_updated(self):
        c = self.ctx(self.now + dt.timedelta(hours=1))
        self.assertEqual(c["freschezza"]["per_sezione"], {"lezioni": "aggiornato", "agenda": "aggiornato", "voti": "aggiornato", "classroom": "aggiornato"})
        self.assertFalse(c["freschezza"]["qualcosa_stale"])
        self.assertFalse(c["sezioni"]["lezioni"]["freschezza"]["legacy"])
        self.assertIn("NON garantisce", c["freschezza"]["nota"])

    def test_old_reads_are_stale_and_reported(self):
        c = self.ctx(self.now + dt.timedelta(hours=100))
        self.assertEqual(set(c["freschezza"]["per_sezione"].values()), {"stale"})
        self.assertTrue(c["freschezza"]["qualcosa_stale"])
        self.assertTrue(any(w["codice"] == "sezione_stale" and w["sezione"] == "lezioni" for w in c["warning"]))
        self.assertGreater(c["sezioni"]["lezioni"]["freschezza"]["eta_ore_max"], 48)

    def test_threshold_is_configurable(self):
        self.assertEqual(self.ctx(self.now + dt.timedelta(hours=5), soglia_ore=2)["freschezza"]["per_sezione"]["lezioni"], "stale")

    def test_missing_read_for_a_section(self):
        # registro con record ma senza alcun run di commit: registro in memoria (run 'dry_run')
        mem = make_reg()
        c = C3.build(mem, "matematica", SUBJECTS, NOW, manual=CONF_MAT)
        self.assertEqual(set(c["freschezza"]["per_sezione"].values()), {"senza_lettura"})
        self.assertTrue(any(w["codice"] == "sezione_senza_lettura" for w in c["warning"]))

    def test_legacy_runs_flagged_but_not_an_error(self):
        downgrade_to_v1(self.path)
        c = self.ctx(self.now + dt.timedelta(hours=1))
        self.assertTrue(c["provenienza"]["registro_migrato_in_memoria"])
        self.assertEqual(c["provenienza"]["schema_registro"], 2)
        for n in ("lezioni", "agenda", "voti", "classroom"):
            self.assertTrue(c["sezioni"][n]["freschezza"]["legacy"])
        self.assertEqual(c["freschezza"]["per_sezione"]["lezioni"], "aggiornato")
        w = [x for x in c["warning"] if x["codice"] == "dati_legacy"]
        self.assertTrue(w and all(x["gravita"] == "info" for x in w))
        self.assertEqual(R.stored_version(self.path), 1)                      # il file resta v1


class V2Compat(Base):
    def test_v2_unchanged_by_v3(self):
        before = dump(SC.build(self.reg, "matematica", SUBJECTS, NOW, manual=CONF_MAT))
        self.v3()
        C3.overview(self.reg, SUBJECTS, NOW, manual=CONF_MAT)
        after = dump(SC.build(self.reg, "matematica", SUBJECTS, NOW, manual=CONF_MAT))
        self.assertEqual(before, after)
        self.assertEqual(json.loads(after)["schema"], "cloud.school.context/2")
        self.assertEqual(SC.SCHEMA, "cloud.school.context/2")

    def test_v3_declares_compatibility_and_reuses_v2_classroom(self):
        c = self.v3()
        self.assertIn("context/2", c["compatibilita"]["v2"])
        v2 = SC.build(self.reg, "matematica", SUBJECTS, NOW, manual=CONF_MAT)
        self.assertEqual(dump(c["sezioni"]["classroom"]["confermato"]), dump(v2["classroom"]["confermato"]))
        self.assertEqual(dump(c["sezioni"]["classroom"]["proposto"]), dump(v2["classroom_proposto"]))


class ReadOnly(Base):
    def test_in_memory_registry_untouched_and_no_duplication(self):
        n0 = counts(self.reg)
        c = self.v3(include_raw=True)
        C3.overview(self.reg, SUBJECTS, NOW, manual=CONF_MAT)
        self.assertEqual(counts(self.reg), n0)
        self.assertEqual(self.reg.verify_integrity(), [])
        for name in ("lezioni",):
            uids = [r["uid"] for r in c["sezioni"][name]["record"]] + [r["uid"] for r in c["sezioni"][name]["possibili_da_verificare"]]
            self.assertEqual(len(uids), len(set(uids)))                    # nessun record duplicato nel contesto
        for r in c["sezioni"]["lezioni"]["record"]:                        # il raw esposto e' IDENTICO al canonico
            rec = self.reg.get_record("classeviva", "lesson", r["uid"].replace("cvv:", ""))
            self.assertEqual(r["raw"], rec["raw"])

    def test_disk_registry_files_unchanged(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "school"); os.makedirs(root); os.chmod(root, 0o700)
        path = disk_registry(root)
        files0, h0 = sorted(os.listdir(root)), sha(path)
        C3.build_from_path(path, "matematica", SUBJECTS, NOW, manual_path=os.path.join(root, "manual", "none.json"))
        C3.overview_from_path(path, SUBJECTS, NOW, manual_path=os.path.join(root, "manual", "none.json"))
        self.assertEqual(files0, sorted(os.listdir(root)))
        self.assertEqual(h0, sha(path))
        self.assertFalse(os.path.exists(os.path.join(root, "manual")))     # il livello manuale non viene mai creato

    def test_inputs_not_mutated(self):
        man, subs = copy.deepcopy(CONF_MAT), copy.deepcopy(SUBJECTS)
        C3.build(self.reg, "matematica", subs, NOW, manual=man)
        C3.overview(self.reg, subs, NOW, manual=man)
        self.assertEqual(man, CONF_MAT); self.assertEqual(subs, SUBJECTS)


class Overview(Base):
    def ov(self, **kw):
        return C3.overview(self.reg, SUBJECTS, NOW, manual=kw.pop("manual", CONF_MAT), **kw)

    def test_shape_and_nature(self):
        o = self.ov()
        self.assertEqual(o["schema"], "cloud.school.overview/1")
        self.assertIn("NON sostituisce il registro", o["natura"])
        self.assertEqual([m["materia"] for m in o["materie"]], list(SC.AGENT_SUBJECTS))
        self.assertEqual(o["registro"]["schema"], 2)
        self.assertEqual(o["registro"]["record_per_kind"]["classeviva/lesson"]["present"], len(LESSONS))

    def test_subject_numbers(self):
        m = {x["materia"]: x for x in self.ov()["materie"]}
        self.assertEqual(m["matematica"]["lezioni"], 4)
        self.assertEqual(m["fisica"]["lezioni"], 1)
        self.assertEqual(m["storia"]["lezioni"], 1)
        self.assertEqual(m["matematica"]["verifiche_future"], 1)
        self.assertEqual(m["matematica"]["prossima_verifica"]["data"], "2026-11-17")
        self.assertGreaterEqual(m["matematica"]["da_verificare"], 2)       # lezioni 5 e 7 + evento 5
        self.assertGreaterEqual(m["matematica"]["conflitti"], 1)
        self.assertEqual(m["matematica"]["classroom"]["corsi_confermati"], 1)
        self.assertEqual(m["storia"]["classroom"]["corsi_confermati"], 0)

    def test_attribution_totals(self):
        a = self.ov()["attribuzione"]
        self.assertEqual(a["lezioni"]["totale"], len(LESSONS))
        self.assertEqual(a["lezioni"]["da_verificare"], 2)
        self.assertEqual(a["lezioni"]["con_conflitti"], 1)
        self.assertEqual(a["lezioni"]["determinate"] + a["lezioni"]["da_verificare"], a["lezioni"]["totale"])

    def test_warnings_and_missing(self):
        o = self.ov(manual={})
        codes = {w["codice"] for w in o["warning"]}
        self.assertTrue({"attribuzione_da_verificare", "conflitto_attribuzione", "kind_stale_o_senza_lettura",
                         "comunicazioni_non_attribuibili"} <= codes)
        self.assertTrue(any(m["sezione"] == "livello_manuale" for m in o["informazioni_mancanti"]))

    def test_empty_registry(self):
        o = C3.overview(R.Registry(":memory:"), SUBJECTS, NOW, manual={})
        self.assertEqual(len(o["materie"]), 8)
        self.assertEqual(o["registro"]["record_per_kind"], {})
        self.assertTrue(any(m["sezione"] == "lezioni" for m in o["informazioni_mancanti"]))

    def test_freshness_per_kind_on_disk(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "school"); os.makedirs(root); os.chmod(root, 0o700)
        path = disk_registry(root)
        now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        o = C3.overview_from_path(path, SUBJECTS, now, manual_path=os.path.join(root, "m.json"))
        self.assertFalse(o["freschezza"]["qualcosa_stale"])
        self.assertEqual(o["freschezza"]["per_kind"]["classeviva/lesson"]["motivo"], "ok")
        o2 = C3.overview_from_path(path, SUBJECTS, now + dt.timedelta(hours=100), manual_path=os.path.join(root, "m.json"))
        self.assertTrue(o2["freschezza"]["qualcosa_stale"])
        self.assertTrue(any(w["codice"] == "kind_stale_o_senza_lettura" for w in o2["warning"]))


class Brief(unittest.TestCase):
    def test_brief_has_lessons_and_freshness_and_keeps_old_keys(self):
        reg = make_reg()
        b = A.brief(reg, "matematica", SUBJECTS, NOW, manual=CONF_MAT)
        self.assertEqual(b["schema"], "cloud.school.brief/2")
        for old in ("materia", "corsi_confermati", "compiti", "annunci", "materiali", "classeviva", "da_verificare", "regole"):
            self.assertIn(old, b)
        self.assertEqual(b["lezioni"]["totale_determinate"], 4)
        self.assertEqual(b["lezioni"]["da_verificare_non_incluse"], 2)
        self.assertEqual(b["lezioni"]["con_conflitti"], 1)
        self.assertTrue(all("@v" in r["ref"] for r in b["lezioni"]["recenti"]))
        self.assertEqual(set(b["freschezza"]["per_sezione"]), {"lezioni", "agenda", "voti", "classroom"})
        txt = dump(b)
        for bad in ("content_hash", '"raw"', '"derivato"'):
            self.assertNotIn(bad, txt)

    def test_brief_other_subject_no_foreign_lessons(self):
        b = A.brief(make_reg(), "storia", SUBJECTS, NOW, manual={})
        self.assertEqual([r["ref"].split("@")[0] for r in b["lezioni"]["recenti"]], ["cvv:lesson:6"])


class ServerTools(unittest.TestCase):
    def setUp(self):
        import server
        from unittest import mock
        self.server = server
        self.d = tempfile.mkdtemp()
        self.db = os.path.join(self.d, "registry.sqlite3")
        for p in (mock.patch.dict(os.environ, {"CLOUD_SCHOOL_DB": self.db}), mock.patch.object(server, "subjects", lambda: SUBJECTS),
                  mock.patch.object(SC, "manual_default_path", lambda: os.path.join(self.d, "manual", "school_manual.json"))):
            p.start(); self.addCleanup(p.stop)

    def test_missing_registry_clear_error_nothing_created(self):
        for fn, args in ((self.server.school_context_v3, ("matematica",)), (self.server.school_overview, ())):
            with self.assertRaises(ValueError) as cm:
                fn(*args)
            self.assertIn("registro locale assente", str(cm.exception))
        self.assertFalse(os.path.exists(self.db))

    def test_tool_schemas(self):
        t = self.server.srv._tools
        d = t["school_context_v3"]["def"]
        self.assertEqual(d["inputSchema"]["required"], ["subject"])
        self.assertEqual(set(d["inputSchema"]["properties"]), {"subject", "include_raw", "docente"})
        self.assertIn("SOLA LETTURA", d["description"])
        self.assertEqual(set(t["school_overview"]["def"]["inputSchema"]["properties"]), {"soglia_ore"})
        self.assertEqual(set(t["school_context"]["def"]["inputSchema"]["properties"]), {"subject", "include_raw"})   # v2 invariato

    def test_tools_read_registry_without_writing(self):
        path = disk_registry(self.d)
        os.chmod(self.d, 0o700)
        h0, files0 = sha(path), sorted(os.listdir(self.d))
        v3 = self.server.school_context_v3("matematica", docente="rossi")
        ov = self.server.school_overview()
        v2 = self.server.school_context("matematica")
        self.assertEqual(v3["schema"], "cloud.school.context/3")
        self.assertEqual(ov["schema"], "cloud.school.overview/1")
        self.assertEqual(v2["schema"], "cloud.school.context/2")
        self.assertFalse(ov["verifica_nessuna_scrittura"]["file_modificati"])
        self.assertFalse(any(ov["scritture"].values()))
        self.assertEqual((h0, files0), (sha(path), sorted(os.listdir(self.d))))
        with self.assertRaises(ValueError):
            self.server.school_context_v3("religione")


class Truncation(Base):
    def test_truncated_classroom_is_declared_in_warning(self):
        from unittest import mock
        full = self.v3()["sezioni"]["classroom"]["confermato"]
        self.assertGreater(full["annunci_conteggio"]["totale"], 1)
        self.assertFalse([w for w in self.v3()["warning"] if w["codice"] == "contenuto_parziale"])      # nulla di troncato: nessun warning
        with mock.patch.dict(SC.LIMITS, {"announcements": 1}):
            c = self.v3()
        cp = [w for w in c["warning"] if w["codice"] == "contenuto_parziale"]
        self.assertEqual(len(cp), 1)
        tot = full["annunci_conteggio"]["totale"]
        self.assertEqual((cp[0]["sezione"], cp[0]["mostrati"], cp[0]["totale"], cp[0]["omessi"]), ("classroom", 1, tot, tot - 1))
        self.assertIn("PARZIALE", cp[0]["messaggio"]); self.assertIn("annunci", cp[0]["messaggio"])
        self.assertEqual(c["sezioni"]["classroom"]["confermato"]["annunci_conteggio"]["troncato"], True)   # selezione invariata

    def test_truncated_lessons_declared_and_selection_unchanged(self):
        full, _, _ = C3.lessons_section(self.reg, "matematica", SUBJECTS, NOW)
        part, warn, _ = C3.lessons_section(self.reg, "matematica", SUBJECTS, NOW, limit=2)
        self.assertEqual([r["uid"] for r in part["record"]], [r["uid"] for r in full["record"]][:2])      # stessi record, piu' recenti prima
        w = [x for x in warn if x["codice"] == "contenuto_parziale"]
        self.assertEqual((w[0]["mostrati"], w[0]["totale"], w[0]["omessi"]), (2, len(full["record"]), len(full["record"]) - 2))

    def test_untruncated_has_no_partial_warning(self):
        _, warn, _ = C3.lessons_section(self.reg, "matematica", SUBJECTS, NOW)
        self.assertFalse([w for w in warn if w["codice"] == "contenuto_parziale"])

    def test_past_tests_truncation_declared(self):
        reg = R.Registry(":memory:")
        CR.sync_kind(reg, "agenda_event", lambda: [ev(1, "Verifica di matematica: a", day="2026-10-08"),
                                                   ev(2, "Verifica di matematica: b", day="2026-10-18")])
        sec, warn, _, _ = C3.agenda_section(reg, "matematica", SUBJECTS, NOW, past_limit=1)
        self.assertEqual([t["dati"]["data"] for t in sec["verifiche_passate"]], ["2026-10-18"])           # selezione invariata
        w = [x for x in warn if x["codice"] == "contenuto_parziale"]
        self.assertEqual((w[0]["sezione"], w[0]["mostrati"], w[0]["totale"], w[0]["omessi"]), ("agenda", 1, 2, 1))
        _, warn2, _, _ = C3.agenda_section(reg, "matematica", SUBJECTS, NOW, past_limit=5)
        self.assertFalse([x for x in warn2 if x["codice"] == "contenuto_parziale"])


class CompatText(Base):
    def test_v2_text_does_not_claim_a_version_parameter(self):
        t = self.v3()["compatibilita"]["v2"]
        self.assertNotIn("versione=2", t)
        self.assertIn("school_context", t); self.assertIn("context/2", t); self.assertIn("separato", t)


if __name__ == "__main__":
    unittest.main()
