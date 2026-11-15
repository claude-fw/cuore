"""CUORE School: ottimizzazioni del calcolo del feed a parita' di risultato (dati SINTETICI).
- `school_dashboard.PageSession`: una copia del Registry e contratti sorgente condivisi fra le pagine; ogni pagina deve
  essere IDENTICA a `build_from_path` (provenienza compresa: solo i contratti usati da quella pagina).
- `cvv_subject.norm` con cache: funzione pura, stesso risultato con e senza cache."""
import datetime as dt, json, os, shutil, sys, tempfile, unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
ENGINE = os.path.normpath(os.path.join(APP, "..", "scuola", "mcp", "classeviva"))
sys.path[:0] = [os.path.join(APP, "bridge"), os.path.join(ENGINE, "tests"), ENGINE, os.path.join(ENGINE, "..", "_common")]
import cuore_school as CS  # noqa: E402
import cvv_subject  # noqa: E402
import registry as R  # noqa: E402
import school_context4 as C4  # noqa: E402
import school_dashboard as SD  # noqa: E402
import test_school_records as TR  # noqa: E402

NOW = dt.datetime.fromisoformat(TR.F.NOW)


class _ConRegistro(unittest.TestCase):
    """Registry sintetico condiviso dalle classi di test (nessun test qui: niente test duplicati per ereditarieta')."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.reg = os.path.join(cls.tmp, "registry.sqlite3")
        TR.make(path=cls.reg).close()
        cls.manual = os.path.join(cls.tmp, "manuale.json")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def _subjects(self):
        p = os.path.join(self.tmp, "materie.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(TR.SUBJECTS, f)
        return p


class Sessione(_ConRegistro):
    def test_pagine_identiche_a_build_from_path(self):
        canon = lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False)
        with SD.PageSession(self.reg, NOW, manual_path=self.manual, subjects=TR.SUBJECTS) as s:
            shared = {p: s.build(p) for p in CS.PAGES}
            materia = s.build("materia", materia=TR.SUBJECTS[0]["key"])
        for p in CS.PAGES:
            alone = SD.build_from_path(self.reg, NOW, manual_path=self.manual, subjects=TR.SUBJECTS, pagina=p)
            self.assertEqual(canon(shared[p]), canon(alone), p)
            self.assertEqual(shared[p]["provenance"]["contratti_usati"], alone["provenance"]["contratti_usati"], p)
        alone = SD.build_from_path(self.reg, NOW, manual_path=self.manual, subjects=TR.SUBJECTS, pagina="materia",
                                   materia=TR.SUBJECTS[0]["key"])
        self.assertEqual(canon(materia), canon(alone))

    def test_ordine_delle_pagine_indifferente(self):
        canon = lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False)
        with SD.PageSession(self.reg, NOW, manual_path=self.manual, subjects=TR.SUBJECTS) as a:
            forward = {p: canon(a.build(p)) for p in CS.PAGES}
        with SD.PageSession(self.reg, NOW, manual_path=self.manual, subjects=TR.SUBJECTS) as b:
            backward = {p: canon(b.build(p)) for p in reversed(CS.PAGES)}
        self.assertEqual(forward, backward)

    def test_fonti_condivise_solo_con_gli_stessi_parametri(self):
        with SD.PageSession(self.reg, NOW, manual_path=self.manual, subjects=TR.SUBJECTS) as s:
            with self.assertRaises(ValueError):
                SD.build(s.reg, NOW + dt.timedelta(hours=1), manual=s.manual, pagina="voti", subjects=TR.SUBJECTS,
                         source_path=s.path, _sources=s._sources)

    def test_una_sola_copia_del_registro_e_chiusa_alla_fine(self):
        real = R.Registry.from_snapshot
        opened = []

        def spy(path):
            r = real(path)
            opened.append(r)
            return r
        with mock.patch.object(R.Registry, "from_snapshot", side_effect=spy):
            f = CS.build_feed(self.reg, NOW, subjects_file=self._subjects(), manual_path=self.manual)
        self.assertEqual(len(f["pagine"]), len(CS.PAGES))
        self.assertEqual(len(opened), 1)                                      # prima: una copia per pagina
        with self.assertRaises(Exception):
            opened[0].list_records()                                          # copia chiusa dopo la costruzione


class StratiComuni(_ConRegistro):
    """Strati di M7 indipendenti dalla materia (M5, M6, L3, argomenti): una volta per feed, solo dentro la sessione."""

    def _count(self):
        real, calls = C4.M.compute, []

        def spy(*a, **kw):
            calls.append(1)
            return real(*a, **kw)
        return mock.patch.object(C4.M, "compute", side_effect=spy), calls

    def test_una_sola_classificazione_per_feed(self):
        patch, calls = self._count()
        with patch:
            f = CS.build_feed(self.reg, NOW, subjects_file=self._subjects(), manual_path=self.manual)
        self.assertEqual(len(f["pagine"]), len(CS.PAGES))
        self.assertEqual(len(calls), 1)                                       # prima: 56 per feed
        self.assertIsNone(C4._SHARED.get())                                   # ambito chiuso dopo la costruzione

    def test_nessun_riuso_fuori_dalla_sessione(self):
        patch, calls = self._count()
        with patch:
            SD.build_from_path(self.reg, NOW, manual_path=self.manual, subjects=TR.SUBJECTS, pagina="attenzione")
            first = len(calls)
            SD.build_from_path(self.reg, NOW, manual_path=self.manual, subjects=TR.SUBJECTS, pagina="attenzione")
        self.assertGreater(first, 1)                                          # strumenti MCP: come prima, nessuna cache
        self.assertEqual(len(calls), 2 * first)

    def test_parametri_diversi_ricalcolano(self):
        patch, calls = self._count()
        reg = R.Registry.from_snapshot(self.reg)
        try:
            with patch, C4.shared_layers({}) as store:
                a = C4._shared_layers(reg, TR.SUBJECTS, NOW, {}, {})
                b = C4._shared_layers(reg, TR.SUBJECTS, NOW, {}, {})
                c = C4._shared_layers(reg, TR.SUBJECTS, NOW + dt.timedelta(hours=1), {}, {})
                d = C4._shared_layers(reg, TR.SUBJECTS[:1], NOW, {}, {})
        finally:
            reg.close()
        self.assertIs(a, b)                                                   # stessa chiave: riuso
        self.assertIsNot(c, a)
        self.assertIsNot(d, a)                                                # altro istante o altre materie: ricalcolo
        self.assertEqual(len(calls), 3)
        self.assertEqual(store["layers"], a)                                  # la sessione conserva solo il primo insieme


class Normalizzazione(unittest.TestCase):
    def test_stesso_risultato_con_e_senza_cache(self):
        samples = ["  Matematica ", "LINGUA E CULTURA STRANIERA (INGLESE)", "Storia dell’arte", "Educazione  civica", "", None,
                   "Àèìòù — ïñç", "religione cattolica/attività alternativa"]
        for s in samples:
            self.assertEqual(cvv_subject.norm(s), cvv_subject.norm.__wrapped__(s), repr(s))
        before = cvv_subject.norm.cache_info().hits
        cvv_subject.norm("  Matematica ")
        self.assertGreater(cvv_subject.norm.cache_info().hits, before)


if __name__ == "__main__":
    unittest.main()
