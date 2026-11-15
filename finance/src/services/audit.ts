import type { Statement } from "../db/types.ts";
import { stmt } from "../db/types.ts";

export type Actor = "system" | "import" | "rule" | "user";

export interface AuditEntry {
  at: string;
  actor: Actor;
  action: string;
  entity: string;
  entityId: string;
  before?: unknown;
  after?: unknown;
  reason?: string | null;
  importRunId?: string | null;
}

/** Riga di audit da includere nello STESSO batch della modifica che descrive. */
export function auditStatement(e: AuditEntry): Statement {
  return stmt(
    `INSERT INTO audit_log (at, actor, action, entity, entity_id, before_json, after_json, reason, import_run_id)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    e.at,
    e.actor,
    e.action,
    e.entity,
    e.entityId,
    e.before === undefined ? null : JSON.stringify(e.before),
    e.after === undefined ? null : JSON.stringify(e.after),
    e.reason ?? null,
    e.importRunId ?? null,
  );
}

export interface ReviewEntry {
  id: string;
  kind: string;
  entity: string;
  entityId: string;
  detail: unknown;
  createdAt: string;
}

/** Apre un elemento da verificare; se ne esiste gia' uno aperto per la stessa entita' e lo stesso tipo, non duplica. */
export function reviewStatement(r: ReviewEntry): Statement {
  return stmt(
    `INSERT OR IGNORE INTO review_items (id, kind, entity, entity_id, status, detail_json, created_at)
     VALUES (?, ?, ?, ?, 'open', ?, ?)`,
    r.id,
    r.kind,
    r.entity,
    r.entityId,
    JSON.stringify(r.detail),
    r.createdAt,
  );
}
