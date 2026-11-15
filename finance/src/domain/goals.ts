/** Avanzamento di un obiettivo di risparmio (funzione pura). Gli accantonamenti sono virtuali: non spostano denaro. */
import { addMinor } from "./money.ts";

export interface GoalProgress {
  allocated: number;
  remaining: number;
  /** punti base: 10000 = 100% (puo' superare 10000) */
  progressBp: number;
  reached: boolean;
}

export function goalProgress(targetMinor: number, allocations: readonly number[]): GoalProgress {
  if (!Number.isSafeInteger(targetMinor) || targetMinor <= 0)
    throw new Error("obiettivo con importo non valido");
  const allocated = allocations.reduce(addMinor, 0);
  const remaining = Math.max(0, targetMinor - allocated);
  return {
    allocated,
    remaining,
    progressBp: Math.floor((Math.max(0, allocated) * 10_000) / targetMinor),
    reached: allocated >= targetMinor,
  };
}
