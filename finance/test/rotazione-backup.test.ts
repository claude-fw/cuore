/**
 * Applicazione della rotazione (C-08): nomi sintetici, cartella finta, `trash` SEMPRE sostituito da una funzione
 * finta (mai eseguito per davvero in questi test).
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { POLICY, runRotation, trashFile } from "../scripts/rotazione-backup.ts";

const sync = (iso: string, db = "finance") => `${db}-prima-di-sync-${iso.replace(/[:.]/g, "-")}.sqlite3`;

describe("rotazione-backup (applicazione)", () => {
  it("in prova (default) non chiama mai trash, anche con scaduti", () => {
    const names = [
      sync("2026-03-03T19:20:00.000Z"),
      sync("2026-03-02T19:20:00.000Z"),
      sync("2026-01-15T07:20:00.000Z"),
      "appunti.txt",
    ];
    const chiamate: string[] = [];
    const result = runRotation(
      "/backup/finta",
      { keepLast: 1, keepDaily: 1, keepMonthly: 1 },
      {
        applica: false,
        listFiles: () => names,
        trash: (p) => chiamate.push(p),
      },
    );
    assert.equal(result.applicato, false);
    assert.deepEqual(result.spostatiNelCestino, []);
    assert.deepEqual(chiamate, []);
    assert.ok(result.scaduti.length > 0, "deve esserci almeno uno scaduto da provare");
  });

  it("con --applica sposta nel Cestino solo gli scaduti, mai i tenuti o gli ignorati", () => {
    const names = [
      sync("2026-03-03T19:20:00.000Z"), // tenuto: piu' recente
      sync("2026-03-02T19:20:00.000Z"), // tenuto: giornaliero
      sync("2026-01-15T07:20:00.000Z"), // scaduto
      "appunti.txt", // ignorato: nome estraneo
    ];
    const chiamate: string[] = [];
    const result = runRotation(
      "/backup/finta",
      { keepLast: 1, keepDaily: 2, keepMonthly: 1 },
      {
        applica: true,
        listFiles: () => names,
        trash: (p) => chiamate.push(p),
      },
    );
    assert.equal(result.applicato, true);
    assert.deepEqual(result.spostatiNelCestino, [sync("2026-01-15T07:20:00.000Z")]);
    assert.deepEqual(chiamate, [`/backup/finta/${sync("2026-01-15T07:20:00.000Z")}`]);
    // mai toccati: i tenuti e gli ignorati
    for (const tenuto of result.tenuti) assert.ok(!chiamate.includes(`/backup/finta/${tenuto.name}`));
    for (const ignorato of result.ignorati) assert.ok(!chiamate.includes(`/backup/finta/${ignorato}`));
  });

  it("non tocca mai gli ultimi 7 recenti con la politica C-08 (7/30/12)", () => {
    assert.deepEqual(POLICY, { keepLast: 7, keepDaily: 30, keepMonthly: 12 });
    // 10 backup dello stesso giorno/ora via via piu' vecchi: con keepLast=7 i primi 7 sono sempre tenuti
    const names = Array.from({ length: 10 }, (_, i) =>
      sync(
        `2026-03-0${String(Math.floor(i / 2) + 1).padStart(1, "0")}T${(19 - (i % 2) * 2)
          .toString()
          .padStart(2, "0")}:20:00.000Z`,
      ),
    );
    const chiamate: string[] = [];
    const result = runRotation("/backup/finta", POLICY, {
      applica: true,
      listFiles: () => names,
      trash: (p) => chiamate.push(p),
    });
    const ultimiSette = result.tenuti.filter((k) => k.reasons.includes("ultimi")).map((k) => k.name);
    assert.equal(ultimiSette.length, 7);
    for (const nome of ultimiSette) assert.ok(!chiamate.includes(`/backup/finta/${nome}`));
  });

  it("trashFile() e' una funzione reale esportata ma distinta da quella usata nei test sopra", () => {
    // verifica solo che esista e sia una funzione: non viene MAI invocata (eseguirebbe il comando di sistema `trash`)
    assert.equal(typeof trashFile, "function");
  });
});
