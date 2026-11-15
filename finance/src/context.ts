import type { Db } from "./db/types.ts";

/** Dipendenze iniettate: database, orologio e generatore di id (deterministici nei test). */
export interface Ctx {
  readonly db: Db;
  now(): string;
  newId(): string;
}

export function createCtx(db: Db): Ctx {
  return {
    db,
    now: () => new Date().toISOString(),
    newId: () => crypto.randomUUID(),
  };
}

/** Errore di input dell'utente (dati o argomenti non validi), distinto dagli errori interni. */
export class ValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ValidationError";
  }
}

export class NotFoundError extends Error {
  constructor(entity: string, id: string) {
    super(`${entity} non trovato: ${id}`);
    this.name = "NotFoundError";
  }
}
