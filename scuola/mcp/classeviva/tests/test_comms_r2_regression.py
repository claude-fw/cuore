"""M6 r2 (M7): regressione contro il golden r1 (tests/comms_golden_r1.json, generato PRIMA della modifica) e caso ASSENZA_DOCENTE.
Ogni comunicazione NON intercettata dalla nuova regola deve essere identica a r1 (salvo comm_id/comm_version, che dipendono dalla
versione); quelle intercettate hanno esplicitamente il nuovo risultato atteso. Negativi: sostituzione/sostituire, 'Portate i compiti'
senza assenza, 'Nessuna verifica', 'Verifica di storia', 'Ariosto', 'professore'."""
import json, os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import comms_fixtures as F
import cvv_comms as M
from links_fixtures import ann

GOLDEN = os.path.join(HERE, "comms_golden_r1.json")
INTERCEPTED = {"cls:C-SF/a-sost"}                      # unica comunicazione delle fixture M6 con un'assenza docente esplicita


def dump(o):
    return json.dumps(o, ensure_ascii=False, sort_keys=True)


def strip(x):
    x = json.loads(json.dumps(x))
    x.pop("comm_id", None)
    x["interpretazione"].pop("comm_version", None)
    x["fonte_record"].pop("first_seen", None)            # istante di creazione del registro di test (orologio): non e' M6
    return x


def classify_texts(texts, course="C-SC"):
    """Classifica testi arbitrari come annunci di un corso (default C-SC, monomateria confermato)."""
    anns = [ann(course, "t%d" % i, t, "2026-10-29") for i, t in enumerate(texts)]
    reg = F.make_registry(announcements=anns, notices=[])
    try:
        doc = M.build(reg, F.SUBJECTS, F.NOW, manual=F.MANUAL, perimetro="tutti", dettaglio=True, limits={"comunicazioni": 0})
    finally:
        reg.close()
    by = {x["uid"]: x for x in doc["comunicazioni"]}
    return [by["cls:%s/t%d" % (course, i)] for i in range(len(texts))]


class Golden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(GOLDEN, encoding="utf-8") as f:
            cls.g = json.load(f)
        reg = F.make_registry()
        cls.doc = M.build(reg, F.SUBJECTS, F.NOW, manual=F.MANUAL, perimetro="tutti", dettaglio=True, limits={"comunicazioni": 0})
        reg.close()
        cls.new = {x["uid"]: x for x in cls.doc["comunicazioni"]}

    def test_golden_is_r1_and_versions_moved(self):
        self.assertTrue(self.g["comm_version_r1"].startswith("r1-"))
        self.assertEqual(M.COMM_RULES_REV, 2)
        self.assertTrue(M.comm_version().startswith("r2-"))
        self.assertEqual(self.doc["comm_version"], M.comm_version())

    def test_same_set_of_communications(self):
        self.assertEqual(set(self.g["comunicazioni"]), set(self.new))

    def test_not_intercepted_identical_to_r1(self):
        diff = [u for u in sorted(self.new) if u not in INTERCEPTED and dump(strip(self.new[u])) != dump(strip(self.g["comunicazioni"][u]))]
        self.assertEqual(diff, [])

    def test_only_expected_intercepted(self):
        changed = {u for u in self.new if dump(strip(self.new[u])) != dump(strip(self.g["comunicazioni"][u]))}
        self.assertEqual(changed, INTERCEPTED)
        self.assertEqual({u for u, x in self.new.items() if x["interpretazione"].get("caso_speciale")}, INTERCEPTED)

    def test_intercepted_explicit_new_result(self):
        old, x = self.g["comunicazioni"]["cls:C-SF/a-sost"], self.new["cls:C-SF/a-sost"]
        i = x["interpretazione"]
        self.assertEqual((x["stato"], i["regola"]), ("probabile", "AD1"))
        self.assertEqual(i["classe_principale"], {"valore": "organizzativa", "stato": "probabile", "pari_livello": None, "secondarie": []})
        self.assertEqual(i["caso_speciale"]["codice"], "ASSENZA_DOCENTE")
        self.assertEqual(i["caso_speciale"]["funzioni_secondarie"], [])
        org = next(f for f in i["funzioni"] if f["funzione"] == "organizzativa")
        self.assertEqual((org["stato"], org["regola"]), ("probabile", "AD1"))
        self.assertEqual(org["prove"][0]["codice"], "S10")
        # la prova r1 (parole del lessico) resta, dopo quella nuova; dati e materia invariati
        self.assertEqual(org["prove"][1:], next(f for f in old["interpretazione"]["funzioni"] if f["funzione"] == "organizzativa")["prove"])
        self.assertEqual((x["dati"], i["materia"], i["ambito"]), (old["dati"], old["interpretazione"]["materia"], old["interpretazione"]["ambito"]))


class AssenzaDocente(unittest.TestCase):
    def test_verdi_shape_case(self):
        x = classify_texts(["Salve ragazzi. In prima ora siamo insieme. Non c’è la professoressa Verdi, vi seguo io. "
                            "Portate quaderno e compiti. Ciao"])[0]
        i = x["interpretazione"]
        self.assertEqual(i["caso_speciale"]["codice"], "ASSENZA_DOCENTE")
        self.assertEqual(i["classe_principale"]["valore"], "organizzativa")
        self.assertEqual(i["classe_principale"]["secondarie"], ["compito"])
        self.assertEqual((x["stato"], i["regola"]), ("probabile", "AD1"))
        self.assertIn("compito", [f["funzione"] for f in i["funzioni"]])

    def test_positive_variants(self):
        texts = ["Oggi non c'e il prof. Rossi", "La prof.ssa Bianchi e' assente", "La professoressa Verdi sara assente domani",
                 "Assenza della docente: entrata posticipata"]
        for t, x in zip(texts, classify_texts(texts)):
            self.assertEqual(x["interpretazione"].get("caso_speciale", {}).get("codice"), "ASSENZA_DOCENTE", t)
            self.assertEqual(x["interpretazione"]["classe_principale"]["valore"], "organizzativa", t)

    def test_secondary_compito_only_if_supported(self):
        a, b = classify_texts(["Non c'e la professoressa Galli: portate il libro", "Non c'e la professoressa Galli"])
        self.assertEqual(a["interpretazione"]["classe_principale"]["secondarie"], ["compito"])     # 'portate' e' parola di compito
        self.assertEqual(b["interpretazione"]["classe_principale"]["secondarie"], [])
        self.assertNotIn("compito", [f["funzione"] for f in b["interpretazione"]["funzioni"]])

    def test_negatives_not_intercepted(self):
        texts = ["reazione di sostituzione nucleofila", "sostituire il valore di x nell'equazione", "Portate i compiti e il quaderno",
                 "Nessuna verifica domani", "Verifica di storia sul Rinascimento", "Ariosto", "professore",
                 "Chi era assente recupererà la verifica", "domani avrete supplenza alla quarta ora",
                 "sostituiro la prof.ssa Neri", "non c'e la verifica", "il professore ha spiegato le derivate",
                 "Sostituzione in classe"]
        for t, x in zip(texts, classify_texts(texts)):
            self.assertNotIn("caso_speciale", x["interpretazione"], t)
            self.assertNotEqual(x["interpretazione"]["regola"], "AD1", t)
            self.assertNotIn("secondarie", x["interpretazione"]["classe_principale"], t)

    def test_conflict_stays_conflict(self):
        x = classify_texts(["Non c'e la prof. Rossi: verifica del 2 novembre 2026 e verifica del 4 novembre 2026"])[0]
        self.assertEqual(x["stato"], "conflitto")
        self.assertEqual(x["interpretazione"]["caso_speciale"]["codice"], "ASSENZA_DOCENTE")
        self.assertEqual(x["interpretazione"]["regola"], "RC2")

    def test_shared_course_materia_still_undetermined(self):
        x = classify_texts(["Non c'e la professoressa Bianchi, portate i compiti"], course="C-LE")[0]
        self.assertEqual(x["interpretazione"]["materia"]["stato"], "da_verificare")
        self.assertEqual(x["interpretazione"]["ambito"]["valore"], "corso")

    def test_rule_is_versioned(self):
        self.assertIn("AD1", M.RULES)
        self.assertIn("assenza_docente", M.LEX)
        self.assertIn("AD1", M.CRITERI["regole"])


if __name__ == "__main__":
    unittest.main()
