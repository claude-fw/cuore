/**
 * Crediti e debiti verso persone o enti.
 *
 * Regole (verificate dai test):
 *   - creare un credito/debito o registrarne un evento NON crea entrate o spese: la cassa e' solo nelle transazioni;
 *   - un movimento bancario si collega SOLO con un'azione esplicita dell'utente (verifica) e SOLO per intero: la somma
 *     delle quote deve essere esattamente l'importo del movimento (un movimento in parte debito e in parte spesa e'
 *     rifiutato), con segno e valuta coerenti; un movimento si collega una sola volta;
 *   - un movimento collegato esce da entrate e spese nei riepiloghi (tipo `obligation`), come i trasferimenti;
 *   - non si collegano movimenti in attesa, doppioni confermati, pending sostituite o movimenti coinvolti in
 *     trasferimenti/rimborsi proposti o confermati;
 *   - pagamenti, condoni e rettifiche non possono rendere negativo il residuo (nessun "pagamento in eccesso");
 *   - eventi identici senza movimento (stesso tipo, importo e data) sono rifiutati come possibili doppioni, salvo
 *     conferma esplicita.
 */
import type { Ctx } from "../context.ts";
import { NotFoundError, ValidationError } from "../context.ts";
import type { Statement } from "../db/types.ts";
import { stmt } from "../db/types.ts";
import { commitGuarded, readWriteSeq } from "../db/write.ts";
import { assertMinor, isSupportedCurrency } from "../domain/money.ts";
import {
  type Direction,
  type EventKind,
  expectedTransactionSign,
  type ObligationEvent,
  type ObligationState,
  obligationState,
  obligationsPosition,
  type PositionLine,
  residualAfter,
} from "../domain/obligations.ts";
import { isIsoDate } from "../domain/text.ts";
import { auditStatement } from "./audit.ts";
import { excludedTransactionIds } from "./links.ts";

const ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export interface Obligation {
  id: string;
  counterparty_id: string;
  direction: Direction;
  currency: string;
  description: string;
  opened_on: string;
  due_on: string | null;
}

interface EventRow {
  id: string;
  obligation_id: string;
  kind: EventKind;
  amount_minor: number;
  occurred_on: string;
  transaction_id: string | null;
  note: string | null;
}

interface TxnRow {
  id: string;
  amount_minor: number;
  currency: string;
  status: "pending" | "booked";
  booking_date: string | null;
}

function positiveAmount(amountMinor: number, what = "importo"): void {
  assertMinor(amountMinor);
  if (amountMinor <= 0) throw new ValidationError(`${what} deve essere positivo (unita' minime)`);
}

function isoDate(value: string, what: string): void {
  if (!isIsoDate(value)) throw new ValidationError(`${what} non valida: ${value}`);
}

export async function createCounterparty(
  ctx: Ctx,
  input: { id: string; name: string; note?: string | null },
): Promise<void> {
  if (!ID.test(input.id))
    throw new ValidationError(`id controparte non valido: ${input.id} (minuscole, cifre, - e _)`);
  if (input.name.trim() === "") throw new ValidationError("nome della controparte obbligatorio");
  const existing = await ctx.db.first<{ name: string }>("SELECT name FROM counterparties WHERE id = ?", [
    input.id,
  ]);
  if (existing !== null) {
    if (existing.name === input.name) return;
    throw new ValidationError(`controparte ${input.id} gia' presente con un altro nome`);
  }
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  await commitGuarded(ctx.db, seq, [
    stmt(
      "INSERT INTO counterparties (id, name, note, created_at) VALUES (?, ?, ?, ?)",
      input.id,
      input.name,
      input.note ?? null,
      at,
    ),
    auditStatement({
      at,
      actor: "user",
      action: "create",
      entity: "counterparty",
      entityId: input.id,
      after: { name: input.name },
    }),
  ]);
}

async function loadObligation(ctx: Ctx, id: string): Promise<{ obligation: Obligation; events: EventRow[] }> {
  const obligation = await ctx.db.first<Obligation>("SELECT * FROM obligations WHERE id = ?", [id]);
  if (obligation === null) throw new NotFoundError("credito/debito", id);
  const events = await ctx.db.all<EventRow>(
    "SELECT * FROM obligation_events WHERE obligation_id = ? ORDER BY occurred_on, created_at, id",
    [id],
  );
  return { obligation, events };
}

const toDomain = (events: readonly EventRow[]): ObligationEvent[] =>
  events.map((e) => ({ kind: e.kind, amountMinor: e.amount_minor, occurredOn: e.occurred_on }));

/** Insieme dei movimenti gia' collegati a crediti/debiti. */
export async function obligationTransactionIds(ctx: Ctx): Promise<Set<string>> {
  const rows = await ctx.db.all<{ id: string }>(
    "SELECT DISTINCT transaction_id AS id FROM obligation_events WHERE transaction_id IS NOT NULL",
  );
  return new Set(rows.map((r) => r.id));
}

/** Controlli comuni prima di collegare un movimento bancario. */
async function linkableTransaction(ctx: Ctx, transactionId: string): Promise<TxnRow> {
  const t = await ctx.db.first<TxnRow>(
    "SELECT id, amount_minor, currency, status, booking_date FROM transactions WHERE id = ?",
    [transactionId],
  );
  if (t === null) throw new NotFoundError("transazione", transactionId);
  if (t.status !== "booked" || t.booking_date === null) {
    throw new ValidationError("si collegano solo movimenti contabilizzati");
  }
  if (t.amount_minor === 0) throw new ValidationError("movimento con importo zero");
  if ((await obligationTransactionIds(ctx)).has(transactionId)) {
    throw new ValidationError("movimento gia' collegato a crediti/debiti");
  }
  if ((await excludedTransactionIds(ctx)).has(transactionId)) {
    throw new ValidationError("movimento escluso (doppione confermato o pending sostituita)");
  }
  const busy = await ctx.db.first<{ type: string; status: string }>(
    `SELECT type, status FROM transaction_links
     WHERE type IN ('transfer', 'refund') AND status IN ('proposed', 'confirmed')
       AND (from_txn_id = ? OR to_txn_id = ?) LIMIT 1`,
    [transactionId, transactionId],
  );
  if (busy !== null) {
    throw new ValidationError(
      `movimento coinvolto in un ${busy.type} ${busy.status === "confirmed" ? "confermato" : "proposto (deciderlo prima)"}`,
    );
  }
  return t;
}

export interface NewObligation {
  counterpartyId: string;
  direction: Direction;
  currency: string;
  amountMinor: number;
  openedOn: string;
  dueOn?: string | null;
  description: string;
  /** movimento bancario con cui il credito/debito e' nato (prestito dato o ricevuto), solo se verificato */
  transactionId?: string | null;
  /** consente di registrare un'apertura identica a una esistente (stessa controparte, importo, data, descrizione) */
  confirmDuplicate?: boolean;
}

export async function createObligation(ctx: Ctx, input: NewObligation): Promise<Obligation> {
  if (input.direction !== "receivable" && input.direction !== "payable") {
    throw new ValidationError(`direzione non valida: ${String(input.direction)}`);
  }
  if (!isSupportedCurrency(input.currency))
    throw new ValidationError(`valuta non supportata: ${input.currency}`);
  positiveAmount(input.amountMinor);
  isoDate(input.openedOn, "data di apertura");
  if (input.dueOn != null) {
    isoDate(input.dueOn, "scadenza");
    if (input.dueOn < input.openedOn) throw new ValidationError("scadenza precedente all'apertura");
  }
  if (input.description.trim() === "") throw new ValidationError("descrizione obbligatoria");
  if ((await ctx.db.first("SELECT id FROM counterparties WHERE id = ?", [input.counterpartyId])) === null) {
    throw new NotFoundError("controparte", input.counterpartyId);
  }
  if (!input.confirmDuplicate) {
    const twin = await ctx.db.first<{ id: string }>(
      `SELECT o.id FROM obligations o JOIN obligation_events e ON e.obligation_id = o.id AND e.kind = 'open'
       WHERE o.counterparty_id = ? AND o.direction = ? AND o.currency = ? AND o.opened_on = ? AND o.description = ?
         AND e.amount_minor = ?`,
      [
        input.counterpartyId,
        input.direction,
        input.currency,
        input.openedOn,
        input.description,
        input.amountMinor,
      ],
    );
    if (twin !== null)
      throw new ValidationError(`possibile doppione di ${twin.id}: confermare esplicitamente se voluto`);
  }
  let occurredOn = input.openedOn;
  if (input.transactionId != null) {
    const t = await linkableTransaction(ctx, input.transactionId);
    if (t.currency !== input.currency)
      throw new ValidationError("valuta del movimento diversa da quella del credito/debito");
    if (Math.abs(t.amount_minor) !== input.amountMinor) {
      throw new ValidationError(
        "l'importo deve coincidere con l'intero movimento (nessun collegamento parziale)",
      );
    }
    if (Math.sign(t.amount_minor) !== expectedTransactionSign(input.direction, "open")) {
      throw new ValidationError(
        input.direction === "receivable"
          ? "un credito nasce da un'uscita (denaro dato)"
          : "un debito nasce da un'entrata (denaro ricevuto)",
      );
    }
    occurredOn = t.booking_date ?? input.openedOn;
  }
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const obligation: Obligation = {
    id: ctx.newId(),
    counterparty_id: input.counterpartyId,
    direction: input.direction,
    currency: input.currency,
    description: input.description,
    opened_on: input.openedOn,
    due_on: input.dueOn ?? null,
  };
  await commitGuarded(ctx.db, seq, [
    stmt(
      `INSERT INTO obligations (id, counterparty_id, direction, currency, description, opened_on, due_on, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      obligation.id,
      obligation.counterparty_id,
      obligation.direction,
      obligation.currency,
      obligation.description,
      obligation.opened_on,
      obligation.due_on,
      at,
    ),
    stmt(
      `INSERT INTO obligation_events (id, obligation_id, kind, amount_minor, occurred_on, transaction_id, note, created_at)
       VALUES (?, ?, 'open', ?, ?, ?, NULL, ?)`,
      ctx.newId(),
      obligation.id,
      input.amountMinor,
      occurredOn,
      input.transactionId ?? null,
      at,
    ),
    auditStatement({
      at,
      actor: "user",
      action: "create",
      entity: "obligation",
      entityId: obligation.id,
      after: { ...obligation, amount_minor: input.amountMinor, transaction_id: input.transactionId ?? null },
    }),
  ]);
  return obligation;
}

export interface NewEvent {
  kind: "increase" | "payment" | "forgive" | "adjust";
  /** positivo; per `adjust` con segno (aumenta o riduce il residuo) */
  amountMinor: number;
  occurredOn: string;
  note?: string | null;
  confirmDuplicate?: boolean;
}

/** Evento SENZA movimento bancario (es. contanti, condono, rettifica). Per i movimenti: linkTransactionToObligations. */
export async function recordObligationEvent(ctx: Ctx, obligationId: string, ev: NewEvent): Promise<string> {
  if (!["increase", "payment", "forgive", "adjust"].includes(ev.kind)) {
    throw new ValidationError(`tipo di evento non ammesso: ${String(ev.kind)}`);
  }
  assertMinor(ev.amountMinor);
  if (ev.kind === "adjust") {
    if (ev.amountMinor === 0) throw new ValidationError("rettifica nulla");
    if ((ev.note ?? "").trim() === "") throw new ValidationError("la rettifica richiede una motivazione");
  } else positiveAmount(ev.amountMinor);
  isoDate(ev.occurredOn, "data dell'evento");
  const { obligation, events } = await loadObligation(ctx, obligationId);
  if (ev.occurredOn < obligation.opened_on) throw new ValidationError("evento precedente all'apertura");
  const state = obligationState(toDomain(events), obligation.due_on, ev.occurredOn);
  if (state.lastEventOn !== null && ev.occurredOn < state.lastEventOn) {
    // in ordine cronologico: ogni posizione "alla data X" resta coerente (mai residui negativi nel passato)
    throw new ValidationError(
      `data precedente all'ultimo evento (${state.lastEventOn}): usare una rettifica datata oggi`,
    );
  }
  if (residualAfter(state.residual, ev.kind, ev.amountMinor) === null) {
    throw new ValidationError(`l'evento supera il residuo (${state.residual}): nessun pagamento in eccesso`);
  }
  if (!ev.confirmDuplicate) {
    const twin = events.find(
      (e) =>
        e.kind === ev.kind &&
        e.amount_minor === ev.amountMinor &&
        e.occurred_on === ev.occurredOn &&
        e.transaction_id === null,
    );
    if (twin !== undefined)
      throw new ValidationError(`possibile doppione dell'evento ${twin.id}: confermare se voluto`);
  }
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const id = ctx.newId();
  await commitGuarded(ctx.db, seq, [
    stmt(
      `INSERT INTO obligation_events (id, obligation_id, kind, amount_minor, occurred_on, transaction_id, note, created_at)
       VALUES (?, ?, ?, ?, ?, NULL, ?, ?)`,
      id,
      obligationId,
      ev.kind,
      ev.amountMinor,
      ev.occurredOn,
      ev.note ?? null,
      at,
    ),
    auditStatement({
      at,
      actor: "user",
      action: `event_${ev.kind}`,
      entity: "obligation",
      entityId: obligationId,
      before: { residual: state.residual },
      after: { kind: ev.kind, amount_minor: ev.amountMinor, occurred_on: ev.occurredOn },
      reason: ev.note ?? null,
    }),
  ]);
  return id;
}

export interface Allocation {
  obligationId: string;
  /** `payment` = restituzione (riduce il residuo), `increase` = nuovo prestito sullo stesso credito/debito */
  kind: "payment" | "increase";
  amountMinor: number;
}

/**
 * Collega un movimento bancario VERIFICATO dall'utente a uno o piu' crediti/debiti, per intero e in un'unica
 * operazione atomica. Da qui il movimento conta come `obligation` nei riepiloghi.
 */
export async function linkTransactionToObligations(
  ctx: Ctx,
  transactionId: string,
  allocations: readonly Allocation[],
  note: string | null = null,
): Promise<string[]> {
  if (allocations.length === 0) throw new ValidationError("indicare almeno una quota");
  const t = await linkableTransaction(ctx, transactionId);
  const seen = new Set<string>();
  let total = 0;
  const loaded: { a: Allocation; obligation: Obligation; residual: number }[] = [];
  for (const a of allocations) {
    if (a.kind !== "payment" && a.kind !== "increase")
      throw new ValidationError(`tipo di quota non ammesso: ${String(a.kind)}`);
    positiveAmount(a.amountMinor, "quota");
    if (seen.has(a.obligationId)) throw new ValidationError("lo stesso credito/debito compare piu' volte");
    seen.add(a.obligationId);
    const { obligation, events } = await loadObligation(ctx, a.obligationId);
    if (obligation.currency !== t.currency) throw new ValidationError(`valuta diversa per ${a.obligationId}`);
    if (Math.sign(t.amount_minor) !== expectedTransactionSign(obligation.direction, a.kind)) {
      throw new ValidationError(
        `segno del movimento incoerente con ${a.kind} di un ${obligation.direction === "receivable" ? "credito" : "debito"}`,
      );
    }
    const bookingDate = t.booking_date ?? "";
    if (bookingDate < obligation.opened_on)
      throw new ValidationError(`movimento precedente all'apertura di ${a.obligationId}`);
    const state = obligationState(toDomain(events), obligation.due_on, bookingDate);
    if (state.lastEventOn !== null && bookingDate < state.lastEventOn) {
      throw new ValidationError(
        `movimento precedente all'ultimo evento di ${a.obligationId} (${state.lastEventOn})`,
      );
    }
    if (residualAfter(state.residual, a.kind, a.amountMinor) === null) {
      throw new ValidationError(`la quota supera il residuo di ${a.obligationId} (${state.residual})`);
    }
    total += a.amountMinor;
    loaded.push({ a, obligation, residual: state.residual });
  }
  if (total !== Math.abs(t.amount_minor)) {
    throw new ValidationError(
      `le quote (${total}) devono coprire esattamente il movimento (${Math.abs(t.amount_minor)}): nessun collegamento parziale`,
    );
  }
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const ids: string[] = [];
  const statements: Statement[] = [];
  for (const { a, residual } of loaded) {
    const id = ctx.newId();
    ids.push(id);
    statements.push(
      stmt(
        `INSERT INTO obligation_events (id, obligation_id, kind, amount_minor, occurred_on, transaction_id, note, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
        id,
        a.obligationId,
        a.kind,
        a.amountMinor,
        t.booking_date,
        transactionId,
        note,
        at,
      ),
      auditStatement({
        at,
        actor: "user",
        action: `link_${a.kind}`,
        entity: "obligation",
        entityId: a.obligationId,
        before: { residual },
        after: { transaction_id: transactionId, amount_minor: a.amountMinor },
        reason: note,
      }),
    );
  }
  await commitGuarded(ctx.db, seq, statements);
  return ids;
}

export interface ObligationView extends Obligation {
  counterparty_name: string;
  state: ObligationState;
  events: EventRow[];
}

export async function listObligations(ctx: Ctx, asOf: string): Promise<ObligationView[]> {
  isoDate(asOf, "data di riferimento");
  const rows = await ctx.db.all<Obligation & { counterparty_name: string }>(
    `SELECT o.*, c.name AS counterparty_name FROM obligations o JOIN counterparties c ON c.id = o.counterparty_id
     ORDER BY o.opened_on, o.id`,
  );
  const out: ObligationView[] = [];
  for (const o of rows) {
    // solo gli eventi fino alla data di riferimento: la posizione "al giorno X" non vede il futuro
    const events = (await loadObligation(ctx, o.id)).events.filter((e) => e.occurred_on <= asOf);
    if (!events.some((e) => e.kind === "open")) continue;
    out.push({ ...o, events, state: obligationState(toDomain(events), o.due_on, asOf) });
  }
  return out;
}

export async function obligationsPositionAt(ctx: Ctx, asOf: string): Promise<PositionLine[]> {
  return obligationsPosition(
    (await listObligations(ctx, asOf)).map((o) => ({
      direction: o.direction,
      currency: o.currency,
      state: o.state,
    })),
  );
}
