import type { Ctx } from "../context.ts";
import { NotFoundError, ValidationError } from "../context.ts";
import { stmt } from "../db/types.ts";
import { commitGuarded, readWriteSeq } from "../db/write.ts";
import { auditStatement } from "./audit.ts";

export interface ReviewItem {
  id: string;
  kind: string;
  entity: string;
  entity_id: string;
  status: "open" | "resolved" | "dismissed";
  detail: unknown;
  created_at: string;
  closed_at: string | null;
  resolution: string | null;
}

export async function listReviewItems(
  ctx: Ctx,
  status: ReviewItem["status"] | "all" = "open",
): Promise<ReviewItem[]> {
  const rows = await ctx.db.all<Omit<ReviewItem, "detail"> & { detail_json: string }>(
    status === "all"
      ? "SELECT * FROM review_items ORDER BY created_at, kind, id"
      : "SELECT * FROM review_items WHERE status = ? ORDER BY created_at, kind, id",
    status === "all" ? [] : [status],
  );
  return rows.map(({ detail_json, ...r }) => ({ ...r, detail: JSON.parse(detail_json) as unknown }));
}

/** Chiude a mano un elemento (risolto o archiviato), con motivazione obbligatoria. */
export async function closeReviewItem(
  ctx: Ctx,
  id: string,
  status: "resolved" | "dismissed",
  resolution: string,
): Promise<void> {
  if (resolution.trim() === "") throw new ValidationError("motivazione obbligatoria");
  const item = await ctx.db.first<{ status: string }>("SELECT status FROM review_items WHERE id = ?", [id]);
  if (item === null) throw new NotFoundError("elemento da verificare", id);
  if (item.status !== "open") throw new ValidationError(`elemento gia' chiuso (${item.status})`);
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  await commitGuarded(ctx.db, seq, [
    stmt(
      "UPDATE review_items SET status = ?, closed_at = ?, resolution = ? WHERE id = ?",
      status,
      at,
      resolution,
      id,
    ),
    auditStatement({
      at,
      actor: "user",
      action: `review_${status}`,
      entity: "review_item",
      entityId: id,
      reason: resolution,
    }),
  ]);
}
