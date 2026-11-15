/**
 * ROTAZIONE DEI BACKUP LOCALI (funzione PURA: pianifica, non cancella).
 *
 * I backup di `bank-sync sincronizza`, `import-plan --apply`, della prima scrittura del giorno dal ponte (`scrivi.ts`) e
 * il backup settimanale verificato (`backup-settimanale.ts`, prima della rotazione della domenica) si chiamano
 * `<db>-prima-di-<sync|apply|scrittura|settimanale>-<istante>.sqlite3`, con l'istante di `stamp()` (ISO con `-` al
 * posto di `:` e `.`).
 * Il piano dice quali tenere e quali sono scaduti secondo la politica; chi lo applica (con consenso) deve spostarli nel
 * Cestino, mai cancellarli. Regole:
 *   - un nome che non segue lo schema e' IGNORATO (mai proposto come scaduto);
 *   - per ogni database e tipo si tengono sempre gli ultimi `keepLast`, piu' l'ultimo di ciascuno degli ultimi
 *     `keepDaily` giorni e di ciascuno degli ultimi `keepMonthly` mesi (giorni e mesi in UTC, contati dal piu' recente);
 *   - il backup piu' recente non scade mai, qualunque sia la politica.
 */

export interface RetentionPolicy {
  keepLast: number;
  keepDaily: number;
  keepMonthly: number;
}

export interface BackupFile {
  name: string;
  database: string;
  kind: "sync" | "apply" | "scrittura" | "settimanale";
  at: string;
}

export interface RetentionPlan {
  keep: { name: string; reasons: string[] }[];
  expire: string[];
  ignored: string[];
}

const NAME =
  /^(.+)-prima-di-(sync|apply|scrittura|settimanale)-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})-(\d{3})Z\.sqlite3$/;

export function parseBackupName(name: string): BackupFile | null {
  const m = NAME.exec(name);
  if (m === null) return null;
  const [, database, kind, day, hh, mm, ss, ms] = m as unknown as string[];
  const at = `${day}T${hh}:${mm}:${ss}.${ms}Z`;
  if (Number.isNaN(Date.parse(at))) return null;
  return { name, database: database as string, kind: kind as BackupFile["kind"], at };
}

function assertPolicy(p: RetentionPolicy): void {
  for (const [k, v] of Object.entries(p)) {
    if (!Number.isInteger(v) || v < 0) throw new Error(`politica non valida: ${k} = ${v}`);
  }
}

export function planRetention(names: readonly string[], policy: RetentionPolicy): RetentionPlan {
  assertPolicy(policy);
  const ignored: string[] = [];
  const groups = new Map<string, BackupFile[]>();
  for (const n of names) {
    const b = parseBackupName(n);
    if (b === null) {
      ignored.push(n);
      continue;
    }
    const key = `${b.database}|${b.kind}`;
    groups.set(key, [...(groups.get(key) ?? []), b]);
  }
  const reasons = new Map<string, Set<string>>();
  const mark = (name: string, why: string) => reasons.set(name, (reasons.get(name) ?? new Set()).add(why));
  const all: BackupFile[] = [];
  for (const list of groups.values()) {
    list.sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : a.name.localeCompare(b.name)));
    all.push(...list);
    mark((list[0] as BackupFile).name, "piu' recente");
    for (const b of list.slice(0, policy.keepLast)) mark(b.name, "ultimi");
    const days = new Set<string>();
    const months = new Set<string>();
    for (const b of list) {
      const day = b.at.slice(0, 10);
      const month = b.at.slice(0, 7);
      if (!days.has(day) && days.size < policy.keepDaily) {
        days.add(day);
        mark(b.name, `giornaliero ${day}`);
      }
      if (!months.has(month) && months.size < policy.keepMonthly) {
        months.add(month);
        mark(b.name, `mensile ${month}`);
      }
    }
  }
  const keep = all
    .filter((b) => reasons.has(b.name))
    .map((b) => ({
      name: b.name,
      reasons: [...(reasons.get(b.name) as Set<string>)],
    }));
  const expire = all.filter((b) => !reasons.has(b.name)).map((b) => b.name);
  return { keep, expire, ignored };
}
