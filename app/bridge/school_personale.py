"""Dati personali di School (E4): compiti fatti, note, impegni, obiettivi di media. Archivio SEPARATO dal Registry
(che resta in sola lettura) e da Finance: un file JSON Lines di soli eventi aggiunti, mai riscritti; lo stato si
ricava rileggendoli. Nessuna cancellazione: note e impegni si annullano con un evento `annulla` (con motivo).

Eventi ammessi (campo `tipo`):
- `compito`   {uid, fatto: bool}                         compito o verifica del registro segnato fatto / da fare
- `nota`      {testo, materia?, uid?}                    nota personale, eventualmente legata a una materia o voce
- `impegno`   {testo, data AAAA-MM-GG, materia?}         impegno proprio (ripasso, ripetizioni, ...)
- `obiettivo` {materia, media 1-10 | null}               obiettivo di media per materia (null = tolto)
- `annulla`   {id, motivo?}                              annulla una nota o un impegno
Il chiamante sceglie il file: in prova la cartella della sessione sintetica; sui dati reali l'archivio personale
verificato da `scritture.school_archive` (fuori da iCloud e dal repository). File creato con permessi 0600.
`richiesta` (facoltativa, il `requestId` del ponte): la stessa richiesta ripetuta restituisce l'evento gia' scritto,
con un contenuto diverso viene rifiutata; nessun doppione anche dopo un riavvio del ponte.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid

SCHEMA = "cuore.school.personale/1"
MAX_EVENTS = 20000
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEY = re.compile(r"^[a-z0-9][a-z0-9_:.-]{0,80}$")


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


def valida(ev):
    """Evento pulito (solo i campi ammessi) o NonValido."""
    if not isinstance(ev, dict):
        raise NonValido("evento non valido")
    t = ev.get("tipo")
    if t == "compito":
        if not isinstance(ev.get("fatto"), bool):
            raise NonValido("fatto: vero o falso")
        return {"tipo": t, "uid": _key(ev.get("uid"), "uid"), "fatto": ev["fatto"]}
    if t == "nota":
        return {"tipo": t, "testo": _text(ev.get("testo"), 500, "nota"), "materia": _key(ev.get("materia"), "materia", True),
                "uid": _key(ev.get("uid"), "uid", True)}
    if t == "impegno":
        d = ev.get("data")
        try:
            dt.date.fromisoformat(d if isinstance(d, str) and _DATE.match(d) else "")
        except ValueError:
            raise NonValido("data: AAAA-MM-GG") from None
        return {"tipo": t, "testo": _text(ev.get("testo"), 200, "impegno"), "data": d, "materia": _key(ev.get("materia"), "materia", True)}
    if t == "obiettivo":
        m = ev.get("media")
        if m is not None and (isinstance(m, bool) or not isinstance(m, (int, float)) or not 1 <= m <= 10):
            raise NonValido("media: da 1 a 10, o null per toglierla")
        return {"tipo": t, "materia": _key(ev.get("materia"), "materia"), "media": m}
    if t == "annulla":
        return {"tipo": t, "id": _key(ev.get("id"), "id"), "motivo": _text(ev.get("motivo") or "annullato", 200, "motivo")}
    raise NonValido("tipo sconosciuto")


def leggi(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


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
    if ev["tipo"] == "annulla" and not any(e["id"] == ev["id"] and e["tipo"] in ("nota", "impegno") for e in evs):
        raise NonValido("nota o impegno inesistente")
    rec = {"id": uuid.uuid4().hex, "at": (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds"), **ev}
    if richiesta is not None:
        rec["richiesta"] = richiesta
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return rec


def stato(path):
    """Stato corrente: l'ultimo evento vince per compiti e obiettivi; note e impegni annullati esclusi."""
    evs = leggi(path)
    off = {e["id"] for e in evs if e["tipo"] == "annulla"}
    fatti, obiettivi = {}, {}
    for e in evs:
        if e["tipo"] == "compito":
            fatti[e["uid"]] = e["at"] if e["fatto"] else None
        elif e["tipo"] == "obiettivo":
            obiettivi[e["materia"]] = e["media"]
    keep = lambda t: [{k: v for k, v in e.items() if k not in ("tipo", "richiesta")} for e in evs if e["tipo"] == t and e["id"] not in off]
    return {"schema": SCHEMA, "fatti": {k: v for k, v in fatti.items() if v}, "obiettivi": {k: v for k, v in obiettivi.items() if v is not None},
            "note": keep("nota"), "impegni": sorted(keep("impegno"), key=lambda e: e["data"]), "eventi": len(evs)}
