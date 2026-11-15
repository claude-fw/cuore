"""JOB DI SINCRONIZZAZIONE (school_job): test d'integrazione OFFLINE su registro temporaneo e fonti SINTETICHE.
Nessuna rete, nessun Keychain, nessun registro reale, nessuna notifica reale (send finto)."""
import copy
import datetime as dt
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "..", "_common"))
import cls_fixtures as CF         # noqa: E402
import cvv_sync as S              # noqa: E402
import registry as R              # noqa: E402
import school_job as J            # noqa: E402
from test_absence_identity import ab, justified   # noqa: E402
from test_dryrun import absences, notices          # noqa: E402

CV_KINDS = ["agenda_event", "grade", "absence", "notice"]
CLS_KINDS = ["course", "course_work", "material", "announcement", "topic", "submission"]
KINDS = {"classeviva": CV_KINDS, "classroom": CLS_KINDS}
WIN = ("2026-09-01", "2027-08-31")
EVENT = {"evtId": 7, "evtCode": "AGNT", "subjectDesc": None, "evtDatetimeBegin": "2026-10-10T08:00:00+02:00",
         "evtDatetimeEnd": "2026-10-10T09:00:00+02:00", "isFullDay": False, "notes": "Verifica di storia (SINT.)",
         "authorName": "X", "classDesc": "3A"}
NOW = dt.datetime(2026, 10, 19, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))


def many_notices(n):
    out = notices(n)
    for i, r in enumerate(out):
        r["pubDT"] = "2026-10-%02dT12:00:00+02:00" % (i % 28 + 1)
    return out


def cv_data():
    return {"agenda_event": [dict(EVENT)], "grade": [], "absence": absences(), "notice": notices()}


def cls_data():
    return {"course": [CF.COURSE_A, CF.COURSE_B], "course_work": CF.WORK, "material": CF.MATERIALS,
            "announcement": CF.ANNOUNCEMENTS, "topic": CF.TOPICS, "submission": CF.SUBMISSIONS}


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


class Clock:
    """`t` = time.monotonic (si ferma con il Mac in stop); `r` = CLOCK_MONOTONIC (conta anche lo stop)."""

    def __init__(self):
        self.t, self.r, self.slept = 0.0, 0.0, []

    def monotonic(self):
        return self.t

    def real(self):
        return self.r

    def sleep(self, s):
        self.slept.append(s)
        self.t += s
        self.r += s


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "school")
        self.path = os.path.join(self.root, "registry.sqlite3")
        self.state = os.path.join(self.root, "job")
        S.ensure_private_dir(self.root)
        R.Registry(self.path).close()                       # registro esistente e vuoto (il job non lo crea mai)
        self.cv, self.cls = cv_data(), cls_data()
        self.cv_fail, self.cls_fail, self.pre_fail = {}, {}, None
        self.calls = {"cv": [], "cls": [], "pre": 0}
        self.sent = []
        self.clock = Clock()
        self.now = NOW
        p = mock.patch.object(R, "default_path", side_effect=AssertionError("il registro reale non va mai aperto"))
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.dict(os.environ, {"CUORE_SCHOOL_JOB_PLIST": os.path.join(self.root, "nessun-calendario.plist")})
        e.start()                                           # `main` non legge mai il calendario reale del LaunchAgent
        self.addCleanup(e.stop)

    def _fetchers(self, data, fails, calls, windows=None):
        def mk(k):
            def f():
                calls.append(k)
                exc = fails.get(k)
                if isinstance(exc, list):
                    if exc:
                        raise exc.pop(0)
                elif exc is not None:
                    raise exc
                return copy.deepcopy(data[k])
            return f
        return {k: (mk(k), (windows or {}).get(k)) for k in data}

    def deps(self):
        def pre():
            self.calls["pre"] += 1
            if self.pre_fail is not None:
                raise self.pre_fail
        return J.Deps(sources={"classeviva": lambda: (self._fetchers(self.cv, self.cv_fail, self.calls["cv"],
                                                                          {"agenda_event": WIN}), None),
                               "classroom": lambda: (self._fetchers(self.cls, self.cls_fail, self.calls["cls"]), pre)},
                      now=lambda: self.now, sleep=self.clock.sleep, monotonic=self.clock.monotonic,
                      notify_send=lambda t, m: self.sent.append((t, m)), real_clock=self.clock.real)

    def job(self, commit=True, **kw):
        self.logged = []
        return J.run_job(commit=commit, db_path=self.path, state_dir=self.state, deps=self.deps(), kinds=KINDS,
                         log=lambda ev, **k: self.logged.append(dict(k, evento=ev)), **kw)

    def records(self, source=None, kind=None, status=None):
        reg = R.Registry.from_snapshot(self.path)
        try:
            return {r["source_uid"]: r["source_status"] for r in reg.list_records(source=source, kind=kind, status=status)}
        finally:
            reg.close()


class ClassificazioneErrori(unittest.TestCase):
    def test_categories(self):
        C = J.classify_error
        for m in ("CVError: Rete non raggiungibile: timed out", "URLError: <urlopen error timed out>",
                  "ClassroomApiError: HTTP 503 [server] backend", "ClassroomApiError: HTTP 429 [rate_limit] x",
                  "AuthError: rinnovo token non riuscito: HTTP 500", "AuthError: rete non raggiungibile: URLError",
                  "FetchFailed: TimeoutError: The read operation timed out", "CVError: Login ClasseViva fallito: HTTP 503 - errore."):
            self.assertEqual(C(m), "temporaneo", m)
        for m in ("AuthError: rinnovo token non riuscito: invalid_grant (Token has been expired or revoked.)",
                  "CVError: Login ClasseViva fallito: HTTP 422 - credenziali non valide",
                  "ClassroomApiError: HTTP 403 [scope_insufficient] x",
                  "ClassroomApiError: HTTP 401 [auth] x"):
            self.assertEqual(C(m), "autorizzazione", m)
        for m in ("CVError: Credenziali non trovate nel Keychain (servizio cloud.classeviva)",
                  "CVError: Accesso al Keychain negato o annullato.",
                  "AuthError: Classroom non autorizzato: esegui `python3 cls_auth.py authorize`",
                  "AuthError: client id/secret mancanti nel Keychain (servizio cloud.classroom-probe)"):
            self.assertEqual(C(m), "keychain", m)                  # non e' detto che serva riautorizzare
        self.assertEqual(C("CVError: Risposta inattesa da /x: chiave 'grades' assente"), "dati")
        self.assertEqual(C("KeyError: 'id'"), "errore")

    def test_mask_hides_token_like_strings(self):
        tok = "SINTETICO-" + "A" * 60
        self.assertNotIn(tok, J.mask("errore con %s dentro" % tok))
        self.assertIn("<MASCHERATO>", J.mask(tok))


class Letture(unittest.TestCase):
    def reader(self, fn, attempts=3, deadline=10_000):
        self.clock = Clock()
        return J.Reader(fn, "x", dict(J.POLICY, attempts=attempts), deadline, self.clock.sleep, self.clock.monotonic,
                        lambda *a, **k: None)

    def test_temporary_error_is_retried_with_backoff_then_memoized(self):
        seq = [OSError("Rete non raggiungibile: timed out"), OSError("timed out")]
        calls = []

        def fn():
            calls.append(1)
            if seq:
                raise seq.pop(0)
            return [{"a": 1}]
        r = self.reader(fn)
        self.assertEqual(r(), [{"a": 1}])
        self.assertEqual((len(calls), r.attempts, self.clock.slept), (3, 3, [30, 120]))
        out = r()
        out[0]["a"] = 99                                   # copia: il risultato memorizzato non cambia
        self.assertEqual((r(), len(calls)), ([{"a": 1}], 3))

    def test_auth_error_is_not_retried(self):
        calls = []

        def fn():
            calls.append(1)
            raise RuntimeError("AuthError: invalid_grant")
        r = self.reader(fn)
        with self.assertRaises(RuntimeError):
            r()
        with self.assertRaises(RuntimeError):
            r()                                            # errore memorizzato: nessuna nuova lettura
        self.assertEqual((len(calls), self.clock.slept), (1, []))

    def test_attempts_are_bounded_and_deadline_stops_retries(self):
        r = self.reader(lambda: (_ for _ in ()).throw(OSError("timed out")), attempts=3)
        with self.assertRaises(OSError):
            r()
        self.assertEqual(r.attempts, 3)
        r = self.reader(lambda: (_ for _ in ()).throw(OSError("timed out")), deadline=10)
        with self.assertRaises(OSError):
            r()
        self.assertEqual((r.attempts, self.clock.slept), (1, []))

    def test_classeviva_login_rejected_is_never_retried(self):
        n = []

        def factory():
            n.append(1)
            raise RuntimeError("Login ClasseViva fallito: HTTP 422 - credenziali non valide")
        login = J._Login(factory)
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                login()
        self.assertEqual(len(n), 1)


class ScadenzaRealeLettura(unittest.TestCase):
    """RealDeadline: CLOCK_MONOTONIC finto che avanza durante lo stop mentre time.monotonic avanza appena."""

    def setUp(self):
        self.clock = Clock()
        self.dl = J.RealDeadline(self.clock.real, J.POLICY["real_deadline_s"])

    def reader(self, fn):
        return J.Reader(fn, "x", dict(J.POLICY), 10_000, self.clock.sleep, self.clock.monotonic, lambda *a, **k: None, self.dl)

    def stop(self, seconds):
        self.clock.r += seconds
        self.clock.t += 1                                  # monotonic: quasi fermo durante lo stop

    def test_default_limit_is_30_minutes_and_monotonic_deadline_is_kept(self):
        self.assertEqual((J.POLICY["real_deadline_s"], J.POLICY["deadline_s"]), (1800, 1800))

    def test_default_real_clock_is_clock_monotonic_without_fallback(self):
        self.assertIs(J.Deps().real_clock, J.sleep_inclusive_clock)
        a, b = J.sleep_inclusive_clock(), time.clock_gettime(time.CLOCK_MONOTONIC)
        self.assertLess(abs(b - a), 1.0)
        with mock.patch.object(J.time, "CLOCK_MONOTONIC", new=None, create=True), \
                mock.patch.object(J.time, "clock_gettime", side_effect=AttributeError("CLOCK_MONOTONIC assente")):
            with self.assertRaises(AttributeError):       # nessun ripiego silenzioso su un altro orologio
                J.sleep_inclusive_clock()

    def test_civil_clock_is_not_used(self):
        src = J.RealDeadline.__init__.__code__.co_names + J.RealDeadline.elapsed.__code__.co_names
        self.assertNotIn("now", src)
        self.assertNotIn("datetime", src)

    def test_no_read_starts_after_the_real_limit(self):
        self.stop(2 * 3600)
        calls = []
        r = self.reader(lambda: calls.append(1) or [])
        with self.assertRaises(J.RealTimeExceeded) as cm:
            r()
        self.assertEqual((calls, r.attempts, self.clock.slept), ([], 0, []))
        self.assertEqual(J.classify_error(J._err(cm.exception)), "temporaneo")      # stato d'errore esistente, mai successo
        with self.assertRaises(J.RealTimeExceeded):
            r()                                                                     # memorizzato: nessuna lettura

    def test_no_retry_and_no_wait_once_the_limit_passes_during_a_read(self):
        calls = []

        def fn():
            calls.append(1)
            self.stop(2 * 3600)                            # la lettura resta appesa durante lo stop, poi va in timeout
            raise OSError("The read operation timed out")
        r = self.reader(fn)
        with self.assertRaises(OSError):
            r()
        self.assertEqual((len(calls), r.attempts, self.clock.slept), (1, 1, []))      # col solo monotonic: 3 letture, [30, 120]

    def test_a_wait_that_would_cross_the_limit_is_not_started(self):
        self.clock.r = 1790                                # 10 s al limite: l'attesa di 30 s lo supererebbe
        r = self.reader(lambda: (_ for _ in ()).throw(OSError("timed out")))
        with self.assertRaises(OSError):
            r()
        self.assertEqual((r.attempts, self.clock.slept), (1, []))

    def test_retry_after_a_wait_spent_in_stop_is_not_started(self):
        calls = []

        def sleep_in_stop(s):                              # l'attesa di 30 s dura ore: il Mac va in stop
            self.clock.slept.append(s)
            self.clock.t += s
            self.clock.r += 2 * 3600
        r = J.Reader(lambda: calls.append(1) or (_ for _ in ()).throw(OSError("timed out")), "x", dict(J.POLICY), 10_000,
                     sleep_in_stop, self.clock.monotonic, lambda *a, **k: None, self.dl)
        with self.assertRaises(OSError):                   # resta l'ultimo errore reale, non un successo
            r()
        self.assertEqual((len(calls), r.attempts, self.clock.slept, r.deadline_hit), (1, 1, [30], True))

    def test_within_the_limit_retries_are_unchanged(self):
        seq = [OSError("timed out"), OSError("timed out")]
        r = self.reader(lambda: (_ for _ in ()).throw(seq.pop(0)) if seq else [{"a": 1}])
        self.assertEqual(r(), [{"a": 1}])
        self.assertEqual((r.attempts, self.clock.slept), (3, [30, 120]))


class _StopClock(Base):
    """Giro intero: dopo `stop_at` letture il Mac va in stop per 2 ore (CLOCK_MONOTONIC avanza), time.monotonic quasi no.
    `civil_jump` sposta invece SOLO l'orologio civile (correzione dell'ora), che il limite deve ignorare."""

    def setUp(self):
        super().setUp()
        self.reads, self.stop_at, self.jump, self.civil_jump = [], None, 2 * 3600, None

    def _fetchers(self, data, fails, calls, windows=None):
        out = super()._fetchers(data, fails, calls, windows)

        def wrap(k, f):
            def g():
                self.reads.append(k)
                try:
                    return f()
                finally:
                    self.clock.t += 1
                    self.clock.r += 1
                    if len(self.reads) == self.stop_at:
                        if self.civil_jump is not None:
                            self.now += self.civil_jump
                        else:
                            self.clock.r += self.jump
            return g
        return {k: (wrap(k, f), w) for k, (f, w) in out.items()}

    def freshness(self, now=None):
        reg = R.Registry.from_snapshot(self.path)
        try:
            return reg.freshness((now or dt.datetime.now(dt.timezone.utc)).isoformat())["per_kind"]
        finally:
            reg.close()


class ScadenzaRealeJob(_StopClock):
    def test_stop_during_classeviva_does_not_starve_classroom_and_next_run_recovers(self):
        self.assertEqual(self.job()["esito"], "OK")                                # giro precedente riuscito (come il 22/10)
        self.reads, self.calls["cv"][:], self.calls["cls"][:] = [], [], []
        self.stop_at = 1                                                           # stop dopo la 1a lettura di ClasseViva
        s = self.job()
        self.assertEqual((s["esito"], s["exit_code"]), ("PARZIALE", 1))           # mai OK con tipi non aggiornati
        self.assertEqual(s["ordine_fonti"]["ordine"], ["classeviva", "classroom"])
        first = self.reads[0]
        cv = s["fonti"]["classeviva"]["kinds"]
        self.assertEqual(cv[first]["stato"], "scritto")                           # completata prima: resta scritta
        for k in CV_KINDS:
            if k != first:
                self.assertEqual((cv[k]["stato"], cv[k]["categoria"], cv[k]["tentativi"], cv[k]["motivo"]),
                                 ("errore", "temporaneo", 0, J.DEADLINE_REASON))
                self.assertNotIn(k, self.calls["cv"])                              # nessuna lettura dopo il limite
        cls = s["fonti"]["classroom"]                                              # budget proprio: NON saltata
        self.assertEqual((cls["esito"], {v["stato"] for v in cls["kinds"].values()}), ("ok", {"scritto"}))
        self.assertEqual(sorted(self.calls["cls"]), sorted(CLS_KINDS))
        self.assertEqual(self.clock.slept, [])
        self.assertLess(s["durata_s"], 60)                                         # monotonic: pochi secondi
        self.assertEqual(s["registro"]["percorso"], self.path)                     # solo il registro temporaneo
        written = self.records(source="classeviva", kind=first)
        self.assertTrue(written and set(written.values()) == {R.PRESENT})
        fr = self.freshness()
        other = next(k for k in CV_KINDS if k != first)
        self.assertEqual(fr["classeviva/" + other]["motivo"], "ultimo_run_in_errore")
        with S._Lock(os.path.join(self.root, "school-job.lock")):                  # lock rilasciato anche alla scadenza
            pass
        self.stop_at, self.reads = None, []                                        # giro successivo, Mac sveglio
        s2 = self.job()
        self.assertEqual((s2["esito"], s2["exit_code"]), ("OK", 0))
        self.assertEqual(s2["ordine_fonti"]["ordine"][0], "classeviva")           # rimasta indietro: va per prima
        self.assertEqual(self.freshness()["classeviva/" + other]["motivo"], "ok")
        self.assertEqual(written, self.records(source="classeviva", kind=first))  # nessuna riscrittura del gia' scritto

    def test_source_left_behind_goes_first_in_the_next_run(self):
        self.stop_at = len(CV_KINDS) + 1                                           # stop dopo la 1a lettura di Classroom
        s = self.job()
        self.assertEqual((s["esito"], s["ordine_fonti"]["ordine"]), ("PARZIALE", ["classeviva", "classroom"]))
        skipped = [k for k, v in s["fonti"]["classroom"]["kinds"].items() if v.get("motivo") == J.DEADLINE_REASON]
        self.assertEqual(len(skipped), len(CLS_KINDS) - 1)
        self.stop_at, self.reads = None, []
        s2 = self.job()
        self.assertEqual(s2["ordine_fonti"], {"ordine": ["classroom", "classeviva"], "criterio": "lettura_riuscita_piu_vecchia"})
        self.assertEqual(self.reads[0] in CLS_KINDS, True)                         # Classroom letta per prima
        self.assertEqual((s2["esito"], s2["exit_code"]), ("OK", 0))
        self.stop_at, self.reads = None, []
        self.assertEqual(self.job()["ordine_fonti"]["ordine"], ["classeviva", "classroom"])   # poi torna l'ordine normale

    def test_source_order_falls_back_to_default_and_says_so(self):
        out = J.source_order(os.path.join(self.root, "manca.sqlite3"), ("classroom", "classeviva"), KINDS, NOW)
        self.assertEqual(out["ordine"], ["classeviva", "classroom"])
        self.assertEqual(out["criterio"], "predefinito")
        self.assertIn("freschezza non leggibile", out["nota"])

    def test_timeout_during_stop_in_classroom_is_not_retried(self):
        self.stop_at = len(CV_KINDS) + 1                                           # prima lettura di Classroom
        self.cls_fail = {k: [OSError("The read operation timed out")] for k in CLS_KINDS}
        s = self.job()
        self.assertEqual(s["esito"], "PARZIALE")
        self.assertEqual({v["stato"] for v in s["fonti"]["classeviva"]["kinds"].values()}, {"scritto"})
        cls = s["fonti"]["classroom"]["kinds"]
        tried = [k for k, v in cls.items() if v.get("tentativi")]
        self.assertEqual(len(tried), 1)
        self.assertEqual((cls[tried[0]]["tentativi"], cls[tried[0]]["categoria"]), (1, "temporaneo"))
        self.assertEqual(len(self.calls["cls"]), 1)                                # nessun retry, nessun altro tipo letto
        self.assertEqual(self.clock.slept, [])                                     # nessuna attesa di 30 o 120 s
        self.assertEqual(self.records(source="classroom"), {})

    def test_normal_run_with_real_time_passing_is_unchanged(self):
        self.stop_at, self.jump = 3, 120                                           # tempo reale normale, sotto il limite
        s = self.job()
        self.assertEqual((s["esito"], s["exit_code"]), ("OK", 0))
        self.assertEqual({v["stato"] for f in s["fonti"].values() for v in f["kinds"].values()}, {"scritto"})
        self.assertEqual(sorted(self.reads), sorted(CV_KINDS + CLS_KINDS))

    def test_civil_clock_jump_does_not_trigger_the_limit(self):
        self.stop_at, self.civil_jump = 1, dt.timedelta(hours=5)                   # ora corretta in avanti di 5 h
        s = self.job()
        self.assertEqual((s["esito"], s["exit_code"]), ("OK", 0))
        self.assertEqual(sorted(self.reads), sorted(CV_KINDS + CLS_KINDS))
        self.stop_at, self.reads, self.civil_jump = 1, [], -dt.timedelta(hours=5)  # e all'indietro
        self.assertEqual(self.job()["esito"], "OK")


class FreschezzaFonti(_StopClock):
    """Riepilogo del job e freschezza del Registry distinguono: aggiornata, fallita, saltata per scadenza, non eseguita,
    dati vecchi. Nessun dato storico cancellato."""

    def baseline(self):
        self.assertEqual(self.job()["esito"], "OK")
        self.reads = []

    def test_updated(self):
        s = self.job()
        self.assertEqual(s["fonti"]["classeviva"]["kinds"]["notice"]["stato"], "scritto")
        fr = self.freshness()["classeviva/notice"]
        self.assertEqual((fr["motivo"], fr["stale"], fr["ultimo_errore"]), ("ok", False, None))

    def test_failed(self):
        self.baseline()
        self.cv_fail["absence"] = OSError("Rete non raggiungibile: timed out")
        k = self.job()["fonti"]["classeviva"]["kinds"]["absence"]
        self.assertEqual((k["stato"], k["categoria"], k["tentativi"], "motivo" in k), ("errore", "temporaneo", 3, False))
        fr = self.freshness()["classeviva/absence"]
        self.assertEqual((fr["motivo"], fr["stale"]), ("ultimo_run_in_errore", True))
        self.assertNotIn("tempo reale", fr["ultimo_errore"]["errore"])
        self.assertEqual(set(self.records(kind="absence").values()), {R.PRESENT})   # dati precedenti conservati

    def test_skipped_for_real_deadline(self):
        self.baseline()
        self.stop_at = 1
        s = self.job()
        skipped = [k for k, v in s["fonti"]["classeviva"]["kinds"].items() if v.get("motivo") == J.DEADLINE_REASON]
        self.assertEqual(len(skipped), len(CV_KINDS) - 1)
        for k in skipped:
            fr = self.freshness()["classeviva/" + k]
            self.assertEqual((fr["motivo"], fr["stale"]), ("ultimo_run_in_errore", True))
            self.assertIn("tempo reale massimo superato", fr["ultimo_errore"]["errore"])
        stale = {x["tipo"] for x in s["freschezza"]["oltre_soglia"]}
        self.assertEqual(stale, {"classeviva/" + k for k in skipped})             # evidente nel riepilogo del job
        probs = J.derive_problems(s)
        # dati ancora entro la soglia: non e' «dati non aggiornati» (prima lo era, con un testo «oltre 48 h» falso,
        # osservato il 2026-10-24 alle 20:00); il parziale di soli errori temporanei si notifica se si ripete
        self.assertNotIn("freschezza", probs)
        self.assertEqual(probs["temporaneo:classeviva"][2], 2)
        self.assertFalse(any(k.startswith("parziale:classeviva") for k in probs))

    def test_not_executed(self):
        os.remove(self.path)                                                       # registro temporaneo assente
        s = self.job()
        self.assertEqual((s["esito"], {f["esito"] for f in s["fonti"].values()}), ("FALLITO", {"non_eseguita"}))
        self.assertEqual((self.reads, self.calls["pre"]), ([], 0))
        R.Registry(self.path).close()
        before = self.freshness()
        self.job(commit=False)                                                     # dry-run: la freschezza non cambia
        self.assertEqual(self.freshness(), before)

    def test_old_data_still_present(self):
        self.baseline()
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=49)
        fr = self.freshness(later)["classroom/course"]
        self.assertEqual((fr["motivo"], fr["stale"]), ("oltre_soglia", True))
        self.assertTrue(self.records(source="classroom", kind="course"))           # dati vecchi ma conservati
        self.assertEqual(set(self.records(source="classroom", kind="course").values()), {R.PRESENT})


class DryRunECommit(Base):
    def test_dry_run_writes_nothing_and_is_ok(self):
        before = sha(self.path)
        s = self.job(commit=False)
        self.assertEqual((s["esito"], s["exit_code"], s["modo"]), ("OK", 0, "dry-run"))
        self.assertEqual(sha(self.path), before)
        self.assertFalse(any(s["scritture"].values()))
        self.assertEqual({v["stato"] for f in s["fonti"].values() for v in f["kinds"].values()}, {"simulato"})
        self.assertEqual(self.records(), {})

    def test_commit_writes_verifies_and_is_idempotent(self):
        s = self.job()
        self.assertEqual((s["esito"], s["exit_code"]), ("OK", 0), json.dumps(s, indent=1))
        self.assertTrue(s["scritture"]["registro"])
        for f in s["fonti"].values():
            self.assertTrue(f["verifica_post_scrittura"])
            self.assertEqual({v["stato"] for v in f["kinds"].values()}, {"scritto"})
        n = len(self.records())
        self.assertGreater(n, 10)
        before = self.records()
        s2 = self.job()
        self.assertEqual(s2["esito"], "OK")
        for f in s2["fonti"].values():
            for v in f["kinds"].values():
                self.assertEqual(set(v["conteggi"]) - {"unchanged"}, set(), v)        # nessuna creazione/modifica
        self.assertEqual(self.records(), before)

    def test_each_kind_is_read_once_per_run(self):
        self.job()
        self.assertEqual(sorted(self.calls["cv"]), sorted(CV_KINDS))                    # dry-run + commit = 1 lettura
        self.assertEqual(sorted(self.calls["cls"]), sorted(CLS_KINDS))


class ErroriEFonti(Base):
    def test_temporary_failure_is_partial_and_marks_nothing_missing(self):
        self.job()
        self.cv_fail["absence"] = OSError("Rete non raggiungibile: timed out")           # fallisce a ogni tentativo
        s = self.job()
        self.assertEqual((s["esito"], s["exit_code"]), ("PARZIALE", 1))
        k = s["fonti"]["classeviva"]["kinds"]["absence"]
        self.assertEqual((k["stato"], k["categoria"], k["tentativi"]), ("errore", "temporaneo", 3))
        self.assertEqual(self.clock.slept, [30, 120])
        self.assertEqual(set(self.records(kind="absence").values()), {R.PRESENT})       # nessuna assenza "scomparsa"
        reg = R.Registry.from_snapshot(self.path)
        try:
            fr = reg.freshness(NOW.isoformat())["per_kind"]["classeviva/absence"]
        finally:
            reg.close()
        self.assertIsNotNone(fr["ultimo_errore"])                                        # errore registrato come run
        self.assertEqual(s["fonti"]["classroom"]["esito"], "ok")

    def test_transient_failure_recovers_with_retry(self):
        self.cv_fail["notice"] = [OSError("timed out")]
        s = self.job()
        self.assertEqual(s["esito"], "OK")
        self.assertEqual(s["fonti"]["classeviva"]["kinds"]["notice"]["tentativi"], 2)

    def test_classroom_auth_expired_needs_user_and_classeviva_still_written(self):
        self.job()
        self.pre_fail = RuntimeError("AuthError: rinnovo token non riuscito: invalid_grant (Token has been expired or revoked.)")
        s = self.job()
        self.assertEqual((s["esito"], s["exit_code"]), ("AUTORIZZAZIONE", 3))
        cls = s["fonti"]["classroom"]
        self.assertEqual((cls["autorizzazione"]["categoria"], cls["esito"]), ("autorizzazione", "fallita"))
        self.assertEqual(self.calls["pre"], 2)                                          # un solo tentativo: non si ritenta
        self.assertEqual(len(self.calls["cls"]), len(CLS_KINDS))                        # solo il primo job ha letto Classroom
        self.assertEqual(s["fonti"]["classeviva"]["esito"], "ok")
        self.assertEqual(set(self.records(source="classroom").values()), {R.PRESENT})
        self.assertIn("autorizzazione:classroom", {k for k in J.load_state(self.state)["problemi"]})

    def test_sudden_drop_refused_by_registry_is_reported(self):
        self.cv["notice"] = many_notices(9)
        self.job()
        self.cv["notice"] = many_notices(2)                                             # calo sotto meta': il Registry rifiuta
        s = self.job()
        self.assertEqual(set(self.records(kind="notice").values()), {R.PRESENT})
        self.assertTrue(any(a["chiave"] == "scomparse_rifiutate:classeviva/notice" and a["gravita"] == "controllo"
                            for a in s["anomalie"]))

    def test_missing_above_threshold_is_held_not_written(self):
        self.cv["notice"] = many_notices(20)
        self.job()
        self.cv["notice"] = many_notices(13)                                            # 7 scomparse > soglia 5
        s = self.job()
        k = s["fonti"]["classeviva"]["kinds"]["notice"]
        self.assertEqual(k["stato"], "trattenuto")
        self.assertEqual(s["esito"], "PARZIALE")
        self.assertEqual(set(self.records(kind="notice").values()), {R.PRESENT})
        self.assertTrue(any(a["chiave"] == "scomparse_sopra_soglia:classeviva/notice" for a in s["anomalie"]))
        s = self.job(policy={"max_missing": 10})                                        # soglia alzata a mano: scritto
        self.assertEqual(s["fonti"]["classeviva"]["kinds"]["notice"]["stato"], "scritto")
        self.assertEqual(list(self.records(kind="notice").values()).count(R.MISSING), 7)

    def test_ambiguous_absences_are_reported_for_human_check(self):
        self.cv["absence"] = [ab(130, "2026-10-03")]
        self.job()
        self.cv["absence"] = [justified(ab(130, "2026-10-03"), 230), justified(ab(130, "2026-10-03"), 231)]
        s = self.job()
        self.assertEqual(s["fonti"]["classeviva"]["kinds"]["absence"]["stato"], "scritto")
        a = [x for x in s["anomalie"] if x["chiave"].startswith("assenze_ambigue:")]
        self.assertEqual((len(a), a[0]["gravita"]), (1, "controllo"))
        self.assertEqual(self.records(kind="absence"), {"130": R.PRESENT})

    def test_registry_absent_is_never_created(self):
        os.remove(self.path)
        s = self.job()
        self.assertEqual((s["esito"], s["exit_code"]), ("FALLITO", 2))
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.calls["cv"] + self.calls["cls"], [])

    def test_concurrent_job_is_refused(self):
        with S._Lock(os.path.join(self.root, "school-job.lock")):
            before = sha(self.path)
            s = self.job()
        self.assertEqual((s["esito"], s["exit_code"]), ("OCCUPATO", 4))
        self.assertEqual((sha(self.path), self.calls["cv"]), (before, []))

    def test_unreadable_keychain_is_not_reported_as_expired_authorization(self):
        self.pre_fail = RuntimeError("AuthError: Classroom non autorizzato: esegui `python3 cls_auth.py authorize`")
        s = self.job()
        self.assertEqual((s["esito"], s["fonti"]["classroom"]["autorizzazione"]["categoria"]), ("AUTORIZZAZIONE", "keychain"))
        titles = [t for t, _ in self.sent]
        self.assertIn("Cloud Scuola: credenziali classroom non leggibili", titles)
        self.assertNotIn("Cloud Scuola: autorizzazione classroom", titles)
        self.assertIn("Non riautorizzare", dict(self.sent)["Cloud Scuola: credenziali classroom non leggibili"])

    def test_source_construction_error_is_isolated(self):
        d = self.deps()
        d.sources["classeviva"] = lambda: (_ for _ in ()).throw(RuntimeError("CVError: Credenziali non trovate nel Keychain"))
        s = J.run_job(commit=True, db_path=self.path, state_dir=self.state, deps=d, kinds=KINDS)
        self.assertEqual(s["esito"], "AUTORIZZAZIONE")
        self.assertEqual(s["fonti"]["classroom"]["esito"], "ok")


class Anomalie(unittest.TestCase):
    def test_same_key_is_reported_once(self):
        a = J._anomaly("titoli_non_determinabili:classroom/material", "info", "classroom", "material", "uno")
        b = J._anomaly("titoli_non_determinabili:classroom/material", "info", "classroom", "material", "due")
        out = J._dedupe([a, b, dict(a)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["testo"], "uno | due")


class NotificheEStato(Base):
    def test_no_notification_when_all_ok(self):
        self.job()
        self.assertEqual(self.sent, [])

    def test_problem_notified_once_renotified_daily_and_resolution_once(self):
        self.job()
        self.pre_fail = RuntimeError("AuthError: rinnovo token non riuscito: invalid_grant")
        auth = lambda: [t for t, _ in self.sent if t == "Cloud Scuola: autorizzazione classroom"]   # noqa: E731
        self.job()
        self.assertEqual(len(auth()), 1)
        # i dati Classroom hanno l'ultimo run in errore ma sono entro la soglia: nessuna seconda notifica «dati non
        # aggiornati» per la stessa causa (la copre l'autorizzazione); i dati davvero vecchi: test_stale_data_notified
        self.assertEqual([t for t, _ in self.sent].count("Cloud Scuola: dati non aggiornati"), 0)
        n = len(self.sent)
        self.now = NOW + dt.timedelta(hours=3)
        self.job()
        self.assertEqual(len(self.sent), n)                                             # stessi problemi: nessuna ripetizione
        self.now = NOW + dt.timedelta(hours=25)
        self.job()
        self.assertEqual(len(auth()), 2)                                                # ancora aperto dopo un giorno
        self.pre_fail = None
        self.job()
        self.assertEqual(self.sent[-1][0], "Cloud Scuola: tutto regolare")
        n = len(self.sent)
        self.job()
        self.assertEqual(len(self.sent), n)
        self.assertEqual(J.load_state(self.state)["problemi"], {})

    def test_stale_data_notified(self):
        self.job()
        # i run del Registry usano l'orologio REALE: "3 giorni dopo" va calcolato da adesso, non dalla data fissa NOW
        # (con NOW fisso il test dipendeva dal giorno in cui girava)
        self.now = dt.datetime.now(dt.timezone.utc).astimezone() + dt.timedelta(days=3)
        s = self.job(commit=False)                                                      # un dry-run non aggiorna
        self.assertTrue(s["freschezza"]["oltre_soglia"])
        self.assertIn("Cloud Scuola: dati non aggiornati", [t for t, _ in self.sent])

    def test_notification_failure_does_not_fail_job(self):
        d = self.deps()

        def boom(t, m):
            raise OSError("osascript assente")
        d.notify_send = boom
        self.pre_fail = RuntimeError("AuthError: invalid_grant")
        s = J.run_job(commit=True, db_path=self.path, state_dir=self.state, deps=d, kinds=KINDS)
        self.assertEqual(s["esito"], "AUTORIZZAZIONE")
        self.assertFalse(s["notifiche"][0]["inviata"])

    def test_state_and_summary_files_are_private(self):
        self.job()
        for f in ("state.json", "last_summary.json"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, f)).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)


class StatoNotificheSoloSeInviate(Base):
    """'ultima_notifica' si aggiorna SOLO dopo un invio riuscito (difetto osservato il 2026-10-21: un dry-run senza --notify
    segnava i problemi come gia' notificati e un giro con --notify nelle 24 ore successive non li avrebbe segnalati)."""
    P = {"autorizzazione:classroom": ("Cloud Scuola: autorizzazione classroom", "Accesso non valido.")}

    def upd(self, state, now, send, problems=None):
        return J.update_notifications(state, self.P if problems is None else problems, now, send, 24)

    def last(self, state):
        return state["problemi"]["autorizzazione:classroom"]["ultima_notifica"]

    def test_without_notify_problem_is_not_marked_as_notified(self):
        st = {}
        out = self.upd(st, NOW, None)
        self.assertEqual([n["inviata"] for n in out], [None])                    # calcolata, non inviata
        self.assertIsNone(self.last(st))
        self.assertEqual(st["problemi"]["autorizzazione:classroom"]["dal"], NOW.isoformat())   # il problema resta tracciato
        sent = []
        out = self.upd(st, NOW + dt.timedelta(hours=1), lambda t, m: sent.append(t))
        self.assertEqual((sent, [n["inviata"] for n in out]), (["Cloud Scuola: autorizzazione classroom"], [True]))

    def test_successful_send_is_recorded(self):
        st = {}
        self.upd(st, NOW, lambda t, m: None)
        self.assertEqual(self.last(st), NOW.isoformat())

    def test_failed_send_is_not_recorded_and_retried_next_run(self):
        st = {}

        def boom(t, m):
            raise OSError("osascript non disponibile")
        out = self.upd(st, NOW, boom)
        self.assertEqual([n["inviata"] for n in out], [False])
        self.assertIsNone(self.last(st))
        sent = []
        self.upd(st, NOW + dt.timedelta(minutes=10), lambda t, m: sent.append(t))
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.last(st), (NOW + dt.timedelta(minutes=10)).isoformat())

    def test_dedup_after_successful_send_within_window(self):
        st, sent = {}, []
        send = lambda t, m: sent.append(t)       # noqa: E731
        self.upd(st, NOW, send)
        for h in (1, 12, 23):
            self.upd(st, NOW + dt.timedelta(hours=h), send)
            self.upd(st, NOW + dt.timedelta(hours=h), None)                      # anche i giri senza --notify
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.last(st), NOW.isoformat())
        self.upd(st, NOW + dt.timedelta(hours=24), send)                         # finestra scaduta: di nuovo
        self.assertEqual(len(sent), 2)

    def test_resolution_only_for_problems_actually_notified(self):
        st, sent = {}, []
        send = lambda t, m: sent.append(t)       # noqa: E731
        self.upd(st, NOW, None)                                                  # mai notificato
        self.upd(st, NOW + dt.timedelta(hours=1), send, problems={})
        self.assertEqual(sent, [])                                               # nessun "tutto regolare" a vuoto
        self.upd(st, NOW + dt.timedelta(hours=2), send)
        self.upd(st, NOW + dt.timedelta(hours=3), send, problems={})
        self.assertEqual(sent, ["Cloud Scuola: autorizzazione classroom", "Cloud Scuola: tutto regolare"])

    def test_job_dry_run_without_notify_then_real_notification(self):
        self.job()
        self.pre_fail = RuntimeError("AuthError: rinnovo token non riuscito: invalid_grant")
        d = self.deps()
        d.notify_send = None                                                     # come `run` senza --notify
        s = J.run_job(commit=False, db_path=self.path, state_dir=self.state, deps=d, kinds=KINDS)
        self.assertEqual(s["esito"], "AUTORIZZAZIONE")
        self.assertTrue(all(n["inviata"] is None for n in s["notifiche"]))
        self.assertIsNone(J.load_state(self.state)["problemi"]["autorizzazione:classroom"]["ultima_notifica"])
        self.now = NOW + dt.timedelta(hours=1)
        self.job(commit=False)                                                   # stesso problema, ora con invio
        self.assertIn("Cloud Scuola: autorizzazione classroom", [t for t, _ in self.sent])


class IdentitaNelRiepilogo(Base):
    """Chi approva una scrittura vede QUALI record cambiano (update/missing/...), non solo quanti; le creazioni restano conteggi."""
    def test_updated_and_missing_uids_are_listed_in_dry_run_and_commit(self):
        self.cv["notice"] = many_notices(20)
        self.job()
        changed = many_notices(20)
        changed[0]["cntTitle"] = "Circolare modificata (SINT.)"
        self.cv["notice"] = changed[:-1]                                               # 1 aggiornata + 1 scomparsa
        for commit in (False, True):
            s = self.job(commit=commit)
            k = s["fonti"]["classeviva"]["kinds"]["notice"]
            self.assertEqual(k["conteggi"].get("update"), 1, k)
            self.assertEqual(k["conteggi"].get("missing"), 1, k)
            self.assertEqual(len(k["uid"]["update"]), 1)
            self.assertEqual(len(k["uid"]["missing"]), 1)
            self.assertNotIn("create", k["uid"])
            self.assertTrue(all(u.startswith("cvv:") for us in k["uid"].values() for u in us), k["uid"])
            self.assertIn("update: cvv:", J.render(s))

    def test_only_creations_have_no_uid_section(self):
        s = self.job(commit=False)
        self.assertFalse(any("uid" in v for f in s["fonti"].values() for v in f["kinds"].values()))


class NessunaDipendenzaNotion(Base):
    def test_summary_log_and_state_have_no_notion(self):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "state.json"), "w") as f:
            json.dump({"notion": {"impronta": "x", "riferimento": {"hub": "y"}}}, f)       # stato storico di prima
        s = self.job()
        self.assertNotIn("notion", s)
        self.assertNotIn("notion", s["scritture"])
        self.assertNotIn("Notion", J.render(s))
        with open(os.path.join(self.state, "state.json")) as f:
            self.assertNotIn("notion", json.load(f))                                    # stato storico eliminato
        self.assertFalse(hasattr(J, "notion_verified"))

    def test_cli_has_no_notion_command(self):
        self.assertEqual(J.main(["notion-verified"]), J.EXIT_USAGE)

    def test_server_and_job_import_no_notion_module(self):
        """Processo pulito: server MCP e job si caricano senza moduli Notion, e quei moduli non esistono piu'."""
        import subprocess
        code = ("import sys; sys.path[:0] = [%r, %r]; import school_job, server; "
                "print(sorted(m for m in sys.modules if 'notion' in m.lower()))" % (ROOT, os.path.join(ROOT, "..", "_common")))
        r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=120,
                           env={"PATH": os.environ.get("PATH", ""), "HOME": self.tmp.name})
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertEqual(r.stdout.strip(), "[]")
        self.assertFalse([f for f in os.listdir(ROOT) if f.startswith("notion_")])


class LogECli(Base):
    def test_json_log_rotating_and_without_secrets(self):
        log_path = os.path.join(self.tmp.name, "logs", "school-job.jsonl")
        log = J.JsonLog(log_path)
        tok = "SINTETICO-" + "B" * 60
        self.cv_fail["grade"] = OSError("timed out con %s" % tok)
        J.run_job(commit=True, db_path=self.path, state_dir=self.state, deps=self.deps(), kinds=KINDS, log=log)
        log.close()
        text = open(log_path).read()
        self.assertNotIn(tok, text)
        self.assertNotIn(tok, open(os.path.join(self.state, "last_summary.json")).read())
        evs = [json.loads(line)["evento"] for line in text.splitlines()]
        self.assertEqual((evs[0], evs[-1]), ("avvio", "fine"))
        self.assertIn("retry", evs)
        self.assertIsInstance(log.handler, J.logging.handlers.RotatingFileHandler)

    def test_cli_exit_codes_and_usage(self):
        argv = ["run", "--db", self.path, "--state-dir", self.state, "--log-file", os.path.join(self.tmp.name, "l.jsonl")]
        with mock.patch.object(J, "SOURCES", {"classeviva": (J.CR, tuple(CV_KINDS)), "classroom": (J.CL, tuple(CLS_KINDS))}), \
                mock.patch("sys.stdout"):
            self.assertEqual(J.main(argv, deps=self.deps()), 0)
            self.pre_fail = RuntimeError("AuthError: invalid_grant")
            self.assertEqual(J.main(argv + ["--commit"], deps=self.deps()), 3)
            self.assertEqual(J.main(["run", "--sources", "nessuna"], deps=self.deps()), J.EXIT_USAGE)
            self.assertEqual(J.main(["boh"]), J.EXIT_USAGE)
            self.assertEqual(J.main(["status", "--state-dir", self.state]), 0)
        # il primo run (senza --commit) non ha scritto nulla; il secondo ha scritto ClasseViva e non Classroom (autorizzazione)
        self.assertTrue(self.records(source="classeviva"))
        self.assertEqual(self.records(source="classroom"), {})


class EquivalenzaServer(unittest.TestCase):
    def test_classeviva_reads_match_server_registry_fetchers(self):
        import server as SV

        class Fake:
            def __init__(self):
                self.calls = []

            def __getattr__(self, name):
                def m(*a, **k):
                    self.calls.append((name, a, tuple(sorted(k.items()))))
                    return []
                return m
        today = dt.date.today()
        a, b = Fake(), Fake()
        with mock.patch.object(SV, "client", lambda: a):
            srv = SV._registry_fetchers()
        job = J.classeviva_fetchers(today=today, factory=lambda: b)
        self.assertEqual(set(srv), set(job))
        self.assertEqual(set(job), set(J.SOURCES["classeviva"][1]))
        for k in srv:
            self.assertEqual(srv[k][1], job[k][1], k)
            srv[k][0]()
            job[k][0]()
        self.assertEqual(a.calls, b.calls)


class LaunchdInattivo(unittest.TestCase):
    """Il modello launchd si genera e valida in una HOME temporanea; nulla viene caricato (install senza --conferma esce)."""
    SH = os.path.join(ROOT, "launchd", "school_job_launchd.sh")

    def run_sh(self, *args):
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = dict(os.environ, HOME=tmp.name)
        return subprocess.run([self.SH] + list(args), capture_output=True, text=True, env=env, timeout=30), tmp.name

    def test_render_is_valid_plist_with_real_paths_and_no_secrets(self):
        import plistlib
        r, home = self.run_sh("render")
        self.assertEqual(r.returncode, 0, r.stderr)
        p = plistlib.loads(r.stdout.encode())
        self.assertEqual(p["Label"], "cloud.scuola.job")
        self.assertEqual(p["ProgramArguments"][1:], [os.path.join(ROOT, "school_job.py"), "check"])
        self.assertTrue(all(os.path.isabs(x) for x in p["ProgramArguments"][:2]))
        self.assertEqual((p["StartInterval"], "StartCalendarInterval" in p), (300, False))
        self.assertTrue(p["RunAtLoad"])
        self.assertEqual(p["EnvironmentVariables"]["PYTHONIOENCODING"], "utf-8")
        self.assertTrue(p["StandardErrorPath"].startswith(home))
        for bad in ("@PYTHON@", "@JOB@", "@HOME@"):
            self.assertNotIn(bad, r.stdout)
        low = r.stdout.lower()
        self.assertFalse(any(w in low for w in ("password", "secret", "refresh_token", "api_key")))

    def test_install_without_confirmation_does_nothing(self):
        r, home = self.run_sh("install")
        self.assertEqual(r.returncode, 64)
        self.assertEqual(os.listdir(home), [])


if __name__ == "__main__":
    unittest.main()
