import type { Migration } from "../migrate.ts";
import { migration001 } from "./001_core.ts";
import { migration002 } from "./002_classification_review.ts";
import { migration003 } from "./003_planning.ts";
import { migration004 } from "./004_bank_formats.ts";
import { migration005 } from "./005_obligations.ts";
import { migration006 } from "./006_account_profiles.ts";
import { migration007 } from "./007_manual_and_budgets.ts";
import { migration008 } from "./008_search.ts";

/** Elenco ordinato delle migrazioni. Si aggiungono SOLO in coda; quelle esistenti non si modificano mai. */
export const MIGRATIONS: readonly Migration[] = [
  migration001,
  migration002,
  migration003,
  migration004,
  migration005,
  migration006,
  migration007,
  migration008,
];

export const SCHEMA_VERSION = MIGRATIONS.length;
