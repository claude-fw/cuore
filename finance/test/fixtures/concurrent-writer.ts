/**
 * Scrittore SINTETICO per i test di concorrenza (test/readonly-sqlite.test.ts): transazioni con un invariante
 * (righe di `t` = contatore di `c`, somma di `t.v` = somma di `c`) e, in WAL, checkpoint frequenti che riscrivono il
 * file principale. Uso: node concurrent-writer.ts <database> <WAL|DELETE> <millisecondi>. Stampa { tx, busy }.
 */
import { DatabaseSync } from "node:sqlite";

const [path, mode, ms] = [process.argv[2] ?? "", process.argv[3] ?? "WAL", Number(process.argv[4] ?? 1000)];
const db = new DatabaseSync(path);
db.exec("PRAGMA busy_timeout = 5000");
db.exec(`PRAGMA journal_mode = ${mode === "DELETE" ? "DELETE" : "WAL"}`);
const pad = "x".repeat(900); // righe grandi: ogni transazione tocca molte pagine
const end = Date.now() + ms;
let tx = 0;
let busy = 0;
while (Date.now() < end) {
  try {
    db.exec("BEGIN IMMEDIATE");
    const v = 1 + (tx % 7);
    for (let i = 0; i < 40; i++) db.prepare("INSERT INTO t(v, pad) VALUES (?, ?)").run(v, pad);
    db.prepare("UPDATE c SET n = n + 40, s = s + ?").run(40 * v);
    db.exec("COMMIT");
    tx++;
    if (mode !== "DELETE" && tx % 3 === 0) db.exec("PRAGMA wal_checkpoint(TRUNCATE)");
  } catch {
    busy++;
    try {
      db.exec("ROLLBACK");
    } catch {
      // nessuna transazione aperta
    }
  }
}
db.close();
process.stdout.write(JSON.stringify({ tx, busy }));
