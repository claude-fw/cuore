"""Contratto `cuore.school.feed/1`: validazione e normalizzazione del feed di CUORE School (stesse convenzioni di CUORE
Money, `cuore_money.py`). Documentazione: `docs/it/contratto-school.md`.

- Ogni pagina e' un documento del motore `cloud.school.dashboard/1`, validato nella struttura (blocchi, stati, grafici,
  provenienza). Una pagina non conforme NON arriva alla dashboard: e' tolta e l'errore `contratto_non_valido` la nomina.
- Errori: `{ambito, codice, messaggio}` con messaggio PUBBLICO fisso per codice (nessun testo di eccezione, percorso o
  dato scolastico). Il tipo dell'eccezione, se utile, resta in `dettaglio` (solo nome della classe).
- `esito`: completo | parziale | non_disponibile (stessa definizione di Money). `freschezza.stato`: aggiornati |
  dati_vecchi | fonti_non_lette | non_disponibile. Risultato vuoto = blocchi `vuoto_valido`, mai un errore.
- Provenienza: conservata cosi' com'e', tranne `provenance.sorgente` (percorso assoluto del Registry, con la cartella
  personale) ridotto al nome del file.
Le stesse funzioni servono a un consumatore futuro (es. un agente) per validare un feed ricevuto.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.normpath(os.path.join(HERE, "..", "..", "scuola", "mcp", "classeviva"))
for _p in (ENGINE, os.path.join(ENGINE, "..", "_common")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import school_dashboard as SD  # noqa: E402   (stati dei blocchi e tipi di grafico: unica definizione, nel motore)

SCHEMA = "cuore.school.feed/1"
PAGE_SCHEMA = SD.SCHEMA
ESITI = ("completo", "parziale", "non_disponibile")
FRESHNESS_STATES = ("aggiornati", "dati_vecchi", "fonti_non_lette", "non_disponibile")
PUBLIC_MESSAGES = {
    "registro_assente": "registro locale della scuola assente",
    "materie_non_disponibili": "istantanea delle materie assente o non valida",
    "rete_bloccata": "il motore ha tentato un accesso di rete durante la lettura: pagina non calcolata",
    "errore_motore": "errore del motore nel calcolo della pagina",
    "sola_lettura_violata": "un file letto e' cambiato durante la lettura: nessun dato mostrato",
    "contratto_non_valido": "pagina non conforme al contratto: non mostrata",
}


def _is_date(v):
    try:
        return isinstance(v, str) and len(v) == 10 and dt.date.fromisoformat(v) is not None
    except ValueError:
        return False


def _is_instant(v):
    try:
        return isinstance(v, str) and dt.datetime.fromisoformat(v).tzinfo is not None
    except ValueError:
        return False


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_page(doc, name=None):
    """Problemi strutturali di UNA pagina `cloud.school.dashboard/1` (vuoto = conforme). Non giudica i contenuti dei
    blocchi (sono contratti del motore), ma che stati, provenienza e grafici siano interpretabili senza ambiguita'."""
    p = []
    if not isinstance(doc, dict):
        return ["pagina: oggetto atteso"]
    if doc.get("schema") != PAGE_SCHEMA:
        p.append("schema della pagina: %r invece di %r" % (doc.get("schema"), PAGE_SCHEMA))
    if not isinstance(doc.get("pagina"), str) or (name is not None and doc.get("pagina") != name):
        p.append("pagina: nome assente o diverso dalla chiave")
    rt = doc.get("riferimento_temporale")
    if not (isinstance(rt, dict) and _is_instant(rt.get("now")) and _is_date(rt.get("data_riferimento"))):
        p.append("riferimento_temporale: now (istante con fuso) e data_riferimento (YYYY-MM-DD) obbligatori")
    prov = doc.get("provenance")
    if not (isinstance(prov, dict) and isinstance(prov.get("dashboard_version"), str)):
        p.append("provenance.dashboard_version obbligatoria")
    blocks = doc.get("blocchi")
    if not isinstance(blocks, list):
        p.append("blocchi: lista attesa")
        blocks = []
    for i, b in enumerate(blocks):
        w = "blocchi[%d]" % i
        if not isinstance(b, dict):
            p.append(w + ": oggetto atteso")
            continue
        if not (isinstance(b.get("id"), str) and isinstance(b.get("titolo"), str)):
            p.append(w + ": id e titolo (testo) obbligatori")
        if b.get("stato") not in SD.BLOCK_STATES:
            p.append("%s.stato: %r non ammesso" % (w, b.get("stato")))
        if not isinstance(b.get("provenienza"), dict):
            p.append(w + ".provenienza: oggetto atteso")
        elif "contratto" in b["provenienza"] and not isinstance(b["provenienza"]["contratto"], str):
            p.append(w + ".provenienza.contratto: testo atteso")
        if b.get("stato") == "dati" and b.get("dati") is None:
            p.append(w + ": stato 'dati' senza dati")
    charts = doc.get("grafici")
    if not isinstance(charts, list):
        p.append("grafici: lista attesa")
        charts = []
    for i, c in enumerate(charts):
        w = "grafici[%d]" % i
        if not isinstance(c, dict) or c.get("tipo") not in SD.CHART_TYPES or not isinstance(c.get("id"), str):
            p.append(w + ": id e tipo ammesso obbligatori")
            continue
        if not isinstance(c.get("provenienza"), dict):
            p.append(w + ".provenienza: oggetto atteso")
        series = c.get("serie")
        if not isinstance(series, list) or not series:
            p.append(w + ".serie: lista non vuota attesa")
            continue
        # calendario: x = data, y = etichetta (es. materia); tutti gli altri tipi: y numerico
        ok_point = ((lambda q: _is_date(q.get("x")) and isinstance(q.get("y"), str) and q["y"].strip() != "")
                    if c["tipo"] == "calendario" else (lambda q: _is_num(q.get("y"))))
        for j, s in enumerate(series):
            pts = s.get("punti") if isinstance(s, dict) else None
            if not isinstance(pts, list) or not all(isinstance(q, dict) and ok_point(q) for q in pts):
                p.append("%s.serie[%d].punti: punti non conformi al tipo %s (nessun punto inventato)" % (w, j, c["tipo"]))
    return p


def validate_feed(feed):
    """Problemi strutturali del feed intero (vuoto = conforme al contratto). Usato dai test e da consumatori esterni."""
    p = []
    if not isinstance(feed, dict):
        return ["feed: oggetto atteso"]
    if feed.get("schema") != SCHEMA:
        return ["schema: %r non supportato (atteso %r)" % (feed.get("schema"), SCHEMA)]
    if feed.get("esito") not in ESITI:
        p.append("esito: %r non ammesso" % feed.get("esito"))
    if not isinstance(feed.get("sola_lettura_verificata"), bool):
        p.append("sola_lettura_verificata: booleano obbligatorio")
    if not _is_instant(feed.get("generato")):
        p.append("generato: istante con fuso obbligatorio")
    if not isinstance(feed.get("pagine"), dict):
        p.append("pagine: oggetto atteso")
    else:
        for name, doc in feed["pagine"].items():
            p += ["%s: %s" % (name, x) for x in validate_page(doc, name)]
    errs = feed.get("errori")
    if not isinstance(errs, list) or not all(isinstance(e, dict) and isinstance(e.get("ambito"), str)
                                             and e.get("codice") in PUBLIC_MESSAGES and e.get("messaggio") == PUBLIC_MESSAGES[e.get("codice")]
                                             for e in errs):
        p.append("errori: {ambito, codice ammesso, messaggio pubblico del codice} attesi")
    fr = feed.get("freschezza")
    if not (isinstance(fr, dict) and isinstance(fr.get("disponibile"), bool) and fr.get("stato") in FRESHNESS_STATES):
        p.append("freschezza: disponibile (booleano) e stato ammesso obbligatori")
    if feed.get("esito") == "completo" and (feed.get("errori") or not feed.get("pagine")):
        p.append("esito 'completo' incoerente con errori o pagine assenti")
    return p


def freshness_state(fr):
    if not isinstance(fr, dict) or not fr.get("disponibile"):
        return "non_disponibile"
    if fr.get("tipi_obsoleti"):
        return "dati_vecchi"
    if any((s or {}).get("non_letti") for s in (fr.get("per_fonte") or {}).values()):
        return "fonti_non_lette"
    return "aggiornati"


def error(ambito, codice, dettaglio=None):
    e = {"ambito": ambito, "codice": codice, "messaggio": PUBLIC_MESSAGES[codice]}
    if dettaglio:
        e["dettaglio"] = str(dettaglio)[:60]
    return e


def finalize(feed, freshness=None):
    """Applica il contratto al feed costruito dal ponte, in un solo passaggio (nessun ricalcolo delle pagine):
    pagine non conformi tolte, percorso personale tolto, freschezza ed esito espliciti. `freshness(pagine validate)`
    calcola la freschezza solo dopo la validazione (mai da una pagina scartata). Mai eccezioni."""
    pages = feed.get("pagine") if isinstance(feed.get("pagine"), dict) else {}
    for name in list(pages):
        doc = pages[name]
        prov = doc.get("provenance") if isinstance(doc, dict) else None
        if isinstance(prov, dict) and isinstance(prov.get("sorgente"), str):
            prov["sorgente"] = os.path.basename(prov["sorgente"])
        if validate_page(doc, name):
            del pages[name]
            feed["errori"].append(error(name, "contratto_non_valido"))
    feed["pagine"] = pages
    if freshness is not None and feed.get("sola_lettura_verificata"):
        feed["freschezza"] = freshness(pages)
    fr = feed.get("freschezza") if isinstance(feed.get("freschezza"), dict) else {"disponibile": False}
    fr["stato"] = freshness_state(fr)
    feed["freschezza"] = fr
    au = feed.get("automazione")
    if au is not None and not (isinstance(au, dict) and isinstance(au.get("disponibile"), bool)):
        feed["automazione"] = {"disponibile": False, "motivo": "stato dell'automazione non conforme al contratto"}
    if not feed.get("sola_lettura_verificata") or not pages:
        feed["esito"] = "non_disponibile"
    else:
        feed["esito"] = "parziale" if feed["errori"] else "completo"
    feed["contratto"] = {"schema": SCHEMA, "pagine": PAGE_SCHEMA, "documentazione": "docs/it/contratto-school.md"}
    return feed
