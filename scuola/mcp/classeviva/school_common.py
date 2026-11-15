"""Primitive PURE condivise tra gli strati derivati (M5 collegamenti, M6 comunicazioni). Nessuna rete, nessun file, nessun orologio,
nessuna logica di dominio: solo mattoni identici per tutti (formato delle prove, hash corto, troncamento dei testi, ordine di freschezza,
parole di verifica e ruolo di una data nel testo). Estratte da cvv_cls_links senza cambiarne il comportamento."""
from __future__ import annotations

import hashlib
import re

FRESH_ORDER = {"aggiornato": 0, "stale": 1, "senza_lettura": 2}

# parole che indicano una verifica/interrogazione/test (testo gia' normalizzato: minuscolo, senza accenti)
TEST_RE = re.compile(r"\b(verific\w*|test|interrogazion\w*|compit\w* in classe|prova (?:scritta|orale))\b")
# la data e' PRECEDUTA da una parola di verifica (con connettivi): "verifica del 5 ottobre" -> data attribuita alla verifica
DATE_ROLE_RE = re.compile(r"(?:verific\w*|test|interrogazion\w*|compit\w* in classe|prova (?:scritta|orale))"
                          r"(?:\s+(?:scritt[aoe]|oral[ei]))?\s*(?:(?:del|di|il|per|in data|giorno|:|-)\s*)*$")


def prove(codice, segnale, esito, peso, dettaglio):
    """Una prova (evidenza) nel formato comune: codice, segnale, esito (a_favore|contro|neutro), peso, dettaglio."""
    return {"codice": codice, "segnale": segnale, "esito": esito, "peso": peso, "dettaglio": dettaglio}


def short_hash(*parts):
    """Hash sha1 a 12 caratteri, deterministico, delle parti unite da '|'."""
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def cap_text(s, limit):
    """(testo troncato a `limit` caratteri, True se e' stato troncato). Il testo originale non viene mai modificato altrove."""
    s = (s or "").strip()
    return (s[:limit], len(s) > limit)


def worst_freshness(*states):
    """Lo stato di freschezza peggiore (senza_lettura > stale > aggiornato; sconosciuto = peggiore)."""
    return max(states, key=lambda s: FRESH_ORDER.get(s, 2))


def date_attributed(text_norm, date_span):
    """True se la data (dict con 'inizio', da it_dates.parse_dates sul testo normalizzato) e' attribuita a una verifica."""
    return bool(DATE_ROLE_RE.search(text_norm[max(0, date_span["inizio"] - 45):date_span["inizio"]]))
