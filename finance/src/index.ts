/**
 * API pubblica del nucleo Cloud Finance. Indipendente dal runtime: per usarla serve un'implementazione di `Db`
 * (oggi `NodeSqliteDb`, domani un adattatore D1) e un `Ctx`.
 */
export { type Ctx, createCtx, NotFoundError, ValidationError } from "./context.ts";
export { MigrationError, migrate, migrationStatus } from "./db/migrate.ts";
export { MIGRATIONS, SCHEMA_VERSION } from "./db/migrations/index.ts";
export type { Db, SqlValue, Statement } from "./db/types.ts";
export { ConflictError } from "./db/write.ts";
export { goalProgress } from "./domain/goals.ts";
export { convertMinor, formatMinor, parseAmountToMinor } from "./domain/money.ts";
export { obligationState, obligationsPosition } from "./domain/obligations.ts";
export { reconcile } from "./domain/reconcile.ts";
export { evaluateRules, validateRuleFile } from "./domain/rules.ts";
export { budgetShares, summarize } from "./domain/summary.ts";
export { type ImportReport, importFile } from "./import/importer.ts";
export { getParser, listParsers } from "./import/parsers/registry.ts";
export type { StatementParser } from "./import/types.ts";
export {
  CONSULTATION_CONTRACT_VERSION,
  LIMITS as CONSULTATION_LIMITS,
  readOnlyDb,
} from "./query/consultation.ts";
export { OPERATIONS, type QueryResponse, runQuery } from "./query/operations.ts";
export { createAccount, createInstitution, getAccount, listAccounts } from "./services/accounts.ts";
export { addManualBalance } from "./services/balances.ts";
export {
  classifyTransactions,
  loadRuleSet,
  publishRuleSet,
  setClassification,
} from "./services/classification.ts";
export { decideLink, detectLinks, listLinks } from "./services/links.ts";
export {
  createCounterparty,
  createObligation,
  linkTransactionToObligations,
  listObligations,
  obligationsPositionAt,
  recordObligationEvent,
} from "./services/obligations.ts";
export {
  addFxRate,
  allocateToGoal,
  createGoal,
  createPlannedPurchase,
  listGoalsWithProgress,
  listPlannedPurchases,
  setPurchaseStatus,
} from "./services/planning.ts";
export { reconcileAccount, reconcileObservedPeriods } from "./services/reconciliation.ts";
export { liquidity, periodReport, status } from "./services/reports.ts";
export { closeReviewItem, listReviewItems } from "./services/review.ts";
