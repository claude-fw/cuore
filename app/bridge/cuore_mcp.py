"""Server MCP di CUORE (stdio, solo libreria standard): il registro degli eventi e il registro delle azioni per l'agente.

    python3 app/bridge/cuore_mcp.py [--azioni FILE]

NON registrato in alcun client: la registrazione richiede un'autorizzazione del titolare. Strumenti:
- `cuore_eventi`: registro `cuore.events/1` (stesse fonti del ponte: School e metadati Finance), validato prima di
  rispondere, filtrabile per area. Sola lettura.
- `cuore_cerca`: ricerca globale (`cuore.search/1`, cuore_search.py) nei feed School e Money e negli eventi; i movimenti
  Finance solo se chiesti esplicitamente (`movimenti: true`). Sola lettura, fonti dichiarate.
- `cuore_azioni`: stato delle azioni proposte, decise ed eseguite (`cuore.actions/1`), con l'integrita' della catena.
- `cuore_proponi_azione`: aggiunge una RICHIESTA (autore «agente»). Approvare, rifiutare o eseguire non e' possibile da
  qui: la decisione e' del titolare dal terminale (`cuore_actions.py approva`), l'esecuzione resta agli strumenti esistenti.
Senza `--azioni` gli strumenti delle azioni rispondono «non configurato» e nessun file viene creato.
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cuore_actions as A  # noqa: E402
import cuore_events as E  # noqa: E402   (imposta anche sys.path di _common)
import cuore_search as S  # noqa: E402
from cloud_mcp import Server  # noqa: E402

INSTRUCTIONS = (
    "CUORE core. cuore_cerca = ricerca in School, Money ed eventi (movimenti solo con movimenti=true). "
    "cuore_eventi = registro unico degli eventi (School e metadati Finance), vista calcolata in sola "
    "lettura: natura dichiarata per ogni evento (fatto, stato attuale, dedotto, anomalia, non verificato), mai eventi "
    "inventati; 'fonti' e 'diagnostica' dicono cosa non e' stato letto. Le azioni che modificano dati o configurazione "
    "si PROPONGONO con cuore_proponi_azione e non si eseguono finche' cuore_azioni non le mostra 'approvata' (decisione "
    "del titolare, valida 24 ore); dopo l'esecuzione l'esito si registra con cuore_actions.py esito.")
RELEVANT = lambda e: not (e["tipo"] == "run_registro" and e["gravita"] == "info")    # noqa: E731 - come la home


def build_server(events_fn, actions_path=None, school_fn=None, money_fn=None, classroom_fn=None):
    srv = Server("cuore", "0.1.0", INSTRUCTIONS)

    def valid_events():
        doc = events_fn()
        if E.validate(doc):
            raise ValueError("registro non conforme")
        return doc

    @srv.tool("cuore_cerca", "Ricerca globale in CUORE (School, materiali/compiti/annunci Classroom con link originale, Money, "
              "eventi) nei dati gia' esposti alla dashboard. Il contenuto dei file Classroom NON e' indicizzato: 'contenuto non "
              "letto' vuol dire che si conosce solo il titolo. "
              "Ogni risultato ha modulo, tipo, titolo, data e la rotta della dashboard; 'fonti' dice cosa non e' stato letto.",
              {"domanda": {"type": "string", "description": "da 2 a 80 caratteri"},
               "movimenti": {"type": "boolean", "description": "includere i movimenti Finance degli ultimi 30 giorni"},
               "limite": {"type": "integer", "minimum": 1, "maximum": 100}},
              required=["domanda"])
    def cerca(domanda, movimenti=False, limite=20):
        try:
            return S.search(domanda, school_fn=school_fn, money_fn=money_fn, events_fn=valid_events,
                            limit=min(int(limite), 100), movements=bool(movimenti), classroom_fn=classroom_fn)
        except S.QueryError as e:
            return {"schema": S.SCHEMA, "errore": "domanda_non_valida", "messaggio": str(e)}

    @srv.tool("cuore_eventi", "Registro degli eventi CUORE in sola lettura (giri del job, orari mancati, problemi aperti, "
              "notifiche, chiusure in attesa, letture del Registry, azioni). Default: solo gli eventi da guardare.",
              {"area": {"type": "string", "enum": ["school", "finance", "core"]},
               "tutti": {"type": "boolean", "description": "anche le letture riuscite del Registry"},
               "limite": {"type": "integer", "minimum": 1, "maximum": 200}})
    def eventi(area=None, tutti=False, limite=40):
        doc = events_fn()
        problems = E.validate(doc)
        if problems:
            return {"schema": E.SCHEMA, "errore": "contratto_non_valido", "problemi": len(problems)}
        evs = [e for e in doc["eventi"] if (area is None or e["area"] == area) and (tutti or RELEVANT(e))]
        limite = max(1, min(int(limite), 200))
        return {"schema": doc["schema"], "generato": doc["generato"], "finestra_giorni": doc["finestra_giorni"],
                "eventi": evs[:limite], "omessi": len(evs) - len(evs[:limite]), "conteggi": doc["conteggi"],
                "fonti": doc["fonti"], "diagnostica": doc["diagnostica"]}

    @srv.tool("cuore_azioni", "Azioni proposte, decise ed eseguite, con l'integrita' del registro. Un'azione si esegue solo "
              "se il suo stato e' 'approvata' e 'eseguibile' e' true.",
              {"stato": {"type": "string", "enum": list(A.STEPS)}})
    def azioni(stato=None):
        if not actions_path:
            return {"configurato": False, "nota": "registro delle azioni non configurato (decisione del titolare)"}
        doc = A.read(actions_path)
        acts = [dict(a, eseguibile=A.approved_and_valid(actions_path, a["id"])) for a in doc["azioni"]
                if stato is None or a["stato"] == stato]
        return {"configurato": True, "schema": doc["schema"], "integro": doc["integro"], "problemi": doc["problemi"],
                "azioni": acts}

    @srv.tool("cuore_proponi_azione", "Propone un'azione che modifica dati o configurazione; resta una richiesta finche' il "
              "titolare non decide dal terminale. Non esegue nulla.",
              {"tipo": {"type": "string", "enum": sorted(A.ACTION_TYPES)},
               "motivo": {"type": "string", "description": "una riga, al massimo 200 caratteri, senza dati personali"},
               "riferimento": {"type": "string", "description": "facoltativo: hash o nome del piano/rapporto"}},
              required=["tipo", "motivo"])
    def proponi(tipo, motivo, riferimento=None):
        if not actions_path:
            return {"configurato": False, "nota": "registro delle azioni non configurato: nessuna richiesta registrata"}
        rec = A.propose(actions_path, tipo, motivo, da="agente", riferimento=riferimento)
        return {"azione": rec["azione"], "stato": rec["passo"],
                "prossimo_passo": "decisione del titolare: python3 app/bridge/cuore_actions.py --registro <file> approva %s"
                                  % rec["azione"]}

    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description="Server MCP di CUORE (stdio).")
    ap.add_argument("--azioni", help="registro delle azioni (nessun percorso predefinito)")
    a = ap.parse_args(argv)
    import serve                                    # stesse cache in memoria e stesse letture del ponte
    school, money, classroom = serve.FeedCache(), serve.money_cache(), serve.classroom_cache()

    def events():
        try:
            m = money.get()
        except Exception:      # noqa: BLE001 - fonte Finance dichiarata illeggibile dal registro
            m = {}
        return E.build(real=True, money_feed=m if isinstance(m, dict) else {}, actions_path=a.azioni)
    build_server(events, a.azioni, school_fn=school.get, money_fn=money.get, classroom_fn=classroom.get).run()


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    main()
