"""REGISTRO VALUTATIVO E PRESENZE (M14 v1): `cloud.school.records/1`. Modulo PURO e SOLA LETTURA.

Fatti e statistiche DESCRITTIVE su voti e assenze, accanto ai layer di contesto (non sopra M8): nessun giudizio, nessun rendimento,
nessuna previsione. Riusa la normalizzazione esistente (`cvv_registry.normalized` -> `cvv_client.normalize_grade` /
`normalize_absence`, interpretazioni SOLO da tabelle verificate) e la freschezza M3; non duplica logica di M1-M13.

Voti:
  * dati grezzi verbatim (`raw`), `decimalValue`, `weightFactor`, componente, periodo, annullato: nessun valore dedotto, salvo i
    giudizi senza numero (Religione: «dc», «ottimo»…) convertiti con la tabella GIUDIZI (decisione del titolare, 15/11) e dichiarati
    in `valore_da_giudizio`;
  * media complessiva SENZA i voti `fuori_media`: giudizi convertiti e voti con `noAverage` di ClasseViva (come fa la scuola);
  * inclusione dichiarata per ogni voto: `incluso` | `escluso_annullato` | `escluso_non_numerico`;
  * medie per POLITICA nominata (elenco chiuso, versionato): `media_pesata_weightfactor` (primaria proposta) e
    `media_aritmetica_semplice`. Ogni media porta formula, uid usati, pesi assunti, arrotondamento. La corrispondenza con la media
    mostrata da ClasseViva e' `non_verificata`: MAI chiamarla "la tua media". Nuove politiche si aggiungono senza toccare i dati;
  * stati distinti: fonte non letta | nessun voto (fonte letta) | solo voti esclusi | media calcolabile | media non calcolabile.
Assenze: eventi verbatim + tipo dalle tabelle verificate (non verificato -> 'Da verificare'), conteggi per tipo e per mese.
Anno operativo = anno scolastico di `now`; gli altri anni sono solo `storico` (conteggi). Stesso input -> stesso JSON; nessun orologio;
nessuna scrittura.
"""
from __future__ import annotations

import collections
import datetime as dt
import os

import cls_derive as D
import cvv_client as cv
import cvv_registry as CR
import cvv_subject as S
import it_dates as ID
import registry as R
import school_context as SC
import school_context3 as C3

SCHEMA = "cloud.school.records/1"
RECORDS_REV = 1
ROUND = 2

INCLUSION = ("incluso", "escluso_annullato", "escluso_non_numerico")
SOURCE_STATES = ("presente", "vuoto_valido", "non_letto")
SCOPE_STATES = ("media_calcolabile", "media_non_calcolabile", "nessun_voto", "fonte_non_letta")
POLICIES = {
    "media_pesata_weightfactor": {
        "formula": "somma(decimalValue * w) / somma(w) sui voti inclusi; w = weightFactor se numerico e >= 0, altrimenti 1 "
                   "(peso assunto, dichiarato in pesi_assunti); non calcolabile se nessun voto incluso o somma(w) = 0",
        "arrotondamento": "%d decimali (valore esatto in valore_esatto)" % ROUND,
        "corrispondenza_classeviva": "non_verificata"},
    "media_aritmetica_semplice": {
        "formula": "somma(decimalValue) / n sui voti inclusi; non calcolabile se nessun voto incluso",
        "arrotondamento": "%d decimali (valore esatto in valore_esatto)" % ROUND,
        "corrispondenza_classeviva": "non_verificata"},
}
PRIMARY_POLICY = "media_pesata_weightfactor"
EXCLUSION_RULES = {"escluso_annullato": "raw.canceled vero",
                   "escluso_non_numerico": "decimalValue assente o non numerico e displayValue non in GIUDIZI"}
# Giudizi senza numero (Religione) -> voto, scala decisa dal titolare il 15/11; si confronta displayValue in minuscolo.
GIUDIZI = {**dict.fromkeys(("o", "ott", "ottimo"), 10), **dict.fromkeys(("ds", "dist", "distinto"), 9),
           **dict.fromkeys(("b", "buono"), 8), **dict.fromkeys(("dc", "disc", "discreto"), 7),
           **dict.fromkeys(("s", "suff", "sufficiente"), 6), **dict.fromkeys(("m", "med", "mediocre"), 5.5),
           **dict.fromkeys(("i", "ins", "insufficiente", "ns", "non sufficiente"), 5)}
BINS = tuple("[%d,%d)" % (k, k + 1) for k in range(9)) + ("[9,10]",)
FORBIDDEN_WORDS = ("bravo", "brava", "scarso", "scarsa", "insufficiente", "sufficiente", "ottimo", "pessimo", "rendimento basso",
                   "rendimento alto", "in calo", "in miglioramento", "peggiorato", "peggiorata", "migliorato", "migliorata",
                   "la tua media", "sei pronto", "sei indietro", "voto atteso")
NATURA = ("fatti e statistiche DESCRITTIVE su voti e assenze in sola lettura: nessun giudizio, rendimento, previsione o confronto; "
          "le medie sono politiche nominate con formula esplicita, corrispondenza con ClasseViva non verificata")
RULES_FOR_AGENTS = [
    "Chiama ogni media con il nome della sua politica e la sua formula, senza presentarla come media ufficiale dello studente: "
    "la corrispondenza con ClasseViva non e' verificata.",
    "Distingui sempre: fonte non letta, nessun voto, voti solo esclusi, media calcolabile, media non calcolabile.",
    "Non trasformare un dato mancante in zero e non dedurre voti, pesi o tipi non presenti nella fonte.",
    "Nessun giudizio sul rendimento, nessuna tendenza interpretata, nessuna previsione: riporta numeri e formule.",
    "Le assenze sono eventi della fonte: il tipo vale solo se in tabella verificata; il resto e' 'Da verificare'.",
    "Lo storico (altri anni) e' separato: non mescolarlo all'anno operativo.",
]


def records_version():
    return cv.interp_version(RECORDS_REV, ROUND, list(INCLUSION), list(SOURCE_STATES), list(SCOPE_STATES), POLICIES, PRIMARY_POLICY,
                             EXCLUSION_RULES, list(BINS), list(FORBIDDEN_WORDS), cv.grade_interp_version(), cv.absence_interp_version())


# ------------------------------------------------------------------------------------------------ helper
def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _sy(day):
    try:
        return ID.school_year_label(str(day)[:10]) if day else None
    except ValueError:
        return None


def _kind_state(fresh, reg, kind):
    e = fresh["per_kind"].get("%s/%s" % (CR.SOURCE, kind)) or {"motivo": "mai_sincronizzato"}
    n = len(reg.list_records(source=CR.SOURCE, kind=kind, status=R.PRESENT))
    if e["motivo"] in ("mai_sincronizzato", "nessuna_lettura_riuscita"):
        return {"stato": "non_letto", "n_registro": n if n else None, "motivo": e["motivo"]}
    return {"stato": "presente" if n else "vuoto_valido", "n_registro": n, "stale": bool(e.get("stale")),
            "legacy": bool((e.get("ultimo_run") or {}).get("legacy"))}


def _bin(v):
    k = int(v // 1)
    if v >= 9:
        return "[9,10]"
    return BINS[max(0, k)]


def _mean(items, policy):
    """items: voti inclusi (dict con valore, peso_raw, uid). -> blocco della media per la politica."""
    if not items:
        return {"calcolabile": False, "valore": None, "valore_esatto": None, "motivo": "nessun voto incluso", "n": 0, "uid_usati": []}
    uids = [g["uid"] for g in items]
    if policy == "media_aritmetica_semplice":
        exact = sum(g["valore"] for g in items) / len(items)
        return {"calcolabile": True, "valore": round(exact, ROUND), "valore_esatto": exact, "n": len(items), "uid_usati": uids}
    assumed = [g["uid"] for g in items if not (_num(g["peso_raw"]) and g["peso_raw"] >= 0)]
    w = [g["peso_raw"] if (_num(g["peso_raw"]) and g["peso_raw"] >= 0) else 1 for g in items]
    sw = sum(w)
    out = {"n": len(items), "uid_usati": uids, "somma_pesi": sw, "pesi_assunti": {"n": len(assumed), "uid": assumed, "valore_assunto": 1}}
    if sw == 0:
        return dict(out, calcolabile=False, valore=None, valore_esatto=None, motivo="somma dei pesi = 0")
    exact = sum(g["valore"] * wi for g, wi in zip(items, w)) / sw
    return dict(out, calcolabile=True, valore=round(exact, ROUND), valore_esatto=exact)


def _block(grades, source_state):
    """Statistiche descrittive di un insieme di voti (gia' filtrati per ambito)."""
    if source_state == "non_letto":
        return {"stato": "fonte_non_letta", "n_totali": None, "n_inclusi": None, "esclusi": None, "medie": None,
                "distribuzione": None, "serie": None}
    inc = [g for g in grades if g["inclusione"] == "incluso"]
    excl = {k: sorted(g["uid"] for g in grades if g["inclusione"] == k) for k in INCLUSION[1:]}
    if not grades:
        stato = "nessun_voto"
    elif inc:
        stato = "media_calcolabile"
    else:
        stato = "media_non_calcolabile"
    medie = {p: _mean(inc, p) for p in POLICIES}
    if stato == "media_calcolabile" and not medie[PRIMARY_POLICY]["calcolabile"]:
        stato = "media_non_calcolabile"
    dist = collections.Counter(_bin(g["valore"]) for g in inc)
    series, run = [], []
    for g in sorted(inc, key=lambda g: (g["data"] or "", g["uid"])):
        run.append(g)
        series.append({"data": g["data"], "uid": g["uid"], "valore": g["valore"], "peso_raw": g["peso_raw"],
                       "progressiva": {p: _mean(run, p)["valore"] for p in POLICIES}})
    return {"stato": stato, "n_totali": len(grades), "n_inclusi": len(inc), "esclusi": excl, "medie": medie,
            "distribuzione": {"fasce": list(BINS), "conteggi": {b: dist.get(b, 0) for b in BINS}, "base": "voti inclusi"} if inc else None,
            "serie": series if inc else None}


# ------------------------------------------------------------------------------------------------ costruzione
def build(reg, now_dt, manual=None, materia=None, periodo=None, dettaglio=False, soglia_ore=48, *, subjects, source_path=None):
    """cloud.school.records/1. Sola lettura; `now_dt` letto UNA volta dal chiamante."""
    if materia is not None:
        if not (isinstance(materia, str) and materia.strip()):
            raise ValueError("materia non valida: %r" % (materia,))
        key = S.subject_key(materia) or (materia.strip() if materia.strip() in [k for k, _ in S.SUBJECT_KEYS] else None)
        if key is None:
            raise ValueError("materia non riconosciuta: %r (chiavi: %s)" % (materia, ", ".join(k for k, _ in S.SUBJECT_KEYS)))
    else:
        key = None
    if periodo is not None and not (isinstance(periodo, str) and periodo.strip()):
        raise ValueError("periodo non valido: %r (periodDesc esatto)" % (periodo,))
    if not isinstance(dettaglio, bool):
        raise ValueError("dettaglio non valido: %r (true | false)" % (dettaglio,))
    if isinstance(soglia_ore, bool) or not isinstance(soglia_ore, (int, float)) or soglia_ore <= 0:
        raise ValueError("soglia_ore non valida: %r" % (soglia_ore,))
    now_dt = D.parse_now(now_dt)
    td = now_dt.astimezone(D._zone()).date().isoformat()
    year = _sy(td)
    fresh = reg.freshness(now_dt.isoformat(), stale_after_hours=soglia_ore)
    gstate, astate = _kind_state(fresh, reg, "grade"), _kind_state(fresh, reg, "absence")
    desc = {s["key"]: s["desc"] for s in subjects if s.get("key")}

    # ---- voti -----------------------------------------------------------------------------------------------------
    all_grades, hist = [], collections.Counter()
    for r in reg.list_records(source=CR.SOURCE, kind="grade", status=R.PRESENT):
        full = reg.get_record(CR.SOURCE, "grade", r["source_uid"])
        n = CR.normalized("grade", full["raw"])
        raw = n["raw"]
        y = _sy(raw.get("evtDate"))
        if y != year:
            hist["voti_%s" % y] += 1
            continue
        giudizio = None if _num(raw.get("decimalValue")) else GIUDIZI.get(str(raw.get("displayValue") or "").strip().lower())
        if raw.get("canceled"):
            inc = "escluso_annullato"
        elif not _num(raw.get("decimalValue")) and giudizio is None:
            inc = "escluso_non_numerico"
        else:
            inc = "incluso"
        g = {"uid": n["uid"], "hash": full["content_hash"], "registry_version": full["version"], "data": str(raw.get("evtDate") or "")[:10] or None,
             "materia": n["interpreted"]["materia_chiave"], "materia_fonte": raw.get("subjectDesc"),
             "displayValue": raw.get("displayValue"),
             "valore": (raw.get("decimalValue") if giudizio is None else giudizio) if inc == "incluso" else None,
             "valore_da_giudizio": giudizio is not None, "fuori_media": giudizio is not None or bool(raw.get("noAverage")),
             "decimalValue": raw.get("decimalValue"), "peso_raw": raw.get("weightFactor"), "componente": raw.get("componentDesc"),
             "tipo_valutazione": n["interpreted"]["tipo_valutazione"], "periodo": raw.get("periodDesc"), "periodo_pos": raw.get("periodPos"),
             "annullato": bool(raw.get("canceled")), "inclusione": inc}
        if dettaglio:
            g["raw"] = raw
        all_grades.append(g)
    all_grades.sort(key=lambda g: (g["data"] or "", g["uid"]))
    scoped = [g for g in all_grades if periodo is None or g["periodo"] == periodo.strip()]
    src = gstate["stato"]
    keys = sorted(set(SC.AGENT_SUBJECTS) | {g["materia"] for g in scoped if g["materia"]}) if key is None else [key]
    per_materia = [dict({"materia": k, "nome_ufficiale": desc.get(k)}, **_block([g for g in scoped if g["materia"] == k], src))
                   for k in keys]
    periods = sorted({(g["periodo_pos"] if _num(g["periodo_pos"]) else 99, g["periodo"] or "") for g in all_grades if g["periodo"]})
    voti = {
        "stato_fonte": gstate,
        "complessivo": dict({"ambito": "tutte le materie" if key is None else key},
                            **_block([g for g in scoped if not g["fuori_media"]] if key is None else
                                     [g for g in scoped if g["materia"] == key], src)),
        "per_materia": per_materia,
        "per_periodo": [dict({"periodo": p}, **_block([g for g in all_grades if g["periodo"] == p and (key is None or g["materia"] == key)], src))
                        for _, p in periods],
        "non_attribuiti": sorted(g["uid"] for g in scoped if not g["materia"]),
        "elenco": [g for g in scoped if key is None or g["materia"] == key] if src != "non_letto" else None,
    }

    # ---- assenze ----------------------------------------------------------------------------------------------------
    events = []
    for r in reg.list_records(source=CR.SOURCE, kind="absence", status=R.PRESENT):
        full = reg.get_record(CR.SOURCE, "absence", r["source_uid"])
        n = CR.normalized("absence", full["raw"])
        raw, it = n["raw"], n["interpreted"]
        y = _sy(raw.get("evtDate"))
        if y != year:
            hist["assenze_%s" % y] += 1
            continue
        # uid = identita' STABILE del registro (legacy_uid): invariato per ogni record, stabile per le assenze riconciliate
        e = {"uid": full["legacy_uid"] or n["uid"], "hash": full["content_hash"], "registry_version": full["version"], "data": str(raw.get("evtDate"))[:10],
             "codice": raw.get("evtCode"), "tipo": it["tipo"], "ora": raw.get("evtHPos"), "ore_perse": it["ore_perse"],
             "giustificata": raw.get("isJustified"), "stato_giustificazione": it["stato_giustificazione"],
             "da_verificare": it["da_verificare"]}
        if dettaglio:
            e["raw"] = raw
        events.append(e)
    events.sort(key=lambda e: (e["data"], e["uid"]))
    if astate["stato"] == "non_letto":
        assenze = {"stato_fonte": astate, "eventi": None, "conteggi": None}
    else:
        per_tipo = collections.Counter(e["tipo"] for e in events)
        per_mese = collections.defaultdict(collections.Counter)
        for e in events:
            per_mese[e["data"][:7]][e["tipo"]] += 1
        assenze = {"stato_fonte": astate, "eventi": events,
                   "conteggi": {"totale": len(events), "per_tipo": dict(sorted(per_tipo.items())),
                                "per_mese": {m: dict(sorted(c.items())) for m, c in sorted(per_mese.items())},
                                "da_verificare": sum(1 for e in events if e["da_verificare"])},
                   "nota": "eventi della fonte; tipo solo da tabella verificata (%s); le assenze non hanno materia" % cv.ABSENCE_SOURCE}

    gaps = []
    if gstate["stato"] == "non_letto":
        gaps.append({"codice": "voti_fonte_non_letta", "messaggio": "voti ClasseViva mai letti: nessuna statistica"})
    elif not all_grades:
        gaps.append({"codice": "nessun_voto_anno_operativo", "messaggio": "fonte voti letta: nessun voto nell'anno operativo"})
    if astate["stato"] == "non_letto":
        gaps.append({"codice": "assenze_fonte_non_letta", "messaggio": "assenze ClasseViva mai lette"})
    if any(g["tipo_valutazione"] == cv.RAW_UNVERIFIED for g in all_grades):
        gaps.append({"codice": "componente_non_verificata", "messaggio": "tipo di valutazione (componentDesc) non in tabella verificata: "
                                                                      "riportato verbatim, nessuna classificazione"})
    if any(e["da_verificare"] for e in events):
        gaps.append({"codice": "assenze_da_verificare", "messaggio": "eventi di assenza con codici/campi non in tabella verificata"})
    gaps.append({"codice": "media_classeviva_non_verificata", "messaggio": "nessuna politica di media e' verificata come identica a "
                                                                          "quella mostrata da ClasseViva"})
    return {
        "schema": SCHEMA,
        "natura": NATURA,
        "riferimento_temporale": {"now": now_dt.isoformat(), "data_riferimento": td, "anno_operativo": year},
        "ambito": {"materia": key, "periodo": periodo.strip() if periodo else None, "dettaglio": dettaglio},
        "politiche": {"primaria": PRIMARY_POLICY, "elenco": POLICIES, "esclusioni": EXCLUSION_RULES,
                      "nota": "le medie sono politiche nominate: non 'la media dello studente'; altre politiche si aggiungono senza "
                              "riscrivere i dati"},
        "voti": voti,
        "assenze": assenze,
        "storico": {"conteggi": dict(sorted(hist.items())), "nota": "anni diversi dall'operativo: solo conteggi, separati"},
        "gap": gaps,
        "freshness": {"voti": C3.section_freshness(fresh, [(CR.SOURCE, "grade")])["stato"],
                      "assenze": C3.section_freshness(fresh, [(CR.SOURCE, "absence")])["stato"], "soglia_ore": fresh["soglia_ore"]},
        "provenance": {"schema": SCHEMA, "records_version": records_version(), "grade_interp_version": cv.grade_interp_version(),
                       "absence_interp_version": cv.absence_interp_version(), "schema_registro": R._version_of(reg.db),
                       "sorgente": source_path},
        "regole_agente": RULES_FOR_AGENTS,
    }


def build_from_path(path, now, manual_path=None, **kw):
    """Registro aperto come COPIA in memoria (Registry.from_snapshot, immutable=1): nessun byte del file viene scritto."""
    reg = R.Registry.from_snapshot(path)
    try:
        return build(reg, now, manual=SC.load_manual(manual_path)["dati"], source_path=os.path.abspath(path), **kw)
    finally:
        reg.close()
