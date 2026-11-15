"""M7 - school_context/4 (cloud.school.context/4): contesto operativo di UNA materia, aggregazione in sola lettura di M2/M4/M5/M6.
Schema, risoluzione materia, sezioni, finestre, limiti, compatto/dettaglio, prossimi impegni, contatori, freschezza, gap, ambiguita',
hash, unione delle verifiche SOLO con stati M5 reali, determinismo byte-per-byte, nessuna scrittura, dimensione, tool MCP."""
import datetime as dt
import hashlib, json, os, sys, tempfile, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import context4_fixtures as F
import cvv_cls_links as L
import cvv_comms as M
import cvv_dryrun as dry
import registry as R
import school_context as SC
import school_context3 as C3
import school_context4 as C4
import cvv_subject as subj
from links_fixtures import ann, lesson

SUBJ, NOW, MAN = F.SUBJECTS, F.NOW, F.MANUAL
A = lambda c, a: "cls:%s/%s" % (c, a)            # noqa: E731


def pretty(o):
    return json.dumps(o, indent=2, ensure_ascii=False)


def counts(reg):
    return {t: reg.db.execute("SELECT count(*) FROM %s" % t).fetchone()[0] for t in ("records", "versions", "events", "runs", "projections")}


def fresh_fake(stale_kinds=(), none_kinds=()):
    """Freschezza finta: tutti i kind 'ok' tranne quelli indicati (stale / mai sincronizzati)."""
    def freshness(self, now, stale_after_hours=None):
        pk = {}
        for s, kinds in (("classeviva", ("agenda_event", "lesson", "notice", "grade")),
                         ("classroom", ("course", "course_work", "submission", "announcement", "material", "topic"))):
            for k in kinds:
                key = "%s/%s" % (s, k)
                if key in none_kinds:
                    pk[key] = {"ultimo_run": None, "stale": True, "motivo": "mai_sincronizzato"}
                else:
                    st = key in stale_kinds
                    pk[key] = {"ultimo_run": {"legacy": False}, "stale": st, "motivo": "oltre_soglia" if st else "ok",
                               "eta_ore": 99 if st else 1, "ultima_lettura_riuscita": "2026-11-01T10:00:00+02:00"}
        return {"calcolato_a": now, "soglia_ore": stale_after_hours or 48, "per_kind": pk, "qualcosa_stale": bool(stale_kinds or none_kinds)}
    return freshness


def build(reg, materia, **kw):
    kw.setdefault("manual", MAN)
    return C4.build(reg, materia, kw.pop("now", NOW), subjects=kw.pop("subjects", SUBJ), **kw)


def els(d, sec):
    return d["sezioni"][sec]["elementi"]


def uids(d, sec):
    return [e["uid"] for e in els(d, sec)]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = F.make_registry()
        cls.p = mock.patch.object(R.Registry, "freshness", fresh_fake())
        cls.p.start()
        cls.docs = {k: build(cls.reg, k) for k in SC.AGENT_SUBJECTS}

    @classmethod
    def tearDownClass(cls):
        cls.p.stop()
        cls.reg.close()


# ------------------------------------------------------------------------------------------------ schema e materia
class SchemaEMateria(Base):
    def test_schema_constants_and_api(self):
        self.assertEqual(C4.SCHEMA, "cloud.school.context/4")
        self.assertEqual(C4.SECTIONS, ("lezioni", "verifiche", "compiti", "comunicazioni", "collegamenti", "possibili_da_verificare", "generali"))
        self.assertEqual(C4.LIMITS_DEFAULT, {"prossimi_impegni": 10, "lezioni": 8, "verifiche": 8, "compiti": 10, "comunicazioni": 8,
                                             "collegamenti": 8, "possibili_da_verificare": 6, "generali": 5})
        self.assertEqual((C4.LIMIT_MAX, C4.COMPACT_TEXT), (50, 160))
        self.assertEqual(C4.WINDOWS, {"verifiche_passato_giorni": 7, "compiti_scaduti_giorni": 14, "generali_giorni": 30})
        self.assertTrue(C4.context_version().startswith("r1-"))

    def test_top_level_keys_in_stable_order(self):
        d = self.docs["matematica"]
        self.assertEqual(list(d), ["schema", "natura", "materia", "riferimento_temporale", "livelli", "sintesi", "sezioni", "freshness",
                                   "gap", "ambiguita", "warning", "provenance", "regole_agente"])
        self.assertEqual(d["schema"], C4.SCHEMA)
        self.assertEqual(list(d["sezioni"]), list(C4.SECTIONS))

    def test_all_eight_subjects_build(self):
        self.assertEqual(sorted(self.docs), sorted(SC.AGENT_SUBJECTS))
        self.assertEqual(len(SC.AGENT_SUBJECTS), 8)
        for k, d in self.docs.items():
            self.assertEqual(d["materia"]["chiave"], k)

    def test_subject_resolution_by_name_and_key(self):
        a = build(self.reg, "MATEMATICA")
        b = build(self.reg, "matematica")
        self.assertEqual(pretty(a), pretty(b))

    def test_invalid_subject_rejected(self):
        for bad in ("chimica organica", "religione", "", None, 3):
            with self.assertRaises(ValueError):
                build(self.reg, bad)

    def test_teacher_only_from_canonical_subjects(self):
        self.assertEqual(self.docs["matematica"]["materia"]["docenti"]["valori"], ["ROSSI MARIO"])
        self.assertNotIn("docente_non_disponibile", [g["codice"] for g in self.docs["matematica"]["gap"]])
        subs = subj.build_subjects([{"id": s["id"], "description": s["desc"], "teachers": [] if s["key"] == "storia" else
                                     [{"teacherName": n} for n in s["docenti_nomi"]]} for s in SUBJ])
        d = build(self.reg, "storia", subjects=subs)
        self.assertEqual(d["materia"]["docenti"], {"valori": [], "stato": "non_disponibile"})
        self.assertIn("docente_non_disponibile", [g["codice"] for g in d["gap"]])

    def test_now_required_with_timezone(self):
        with self.assertRaises(ValueError):
            build(self.reg, "matematica", now="2026-11-01T12:00:00")
        self.assertEqual(self.docs["matematica"]["riferimento_temporale"]["now"], "2026-11-01T12:00:00+02:00")
        self.assertEqual(self.docs["matematica"]["riferimento_temporale"]["finestre"]["verifiche_dal"], "2026-10-25")


# ------------------------------------------------------------------------------------------------ sezioni
class Sezioni(Base):
    def test_lessons_only_m2_determined_date_desc(self):
        d = self.docs["matematica"]
        dates = [e["data"] for e in els(d, "lezioni")]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertEqual(d["sintesi"]["contatori"]["lezioni"], 4)
        for e in els(d, "lezioni"):
            self.assertEqual(e["attribuzione"]["stato"], "determinata")
        self.assertEqual(self.docs["storia"]["sintesi"]["contatori"]["lezioni"], 1)

    def test_verifiche_window_and_sources(self):
        d = self.docs["inglese"]
        self.assertEqual(uids(d, "verifiche"), [A("C-EN", "a-en-ver"), A("C-EN", "a-en-prep"), "cvv:1000107", "cvv:1000108"])
        self.assertNotIn(A("C-EN", "a-en-vold"), uids(d, "verifiche"))                  # verifica del 20/9: oltre 7 giorni
        self.assertNotIn("cvv:1000105", uids(self.docs["filosofia"], "verifiche"))       # 22/9: oltre 7 giorni
        self.assertIn("cvv:1000106", uids(self.docs["storia"], "verifiche"))             # 1/10: entro 7 giorni
        v = els(d, "verifiche")[0]
        self.assertEqual((v["origine"], v["stato_m6"], v["regola_m6"], v["data"]), ("comunicazione_m6", "probabile", "V1", "2026-11-06"))

    def test_m6_verifica_state_not_elevated(self):
        m6 = {c["uid"]: c for c in M.compute(self.reg, SUBJ, C3.D.parse_now(NOW), MAN)[0]}
        n = 0
        for d in self.docs.values():
            for e in els(d, "verifiche"):
                if e["origine"] == "comunicazione_m6":
                    f = next(f for f in m6[e["uid"]]["interpretazione"]["funzioni"] if f["funzione"] == "verifica")
                    self.assertEqual((e["stato_m6"], e["regola_m6"]), (f["stato"], f["regola"]))
                    n += 1
        self.assertGreater(n, 0)

    def test_verifica_without_usable_date_is_gap_not_item(self):
        d = self.docs["inglese"]
        g = next(g for g in d["gap"] if g["codice"] == "verifica_senza_data_utilizzabile")
        self.assertIn(A("C-EN", "a-en-rel"), g["uid"])
        self.assertNotIn(A("C-EN", "a-en-rel"), uids(d, "verifiche"))
        self.assertNotIn(A("C-EN", "a-en-rel"), [p["uid"] for p in d["sintesi"]["prossimi_impegni"]])

    def test_compiti_open_and_recent_overdue_only(self):
        d = self.docs["inglese"]
        self.assertEqual(uids(d, "compiti"), [A("C-EN", "w-en-open"), A("C-EN", "w-en-late")])
        st = {e["uid"]: e["stato_operativo"] for e in els(d, "compiti")}
        self.assertEqual(st, {A("C-EN", "w-en-open"): "aperto", A("C-EN", "w-en-late"): "scaduto_non_consegnato"})
        for out in ("w-en-old", "w-en-done", "w-en-nodue"):
            self.assertNotIn(A("C-EN", out), uids(d, "compiti"))
        g = next(g for g in d["gap"] if g["codice"] == "compito_senza_scadenza")
        self.assertEqual(g["uid"], [A("C-EN", "w-en-nodue")])

    def test_compiti_no_invented_deadline_and_state_unchanged(self):
        d = build(self.reg, "inglese", dettaglio=["compiti"])
        late = next(e for e in els(d, "compiti") if e["uid"] == A("C-EN", "w-en-late"))
        self.assertEqual((late["scadenza"], late["stato_consegna"]), ("2026-10-23", "CREATED"))
        self.assertIn("motivo_stato", late["evidenze"])

    def test_compiti_classeviva_and_shared_course_topic(self):
        d = self.docs["matematica"]
        self.assertEqual(uids(d, "compiti"), ["cvv:3000001", A("C-MF", "w-mat-open"), "cvv:3000002", A("C-MF", "w-hw2")])
        self.assertNotIn(A("C-MF", "w-mat-open"), uids(self.docs["fisica"], "compiti"))     # topic Matematica: solo matematica
        for k in ("matematica", "fisica"):
            self.assertNotIn(A("C-MF", "w-mf-open"), uids(self.docs[k], "compiti"))         # condiviso senza topic: mai attribuito
            self.assertIn(A("C-MF", "w-mf-open"), uids(self.docs[k], "possibili_da_verificare"))

    def test_comunicazioni_only_determined_subject_operational(self):
        d = self.docs["scienze"]
        self.assertNotIn(A("C-SC", "a-hist"), uids(d, "comunicazioni"))                     # 2024-25: storico
        self.assertIn(A("C-SC", "a-old"), uids(d, "comunicazioni"))                          # 2025-26: operativo
        dates = [e["data"] for e in els(d, "comunicazioni")]
        self.assertEqual(dates, sorted(dates, reverse=True))
        for k in ("italiano", "latino", "matematica", "fisica"):                              # corsi condivisi: nessuna determinata
            self.assertEqual(els(self.docs[k], "comunicazioni"), [])

    def test_assenza_docente_visible_in_comunicazioni(self):
        e = next(e for e in els(self.docs["scienze"], "comunicazioni") if e["uid"] == A("C-SC", "a-sci-ass"))
        self.assertEqual(e["caso_speciale"], "ASSENZA_DOCENTE")
        self.assertEqual(e["classe_principale"], {"valore": "organizzativa", "stato": "probabile", "secondarie": ["compito"]})

    def test_collegamenti_touch_subject_and_keep_m5_state(self):
        res = L.compute(L.collect(self.reg, SUBJ, MAN))
        m5 = {x["link_id"]: x["stato"] for x in res["collegamenti"]}
        for k, d in self.docs.items():
            for e in els(d, "collegamenti"):
                sides = [e.get("a"), e.get("b"), e.get("elemento")] + (e.get("candidati") or [])
                self.assertIn(k, [s["materia"] for s in sides if s])
                if e["tipo"] == "collegamento":
                    self.assertEqual(e["stato"], m5[e["uid"]])
                    self.assertIn(e["stato"], ("probabile", "proposto"))

    def test_generali_window_validity_and_not_attributed(self):
        d = self.docs["storia"]
        g = d["sezioni"]["generali"]
        all_ = [x["uid"] for x in build(self.reg, "storia", limiti={"generali": 50})["sezioni"]["generali"]["elementi"]]
        self.assertIn("cvv:1014", all_)                     # 20/8 ma valida fino al 31/12
        self.assertNotIn("cvv:1015", all_)                  # 25/8 senza validita': oltre 30 giorni
        self.assertNotIn("cvv:1006", all_)                  # validita' nativa scaduta
        self.assertNotIn("cvv:1010", all_)                  # storico
        self.assertEqual(g["esclusi"], {"oltre_finestra": 1, "scaduta": 1})
        self.assertFalse(any(p["uid"].startswith("cvv:10") for p in d["sintesi"]["prossimi_impegni"]))
        self.assertTrue(all("materia" not in e and "attribuzione" not in e for e in g["elementi"]))

    def test_possibili_never_promoted_ariosto(self):
        for k in ("italiano", "latino"):
            d = self.docs[k]
            self.assertIn(A("C-LE", "a-nome"), uids(d, "possibili_da_verificare"))
            for sec in ("lezioni", "verifiche", "compiti", "comunicazioni"):
                self.assertNotIn(A("C-LE", "a-nome"), uids(d, sec))
            self.assertNotIn(A("C-LE", "a-nome"), [p["uid"] for p in d["sintesi"]["prossimi_impegni"]])
        e = next(e for e in els(self.docs["italiano"], "possibili_da_verificare") if e["uid"] == A("C-LE", "a-nome"))
        self.assertEqual((e["motivo"], e["candidati"]), ("corso_condiviso_senza_topic", ["italiano", "latino"]))

    def test_possibili_unconfirmed_course_and_conflict(self):
        d = self.docs["matematica"]
        p = {e["uid"]: e["motivo"] for e in els(d, "possibili_da_verificare")}
        self.assertEqual(p.get("cls:C-OLD"), "corso_non_confermato")
        self.assertEqual(p.get(A("C-OLD", "a-old-mat")), "corso_non_confermato")
        self.assertNotIn(A("C-OLD", "w-old"), uids(d, "compiti"))
        self.assertEqual({e["uid"]: e["motivo"] for e in els(self.docs["inglese"], "possibili_da_verificare")},
                         {A("C-EN", "a-disc"): "conflitto_materia"})
        self.assertNotIn(A("C-EN", "a-disc"), uids(self.docs["inglese"], "comunicazioni"))
        self.assertNotIn(A("C-EN", "a-disc"), uids(self.docs["storia"], "comunicazioni"))


# ------------------------------------------------------------------------------------------------ sintesi
class Sintesi(Base):
    def test_prossimi_impegni_only_attributed_future_dated(self):
        d = self.docs["matematica"]
        pi = d["sintesi"]["prossimi_impegni"]
        self.assertEqual([(p["tipo"], p["data"], p["uid"]) for p in pi],
                         [("verifica", "2026-11-02", "cvv:1000101"), ("compito", "2026-11-06", "cvv:3000001"),
                          ("compito", "2026-11-09", A("C-MF", "w-mat-open")), ("compito", "2026-11-11", "cvv:3000002"),
                          ("compito", "2026-11-13", A("C-MF", "w-hw2"))])
        poss = set(uids(d, "possibili_da_verificare")) | set(uids(d, "generali"))
        for dd in self.docs.values():
            for p in dd["sintesi"]["prossimi_impegni"]:
                self.assertGreaterEqual(p["data"], "2026-11-01")
                self.assertNotIn(p["uid"], poss)

    def test_prossimi_impegni_m6_only_probabile(self):
        pi = self.docs["inglese"]["sintesi"]["prossimi_impegni"]
        self.assertEqual([(p["uid"], p["stato"]) for p in pi], [(A("C-EN", "a-en-ver"), "probabile"), (A("C-EN", "w-en-open"), "aperto")])
        self.assertNotIn(A("C-EN", "w-en-late"), [p["uid"] for p in pi])               # scaduto: mai prossimo impegno

    def test_prossimi_impegni_limit(self):
        d = build(self.reg, "matematica", limiti={"prossimi_impegni": 2})
        self.assertEqual(len(d["sintesi"]["prossimi_impegni"]), 2)
        self.assertEqual(d["sintesi"]["contatori"]["prossimi_impegni"], 5)
        self.assertIn("prossimi_impegni", [g["sezione"] for g in d["gap"] if g["codice"] == "sezione_troncata"])

    def test_contatori_are_totals_before_limit(self):
        d = build(self.reg, "inglese", limiti={"comunicazioni": 2, "verifiche": 1})
        self.assertEqual(d["sintesi"]["contatori"]["comunicazioni"], 8)
        self.assertEqual(d["sintesi"]["mostrati"]["comunicazioni"], 2)
        self.assertEqual(d["sintesi"]["contatori"]["verifiche"], 4)
        self.assertEqual(len(els(d, "verifiche")), 1)
        for s in C4.SECTIONS:
            self.assertEqual(d["sezioni"][s]["totale"], d["sintesi"]["contatori"][s])
            self.assertEqual(d["sezioni"][s]["mostrati"], len(els(d, s)))
            self.assertEqual(d["sezioni"][s]["troncato"], d["sezioni"][s]["totale"] > d["sezioni"][s]["mostrati"])

    def test_time_windows_move_with_now(self):
        later = build(self.reg, "matematica", now="2026-11-22T12:00:00+02:00")
        self.assertEqual(uids(later, "verifiche"), [])                                  # 5/10 e' oltre 7 giorni dal 25/10
        self.assertEqual([p["uid"] for p in later["sintesi"]["prossimi_impegni"]], [])
        self.assertIn("cvv:3000002", uids(later, "compiti"))                            # 14/10: scaduto da 11 giorni, entro 14
        self.assertNotIn("cvv:3000001", uids(later, "compiti"))


# ------------------------------------------------------------------------------------------------ limiti e livelli
class LimitiEDettaglio(Base):
    def test_limits_validation_and_normalization(self):
        for bad in ({"lezioni": 0}, {"lezioni": -1}, {"lezioni": "3"}, {"lezioni": True}, {"voti": 3}, ["lezioni"]):
            with self.assertRaises(ValueError):
                build(self.reg, "matematica", limiti=bad)
        d = build(self.reg, "matematica", limiti={"generali": 500})
        self.assertEqual(d["livelli"]["limiti"]["generali"], 50)
        self.assertIn("limite_normalizzato", [w["codice"] for w in d["warning"]])

    def test_limits_applied_after_candidates(self):
        d = build(self.reg, "matematica", limiti={"lezioni": 1})
        self.assertEqual(uids(d, "lezioni"), uids(self.docs["matematica"], "lezioni")[:1])
        self.assertEqual(d["sintesi"]["contatori"]["lezioni"], 4)

    def test_compact_default(self):
        d = self.docs["inglese"]
        self.assertEqual(d["livelli"]["modo"], "compatto")
        for s in C4.SECTIONS:
            self.assertFalse(d["sezioni"][s]["dettaglio"])
            for e in els(d, s):
                self.assertNotIn("evidenze", e)
                self.assertNotIn("content_hash", e)
                self.assertLessEqual(len(e["testo"] or ""), C4.COMPACT_TEXT)
                self.assertEqual(len(e["hash"]), 12)

    def test_compact_text_truncated_and_flagged(self):
        long = "Leggete " + "molto " * 60 + "fine"
        reg = F.make_registry(announcements=F.ANNOUNCEMENTS + [ann("C-SC", "a-long", long, "2026-10-31")])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            c = build(reg, "scienze")
            dd = build(reg, "scienze", dettaglio=True)
        e = next(e for e in els(c, "comunicazioni") if e["uid"] == A("C-SC", "a-long"))
        self.assertEqual((len(e["testo"]), e["testo_troncato"]), (160, True))
        f = next(e for e in els(dd, "comunicazioni") if e["uid"] == A("C-SC", "a-long"))
        self.assertEqual(f["testo"], long)                                              # dettaglio: testo completo (oltre il tetto M6)
        reg.close()

    def test_detail_full(self):
        d = build(self.reg, "matematica", dettaglio=True)
        self.assertEqual(d["livelli"]["modo"], "dettaglio")
        for s in C4.SECTIONS:
            self.assertTrue(d["sezioni"][s]["dettaglio"])
            for e in els(d, s):
                self.assertRegex(e["content_hash"], r"^[0-9a-f]{12,}$")
                self.assertEqual(e["hash"], e["content_hash"][:12])
                self.assertIn("registry_version", e)
                self.assertIn("fonte_record", e)
                self.assertIn(e["freschezza"], ("aggiornato", "stale", "senza_lettura"))
                self.assertIn("evidenze", e)

    def test_detail_selective(self):
        d = build(self.reg, "matematica", dettaglio=["verifiche", "compiti"])
        self.assertEqual(d["livelli"]["modo"], "selettivo")
        self.assertEqual(d["livelli"]["sezioni_in_dettaglio"], ["verifiche", "compiti"])
        for s in C4.SECTIONS:
            exp = s in ("verifiche", "compiti")
            self.assertEqual(d["sezioni"][s]["dettaglio"], exp)
            for e in els(d, s):
                self.assertEqual("evidenze" in e, exp)
        c = self.docs["matematica"]
        self.assertEqual(pretty(c["sezioni"]["lezioni"]), pretty(d["sezioni"]["lezioni"]))   # le altre sezioni restano compatte
        with self.assertRaises(ValueError):
            build(self.reg, "matematica", dettaglio=["voti"])
        with self.assertRaises(ValueError):
            build(self.reg, "matematica", dettaglio="tutto")

    def test_hash_is_registry_content_hash(self):
        d = build(self.reg, "matematica", dettaglio=["lezioni", "verifiche"])
        for s in ("lezioni", "verifiche"):
            for e in els(d, s):
                self.assertEqual(e["content_hash"], e["fonte_record"]["content_hash"])
                rec = [r for r in self.reg.list_records() if r["content_hash"] == e["content_hash"]]
                self.assertEqual(len(rec), 1)
        c = self.docs["matematica"]
        self.assertEqual([e["hash"] for e in els(c, "lezioni")], [e["content_hash"][:12] for e in els(d, "lezioni")])

    def test_uid_and_hash_on_every_element(self):
        for d in self.docs.values():
            for s in C4.SECTIONS:
                for e in els(d, s):
                    self.assertTrue(e["uid"])
                    self.assertTrue(e["hash"])
            for p in d["sintesi"]["prossimi_impegni"]:
                self.assertTrue(p["uid"] and p["hash"])


# ------------------------------------------------------------------------------------------------ freschezza, gap, ambiguita'
class FreschezzaGapAmbiguita(Base):
    def test_freshness_per_section(self):
        d = self.docs["matematica"]
        self.assertEqual(set(d["freshness"]["per_sezione"]), set(C4.SECTIONS))
        self.assertEqual(set(d["freshness"]["per_sezione"].values()), {"aggiornato"})
        self.assertNotIn("sezione_non_fresca", [g["codice"] for g in d["gap"]])

    def test_stale_section_gap_and_warning_without_changing_content(self):
        with mock.patch.object(R.Registry, "freshness", fresh_fake(stale_kinds=("classeviva/notice",))):
            d = build(self.reg, "matematica")
        self.assertEqual(d["freshness"]["per_sezione"]["generali"], "stale")
        self.assertEqual(d["freshness"]["per_sezione"]["lezioni"], "aggiornato")
        self.assertIn(("sezione_non_fresca", "generali"), [(g["codice"], g["sezione"]) for g in d["gap"]])
        self.assertIn(("sezione_stale", "generali"), [(w["codice"], w["sezione"]) for w in d["warning"]])
        c = self.docs["matematica"]
        for s in C4.SECTIONS:
            self.assertEqual(pretty(els(c, s)), pretty(els(d, s)))

    def test_never_synced_is_senza_lettura(self):
        with mock.patch.object(R.Registry, "freshness", fresh_fake(none_kinds=("classeviva/lesson",))):
            d = build(self.reg, "matematica")
        self.assertEqual(d["freshness"]["per_sezione"]["lezioni"], "senza_lettura")
        self.assertIn(("sezione_non_fresca", "lezioni"), [(g["codice"], g["sezione"]) for g in d["gap"]])

    def test_soglia_ore_passed_and_validated(self):
        seen = []
        orig = fresh_fake()
        with mock.patch.object(R.Registry, "freshness", lambda self, now, stale_after_hours=None: seen.append(stale_after_hours) or orig(self, now, stale_after_hours)):
            build(self.reg, "matematica", soglia_ore=12)
        self.assertEqual(seen, [12])
        for bad in (0, -1, "48", True):
            with self.assertRaises(ValueError):
                build(self.reg, "matematica", soglia_ore=bad)

    def test_gap_codes_known(self):
        for d in self.docs.values():
            for g in d["gap"]:
                self.assertIn(g["codice"], C4.GAP_CODES)
            for a in d["ambiguita"]:
                self.assertIn(a["codice"], C4.AMBIGUITY_CODES)

    def test_expected_gaps(self):
        codes = lambda k: {g["codice"] for g in self.docs[k]["gap"]}                     # noqa: E731
        self.assertIn("corso_condiviso_senza_topic", codes("italiano"))
        self.assertIn("corso_non_confermato", codes("matematica"))
        self.assertIn("verifica_solo_da_annuncio", codes("inglese"))
        self.assertIn("circolare_senza_testo", codes("storia"))
        self.assertIn("sezione_troncata", codes("storia"))
        self.assertIn("possibili_non_comunicazioni_non_coperti", codes("matematica"))
        self.assertIn("nessuna_lezione_recente", codes("inglese"))
        self.assertNotIn("nessuna_lezione_recente", codes("matematica"))

    def test_no_false_gaps_when_simply_empty(self):
        reg = F.make_registry(lessons=[], notices=[], announcements=[], works=[], submissions=[])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            d = build(reg, "matematica")
        codes = {g["codice"] for g in d["gap"]}
        for c in ("nessuna_lezione_recente", "circolare_senza_testo", "verifica_senza_data_utilizzabile", "compito_senza_scadenza",
                  "sezione_troncata", "verifica_solo_da_annuncio", "corso_condiviso_senza_topic"):
            self.assertNotIn(c, codes)
        reg.close()

    def test_lessons_gap_when_none_recent(self):
        d = build(self.reg, "matematica", now="2026-12-28T12:00:00+01:00")
        g = next(g for g in d["gap"] if g["codice"] == "nessuna_lezione_recente")
        self.assertEqual(g["ultima"], "2026-10-24")

    def test_ambiguities(self):
        amb = lambda k: {a["codice"] for a in self.docs[k]["ambiguita"]}                  # noqa: E731
        self.assertIn("pari_livello", amb("scienze"))                                    # a-two (M6 RN2)
        self.assertIn("riferimenti_relativi_non_risolti", amb("scienze"))                # a-sci2 'per lunedi'
        self.assertIn("discordanza_date", amb("matematica"))                             # M5 RC3
        self.assertIn("conflitto_materia", amb("inglese"))                               # a-disc (M6 RC1)
        self.assertIn("possibile_stessa_verifica", amb("inglese"))
        reg = F.make_registry(lessons=F.LF.LESSONS + [lesson(9, "MATEMATICA", "ROSSI MARIO", "2026-10-30", "Leggi di Keplero e gravitazione")])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            d = build(reg, "matematica")
        self.assertIn("discordanza_materia", {a["codice"] for a in d["ambiguita"]})
        self.assertIn("cvv:lesson:9", uids(d, "lezioni"))                                # M2: materia NON modificata
        reg.close()

    def test_rc2_dates_discordant_not_a_verifica(self):
        reg = F.make_registry(announcements=F.ANNOUNCEMENTS + [ann("C-EN", "a-rc2", "Verifica del 5 novembre 2026 e verifica del 6 novembre 2026", "2026-10-31")])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            d = build(reg, "inglese")
        self.assertNotIn(A("C-EN", "a-rc2"), uids(d, "verifiche"))
        a = next(a for a in d["ambiguita"] if a["codice"] == "discordanza_date" and a["sezione"] == "verifiche")
        self.assertIn(A("C-EN", "a-rc2"), a["uid"])
        reg.close()


# ------------------------------------------------------------------------------------------------ unione verifiche (solo stati M5 reali)
class UnioneVerifiche(Base):
    def test_merge_state_is_highest_real_m5_state(self):
        self.assertEqual(C4.MERGE_STATE, "probabile")
        self.assertIn("confermato", L.RESERVED_STATES)
        self.assertEqual(C4.MERGE_RELATION, "stessa_scadenza")
        self.assertIn(C4.MERGE_RELATION, L.RELATIONS)

    def test_prepara_link_does_not_merge(self):
        d = self.docs["inglese"]
        self.assertIn(A("C-EN", "a-en-prep"), uids(d, "verifiche"))
        self.assertIn("cvv:1000107", uids(d, "verifiche"))
        a = next(a for a in d["ambiguita"] if a["codice"] == "possibile_stessa_verifica")
        self.assertEqual((a["relazione"], a["stato_m5"], a["uid"]), ("prepara", "probabile", [A("C-EN", "a-en-prep"), "cvv:1000107"]))
        self.assertEqual(d["sintesi"]["verifiche_unite"], 0)

    def _with_link(self, rel, stato):
        orig = L.compute

        def fake(prep):
            res = orig(prep)
            a = {"uid": "cvv:1000107", "fonte": {"content_hash": "x"}, "tipo": "agenda_event", "dati": {"data": "2026-10-30"},
                 "interpretazione": {"materia": "inglese", "stato_materia": "determinata"}}
            b = {"uid": A("C-EN", "a-en-ver"), "fonte": {"content_hash": "y"}, "tipo": "announcement", "dati": {"data_creazione": "2026-10-30"},
                 "interpretazione": {"materia": "inglese", "stato_materia": "determinata"}}
            res = dict(res, collegamenti=res["collegamenti"] + [{"link_id": "fake-" + stato, "relazione": rel, "stato": stato, "a": a, "b": b,
                                                                 "interpretazione": {"regola": "RX", "prove": []}}])
            return res
        with mock.patch.object(L, "compute", fake):
            return build(self.reg, "inglese")

    def test_merge_only_with_stessa_scadenza_probabile(self):
        d = self._with_link("stessa_scadenza", "probabile")
        self.assertEqual(d["sintesi"]["verifiche_unite"], 1)
        self.assertNotIn(A("C-EN", "a-en-ver"), uids(d, "verifiche"))
        keep = next(e for e in els(d, "verifiche") if e["uid"] == "cvv:1000107")
        self.assertEqual(keep["uniti"], [{"uid": A("C-EN", "a-en-ver"), "origine": "comunicazione_m6",
                                          "regola": "m5_stessa_scadenza_probabile", "link_id": "fake-probabile"}])
        self.assertEqual(d["sintesi"]["contatori"]["verifiche"], 3)
        self.assertEqual(d["sezioni"]["verifiche"]["unioni"]["n"], 1)

    def test_lower_state_or_other_relation_never_merges(self):
        for rel, st in (("stessa_scadenza", "proposto"), ("prepara", "probabile"), ("stesso_argomento", "proposto")):
            d = self._with_link(rel, st)
            self.assertEqual(d["sintesi"]["verifiche_unite"], 0)
            self.assertIn(A("C-EN", "a-en-ver"), uids(d, "verifiche"))
            self.assertIn("fake-" + st, [a.get("link_id") for a in d["ambiguita"] if a["codice"] == "possibile_stessa_verifica"])

    def test_same_uid_merge(self):
        orig = M.compute

        def fake(reg, subjects, now_dt, manual=None):
            allc, prep, lv = orig(reg, subjects, now_dt, manual)
            allc = [dict(x, uid="cvv:1000107") if x["uid"] == A("C-EN", "a-en-ver") else x for x in allc]
            return allc, prep, lv
        with mock.patch.object(M, "compute", fake):
            d = build(self.reg, "inglese")
        self.assertEqual(d["sintesi"]["verifiche_unite"], 1)
        self.assertEqual(uids(d, "verifiche").count("cvv:1000107"), 1)
        e = next(e for e in els(d, "verifiche") if e["uid"] == "cvv:1000107")
        self.assertEqual((e["origine"], e["uniti"][0]["regola"]), ("agenda_classeviva", "stesso_uid"))

    def test_m5_module_untouched_and_never_confirmed(self):
        res = L.compute(L.collect(self.reg, SUBJ, MAN))
        self.assertFalse([x for x in res["collegamenti"] if x["stato"] == "confermato"])
        self.assertEqual(L.link_version(), "r1-15cce5")


# ------------------------------------------------------------------------------------------------ determinismo, scritture, dimensione
class DeterminismoESicurezza(unittest.TestCase):
    def test_byte_identical_and_order_independent(self):
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            a, b = F.make_registry(), F.make_registry(reverse=True)
            for k in SC.AGENT_SUBJECTS:
                for kw in ({}, {"dettaglio": True}, {"dettaglio": ["compiti"], "limiti": {"generali": 2}}):
                    x1, x2, y = build(a, k, **kw), build(a, k, **kw), build(b, k, **kw)
                    self.assertEqual(pretty(x1), pretty(x2))
                    self.assertEqual(pretty(x1), pretty(y))
            a.close(); b.close()

    def test_builder_never_reads_clock(self):
        src = open(C4.__file__, encoding="utf-8").read()
        for bad in ("datetime.now", "dt.datetime.now", "time.time", "date.today", "utcnow"):
            self.assertNotIn(bad, src)

    def test_no_writes_in_memory_registry(self):
        reg = F.make_registry()
        before, ver = counts(reg), {(r["source"], r["kind"], r["source_uid"]): r["content_hash"] for r in reg.list_records()}
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            for k in SC.AGENT_SUBJECTS:
                build(reg, k, dettaglio=True)
        self.assertEqual(before, counts(reg))
        self.assertEqual(ver, {(r["source"], r["kind"], r["source_uid"]): r["content_hash"] for r in reg.list_records()})
        reg.close()

    def test_build_from_path_writes_nothing(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "registry.sqlite3")
        reg = F.make_registry(path=p)
        reg.close()
        man = os.path.join(d, "school_manual.json")
        with open(man, "w") as f:
            json.dump(MAN, f)
        h = lambda q: hashlib.sha256(open(q, "rb").read()).hexdigest()                 # noqa: E731
        s0, files0, h0, hm = dry._fs_state(p), sorted(os.listdir(d)), h(p), h(man)
        out = C4.build_from_path(p, "matematica", NOW, manual_path=man, subjects=SUBJ)
        self.assertEqual(out["provenance"]["sorgente"], os.path.abspath(p))
        self.assertEqual((s0, files0, h0, hm), (dry._fs_state(p), sorted(os.listdir(d)), h(p), h(man)))

    def test_size_under_40kb_with_heavy_data(self):
        long = "Esercizi di ripasso con molte indicazioni " * 20
        anns = F.ANNOUNCEMENTS + [ann("C-EN", "a-h%d" % i, "%d %s" % (i, long), "2026-09-%02d" % (1 + i % 28)) for i in range(150)]
        anns += [ann("C-LE", "a-l%d" % i, "%d %s" % (i, long), "2026-09-%02d" % (1 + i % 28)) for i in range(150)]
        les = F.LF.LESSONS + [lesson(100 + i, "LINGUA E CULTURA STRANIERA 1", "NERI SARA", "2026-09-%02d" % (1 + i % 28), long) for i in range(60)]
        reg = F.make_registry(announcements=anns, lessons=les)
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            for k in SC.AGENT_SUBJECTS:
                n = len(pretty(build(reg, k)).encode("utf-8"))
                self.assertLessEqual(n, 40 * 1024, k)
        reg.close()

    def test_m4_m5_m6_outputs_unchanged_by_m7(self):
        reg = F.make_registry()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            before = [pretty(C3.build(reg, "matematica", SUBJ, NOW, manual=MAN)), pretty(C3.overview(reg, SUBJ, NOW, manual=MAN)),
                      pretty(L.build(reg, SUBJ, NOW, manual=MAN)), pretty(M.build(reg, SUBJ, NOW, manual=MAN))]
            for k in SC.AGENT_SUBJECTS:
                build(reg, k, dettaglio=True)
            after = [pretty(C3.build(reg, "matematica", SUBJ, NOW, manual=MAN)), pretty(C3.overview(reg, SUBJ, NOW, manual=MAN)),
                     pretty(L.build(reg, SUBJ, NOW, manual=MAN)), pretty(M.build(reg, SUBJ, NOW, manual=MAN))]
        self.assertEqual(before, after)
        reg.close()

    def test_provenance_versions(self):
        reg = F.make_registry()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            p = build(reg, "fisica")["provenance"]
        self.assertEqual((p["schema"], p["context_version"], p["comm_version"], p["comm_rules_rev"], p["link_version"]),
                         (C4.SCHEMA, C4.context_version(), M.comm_version(), 2, L.link_version()))
        self.assertEqual(p["corsi_confermati"], ["C-MF"])
        reg.close()


# ------------------------------------------------------------------------------------------------ M7.1: collect / render
class CollectRender(Base):
    def test_build_is_render_of_collect(self):
        for k in SC.AGENT_SUBJECTS:
            for kw in ({}, {"dettaglio": True}, {"dettaglio": ["compiti"], "limiti": {"lezioni": 1, "prossimi_impegni": 1}}):
                c = C4.collect(self.reg, k, NOW, manual=MAN, subjects=SUBJ)
                self.assertEqual(pretty(build(self.reg, k, **kw)), pretty(C4.render(c, **kw)))

    def test_collect_has_all_candidates_before_limits(self):
        c = C4.collect(self.reg, "inglese", NOW, manual=MAN, subjects=SUBJ)
        d = C4.render(c, limiti={s: 1 for s in C4.LIMITS_DEFAULT})
        for s in C4.SECTIONS:
            self.assertEqual(len(c["candidati"][s]), d["sintesi"]["contatori"][s])
        self.assertEqual(len(c["prossimi_impegni"]), d["sintesi"]["contatori"]["prossimi_impegni"])
        self.assertEqual([r["uid"] for r in c["candidati"]["comunicazioni"]][:1], uids(d, "comunicazioni"))

    def test_render_does_not_mutate_collect(self):
        c = C4.collect(self.reg, "matematica", NOW, manual=MAN, subjects=SUBJ)
        first = pretty(C4.render(c))
        C4.render(c, dettaglio=True, limiti={"generali": 1, "prossimi_impegni": 1})
        self.assertEqual(first, pretty(C4.render(c)))

    def test_fatti_internal_only_and_with_school_year(self):
        c = C4.collect(self.reg, "inglese", NOW, manual=MAN, subjects=SUBJ)
        for s in ("lezioni", "verifiche", "compiti", "comunicazioni"):
            for r in c["candidati"][s]:
                self.assertIn("anno_scolastico", r["_fatti"])
        late = {r["uid"]: r["_fatti"] for r in c["candidati"]["compiti"]}
        self.assertEqual(late[A("C-EN", "w-en-open")]["late"], False)
        self.assertEqual(late[A("C-EN", "w-en-open")]["anno_scolastico"], "2026-27")
        self.assertNotIn("_fatti", pretty(C4.render(c, dettaglio=True)))

    def test_collect_validates_like_build(self):
        for bad in ("chimica", "", None):
            with self.assertRaises(ValueError):
                C4.collect(self.reg, bad, NOW, manual=MAN, subjects=SUBJ)
        c = C4.collect(self.reg, "matematica", NOW, manual=MAN, subjects=SUBJ)
        with self.assertRaises(ValueError):
            C4.render(c, limiti={"voti": 1})


# ------------------------------------------------------------------------------------------------ tool MCP
class ServerTool(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.d = tempfile.mkdtemp()
        os.chmod(self.d, 0o700)
        self.db = os.path.join(self.d, "registry.sqlite3")
        self.manual = os.path.join(self.d, "manual", "school_manual.json")
        for p in (mock.patch.dict(os.environ, {"CLOUD_SCHOOL_DB": self.db}), mock.patch.object(server, "subjects", lambda: SUBJ),
                  mock.patch.object(SC, "manual_default_path", lambda: self.manual)):
            p.start(); self.addCleanup(p.stop)

    def populate(self):
        F.make_registry(path=self.db).close()
        os.makedirs(os.path.dirname(self.manual), exist_ok=True)
        with open(self.manual, "w") as f:
            json.dump(MAN, f)

    def test_tool_registered_with_schema(self):
        t = self.server.srv._tools["school_context_v4"]["def"]
        self.assertEqual(set(t["inputSchema"]["properties"]), {"materia", "dettaglio", "limiti", "soglia_ore"})
        self.assertEqual(t["inputSchema"]["required"], ["materia"])
        self.assertIn("SOLA LETTURA", t["description"])
        self.assertIn("school_context_v3", self.server.srv._tools)

    def test_missing_registry_clear_error(self):
        with self.assertRaises(ValueError) as cm:
            self.server.school_context_v4("matematica")
        self.assertIn("registro locale assente", str(cm.exception))
        self.assertFalse(os.path.exists(self.db))

    def test_tool_reads_clock_once_and_writes_nothing(self):
        self.populate()
        calls = []
        fixed = dt.datetime(2026, 11, 1, 12, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))

        class FakeDT:
            class datetime:
                @staticmethod
                def now(tz=None):
                    calls.append(tz)
                    return fixed
        h = lambda q: hashlib.sha256(open(q, "rb").read()).hexdigest()                 # noqa: E731
        h0, files0 = h(self.db), sorted(os.listdir(self.d))
        with mock.patch.object(self.server, "dt", FakeDT):
            out = self.server.school_context_v4("matematica", dettaglio=["verifiche"], limiti={"lezioni": 2})
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["riferimento_temporale"]["now"], "2026-11-01T12:00:00+02:00")
        self.assertEqual(out["schema"], "cloud.school.context/4")
        self.assertFalse(out["verifica_nessuna_scrittura"]["file_modificati"])
        self.assertFalse(any(out["scritture"].values()))
        self.assertEqual(len(out["sezioni"]["lezioni"]["elementi"]), 2)
        self.assertEqual((h0, files0), (h(self.db), sorted(os.listdir(self.d))))


if __name__ == "__main__":
    unittest.main()
