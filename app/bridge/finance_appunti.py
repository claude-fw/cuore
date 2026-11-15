"""Appunti personali di Finance con promemoria facoltativo: archivio SEPARATO dal database Finance (che resta il motore
dei soldi) e da School. Un file JSON Lines di soli eventi aggiunti, mai riscritti; lo stato si ricava rileggendoli.
Nessuna cancellazione: un appunto si completa o si annulla (con motivo) e resta nel file.

Eventi ammessi (campo `tipo`; `id` = l'appunto a cui si riferisce l'evento):
- `crea`       {testo, categoria?, quando?, promemoria?, acquisto?}   nuovo appunto
- `modifica`   {id, testo?, categoria?, quando?, acquisto?}           cambia i campi indicati (null = toglie il campo)
- `promemoria` {id, attivo: bool}                                     accende o spegne il promemoria dell'appunto
- `completa`   {id}                                                   fatto (non si modifica piu')
- `annulla`    {id, motivo?}                                          annullato (non si modifica piu')
Campi: `testo` 1-300 caratteri; `categoria` facoltativa, parola minuscola (suggerite: da_comprare, budget, spese,
altro); `quando` facoltativo, ora italiana `AAAA-MM-GG` o `AAAA-MM-GGTHH:MM` (solo data = dal primo orario di quel
giorno); `promemoria` vero/falso (predefinito: vero se c'e' `quando`), acceso solo con `quando`; `acquisto`
facoltativo, id di un acquisto pianificato di Finance (solo il collegamento: titolo e importo restano nel database).
Il chiamante sceglie il file: sui dati reali l'archivio verificato da `scritture.archive` (predefinito
`~/Library/Application Support/CUORE/finance/appunti.jsonl`, fuori da iCloud e dal repository); file creato 0600.
`richiesta` (il `requestId` del ponte): la stessa richiesta ripetuta restituisce l'evento gia' scritto, con un
contenuto diverso viene rifiutata; nessun doppione anche dopo un riavvio del ponte.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid

SCHEMA = "cuore.finance.appunti/1"
MAX_EVENTS = 20000
_WHEN = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$")
_KEY = re.compile(r"^[a-z0-9][a-z0-9_:.-]{0,80}$")
_FIELDS = {"testo", "categoria", "quando", "acquisto"}


class NonValido(ValueError):
    pass


def _text(v, n, name):
    if not isinstance(v, str) or not v.strip() or len(v) > n:
        raise NonValido(f"{name}: testo da 1 a {n} caratteri")
    return v.strip()


def _key(v, name, optional=False):
    if v is None and optional:
        return None
    if not isinstance(v, str) or not _KEY.match(v):
        raise NonValido(f"{name} non valido")
    return v


def _when(v):
    if v is None:
        return None
    try:
        if isinstance(v, str) and _WHEN.match(v):
            dt.datetime.fromisoformat(v)
            return v
    except ValueError:
        pass
    raise NonValido("quando: AAAA-MM-GG oppure AAAA-MM-GGTHH:MM (ora italiana)")


def _field(name, v):
    return {"testo": lambda: _text(v, 300, "testo"), "categoria": lambda: _key(v, "categoria", True),
            "quando": lambda: _when(v), "acquisto": lambda: _key(v, "acquisto", True)}[name]()


def valida(ev):
    """Evento pulito (solo i campi ammessi) o NonValido. Le regole che dipendono dallo stato sono in `aggiungi`."""
    if not isinstance(ev, dict):
        raise NonValido("evento non valido")
    t = ev.get("tipo")
    if t == "crea":
        if set(ev) - _FIELDS - {"tipo", "promemoria"}:
            raise NonValido("crea: campi ammessi testo, categoria, quando, promemoria, acquisto")
        out = {"tipo": t, **{k: _field(k, ev.get(k)) for k in sorted(_FIELDS)}}
        on = ev.get("promemoria", out["quando"] is not None)
        if not isinstance(on, bool):
            raise NonValido("promemoria: vero o falso")
        if on and out["quando"] is None:
            raise NonValido("promemoria acceso senza data: indicare quando")
        return dict(out, promemoria=on)
    target = _key(ev.get("id"), "id")
    if t == "modifica":
        changes = {k: _field(k, ev[k]) for k in sorted(_FIELDS) if k in ev}
        if not changes or set(ev) - _FIELDS - {"tipo", "id"}:
            raise NonValido("modifica: indicare almeno uno fra testo, categoria, quando, acquisto")
        return {"tipo": t, "appunto": target, **changes}
    if t == "promemoria":
        if not isinstance(ev.get("attivo"), bool):
            raise NonValido("attivo: vero o falso")
        return {"tipo": t, "appunto": target, "attivo": ev["attivo"]}
    if t == "completa":
        return {"tipo": t, "appunto": target}
    if t == "annulla":
        return {"tipo": t, "appunto": target, "motivo": _text(ev.get("motivo") or "annullato", 200, "motivo")}
    raise NonValido("tipo sconosciuto")


def leggi(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _apply(evs):
    """id -> appunto, applicando gli eventi in ordine."""
    out = {}
    for e in evs:
        if e["tipo"] == "crea":
            out[e["id"]] = {"id": e["id"], "testo": e["testo"], "categoria": e.get("categoria"), "quando": e.get("quando"),
                            "promemoria": e.get("promemoria", False), "acquisto": e.get("acquisto"), "stato": "aperto",
                            "creato": e["at"], "aggiornato": e["at"], "motivo": None}
            continue
        a = out.get(e.get("appunto"))
        if a is None:
            continue
        a["aggiornato"] = e["at"]
        if e["tipo"] == "modifica":
            a.update({k: e[k] for k in _FIELDS if k in e})
            if a["quando"] is None:
                a["promemoria"] = False
        elif e["tipo"] == "promemoria":
            a["promemoria"] = e["attivo"]
        elif e["tipo"] == "completa":
            a["stato"] = "completato"
        elif e["tipo"] == "annulla":
            a["stato"], a["motivo"] = "annullato", e["motivo"]
    return out


def aggiungi(path, ev, now=None, richiesta=None):
    ev = valida(ev)
    evs = leggi(path)
    if richiesta is not None:
        if not isinstance(richiesta, str) or not 1 <= len(richiesta) <= 100:
            raise NonValido("requestId non valido")
        prev = next((e for e in evs if e.get("richiesta") == richiesta), None)
        if prev is not None:
            if {k: prev.get(k) for k in ev} != ev:
                raise NonValido("richiesta gia' usata con un contenuto diverso")
            return prev
    if len(evs) >= MAX_EVENTS:
        raise NonValido("archivio pieno")
    if ev["tipo"] != "crea":
        a = _apply(evs).get(ev["appunto"])
        if a is None:
            raise NonValido("appunto inesistente")
        if a["stato"] != "aperto":
            raise NonValido("appunto gia' %s: non si modifica" % a["stato"])
        if ev["tipo"] == "promemoria" and ev["attivo"] and a["quando"] is None:
            raise NonValido("promemoria acceso senza data: indicare quando")      # togliere la data lo spegne da solo
    rec = {"id": uuid.uuid4().hex, "at": (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds"), **ev}
    if richiesta is not None:
        rec["richiesta"] = richiesta
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return rec


def stato(path):
    """Tutti gli appunti (aperti prima, per data del promemoria; poi i chiusi), con lo stato corrente."""
    evs = leggi(path)
    items = list(_apply(evs).values())
    items.sort(key=lambda a: (a["stato"] != "aperto", a["quando"] is None, a["quando"] or "", a["creato"]))
    return {"schema": SCHEMA, "appunti": items, "aperti": sum(a["stato"] == "aperto" for a in items), "eventi": len(evs)}
