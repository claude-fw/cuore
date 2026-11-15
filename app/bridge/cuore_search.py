"""Ricerca globale di CUORE (`cuore.search/1`): una sola implementazione per la dashboard (`/api/search`) e per l'agente
(server MCP `cuore`, strumento `cuore_cerca`).

Cerca SOLO in cio' che i feed gia' espongono alla dashboard, in memoria, senza nuove letture:
  School  - `cuore.school.feed/1`: materie e voci dei blocchi (verifiche, impegni, obiettivi, materiali collegati, ...),
            solo campi descrittivi (testo, oggetto, materia, tipo, data); mai uid, hash, versioni o percorsi.
  Money   - `cuore.money.feed/1`: conti, categorie, obiettivi, acquisti pianificati e, se richiesto, i movimenti degli
            ultimi 30 giorni (descrizione, data, importo). L'agente li riceve solo chiedendoli esplicitamente.
  Classroom - `cuore.school.classroom_index/1` (cuore_classroom.py): corsi, argomenti, compiti, annunci e riferimenti ai
            materiali (file Drive, link, video, allegati) con il LINK ORIGINALE (solo https) e lo stato del contenuto: si
            cerca in titolo, corso, argomento, materia e testo del compito/annuncio, MAI nel contenuto dei file (non letto).
  Eventi  - `cuore.events/1` gia' validato: tipo, esito e dettaglio a vocabolario chiuso.
Ogni fonte e' dichiarata in `fonti` (`letta` | `non_disponibile` | `non_collegata`): una fonte non letta non diventa mai
«nessun risultato». Nessuna credenziale, nessun dato fuori dai feed, nessun testo libero degli errori.
"""
from __future__ import annotations

import re
import unicodedata

SCHEMA = "cuore.search/1"
MIN_Q, MAX_Q, MAX_RESULTS = 2, 80, 200
SCHOOL_SECTION = {"home": "oggi", "voti": "voti", "assenze": "assenze", "verifiche": "verifiche", "compiti": "compiti",
                  "materiali": "materiali", "studio": "studio", "qualita_dati": "oggi"}
TITLE_KEYS = ("testo", "oggetto", "obiettivo", "tipo", "periodo", "materia")
DETAIL_KEYS = ("materia", "tipo", "motivo", "affidabilita", "stato_decisione", "origine")
EVENT_LABEL = {"giro_job": "giro del job", "anomalia_giro": "anomalia nel giro", "orario_mancato": "orario senza avvio",
               "orario_non_verificabile": "orario non verificabile", "problema_aperto": "problema aperto",
               "notifica_inviata": "notifica inviata", "chiusura_in_attesa": "chiusura in attesa",
               "run_registro": "lettura nel Registry", "importazione_riuscita": "importazione riuscita",
               "anomalia_dati": "anomalia dei dati", "azione": "azione",
               "sincronizzazione_bancaria": "sincronizzazione bancaria", "consenso_bancario": "consenso bancario"}
_DATE = re.compile(r"^\d{4}-\d\d-\d\d")


class QueryError(ValueError):
    pass


def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _str(v):
    return v.strip() if isinstance(v, str) and v.strip() else None


def _item(modulo, tipo, titolo, dettaglio, data, rotta, provenienza, extra="", search_detail=True, link=None):
    """`search_detail=False`: il dettaglio e' solo descrittivo (es. «conto X · in attesa») e non entra nella ricerca,
    altrimenti una parola di contorno farebbe trovare ogni voce; si cerca in titolo ed `extra`."""
    words = (titolo, dettaglio or "" if search_detail else "", extra)
    return {"modulo": modulo, "tipo": tipo, "titolo": titolo, "dettaglio": dettaglio, "data": data, "rotta": rotta,
            "link": link if isinstance(link, str) and link.startswith("https://") else None,
            "provenienza": provenienza, "_testo": norm(" ".join(x for x in words if x)), "_titolo": norm(titolo)}


def school_items(feed):
    out = []
    for key, desc in sorted((feed.get("materie") or {}).items()):
        name = _str(desc) or _str(key)
        if name:
            out.append(_item("school", "materia", name, None, None, "school/studio", {"fonte": "materie"}, key))
    for page, doc in sorted((feed.get("pagine") or {}).items()):
        for b in doc.get("blocchi") or []:
            if b.get("stato") != "dati":
                continue
            d = b.get("dati")
            lists = [d] if isinstance(d, list) else [v for v in d.values() if isinstance(v, list)] if isinstance(d, dict) else []
            for lst in lists:
                for x in lst:
                    if not isinstance(x, dict):
                        continue
                    title = next((_str(x.get(k)) for k in TITLE_KEYS if _str(x.get(k))), None)
                    if not title:
                        continue
                    det = " · ".join(dict.fromkeys(v for v in (_str(x.get(k)) for k in DETAIL_KEYS) if v and v != title))
                    day = x.get("data") if isinstance(x.get("data"), str) and _DATE.match(x["data"]) else None
                    out.append(_item("school", b.get("titolo") or b.get("id") or page, title, det or None,
                                     day[:10] if day else None, "school/%s" % SCHOOL_SECTION.get(page, "oggi"),
                                     {"fonte": "pagina %s" % page, "blocco": b.get("id")}, b.get("titolo") or ""))
    return out


def _money(minor, cur):
    if not isinstance(minor, int):
        return None
    return "%s%d,%02d %s" % ("-" if minor < 0 else "", abs(minor) // 100, abs(minor) % 100, cur or "")


def money_items(feed, movements=True):
    ops, out = feed.get("operazioni") or {}, []
    data = lambda op: (ops.get(op) or {}).get("data") if (ops.get(op) or {}).get("ok") else None    # noqa: E731
    for a in data("accounts") or []:
        if _str(a.get("name")):
            out.append(_item("money", "conto", a["name"], " · ".join(x for x in (a.get("type"), a.get("currency"),
                             "chiuso" if a.get("closed") else None) if x), a.get("lastMovementOn"), "money/saldi",
                             {"fonte": "accounts"}, a.get("id") or ""))
    for c in (data("categories") or {}).get("items") or []:
        if _str(c.get("name")):
            out.append(_item("money", "categoria", c["name"], c.get("budgetGroup"), None, "money/budget", {"fonte": "categories"}))
    plan = data("planning") or {}
    for g in plan.get("goals") or []:
        if _str(g.get("name")):
            out.append(_item("money", "obiettivo", g["name"], g.get("status"), g.get("deadline"), "money/budget", {"fonte": "planning"}))
    for p in plan.get("purchases") or []:
        if _str(p.get("title")):
            out.append(_item("money", "acquisto pianificato", p["title"], p.get("status"), None, "money/budget", {"fonte": "planning"}))
    if movements:
        for t in (data("transactions") or {}).get("items") or []:
            if _str(t.get("description")):
                out.append(_item("money", "movimento", t["description"], " · ".join(x for x in (
                    _money(t.get("amountMinor"), t.get("currency")), "conto %s" % t["accountId"] if t.get("accountId") else None,
                    "in attesa" if t.get("status") == "pending" else None) if x), t.get("date"), "money/movimenti",
                    {"fonte": "transactions (ultimi 30 giorni)"},
                    " ".join(x for x in (t.get("accountId"), _money(t.get("amountMinor"), t.get("currency"))) if x),
                    search_detail=False))
    return out


def classroom_items(doc):
    out = []
    for v in doc.get("voci") or []:
        if not isinstance(v, dict) or not _str(v.get("titolo")):
            continue
        det = " · ".join(x for x in (v.get("corso"), v.get("argomento"), v.get("materia") and "materia " + v["materia"],
                                     v.get("origine") and "da " + v["origine"] + (": " + v["elemento"] if v.get("elemento") else ""),
                                     v.get("contenuto") and "contenuto " + v["contenuto"],
                                     "storico" if v.get("perimetro") == "storico" else None) if x)
        out.append(_item("school", v.get("tipo"), v["titolo"], det or None, v.get("data"), v.get("rotta") or "school/materiali",
                         {"fonte": "Classroom (Registry)"},
                         " ".join(str(v.get(k) or "") for k in ("corso", "argomento", "materia", "elemento", "testo", "tipo")),
                         search_detail=False, link=v.get("link")))
    return out


def event_items(doc):
    out = []
    for e in doc.get("eventi") or []:
        det = " · ".join(str(v) for v in (e.get("dettaglio") or {}).values() if isinstance(v, (str, int)) and not isinstance(v, bool))
        out.append(_item("eventi", e.get("area"), "%s · %s" % (EVENT_LABEL.get(e.get("tipo"), e.get("tipo")), e.get("esito") or "—"),
                         det or None, (e.get("quando") or "")[:10] or None, "cuore/eventi", {"fonte": e["fonte"]["origine"]}))
    return out


def search(q, school_fn=None, money_fn=None, events_fn=None, limit=50, movements=True, classroom_fn=None):
    """Risultati per tutte le parole della domanda (senza accenti e maiuscole), titoli prima, poi i piu' recenti.
    Le funzioni `*_fn` restituiscono il feed (o sollevano): None = fonte non collegata."""
    q = (q or "").strip()
    if not MIN_Q <= len(q) <= MAX_Q:
        raise QueryError("la ricerca richiede da %d a %d caratteri" % (MIN_Q, MAX_Q))
    words = norm(q).split()
    if not words:
        raise QueryError("la ricerca non contiene lettere o cifre")
    fonti, items = {}, []
    for name, fn, build, ok in (("school", school_fn, school_items, lambda f: isinstance(f.get("pagine"), dict)),
                                ("classroom", classroom_fn, classroom_items,
                                 lambda f: f.get("sola_lettura_verificata") is True and not f.get("errori")),
                                ("money", money_fn, lambda f: money_items(f, movements), lambda f: isinstance(f.get("operazioni"), dict)),
                                ("eventi", events_fn, event_items, lambda f: isinstance(f.get("eventi"), list) and not f.get("errori"))):
        if fn is None:
            fonti[name] = {"stato": "non_collegata"}
            continue
        try:
            feed = fn()
            if not isinstance(feed, dict) or not ok(feed):
                raise ValueError("feed non riconosciuto")
            got = build(feed)
            fonti[name] = {"stato": "letta", "voci": len(got)}
            if name == "money" and not movements:
                fonti[name]["movimenti"] = "esclusi"
            items += got
        except Exception as e:      # noqa: BLE001 - fonte non leggibile: dichiarata, le altre restano
            fonti[name] = {"stato": "non_disponibile", "motivo": type(e).__name__}
    hits = []
    for it in items:
        if all(w in it["_testo"] for w in words):
            score = sum(2 if re.search(r"\b" + re.escape(w), it["_titolo"]) else 1 for w in words)
            hits.append((score, it))
    ordered = sorted(hits, key=lambda h: (h[0], h[1]["data"] or ""), reverse=True)     # stabile: a parita', ordine delle fonti
    limit = max(1, min(int(limit), MAX_RESULTS))
    out = [{k: v for k, v in it.items() if not k.startswith("_")} for _, it in ordered[:limit]]
    return {"schema": SCHEMA, "domanda": q, "risultati": out, "totale": len(ordered), "omessi": max(0, len(ordered) - limit),
            "fonti": fonti}
