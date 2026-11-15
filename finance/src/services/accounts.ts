import type { Ctx } from "../context.ts";
import { NotFoundError, ValidationError } from "../context.ts";
import { stmt } from "../db/types.ts";
import { commitGuarded, readWriteSeq } from "../db/write.ts";
import { isSupportedCurrency } from "../domain/money.ts";
import { isIsoDate } from "../domain/text.ts";
import { auditStatement } from "./audit.ts";

export const ACCOUNT_TYPES = ["current", "savings", "card", "cash", "investment", "other"] as const;
export const HOLDER_ROLES = ["owner", "joint", "delegate", "other"] as const;
export type AccountType = (typeof ACCOUNT_TYPES)[number];
export type HolderRole = (typeof HOLDER_ROLES)[number];

export interface Account {
  id: string;
  institution_id: string | null;
  name: string;
  type: AccountType;
  currency: string;
  holder_role: HolderRole;
  created_at: string;
  closed_at: string | null;
}

export interface NewAccount {
  id: string;
  name: string;
  type: AccountType;
  currency: string;
  holderRole: HolderRole;
  institutionId?: string | null;
}

const ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export async function createInstitution(ctx: Ctx, id: string, name: string): Promise<void> {
  if (!ID.test(id)) throw new ValidationError(`id istituto non valido: ${id}`);
  if (name.trim() === "") throw new ValidationError("nome istituto obbligatorio");
  const existing = await ctx.db.first<{ name: string }>("SELECT name FROM institutions WHERE id = ?", [id]);
  if (existing !== null) {
    if (existing.name === name) return;
    throw new ValidationError(`istituto ${id} gia' presente con un altro nome`);
  }
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  await commitGuarded(ctx.db, seq, [
    stmt("INSERT INTO institutions (id, name, created_at) VALUES (?, ?, ?)", id, name, at),
    auditStatement({
      at,
      actor: "user",
      action: "create",
      entity: "institution",
      entityId: id,
      after: { name },
    }),
  ]);
}

export async function createAccount(ctx: Ctx, input: NewAccount): Promise<Account> {
  if (!ID.test(input.id))
    throw new ValidationError(`id conto non valido: ${input.id} (minuscole, cifre, - e _)`);
  if (input.name.trim() === "") throw new ValidationError("nome del conto obbligatorio");
  if (!ACCOUNT_TYPES.includes(input.type))
    throw new ValidationError(`tipo di conto non valido: ${input.type}`);
  if (!HOLDER_ROLES.includes(input.holderRole))
    throw new ValidationError(`titolarita' non valida: ${input.holderRole}`);
  if (!isSupportedCurrency(input.currency))
    throw new ValidationError(`valuta non supportata: ${input.currency}`);
  if (input.institutionId != null) {
    const inst = await ctx.db.first("SELECT id FROM institutions WHERE id = ?", [input.institutionId]);
    if (inst === null) throw new NotFoundError("istituto", input.institutionId);
  }
  if ((await ctx.db.first("SELECT id FROM accounts WHERE id = ?", [input.id])) !== null) {
    throw new ValidationError(`conto gia' esistente: ${input.id}`);
  }
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const account: Account = {
    id: input.id,
    institution_id: input.institutionId ?? null,
    name: input.name,
    type: input.type,
    currency: input.currency,
    holder_role: input.holderRole,
    created_at: at,
    closed_at: null,
  };
  await commitGuarded(ctx.db, seq, [
    stmt(
      `INSERT INTO accounts (id, institution_id, name, type, currency, holder_role, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)`,
      account.id,
      account.institution_id,
      account.name,
      account.type,
      account.currency,
      account.holder_role,
      at,
    ),
    auditStatement({
      at,
      actor: "user",
      action: "create",
      entity: "account",
      entityId: account.id,
      after: account,
    }),
  ]);
  return account;
}

export async function getAccount(ctx: Ctx, id: string): Promise<Account> {
  const account = await ctx.db.first<Account>("SELECT * FROM accounts WHERE id = ?", [id]);
  if (account === null) throw new NotFoundError("conto", id);
  return account;
}

export function listAccounts(ctx: Ctx): Promise<Account[]> {
  return ctx.db.all<Account>("SELECT * FROM accounts ORDER BY id");
}

export const FORMAL_HOLDERS = ["self", "parent", "self_with_parent", "other", "unknown"] as const;
export const PRIMARY_USERS = ["self", "parent", "shared", "unknown"] as const;
export const SUPERVISIONS = ["parent", "none", "unknown"] as const;

export interface AccountProfile {
  accountId: string;
  formalHolder: (typeof FORMAL_HOLDERS)[number];
  primaryUser: (typeof PRIMARY_USERS)[number];
  supervision: (typeof SUPERVISIONS)[number];
  note: string | null;
  /** null = mai dichiarato: tutti i valori restano `unknown` */
  declaredAt: string | null;
}

/**
 * Dichiarazione dell'utente su intestatario formale, utilizzatore e supervisione familiare. Mai dedotta dal nome in
 * un estratto: e' un'azione esplicita, conservata nello storico (vale l'ultima).
 */
export async function setAccountProfile(
  ctx: Ctx,
  accountId: string,
  input: Omit<AccountProfile, "accountId" | "declaredAt">,
): Promise<AccountProfile> {
  await getAccount(ctx, accountId);
  if (!FORMAL_HOLDERS.includes(input.formalHolder))
    throw new ValidationError(`intestatario formale non valido: ${input.formalHolder}`);
  if (!PRIMARY_USERS.includes(input.primaryUser))
    throw new ValidationError(`utilizzatore non valido: ${input.primaryUser}`);
  if (!SUPERVISIONS.includes(input.supervision))
    throw new ValidationError(`supervisione non valida: ${input.supervision}`);
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const id = ctx.newId();
  await commitGuarded(ctx.db, seq, [
    stmt(
      `INSERT INTO account_profiles (id, account_id, formal_holder, primary_user, supervision, note, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)`,
      id,
      accountId,
      input.formalHolder,
      input.primaryUser,
      input.supervision,
      input.note,
      at,
    ),
    auditStatement({
      at,
      actor: "user",
      action: "declare_profile",
      entity: "account",
      entityId: accountId,
      after: input,
    }),
  ]);
  return { accountId, ...input, declaredAt: at };
}

/** Profilo corrente di ogni conto: ultima dichiarazione, oppure tutto `unknown`. */
export async function listAccountProfiles(ctx: Ctx): Promise<AccountProfile[]> {
  const rows = await ctx.db.all<{
    id: string;
    formal_holder: AccountProfile["formalHolder"] | null;
    primary_user: AccountProfile["primaryUser"] | null;
    supervision: AccountProfile["supervision"] | null;
    note: string | null;
    created_at: string | null;
  }>(
    `SELECT a.id, p.formal_holder, p.primary_user, p.supervision, p.note, p.created_at
     FROM accounts a LEFT JOIN account_profiles p ON p.id = (
       SELECT p2.id FROM account_profiles p2 WHERE p2.account_id = a.id ORDER BY p2.created_at DESC, p2.id DESC LIMIT 1)
     ORDER BY a.id`,
  );
  return rows.map((r) => ({
    accountId: r.id,
    formalHolder: r.formal_holder ?? "unknown",
    primaryUser: r.primary_user ?? "unknown",
    supervision: r.supervision ?? "unknown",
    note: r.note,
    declaredAt: r.created_at,
  }));
}

export const CLOSURE_DATE_STATUSES = ["verified", "declared", "unknown"] as const;
export type ClosureDateStatus = (typeof CLOSURE_DATE_STATUSES)[number];

export interface AccountStatus {
  accountId: string;
  /** stato operativo dichiarato: chiuso = non e' piu' un conto attivo (lo storico resta) */
  closed: boolean;
  /** data di chiusura SOLO se nota; null se ignota */
  closureDate: string | null;
  closureDateStatus: ClosureDateStatus | null;
  /** ultimo movimento osservato (dato separato dalla chiusura) */
  lastMovementOn: string | null;
  reason: string | null;
  declaredAt: string | null;
}

/**
 * Chiusura di un conto (storico conservato). La data di chiusura e' facoltativa e porta il suo stato:
 * `verified` (documento), `declared` (dichiarata dall'utente), `unknown` (nessuna data). Ammessa solo se l'ultimo saldo
 * osservato (alla data, se nota) e' zero e nessun movimento e' successivo alla data: l'ultimo movimento resta un dato
 * separato e non diventa mai la data di chiusura.
 */
export async function closeAccount(
  ctx: Ctx,
  accountId: string,
  input: { effectiveDate: string | null; dateStatus: ClosureDateStatus; reason: string },
): Promise<AccountStatus> {
  await getAccount(ctx, accountId);
  const current = (await listAccountStatuses(ctx)).find((s) => s.accountId === accountId);
  if (current?.closed) throw new ValidationError("conto gia' chiuso");
  if (!CLOSURE_DATE_STATUSES.includes(input.dateStatus))
    throw new ValidationError(`stato della data non valido: ${input.dateStatus}`);
  if ((input.effectiveDate === null) !== (input.dateStatus === "unknown"))
    throw new ValidationError(
      "data di chiusura: indicarla con stato verified/declared, oppure null con stato unknown",
    );
  if (input.effectiveDate !== null && !isIsoDate(input.effectiveDate))
    throw new ValidationError(`data di chiusura non valida: ${input.effectiveDate}`);
  if (input.reason.trim() === "") throw new ValidationError("motivo della chiusura obbligatorio");
  const lastMovementOn = current?.lastMovementOn ?? null;
  if (input.effectiveDate !== null && lastMovementOn !== null && lastMovementOn > input.effectiveDate)
    throw new ValidationError(`movimenti successivi alla data di chiusura (ultimo: ${lastMovementOn})`);
  const bound = input.effectiveDate ?? "9999-12-31";
  const last = await ctx.db.all<{ balance_minor: number }>(
    `SELECT balance_minor FROM balance_observations WHERE account_id = ? AND as_of_date =
       (SELECT MAX(as_of_date) FROM balance_observations WHERE account_id = ? AND as_of_date <= ?)`,
    [accountId, accountId, bound],
  );
  if (last.length === 0 || last.some((b) => b.balance_minor !== 0))
    throw new ValidationError("chiusura rifiutata: ultimo saldo osservato mancante o diverso da zero");
  const seq = await readWriteSeq(ctx.db);
  const at = ctx.now();
  const id = ctx.newId();
  await commitGuarded(ctx.db, seq, [
    stmt(
      `INSERT INTO account_status_events (id, account_id, status, effective_date, effective_date_status,
         last_movement_on, reason, created_at) VALUES (?, ?, 'closed', ?, ?, ?, ?, ?)`,
      id,
      accountId,
      input.effectiveDate,
      input.dateStatus,
      lastMovementOn,
      input.reason,
      at,
    ),
    auditStatement({
      at,
      actor: "user",
      action: "close",
      entity: "account",
      entityId: accountId,
      after: {
        effective_date: input.effectiveDate,
        effective_date_status: input.dateStatus,
        last_movement_on: lastMovementOn,
      },
      reason: input.reason,
    }),
  ]);
  return {
    accountId,
    closed: true,
    closureDate: input.effectiveDate,
    closureDateStatus: input.dateStatus,
    lastMovementOn,
    reason: input.reason,
    declaredAt: at,
  };
}

/** Stato operativo di ogni conto: ultimo evento (chiuso/riaperto) e ultimo movimento osservato. */
export async function listAccountStatuses(ctx: Ctx): Promise<AccountStatus[]> {
  const rows = await ctx.db.all<{
    id: string;
    status: "closed" | "reopened" | null;
    effective_date: string | null;
    effective_date_status: ClosureDateStatus | null;
    reason: string | null;
    created_at: string | null;
    last_movement_on: string | null;
  }>(
    `SELECT a.id, e.status, e.effective_date, e.effective_date_status, e.reason, e.created_at,
            (SELECT MAX(COALESCE(t.booking_date, t.transaction_date)) FROM transactions t WHERE t.account_id = a.id)
              AS last_movement_on
     FROM accounts a LEFT JOIN account_status_events e ON e.id = (
       SELECT e2.id FROM account_status_events e2 WHERE e2.account_id = a.id ORDER BY e2.created_at DESC, e2.id DESC LIMIT 1)
     ORDER BY a.id`,
  );
  return rows.map((r) => {
    const closed = r.status === "closed";
    return {
      accountId: r.id,
      closed,
      closureDate: closed ? r.effective_date : null,
      closureDateStatus: closed ? r.effective_date_status : null,
      lastMovementOn: r.last_movement_on,
      reason: r.reason,
      declaredAt: r.created_at,
    };
  });
}

/**
 * Conto chiuso alla data `asOf`? Con data di chiusura nota: dalla data. Con data ignota: solo DOPO l'ultimo movimento
 * osservato (limite operativo, NON una data di chiusura): prima di allora il conto era certamente attivo.
 */
export function isClosedAt(s: AccountStatus | undefined, asOf: string): boolean {
  if (s === undefined || !s.closed) return false;
  if (s.closureDate !== null) return s.closureDate <= asOf;
  return s.lastMovementOn === null || asOf > s.lastMovementOn;
}
