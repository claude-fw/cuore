/**
 * Collegamenti tra transazioni: rilevamento (solo proposte) e decisione dell'utente.
 *
 * Effetto delle conferme:
 *   - transfer           -> entrambe classificate dall'utente come `transfer` (escluse da entrate e spese);
 *   - refund             -> l'entrata diventa `refund` con la categoria della spesa originale;
 *   - pending_booked     -> la pending e' sostituita dalla contabilizzata: esce dalle pending aperte;
 *   - possible_duplicate -> la transazione piu' recente (`to`) e' un doppione: esclusa da riepiloghi e riconciliazione.
 * Nessuna transazione viene cancellata o fusa.
 */
import type { Ctx } from "../context.ts";
import { NotFoundError, ValidationError } from "../context.ts";
import type { Statement } from "../db/types.ts";
import { stmt } from "../db/types.ts";
import { commitGuarded, readWriteSeq } from "../db/write.ts";
import { findRefundPairs, findTransferPairs, type MatchResult, type MatchTxn } from "../domain/matching.ts";
import { auditStatement, reviewStatement } from "./audit.ts";
import { closeReviewStatement, userClassificationStatements } from "./classification.ts";
import { EXCLUDED_IDS_SQL } from "./effective.ts";

export type LinkType = "transfer" | "refund" | "pending_booked" | "possible_duplicate";

export interface Link {
  id: string;
  type: LinkType;
  from_txn_id: string;
  to_txn_id: string;
  status: "proposed" | "confirmed" | "rejected";
  origin: "detector" | "user";
  created_at: string;
  decided_at: string | null;
}

/** Movimenti collegati a crediti/debiti (query diretta: evita una dipendenza circolare con obligations.ts). */
async function obligationLinked(ctx: Ctx): Promise<Set<string>> {
  const rows = await ctx.db.all<{ id: string }>(
    "SELECT DISTINCT transaction_id AS id FROM obligation_events WHERE transaction_id IS NOT NULL",
  );
  return new Set(rows.map((r) => r.id));
}

/** Transazioni da escludere da riepiloghi e riconciliazioni (doppioni confermati, pending sostituite). */
export async function excludedTransactionIds(ctx: Ctx): Promise<Set<string>> {
  const rows = await ctx.db.all<{ id: string }>(EXCLUDED_IDS_SQL);
  return new Set(rows.map((r) => r.id));
}

export interface DetectCounts {
  transfersProposed: number;
  refundsProposed: number;
  ambiguous: number;
}

export async function detectLinks(ctx: Ctx): Promise<DetectCounts> {
  const seq = await readWriteSeq(ctx.db);
  const txns = await ctx.db.all<MatchTxn>(
    `SELECT id, account_id, amount_minor, currency, COALESCE(booking_date, transaction_date) AS date, description, counterparty_norm,
            source_txn_id
     FROM transactions WHERE status = 'booked' ORDER BY date, id`,
  );
  const links = await ctx.db.all<Link>(
    "SELECT * FROM transaction_links WHERE type IN ('transfer', 'refund')",
  );
  const excluded = await excludedTransactionIds(ctx);
  // movimenti gia' collegati a crediti/debiti: mai riproposti come trasferimenti o rimborsi
  const inUse = new Set([...excluded, ...(await obligationLinked(ctx))]);
  for (const l of links) {
    if (l.status !== "rejected") {
      inUse.add(l.from_txn_id);
      inUse.add(l.to_txn_id);
    }
  }
  const rejected = (type: LinkType) =>
    new Set(
      links
        .filter((l) => l.type === type && l.status === "rejected")
        .map((l) => `${l.from_txn_id}>${l.to_txn_id}`),
    );

  const transfers = findTransferPairs(txns, inUse, rejected("transfer"));
  const refunds = findRefundPairs(txns, inUse, rejected("refund"));
  const at = ctx.now();
  const statements: Statement[] = [];
  const add = (type: LinkType, result: MatchResult) => {
    for (const p of result.proposals) {
      statements.push(
        stmt(
          `INSERT OR IGNORE INTO transaction_links (id, type, from_txn_id, to_txn_id, status, origin, created_at)
           VALUES (?, ?, ?, ?, 'proposed', 'detector', ?)`,
          ctx.newId(),
          type,
          p.fromId,
          p.toId,
          at,
        ),
        reviewStatement({
          id: ctx.newId(),
          kind: type,
          entity: "transaction",
          entityId: p.toId,
          detail: { da: p.fromId, a: p.toId },
          createdAt: at,
        }),
      );
    }
    for (const a of result.ambiguous) {
      statements.push(
        reviewStatement({
          id: ctx.newId(),
          kind: `${type}_ambiguous`,
          entity: "transaction",
          entityId: a.id,
          detail: { candidate: a.candidates },
          createdAt: at,
        }),
      );
    }
  };
  add("transfer", transfers);
  add("refund", refunds);
  const counts = {
    transfersProposed: transfers.proposals.length,
    refundsProposed: refunds.proposals.length,
    ambiguous: transfers.ambiguous.length + refunds.ambiguous.length,
  };
  if (statements.length > 0) await commitGuarded(ctx.db, seq, statements);
  return counts;
}

export function listLinks(ctx: Ctx, status?: Link["status"]): Promise<Link[]> {
  return status === undefined
    ? ctx.db.all<Link>("SELECT * FROM transaction_links ORDER BY created_at, id")
    : ctx.db.all<Link>("SELECT * FROM transaction_links WHERE status = ? ORDER BY created_at, id", [status]);
}

export async function decideLink(
  ctx: Ctx,
  linkId: string,
  decision: "confirmed" | "rejected",
): Promise<Link> {
  const link = await ctx.db.first<Link>("SELECT * FROM transaction_links WHERE id = ?", [linkId]);
  if (link === null) throw new NotFoundError("collegamento", linkId);
  if (link.status !== "proposed") throw new ValidationError(`collegamento gia' deciso (${link.status})`);
  if (decision === "confirmed" && (link.type === "transfer" || link.type === "refund")) {
    const used = await obligationLinked(ctx);
    if (used.has(link.from_txn_id) || used.has(link.to_txn_id)) {
      throw new ValidationError(
        "movimento gia' collegato a crediti/debiti: non puo' essere anche trasferimento o rimborso",
      );
    }
  }
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const statements: Statement[] = [
    stmt("UPDATE transaction_links SET status = ?, decided_at = ? WHERE id = ?", decision, at, linkId),
    closeReviewStatement(link.type, "transaction", link.to_txn_id, at, `collegamento ${decision}`),
    auditStatement({
      at,
      actor: "user",
      action: `link_${decision}`,
      entity: "transaction_link",
      entityId: linkId,
      before: { status: link.status },
      after: { status: decision, type: link.type, from: link.from_txn_id, to: link.to_txn_id },
    }),
  ];
  if (decision === "confirmed") {
    if (link.type === "transfer") {
      statements.push(
        ...userClassificationStatements(
          link.from_txn_id,
          "transfer",
          null,
          `trasferimento confermato (${linkId})`,
          at,
        ),
        ...userClassificationStatements(
          link.to_txn_id,
          "transfer",
          null,
          `trasferimento confermato (${linkId})`,
          at,
        ),
      );
    } else if (link.type === "refund") {
      const original = await ctx.db.first<{ category_id: string | null }>(
        "SELECT category_id FROM current_classifications WHERE transaction_id = ?",
        [link.from_txn_id],
      );
      statements.push(
        ...userClassificationStatements(
          link.to_txn_id,
          "refund",
          original?.category_id ?? null,
          `rimborso confermato (${linkId})`,
          at,
        ),
      );
    } else if (link.type === "pending_booked") {
      statements.push(
        closeReviewStatement(
          "pending_not_seen",
          "transaction",
          link.from_txn_id,
          at,
          "sostituita dalla contabilizzata",
        ),
      );
    }
  }
  await commitGuarded(ctx.db, seq, statements);
  return { ...link, status: decision, decided_at: at };
}
