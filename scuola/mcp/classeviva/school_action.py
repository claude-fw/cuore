"""AZIONI ASSISTIVE (M10 v1): `cloud.school.action/1`. Modulo PURO e SOLA LETTURA.

M9 = "cosa merita attenzione?"; M10 = "quale azione assistiva concreta e' disponibile adesso?".
Input (soluzione B, approvata 2026-10-06): il contratto pubblico di M9 (`school_priority.build(dettaglio=True)`) e, SOLO per risolvere
le evidenze `(segnale, uid, hash)` che M9 cita, il contratto pubblico di M8 (`school_status.build(dettaglio=True)`,
`evidenze_dettaglio`). Nessun ricalcolo di segnali, priorita' o affidabilita'; nessuna lettura del testo libero `motivo`.
Un'evidenza si risolve solo se uid E hash coincidono; altrimenti l'azione e' `non_eseguibile` con gap `evidenza_non_risolvibile`.
Catena: Registry -> M2/M5/M6 -> M7 (elemento con hash del content_hash) -> M8 (segnale) -> M9 (voce) -> M10 (azione).

Per ogni azione M9 (vocabolario chiuso) M10 espone: contesto disponibile (solo campi gia' presenti negli elementi M8/M7 risolti),
`eseguibilita` (eseguibile | parzialmente_eseguibile | non_eseguibile), `gaps` supportati dai dati, affidabilita' (mai sopra
quella M9: monotona) e provenance. Priorita' M9, affidabilita' e eseguibilita' restano campi distinti.
Decisioni: argomenti di una verifica SOLO dal testo della verifica e dai collegamenti M5 della verifica (mai dalle lezioni recenti);
comunicazione con testo troncato -> puo' restare eseguibile ma con gap `contenuto_parziale`.
Nessuna scrittura, nessuna automazione esterna, nessun piano, durata, giudizio, voto o diagnosi.
"""
from __future__ import annotations

import os

import cvv_client as cv
import registry as R
import school_common as CM
import school_context as SC
import school_priority as PR
import school_status as ST

SCHEMA = "cloud.school.action/1"
ACTION_REV = 1

ACTIONS = PR.ACTIONS                              # vocabolario chiuso: identico a M9, nessuna azione nuova in v1
EXECUTABILITY = ("eseguibile", "parzialmente_eseguibile", "non_eseguibile")
RELIABILITY = ST.RELIABILITY

# Effetto di ogni gap sull'eseguibilita' (e quindi sul tetto di affidabilita' dell'azione). Ogni gap deve derivare da un dato M9/M8.
GAP_EFFECT = {
    "evidenza_non_risolvibile": "non_eseguibile",     # uid/hash di M9 non trovati negli evidenze_dettaglio di M8
    "data_non_utilizzabile": "non_eseguibile",        # l'elemento risolto non ha la data che l'azione richiede
    "argomenti_non_disponibili": "parzialmente_eseguibile",   # verifica senza testo e senza collegamenti M5 risolti
    "stato_consegna_non_disponibile": "parzialmente_eseguibile",  # compito di agenda ClasseViva: la fonte non ha consegne
    "collegamento_non_determinabile": "nessuno",      # M5 non_determinabile sulla verifica: dichiarato, non blocca il controllo
    "collegamento_in_conflitto": "nessuno",           # dichiarato; l'azione e' proprio verificare
    "contenuto_parziale": "nessuno",                  # testo troncato (testo_troncato=true): l'oggetto resta identificato
    "affidabilita_non_determinabile": "parzialmente_eseguibile",  # affidabilita' M9 non_determinabile
}
GAP_CODES = tuple(GAP_EFFECT)
EXEC_CAP = {"eseguibile": "alta", "parzialmente_eseguibile": "media", "non_eseguibile": "non_determinabile"}
COVERAGE = ("esposti_da_m9", "non_esposti_da_m9")
NOT_EXPOSED_NOTE = ("M9 espone i collegamenti M5 di una verifica (S6) solo se generano un'azione: una lista vuota di collegamenti_m5, "
                    "e quindi di comunicazioni_collegate, NON significa che non esistano collegamenti, ma che non sono esposti a questo "
                    "livello; l'elenco completo e' in school_links / school_link_explain")
UNCERTAIN_INFO = {                                # cosa non e' determinabile, dal CODICE della regola M9 (mai dal testo `motivo`)
    "P1b": "data della verifica (indizio senza data esplicita attribuita)",
    "P2": "collegamento M5 in conflitto: quale elemento corrisponde alla verifica",
    "P6": "affidabilita' dell'informazione della voce (priorita' >= attenzione con affidabilita' bassa)",
}
RULES = {
    "controllare_verifica": "contesto della verifica dall'elemento S1 risolto: data, uid, fonte (origine, docente), testo; argomenti SOLO "
                            "dal testo della verifica e dai collegamenti M5 della verifica; comunicazioni collegate solo se un "
                            "collegamento M5 risolto ha un annuncio come altro lato; collegamenti_m5_copertura dice se M9 espone i "
                            "collegamenti M5 della verifica (esposti_da_m9) o no (non_esposti_da_m9: lista vuota = non esposti, non assenti)",
    "aprire_preparazione": "segue solo il collegamento M5 citato da M9 (S6), mantenendo relazione e stato (probabile/proposto non diventa certo)",
    "controllare_compito": "contesto del compito dall'elemento S2/S3 risolto: scadenza, stato Classroom, stato consegna; per gli scaduti "
                           "la formula e' 'non consegnato secondo Classroom'",
    "leggere_comunicazione": "contesto della comunicazione dall'elemento S7 (o S1) risolto: data, classe e stato M6 come sono, testo disponibile",
    "verificare_informazione": "cosa non e' determinabile (dal codice della regola M9), oggetto coinvolto, motivi di affidabilita' M8 dei "
                               "segnali coinvolti ed evidenze: l'incertezza non viene risolta",
}
FORBIDDEN_WORDS = PR.FORBIDDEN_WORDS + ("non hai fatto", "devi", "certamente", "sicuramente")
NATURA = ("azioni assistive DERIVATE e in SOLA LETTURA sopra M9: per ogni azione il contesto gia' disponibile, l'eseguibilita', i gap e la "
          "provenienza fino al Registry; nessuna esecuzione, scrittura, automazione, piano o giudizio")
RULES_FOR_AGENTS = [
    "Priorita' (M9), affidabilita' e eseguibilita' (M10) sono tre cose diverse: alta priorita' non implica che l'azione sia eseguibile.",
    "Presenta solo il contesto riportato: non aggiungere argomenti, date, collegamenti o contenuti che non ci sono.",
    "Un collegamento 'probabile' o 'proposto' resta tale: non presentarlo come certo.",
    "Con collegamenti_m5_copertura = non_esposti_da_m9 non dire che non esistono collegamenti: non sono esposti a questo livello.",
    "Per un compito scaduto usa la formula 'non consegnato secondo Classroom': Classroom non dice se la consegna era richiesta.",
    "Con un gap 'contenuto_parziale' dichiara che il testo e' troncato.",
    "'verificare_informazione' segnala un'incertezza: non risolverla, indica cosa manca e da quali evidenze nasce.",
    "Cita la provenienza (voce M9, segnale M8, uid, hash) quando spieghi perche' un'azione e' proposta.",
]


def action_version():
    return cv.interp_version(ACTION_REV, list(ACTIONS), list(EXECUTABILITY), GAP_EFFECT, EXEC_CAP, UNCERTAIN_INFO, RULES, list(COVERAGE),
                             NOT_EXPOSED_NOTE,
                             list(FORBIDDEN_WORDS), PR.priority_version())


# ------------------------------------------------------------------------------------------------ risoluzione evidenze
def _index(doc8):
    """(segnale, uid) -> (hash citato in M8, elemento M7 di evidenze_dettaglio). Solo cio' che M8 pubblica."""
    idx = {}
    for s in doc8["segnali"]:
        hashes = {e["uid"]: e["hash"] for e in s["evidenze"] if "uid" in e}
        for el in s.get("evidenze_dettaglio") or []:
            idx[(s["id"], el["uid"])] = (hashes.get(el["uid"]), el)
    return idx


def _resolve(idx, ev):
    """Evidenza M9 -> elemento M8/M7, SOLO se uid e hash coincidono (in M8 e nell'elemento M7)."""
    hit = idx.get((ev["segnale"], ev["uid"]))
    if hit is None:
        return None
    h8, el = hit
    return el if h8 == ev["hash"] and el.get("hash") == ev["hash"] else None


def _worst(*levels):
    return max(levels, key=RELIABILITY.index)


def _gap(codice, messaggio, **extra):
    assert codice in GAP_EFFECT, codice
    return dict({"codice": codice, "effetto": GAP_EFFECT[codice], "messaggio": messaggio}, **extra)


def _executability(gaps):
    eff = [g["effetto"] for g in gaps]
    if "non_eseguibile" in eff:
        return "non_eseguibile"
    if "parzialmente_eseguibile" in eff:
        return "parzialmente_eseguibile"
    return "eseguibile"


# ------------------------------------------------------------------------------------------------ contesti per azione
def _ctx_verifica(el, voce, m9s):
    ctx = {"data": el.get("data"), "fonte": {"origine": el.get("origine"), "docente": el.get("docente"),
                                              "stato_m6": el.get("stato_m6"), "regola_m6": el.get("regola_m6")},
           "testo_verifica": el.get("testo"), "nota_testo": "testo riportato com'e': M10 non estrae argomenti"}
    ctx["fonte"] = {k: v for k, v in ctx["fonte"].items() if v is not None}
    if el.get("testo_troncato"):
        ctx["testo_troncato"] = True
    s6 = m9s.get("S6")
    links, exposed = [], False
    if s6 and isinstance(s6["valore"], list):
        for item in s6["valore"]:
            if item["verifica"] == voce["oggetto"]:
                links, exposed = [dict(l) for l in item["collegamenti"]], True
    ctx["collegamenti_m5"] = links
    ctx["collegamenti_m5_copertura"] = "esposti_da_m9" if exposed else "non_esposti_da_m9"
    if not exposed:
        ctx["nota_copertura"] = NOT_EXPOSED_NOTE
    return ctx


def _ctx_link(el):
    side = lambda s: {k: s.get(k) for k in ("uid", "tipo", "data", "materia", "stato_materia")} if s else None  # noqa: E731
    out = {"relazione": el.get("relazione"), "stato_collegamento": el.get("stato"), "regola_m5": el.get("regola"),
           "tipo": el.get("tipo"), "testo": el.get("testo"), "certezza": "non_confermato: M5 non produce mai 'confermato'"}
    if el.get("tipo") == "conflitto":
        out["elemento"] = side(el.get("elemento"))
        out["candidati"] = [side(c) for c in el.get("candidati") or []]
    else:
        out["verifica"], out["materiale"] = side(el.get("a")), side(el.get("b"))
    return out


def _ctx_compito(el, regola):
    ctx = {"scadenza": el.get("scadenza") or el.get("data"), "origine": el.get("origine"), "testo": el.get("testo")}
    for k in ("scadenza_momento", "scadenza_precisione", "corso", "stato_operativo", "stato_consegna", "docente"):
        if el.get(k) is not None:
            ctx[k] = el[k]
    if regola == "P3b":
        ctx["formulazione"] = "non consegnato secondo Classroom (Classroom non dice se la consegna era richiesta)"
    return ctx


def _ctx_comunicazione(el):
    ctx = {"data": el.get("data"), "testo": el.get("testo"), "comm_id": el.get("comm_id"), "stato_m6": el.get("stato"),
           "classe_principale": el.get("classe_principale"), "funzioni": el.get("funzioni")}
    for k in ("caso_speciale", "materiali", "testo_troncato"):
        if el.get(k) is not None:
            ctx[k] = el[k]
    return ctx


# ------------------------------------------------------------------------------------------------ una materia
def subject_actions(m9, doc8, dettaglio=False):
    """Azioni M10 di UNA materia: `m9` = voce materia del documento M9 (dettaglio=True), `doc8` = documento M8 (dettaglio=True) della
    stessa materia, stesso now. Puro: nessun accesso al registro."""
    idx = _index(doc8)
    m9s = {s["id"]: s for s in m9.get("segnali_m8") or []}
    out = []
    for voce in m9["voci"]:
        for a in voce["azioni"]:
            name, target = a["azione"], a["uid"]
            assert name in ACTIONS, name
            evs = a.get("evidenze") or [e for e in voce["evidenze"] if e["uid"] == target]
            order = {"aprire_preparazione": ("S6",), "verificare_informazione": ("S6", "S1", "S7", "S2", "S3"),
                     "leggere_comunicazione": ("S7", "S1"), "controllare_compito": ("S2", "S3"), "controllare_verifica": ("S1",)}[name]
            evs = sorted(evs, key=lambda e: (order.index(e["segnale"]) if e["segnale"] in order else 99, e["segnale"]))
            resolved = [(e, _resolve(idx, e)) for e in evs]
            el = next((r for e, r in resolved if r is not None and e["segnale"] in order), None)
            gaps, ctx = [], {}
            if not evs or el is None:
                gaps.append(_gap("evidenza_non_risolvibile", "nessuna evidenza M9 di %s risolvibile in M8 per uid+hash" % target,
                                 evidenze=[dict(e) for e in evs]))
            elif name == "controllare_verifica":
                ctx = _ctx_verifica(el, voce, m9s)
                if not ctx["data"]:
                    gaps.append(_gap("data_non_utilizzabile", "la verifica risolta non ha una data"))
                if not (ctx["testo_verifica"] or "").strip() and not [l for l in ctx["collegamenti_m5"] if l["stato"] != "non_determinabile"]:
                    gaps.append(_gap("argomenti_non_disponibili", "nessun testo della verifica e nessun collegamento M5 utilizzabile"))
                nd = [l["uid"] for l in ctx["collegamenti_m5"] if l["stato"] == "non_determinabile"]
                if nd:
                    gaps.append(_gap("collegamento_non_determinabile", "collegamenti M5 non_determinabili sulla verifica: nessuna "
                                     "preparazione proposta da questi", uid=nd))
                if ctx.get("testo_troncato"):
                    gaps.append(_gap("contenuto_parziale", "testo della verifica troncato"))
                ann = [s for l in voce["azioni"] if l["azione"] in ("aprire_preparazione", "verificare_informazione") and l["uid"] != target
                       for e in (l.get("evidenze") or []) for r in [_resolve(idx, e)] if r for s in [r.get("b")] if s and s.get("tipo") == "announcement"]
                ctx["comunicazioni_collegate"] = [{"uid": s["uid"], "data": s.get("data")} for s in ann]
            elif name == "aprire_preparazione":
                ctx = _ctx_link(el)
            elif name == "controllare_compito":
                ctx = _ctx_compito(el, voce["regola"])
                if not ctx["scadenza"]:
                    gaps.append(_gap("data_non_utilizzabile", "il compito risolto non ha una scadenza"))
                if el.get("origine") == "agenda_classeviva":
                    gaps.append(_gap("stato_consegna_non_disponibile", "compito di agenda ClasseViva: la fonte non registra consegne"))
            elif name == "leggere_comunicazione":
                ctx = _ctx_comunicazione(el)
                if el.get("testo_troncato"):
                    gaps.append(_gap("contenuto_parziale", "testo della comunicazione troncato (testo_troncato=true)"))
            else:                                               # verificare_informazione
                code = "P2" if a.get("evidenze") else ("P1b" if voce["regola"] == "P1b" and target == voce["oggetto"] else "P6")
                sigs = sorted({e["segnale"] for e in evs})
                ctx = {"informazione_non_determinabile": UNCERTAIN_INFO[code], "origine_regola_m9": code, "oggetto": target,
                       "dati_mancanti_o_incerti": [{"segnale": sid, "affidabilita": m9s[sid]["affidabilita"],
                                                   "motivi_affidabilita_m8": m9s[sid]["motivi_affidabilita"]} for sid in sigs if sid in m9s],
                       "elemento": _ctx_link(el) if el.get("relazione") else _ctx_comunicazione(el) if "comm_id" in el else
                       {k: el.get(k) for k in ("data", "testo", "origine")}}
                if code == "P2":
                    gaps.append(_gap("collegamento_in_conflitto", "conflitto M5: nessun candidato scelto", uid=[target]))
                if el.get("testo_troncato"):
                    gaps.append(_gap("contenuto_parziale", "testo troncato (testo_troncato=true)"))
            aff9 = a["affidabilita"]
            if aff9 == "non_determinabile":
                gaps.append(_gap("affidabilita_non_determinabile", "affidabilita' M9 non_determinabile: dato non verificabile"))
            ex = _executability(gaps)
            aff = _worst(aff9, EXEC_CAP[ex])
            rec = {"id": CM.short_hash(ACTION_REV, voce["id"], name, target), "azione": name, "target": target,
                   "eseguibilita": ex, "affidabilita": aff, "affidabilita_m9": aff9,
                   "priorita_m9": voce["priorita"], "voce_m9": voce["id"], "regola_m9": voce["regola"],
                   "contesto": ctx, "gaps": gaps,
                   "provenienza": {"m9": {"voce": voce["id"], "regola": voce["regola"], "azione_secondaria_da": a.get("secondaria_da")},
                                   "m8": [{"segnale": e["segnale"], "uid": e["uid"], "hash": e["hash"], "risolta": r is not None}
                                          for e, r in resolved],
                                   "m7": {"uid": el["uid"], "hash": el["hash"]} if el else None,
                                   "catena": "Registry (content_hash) -> M7 elemento -> M8 %s -> M9 voce %s -> M10" % (
                                       "/".join(sorted({e["segnale"] for e in evs})) or "?", voce["id"])}}
            if rec["provenienza"]["m9"]["azione_secondaria_da"] is None:
                rec["provenienza"]["m9"].pop("azione_secondaria_da")
            if dettaglio:
                rec["elementi_m8"] = [r for _, r in resolved if r is not None]
            out.append(rec)
    return {"materia": m9["materia"], "nome_ufficiale": m9["nome_ufficiale"], "priorita_m9": m9["priorita"],
            "affidabilita_m9": m9["affidabilita"], "stato_decisione_m9": m9["stato_decisione"],
            "regole_non_valutate_m9": m9["regole_non_valutate"], "azioni": out,
            "sintesi": {e: sum(1 for x in out if x["eseguibilita"] == e) for e in EXECUTABILITY}}


# ------------------------------------------------------------------------------------------------ costruzione
def build(reg, now_dt, manual=None, materia=None, dettaglio=False, soglia_ore=48, *, subjects, source_path=None):
    """cloud.school.action/1: una materia o le 8. Sola lettura; `now_dt` letto UNA volta dal chiamante e passato a M9 e M8."""
    if not isinstance(dettaglio, bool):
        raise ValueError("dettaglio non valido: %r (true | false)" % (dettaglio,))
    m9 = PR.build(reg, now_dt, manual=manual, materia=materia, dettaglio=True, soglia_ore=soglia_ore, subjects=subjects,
                  source_path=source_path)
    materie = []
    for m in m9["materie"]:                                   # stesso ordine di M9
        doc8 = ST.build(reg, m["materia"], now_dt, manual=manual, dettaglio=True, soglia_ore=soglia_ore, subjects=subjects,
                        source_path=source_path)
        materie.append(subject_actions(m, doc8, dettaglio=dettaglio))
    allx = [x for m in materie for x in m["azioni"]]
    return {
        "schema": SCHEMA,
        "natura": NATURA,
        "riferimento_temporale": dict(m9["riferimento_temporale"], nota="riferimento temporale di M9 (nessuna nuova interpretazione)"),
        "politica": {"versione": action_version(), "azioni": list(ACTIONS), "eseguibilita": list(EXECUTABILITY),
                     "gap": GAP_EFFECT, "tetto_affidabilita_per_eseguibilita": EXEC_CAP, "regole": RULES,
                     "monotonia": "affidabilita' azione = peggiore tra quella M9 e il tetto dell'eseguibilita': mai sopra M9"},
        "ambito": dict(m9["ambito"], dettaglio=dettaglio),
        "sintesi": {"azioni": len(allx), "per_eseguibilita": {e: sum(1 for x in allx if x["eseguibilita"] == e) for e in EXECUTABILITY},
                    "per_azione": {a: sum(1 for x in allx if x["azione"] == a) for a in ACTIONS}},
        "materie": materie,
        "provenance": {"schema": SCHEMA, "action_version": action_version(), "priority_version": m9["provenance"]["priority_version"],
                       "status_version": m9["provenance"]["status_version"], "context_version": m9["provenance"]["context_version"],
                       "sorgente": source_path, "input": "contratto pubblico M9 + risoluzione delle sole evidenze M9 nel contratto M8"},
        "regole_agente": RULES_FOR_AGENTS,
    }


def build_from_path(path, now, manual_path=None, **kw):
    """Registro aperto come COPIA in memoria (Registry.from_snapshot, immutable=1): nessun byte del file viene scritto."""
    reg = R.Registry.from_snapshot(path)
    try:
        return build(reg, now, manual=SC.load_manual(manual_path)["dati"], source_path=os.path.abspath(path), **kw)
    finally:
        reg.close()
