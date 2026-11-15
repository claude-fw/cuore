"""M6 - CLASSIFICAZIONE DERIVATA DELLE COMUNICAZIONI (circolari ClasseViva + annunci Classroom): `cloud.school.communications/1`.
Modulo PURO e SOLA LETTURA (nessuna rete, nessuna scrittura, nessun orologio: `now` arriva dal chiamante).

Il Registry resta l'UNICA fonte canonica. Questo modulo non scrive nulla, non modifica ne' duplica i record originali (li cita con
uid + versione + hash) e non crea tabelle: le classificazioni sono INTERPRETAZIONI ricalcolate a ogni lettura, con una loro versione
(`comm_version`). Tre livelli sempre separati: `dati` (canonico, citato), `interpretazione` (ambito, materia, funzioni, classe
principale, prove), `freschezza` (operativo, M3).

Modello:
  * CLASSI (funzioni, multi-etichetta): verifica, compito, attivita, organizzativa, avviso, materiale. Una comunicazione puo' avere
    piu' funzioni; la `classe_principale` e' scelta SOLO se unica al livello massimo (altrimenti `pari_livello`, nessuna scelta);
  * ATTRIBUTI (non classi): ambito (generale | corso | materia), materia (M2/corso), date esplicite con ruolo (verifica | scadenza |
    non_attribuita), riferimenti relativi (RILEVATI, MAI risolti), validita' nativa della circolare, allegati/materiali;
  * STATI: determinato (SOLO struttura nativa del record) | probabile | proposto | conflitto | non_determinabile.
    Il testo libero non produce MAI `determinato`: al massimo `probabile`.
Principi non negoziabili:
  * regole esplicite e prove (codice/esito/peso), nessun punteggio opaco; lessico versionato in `comm_version`;
  * i falsi positivi sono piu' gravi dei falsi negativi: nel dubbio `proposto` o `non_determinabile`; testi senza indizi
    (es. 'Ariosto', un nome) restano `non_determinabile`;
  * i collegamenti M5 sono solo evidenza CORROBORATIVA: un collegamento M5 non crea MAI una classificazione e non supera `probabile`;
  * un `conflitto` riporta i valori in contrasto e NON sceglie; nessuna fonte viene modificata;
  * stesso Registry + stesso `now` -> stesso JSON, indipendente dall'ordine di lettura.
"""
from __future__ import annotations

import collections
import datetime as dt
import re

import cls_derive as D
import cls_registry as CL
import cls_subjects as CS
import cvv_attribution as CA
import cvv_client as cv
import cvv_registry as CR
import cvv_subject as S
import cvv_cls_links as L
import it_dates as ID
import registry as R
import school_common as CM
import school_context3 as C3

SCHEMA = "cloud.school.communications/1"
EXPLAIN_SCHEMA = "cloud.school.communications.explain/1"
COMM_RULES_REV = 2                   # r2 (M7): caso ASSENZA_DOCENTE (regola AD1); tutto il resto invariato rispetto a r1

TEXT_CAP = 300
LIMITS = {"comunicazioni": 20}
COMPACT_TEXT = 160

STATES = ("determinato", "probabile", "proposto", "conflitto", "non_determinabile")
CLASSES = ("verifica", "compito", "attivita", "organizzativa", "avviso", "materiale")
SPECIFIC = ("verifica", "compito", "attivita", "organizzativa")      # competono per la classe principale
GENERIC = ("avviso",)                                                # solo se nessuna specifica ha evidenza
FALLBACK = ("materiale",)                                            # solo se nessuna delle precedenti ha evidenza
AMBITI = ("generale", "corso", "materia")
SOURCES = ("classeviva", "classroom")
PERIMETRI = ("operativo", "storico", "tutti")
RANK = {"determinato": 3, "probabile": 2, "proposto": 1}

# Lessico (testo NORMALIZZATO: minuscolo, senza accenti). Versionato: se cambia, cambia comm_version.
LEX = {
    "verifica": CM.TEST_RE.pattern,
    "variazione": r"\b(annullat\w*|rinviat\w*|spostat\w*|posticipat\w*|anticipat\w*|slitt\w*|sospes\w*|nessun[a]? verific\w*|"
                  r"non (?:ci )?(?:sara|sono|ci sono|c'?e)\w* (?:la |le |alcuna )?(?:verific\w*|interrogazion\w*|test))\b",
    "compito": r"\b(compit\w*|esercizi\w*|studiate|studiare|leggete|leggere|ripassate|ripassare|ripasso|svolgete|svolgere|"
               r"preparate|portate|consegn\w+)\b",
    "attivita": r"\b(progett\w*|corso|corsi|uscita didattica|viaggio d'?istruzione|viaggio|teatro|spettacol\w*|conferenz\w*|gara|"
                r"olimpiad\w*|concorso|visita guidata|visita|certificazion\w*|orientamento|laboratorio|torneo|pallavolo|campionat\w*|"
                r"pomeriggi? di studio|sportello|recupero|potenziamento)\b",
    "organizzativa": r"\b(ricevimento|genitori|colloqui\w*|orari\w*|assemblea|sostituzion\w*|non c'?e|assente|assenza|variazion\w*|"
                     r"entrata|uscita anticipata|scrutini\w*|riunione|consiglio di classe|sciopero|vacanz\w*|chiusura|calendario|"
                     r"pagament\w*|autorizzazion\w*|iscrizion\w*|modulo|firmare|firma|delega)\b",
    "avviso": r"\b(avvis\w*|comunicazion\w*|si comunica|si informa|informiamo|promemoria|ricordo|ricordate)\b",
    "materiale": r"\b(allegat\w*|allego|slide|dispens\w*|appunti|presentazion\w*|video|link|materiale|schema|mappa)\b",
    "url": r"https?://\S+|www\.\S+",
    "relativo": r"\b(?:(?:per|entro)\s+(?:il\s+)?(?:lunedi|martedi|mercoledi|giovedi|venerdi|sabato|domenica)|domani|dopodomani|oggi|"
                r"(?:prossima|settimana prossima)|(?:lunedi|martedi|mercoledi|giovedi|venerdi|sabato|domenica) prossim[oa])\b",
    "scadenza": r"(?:entro|scadenza|consegna(?:re)?(?: entro| per)?|da consegnare(?: entro| per)?)\s*(?:il|per il|il giorno|giorno)?\s*$",
    # r2: assenza di un DOCENTE solo con un titolo esplicito (prof/professoressa/docente) e una forma di assenza in terza persona.
    # 'sostituzione'/'sostituire', 'assenti' (studenti), 'professore' da solo NON bastano.
    "assenza_docente": r"\bnon c'?e\s+(?:la |il |lo )?(?:prof\.?ssa|professoressa|professore|prof\.?|docente)(?=[\s.,:;!]|$)|"
                       r"\b(?:prof\.?ssa|professoressa|professore|prof\.?|docente)\s+[a-z']+(?:\s+[a-z']+)?\s+(?:e'?|sara|risulta) assente\b|"
                       r"\bassenza (?:della |del |dello )(?:prof\.?ssa|professoressa|professore|prof\.?|docente)(?=[\s.,:;!]|$)",
}
_RX = {k: re.compile(v) for k, v in LEX.items()}

RULES = {
    "A1": "ambito generale determinato: circolare della bacheca ClasseViva (nessun campo materia/docente/corso nel record)",
    "A2": "ambito materia determinato: annuncio di un corso Classroom monomateria CONFERMATO",
    "A3": "ambito corso: annuncio di un corso condiviso o non confermato; la materia resta da_verificare",
    "M1": "materiale determinato: materiali nel record nativo di un annuncio Classroom (gli allegati delle circolari sono la circolare stessa: non contano)",
    "M2": "materiale probabile: URL nel testo (riconoscimento sintattico)", "M3": "materiale proposto: parole di materiale nel testo",
    "V1": "verifica probabile: parola di verifica + data esplicita attribuita alla verifica",
    "V2": "verifica proposta: solo parola di verifica (nessuna data attribuita)",
    "V3": "verifica proposta (tetto): parola di verifica con variazione/negazione (annullata, rinviata, nessuna verifica...)",
    "V4": "verifica probabile: V2 + collegamento M5 'prepara' (probabile|proposto) a una verifica ClasseViva (corroborativo)",
    "C1": "compito probabile: parola di compito + data esplicita o riferimento relativo (non risolto)",
    "C2": "compito proposto: solo parola di compito",
    "G1": "funzione proposta: una parola del lessico", "G2": "funzione probabile: >=2 parole distinte del lessico",
    "RC1": "conflitto: il testo nomina una materia diversa da quella del corso/candidati",
    "RC2": "conflitto: >=2 date diverse attribuite alla verifica nello stesso testo",
    "RN1": "non_determinabile: nessuna funzione con evidenza (testo senza indizi o troppo breve)",
    "RN2": "pari livello: >=2 classi allo stesso livello massimo, nessuna scelta",
    "AD1": "ASSENZA_DOCENTE (r2): pattern esplicito di assenza di un docente (titolo + 'non c'e'/'e' assente'/'assenza del'): "
           "funzione principale organizzativa (probabile); compito solo come funzione secondaria se il testo ha parole di compito",
}

_p = CM.prove


def comm_version():
    """'r<rev>-<impronta>': cambia da sola se cambiano lessico, regole, soglie o la versione dei collegamenti M5 usati."""
    return cv.interp_version(COMM_RULES_REV, LEX, sorted(RULES), list(STATES), list(CLASSES), list(SPECIFIC), list(GENERIC),
                             list(FALLBACK), TEXT_CAP, L.link_version(), D.CLASSI)


def _cues(key, tn):
    return sorted({m.group(0) for m in _RX[key].finditer(tn)})


# ------------------------------------------------------------------------------------------------ raccolta (sola lettura)
def _iso(s):
    s = str(s or "")[:10]
    try:
        dt.date.fromisoformat(s)
        return s
    except ValueError:
        return None


def _dates(testo, tn, sy):
    out = []
    for d in ID.parse_dates(testo, sy):
        d = dict(d)
        window = tn[max(0, d["inizio"] - 45):d["inizio"]]
        if d.get("data") and d["stato"] == "valida":
            d["ruolo"] = "verifica" if CM.date_attributed(tn, d) else ("scadenza" if _RX["scadenza"].search(window) else "non_attribuita")
        else:
            d["ruolo"] = "non_utilizzabile"
        out.append(d)
    return out


def _validity(raw, today):
    f, t = _iso(raw.get("cntValidFrom")), _iso(raw.get("cntValidTo"))
    flag = raw.get("cntValidInRange")
    if f is None and t is None:
        stato = "non_dichiarata" if flag is None else ("valida" if flag else "fuori_intervallo_nativo")
        return {"dal": None, "al": None, "stato": stato, "cntValidInRange": flag, "criterio": "nessuna data di validita' nel record"
                if flag is None else "solo il flag nativo cntValidInRange"}
    td = today.isoformat()
    stato = "scaduta" if (t and t < td) else ("non_ancora_valida" if (f and f > td) else "valida")
    return {"dal": f, "al": t, "stato": stato, "cntValidInRange": flag,
            "criterio": "date di validita' native confrontate con la data locale di riferimento (%s)" % td}


def collect(reg, subjects, now_dt, manual=None):
    """Legge dal Registry (solo lettura): circolari ClasseViva e annunci Classroom PUBBLICATI. -> dict con comms e conteggi."""
    confirmed = CS.confirmed_map(manual or {})
    today = now_dt.astimezone(D._zone()).date()
    diag = collections.Counter()
    comms = []

    full_n, gone_n = C3._present(reg, CR.SOURCE, "notice")
    for full in full_n:
        raw = full["raw"]
        n = CR.normalized("notice", raw)
        date = _iso(n.get("date"))
        if not date:
            diag["circolari_senza_data"] += 1
            continue
        sy = ID.school_year_start(date)
        titolo = (raw.get("cntTitle") or "").strip()
        atts = [a for a in (raw.get("attachments") or []) if isinstance(a, dict)]
        n_att = len(atts) if atts else (1 if raw.get("cntHasAttach") in (True, 1, "true", "True") else 0)
        tn = S.norm(titolo)
        comms.append({"uid": n["uid"], "fonte": "classeviva", "tipo": "notice", "date": date, "anno": ID.school_year_label(date),
                      "titolo": titolo, "testo": titolo, "tn": tn, "dates": _dates(titolo, tn, sy),
                      "n_materiali": n_att, "nomi_materiali": sorted(str(a.get("fileName")) for a in atts if a.get("fileName")),
                      "categoria_scuola": (raw.get("cntCategory") or "").strip() or None,
                      "numero": raw.get("cntNum"), "validita": _validity(raw, today), "corso": None,
                      "materia": {"key": None, "stato": "da_verificare", "candidati": [], "criterio": "le circolari ClasseViva non hanno "
                                  "campo materia/docente/corso: la materia non e' determinabile"},
                      "cite": C3._cite(full, n["uid"]), "sezioni": [(CR.SOURCE, "notice")], "item_uid": None})

    courses = {}
    for f in reg.list_records(source=CL.SOURCE, kind="course", status=R.PRESENT):
        c = reg.get_record(CL.SOURCE, "course", f["source_uid"])["raw"]
        courses[str(c.get("id"))] = c
    full_a, gone_a = C3._present(reg, CL.SOURCE, "announcement")
    for full in full_a:
        raw = full["raw"]
        if raw.get("state") != "PUBLISHED":
            diag["annunci_non_pubblicati"] += 1
            continue
        n = CL.normalized("announcement", raw)
        created = D._local_date(raw.get("creationTime"))
        if created is None:
            diag["annunci_senza_data_creazione"] += 1
            continue
        date = created.isoformat()
        sy = ID.school_year_start(date)
        cid = str(raw.get("courseId"))
        testo = (n.get("text") or "").strip()
        tn = S.norm(testo)
        if cid in confirmed:
            subj = dict(L.item_subject(confirmed[cid], None))
        else:
            subj = {"key": None, "stato": "da_verificare", "candidati": [],
                    "criterio": "corso Classroom NON confermato in school_manual.json: materia non determinabile"}
        found, _, _ = CS._scan(testo)
        subj["menzionate_nel_testo"] = found
        comms.append({"uid": n["uid"], "fonte": "classroom", "tipo": "announcement", "date": date, "anno": ID.school_year_label(date),
                      "titolo": None, "testo": testo, "tn": tn, "dates": _dates(testo, tn, sy),
                      "n_materiali": len(n.get("materials") or []), "nomi_materiali": [],
                      "categoria_scuola": None, "numero": None, "validita": None,
                      "corso": {"id": cid, "nome": (courses.get(cid) or {}).get("name"), "confermato": cid in confirmed},
                      "materia": subj, "cite": C3._cite(full, n["uid"]),
                      "sezioni": [("classroom", "announcement"), ("classroom", "course")], "item_uid": n["uid"]})
    comms.sort(key=lambda x: x["uid"])
    return {"comms": comms, "diag": diag, "confirmed": confirmed, "gone": {"notice": gone_n, "announcement": gone_a}}


# ------------------------------------------------------------------------------------------------ funzioni
def _fn(name, stato, regola, motiv, prove):
    return {"funzione": name, "stato": stato, "regola": regola, "motivazione": motiv, "prove": prove}


def _kw(key, cues, peso):
    return _p("S4", "parole chiave", "a_favore", peso, "%s: %s" % (key, ", ".join(cues)))


def _f_verifica(c, m5):
    cues = _cues("verifica", c["tn"])
    if not cues:
        return None
    prove = [_kw("verifica", cues, "media")]
    var = _cues("variazione", c["tn"])
    vd = sorted({d["data"] for d in c["dates"] if d["ruolo"] == "verifica"})
    for d in c["dates"]:
        if d["ruolo"] == "verifica":
            prove.append(_p("S5", "data esplicita", "a_favore", "forte", "'%s' (%s%s) e' attribuita alla verifica" % (
                d["testo"], d["data"], "" if d["anno_esplicito"] else ", anno dedotto dall'anno scolastico")))
    if len(vd) > 1:
        prove.append(_p("S5", "data esplicita", "contro", "forte", "date diverse attribuite alla verifica: %s" % ", ".join(vd)))
        return _fn("verifica", "conflitto", "RC2", "date diverse attribuite alla verifica nello stesso testo: %s (nessuna scelta)" % ", ".join(vd), prove)
    if var:
        prove.append(_p("S9", "variazione/negazione", "contro", "media", "parole di variazione: %s" % ", ".join(var)))
        return _fn("verifica", "proposto", "V3", RULES["V3"], prove)
    if vd:
        return _fn("verifica", "probabile", "V1", RULES["V1"], prove)
    prep = [x for x in m5 if x["relazione"] == "prepara" and x["stato"] in ("probabile", "proposto")]
    if prep:
        prove.append(_p("S8", "collegamento M5", "a_favore", "corroborativa",
                        "M5 collega l'annuncio a una verifica ClasseViva (%s): solo corroborazione" % ", ".join(
                            "%s %s" % (x["link_id"], x["stato"]) for x in prep)))
        return _fn("verifica", "probabile", "V4", RULES["V4"], prove)
    return _fn("verifica", "proposto", "V2", RULES["V2"], prove)


def _f_compito(c):
    tn = CM.TEST_RE.sub(" ", c["tn"])                       # 'compiti in classe' e' una verifica, non un compito
    cues = sorted({m.group(0) for m in _RX["compito"].finditer(tn)})
    if not cues:
        return None
    prove = [_kw("compito", cues, "media")]
    when = [d for d in c["dates"] if d["ruolo"] in ("scadenza", "non_attribuita")]
    rel = _cues("relativo", c["tn"])
    for d in when:
        prove.append(_p("S5", "data esplicita", "a_favore", "media", "'%s' (%s): data presente, ruolo %s" % (d["testo"], d["data"], d["ruolo"])))
    if rel:
        prove.append(_p("S5", "riferimento relativo", "a_favore", "debole", "'%s' RILEVATO ma NON risolto in una data" % ", ".join(rel)))
    if when or rel:
        return _fn("compito", "probabile", "C1", RULES["C1"], prove)
    return _fn("compito", "proposto", "C2", RULES["C2"], prove)


def _f_group(name, c):
    cues = _cues(name, c["tn"])
    if not cues:
        return None
    prove = [_kw(name, cues, "forte" if len(cues) >= 2 else "media")]
    return _fn(name, "probabile" if len(cues) >= 2 else "proposto", "G2" if len(cues) >= 2 else "G1",
               RULES["G2" if len(cues) >= 2 else "G1"], prove)


def _f_materiale(c):
    urls, cues = _cues("url", c["tn"]), _cues("materiale", c["tn"])
    if c["n_materiali"] and c["tipo"] == "announcement":     # l'allegato di una circolare E' la circolare: non e' un indizio
        det = "%d %s nel record nativo" % (c["n_materiali"], "allegati" if c["tipo"] == "notice" else "materiali")
        if c["nomi_materiali"]:
            det += " (%s)" % ", ".join(c["nomi_materiali"])
        return _fn("materiale", "determinato", "M1", RULES["M1"], [_p("S6", "struttura nativa", "a_favore", "forte", det)])
    if urls:
        return _fn("materiale", "probabile", "M2", RULES["M2"], [_p("S7", "URL nel testo", "a_favore", "media", "%d URL" % len(urls))])
    if cues:
        return _fn("materiale", "proposto", "M3", RULES["M3"], [_kw("materiale", cues, "debole")])
    return None


def _assenza_docente(c, fns):
    """r2: caso ASSENZA_DOCENTE. -> (funzioni, caso) con organizzativa principale; None se il pattern esplicito manca."""
    cues = _cues("assenza_docente", c["tn"])
    if not cues:
        return fns, None
    prev = next((f for f in fns if f["funzione"] == "organizzativa"), None)
    prove = [_p("S10", "assenza docente", "a_favore", "forte", "pattern esplicito di assenza di un docente: %s" % ", ".join(cues))]
    org = _fn("organizzativa", "probabile", "AD1", RULES["AD1"], prove + (prev["prove"] if prev else []))
    fns = sorted([f for f in fns if f["funzione"] != "organizzativa"] + [org], key=lambda f: CLASSES.index(f["funzione"]))
    sec = [f["funzione"] for f in fns if f["funzione"] == "compito" and f["stato"] in RANK]
    return fns, {"codice": "ASSENZA_DOCENTE", "regola": "AD1", "funzione_principale": "organizzativa", "funzioni_secondarie": sec,
                 "motivazione": "assenza di un docente dichiarata nel testo: comunicazione organizzativa" +
                                ("; il compito e' secondario (parole di compito nel testo)" if sec else "; nessun compito nel testo")}


def _principal(fns):
    live = {f["funzione"]: f for f in fns if f["stato"] in RANK}
    for group in (SPECIFIC, GENERIC, FALLBACK):
        cands = [live[g] for g in group if g in live]
        if not cands:
            continue
        top = max(RANK[f["stato"]] for f in cands)
        tops = [f for f in cands if RANK[f["stato"]] == top]
        if len(tops) == 1:
            return tops[0]["funzione"], tops[0]["stato"], None
        return None, tops[0]["stato"], [f["funzione"] for f in tops]
    return None, "non_determinabile", None


# ------------------------------------------------------------------------------------------------ classificazione
def classify(c, m5):
    """Classificazione di UNA comunicazione preparata (`m5` = collegamenti M5 che la riguardano). Deterministica, senza mutazioni."""
    ver = comm_version()
    fns = [f for f in (_f_verifica(c, m5["collegamenti"]), _f_compito(c), _f_group("attivita", c), _f_group("organizzativa", c),
                       _f_group("avviso", c), _f_materiale(c)) if f]
    fns.sort(key=lambda f: CLASSES.index(f["funzione"]))
    fns, caso = _assenza_docente(c, fns)
    conflitti = []
    for f in fns:
        if f["stato"] == "conflitto":
            conflitti.append({"tipo": "date_discordanti", "regola": f["regola"], "funzione": f["funzione"],
                              "motivazione": f["motivazione"], "valori": sorted({d["data"] for d in c["dates"] if d["ruolo"] == "verifica"})})
    mat = c["materia"]
    cand = {mat["key"]} if mat["key"] else set(mat["candidati"])
    found = set(mat.get("menzionate_nel_testo") or [])
    if found and cand and not (found & cand):
        conflitti.append({"tipo": "materia_discordante", "regola": "RC1",
                          "motivazione": "il testo nomina %s ma il corso/attribuzione indica %s: nessuna scelta" % (
                              "/".join(sorted(found)), "/".join(sorted(cand))),
                          "valori": {"testo": sorted(found), "corso": sorted(cand)}})
        mat = dict(mat, stato="conflitto")
    prin, st, pari = ("organizzativa", "probabile", None) if caso else _principal(fns)
    if conflitti:
        stato, regola = "conflitto", conflitti[0]["regola"]
    elif prin or pari:
        stato, regola = st, ("RN2" if pari else ("AD1" if caso else next(f["regola"] for f in fns if f["funzione"] == prin)))
    else:
        stato, regola = "non_determinabile", "RN1"
    if c["fonte"] == "classeviva":
        ambito = {"valore": "generale", "stato": "determinato", "regola": "A1", "criterio": RULES["A1"]}
    elif mat["stato"] == "determinata":
        ambito = {"valore": "materia", "stato": "determinato", "regola": "A2", "criterio": RULES["A2"]}
    else:
        ambito = {"valore": "corso", "stato": "determinato", "regola": "A3", "criterio": RULES["A3"]}
    miss = []
    if mat["stato"] != "determinata" and c["tipo"] == "announcement":
        miss.append("materia non determinata: %s" % mat["criterio"])
    rel = _cues("relativo", c["tn"])
    if rel:
        miss.append("riferimenti relativi rilevati e NON risolti in data: %s" % ", ".join(rel))
    if not fns:
        miss.append("nessun indizio nel testo: nessuna classe determinabile (e' il risultato prudente)")
    if stato == "non_determinabile" and m5["collegamenti"]:
        miss.append("esistono collegamenti M5 ma M5 e' solo corroborazione: non crea classificazioni")
    used_m5 = any(p["codice"] == "S8" for f in fns for p in f["prove"])
    out = {"comm_id": CM.short_hash(ver, c["uid"], c["cite"]["content_hash"]), "fonte": c["fonte"], "tipo": c["tipo"], "uid": c["uid"],
           "data": c["date"], "stato": stato,
           "dati": {"data_comunicazione": c["date"], "titolo": c["titolo"], "testo": CM.cap_text(c["testo"], TEXT_CAP)[0],
                    "testo_troncato": CM.cap_text(c["testo"], TEXT_CAP)[1], "corso": c["corso"], "numero": c["numero"],
                    "categoria_scuola": c["categoria_scuola"], "materiali": c["n_materiali"], "validita_nativa": c["validita"]},
           "interpretazione": {"comm_version": ver, "regola": regola, "ambito": ambito,
                               "materia": {k: mat[k] for k in ("key", "stato", "candidati", "criterio") if k in mat} |
                               {"menzionate_nel_testo": sorted(found)},
                               "classe_principale": {"valore": prin, "stato": st if (prin or pari) else "non_determinabile",
                                                     "pari_livello": pari},
                               "funzioni": fns,
                               "date": [{"testo": d["testo"], "data": d["data"], "ruolo": d["ruolo"], "anno_esplicito": d["anno_esplicito"],
                                         "stato": d["stato"]} for d in c["dates"]],
                               "riferimenti_relativi_non_risolti": rel,
                               "perimetro": {"valore": "operativo" if c["anno"] in D.CLASSI else "storico", "anno": c["anno"],
                                             "classe": D.CLASSI.get(c["anno"])},
                               "collegamenti_m5": {"usato_come_evidenza": used_m5, **m5},
                               "conflitti": conflitti, "informazioni_mancanti": miss,
                               "nota": "interpretazione derivata: il record originale non e' stato modificato"},
           "fonte_record": c["cite"], "_sezioni": list(c["sezioni"]) + ([(CR.SOURCE, "agenda_event"), (CR.SOURCE, "lesson")] if used_m5 else [])}
    if caso:                                   # chiavi aggiunte SOLO alle comunicazioni intercettate (le altre restano identiche a r1)
        out["interpretazione"]["caso_speciale"] = caso
        out["interpretazione"]["classe_principale"]["secondarie"] = caso["funzioni_secondarie"]
    return out


def _m5_index(reg, subjects, manual):
    """Collegamenti M5 per uid di annuncio Classroom (SOLA LETTURA, API pubblica di M5)."""
    prep = L.collect(reg, subjects, manual)
    res = L.compute(prep)
    idx = collections.defaultdict(lambda: {"collegamenti": [], "conflitti": [], "non_determinabili": 0})
    for x in res["collegamenti"]:
        if x["b"]["tipo"] == "announcement":
            idx[x["b"]["uid"]]["collegamenti"].append({"link_id": x["link_id"], "relazione": x["relazione"], "stato": x["stato"],
                                                      "regola": x["interpretazione"]["regola"],
                                                      "con": {"sistema": "classeviva", "tipo": x["a"]["tipo"], "uid": x["a"]["uid"]}})
    for x in res["conflitti"]:
        if x["elemento"]["tipo"] == "announcement":
            idx[x["elemento"]["uid"]]["conflitti"].append({"conflitto_id": x["conflitto_id"], "tipo": x["tipo"], "regola": x["interpretazione"]["regola"]})
    for x in res["non_determinabili"]:
        if x["b"]["tipo"] == "announcement":
            idx[x["b"]["uid"]]["non_determinabili"] += 1
    for v in idx.values():
        v["collegamenti"].sort(key=lambda z: z["link_id"])
        v["conflitti"].sort(key=lambda z: z["conflitto_id"])
        v["nota"] = "M5 e' solo evidenza corroborativa: non crea classificazioni e non e' un fatto"
    return idx, res["link_version"]


def compute(reg, subjects, now_dt, manual=None):
    prep = collect(reg, subjects, now_dt, manual)
    idx, lv = _m5_index(reg, subjects, manual)
    empty = {"collegamenti": [], "conflitti": [], "non_determinabili": 0, "nota": "nessun collegamento M5 per questa comunicazione"}
    out = [classify(c, idx.get(c["item_uid"], empty) if c["item_uid"] else dict(empty)) for c in prep["comms"]]
    out.sort(key=lambda x: (x["data"], x["uid"]), reverse=True)
    return out, prep, lv


# ------------------------------------------------------------------------------------------------ documento
LEVELS = {
    "dati": "canonico: campi della fonte (testi lunghi troncati con flag), citati in 'fonte_record' con uid + registry_version + content_hash",
    "interpretazione": "derivato: ambito, materia, funzioni (classi), classe principale, date con ruolo, prove; e' una lettura, non un fatto",
    "freschezza": "operativo: eta' e stato delle ultime letture da cui la classificazione dipende (M3); 'aggiornato' NON garantisce correttezza",
}
CRITERI = {
    "stati": {"determinato": "SOLO struttura nativa del record (ambito della circolare, materiali degli annunci): il testo libero non lo produce mai",
              "probabile": "evidenza forte (>=2 parole del lessico, data esplicita attribuita, URL, corroborazione M5 su evidenza testuale)",
              "proposto": "una sola parola del lessico o evidenza con tetto (variazione/negazione): da verificare",
              "conflitto": "segnali in contrasto (RC1 materia discordante, RC2 date discordanti): nessuna scelta, nessuna fonte modificata",
              "non_determinabile": "nessuna funzione con evidenza (RN1) o niente di cui fidarsi: e' il risultato prudente"},
    "classi": {"verifica": "verifica/interrogazione/test", "compito": "compito, studio, lettura, esercizi (anche relativo, non risolto)",
               "attivita": "progetto, corso, uscita, spettacolo, concorso, recupero",
               "organizzativa": "ricevimento, orari, sostituzioni, assemblee, scioperi, moduli", "avviso": "avviso generico (solo se nulla di piu' specifico)",
               "materiale": "materiali degli annunci, URL, parole di materiale (solo se nulla di piu' specifico)"},
    "classe_principale": "unica classe al livello massimo nell'ordine specifiche > avviso > materiale; se piu' classi pari: nessuna scelta (pari_livello)",
    "attributi_non_classi": ["ambito (generale|corso|materia)", "materia", "date con ruolo (verifica|scadenza|non_attribuita)",
                             "validita' nativa della circolare", "allegati/materiali", "scadenza"],
    "date": "solo date ESPLICITE gia' supportate (it_dates); gli intervalli 'dal... al...' e i riferimenti relativi non sono risolti",
    "m5": "i collegamenti M5 sono corroborazione: V4 puo' alzare 'proposto' a 'probabile' solo con evidenza testuale gia' presente; mai da soli",
    "regole": RULES,
    "perimetro": "default 'operativo' = anni di cls_derive.CLASSI (etichetta; nessun record viene escluso dal Registry)",
}
RULES_FOR_AGENTS = [
    "Le classificazioni sono INTERPRETAZIONI derivate, non fatti: presentale come tali e cita uid + registry_version + content_hash.",
    "'determinato' vale solo per la struttura nativa (ambito della circolare, materiali di un annuncio); 'probabile' e 'proposto' non sono certezze.",
    "Una circolare ClasseViva e' 'generale': non assegnarle una materia; l'ambito 'corso' non e' una materia.",
    "Un 'conflitto' va mostrato con entrambi i valori: non scegliere e non dire che una fonte ha torto.",
    "'non_determinabile' = nessuna classe affidabile: non completarla ne' dedurla; un nome solo (es. 'Ariosto') non basta.",
    "I riferimenti relativi ('per lunedi', 'domani') sono solo rilevati: non trasformarli in date.",
    "Il collegamento M5 e' solo corroborazione; una circolare scaduta (validita_nativa.stato) non e' piu' operativa.",
    "Controlla 'freschezza': se stale o senza_lettura dichiaralo; 'informazioni_mancanti' elenca cio' che non c'e'.",
]


def _resolve_materia(materia, subjects):
    if materia is None:
        return None
    if not (isinstance(materia, str) and materia.strip()):
        raise ValueError("materia non valida: %r" % (materia,))
    import school_context as SC
    return SC.resolve_subject(materia, subjects)


def _need_date(name, v):
    if v is None:
        return None
    try:
        return dt.date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        raise ValueError("%s non valida: %r (formato YYYY-MM-DD)" % (name, v))


def _shape(x, dettaglio):
    x = {k: v for k, v in x.items() if not k.startswith("_")}
    if dettaglio:
        return x
    i = dict(x["interpretazione"])
    i.pop("comm_version", None)
    i.pop("nota", None)
    i["funzioni"] = [{k: v for k, v in f.items() if k not in ("prove", "motivazione")} for f in i["funzioni"]]
    i["ambito"] = i["ambito"]["valore"]
    mt = i["materia"]
    m = {"key": mt["key"], "stato": mt["stato"]}
    if not mt["key"] and mt["candidati"]:
        m["candidati"] = mt["candidati"]
    if mt.get("menzionate_nel_testo"):
        m["menzionate_nel_testo"] = mt["menzionate_nel_testo"]
    i["materia"] = m
    cp = i["classe_principale"]
    i["classe_principale"] = {k: v for k, v in cp.items() if k != "pari_livello" or v}
    i["perimetro"] = "%s:%s" % (i["perimetro"]["valore"], i["perimetro"]["anno"])
    m5 = {k: v for k, v in i["collegamenti_m5"].items() if k not in ("nota", "usato_come_evidenza") and v}
    if i["collegamenti_m5"].get("usato_come_evidenza"):
        m5["usato_come_evidenza"] = True
    if m5:
        i["collegamenti_m5"] = m5
    else:
        i.pop("collegamenti_m5")
    for k in ("date", "riferimenti_relativi_non_risolti", "conflitti", "informazioni_mancanti"):
        if not i[k]:
            i.pop(k)
    x["interpretazione"] = i
    d = dict(x["dati"])
    d.pop("data_comunicazione", None)
    if d.get("testo") == d.get("titolo"):
        d.pop("testo", None)
    if d.get("testo") and len(d["testo"]) > COMPACT_TEXT:
        d["testo"], d["testo_troncato"] = d["testo"][:COMPACT_TEXT], True
    if not d.get("testo_troncato"):
        d.pop("testo_troncato", None)
    for k in ("titolo", "categoria_scuola", "numero", "validita_nativa"):
        if d.get(k) is None:
            d.pop(k, None)
    if d.get("corso"):
        d["corso"] = d["corso"]["nome"] if d["corso"]["confermato"] else "%s (non confermato)" % d["corso"]["nome"]
    if d.get("validita_nativa"):
        v = d["validita_nativa"]
        d["validita_nativa"] = {"stato": v["stato"], "al": v["al"]}
    if not d.get("materiali"):
        d.pop("materiali", None)
    x["dati"] = d
    x["fonte_record"] = {k: v for k, v in x["fonte_record"].items() if k in ("registry_version", "content_hash")}
    return x


def _build(reg, subjects, now, manual=None, materia=None, classe=None, stato=None, fonte=None, perimetro="operativo",
           dal=None, al=None, includi_generali=False, dettaglio=False, soglia_ore=None, source_path=None, limits=None):
    key = _resolve_materia(materia, subjects)
    if classe is not None and classe not in CLASSES:
        raise ValueError("classe non valida: %r (ammesse: %s)" % (classe, ", ".join(CLASSES)))
    if stato is not None and stato not in STATES:
        raise ValueError("stato non valido: %r (ammessi: %s)" % (stato, ", ".join(STATES)))
    if fonte is not None and fonte not in SOURCES:
        raise ValueError("fonte non valida: %r (ammesse: %s)" % (fonte, ", ".join(SOURCES)))
    if perimetro not in PERIMETRI:
        raise ValueError("perimetro non valido: %r (ammessi: %s)" % (perimetro, ", ".join(PERIMETRI)))
    dal, al = _need_date("dal", dal), _need_date("al", al)
    if dal and al and dal > al:
        raise ValueError("intervallo non valido: dal %s e' dopo al %s" % (dal, al))
    now_dt = D.parse_now(now)
    fresh = reg.freshness(now_dt.isoformat(), stale_after_hours=soglia_ore)
    allc, prep, lv = compute(reg, subjects, now_dt, manual)

    kinds = {"circolari": [(CR.SOURCE, "notice")], "annunci": [("classroom", "announcement"), ("classroom", "course")],
             "agenda": C3.SECTION_KINDS["agenda"], "lezioni": C3.SECTION_KINDS["lezioni"]}
    fsec = {n: C3.section_freshness(fresh, k) for n, k in kinds.items()}
    warn, miss = [], []
    for n in ("circolari", "annunci"):
        C3._freshness_warnings(warn, n, fsec[n])

    def fresh_of(x):
        sts = []
        for s in x["_sezioni"]:
            sec = "circolari" if s == (CR.SOURCE, "notice") else ("agenda" if s == (CR.SOURCE, "agenda_event") else
                                                                  ("lezioni" if s == (CR.SOURCE, "lesson") else "annunci"))
            sts.append(fsec[sec]["stato"])
        return CM.worst_freshness(*sts)

    excl = collections.Counter()
    rows = []
    for x in allc:
        if perimetro != "tutti" and x["interpretazione"]["perimetro"]["valore"] != perimetro:
            excl["fuori_perimetro_%s" % perimetro] += 1
            continue
        if (dal and x["data"] < dal) or (al and x["data"] > al):
            excl["fuori_intervallo_date"] += 1
            continue
        if fonte and x["fonte"] != fonte:
            continue
        if stato and x["stato"] != stato:
            continue
        if classe and classe not in {f["funzione"] for f in x["interpretazione"]["funzioni"]}:
            continue
        if key:
            m = x["interpretazione"]["materia"]
            if m["key"] != key:
                if x["interpretazione"]["ambito"]["valore"] == "generale" and includi_generali:
                    pass
                else:
                    excl["materia_generali" if x["interpretazione"]["ambito"]["valore"] == "generale" else
                         ("materia_possibile_da_verificare" if key in (m["candidati"] or []) else "materia_altra")] += 1
                    continue
        rows.append(x)

    lim = dict(LIMITS, **(limits or {}))
    shown, meta = C3.SC._capped(rows, lim["comunicazioni"], "operativo", "comunicazioni") if lim["comunicazioni"] else (rows, {"troncato": False})
    C3._truncation_warning(warn, "comunicazioni", "comunicazioni", meta)
    out_rows = []
    for x in shown:
        y = _shape(x, dettaglio)
        y["freschezza"] = {"stato": fresh_of(x)} if not dettaglio else {"stato": fresh_of(x), "nota": C3.FRESH_NOTE}
        out_rows.append(y)

    confl = [x for x in rows if x["stato"] == "conflitto"]
    if confl:
        C3._w(warn, "conflitti_presenti", "attenzione", "comunicazioni",
              "%d comunicazioni con segnali in contrasto: nessuna scelta, nessuna fonte modificata" % len(confl), [x["comm_id"] for x in confl])
    stale = [x["comm_id"] for x in shown if fresh_of(x) != "aggiornato"]
    if stale:
        C3._w(warn, "classificazioni_con_fonte_non_aggiornata", "attenzione", "comunicazioni",
              "classificazioni calcolate su letture stale o senza lettura: verifica la freschezza", stale)
    scad = [x["comm_id"] for x in rows if x["tipo"] == "notice" and x["dati"]["validita_nativa"]["stato"] == "scaduta"]
    if scad:
        C3._w(warn, "circolari_scadute", "info", "circolari", "%d circolari con validita' nativa scaduta: non piu' operative" % len(scad), scad)
    d = prep["diag"]
    if key and not includi_generali:
        miss.append({"sezione": "filtri", "messaggio": "con il filtro materia le circolari generali non sono incluse (includi_generali=true per includerle)"})
    if not prep["confirmed"]:
        miss.append({"sezione": "classroom", "messaggio": "nessun corso Classroom CONFERMATO (school_manual.json): la materia degli annunci non e' determinabile"})
    miss.append({"sezione": "conferme", "messaggio": "M6 v1 non prevede conferme o correzioni manuali: nessuna classificazione puo' essere confermata dall'utente"})
    miss.append({"sezione": "date", "messaggio": "gli intervalli 'dal... al...' e le date relative non sono interpretati; solo date esplicite"})
    miss.append({"sezione": "circolari", "messaggio": "le circolari ClasseViva hanno solo titolo, categoria e allegati: niente destinatari ne' testo integrale, "
                 "classificate solo dal titolo; il contenuto degli allegati non e' letto"})
    miss.append({"sezione": "annunci", "messaggio": "gli annunci Classroom non hanno titolo, topic ne' autore leggibile; il contenuto dei materiali non e' letto (solo metadati)"})

    def count(rs, f):
        c = collections.Counter(f(x) for x in rs)
        return dict(sorted((str(k), v) for k, v in c.items()))
    per_stato = {s: 0 for s in STATES}
    per_stato.update({k: v for k, v in count(rows, lambda x: x["stato"]).items()})
    return {
        "schema": SCHEMA,
        "natura": "interpretazione derivata e in SOLA LETTURA: il Registry e' l'unica fonte canonica; nessun record originale e' stato modificato",
        "comm_version": comm_version(), "riferimento_temporale": now_dt.isoformat(),
        "livelli": LEVELS, "criteri": CRITERI,
        "filtri": {"materia": key, "classe": classe, "stato": stato, "fonte": fonte, "perimetro": perimetro, "dal": dal, "al": al,
                   "includi_generali": includi_generali, "dettaglio": dettaglio},
        "comunicazioni": out_rows,
        "riepilogo": {"per_stato": per_stato, "per_fonte": count(rows, lambda x: x["fonte"]),
                      "per_ambito": count(rows, lambda x: x["interpretazione"]["ambito"]["valore"]),
                      "per_classe_principale": count(rows, lambda x: x["interpretazione"]["classe_principale"]["valore"] or "nessuna"),
                      "per_funzione": count([f for x in rows for f in x["interpretazione"]["funzioni"]], lambda f: f["funzione"]),
                      "comunicazioni_valutate": len(allc), "comunicazioni_selezionate": len(rows),
                      "esclusi_dai_filtri": dict(sorted(excl.items())),
                      "esclusi_dalla_raccolta": dict(sorted(d.items())),
                      "scomparse_dalla_fonte": prep["gone"]},
        "freschezza": dict({"calcolata_a": fresh["calcolato_a"], "soglia_ore": fresh["soglia_ore"],
                            "per_sezione": {n: f["stato"] for n, f in fsec.items()}, "nota": C3.FRESH_NOTE},
                           **({"dettaglio": fsec} if dettaglio else {"dettaglio": "con dettaglio=true o school_communication_explain"})),
        "warning": warn, "informazioni_mancanti": miss,
        "provenienza": {"schema_registro": R._version_of(reg.db), "comm_version": comm_version(), "link_version": lv,
                        "attr_version": CA.attr_version(), "derive_version": D.DERIVE_VERSION,
                        "registro_migrato_in_memoria": bool(getattr(reg, "migrated_in_memory", False)), "sorgente": source_path},
        "regole_per_gli_agenti": RULES_FOR_AGENTS,
    }, allc


def build(reg, subjects, now, manual=None, materia=None, classe=None, stato=None, fonte=None, perimetro="operativo", dal=None, al=None,
          includi_generali=False, dettaglio=False, soglia_ore=None, source_path=None, limits=None):
    """cloud.school.communications/1. Sola lettura; stesso registro + stesso now -> stesso JSON."""
    return _build(reg, subjects, now, manual, materia, classe, stato, fonte, perimetro, dal, al, includi_generali, dettaglio,
                  soglia_ore, source_path, limits)[0]


def explain(reg, subjects, now, target, manual=None, soglia_ore=None, source_path=None):
    """cloud.school.communications.explain/1: classificazione COMPLETA (con prove) di un uid o di un comm_id."""
    if not (isinstance(target, str) and target.strip()):
        raise ValueError("target non valido: indica un uid (cvv:<pubId>, cls:...) o un comm_id")
    target = target.strip()
    doc, allc = _build(reg, subjects, now, manual, perimetro="tutti", dettaglio=True, soglia_ore=soglia_ore, source_path=source_path,
                       limits={"comunicazioni": 0})
    hit = [x for x in doc["comunicazioni"] if target in (x["uid"], x["comm_id"])]
    if hit:
        tipo, esito = ("comm_id" if hit[0]["comm_id"] == target else "uid"), "classificazione trovata: stato %s" % hit[0]["stato"]
    else:
        tipo = "sconosciuto"
        esito = ("target non trovato tra le comunicazioni valutate: puo' non esistere, essere scomparso dalla fonte, non essere una "
                 "circolare/annuncio PUBBLICATO o non avere una data")
    return {"schema": EXPLAIN_SCHEMA, "richiesto": target, "tipo_richiesta": tipo, "esito": esito, "comunicazioni": hit,
            "comm_version": doc["comm_version"], "riferimento_temporale": doc["riferimento_temporale"], "freschezza": doc["freschezza"],
            "warning": [w for w in doc["warning"] if w["codice"] in ("sezione_stale", "sezione_senza_lettura", "dati_legacy",
                                                                     "ultima_lettura_con_rifiuti")],
            "provenienza": doc["provenienza"], "criteri": doc["criteri"], "regole_per_gli_agenti": RULES_FOR_AGENTS}


def build_from_path(path, subjects, now, manual_path=None, **kw):
    import os
    import school_context as SC
    reg = R.Registry.from_snapshot(path)
    try:
        return build(reg, subjects, now, manual=SC.load_manual(manual_path)["dati"], source_path=os.path.abspath(path), **kw)
    finally:
        reg.close()


def explain_from_path(path, subjects, now, target, manual_path=None, soglia_ore=None):
    import os
    import school_context as SC
    reg = R.Registry.from_snapshot(path)
    try:
        return explain(reg, subjects, now, target, manual=SC.load_manual(manual_path)["dati"], soglia_ore=soglia_ore,
                       source_path=os.path.abspath(path))
    finally:
        reg.close()
