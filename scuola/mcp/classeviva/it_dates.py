"""Date italiane ESPLICITE nel testo libero (M5). Modulo PURO: nessuna rete, nessun file, nessun orologio.

Riconosce SOLO forme esplicite; non interpreta mai date relative ("domani", "per lunedi", "tra una settimana"):
  * numeriche   `g/m`, `g/m/aa`, `g/m/aaaa`, con giorno della settimana facoltativo davanti ("giovedi 1/10");
  * con mese in lettere `g <mese>` e `g <mese> aaaa` ("5 ottobre", "5 ottobre 2026"), giorno della settimana facoltativo.
Se l'anno manca si risolve con l'ANNO SCOLASTICO di riferimento (1 settembre -> 31 agosto): settembre-dicembre = anno di
inizio, gennaio-agosto = anno successivo. E' una DEDUZIONE e viene dichiarata (`anno_esplicito=False`).
Una data impossibile (31/9, 25/26) o con giorno della settimana incoerente NON e' utilizzabile: viene restituita con lo
`stato` che lo dice e `data=None` / `data` presente ma stato 'weekday_incoerente', mai silenziosamente corretta.
Limiti noti: una frazione scritta come "3/4" e' una data valida (3 aprile) e verra' riconosciuta; il chiamante usa le date
solo come EVIDENZA da confrontare con una data gia' nota, mai come fatto.
"""
from __future__ import annotations

import datetime as dt
import re

import cvv_subject as S

MONTHS = {"gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
          "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12}
WEEKDAYS = {"lunedi": 0, "martedi": 1, "mercoledi": 2, "giovedi": 3, "venerdi": 4, "sabato": 5, "domenica": 6}
SCHOOL_YEAR_START_MONTH = 9

_WD = r"(?:(lunedi|martedi|mercoledi|giovedi|venerdi|sabato|domenica)\s+)?"
_NUM = re.compile(_WD + r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{4}|\d{2}))?(?!\d)")
_TXT = re.compile(_WD + r"(?<!\d)(\d{1,2})\s*[°º]?\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?(?!\d)")


def _as_date(d):
    return d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d)[:10])


def school_year_start(d):
    """Anno (int) in cui inizia l'anno scolastico che contiene la data `d` (date o 'YYYY-MM-DD...')."""
    d = _as_date(d)
    return d.year if d.month >= SCHOOL_YEAR_START_MONTH else d.year - 1


def school_year_label(d):
    y = school_year_start(d)
    return "%d-%02d" % (y, (y + 1) % 100)


def _resolve(day, month, year_txt, anchor):
    """-> (anno, anno_esplicito)."""
    if year_txt:
        y = int(year_txt)
        return (2000 + y if len(year_txt) == 2 else y), True
    return (anchor if month >= SCHOOL_YEAR_START_MONTH else anchor + 1), False


def _make(m, day, month, year_txt, wd, anchor):
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return {"testo": m.group(0), "inizio": m.start(), "fine": m.end(), "giorno": day, "mese": month, "anno": None,
                "anno_esplicito": bool(year_txt), "weekday": wd, "data": None, "stato": "impossibile"}
    year, explicit = _resolve(day, month, year_txt, anchor)
    out = {"testo": m.group(0), "inizio": m.start(), "fine": m.end(), "giorno": day, "mese": month, "anno": year,
           "anno_esplicito": explicit, "weekday": wd, "data": None, "stato": "valida"}
    try:
        d = dt.date(year, month, day)
    except ValueError:
        out["stato"] = "impossibile"
        return out
    out["data"] = d.isoformat()
    if wd is not None and WEEKDAYS[wd] != d.weekday():
        out["stato"] = "weekday_incoerente"
    return out


def parse_dates(text, anno_scolastico_inizio):
    """Date esplicite di `text`, in ordine di posizione. `anno_scolastico_inizio` (int) risolve le date senza anno.
    Il testo viene normalizzato (minuscolo, senza accenti, spazi singoli): `inizio`/`fine` riferiscono a QUEL testo."""
    t = S.norm(text)
    found = []
    for m in _NUM.finditer(t):
        wd, d, mo, y = m.groups()
        found.append(_make(m, int(d), int(mo), y, wd, anno_scolastico_inizio))
    for m in _TXT.finditer(t):
        wd, d, mo, y = m.groups()
        found.append(_make(m, int(d), MONTHS[mo], y, wd, anno_scolastico_inizio))
    found.sort(key=lambda x: (x["inizio"], x["fine"]))
    return found


def normalized_text(text):
    return S.norm(text)
