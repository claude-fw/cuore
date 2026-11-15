"""CONTESTO OPERATIVO PER UNA MATERIA (M7): `cloud.school.context/4`. Modulo PURO e SOLA LETTURA.

Layer di AGGREGAZIONE sopra M2/M4/M5/M6: non e' una nuova fonte, non e' un nuovo matcher, non e' un nuovo sistema di attribuzione.
Riusa SOLO cio' che gli strati esistenti producono gia':
  * lezioni e agenda       -> school_context3 (M4: attribuzione M2, `possibili_da_verificare`);
  * compiti Classroom       -> cls_derive (L3: `stato_operativo`, definizione M4 di compito aperto) + cvv_cls_links.item_subject (M5);
  * comunicazioni/generali  -> cvv_comms (M6: classificazioni, ambito, materia, date con ruolo);
  * collegamenti            -> cvv_cls_links (M5: collegamenti, conflitti, non_determinabili, con i loro stati REALI);
  * freschezza              -> registry.freshness + school_context3.section_freshness (M3/M4).

Regole (non negoziabili):
  * un elemento e' della materia SOLO se lo strato di origine la DETERMINA; il resto sta in `possibili_da_verificare` e non viene MAI
    promosso; i conflitti restano conflitti, le ambiguita' restano ambiguita';
  * verifiche: due elementi sono UNITI solo se hanno lo stesso uid oppure un collegamento M5 `stessa_scadenza` nello stato piu' alto
    che M5 produce davvero (`probabile`: `confermato` e' riservato e mai prodotto). Ogni altro collegamento -> ambiguita'
    `possibile_stessa_verifica`, elementi distinti. Nessuna deduplicazione per testo o data;
  * `now` arriva dal chiamante (una sola lettura dell'orologio, nel tool MCP): nessuna sezione legge l'orologio;
  * stesso Registry + materia + now + manual + dettaglio + limiti + soglia -> stesso JSON (byte per byte);
  * nessuna scrittura: il Registry e' aperto come copia in memoria (Registry.from_snapshot) o passato gia' aperto dal chiamante.
"""
from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import hashlib
import json
import os

import cls_derive as D
import cls_registry as CL
import cls_subjects as CS
import cvv_attribution as CA
import cvv_client as cv
import cvv_cls_links as L
import cvv_comms as M
import cvv_registry as CR
import it_dates as ID
import registry as R
import school_common as CM
import school_context as SC
import school_context3 as C3

SCHEMA = "cloud.school.context/4"
CONTEXT_REV = 1

SECTIONS = (
    "lezioni",
    "verifiche",
    "compiti",
    "comunicazioni",
    "collegamenti",
    "possibili_da_verificare",
    "generali",
)

LIMITS_DEFAULT = {
    "prossimi_impegni": 10,
    "lezioni": 8,
    "verifiche": 8,
    "compiti": 10,
    "comunicazioni": 8,
    "collegamenti": 8,
    "possibili_da_verificare": 6,
    "generali": 5,
}

LIMIT_MAX = 50

COMPACT_TEXT = 160

WINDOWS = {
    "verifiche_passato_giorni": 7,
    "compiti_scaduti_giorni": 14,
    "generali_giorni": 30,
}

# Lezione "recente" per il gap nessuna_lezione_recente: stessa ampiezza della finestra dei compiti scaduti (nessuna nuova finestra).
RECENT_LESSON_DAYS = WINDOWS["compiti_scaduti_giorni"]
UID_IN_GAP = 10

# Stato M5 usato per l'UNIONE di due verifiche: il piu' alto che M5 produce DAVVERO (gli stati riservati non sono mai prodotti).
MERGE_RELATION = "stessa_scadenza"
MERGE_STATE = next(s for s in L.STATES if s not in L.RESERVED_STATES and s not in ("conflitto", "non_determinabile"))

_CV, _CLS = CR.SOURCE, CL.SOURCE
_CLS_ALL = [(_CLS, k) for k in ("course", "course_work", "submission", "announcement", "material", "topic")]
SECTION_KINDS = {
    "lezioni": [(_CV, "lesson")],
    "verifiche": [(_CV, "agenda_event"), (_CV, "notice"), (_CLS, "announcement"), (_CLS, "course")],
    "compiti": [(_CV, "agenda_event"), (_CLS, "course"), (_CLS, "course_work"), (_CLS, "submission"), (_CLS, "topic")],
    "comunicazioni": [(_CLS, "announcement"), (_CLS, "course")],
    "collegamenti": [(_CV, "agenda_event"), (_CV, "lesson")] + [(_CLS, k) for k in ("course", "course_work", "material", "announcement", "topic")],
    "possibili_da_verificare": [(_CV, "lesson"), (_CV, "agenda_event"), (_CLS, "course"), (_CLS, "course_work"),
                                (_CLS, "submission"), (_CLS, "announcement"), (_CLS, "topic")],
    "generali": [(_CV, "notice")],
}

GAP_CODES = ("nessuna_lezione_recente", "docente_non_disponibile", "corso_condiviso_senza_topic", "corso_non_confermato",
             "verifica_senza_data_utilizzabile", "verifica_solo_da_annuncio", "compito_senza_scadenza", "sezione_non_fresca",
             "circolare_senza_testo", "sezione_troncata", "possibili_non_comunicazioni_non_coperti")
AMBIGUITY_CODES = ("pari_livello", "riferimenti_relativi_non_risolti", "discordanza_date", "discordanza_materia", "conflitto_materia",
                   "possibile_stessa_verifica")

NATURA = ("contesto operativo DERIVATO e in SOLA LETTURA per UNA materia: aggrega M2/M4/M5/M6 senza nuove fonti, matcher o "
          "attribuzioni; il Registry resta l'unica fonte canonica")

SECTION_RULES = {
    "lezioni": "lezioni ClasseViva con materia DETERMINATA da M2; data DESC poi uid; nessun raggruppamento per argomento",
    "verifiche": "eventi di agenda 'verifica' della materia (M2) + comunicazioni M6 della materia con funzione verifica e UNA data "
                 "esplicita con ruolo verifica; future + %d giorni passati; future in ordine di data, poi passate dalla piu' recente; "
                 "unione SOLO per stesso uid o collegamento M5 '%s' '%s'" % (WINDOWS["verifiche_passato_giorni"], MERGE_RELATION, MERGE_STATE),
    "compiti": "compiti di agenda ClasseViva della materia (M2) + compiti Classroom APERTI (definizione M4: stato_operativo='aperto') o "
               "scaduti non consegnati da al massimo %d giorni, solo se la materia e' determinata (corso monomateria confermato o topic "
               "che nomina una sola materia); stato di consegna e scadenza invariati" % WINDOWS["compiti_scaduti_giorni"],
    "comunicazioni": "annunci M6 del perimetro operativo con materia.stato='determinata' uguale a questa materia; data DESC",
    "collegamenti": "collegamenti/conflitti/non_determinabili M5 con almeno un lato attribuito (determinato) a questa materia; "
                    "classificazione M5 invariata",
    "possibili_da_verificare": "elementi che POTREBBERO riguardare la materia ma non sono attribuiti: M2 da verificare, corsi condivisi "
                               "senza topic, corsi non confermati, conflitti di materia. Non vengono MAI promossi nelle sezioni certe",
    "generali": "circolari ClasseViva (ambito generale, M6) degli ultimi %d giorni o ancora valide, escluse quelle con validita' nativa "
                "scaduta/fuori intervallo; NON attribuite alla materia, MAI prossimi impegni" % WINDOWS["generali_giorni"],
}

RULES_FOR_AGENTS = [
    "Questo contesto e' una vista DERIVATA in sola lettura: cita uid + hash (content_hash del record) di cio' che usi.",
    "Solo le sezioni lezioni, verifiche, compiti, comunicazioni e collegamenti contengono elementi attribuiti alla materia; "
    "'possibili_da_verificare' NON e' della materia: non promuoverli e non dedurne la materia.",
    "Gli stati M5/M6 (probabile, proposto, conflitto, non_determinabile) non sono certezze: riportali come tali.",
    "Un conflitto o un'ambiguita' va mostrato con entrambi i valori: non scegliere e non dire che una fonte ha torto.",
    "'prossimi_impegni' contiene solo elementi della materia con data futura utilizzabile: non aggiungere generali, possibili o "
    "riferimenti relativi ('per lunedi') non risolti.",
    "Le circolari 'generali' non sono della materia e non sono impegni: citale come contesto della scuola.",
    "'contatori' = candidati totali dopo le regole e le finestre; 'mostrati' = quanti sono nel documento: se troncato dillo.",
    "Controlla 'freshness': se una sezione e' stale o senza_lettura dichiaralo; 'aggiornato' non garantisce correttezza.",
    "'gap' e 'ambiguita' elencano cio' che manca o e' incerto: non inventarlo e non risolverlo.",
    "Un compito senza scadenza resta senza scadenza; lo stato di consegna Classroom e' tecnico, non un giudizio.",
]


def context_version():
    """'r<rev>-<impronta>': cambia da sola se cambiano sezioni, limiti, finestre o le versioni degli strati riusati (M2/M5/M6/L3)."""
    return cv.interp_version(CONTEXT_REV, list(SECTIONS), LIMITS_DEFAULT, LIMIT_MAX, COMPACT_TEXT, WINDOWS, RECENT_LESSON_DAYS,
                             MERGE_RELATION, MERGE_STATE, SECTION_KINDS, list(GAP_CODES), list(AMBIGUITY_CODES),
                             M.comm_version(), L.link_version(), CA.attr_version(), D.DERIVE_VERSION)


# ------------------------------------------------------------------------------------------------ parametri
def _limits(limiti):
    """Limiti effettivi + avvisi. Convenzione esistente (school_agent._limit): intero >= 1 obbligatorio, oltre il massimo NORMALIZZATO."""
    eff, notes = dict(LIMITS_DEFAULT), []
    if limiti is None:
        return eff, notes
    if not isinstance(limiti, dict):
        raise ValueError("limiti non validi: %r (atteso un oggetto {sezione: intero})" % (limiti,))
    for k in sorted(limiti):
        v = limiti[k]
        if k not in LIMITS_DEFAULT:
            raise ValueError("limite sconosciuto: %r (ammessi: %s)" % (k, ", ".join(LIMITS_DEFAULT)))
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ValueError("limite non valido per %s: %r (intero da 1 a %d)" % (k, v, LIMIT_MAX))
        if v > LIMIT_MAX:
            notes.append((k, v))
            v = LIMIT_MAX
        eff[k] = v
    return eff, notes


def _detail_set(dettaglio):
    if dettaglio is None or dettaglio is False:
        return []
    if dettaglio is True:
        return list(SECTIONS)
    if isinstance(dettaglio, (list, tuple)):
        bad = [s for s in dettaglio if s not in SECTIONS]
        if bad or not all(isinstance(s, str) for s in dettaglio):
            raise ValueError("dettaglio non valido: %r (sezioni: %s)" % (bad or dettaglio, ", ".join(SECTIONS)))
        return [s for s in SECTIONS if s in set(dettaglio)]
    raise ValueError("dettaglio non valido: %r (false | true | lista di sezioni)" % (dettaglio,))


# ------------------------------------------------------------------------------------------------ elementi
def _cite(c):
    """Citazione uniforme di un record del Registry (uid, registry_version, content_hash, source_status)."""
    if not c:
        return None
    return {"uid": c.get("uid"), "registry_version": c.get("registry_version", c.get("version")),
            "content_hash": c.get("content_hash"), "source_status": c.get("source_status", c.get("stato_registro"))}


def _pair_hash(*hashes):
    return hashlib.sha256("|".join(h or "" for h in hashes).encode("utf-8")).hexdigest()


def _el(uid, full_hash, cite, data, testo, extra, detail, evid=None, fresh=None, full_text=None):
    """Elemento uniforme. Compatto: uid, hash (12), data, testo <= COMPACT_TEXT, campi essenziali. Dettaglio: + hash completo,
    registry_version, record di origine, testo completo, evidenze, freschezza dell'elemento."""
    e = {"uid": uid, "hash": full_hash[:12] if full_hash else None, "data": data}
    txt = (full_text if full_text is not None else testo) or ""
    if detail:
        e["testo"] = txt.strip() or None
    else:
        t, cut = CM.cap_text(txt, COMPACT_TEXT)
        e["testo"] = t or None
        if cut:
            e["testo_troncato"] = True
    e.update(extra)
    if detail:
        e["content_hash"] = full_hash
        e["registry_version"] = (cite or {}).get("registry_version")
        e["fonte_record"] = cite
        if evid is not None:
            e["evidenze"] = evid
        e["freschezza"] = fresh
    return e


def _today(now_dt):
    return now_dt.astimezone(D._zone()).date()


def _minus(d, days):
    return (d - dt.timedelta(days=days)).isoformat()


def _future_then_past(rows, today_iso, date_of):
    """Prima le future (data crescente), poi le passate (dalla piu' recente). Tie-break sempre sull'uid."""
    fut = sorted((r for r in rows if (date_of(r) or "") >= today_iso), key=lambda r: (date_of(r) or "", r["uid"]))
    past = sorted((r for r in rows if (date_of(r) or "") < today_iso), key=lambda r: r["uid"])
    past.sort(key=lambda r: date_of(r) or "", reverse=True)
    return fut + past


def _desc(rows, date_of):
    out = sorted(rows, key=lambda r: r["uid"])
    out.sort(key=lambda r: date_of(r) or "", reverse=True)
    return out


def _uids(xs):
    xs = sorted(set(xs))
    return {"n": len(xs), "uid": xs[:UID_IN_GAP], "uid_troncati": len(xs) > UID_IN_GAP}


def _gap(out, codice, sezione, messaggio, uids=None, **extra):
    g = {"codice": codice, "sezione": sezione, "messaggio": messaggio}
    if uids is not None:
        g.update(_uids(uids))
    g.update(extra)
    out.append(g)


# ------------------------------------------------------------------------------------------------ raccolta (sola lettura)
def _topics(reg):
    out = {}
    for f in reg.list_records(source=_CLS, kind="topic", status=R.PRESENT):
        raw = reg.get_record(_CLS, "topic", f["source_uid"])["raw"]
        out[(raw.get("courseId"), raw.get("topicId"))] = raw.get("name")
    return out


def _m6_dates(x):
    """Date esplicite VALIDE con ruolo verifica di una classificazione M6 (nessuna risoluzione di riferimenti relativi)."""
    return sorted({d["data"] for d in x["interpretazione"]["date"] if d["ruolo"] == "verifica" and d["stato"] == "valida" and d["data"]})


def _sy(day):
    """Anno scolastico 'AAAA-AA' di una data ISO (it_dates, gia' usato da M5/M6); None se assente o illeggibile."""
    try:
        return ID.school_year_label(day) if day else None
    except ValueError:
        return None


def _fn(x, name):
    return next((f for f in x["interpretazione"]["funzioni"] if f["funzione"] == name), None)


# ------------------------------------------------------------------------------------------------ costruzione
def _prepare(materia, now_dt, soglia_ore, subjects):
    """Validazione comune (stesso ordine degli errori di sempre) -> (now_dt con fuso, chiave materia)."""
    if not (isinstance(materia, str) and materia.strip()):
        raise ValueError("materia non valida: %r" % (materia,))
    if isinstance(soglia_ore, bool) or not isinstance(soglia_ore, (int, float)) or soglia_ore <= 0:
        raise ValueError("soglia_ore non valida: %r" % (soglia_ore,))
    now_dt = D.parse_now(now_dt)
    return now_dt, SC.resolve_subject(materia.strip(), subjects)


# Strati che NON dipendono dalla materia (M5 collegamenti, M6 comunicazioni, L3 derivazione Classroom, argomenti): identici per
# le 8 materie. Riusati SOLO dentro uno `shared_layers(store)` aperto da school_dashboard.PageSession (stessa copia del Registry,
# stesso istante, manuale e materie); la chiave li ricontrolla a ogni uso e, se non coincide, ricalcola senza riusare. Fuori da
# una sessione (strumenti MCP, build_from_path) nessun riuso: comportamento invariato.
_SHARED = contextvars.ContextVar("school_context4_shared_layers", default=None)


@contextlib.contextmanager
def shared_layers(store):
    token = _SHARED.set(store)
    try:
        yield store
    finally:
        _SHARED.reset(token)


def _shared_layers(reg, subjects, now_dt, manual, confirmed):
    store = _SHARED.get()
    key = None
    if store is not None:
        key = (id(reg), now_dt.isoformat(), json.dumps(manual, sort_keys=True, default=str),
               json.dumps(subjects, sort_keys=True, default=str))
        if store.get("key") == key:
            return store["layers"]
    prep5 = L.collect(reg, subjects, manual)
    res5 = L.compute(prep5)
    allc, prep6, _ = M.compute(reg, subjects, now_dt, manual)
    l3 = D.from_registry(reg, now_dt, manual={"course_subjects": {
        cid: [{"subject": k} for k in sorted(ks)] for cid, ks in confirmed.items()}})
    layers = (prep5, res5, allc, prep6, l3, _topics(reg))
    if store is not None and "key" not in store:                 # solo il primo insieme di parametri della sessione
        store.update(key=key, layers=layers)
    return layers


def collect(reg, materia, now_dt, manual=None, soglia_ore=48, *, subjects):
    """M7.1 - RACCOLTA: TUTTI i candidati di ogni sezione (dopo regole e finestre, PRIMA di limiti e livello di dettaglio), gap e
    ambiguita' che non dipendono dai limiti, prossimi impegni completi, freschezza. Sola lettura; nessun orologio.
    Ogni riga di `candidati[sezione]` ha `uid`, `_mk(detail)` (l'elemento M7 esatto) e `_fatti` (campi per gli strati successivi,
    es. anno_scolastico, confidenza M2, late della consegna, topic): `_fatti` NON entra mai nel documento M7."""
    now_dt, key = _prepare(materia, now_dt, soglia_ore, subjects)
    manual = manual or {}
    today = _today(now_dt)
    td = today.isoformat()
    official = [s for s in subjects if s["key"] == key]
    desc = official[0]["desc"] if official else None
    teachers = sorted({t for s in official for t in s.get("docenti_nomi", []) if t})

    fresh = reg.freshness(now_dt.isoformat(), stale_after_hours=soglia_ore)
    fsec = {n: C3.section_freshness(fresh, k) for n, k in SECTION_KINDS.items()}
    fkind = {}

    def fk(*kinds):
        """Freschezza di un elemento: peggiore tra i kind da cui dipende."""
        for k in kinds:
            if k not in fkind:
                fkind[k] = C3.section_freshness(fresh, [k])["stato"]
        return CM.worst_freshness(*[fkind[k] for k in kinds])

    gaps, amb, warn = [], [], []
    confirmed = CS.confirmed_map(manual)
    my_courses = sorted(cid for cid, ks in confirmed.items() if key in ks)

    # ---- strati esistenti (tutti in sola lettura, stesso now) ------------------------------------------------------
    lez, _, _ = C3.lessons_section(reg, key, subjects, now_dt, limit=10 ** 9)
    age, _, _, _ = C3.agenda_section(reg, key, subjects, now_dt, past_limit=10 ** 9)
    prep5, res5, allc, prep6, l3, topics = _shared_layers(reg, subjects, now_dt, manual, confirmed)
    full_text = {c["uid"]: c["testo"] for c in prep6["comms"]}
    oper6 = [x for x in allc if x["interpretazione"]["perimetro"]["valore"] == "operativo"]

    # ================================================================================================ LEZIONI
    sec_rows = {}
    rows = []
    for r in lez["record"]:
        d = r["dati"]
        extra = {"ora": d["ora"], "docente": d["docente"], "tipo_lezione": d["tipo"],
                 "attribuzione": {"stato": r["interpretazione"]["stato"], "criterio": r["interpretazione"]["criterio"]}}
        if r["interpretazione"]["conflitti"]:
            extra["conflitti_m2"] = len(r["interpretazione"]["conflitti"])
        rows.append(dict(uid=r["uid"], _date=d["data"], _mk=lambda detail, r=r, extra=extra, d=d: _el(
            r["uid"], r["fonte"]["content_hash"], _cite(r["fonte"]), d["data"], d["argomento"], extra, detail,
            evid=r["interpretazione"], fresh=fk((_CV, "lesson"))),
            _fatti={"anno_scolastico": _sy(d["data"]), "argomento": d["argomento"], "confidenza_m2": r["interpretazione"]["confidenza"],
                    "conflitti_m2": len(r["interpretazione"]["conflitti"])}))
    sec_rows["lezioni"] = _desc(rows, lambda r: r["_date"])
    recent = [r for r in rows if (r["_date"] or "") >= _minus(today, RECENT_LESSON_DAYS)]
    n_lessons_reg = lez["conteggio"]["nel_registro"]
    if n_lessons_reg and not recent:
        last = max((r["_date"] or "" for r in rows), default=None)
        _gap(gaps, "nessuna_lezione_recente", "lezioni",
             "nessuna lezione attribuita a questa materia negli ultimi %d giorni%s" % (
                 RECENT_LESSON_DAYS, " (ultima: %s)" % last if last else " (nessuna lezione attribuita)"), ultima=last or None)
    disc_m = [r["uid"] for r in lez["record"] if r["interpretazione"]["conflitti"]]
    disc_m += [r["uid"] for r in age["verifiche_future"] + age["verifiche_passate"] + age["compiti"] + age["eventi"]
               if r["interpretazione"]["conflitti"]]
    if disc_m:
        amb.append(dict({"codice": "discordanza_materia", "sezione": "lezioni",
                         "messaggio": "M2 ha determinato la materia ma segnala un conflitto (es. subjectDesc vs testo): materia NON modificata"},
                        **_uids(disc_m)))

    # ================================================================================================ VERIFICHE
    v_cands = []
    v_from = _minus(today, WINDOWS["verifiche_passato_giorni"])
    for r in age["verifiche_future"] + age["verifiche_passate"]:
        d = r["dati"]
        if (d["data"] or "") < v_from:
            continue
        v_cands.append({"uid": r["uid"], "_date": d["data"], "_origine": "agenda", "_hash": r["fonte"]["content_hash"],
                        "_cite": _cite(r["fonte"]), "_text": d["testo"], "_stato": "determinata (M2)", "_ev": r["interpretazione"],
                        "_extra": {"origine": "agenda_classeviva", "docente": d["docente"],
                                   "attribuzione": {"stato": r["interpretazione"]["stato"], "criterio": r["interpretazione"]["criterio"]}},
                        "_fresh": fk((_CV, "agenda_event")),
                        "_fatti": {"anno_scolastico": _sy(d["data"]), "confidenza_m2": r["interpretazione"]["confidenza"]}})
    no_date, rel_v, disc_d = [], [], []
    for x in oper6:
        m = x["interpretazione"]["materia"]
        f = _fn(x, "verifica")
        if f is None or m["key"] != key or m["stato"] != "determinata":
            continue
        vd = _m6_dates(x)
        if len(vd) > 1:
            disc_d.append(x["uid"])
            continue
        if not vd:
            no_date.append(x["uid"])
            if x["interpretazione"]["riferimenti_relativi_non_risolti"]:
                rel_v.append(x["uid"])
            continue
        if vd[0] < v_from:
            continue
        kinds = [tuple(s) for s in x["_sezioni"]]
        v_cands.append({"uid": x["uid"], "_date": vd[0], "_origine": "comunicazione", "_hash": x["fonte_record"]["content_hash"],
                        "_cite": _cite(x["fonte_record"]), "_text": x["dati"]["testo"], "_full": full_text.get(x["uid"]),
                        "_stato": f["stato"], "_ev": {"funzione_verifica": f, "date": x["interpretazione"]["date"]},
                        "_extra": {"origine": "comunicazione_m6", "comm_id": x["comm_id"], "stato_m6": f["stato"], "regola_m6": f["regola"]},
                        "_fresh": fk(*kinds),
                        "_fatti": {"anno_scolastico": _sy(vd[0]), "anno_comunicazione": x["interpretazione"]["perimetro"]["anno"]}})
    # unione: stesso uid oppure collegamento M5 stessa_scadenza nello stato massimo realmente prodotto da M5
    merged, merges, by_uid = [], [], {}
    for c in sorted(v_cands, key=lambda c: (0 if c["_origine"] == "agenda" else 1, c["uid"])):
        if c["uid"] in by_uid:
            by_uid[c["uid"]].setdefault("_uniti", []).append({"uid": c["uid"], "origine": c["_extra"]["origine"], "regola": "stesso_uid"})
            merges.append(("stesso_uid", c["uid"], c["uid"], None))
            continue
        by_uid[c["uid"]] = c
        merged.append(c)
    idx = {c["uid"]: c for c in merged}
    absorbed = set()
    for x in res5["collegamenti"]:
        ua, ub = x["a"]["uid"], x["b"]["uid"]
        if ua not in idx or ub not in idx or ua == ub or ua in absorbed or ub in absorbed:
            continue
        if x["relazione"] == MERGE_RELATION and x["stato"] == MERGE_STATE:
            keep, drop = (idx[ua], idx[ub]) if (idx[ua]["_origine"], ua) <= (idx[ub]["_origine"], ub) else (idx[ub], idx[ua])
            keep.setdefault("_uniti", []).append({"uid": drop["uid"], "origine": drop["_extra"]["origine"],
                                                  "regola": "m5_%s_%s" % (MERGE_RELATION, MERGE_STATE), "link_id": x["link_id"]})
            absorbed.add(drop["uid"])
            merges.append(("m5", keep["uid"], drop["uid"], x["link_id"]))
        else:
            amb.append({"codice": "possibile_stessa_verifica", "sezione": "verifiche",
                        "messaggio": "collegamento M5 '%s' (%s) tra due verifiche: NON unite (unione solo con '%s' '%s'), restano distinte" % (
                            x["relazione"], x["stato"], MERGE_RELATION, MERGE_STATE),
                        "uid": sorted([ua, ub]), "link_id": x["link_id"], "relazione": x["relazione"], "stato_m5": x["stato"]})
    merged = [c for c in merged if c["uid"] not in absorbed]

    def mk_v(c):
        def mk(detail):
            extra = dict(c["_extra"])
            if c.get("_uniti"):
                extra["uniti"] = c["_uniti"]
            return _el(c["uid"], c["_hash"], c["_cite"], c["_date"], c["_text"], extra, detail, evid=c["_ev"], fresh=c["_fresh"],
                       full_text=c.get("_full") if detail else None)
        return mk
    sec_rows["verifiche"] = _future_then_past([{"uid": c["uid"], "_date": c["_date"], "_c": c, "_mk": mk_v(c),
                                                "_fatti": dict(c["_fatti"], origine=c["_origine"], stato=c["_stato"],
                                                               uniti=list(c.get("_uniti", [])))} for c in merged],
                                              td, lambda r: r["_date"])
    if no_date:
        _gap(gaps, "verifica_senza_data_utilizzabile", "verifiche",
             "comunicazioni M6 della materia con funzione verifica ma senza una data esplicita attribuita alla verifica: non incluse "
             "tra le verifiche e mai prossimi impegni", no_date, con_riferimenti_relativi=len(rel_v))
    only_ann = [c["uid"] for c in merged if c["_origine"] == "comunicazione" and not any(
        u["origine"] == "agenda_classeviva" for u in c.get("_uniti", []))]
    if only_ann:
        _gap(gaps, "verifica_solo_da_annuncio", "verifiche",
             "verifiche note solo da un annuncio Classroom (nessun evento di agenda ClasseViva unito): interpretazione M6, non un fatto",
             only_ann)

    # ================================================================================================ COMPITI
    c_from = _minus(today, WINDOWS["compiti_scaduti_giorni"])
    t_rows, t_poss, no_due, shared_nt = [], [], [], []
    for r in age["compiti"]:
        d = r["dati"]
        if (d["data"] or "") < c_from:
            continue
        extra = {"origine": "agenda_classeviva", "scadenza": d["data"], "stato_consegna": None, "docente": d["docente"],
                 "attribuzione": {"stato": r["interpretazione"]["stato"], "criterio": r["interpretazione"]["criterio"]}}
        t_rows.append({"uid": r["uid"], "_date": d["data"], "_prossimo": (d["data"] or "") >= td, "_text": d["testo"],
                       "_fatti": {"anno_scolastico": _sy(d["data"]), "origine": "agenda_classeviva",
                                  "confidenza_m2": r["interpretazione"]["confidenza"]},
                       "_mk": lambda detail, r=r, d=d, extra=extra: _el(r["uid"], r["fonte"]["content_hash"], _cite(r["fonte"]), d["data"],
                                                                        d["testo"], extra, detail, evid=r["interpretazione"],
                                                                        fresh=fk((_CV, "agenda_event")))})
    for v in l3["work_views"]:
        cw = v["provenienza"]["course_work"] or {}
        if v["derivato"]["perimetro"]["valore"] != "operativo" or cw.get("stato_registro") not in (None, R.PRESENT):
            continue
        cid = v["fonte"]["corso"]["id"]
        if cid not in confirmed:
            continue
        tname = topics.get((cid, v["fonte"]["compito"]["topic_id"])) if v["fonte"]["compito"]["topic_id"] else None
        s = L.item_subject(confirmed[cid], tname)
        mine = s["stato"] == "determinata" and s["key"] == key
        maybe = s["stato"] != "determinata" and key in s["candidati"]
        if not (mine or maybe):
            continue
        st, sc = v["derivato"]["stato_operativo"], v["derivato"]["scadenza"]
        if st == "senza_scadenza" and mine:
            no_due.append(v["uid"])
        keep = st == "aperto" or (st == "scaduto_non_consegnato" and (sc["data_locale"] or "") >= c_from)
        if not keep:
            continue
        cons = v["fonte"]["consegna"]
        extra = {"origine": "classroom", "corso": v["fonte"]["corso"]["nome"], "stato_operativo": st,
                 "scadenza": sc["data_locale"], "scadenza_precisione": sc["precisione"], "stato_consegna": cons["stato_fonte"]}
        if sc["momento_locale"]:
            extra["scadenza_momento"] = sc["momento_locale"]
        if mine:
            extra["attribuzione"] = {"stato": s["stato"], "criterio": s["criterio"]}
        else:
            extra.update(motivo="corso_condiviso_senza_topic", candidati=s["candidati"], criterio=s["criterio"])
            shared_nt.append(v["uid"])
        evid = {"motivo_stato": v["derivato"]["motivo_stato"], "note_stato": v["derivato"]["note_stato"], "materia": s,
                "derive_version": v["derive_version"], "consegna": _cite(v["provenienza"]["submission"])}
        row = {"uid": v["uid"], "_date": sc["data_locale"], "_prossimo": st == "aperto" and (sc["data_locale"] or "") >= td,
               "_text": v["fonte"]["compito"]["titolo"], "_tipo": "compito_classroom",
               "_fatti": {"anno_scolastico": v["derivato"]["perimetro"]["anno"], "origine": "classroom", "stato_operativo": st,
                          "late": cons["late"], "topic": tname, "corso_id": cid},
               "_mk": lambda detail, v=v, extra=extra, evid=evid, cw=cw: _el(
                   v["uid"], cw.get("content_hash"), _cite(cw), v["derivato"]["scadenza"]["data_locale"],
                   v["fonte"]["compito"]["titolo"], extra, detail, evid=evid,
                   fresh=fk((_CLS, "course_work"), (_CLS, "submission"), (_CLS, "course")),
                   full_text="\n".join(t for t in (v["fonte"]["compito"]["titolo"], v["fonte"]["compito"]["descrizione"]) if t)
                   if detail else None)}
        (t_rows if mine else t_poss).append(row)
    sec_rows["compiti"] = _future_then_past(t_rows, td, lambda r: r["_date"])
    if no_due:
        _gap(gaps, "compito_senza_scadenza", "compiti",
             "compiti Classroom della materia senza scadenza: NON inclusi tra i compiti aperti (definizione M4) e nessuna scadenza "
             "inventata; 'senza_scadenza' non significa 'da fare'", no_due)

    # ================================================================================================ COMUNICAZIONI
    rows, rel_c, pari, disc_dc = [], [], [], []
    for x in oper6:
        m = x["interpretazione"]["materia"]
        if m["key"] != key or m["stato"] != "determinata":
            continue
        i = x["interpretazione"]
        if i["riferimenti_relativi_non_risolti"]:
            rel_c.append(x["uid"])
        if i["classe_principale"]["pari_livello"]:
            pari.append(x["uid"])
        if any(c["tipo"] == "date_discordanti" for c in i["conflitti"]):
            disc_dc.append(x["uid"])
        cp = i["classe_principale"]
        extra = {"comm_id": x["comm_id"], "stato": x["stato"],
                 "classe_principale": {k: v for k, v in cp.items() if v is not None and v != []},
                 "funzioni": ["%s:%s" % (f["funzione"], f["stato"]) for f in i["funzioni"]]}
        if i.get("caso_speciale"):
            extra["caso_speciale"] = i["caso_speciale"]["codice"]
        if x["dati"]["materiali"]:
            extra["materiali"] = x["dati"]["materiali"]
        kinds = [tuple(s) for s in x["_sezioni"]]
        rows.append({"uid": x["uid"], "_date": x["data"],
                     "_fatti": {"anno_scolastico": i["perimetro"]["anno"], "stato": x["stato"], "classe_principale": cp["valore"],
                                "stato_classe": cp["stato"], "funzioni": [(f["funzione"], f["stato"]) for f in i["funzioni"]]},
                     "_mk": lambda detail, x=x, extra=extra, kinds=kinds: _el(
                         x["uid"], x["fonte_record"]["content_hash"], _cite(x["fonte_record"]), x["data"], x["dati"]["testo"], extra,
                         detail, evid={k: v for k, v in x["interpretazione"].items() if k != "comm_version"}, fresh=fk(*kinds),
                         full_text=full_text.get(x["uid"]) if detail else None)})
    sec_rows["comunicazioni"] = _desc(rows, lambda r: r["_date"])
    if rel_c:
        amb.append(dict({"codice": "riferimenti_relativi_non_risolti", "sezione": "comunicazioni",
                         "messaggio": "comunicazioni con riferimenti relativi ('per lunedi', 'domani'): rilevati da M6 e NON risolti in data"},
                        **_uids(rel_c)))
    if pari:
        amb.append(dict({"codice": "pari_livello", "sezione": "comunicazioni",
                         "messaggio": "comunicazioni con piu' classi allo stesso livello (M6 RN2): nessuna classe principale scelta"},
                        **_uids(pari)))
    if disc_d or disc_dc:
        amb.append(dict({"codice": "discordanza_date", "sezione": "verifiche",
                         "messaggio": "comunicazioni con date diverse attribuite alla verifica (M6 RC2): nessuna data scelta, non incluse "
                                      "tra le verifiche"}, **_uids(disc_d + disc_dc)))

    # ================================================================================================ COLLEGAMENTI
    def side(s):
        dd = s["dati"]
        return {"uid": s["uid"], "tipo": s["tipo"], "data": dd.get("data") or dd.get("data_creazione"),
                "materia": s["interpretazione"]["materia"], "stato_materia": s["interpretazione"]["stato_materia"]}

    def short(s):
        dd = s["dati"]
        return (dd.get("testo") or dd.get("argomento") or dd.get("titolo") or "").strip()
    rank5 = {"probabile": 0, "proposto": 1, "conflitto": 2, "non_determinabile": 3}
    rows = []
    for x in res5["collegamenti"] + res5["non_determinabili"]:
        if key not in (x["a"]["interpretazione"]["materia"], x["b"]["interpretazione"]["materia"]):
            continue
        uid = x.get("link_id") or "nd:" + CM.short_hash(res5["link_version"], x["relazione"], x["a"]["uid"], x["a"]["fonte"]["content_hash"],
                                                        x["b"]["uid"], x["b"]["fonte"]["content_hash"])
        h = _pair_hash(x["a"]["fonte"]["content_hash"], x["b"]["fonte"]["content_hash"])
        extra = {"tipo": "collegamento" if "link_id" in x else "non_determinabile", "relazione": x["relazione"], "stato": x["stato"],
                 "regola": x["interpretazione"]["regola"], "a": side(x["a"]), "b": side(x["b"])}
        rows.append({"uid": uid, "_date": extra["a"]["data"], "_rank": rank5[x["stato"]],
                     "_fatti": {"tipo": extra["tipo"], "relazione": x["relazione"], "stato": x["stato"],
                                "lati": [x["a"]["uid"], x["b"]["uid"]]},
                     "_mk": lambda detail, x=x, uid=uid, h=h, extra=extra: _el(
                         uid, h, {"a": _cite(x["a"]["fonte"]), "b": _cite(x["b"]["fonte"])}, extra["a"]["data"],
                         "%s <-> %s" % (short(x["a"]), short(x["b"])), extra, detail,
                         evid=x["interpretazione"], fresh=fk(*SECTION_KINDS["collegamenti"]))})
    confl_amb = {"candidati_multipli": [], "data_discordante": [], "segnali_discordanti": []}
    for x in res5["conflitti"]:
        sides = [x["elemento"]] + [c["lato"] for c in x["candidati"]]
        if key not in [s["interpretazione"]["materia"] for s in sides]:
            continue
        h = _pair_hash(*[s["fonte"]["content_hash"] for s in sides])
        confl_amb.setdefault(x["tipo"], []).append(x["conflitto_id"])
        extra = {"tipo": "conflitto", "relazione": x["relazione"], "stato": x["stato"], "regola": x["interpretazione"]["regola"],
                 "conflitto": x["tipo"], "elemento": side(x["elemento"]), "candidati": [side(c["lato"]) for c in x["candidati"]]}
        if x.get("valori"):
            extra["valori"] = x["valori"]
        rows.append({"uid": x["conflitto_id"], "_date": extra["elemento"]["data"], "_rank": rank5["conflitto"],
                     "_fatti": {"tipo": "conflitto", "relazione": x["relazione"], "stato": x["stato"], "conflitto": x["tipo"],
                                "lati": [s["uid"] for s in sides]},
                     "_mk": lambda detail, x=x, h=h, extra=extra, sides=sides: _el(
                         x["conflitto_id"], h, {"elemento": _cite(x["elemento"]["fonte"]),
                                                "candidati": [_cite(c["lato"]["fonte"]) for c in x["candidati"]]},
                         extra["elemento"]["data"], x["interpretazione"]["motivazione"], extra, detail,
                         evid=x["interpretazione"], fresh=fk(*SECTION_KINDS["collegamenti"]))})
    rows = _desc(rows, lambda r: r["_date"])
    rows.sort(key=lambda r: r["_rank"])
    sec_rows["collegamenti"] = rows
    if confl_amb["candidati_multipli"]:
        amb.append(dict({"codice": "pari_livello", "sezione": "collegamenti",
                         "messaggio": "conflitti M5 'candidati_multipli': piu' candidati con la stessa forza di evidenza, nessuno scelto"},
                        **_uids(confl_amb["candidati_multipli"])))
    dd5 = confl_amb["data_discordante"] + confl_amb["segnali_discordanti"]
    if dd5:
        amb.append(dict({"codice": "discordanza_date", "sezione": "collegamenti",
                         "messaggio": "conflitti M5 di data (data_discordante/segnali_discordanti): le fonti indicano date diverse, nessuna scelta"},
                        **_uids(dd5)))

    # ================================================================================================ POSSIBILI
    p_rows = []
    for r in lez["possibili_da_verificare"]:
        d = r["dati"]
        extra = {"tipo": "lezione", "motivo": "attribuzione_m2_da_verificare", "candidati": r["interpretazione"]["candidati"],
                 "criterio": r["interpretazione"]["criterio"]}
        p_rows.append({"uid": r["uid"], "_date": d["data"],
                       "_mk": lambda detail, r=r, d=d, extra=extra: _el(r["uid"], r["fonte"]["content_hash"], _cite(r["fonte"]), d["data"],
                                                                        d["argomento"], extra, detail, evid=r["interpretazione"],
                                                                        fresh=fk((_CV, "lesson")))})
    for r in age["possibili_da_verificare"]:
        d = r["dati"]
        extra = {"tipo": "agenda_event", "tipo_dedotto": r["interpretazione"]["tipo_dedotto"], "motivo": "attribuzione_m2_da_verificare",
                 "candidati": r["interpretazione"]["candidati"], "criterio": r["interpretazione"]["criterio"]}
        p_rows.append({"uid": r["uid"], "_date": d["data"],
                       "_mk": lambda detail, r=r, d=d, extra=extra: _el(r["uid"], r["fonte"]["content_hash"], _cite(r["fonte"]), d["data"],
                                                                        d["testo"], extra, detail, evid=r["interpretazione"],
                                                                        fresh=fk((_CV, "agenda_event")))})
    for r in t_poss:
        r = dict(r)
        p_rows.append(r)
    report = CS.classify([reg.get_record(_CLS, "course", f["source_uid"])
                          for f in reg.list_records(source=_CLS, kind="course", status=R.PRESENT)], subjects, manual)
    proposed = [cid for cid in CS.proposed_for(report, key) if cid not in my_courses]
    crow = {r["course_id"]: r for r in report["corsi"]}
    confl_m = []
    for x in oper6:
        m = x["interpretazione"]["materia"]
        if m["stato"] == "determinata":
            continue
        cid = (x["dati"]["corso"] or {}).get("id")
        cand = set(m.get("candidati") or []) | set(m.get("menzionate_nel_testo") or [])
        if m["stato"] == "conflitto" and (key in cand or m["key"] == key):
            motivo = "conflitto_materia"
            confl_m.append(x["uid"])
        elif m["stato"] == "da_verificare" and key in (m.get("candidati") or []):
            motivo = "corso_condiviso_senza_topic"
            shared_nt.append(x["uid"])
        elif cid in proposed:
            motivo = "corso_non_confermato"
        else:
            continue
        extra = {"tipo": "comunicazione", "motivo": motivo, "candidati": sorted(cand) if cand else [],
                 "criterio": m.get("criterio"), "comm_id": x["comm_id"], "stato_m6": x["stato"],
                 "corso": (x["dati"]["corso"] or {}).get("nome")}
        kinds = [tuple(s) for s in x["_sezioni"]]
        p_rows.append({"uid": x["uid"], "_date": x["data"],
                       "_mk": lambda detail, x=x, extra=extra, kinds=kinds: _el(
                           x["uid"], x["fonte_record"]["content_hash"], _cite(x["fonte_record"]), x["data"], x["dati"]["testo"], extra,
                           detail, evid={k: v for k, v in x["interpretazione"].items() if k != "comm_version"}, fresh=fk(*kinds),
                           full_text=full_text.get(x["uid"]) if detail else None)})
    for cid in proposed:
        c = reg.get_record(_CLS, "course", cid)
        if c is None:
            continue
        r = crow.get(cid, {})
        cite = {"uid": "cls:" + cid, "registry_version": c["version"], "content_hash": c["content_hash"], "source_status": c["source_status"]}
        extra = {"tipo": "corso_classroom", "motivo": "corso_non_confermato", "candidati": r.get("materie_proposte") or [],
                 "criterio": r.get("motivazione"), "categoria": r.get("categoria")}
        p_rows.append({"uid": "cls:" + cid, "_date": None,
                       "_mk": lambda detail, c=c, cite=cite, extra=extra, r=r: _el(
                           cite["uid"], c["content_hash"], cite, None, c["raw"].get("name"), extra, detail,
                           evid={"evidenze": r.get("evidenze")}, fresh=fk((_CLS, "course")))})
    sec_rows["possibili_da_verificare"] = _desc(p_rows, lambda r: r["_date"])
    if shared_nt:
        _gap(gaps, "corso_condiviso_senza_topic", "possibili_da_verificare",
             "elementi di corsi Classroom CONDIVISI tra piu' materie senza topic che individui la materia: restano possibili, mai "
             "attribuiti", shared_nt)
    if not my_courses or proposed:
        msg = []
        if not my_courses:
            msg.append("nessun corso Classroom CONFERMATO per questa materia (conferme solo in school_manual.json)")
        if proposed:
            msg.append("%d corsi PROPOSTI e non confermati: %s" % (len(proposed), ", ".join(proposed)))
        _gap(gaps, "corso_non_confermato", "possibili_da_verificare", "; ".join(msg), corsi_proposti=proposed)
    if confl_m:
        amb.append(dict({"codice": "conflitto_materia", "sezione": "possibili_da_verificare",
                         "messaggio": "comunicazioni con materia in conflitto (M6 RC1: il testo nomina un'altra materia): nessuna scelta"},
                        **_uids(confl_m)))

    # materiali e consegne non coperti da M7 (nessuna sezione): dichiarati, mai attribuiti
    n_mat = sum(1 for f in reg.list_records(source=_CLS, kind="material", status=R.PRESENT) if f["source_uid"].split("/")[0] in my_courses)
    n_prop = sum(1 for k in ("course_work", "material") for f in reg.list_records(source=_CLS, kind=k, status=R.PRESENT)
                 if f["source_uid"].split("/")[0] in proposed)
    if n_mat or n_prop:
        _gap(gaps, "possibili_non_comunicazioni_non_coperti", "possibili_da_verificare",
             "elementi Classroom che non sono comunicazioni e non sono coperti da questo contesto: %d materiali dei corsi confermati "
             "(usa school_materials) e %d compiti/materiali di corsi proposti non confermati (non considerati)" % (n_mat, n_prop),
             materiali_corsi_confermati=n_mat, elementi_corsi_proposti=n_prop)

    # ================================================================================================ GENERALI
    g_from = _minus(today, WINDOWS["generali_giorni"])
    rows, no_text, g_excl = [], [], {}
    for x in oper6:
        if x["interpretazione"]["ambito"]["valore"] != "generale":
            continue
        val = x["dati"]["validita_nativa"] or {}
        vs = val.get("stato")
        if vs in ("scaduta", "fuori_intervallo_nativo"):
            g_excl[vs] = g_excl.get(vs, 0) + 1
            continue
        if not (x["data"] >= g_from or (val.get("al") and val["al"] >= td)):
            g_excl["oltre_finestra"] = g_excl.get("oltre_finestra", 0) + 1
            continue
        if not (x["dati"]["titolo"] or "").strip():
            no_text.append(x["uid"])
        cp = x["interpretazione"]["classe_principale"]
        extra = {"numero": x["dati"]["numero"], "categoria": x["dati"]["categoria_scuola"],
                 "validita": {"stato": vs, "al": val.get("al")}, "classe_principale": cp["valore"], "stato_m6": x["stato"],
                 "allegati": x["dati"]["materiali"]}
        rows.append({"uid": x["uid"], "_date": x["data"],
                     "_mk": lambda detail, x=x, extra=extra: _el(
                         x["uid"], x["fonte_record"]["content_hash"], _cite(x["fonte_record"]), x["data"], x["dati"]["titolo"], extra,
                         detail, evid={k: v for k, v in x["interpretazione"].items() if k != "comm_version"},
                         fresh=fk((_CV, "notice")))})
    sec_rows["generali"] = _desc(rows, lambda r: r["_date"])
    if no_text:
        _gap(gaps, "circolare_senza_testo", "generali", "circolari senza titolo leggibile: nessun testo inventato", no_text)

    # ================================================================================================ PROSSIMI IMPEGNI (completi)
    pi = []
    for c in merged:
        if c["_date"] and c["_date"] >= td and (c["_origine"] == "agenda" or c["_stato"] == "probabile"):
            pi.append({"tipo": "verifica", "data": c["_date"], "uid": c["uid"], "hash": c["_hash"][:12],
                       "origine": c["_extra"]["origine"], "stato": c["_stato"], "testo": CM.cap_text(c["_text"], COMPACT_TEXT)[0] or None})
    for r in t_rows:
        if r["_prossimo"] and r["_date"]:
            e = r["_mk"](False)
            pi.append({"tipo": "compito", "data": r["_date"], "uid": r["uid"], "hash": e["hash"], "origine": e["origine"],
                       "stato": e.get("stato_operativo") or "determinata (M2)", "testo": e["testo"]})
    pi.sort(key=lambda p: (p["data"], p["tipo"], p["uid"]))

    return {"chiave": key, "nome_ufficiale": desc, "docenti": teachers, "now": now_dt, "data_riferimento": td,
            "finestre": dict(WINDOWS, verifiche_dal=v_from, compiti_scaduti_dal=c_from, generali_dal=g_from),
            "candidati": sec_rows, "prossimi_impegni": pi, "unioni": merges, "generali_esclusi": g_excl,
            "gap": gaps, "ambiguita": amb, "conflitti_n": len(confl_m) + sum(len(v) for v in confl_amb.values()),
            "freshness": fresh, "freshness_sezioni": fsec, "manuale_presente": bool(manual), "corsi_confermati": my_courses,
            "corsi_proposti": proposed, "link_version": res5["link_version"], "schema_registro": R._version_of(reg.db),
            "registro_migrato_in_memoria": bool(getattr(reg, "migrated_in_memory", False))}


def render(c, dettaglio=False, limiti=None, source_path=None):
    """M7.1 - RENDERING del contratto pubblico cloud.school.context/4 da una raccolta `collect()`: limiti, livello di dettaglio,
    gap/warning che dipendono da limiti e freschezza. Output identico byte per byte a quello di M7."""
    lim, lim_notes = _limits(limiti)
    det = set(_detail_set(dettaglio))
    sec_rows, fsec, fresh, pi, merges = c["candidati"], c["freshness_sezioni"], c["freshness"], c["prossimi_impegni"], c["unioni"]
    gaps, amb, warn = [dict(g) for g in c["gap"]], [dict(a) for a in c["ambiguita"]], []
    key, desc, teachers, td, now_dt = c["chiave"], c["nome_ufficiale"], c["docenti"], c["data_riferimento"], c["now"]
    g_excl = c["generali_esclusi"]

    # ================================================================================================ SEZIONI + LIMITI
    sezioni, contatori, mostrati = {}, {}, {}
    for name in SECTIONS:
        rows = sec_rows[name]
        n = lim[name]
        shown = rows[:n]
        detail = name in det
        contatori[name], mostrati[name] = len(rows), len(shown)
        f = fsec[name]
        s = {"regola": SECTION_RULES[name], "dettaglio": detail, "totale": len(rows), "mostrati": len(shown),
             "troncato": len(rows) > len(shown), "freschezza": f["stato"], "elementi": [r["_mk"](detail) for r in shown]}
        if detail:
            s["freschezza_dettaglio"] = f
        if name == "verifiche":
            s["unioni"] = {"n": len(merges), "regola": "stesso uid oppure M5 '%s' nello stato '%s'" % (MERGE_RELATION, MERGE_STATE)}
        if name == "generali":
            s["esclusi"] = dict(sorted(g_excl.items()))
            s["nota"] = ("circolari: solo titolo, categoria e allegati (nessun testo integrale); ambito generale, NON della materia")
        sezioni[name] = s
        if s["troncato"]:
            _gap(gaps, "sezione_troncata", name, "%s: mostrati %d di %d (aumenta limiti.%s, max %d, o usa gli strumenti M4/M5/M6)" % (
                name, len(shown), len(rows), name, LIMIT_MAX), mostrati=len(shown), totale=len(rows))
            C3._truncation_warning(warn, name, name, {"troncato": True, "inclusi": len(shown), "totale": len(rows),
                                                      "omessi": len(rows) - len(shown)})
        if f["stato"] != "aggiornato":
            _gap(gaps, "sezione_non_fresca", name, "sezione %s: freschezza '%s' (kind: %s)" % (
                name, f["stato"], ", ".join(f["kind_senza_lettura"] + f["kind_stale"])), freschezza=f["stato"])
        C3._freshness_warnings(warn, name, f)

    # ================================================================================================ PROSSIMI IMPEGNI
    contatori["prossimi_impegni"] = len(pi)
    mostrati["prossimi_impegni"] = min(len(pi), lim["prossimi_impegni"])
    if len(pi) > lim["prossimi_impegni"]:
        _gap(gaps, "sezione_troncata", "prossimi_impegni", "prossimi_impegni: mostrati %d di %d" % (lim["prossimi_impegni"], len(pi)),
             mostrati=lim["prossimi_impegni"], totale=len(pi))

    for k, v in lim_notes:
        C3._w(warn, "limite_normalizzato", "info", k, "limite %s=%d oltre il massimo: normalizzato a %d" % (k, v, LIMIT_MAX))
    if not teachers:
        _gap(gaps, "docente_non_disponibile", "materia", "nessun docente nell'elenco materie ClasseViva (/subjects) per questa materia: "
                                                         "non dedotto da lezioni o testi")
    if not c["manuale_presente"]:
        C3._w(warn, "livello_manuale_assente", "attenzione", "materia", "school_manual.json assente o vuoto: nessun corso Classroom confermato")
    n_confl = c["conflitti_n"]
    if n_confl:
        C3._w(warn, "conflitti_presenti", "attenzione", "contesto",
              "%d conflitti (M5/M6) che toccano la materia: nessuna scelta, nessuna fonte modificata" % n_confl)
    gaps.sort(key=lambda g: (GAP_CODES.index(g["codice"]), SECTIONS.index(g["sezione"]) if g["sezione"] in SECTIONS else -1))
    amb.sort(key=lambda a: (AMBIGUITY_CODES.index(a["codice"]), a["sezione"], a.get("uid", [])))

    modo = "compatto" if not det else ("dettaglio" if len(det) == len(SECTIONS) else "selettivo")
    return {
        "schema": SCHEMA,
        "natura": NATURA,
        "materia": {"chiave": key, "nome_ufficiale": desc,
                    "docenti": {"valori": teachers, "fonte": "ClasseViva /subjects (elenco canonico materie-docenti)"} if teachers
                    else {"valori": [], "stato": "non_disponibile"}},
        "riferimento_temporale": {"now": now_dt.isoformat(), "data_riferimento": td, "fuso": D.LOCAL_TZ,
                                  "finestre": c["finestre"],
                                  "nota": "now letto UNA volta dal chiamante e usato da tutte le sezioni"},
        "livelli": {"modo": modo, "sezioni_in_dettaglio": [s for s in SECTIONS if s in det], "limiti": lim, "limite_massimo": LIMIT_MAX,
                    "testo_compatto_max": COMPACT_TEXT,
                    "compatto": "uid, hash (12 caratteri del content_hash), data, testo <= %d caratteri, campi essenziali" % COMPACT_TEXT,
                    "dettaglio": "+ content_hash completo, registry_version, fonte_record, testo completo, evidenze, freschezza per elemento"},
        "sintesi": {"prossimi_impegni": pi[:lim["prossimi_impegni"]], "contatori": contatori, "mostrati": mostrati,
                    "verifiche_unite": len(merges),
                    "nota": "contatori = candidati totali dopo regole e finestre, PRIMA dei limiti; mostrati = elementi nel documento"},
        "sezioni": sezioni,
        "freshness": {"calcolata_a": fresh["calcolato_a"], "soglia_ore": fresh["soglia_ore"],
                      "per_sezione": {n: fsec[n]["stato"] for n in SECTIONS}, "qualcosa_stale": fresh["qualcosa_stale"],
                      "nota": C3.FRESH_NOTE},
        "gap": gaps,
        "ambiguita": amb,
        "warning": warn,
        "provenance": {"schema": SCHEMA, "context_version": context_version(), "comm_version": M.comm_version(),
                       "comm_rules_rev": M.COMM_RULES_REV, "link_version": c["link_version"], "attr_version": CA.attr_version(),
                       "derive_version": D.DERIVE_VERSION, "schema_registro": c["schema_registro"],
                       "registro_migrato_in_memoria": c["registro_migrato_in_memoria"], "sorgente": source_path,
                       "livello_manuale_presente": c["manuale_presente"], "corsi_confermati": c["corsi_confermati"],
                       "strati_riusati": ["M2 cvv_attribution", "M4 school_context3", "L3 cls_derive", "M5 cvv_cls_links", "M6 cvv_comms"]},
        "regole_agente": RULES_FOR_AGENTS,
    }


def build(reg, materia, now_dt, manual=None, dettaglio=False, limiti=None, soglia_ore=48, *, subjects, source_path=None):
    """cloud.school.context/4 per UNA delle 8 materie = render(collect(...)). Sola lettura. `now_dt`: datetime con fuso (o ISO con
    offset), letto UNA volta dal chiamante e usato da tutte le sezioni. `subjects`: elenco materie ClasseViva (build_subjects),
    necessario a M2/M4/M5/M6. Parametri non validi -> ValueError PRIMA di leggere il registro."""
    _prepare(materia, now_dt, soglia_ore, subjects)
    _limits(limiti)
    _detail_set(dettaglio)
    return render(collect(reg, materia, now_dt, manual=manual, soglia_ore=soglia_ore, subjects=subjects),
                  dettaglio=dettaglio, limiti=limiti, source_path=source_path)


def build_from_path(path, materia, now, manual_path=None, **kw):
    """Registro aperto come COPIA in memoria (Registry.from_snapshot, immutable=1): nessun byte del file viene scritto."""
    reg = R.Registry.from_snapshot(path)
    try:
        return build(reg, materia, now, manual=SC.load_manual(manual_path)["dati"], source_path=os.path.abspath(path), **kw)
    finally:
        reg.close()
