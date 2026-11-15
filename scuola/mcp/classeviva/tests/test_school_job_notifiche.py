"""Job scuola: notifiche proporzionate a gravita' e persistenza, giri mancati, stop del Mac, stato illeggibile (2026-10-25).
Solo dati, log e calendari SINTETICI in cartelle temporanee: nessuna rete, nessun file reale (registro, log, plist)."""
import datetime as dt, glob, json, os, plistlib, sys, unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_school_job import Base, NOW, _StopClock, CLS_KINDS, CV_KINDS, KINDS, J, S  # noqa: E402

TEMP = OSError("The read operation timed out")
P_REP = "Cloud Scuola: Classroom non aggiornato da piu' giri"


def titles(sent):
    return [t for t, _ in sent]


class ParzialiEFalliti(Base):
    def test_parziale_temporaneo_notificato_solo_se_si_ripete_poi_risolto(self):
        self.assertEqual(self.job()["esito"], "OK")
        self.cls_fail = {"material": TEMP}
        s = self.job()
        self.assertEqual((s["esito"], s["exit_code"]), ("PARZIALE", 1))                # mai OK con un tipo non aggiornato
        self.assertEqual(self.sent, [])                                                  # 1o giro: in osservazione
        self.assertEqual(s["problemi_in_osservazione"], ["temporaneo:classroom"])
        st = J.load_state(self.state)["problemi"]["temporaneo:classroom"]
        self.assertEqual((st["giri"], st["attesa_conferma"], st["ultima_notifica"]), (1, 2, None))
        self.cls_fail = {"material": TEMP}
        self.now = NOW + dt.timedelta(hours=6)
        self.job()
        self.assertEqual(titles(self.sent), [P_REP])                                     # 2o giro consecutivo: notificato
        self.cls_fail = {"material": TEMP}
        self.now = NOW + dt.timedelta(hours=12)
        self.job()
        self.assertEqual(titles(self.sent), [P_REP])                                     # nessuna ripetizione entro 24 h
        self.cls_fail = {}
        self.now = NOW + dt.timedelta(hours=18)
        self.job()
        self.assertEqual(titles(self.sent), [P_REP, "Cloud Scuola: tutto regolare"])
        self.assertEqual(J.load_state(self.state)["problemi"], {})

    def test_parziale_temporaneo_isolato_si_chiude_in_silenzio(self):
        self.job()
        self.cls_fail = {"material": TEMP}
        self.job()
        self.cls_fail = {}
        self.now = NOW + dt.timedelta(hours=6)
        s = self.job()
        self.assertEqual((s["esito"], self.sent), ("OK", []))                           # mai notificato: nessun «risolto»
        self.assertEqual(J.load_state(self.state)["problemi"], {})

    def test_parziale_non_temporaneo_notificato_subito(self):
        self.job()
        self.cv_fail = {"grade": RuntimeError("Risposta inattesa da ClasseViva: chiave 'grades' assente")}
        s = self.job()
        self.assertEqual(s["esito"], "PARZIALE")
        self.assertEqual(titles(self.sent), ["Cloud Scuola: sincronizzazione parziale"])
        self.assertIn("parziale:classeviva:grade", J.load_state(self.state)["problemi"])

    def test_fallito_per_soli_errori_temporanei_poi_ripetuto(self):
        self.job()
        self.cv_fail = {k: TEMP for k in CV_KINDS}
        self.cls_fail = {k: TEMP for k in CLS_KINDS}
        s = self.job()
        self.assertEqual((s["esito"], s["exit_code"]), ("FALLITO", 2))
        self.assertEqual(self.sent, [])
        self.now = NOW + dt.timedelta(hours=6)
        self.job()
        # due fonti, stessa causa: una sola notifica
        self.assertEqual(titles(self.sent), ["Cloud Scuola: ClasseViva e Classroom non aggiornati da piu' giri"])

    def test_fallito_non_temporaneo_notificato_subito(self):
        os.remove(self.path)                                                             # registro assente: strutturale
        s = self.job()
        self.assertEqual(s["esito"], "FALLITO")
        self.assertEqual(titles(self.sent), ["Cloud Scuola: sincronizzazione fallita"])

    def test_autorizzazione_resta_immediata(self):
        self.job()
        self.pre_fail = RuntimeError("AuthError: invalid_grant")
        self.job()
        self.assertEqual(titles(self.sent), ["Cloud Scuola: autorizzazione classroom"])


TMP = {"stato": "errore", "categoria": "temporaneo", "motivo": "scadenza_tempo_reale"}
CLS6 = ("announcement", "course", "course_work", "material", "submission", "topic")
CV5 = ("absence", "agenda_event", "grade", "lesson", "notice")
CET = dt.timezone(dt.timedelta(hours=1))


def summary(esito, cls=None, cv=None):
    """Riepilogo minimo: tipi indicati in errore temporaneo (o con la categoria data), gli altri scritti."""
    def kinds(all_, bad):
        out = {k: {"stato": "scritto"} for k in all_}
        for k, cat in (bad or {}).items():
            out[k] = dict(TMP, categoria=cat)
        return out
    return {"esito": esito, "fonti": {"classroom": {"kinds": kinds(CLS6, cls)}, "classeviva": {"kinds": kinds(CV5, cv)}},
            "freschezza": {"soglia_ore": 48, "oltre_soglia": []}, "anomalie": []}


class SequenzeReali(unittest.TestCase):
    """Regressione del 25/10: parziale -> fallito -> parziale di Classroom senza notifica e «problema risolto» falso alle 06:45."""

    def step(self, st, s, when, send=True):
        sent = []
        J.update_notifications(st, J.derive_problems(s), when, (lambda t, m: sent.append((t, m))) if send else None, 24,
                               failing=J.failing_sources(s))
        return [t for t, _ in sent], sent

    def test_parziale_fallito_parziale_conserva_il_conteggio_e_notifica_alla_soglia(self):
        st, t0 = {}, dt.datetime(2026, 10, 24, 21, 42, tzinfo=CET)
        a, _ = self.step(st, summary("PARZIALE", cls={"material": "temporaneo"}), t0)
        self.assertEqual((a, st["problemi"]["temporaneo:classroom"]["giri"]), ([], 1))       # episodio singolo: niente
        b, _ = self.step(st, summary("FALLITO", cls={k: "temporaneo" for k in CLS6}, cv={k: "temporaneo" for k in CV5}),
                         t0 + dt.timedelta(hours=11))
        self.assertEqual(b, ["Cloud Scuola: Classroom non aggiornato da piu' giri"])        # 2o giro: soglia raggiunta
        self.assertEqual(st["problemi"]["temporaneo:classroom"]["giri"], 2)                  # conteggio sopravvissuto
        self.assertEqual(st["problemi"]["temporaneo:classeviva"]["giri"], 1)                 # ClasseViva: solo 1 giro
        c, _ = self.step(st, summary("PARZIALE", cls={k: "temporaneo" for k in CLS6 if k != "course"}),
                         t0 + dt.timedelta(hours=18))
        self.assertEqual(c, [])                                                              # entro 24 h: nessuna ripetizione
        self.assertEqual((st["problemi"]["temporaneo:classroom"]["giri"], "temporaneo:classeviva" in st["problemi"]), (3, False))
        self.assertNotIn("risolti_in_attesa", st)                                            # ClasseViva mai notificata
        d, _ = self.step(st, summary("OK"), t0 + dt.timedelta(hours=22))
        self.assertEqual(d, ["Cloud Scuola: tutto regolare"])                                # risoluzione autentica
        self.assertEqual(self.step(st, summary("OK"), t0 + dt.timedelta(hours=26))[0], [])  # una sola volta
        self.assertEqual(st["problemi"], {})

    def test_stato_reale_del_24_10_nessun_risolto_falso(self):
        """Stato persistito reale (chiavi vecchie notificate alle 21:42) e i giri reali del 25/10."""
        n = dt.datetime(2026, 10, 24, 21, 42, tzinfo=dt.timezone(dt.timedelta(hours=2))).isoformat()
        st = {"problemi": {"freschezza": {"dal": n, "ultima_notifica": n, "titolo": "Cloud Scuola: dati non aggiornati"},
                           "parziale:classroom:announcement,course_work,material,submission,topic":
                               {"dal": n, "ultima_notifica": n, "titolo": "Cloud Scuola: sincronizzazione parziale"}}}
        a, _ = self.step(st, summary("FALLITO", cls={k: "temporaneo" for k in CLS6}, cv={k: "temporaneo" for k in CV5}),
                         dt.datetime(2026, 10, 25, 8, 52, tzinfo=CET))
        self.assertEqual(a, [])                                                              # prima: «problema risolto»
        p = st["problemi"]
        self.assertEqual(sorted(p), ["temporaneo:classeviva", "temporaneo:classroom"])
        self.assertEqual((p["temporaneo:classroom"]["giri"], p["temporaneo:classroom"]["ultima_notifica"], p["temporaneo:classroom"]["dal"]),
                         (2, n, n))                                                          # storia ereditata
        self.assertEqual(st["risolti_in_attesa"], {"freschezza": "Cloud Scuola: dati non aggiornati"})   # chiusura rinviata
        b, _ = self.step(st, summary("PARZIALE", cls={k: "temporaneo" for k in CLS6 if k != "course"}),
                         dt.datetime(2026, 10, 25, 15, 54, tzinfo=CET))
        self.assertEqual(b, [])                                                              # notificato < 24 h fa
        self.assertEqual(st["problemi"]["temporaneo:classroom"]["giri"], 3)
        c, sent = self.step(st, summary("OK"), dt.datetime(2026, 10, 25, 20, 2, tzinfo=CET))
        self.assertEqual(c, ["Cloud Scuola: tutto regolare"])
        self.assertIn("dati non aggiornati", sent[0][1])
        self.assertIn("Classroom non aggiornato", sent[0][1])
        self.assertNotIn("risolti_in_attesa", st)

    def test_stato_reale_attuale_parziale_temporaneo_in_osservazione(self):
        """Stato reale dopo il giro delle 14:30 del 25/10: la chiave `parziale_temporaneo:classroom` (1 giro) prosegue."""
        st = {"problemi": {"parziale_temporaneo:classroom": {"dal": "2026-10-25T15:54:34+01:00", "ultima_notifica": None,
                                                             "titolo": "Cloud Scuola: sincronizzazione parziale ripetuta",
                                                             "giri": 1, "attesa_conferma": 2}}}
        a, _ = self.step(dict(st, problemi=dict(st["problemi"])), summary("OK"), dt.datetime(2026, 10, 25, 20, 2, tzinfo=CET))
        self.assertEqual(a, [])                                                              # mai notificato: chiusura muta
        a, _ = self.step(st, summary("PARZIALE", cls={"material": "temporaneo"}), dt.datetime(2026, 10, 25, 20, 2, tzinfo=CET))
        self.assertEqual(a, ["Cloud Scuola: Classroom non aggiornato da piu' giri"])        # 2o giro: notificato
        self.assertEqual((list(st["problemi"]), st["problemi"]["temporaneo:classroom"]["dal"]),
                         (["temporaneo:classroom"], "2026-10-25T15:54:34+01:00"))

    def test_cambio_di_categoria_con_errori_ancora_presenti(self):
        st, t0 = {}, dt.datetime(2026, 10, 25, 8, 0, tzinfo=CET)
        for h in (0, 6):
            self.step(st, summary("PARZIALE", cls={"material": "temporaneo", "topic": "temporaneo"}), t0 + dt.timedelta(hours=h))
        self.assertTrue(st["problemi"]["temporaneo:classroom"]["ultima_notifica"])          # notificato al 2o giro
        a, sent = self.step(st, summary("PARZIALE", cls={"material": "dati", "topic": "dati"}), t0 + dt.timedelta(hours=12))
        self.assertEqual(a, ["Cloud Scuola: sincronizzazione parziale"])                    # nuova categoria: subito
        self.assertNotIn("Cloud Scuola: problema risolto", a)                                # Classroom ancora in errore
        self.assertIn("temporaneo:classroom", st["risolti_in_attesa"])
        b, sent = self.step(st, summary("OK"), t0 + dt.timedelta(hours=18))
        self.assertEqual(b, ["Cloud Scuola: tutto regolare"])
        self.assertIn("Classroom non aggiornato", sent[0][1])
        self.assertIn("sincronizzazione parziale", sent[0][1])

    def test_piu_tipi_e_piu_problemi_insieme(self):
        st, t0 = {}, dt.datetime(2026, 10, 25, 8, 0, tzinfo=CET)
        s = summary("PARZIALE", cls={"material": "temporaneo", "topic": "temporaneo", "course_work": "temporaneo"},
                    cv={"grade": "dati"})
        a, sent = self.step(st, s, t0)
        self.assertEqual(a, ["Cloud Scuola: sincronizzazione parziale"])                    # ClasseViva (dati): subito
        self.assertEqual(sorted(st["problemi"]), ["parziale:classeviva:grade", "temporaneo:classroom"])
        b, sent = self.step(st, s, t0 + dt.timedelta(hours=6))
        self.assertEqual(b, ["Cloud Scuola: Classroom non aggiornato da piu' giri"])
        self.assertIn("course_work, material, topic", sent[0][1])                            # un solo avviso per i tre tipi
        c, _ = self.step(st, summary("PARZIALE", cls={"material": "temporaneo"}), t0 + dt.timedelta(hours=12))
        self.assertEqual(c, [])                                                              # ClasseViva ok ma Classroom no:
        self.assertIn("parziale:classeviva:grade", st["risolti_in_attesa"])                 # nessun «tutto regolare»...
        d, sent = self.step(st, summary("OK"), t0 + dt.timedelta(hours=18))
        self.assertEqual(d, ["Cloud Scuola: tutto regolare"])                                # ...finche' tutto torna a posto

    def test_nessun_risolto_per_una_fonte_ancora_in_errore(self):
        """Resta solo un problema in osservazione (mai notificato): la chiusura di un problema notificato della STESSA fonte,
        o di uno globale, aspetta che la fonte (o ogni fonte) non abbia piu' errori."""
        n = "2026-10-25T07:00:00+01:00"
        for old, fonte_errore in (("autorizzazione:classroom", "classroom"), ("freschezza", "classeviva")):
            with self.subTest(old):
                st = {"problemi": {old: {"dal": n, "ultima_notifica": n, "titolo": "Cloud Scuola: titolo vecchio"}}}
                bad = {"material": "temporaneo"} if fonte_errore == "classroom" else None
                s = summary("PARZIALE", cls=bad, cv=None if bad else {"grade": "temporaneo"})
                a, _ = self.step(st, s, dt.datetime(2026, 10, 25, 9, 0, tzinfo=CET))
                self.assertEqual(a, [])                                                      # prima: «problema risolto»
                self.assertEqual(list(st["risolti_in_attesa"]), [old])
                b, sent = self.step(st, summary("OK"), dt.datetime(2026, 10, 25, 15, 0, tzinfo=CET))
                self.assertEqual(b, ["Cloud Scuola: tutto regolare"])
                self.assertIn("titolo vecchio", sent[0][1])
        st = {"problemi": {"autorizzazione:classroom": {"dal": n, "ultima_notifica": n, "titolo": "Cloud Scuola: titolo vecchio"}}}
        a, sent = self.step(st, summary("PARZIALE", cv={"grade": "temporaneo"}), dt.datetime(2026, 10, 25, 9, 0, tzinfo=CET))
        self.assertEqual(a, ["Cloud Scuola: problema risolto"])                              # altra fonte: Classroom e' a posto
        self.assertIn("In osservazione", sent[0][1])

    def test_chiusura_conservata_senza_notify_o_se_l_invio_fallisce(self):
        st, t0 = {}, dt.datetime(2026, 10, 25, 8, 0, tzinfo=CET)
        self.step(st, summary("PARZIALE", cv={"grade": "dati"}), t0)
        self.step(st, summary("OK"), t0 + dt.timedelta(hours=6), send=False)                # dry-run: calcolata, non inviata
        self.assertIn("parziale:classeviva:grade", st["risolti_in_attesa"])
        boom = []

        def fail(t, m):
            boom.append(t)
            raise OSError("osascript assente")
        J.update_notifications(st, {}, t0 + dt.timedelta(hours=7), fail, 24, failing=set())
        self.assertEqual(boom, ["Cloud Scuola: tutto regolare"])
        self.assertIn("parziale:classeviva:grade", st["risolti_in_attesa"])                 # invio fallito: resta
        self.assertEqual(self.step(st, summary("OK"), t0 + dt.timedelta(hours=8))[0], ["Cloud Scuola: tutto regolare"])
        self.assertNotIn("risolti_in_attesa", st)

    def test_migrazione_delle_vecchie_chiavi(self):
        prev = {"parziale_temporaneo:classroom": {"dal": "2026-10-25T08:00:00+01:00", "giri": 1, "ultima_notifica": None},
                "parziale:classroom:material": {"dal": "2026-10-24T20:00:00+02:00", "ultima_notifica": "2026-10-24T21:42:00+02:00"},
                "fallito_temporaneo": {"dal": "2026-10-24T06:00:00+02:00", "giri": 2, "ultima_notifica": None},
                "parziale:classeviva:grade": {"dal": "2026-10-24T20:00:00+02:00", "ultima_notifica": None},
                "autorizzazione:classroom": {"dal": "2026-10-20T10:00:00+02:00", "ultima_notifica": None}}
        out = J.migrate_problem_keys(prev, {"temporaneo:classroom": 1, "temporaneo:classeviva": 1, "parziale:classeviva:grade": 1})
        self.assertEqual(sorted(out), ["autorizzazione:classroom", "parziale:classeviva:grade", "temporaneo:classeviva",
                                       "temporaneo:classroom"])
        self.assertEqual(out["temporaneo:classroom"], {"dal": "2026-10-24T06:00:00+02:00", "giri": 2,
                                                       "ultima_notifica": "2026-10-24T21:42:00+02:00"})
        self.assertEqual(out["temporaneo:classeviva"]["giri"], 2)                           # fallito_temporaneo: ogni fonte
        self.assertIs(out["parziale:classeviva:grade"], prev["parziale:classeviva:grade"])   # ancora prodotta: intatta
        self.assertEqual(J.migrate_problem_keys(prev, {})["fallito_temporaneo"], prev["fallito_temporaneo"])   # nessuna perdita
        self.assertEqual(J._key_source("parziale:classroom:a,b"), "classroom")
        self.assertEqual((J._key_source("fallito"), J._key_source("freschezza"), J._key_source("anomalia:x:y")), ("*", "*", "*"))


class Compatibilita(Base):
    def legacy(self, key, notified):
        self.job()                                             # giro riuscito: dati entro la soglia
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "state.json"), "w") as f:
            json.dump({"problemi": {key: {"dal": NOW.isoformat(), "titolo": "Cloud Scuola: sincronizzazione parziale",
                                          "ultima_notifica": notified}}, "ultimo": {}}, f)

    def test_vecchia_chiave_parziale_ereditata_senza_falso_risolto(self):
        self.legacy("parziale:classroom:announcement,material", (NOW - dt.timedelta(hours=9)).isoformat())
        self.cls_fail = {"material": TEMP}
        self.job()                                                                       # stessa fonte ancora in errore
        self.assertEqual(self.sent, [])                                                  # gia' notificato 9 h fa, nessun «risolto»
        st = J.load_state(self.state)["problemi"]
        self.assertEqual(list(st), ["temporaneo:classroom"])
        self.assertEqual(st["temporaneo:classroom"]["giri"], 2)
        self.cls_fail = {}
        self.now = NOW + dt.timedelta(hours=3)
        self.job()
        self.assertEqual(titles(self.sent), ["Cloud Scuola: tutto regolare"])

    def test_vecchia_chiave_risolta_mentre_un_altro_problema_e_in_osservazione(self):
        self.legacy("autorizzazione:classroom", (NOW - dt.timedelta(hours=9)).isoformat())
        self.cv_fail = {"grade": TEMP}
        self.job()
        self.assertEqual(titles(self.sent), ["Cloud Scuola: problema risolto"])
        self.assertIn("In osservazione", self.sent[0][1])

    def test_voce_problema_non_valida_e_data_illeggibile(self):
        self.job()
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "state.json"), "w") as f:
            json.dump({"problemi": {"x": "non un oggetto", "autorizzazione:classroom": {"ultima_notifica": "ieri"}}}, f)
        self.pre_fail = RuntimeError("AuthError: invalid_grant")
        self.job()
        self.assertEqual(titles(self.sent), ["Cloud Scuola: autorizzazione classroom"])  # data illeggibile: si notifica


class StatoIllegibile(Base):
    def write(self, text):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "state.json"), "w") as f:
            f.write(text)

    def test_stato_corrotto_conservato_e_dichiarato(self):
        self.write("{ non e' json")
        s = self.job()
        self.assertEqual(s["esito"], "OK")
        kept = glob.glob(os.path.join(self.state, "state.json.illeggibile-*"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(open(kept[0]).read(), "{ non e' json")                         # mai cancellato
        self.assertIn("stato_job_illeggibile", [a["chiave"] for a in s["anomalie"]])
        self.assertIsInstance(J.load_state(self.state), dict)                            # nuovo stato valido
        self.assertEqual(self.sent, [])                                                  # gravita' info: nessuna notifica

    def test_stato_non_oggetto_e_problemi_non_validi(self):
        self.write("[1, 2]")
        self.assertIn("stato_job_illeggibile", [a["chiave"] for a in self.job()["anomalie"]])
        self.write(json.dumps({"problemi": "rotto", "ultimo_commit_ok": "conservato"}))
        s = self.job()
        self.assertIn("'problemi' non valido", [a["testo"] for a in s["anomalie"]][-1])
        self.assertEqual(J.load_state(self.state)["ultimo_commit_ok"], s["fine"])        # altre voci aggiornate, non perse

    def test_occupato_non_tocca_lo_stato(self):
        self.write("{ corrotto")
        with S._Lock(os.path.join(self.root, "school-job.lock")):
            s = self.job()
        self.assertEqual(s["esito"], "OCCUPATO")
        self.assertEqual(open(os.path.join(self.state, "state.json")).read(), "{ corrotto")
        self.assertEqual(glob.glob(os.path.join(self.state, "state.json.illeggibile-*")), [])


class GiriMancati(Base):
    """Log JSONL e calendario sintetici; il job registra nello STESSO log (orologio dei test). Orari 06:45, 14:30, 20:00."""

    def setUp(self):
        super().setUp()
        self.logf = os.path.join(self.root, "school-job.jsonl")
        self.plist = os.path.join(self.root, "cloud.scuola.job.plist")
        with open(self.plist, "wb") as f:
            plistlib.dump({"Label": "cloud.scuola.job", "RunAtLoad": True,
                           "StartCalendarInterval": [{"Hour": 6, "Minute": 45}, {"Hour": 14, "Minute": 30},
                                                     {"Hour": 20, "Minute": 0}]}, f)
        self.z = NOW.tzinfo

    def at(self, day, hhmm):
        h, m = map(int, hhmm.split(":"))
        return dt.datetime(2026, 10, day, h, m, tzinfo=self.z)

    def past_run(self, start, end):
        with open(self.logf, "a") as f:
            f.write(json.dumps({"ts": start.isoformat(), "evento": "avvio", "modo": "commit"}) + "\n")
            f.write(json.dumps({"ts": end.isoformat(), "evento": "fine", "esito": "OK", "exit_code": 0, "durata_s": 60}) + "\n")

    def run_at(self, when, notify=True):
        self.now = when
        d = self.deps()
        d.schedule = (self.logf, self.plist)
        if not notify:
            d.notify_send = None

        def log(ev, **k):
            with open(self.logf, "a") as f:
                f.write(json.dumps(dict(k, ts=self.now.isoformat(), evento=ev), default=str) + "\n")
        return J.run_job(commit=True, db_path=self.path, state_dir=self.state, deps=d, kinds=KINDS, log=log)

    def test_tre_orari_consecutivi_senza_avvio_un_solo_avviso(self):
        self.past_run(self.at(18, "14:30"), self.at(18, "14:32"))
        s = self.run_at(self.at(19, "20:05"))
        au = s["automazione"]
        self.assertEqual(len(au["giri_mancati_consecutivi"]), 3)                         # 18/20:00, 19/06:45, 19/14:30
        self.assertEqual(au["questo_giro"]["orario_previsto"][11:16], "20:00")
        self.assertTrue(all("nessun avvio" in m["causa"] for m in au["orari_mancati_48h"]))
        self.assertEqual(titles(self.sent), ["Cloud Scuola: automazione ferma"])
        self.run_at(self.at(19, "20:40"))                                               # di nuovo: nessuna ripetizione
        self.assertEqual(titles(self.sent), ["Cloud Scuola: automazione ferma"])
        self.assertEqual(len(J.load_state(self.state)["eventi_notificati"]), 1)

    def test_due_orari_mancati_solo_nel_riepilogo(self):
        self.past_run(self.at(18, "20:00"), self.at(18, "20:02"))
        s = self.run_at(self.at(19, "20:05"))
        self.assertEqual(len(s["automazione"]["giri_mancati_consecutivi"]), 2)
        self.assertEqual(len(s["automazione"]["orari_mancati_48h"]), 2)
        self.assertEqual(self.sent, [])

    def test_orari_coperti_da_un_giro_ancora_in_corso_non_contano(self):
        self.past_run(self.at(18, "20:00"), self.at(19, "15:00"))                       # giro lunghissimo (stop del Mac)
        s = self.run_at(self.at(19, "20:05"))
        causes = {m["causa"] for m in s["automazione"]["orari_mancati_48h"]}
        self.assertEqual(causes, {"giro precedente ancora in corso"})
        self.assertEqual(s["automazione"]["giri_mancati_consecutivi"], [])
        self.assertEqual(self.sent, [])

    def test_avvio_in_ritardo_al_risveglio(self):
        self.past_run(self.at(19, "06:45"), self.at(19, "06:47"))
        self.past_run(self.at(19, "14:30"), self.at(19, "14:32"))
        s = self.run_at(self.at(19, "20:14"))
        q = s["automazione"]["questo_giro"]
        self.assertEqual((q["ritardo_avvio_min"], q["in_ritardo"]), (14, False))          # sotto la soglia di 15 min
        s = self.run_at(self.at(20, "07:30"))
        self.assertEqual(s["automazione"]["questo_giro"]["in_ritardo"], True)

    def test_senza_notify_l_evento_non_e_registrato(self):
        self.past_run(self.at(18, "14:30"), self.at(18, "14:32"))
        self.run_at(self.at(19, "20:05"), notify=False)
        self.assertEqual(J.load_state(self.state)["eventi_notificati"], {})
        self.run_at(self.at(19, "20:40"))                                               # la chiave e' la stessa: ora inviato
        self.assertEqual(titles(self.sent), ["Cloud Scuola: automazione ferma"])

    def test_senza_calendario_o_log_nessuna_conclusione(self):
        os.remove(self.plist)
        s = self.run_at(self.at(19, "20:05"))
        self.assertEqual(s["automazione"]["giri_mancati_consecutivi"], [])
        self.assertEqual(self.sent, [])
        s = J.run_job(commit=True, db_path=self.path, state_dir=self.state, deps=self.deps(), kinds=KINDS)
        self.assertEqual(s["automazione"]["orari"], "non controllati")                    # libreria: mai log reali


class StopDuranteIlGiro(_StopClock):
    def test_stop_misurato_e_giro_normale_senza_stop(self):
        s = self.job()
        self.assertEqual(s["automazione"]["stop_durante_il_giro_s"], 0)
        self.stop_at, self.reads = 1, []
        s = self.job()
        self.assertGreaterEqual(s["automazione"]["stop_durante_il_giro_s"], 2 * 3600 - 60)
        self.assertGreater(s["automazione"]["durata_reale_s"], s["durata_s"])


class Cli(Base):
    def test_main_collega_log_e_calendario_indicati(self):
        log = os.path.join(self.tmp.name, "l.jsonl")
        d = self.deps()
        argv = ["run", "--commit", "--db", self.path, "--state-dir", self.state, "--log-file", log]
        with mock.patch.object(J, "SOURCES", {"classeviva": (J.CR, tuple(CV_KINDS)), "classroom": (J.CL, tuple(CLS_KINDS))}), \
                mock.patch("sys.stdout"):
            self.assertEqual(J.main(argv, deps=d), 0)
        self.assertEqual(d.schedule, (log, os.environ["CUORE_SCHOOL_JOB_PLIST"]))          # mai il plist reale nei test
        with open(os.path.join(self.state, "last_summary.json")) as f:
            self.assertIn("automazione", json.load(f))


if __name__ == "__main__":
    unittest.main()
