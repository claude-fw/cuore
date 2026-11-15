/**
 * Registro «chi deve cosa» da un file preparato sul Mac (schema `cuore-registro/1`, dati personali: mai nel
 * repository). Ogni credito/debito si apre una volta sola: se esiste gia' (stessa controparte e descrizione) non si
 * riapre e si aggiungono solo i suoi eventi nuovi (stesso tipo, importo e giorno = gia' registrato), quindi il file si
 * puo' ricaricare. I movimenti si indicano per conto, data e importo, perche' gli id online nascono
 * all'importazione; un movimento che non si trova o che e' ambiguo ferma tutto con un errore che lo nomina.
 */
import { type Ctx, ValidationError } from "../../finance/src/context.ts";
import {
  createCounterparty,
  createObligation,
  linkTransactionToObligations,
  recordObligationEvent,
} from "../../finance/src/services/obligations.ts";

interface Movimento {
  conto: string;
  data: string;
  importo: number;
}
interface Evento {
  tipo: "increase" | "payment";
  importo: number;
  data: string;
  nota?: string;
  movimento?: Movimento;
}
interface Voce {
  controparte: string;
  direzione: "receivable" | "payable";
  valuta?: string;
  importo: number;
  aperto: string;
  descrizione: string;
  movimento?: Movimento;
  eventi?: Evento[];
}
interface Registro {
  schema: "cuore-registro/1";
  controparti: { id: string; nome: string; nota?: string }[];
  voci: Voce[];
}

async function trova(ctx: Ctx, m: Movimento): Promise<string> {
  const rows = await ctx.db.all<{ id: string }>(
    `SELECT id FROM transactions WHERE account_id = ? AND amount_minor = ? AND status = 'booked'
       AND (transaction_date = ? OR booking_date = ?)`,
    [m.conto, m.importo, m.data, m.data],
  );
  const [solo] = rows;
  if (rows.length !== 1 || solo === undefined)
    throw new ValidationError(
      `registro: movimento ${m.conto} ${m.data} ${m.importo} ${rows.length ? "ambiguo" : "non trovato"}`,
    );
  return solo.id;
}

export async function applicaRegistro(ctx: Ctx, input: unknown) {
  const r = input as Registro;
  if (r?.schema !== "cuore-registro/1" || !Array.isArray(r.voci) || !Array.isArray(r.controparti))
    throw new ValidationError("registro: schema atteso cuore-registro/1");
  for (const c of r.controparti)
    await createCounterparty(ctx, { id: c.id, name: c.nome, note: c.nota ?? null });
  const esito = { aperte: 0, saltate: 0, eventi: 0 };
  for (const v of r.voci) {
    const gia = await ctx.db.first(
      "SELECT id FROM obligations WHERE counterparty_id = ? AND direction = ? AND description = ?",
      [v.controparte, v.direzione, v.descrizione],
    );
    if (gia !== null) esito.saltate++;
    else esito.aperte++;
    const o =
      gia !== null
        ? (gia as { id: string })
        : await createObligation(ctx, {
            counterpartyId: v.controparte,
            direction: v.direzione,
            currency: v.valuta ?? "EUR",
            amountMinor: v.importo,
            openedOn: v.aperto,
            description: v.descrizione,
            transactionId: v.movimento ? await trova(ctx, v.movimento) : null,
          });
    for (const e of v.eventi ?? []) {
      // voce gia' aperta: si aggiungono solo gli eventi nuovi (es. una restituzione), cosi' il file si puo' ricaricare
      const visto = await ctx.db.first(
        "SELECT id FROM obligation_events WHERE obligation_id = ? AND kind = ? AND amount_minor = ? AND occurred_on = ?",
        [o.id, e.tipo, e.importo, e.data],
      );
      if (visto !== null) continue;
      const quota = { obligationId: o.id, kind: e.tipo, amountMinor: e.importo };
      if (e.movimento)
        await linkTransactionToObligations(ctx, await trova(ctx, e.movimento), [quota], e.nota ?? null);
      else
        await recordObligationEvent(ctx, o.id, {
          kind: e.tipo,
          amountMinor: e.importo,
          occurredOn: e.data,
          note: e.nota,
        });
      esito.eventi++;
    }
  }
  return esito;
}
