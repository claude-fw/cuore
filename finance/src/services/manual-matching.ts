/**
 * RICONCILIAZIONE MANUALE ↔ BANCA (A4; richiede la migrazione v7, in attesa).
 *
 * Quando arriva dalla banca il movimento di una spesa gia' inserita a mano, i due si abbinano con il contratto esistente
 * dei collegamenti (services/links.ts): `possible_duplicate` da BANCA (`from`, la fonte originale, resta) a MANUALE
 * (`to`): una volta confermato, il manuale esce da riepiloghi e riconciliazioni (`EXCLUDED_IDS_SQL`). Niente viene
 * cancellato o fuso; lo storico sono i collegamenti, la coda di revisione e `audit_log`.
 *
 * Regole (deterministiche):
 *   - candidati: stesso conto, stessa valuta, stesso importo, movimento IMPORTATO, data operazione a +-3 giorni;
 *   - esclusi: manuali annullati o gia' riconciliati; movimenti bancari esclusi (doppioni, pending sostituite) o gia'
 *     usati per un altro manuale; coppie gia' collegate in qualsiasi stato (un rifiuto non viene riproposto);
 *   - **mai una conferma automatica**: il rilevatore propone; «univoco» se manuale e movimento hanno un solo candidato
 *     ciascuno, altrimenti «ambiguo» (tutte le coppie proposte, un elemento di revisione per il manuale);
 *   - la conferma (`confirmManualMatch`) controlla che manuale e movimento siano ancora liberi e rifiuta nello stesso
 *     batch le altre proposte che li coinvolgono; un abbinamento sbagliato si revoca con `reverseManualMatch` (riga in
 *     `link_reversals`, con motivo): il manuale torna aperto.
 */
import type { Ctx } from "../context.ts";
import { NotFoundError, ValidationError } from "../context.ts";
import type { Statement } from "../db/types.ts";
import { stmt } from "../db/types.ts";
import { commitGuarded, readWriteSeq } from "../db/write.ts";
import { addDays } from "../domain/text.ts";
import { auditStatement, reviewStatement } from "./audit.ts";
import { closeReviewStatement } from "./classification.ts";
import { EXCLUDED_IDS_SQL } from "./effective.ts";
import { DUPLICATE_WINDOW_DAYS } from "./manual.ts";

export const MATCH_WINDOW_DAYS = DUPLICATE_WINDOW_DAYS;
const REVIEW_KIND = "possible_duplicate";

interface Txn {
  id: string;
  account_id: string;
  currency: string;
  amount_minor: number;
  transaction_date: string;
}

interface MatchLink {
  id: string;
  from_txn_id: string;
  to_txn_id: string;
  status: "proposed" | "confirmed" | "rejected";
  reversed: number;
}

export interface DetectManualCounts {
  manualOpen: number;
  proposed: number;
  unique: number;
  ambiguous: number;
}

export type ManualMatchState =
  | { state: "annullato" }
  | { state: "riconciliato"; linkId: string; bankId: string }
  | { state: "proposto"; linkId: string; bankId: string }
  | { state: "ambiguo"; candidates: { linkId: string; bankId: string }[] }
  | { state: "aperto" };

const daysApart = (a: string, b: string) => Math.abs((Date.parse(a) - Date.parse(b)) / 86_400_000);

/** Collegamenti manuale ↔ banca (from importato, to manuale), con la revoca. */
async function manualLinks(ctx: Ctx): Promise<MatchLink[]> {
  return ctx.db.all<MatchLink>(
    `SELECT l.id, l.from_txn_id, l.to_txn_id, l.status,
            EXISTS (SELECT 1 FROM link_reversals r WHERE r.link_id = l.id) AS reversed
     FROM transaction_links l
     JOIN transactions f ON f.id = l.from_txn_id AND f.origin = 'import'
     JOIN transactions t ON t.id = l.to_txn_id AND t.origin = 'manual'
     WHERE l.type = 'possible_duplicate'
     ORDER BY l.created_at, l.id`,
  );
}

const active = (l: MatchLink) => l.status === "confirmed" && l.reversed === 0;

export async function detectManualMatches(ctx: Ctx): Promise<DetectManualCounts> {
  const links = await manualLinks(ctx);
  const reconciledManual = new Set(links.filter(active).map((l) => l.to_txn_id));
  const usedBank = new Set(links.filter(active).map((l) => l.from_txn_id));
  const linkedPairs = new Set(links.map((l) => `${l.from_txn_id}|${l.to_txn_id}`));
  const manuals = (
    await ctx.db.all<Txn>(
      `SELECT id, account_id, currency, amount_minor, transaction_date FROM transactions
       WHERE origin = 'manual' AND id NOT IN (SELECT transaction_id FROM manual_voids)
       ORDER BY transaction_date, id`,
    )
  ).filter((m) => !reconciledManual.has(m.id));
  const banks = (
    await ctx.db.all<Txn>(
      `SELECT id, account_id, currency, amount_minor, transaction_date FROM transactions
       WHERE origin = 'import' AND id NOT IN (${EXCLUDED_IDS_SQL})
       ORDER BY transaction_date, id`,
    )
  ).filter((b) => !usedBank.has(b.id));

  const candidatesOf = new Map<string, Txn[]>();
  const manualsOfBank = new Map<string, string[]>();
  for (const m of manuals) {
    const c = banks
      .filter(
        (b) =>
          b.account_id === m.account_id &&
          b.currency === m.currency &&
          b.amount_minor === m.amount_minor &&
          daysApart(b.transaction_date, m.transaction_date) <= MATCH_WINDOW_DAYS,
      )
      .sort(
        (x, y) =>
          daysApart(x.transaction_date, m.transaction_date) -
            daysApart(y.transaction_date, m.transaction_date) || x.id.localeCompare(y.id),
      );
    candidatesOf.set(m.id, c);
    for (const b of c) manualsOfBank.set(b.id, [...(manualsOfBank.get(b.id) ?? []), m.id]);
  }

  const counts: DetectManualCounts = { manualOpen: manuals.length, proposed: 0, unique: 0, ambiguous: 0 };
  const statements: Statement[] = [];
  const at = ctx.now();
  for (const m of manuals) {
    const c = candidatesOf.get(m.id) ?? [];
    if (c.length === 0) continue;
    const ambiguous = c.length > 1 || c.some((b) => (manualsOfBank.get(b.id) ?? []).length > 1);
    const fresh = c.filter((b) => !linkedPairs.has(`${b.id}|${m.id}`));
    if (fresh.length === 0) continue;
    if (ambiguous) counts.ambiguous += 1;
    else counts.unique += 1;
    for (const b of fresh) {
      counts.proposed += 1;
      statements.push(
        stmt(
          `INSERT INTO transaction_links (id, type, from_txn_id, to_txn_id, status, origin, note, created_at, decided_at)
           VALUES (?, 'possible_duplicate', ?, ?, 'proposed', 'detector', ?, ?, NULL)`,
          ctx.newId(),
          b.id,
          m.id,
          JSON.stringify({
            criterio: "manuale-banca: stesso conto, valuta e importo",
            giorni: daysApart(b.transaction_date, m.transaction_date),
            esito: ambiguous ? "ambiguo" : "univoco",
          }),
          at,
        ),
      );
    }
    statements.push(
      reviewStatement({
        id: ctx.newId(),
        kind: REVIEW_KIND,
        entity: "transaction",
        entityId: m.id,
        detail: { manuale: m.id, candidati: c.map((b) => b.id), esito: ambiguous ? "ambiguo" : "univoco" },
        createdAt: at,
      }),
    );
  }
  if (statements.length > 0) await commitGuarded(ctx.db, await readWriteSeq(ctx.db), statements);
  return counts;
}

async function loadMatchLink(ctx: Ctx, linkId: string): Promise<MatchLink> {
  const link = (await manualLinks(ctx)).find((l) => l.id === linkId);
  if (link === undefined) {
    const any = await ctx.db.first("SELECT id FROM transaction_links WHERE id = ?", [linkId]);
    if (any === null) throw new NotFoundError("collegamento", linkId);
    throw new ValidationError("non e' un abbinamento manuale ↔ banca");
  }
  return link;
}

export async function confirmManualMatch(
  ctx: Ctx,
  linkId: string,
): Promise<{ linkId: string; rejected: string[] }> {
  const link = await loadMatchLink(ctx, linkId);
  if (link.status !== "proposed") throw new ValidationError(`abbinamento gia' deciso (${link.status})`);
  const voided = await ctx.db.first("SELECT 1 AS x FROM manual_voids WHERE transaction_id = ?", [
    link.to_txn_id,
  ]);
  if (voided !== null) throw new ValidationError("movimento manuale annullato");
  const links = await manualLinks(ctx);
  if (links.some((l) => active(l) && l.to_txn_id === link.to_txn_id)) {
    throw new ValidationError("movimento manuale gia' riconciliato");
  }
  if (links.some((l) => active(l) && l.from_txn_id === link.from_txn_id)) {
    throw new ValidationError("movimento bancario gia' abbinato a un altro manuale");
  }
  const siblings = links.filter(
    (l) =>
      l.id !== link.id &&
      l.status === "proposed" &&
      (l.to_txn_id === link.to_txn_id || l.from_txn_id === link.from_txn_id),
  );
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const statements: Statement[] = [
    stmt("UPDATE transaction_links SET status = 'confirmed', decided_at = ? WHERE id = ?", at, link.id),
    closeReviewStatement(REVIEW_KIND, "transaction", link.to_txn_id, at, `abbinato alla banca (${link.id})`),
    auditStatement({
      at,
      actor: "user",
      action: "confirm_manual_match",
      entity: "transaction_link",
      entityId: link.id,
      before: { status: "proposed" },
      after: { status: "confirmed", bank: link.from_txn_id, manual: link.to_txn_id },
    }),
  ];
  for (const s of siblings) {
    statements.push(
      stmt("UPDATE transaction_links SET status = 'rejected', decided_at = ? WHERE id = ?", at, s.id),
      auditStatement({
        at,
        actor: "user",
        action: "reject_manual_match",
        entity: "transaction_link",
        entityId: s.id,
        before: { status: "proposed" },
        after: { status: "rejected" },
        reason: `alternativa scartata dalla conferma di ${link.id}`,
      }),
    );
    if (s.to_txn_id !== link.to_txn_id) {
      // l'altro manuale aveva questo movimento come candidato: resta da verificare solo se ha altre proposte aperte
      const others = links.filter(
        (o) =>
          o.to_txn_id === s.to_txn_id && o.status === "proposed" && !siblings.includes(o) && o.id !== link.id,
      );
      if (others.length === 0) {
        statements.push(
          closeReviewStatement(REVIEW_KIND, "transaction", s.to_txn_id, at, `candidato usato da ${link.id}`),
        );
      }
    }
  }
  await commitGuarded(ctx.db, seq, statements);
  return { linkId: link.id, rejected: siblings.map((s) => s.id) };
}

export async function rejectManualMatch(
  ctx: Ctx,
  linkId: string,
  reason: string,
): Promise<{ linkId: string }> {
  if (typeof reason !== "string" || reason.trim() === "") throw new ValidationError("motivo obbligatorio");
  const link = await loadMatchLink(ctx, linkId);
  if (link.status !== "proposed") throw new ValidationError(`abbinamento gia' deciso (${link.status})`);
  const stillOpen = (await manualLinks(ctx)).filter(
    (l) => l.to_txn_id === link.to_txn_id && l.status === "proposed" && l.id !== link.id,
  );
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const statements: Statement[] = [
    stmt("UPDATE transaction_links SET status = 'rejected', decided_at = ? WHERE id = ?", at, link.id),
    auditStatement({
      at,
      actor: "user",
      action: "reject_manual_match",
      entity: "transaction_link",
      entityId: link.id,
      before: { status: "proposed" },
      after: { status: "rejected" },
      reason: reason.trim(),
    }),
  ];
  if (stillOpen.length === 0) {
    statements.push(
      closeReviewStatement(REVIEW_KIND, "transaction", link.to_txn_id, at, "nessun abbinamento"),
    );
  }
  await commitGuarded(ctx.db, seq, statements);
  return { linkId: link.id };
}

export async function reverseManualMatch(
  ctx: Ctx,
  linkId: string,
  reason: string,
): Promise<{ linkId: string }> {
  if (typeof reason !== "string" || reason.trim() === "") throw new ValidationError("motivo obbligatorio");
  const link = await loadMatchLink(ctx, linkId);
  if (!active(link))
    throw new ValidationError("si revoca solo un abbinamento confermato e non gia' revocato");
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  await commitGuarded(ctx.db, seq, [
    stmt(
      "INSERT INTO link_reversals (link_id, reason, created_at) VALUES (?, ?, ?)",
      link.id,
      reason.trim(),
      at,
    ),
    auditStatement({
      at,
      actor: "user",
      action: "reverse_manual_match",
      entity: "transaction_link",
      entityId: link.id,
      before: { status: "confirmed", bank: link.from_txn_id, manual: link.to_txn_id },
      after: { reversed: true },
      reason: reason.trim(),
    }),
  ]);
  return { linkId: link.id };
}

export async function manualMatchState(ctx: Ctx, manualId: string): Promise<ManualMatchState> {
  const txn = await ctx.db.first<{ origin: string }>("SELECT origin FROM transactions WHERE id = ?", [
    manualId,
  ]);
  if (txn === null) throw new NotFoundError("movimento", manualId);
  if (txn.origin !== "manual") throw new ValidationError("non e' un movimento manuale");
  if ((await ctx.db.first("SELECT 1 AS x FROM manual_voids WHERE transaction_id = ?", [manualId])) !== null) {
    return { state: "annullato" };
  }
  const mine = (await manualLinks(ctx)).filter((l) => l.to_txn_id === manualId);
  const done = mine.find(active);
  if (done !== undefined) return { state: "riconciliato", linkId: done.id, bankId: done.from_txn_id };
  const open = mine.filter((l) => l.status === "proposed");
  if (open.length === 1) {
    const only = open[0] as MatchLink;
    return { state: "proposto", linkId: only.id, bankId: only.from_txn_id };
  }
  if (open.length > 1)
    return { state: "ambiguo", candidates: open.map((l) => ({ linkId: l.id, bankId: l.from_txn_id })) };
  return { state: "aperto" };
}

/** Finestra di ricerca usata dal rilevatore (per l'interfaccia): [data - N, data + N]. */
export function matchWindow(date: string): { from: string; to: string } {
  return { from: addDays(date, -MATCH_WINDOW_DAYS), to: addDays(date, MATCH_WINDOW_DAYS) };
}
