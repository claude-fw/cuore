/**
 * Processo SEPARATO per la copia sintetica di CUORE Finance (D3a, C-10). Mai usato dal ponte reale.
 *
 *   node scripts/prova.ts crea     --dati <cartella nuova>   crea la sessione (cartella inesistente, sotto $TMPDIR)
 *   node scripts/prova.ts verifica --dati <cartella>         controlli di percorso, marcatore e sessione (sola lettura)
 *   node scripts/prova.ts migra    --dati <cartella>         migrazioni sintetiche, DOPO la verifica del marcatore
 *   node scripts/prova.ts chiudi   --dati <cartella>         sessione chiusa: scritture rifiutate (nessun file cancellato)
 *   node scripts/prova.ts esegui   --dati <cartella>         un'operazione in JSON sull'ingresso standard
 *
 * Nessun percorso predefinito, nessuna variabile d'ambiente: senza `--dati` esce con 64. Rifiuti di sicurezza: 2.
 * Errori dei servizi: 1, con `codice` = `non_valido` (ingresso rifiutato) | `non_trovato` | `errore` (D3b: il ponte di
 * prova li traduce in 422 / 404 / 500).
 * Operazioni di `esegui` ({"op": ..., "input": {...}}): consulta (runQuery, schema sintetico), aggiungi_manuale,
 * correggi_manuale, annulla_manuale, storico_manuale, imposta_budget, stato_budget, rileva_abbinamenti,
 * conferma_abbinamento, rifiuta_abbinamento, revoca_abbinamento, crea_conto (D2: es. contanti), classifica (D2).
 */
import { readFileSync } from "node:fs";
import { SYNTHETIC_QUERY_SCHEMA_VERSION } from "../src/query/operations.ts";
import { createSyntheticSession } from "../src/synthetic/generate.ts";
import {
  closeSession,
  openSyntheticForWrite,
  SyntheticGuardError,
  verifySynthetic,
} from "../src/synthetic/guard.ts";
import { type Input, OPS, out, serviceError } from "./operazioni.ts";

const EXIT_USAGE = 64;
const EXIT_REFUSED = 2;

export async function main(argv: readonly string[]): Promise<number> {
  const [command, ...rest] = argv;
  const at = rest.indexOf("--dati");
  const dir = at >= 0 ? rest[at + 1] : undefined;
  if (command === undefined || !["crea", "verifica", "migra", "chiudi", "esegui"].includes(command)) {
    process.stderr.write("uso: prova.ts <crea|verifica|migra|chiudi|esegui> --dati <cartella>\n");
    return EXIT_USAGE;
  }
  if (dir === undefined || dir.startsWith("--")) {
    process.stderr.write("--dati <cartella> obbligatorio: nessun percorso predefinito\n");
    return EXIT_USAGE;
  }
  try {
    if (command === "crea") {
      const g = await createSyntheticSession(dir);
      return out({ ok: true, sessione: g.session.id, cartella: g.realDir, conteggi: g.counts });
    }
    if (command === "verifica") {
      const v = await verifySynthetic(dir);
      return out({
        ok: true,
        sessione: v.session.id,
        stato: v.session.stato,
        versioneSchema: v.schemaVersion,
      });
    }
    if (command === "chiudi") return out({ ok: true, stato: (await closeSession(dir)).stato });
    const open = await openSyntheticForWrite(dir, { migrate: command === "migra" });
    try {
      if (command === "migra") return out({ ok: true, sessione: open.session.id });
      let request: { op?: string; input?: Input };
      try {
        request = JSON.parse(readFileSync(0, "utf8"));
      } catch {
        return out({ ok: false, errore: "richiesta_non_valida" }, EXIT_USAGE);
      }
      const op = OPS[request.op ?? ""];
      if (op === undefined) return out({ ok: false, errore: "operazione_sconosciuta" }, EXIT_USAGE);
      return out({
        ok: true,
        risultato: await op(open.ctx, request.input ?? {}, {
          expectedSchemaVersion: SYNTHETIC_QUERY_SCHEMA_VERSION,
        }),
      });
    } finally {
      open.db.close();
    }
  } catch (err) {
    if (err instanceof SyntheticGuardError)
      return out({ ok: false, errore: "rifiutato", motivo: err.message }, EXIT_REFUSED);
    return serviceError(err);
  }
}

if (import.meta.main) process.exitCode = await main(process.argv.slice(2));
