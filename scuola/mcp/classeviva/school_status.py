"""STATO OGGETTIVO PER MATERIA (M8 v1): `cloud.school.status/1`. Modulo PURO e SOLA LETTURA.

Livello sopra M7 (`school_context4.collect`, candidati COMPLETI): nessuna nuova fonte, nessun matcher, nessuna attribuzione.
Tre livelli sempre separati:
  * `fatti`       = conteggi e date osservabili, presi dai candidati M7 dell'anno OPERATIVO;
  * `segnali`     = regole deterministiche e versionate sui fatti (nessuna soglia nuova: solo confronti e finestre gia' di M7),
                    ognuno con evidenze (uid + hash M7), natura, affidabilita' e motivi;
  * `valutazioni` = in v1 SEMPRE `non_valutabile`: nessun giudizio di rendimento o puntualita'.
Decisioni (2026-10-05): D1 `legacy` e' solo una nota di provenance, non declassa; D2 si usano i candidati completi di M7.1;
D3 anno operativo = anno scolastico di `now` (2026-27); gli altri anni sono SOLO `storico`, separati e mai usati nei segnali.
Affidabilita': parte da `alta` e puo' solo scendere (monotona) per problemi che toccano davvero l'evidenza: interpretazione
M5/M6 non strutturale, confidenza M2 non alta, sezione stale/senza lettura, ambiguita' M7 su un elemento usato.
`possibili_da_verificare` e `generali` non alimentano MAI i segnali (solo copertura/storico); le assenze sono fuori perimetro.
Stesso Registry + materia + now + manuale + dettaglio + soglia -> stesso JSON; nessun orologio, nessuna scrittura.
"""
from __future__ import annotations

import collections
import datetime as dt
import os

import cls_subjects as CS
import cvv_client as cv
import cvv_comms as M
import cvv_registry as CR
import it_dates as ID
import registry as R
import school_common as CM
import school_context as SC
import school_context3 as C3
import school_context4 as C4

SCHEMA = "cloud.school.status/1"
STATUS_REV = 1

RELIABILITY = ("alta", "media", "bassa", "non_determinabile")
NATURE = ("fatto", "derivato_deterministico", "dipende_da_interpretazione")
STRUCTURAL = (None, "determinata", "determinato")                       # stati che NON sono interpretazione testuale
RECENT_DAYS = C4.WINDOWS["compiti_scaduti_giorni"]                       # finestra gia' di M7 (14 giorni), nessuna nuova soglia

SIGNALS = {
    "S1": "verifica_in_calendario",
    "S2": "compiti_aperti",
    "S3": "compiti_scaduti_non_consegnati_14g",
    "S4": "continuita_lezioni",
    "S5": "argomenti_recenti",
    "S6": "preparazione_collegata",
    "S7": "comunicazioni_operative",
    "S8": "attribuzione_classroom",
    "S9": "dati_valutativi",
}
RULES = {
    "S1": "prima verifica attribuita con data >= oggi nell'anno operativo; se assente e ci sono comunicazioni della materia con funzione "
          "verifica senza data utilizzabile (anno operativo) -> 'assente_con_indizi_non_datati'",
    "S2": "compiti dell'anno operativo con scadenza >= oggi: Classroom stato_operativo='aperto' (definizione M4) o compito di agenda futuro",
    "S3": "compiti Classroom dell'anno operativo 'scaduto_non_consegnato' entro %d giorni (finestra M7)" % RECENT_DAYS,
    "S4": "lezioni attribuite dell'anno operativo: numero, prima, ultima, giorni dall'ultima. Nessun rapporto atteso (orario assente)",
    "S5": "argomenti delle lezioni attribuite degli ultimi %d giorni (finestra M7) + lezioni senza argomento" % RECENT_DAYS,
    "S6": "per ogni verifica futura: collegamenti M5 (prepara/stesso_argomento/stessa_scadenza) che la toccano, con il loro stato",
    "S7": "comunicazioni della materia (materia determinata) dell'anno operativo: totale, ultima, classi principali M6 con il loro stato",
    "S8": "elementi Classroom dell'anno operativo attribuiti alla materia vs elementi solo possibili (corso condiviso senza topic)",
    "S9": "voti della materia nel registro e stato del kind classeviva/grade (letto e vuoto != mai letto)",
}
RELIABILITY_RULES = {
    "A1": "un'evidenza con stato M5/M6 'probabile' -> al massimo media",
    "A2": "un'evidenza con stato 'proposto' | 'conflitto' | 'non_determinabile' -> al massimo bassa",
    "A3": "confidenza M2 dell'attribuzione diversa da 'alta' -> al massimo media",
    "A4": "sezione M7 da cui dipende il segnale 'stale' -> al massimo bassa; 'senza_lettura' -> non_determinabile",
    "A5": "SOLO ambiguita' M7 pertinenti al segnale (tabella AMBIGUITY_RELEVANCE): su un'evidenza PRIMARIA (quella che determina il "
          "valore) -> al massimo bassa; su un'evidenza solo conteggiata -> al massimo media; ambiguita' pertinente con elenco uid troncato "
          "-> al massimo media (non verificabile)",
    "A6": "fonte mai letta -> valore null e non_determinabile",
    "D1": "'legacy' (run precedenti a M3) NON declassa: e' una nota di provenance",
}
# Quali ambiguita' M7 compromettono DAVVERO l'evidenza di ciascun segnale (D1: si declassa solo per problemi reali dell'evidenza).
AMBIGUITY_RELEVANCE = {
    "S1": ("discordanza_materia", "discordanza_date", "possibile_stessa_verifica"),
    "S2": ("discordanza_materia",),
    "S3": ("discordanza_materia",),
    "S4": ("discordanza_materia",),
    "S5": ("discordanza_materia",),
    "S6": ("discordanza_date", "pari_livello", "possibile_stessa_verifica"),
    "S7": ("conflitto_materia",),
    "S8": ("conflitto_materia",),
    "S9": (),
}
FORBIDDEN_WORDS = ("bravo", "brava", "scarso", "scarsa", "insufficiente", "sufficiente", "ottimo", "pessimo", "rendimento basso",
                   "rendimento alto", "male", "bene", "voto stimato", "promosso", "bocciato")

NATURA = ("stato OGGETTIVO derivato e in SOLA LETTURA per UNA materia: fatti osservabili, segnali spiegabili e valutazioni "
          "non_valutabile; nessun giudizio sul rendimento; il Registry resta l'unica fonte canonica")
RULES_FOR_AGENTS = [
    "Questo NON e' un giudizio: riporta fatti e segnali con la loro affidabilita'; non esprimere giudizi sullo studente.",
    "'valutazioni' e' sempre non_valutabile in v1: non stimare voti, medie, rendimento o puntualita'.",
    "Ogni segnale cita le sue evidenze (uid + hash M7): usale per spiegare, non aggiungere fatti che non ci sono.",
    "Riporta l'affidabilita' e i suoi motivi; 'probabile'/'proposto' non sono certezze.",
    "'storico' (anni diversi dall'operativo) e' contesto separato: non usarlo per descrivere la situazione attuale.",
    "Non dedurre 'poche' o 'tante' lezioni: l'orario non e' disponibile.",
    "Gli elementi 'possibili' e le circolari generali non sono della materia e non entrano nei segnali.",
    "Dati mancanti (null, non_determinabile) e dati vuoti (0 con fonte letta) sono diversi: dillo.",
]


def status_version():
    """'r<rev>-<impronta>': cambia con regole, segnali, livelli o con la versione del contesto M7 da cui dipende."""
    return cv.interp_version(STATUS_REV, SIGNALS, RULES, RELIABILITY_RULES, AMBIGUITY_RELEVANCE, list(RELIABILITY), list(NATURE), RECENT_DAYS,
                             list(FORBIDDEN_WORDS), C4.context_version())


# ------------------------------------------------------------------------------------------------ helper
def _sy(day):
    try:
        return ID.school_year_label(day) if day else None
    except ValueError:
        return None


def _ev(row):
    e = row["_mk"](False)
    return {"uid": e["uid"], "hash": e["hash"]}


def _year(row, section):
    """Anno scolastico di un candidato M7 (dai `_fatti` di M7.1; per i possibili dalla data o dalla perimetrazione M6)."""
    f = row.get("_fatti") or {}
    if "anno_scolastico" in f:
        return f["anno_scolastico"]
    e = row["_mk"](False)
    if section == "possibili_da_verificare" and e.get("tipo") == "comunicazione":
        return ((row["_mk"](True).get("evidenze") or {}).get("perimetro") or {}).get("anno")
    if section == "possibili_da_verificare" and e.get("tipo") == "corso_classroom":
        return None
    return _sy(e.get("data"))


def _kind_state(fresh, reg, source, kind):
    """'presente' | 'vuoto_valido' (letto con successo, 0 record) | 'non_letto' (mai letto o nessuna lettura riuscita)."""
    e = fresh["per_kind"].get("%s/%s" % (source, kind)) or {"motivo": "mai_sincronizzato"}
    n = len(reg.list_records(source=source, kind=kind, status=R.PRESENT))
    if e["motivo"] in ("mai_sincronizzato", "nessuna_lettura_riuscita"):
        return {"stato": "non_letto", "n": n if n else None, "motivo": e["motivo"]}
    return {"stato": "presente" if n else "vuoto_valido", "n": n, "stale": bool(e.get("stale")),
            "legacy": bool((e.get("ultimo_run") or {}).get("legacy"))}


class _Rel:
    """Affidabilita' MONOTONA: parte da 'alta' e puo' solo scendere; ogni discesa ha un motivo (regola A1..A6)."""

    def __init__(self):
        self.level, self.why = "alta", []

    def cap(self, level, rule, msg):
        if RELIABILITY.index(level) > RELIABILITY.index(self.level):
            self.level = level
        line = "%s: %s" % (rule, msg)
        if line not in self.why:
            self.why.append(line)

    def evidence(self, stato=None, confidenza=None, uid=None):
        if stato == "probabile":
            self.cap("media", "A1", "interpretazione 'probabile'%s" % (" (%s)" % uid if uid else ""))
        elif stato not in STRUCTURAL:
            self.cap("bassa", "A2", "interpretazione '%s'%s" % (stato, " (%s)" % uid if uid else ""))
        if confidenza not in (None, "alta"):
            self.cap("media", "A3", "confidenza M2 '%s'%s" % (confidenza, " (%s)" % uid if uid else ""))

    def sections(self, fsec, names):
        for n in names:
            st = fsec[n]["stato"]
            if st == "stale":
                self.cap("bassa", "A4", "sezione %s stale" % n)
            elif st == "senza_lettura":
                self.cap("non_determinabile", "A4", "sezione %s senza lettura" % n)

    def ambiguity(self, amb, primary, counted, sections, sid):
        for a in amb:
            if a["codice"] not in AMBIGUITY_RELEVANCE[sid]:
                continue
            uids = set(a.get("uid") or [])
            hit_p, hit_c = sorted(uids & set(primary)), sorted(uids & (set(counted) - set(primary)))
            if hit_p:
                self.cap("bassa", "A5", "%s su %s" % (a["codice"], ", ".join(hit_p)))
            elif hit_c:
                self.cap("media", "A5", "%s su %d elementi conteggiati" % (a["codice"], len(hit_c)))
            elif a.get("uid_troncati") and a["sezione"] in sections:
                self.cap("media", "A5", "%s con elenco uid troncato nella sezione %s: non verificabile" % (a["codice"], a["sezione"]))

    def out(self):
        return self.level, sorted(self.why)


def _signal(sid, valore, natura, evidenze, rel, sezioni, fsec, note=None, dettaglio=None):
    level, why = rel.out()
    s = {"id": sid, "nome": SIGNALS[sid], "valore": valore, "natura": natura, "regola": RULES[sid],
         "evidenze": evidenze, "affidabilita": level, "motivi_affidabilita": why,
         "freschezza": CM.worst_freshness(*[fsec[n]["stato"] for n in sezioni]) if sezioni else None}
    if note:
        s["note"] = note
    if dettaglio is not None:
        s["evidenze_dettaglio"] = dettaglio
    return s


def _counter(evid):
    """Evidenza di un CONTEGGIO (anche 0): mai vuota; dice da dove viene il numero."""
    return [{"contatore": evid}]


# ------------------------------------------------------------------------------------------------ costruzione
def build(reg, materia, now_dt, manual=None, dettaglio=False, soglia_ore=48, *, subjects, source_path=None):
    """cloud.school.status/1 per UNA delle 8 materie. Sola lettura; `now_dt` letto UNA volta dal chiamante."""
    if not isinstance(dettaglio, bool):
        raise ValueError("dettaglio non valido: %r (true | false)" % (dettaglio,))
    c = C4.collect(reg, materia, now_dt, manual=manual, soglia_ore=soglia_ore, subjects=subjects)
    manual = manual or {}
    now_dt = c["now"]
    td = c["data_riferimento"]
    today = dt.date.fromisoformat(td)
    year = _sy(td)
    key, cand, fsec, fresh, amb = c["chiave"], c["candidati"], c["freshness_sezioni"], c["freshness"], c["ambiguita"]
    recent_from = (today - dt.timedelta(days=RECENT_DAYS)).isoformat()

    def op(section):
        return [r for r in cand[section] if _year(r, section) == year]

    def el(r):
        return r["_mk"](False)

    def det(rows):
        return [el(r) for r in rows] if dettaglio else None

    kinds = {k: _kind_state(fresh, reg, s, k) for s, k in ((CR.SOURCE, "lesson"), (CR.SOURCE, "agenda_event"), (CR.SOURCE, "grade"),
                                                         ("classroom", "course_work"), ("classroom", "submission"),
                                                         ("classroom", "announcement"), (CR.SOURCE, "notice"))}
    grade_fresh = C3.section_freshness(fresh, [(CR.SOURCE, "grade")])
    fsec = dict(fsec, voti=grade_fresh)

    lez, ver, com, comp, coll = op("lezioni"), op("verifiche"), op("comunicazioni"), op("compiti"), op("collegamenti")
    poss_all = cand["possibili_da_verificare"]
    poss = [r for r in poss_all if _year(r, "possibili_da_verificare") == year]
    segnali = []

    # ---- S1 verifica_in_calendario ------------------------------------------------------------------------------
    fut = sorted((r for r in ver if (r["_date"] or "") >= td), key=lambda r: (r["_date"], r["uid"]))
    rel = _Rel()
    rel.sections(fsec, ["verifiche"])
    if fut:
        first = fut[0]
        for r in fut:
            f = r["_fatti"]
            rel.evidence(None if f["origine"] == "agenda" else f["stato"], f.get("confidenza_m2"), r["uid"])
        rel.ambiguity(amb, [first["uid"]], [r["uid"] for r in fut], ["verifiche"], "S1")
        e = el(first)
        val = {"stato": "presente", "data": first["_date"], "giorni_mancanti": (dt.date.fromisoformat(first["_date"]) - today).days,
               "uid": first["uid"], "origine": e["origine"], "n_future": len(fut)}
        natura = "dipende_da_interpretazione" if any(r["_fatti"]["origine"] != "agenda" or r["_fatti"].get("confidenza_m2") != "alta"
                                                    for r in fut) else "derivato_deterministico"
        segnali.append(_signal("S1", val, natura, [_ev(r) for r in fut], rel, ["verifiche"], fsec, dettaglio=det(fut)))
    else:
        gap_nd = next((g for g in c["gap"] if g["codice"] == "verifica_senza_data_utilizzabile"), None)
        com_year = {r["uid"]: r for r in com}
        hints = sorted(u for u in (gap_nd["uid"] if gap_nd else []) if u in com_year)
        if gap_nd and gap_nd.get("uid_troncati"):
            rel.cap("media", "A5", "elenco delle verifiche senza data troncato in M7: indizi forse incompleti")
        if kinds["agenda_event"]["stato"] == "non_letto":
            rel.cap("non_determinabile", "A6", "agenda ClasseViva mai letta")
            val = None
        elif hints:
            rel.cap("bassa", "A2", "indizi solo testuali M6, senza data utilizzabile")
            val = {"stato": "assente_con_indizi_non_datati", "n_indizi": len(hints)}
        else:
            val = {"stato": "assente"}
        ev = [_ev(com_year[u]) for u in hints] or _counter({"sezione": "verifiche", "anno": year, "n_future": 0})
        segnali.append(_signal("S1", val, "derivato_deterministico" if not hints else "dipende_da_interpretazione", ev, rel,
                               ["verifiche"], fsec, dettaglio=det([com_year[u] for u in hints])))

    # ---- S2 compiti_aperti / S3 scaduti non consegnati -----------------------------------------------------------
    open_ = sorted((r for r in comp if r["_prossimo"]), key=lambda r: (r["_date"], r["uid"]))
    late14 = sorted((r for r in comp if r["_fatti"].get("stato_operativo") == "scaduto_non_consegnato"), key=lambda r: (r["_date"], r["uid"]))
    for sid, rows, extra_note in (("S2", open_, None),
                                  ("S3", late14, ["classificazione tecnica Classroom: non dice se la consegna era richiesta"])):
        rel = _Rel()
        rel.sections(fsec, ["compiti"])
        for r in rows:
            rel.evidence(None, r["_fatti"].get("confidenza_m2"), r["uid"])
        rel.ambiguity(amb, [rows[0]["uid"]] if rows else [], [r["uid"] for r in rows], ["compiti"], sid)
        if kinds["course_work"]["stato"] == "non_letto" and kinds["agenda_event"]["stato"] == "non_letto":
            rel.cap("non_determinabile", "A6", "ne' agenda ne' compiti Classroom letti")
            val = None
        elif sid == "S2":
            val = {"n": len(rows), "prossima_scadenza": rows[0]["_date"] if rows else None, "uid": rows[0]["uid"] if rows else None}
        else:
            val = {"n": len(rows), "dal": recent_from}
        segnali.append(_signal(sid, val, "derivato_deterministico", [_ev(r) for r in rows] or _counter(
            {"sezione": "compiti", "anno": year, "n": 0}), rel, ["compiti"], fsec, note=extra_note, dettaglio=det(rows)))

    # ---- S4 continuita_lezioni / S5 argomenti_recenti ------------------------------------------------------------
    lez_sorted = sorted(lez, key=lambda r: (r["_date"] or "", r["uid"]))
    rel = _Rel()
    rel.sections(fsec, ["lezioni"])
    for r in lez_sorted:
        rel.evidence(None, r["_fatti"].get("confidenza_m2"), r["uid"])
    rel.ambiguity(amb, [lez_sorted[-1]["uid"]] if lez_sorted else [], [r["uid"] for r in lez_sorted], ["lezioni"], "S4")
    if kinds["lesson"]["stato"] == "non_letto":
        rel.cap("non_determinabile", "A6", "lezioni ClasseViva mai lette")
        val = None
    elif lez_sorted:
        last = lez_sorted[-1]["_date"]
        val = {"n": len(lez_sorted), "prima": lez_sorted[0]["_date"], "ultima": last,
               "giorni_da_ultima": (today - dt.date.fromisoformat(last)).days,
               "con_discordanza_m2": sum(1 for r in lez_sorted if r["_fatti"].get("conflitti_m2"))}
    else:
        val = {"n": 0, "prima": None, "ultima": None, "giorni_da_ultima": None, "con_discordanza_m2": 0}
    segnali.append(_signal("S4", val, "fatto", [_ev(r) for r in lez_sorted] or _counter({"sezione": "lezioni", "anno": year, "n": 0}),
                           rel, ["lezioni"], fsec, note=["nessun confronto con un numero atteso: l'orario delle lezioni non e' disponibile"],
                           dettaglio=det(lez_sorted)))

    rec = [r for r in lez_sorted if (r["_date"] or "") >= recent_from]
    rel = _Rel()
    rel.sections(fsec, ["lezioni"])
    for r in rec:
        rel.evidence(None, r["_fatti"].get("confidenza_m2"), r["uid"])
    rel.ambiguity(amb, [], [r["uid"] for r in rec], ["lezioni"], "S5")
    if kinds["lesson"]["stato"] == "non_letto":
        rel.cap("non_determinabile", "A6", "lezioni ClasseViva mai lette")
        val = None
    else:
        val = {"dal": recent_from,
               "argomenti": [dict({"data": r["_date"], "argomento": r["_fatti"]["argomento"], "uid": r["uid"]},
                                  **({"discordanza_m2": True} if r["_fatti"].get("conflitti_m2") else {}))
                             for r in sorted(rec, key=lambda r: (r["_date"], r["uid"]), reverse=True) if r["_fatti"].get("argomento")],
               "lezioni_senza_argomento": sum(1 for r in rec if not r["_fatti"].get("argomento"))}
    segnali.append(_signal("S5", val, "fatto", [_ev(r) for r in rec] or _counter({"sezione": "lezioni", "anno": year, "dal": recent_from, "n": 0}),
                           rel, ["lezioni"], fsec, dettaglio=det(rec)))

    # ---- S6 preparazione_collegata --------------------------------------------------------------------------------
    rel = _Rel()
    rel.sections(fsec, ["verifiche", "collegamenti"])
    per, used = [], []
    all_coll = cand["collegamenti"]                    # i collegamenti si cercano per uid della verifica (stesso anno della verifica)
    for v in fut:
        links = sorted((r for r in all_coll if v["uid"] in r["_fatti"]["lati"]), key=lambda r: (r["_fatti"]["stato"], r["uid"]))
        for r in links:
            rel.evidence(r["_fatti"]["stato"], None, r["uid"])
        used += links
        per.append({"verifica": v["uid"], "data": v["_date"],
                    "collegamenti": [{"uid": r["uid"], "relazione": r["_fatti"]["relazione"], "stato": r["_fatti"]["stato"],
                                      "tipo": r["_fatti"]["tipo"]} for r in links]})
    rel.ambiguity(amb, [r["uid"] for r in used] + [v["uid"] for v in fut], [r["uid"] for r in used], ["collegamenti", "verifiche"], "S6")
    val = {"stato": "non_applicabile", "motivo": "nessuna verifica futura nell'anno operativo"} if not fut else per
    ev = [_ev(r) for r in used] or _counter({"sezione": "collegamenti", "verifiche_future": len(fut), "collegamenti": 0})
    segnali.append(_signal("S6", val, "dipende_da_interpretazione" if used else "derivato_deterministico", ev, rel,
                           ["verifiche", "collegamenti"], fsec, note=["M5 non produce mai 'confermato': un collegamento non e' un fatto"],
                           dettaglio=det(used)))

    # ---- S7 comunicazioni_operative --------------------------------------------------------------------------------
    rel = _Rel()
    rel.sections(fsec, ["comunicazioni"])
    rel.ambiguity(amb, [], [r["uid"] for r in com], ["comunicazioni"], "S7")
    if kinds["announcement"]["stato"] == "non_letto":
        rel.cap("non_determinabile", "A6", "annunci Classroom mai letti")
        val = None
    else:
        per_cls = collections.defaultdict(lambda: collections.Counter())
        for r in com:
            per_cls[r["_fatti"]["classe_principale"] or "nessuna"][r["_fatti"]["stato_classe"]] += 1
        val = {"n": len(com), "ultima": max((r["_date"] for r in com), default=None),
               "per_classe_principale": {k: dict(sorted(v.items())) for k, v in sorted(per_cls.items())}}
    segnali.append(_signal("S7", val, "fatto", [_ev(r) for r in sorted(com, key=lambda r: r["uid"])] or _counter(
        {"sezione": "comunicazioni", "anno": year, "n": 0}), rel, ["comunicazioni"], fsec,
        note=["il totale e' un fatto (materia determinata da corso monomateria confermato); le classi sono interpretazioni M6 "
              "riportate con il loro stato"], dettaglio=det(sorted(com, key=lambda r: r["uid"]))))

    # ---- S8 attribuzione_classroom ---------------------------------------------------------------------------------
    cls_attr = [r for r in com] + [r for r in comp if r["_fatti"].get("origine") == "classroom"]
    cls_poss = [r for r in poss if el(r).get("motivo") == "corso_condiviso_senza_topic"]
    rel = _Rel()
    rel.sections(fsec, ["comunicazioni", "compiti", "possibili_da_verificare"])
    if not c["corsi_confermati"]:
        val = {"stato": "nessun_corso_confermato", "n_attribuiti": 0, "n_solo_possibili": len(cls_poss)}
    elif cls_attr:
        val = {"stato": "attribuiti", "n_attribuiti": len(cls_attr), "n_solo_possibili": len(cls_poss)}
    elif cls_poss:
        val = {"stato": "solo_possibili", "n_attribuiti": 0, "n_solo_possibili": len(cls_poss)}
    else:
        val = {"stato": "nessun_elemento", "n_attribuiti": 0, "n_solo_possibili": 0}
    conf = CS.confirmed_map(manual)
    courses = [{"id": cid, "materie": sorted(conf.get(cid, ()))} for cid in c["corsi_confermati"]]
    val["corsi_confermati"] = courses
    segnali.append(_signal("S8", val, "derivato_deterministico", _counter(
        {"sezioni": ["comunicazioni", "compiti", "possibili_da_verificare"], "anno": year, "n_attribuiti": len(cls_attr),
         "n_solo_possibili": len(cls_poss), "corsi_confermati": c["corsi_confermati"]}), rel,
        ["comunicazioni", "compiti", "possibili_da_verificare"], fsec,
        note=["gli elementi solo possibili NON sono della materia: contano solo per dire quanto Classroom e' attribuibile"]))

    # ---- S9 dati_valutativi ----------------------------------------------------------------------------------------
    rel = _Rel()
    rel.sections(fsec, ["voti"])
    gk = kinds["grade"]
    n_subject = 0
    for r in reg.list_records(source=CR.SOURCE, kind="grade", status=R.PRESENT):
        n = CR.normalized("grade", reg.get_record(CR.SOURCE, "grade", r["source_uid"])["raw"])
        n_subject += n["interpreted"]["materia_chiave"] == key
    if gk["stato"] == "non_letto":
        rel.cap("non_determinabile", "A6", "voti ClasseViva mai letti")
        s9 = {"stato": "non_disponibile", "n_materia": None, "n_registro": gk["n"]}
    else:
        s9 = {"stato": "voti_presenti" if n_subject else "nessun_voto", "n_materia": n_subject, "n_registro": gk["n"]}
    segnali.append(_signal("S9", s9, "fatto", _counter({"kind": "classeviva/grade", "stato_kind": gk["stato"], "n_registro": gk["n"],
                                                        "n_materia": s9["n_materia"]}), rel, ["voti"], fsec))

    # ---- fatti (aggregati dell'anno operativo) ---------------------------------------------------------------------
    stati_coll = collections.Counter(r["_fatti"]["stato"] for r in coll)
    poss_m = collections.Counter(el(r).get("motivo") for r in poss)
    fatti = {
        "lezioni": {"n": len(lez), "prima": lez_sorted[0]["_date"] if lez_sorted else None,
                    "ultima": lez_sorted[-1]["_date"] if lez_sorted else None,
                    "senza_argomento": sum(1 for r in lez if not r["_fatti"].get("argomento"))},
        "verifiche": {"future": len(fut), "passate_entro_7g": len(ver) - len(fut),
                      "da_agenda": sum(1 for r in ver if r["_fatti"]["origine"] == "agenda"),
                      "da_comunicazione": sum(1 for r in ver if r["_fatti"]["origine"] != "agenda"),
                      "unite": sum(len(r["_fatti"]["uniti"]) for r in ver)},
        "compiti": {"aperti_futuri": len(open_), "scaduti_non_consegnati_14g": len(late14),
                    "da_agenda": sum(1 for r in comp if r["_fatti"]["origine"] == "agenda_classeviva"),
                    "da_classroom": sum(1 for r in comp if r["_fatti"]["origine"] == "classroom"),
                    "consegne_in_ritardo_tra_i_candidati": sum(1 for r in comp if r["_fatti"].get("late"))},
        "comunicazioni": {"n": len(com), "ultima": max((r["_date"] for r in com), default=None)},
        "collegamenti": {"n": len(coll), "per_stato": dict(sorted(stati_coll.items()))},
        "possibili_non_attribuiti": {"n": len(poss), "per_motivo": dict(sorted((str(k), v) for k, v in poss_m.items())),
                                     "nota": "NON della materia: solo copertura"},
        "voti": {"n_materia": s9["n_materia"], "n_registro": gk["n"], "stato_kind": gk["stato"]},
        "anno": year, "fonte": "candidati completi M7 (school_context4.collect) dell'anno operativo",
    }

    # ---- storico separato (mai nei segnali) --------------------------------------------------------------------------
    storico = collections.defaultdict(collections.Counter)
    senza_anno = 0
    for s in ("lezioni", "verifiche", "compiti", "comunicazioni", "collegamenti", "possibili_da_verificare"):
        for r in cand[s]:
            y = _year(r, s)
            if y is None:
                senza_anno += 1
            elif y != year:
                storico[y][s] += 1
    storico_out = {"anni": {y: dict(sorted(v.items())) for y, v in sorted(storico.items())}, "elementi_senza_anno": senza_anno,
                   "nota": "contesto storico SEPARATO (D3): conteggi dei candidati M7 di anni diversi dall'operativo; non usati nei segnali"}

    # ---- copertura ----------------------------------------------------------------------------------------------------
    copertura = {
        "lezioni": dict(kinds["lesson"], anno_operativo=len(lez), dal=fatti["lezioni"]["prima"], al=fatti["lezioni"]["ultima"]),
        "agenda": kinds["agenda_event"],
        "voti": kinds["grade"],
        "classroom": {"corsi_confermati": courses,
                      "compiti": kinds["course_work"], "consegne": kinds["submission"], "annunci": kinds["announcement"]},
        "circolari": dict(kinds["notice"], nota="ambito generale: non usate nei segnali della materia"),
        "assenze": {"stato": "fuori_perimetro", "nota": "le assenze non hanno materia; attribuirle sarebbe un nuovo matcher"},
        "orario": {"stato": "non_disponibile", "nota": "nessun numero atteso di lezioni"},
    }

    # ---- gap e ambiguita' ---------------------------------------------------------------------------------------------
    gap = []
    if s9["stato"] == "nessun_voto":
        gap.append({"codice": "dati_valutativi_assenti", "messaggio": "0 voti per la materia (kind letto): rendimento non valutabile"})
    for name, k in sorted(kinds.items()):
        if k["stato"] == "non_letto":
            gap.append({"codice": "fonte_non_letta", "messaggio": "kind %s mai letto: segnali che ne dipendono non determinabili" % name})
    gap_m7 = [{"codice": g["codice"], "sezione": g["sezione"], "n": g.get("n")} for g in c["gap"]]
    sig_uids = {s["id"]: {e.get("uid") for e in s["evidenze"]} for s in segnali}
    ambig = []
    for a in amb:
        touched = sorted(sid for sid, us in sig_uids.items() if us & set(a.get("uid") or []) and a["codice"] in AMBIGUITY_RELEVANCE[sid])
        ambig.append({"codice": a["codice"], "sezione": a["sezione"], "n": a.get("n", len(a.get("uid") or [])),
                      "uid": a.get("uid"), "uid_troncati": bool(a.get("uid_troncati")), "tocca_segnali": touched})

    legacy = sorted(k for k, e in fresh["per_kind"].items() if (e.get("ultimo_run") or {}).get("legacy"))
    return {
        "schema": SCHEMA,
        "natura": NATURA,
        "materia": {"chiave": key, "nome_ufficiale": c["nome_ufficiale"],
                    "docenti": {"valori": c["docenti"], "fonte": "ClasseViva /subjects"} if c["docenti"] else {"valori": [], "stato": "non_disponibile"}},
        "riferimento_temporale": {"now": now_dt.isoformat(), "data_riferimento": td, "anno_operativo": year,
                                  "finestra_recente_giorni": RECENT_DAYS, "finestre_m7": c["finestre"],
                                  "nota": "anno operativo = anno scolastico di now (D3); gli altri anni sono solo storico"},
        "livelli": {"fatti": "conteggi e date osservabili dai candidati M7 dell'anno operativo",
                    "segnali": "regole deterministiche sui fatti, con evidenze, natura e affidabilita'",
                    "valutazioni": "v1: sempre non_valutabile (nessun giudizio)", "dettaglio": dettaglio},
        "copertura": copertura,
        "fatti": fatti,
        "segnali": segnali,
        "valutazioni": {
            "rendimento": {"stato": "non_valutabile",
                           "motivo": "0 voti nel registro per la materia" if not s9["n_materia"] else
                           "M8 v1 non valuta il rendimento (nessun modello approvato)"},
            "puntualita": {"stato": "non_valutabile", "motivo": "M8 v1 non calcola indici di puntualita'",
                           "fatti": {"compiti_classroom_candidati": fatti["compiti"]["da_classroom"],
                                     "consegne_in_ritardo_tra_i_candidati": fatti["compiti"]["consegne_in_ritardo_tra_i_candidati"],
                                     "nota": "i compiti consegnati non sono candidati M7: nessun denominatore affidabile"}},
            "nota": "nessun giudizio: valutazioni sempre non_valutabile in v1"},
        "storico": storico_out,
        "freshness": {"calcolata_a": fresh["calcolato_a"], "soglia_ore": fresh["soglia_ore"],
                      "per_sezione": {n: f["stato"] for n, f in sorted(fsec.items())}, "nota": C3.FRESH_NOTE},
        "gap": gap,
        "gap_m7": gap_m7,
        "ambiguita": ambig,
        "provenance": {"schema": SCHEMA, "status_version": status_version(), "context_version": C4.context_version(),
                       "comm_version": M.comm_version(), "link_version": c["link_version"],
                       "schema_registro": c["schema_registro"], "registro_migrato_in_memoria": c["registro_migrato_in_memoria"],
                       "sorgente": source_path, "livello_manuale_presente": c["manuale_presente"],
                       "kind_legacy": legacy, "nota_legacy": "D1: legacy e' solo provenance, non declassa l'affidabilita'",
                       "regole_affidabilita": RELIABILITY_RULES},
        "regole_agente": RULES_FOR_AGENTS,
    }


def build_from_path(path, materia, now, manual_path=None, **kw):
    """Registro aperto come COPIA in memoria (Registry.from_snapshot, immutable=1): nessun byte del file viene scritto."""
    reg = R.Registry.from_snapshot(path)
    try:
        return build(reg, materia, now, manual=SC.load_manual(manual_path)["dati"], source_path=os.path.abspath(path), **kw)
    finally:
        reg.close()
