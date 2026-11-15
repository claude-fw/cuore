/** Pianificatore della rotazione dei backup locali (funzione pura, nomi sintetici). */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { parseBackupName, planRetention } from "../src/domain/backup-retention.ts";

const sync = (iso: string, db = "finance") => `${db}-prima-di-sync-${iso.replace(/[:.]/g, "-")}.sqlite3`;

describe("rotazione dei backup", () => {
  it("riconosce solo i nomi prodotti da stamp()", () => {
    assert.deepEqual(parseBackupName(sync("2026-03-01T07:20:00.000Z")), {
      name: "finance-prima-di-sync-2026-03-01T07-20-00-000Z.sqlite3",
      database: "finance",
      kind: "sync",
      at: "2026-03-01T07:20:00.000Z",
    });
    assert.equal(
      parseBackupName("cloud-finance-prima-di-scrittura-2026-11-04T07-20-00-000Z.sqlite3")?.kind,
      "scrittura",
    );
    assert.equal(
      parseBackupName("cloud-finance-prima-di-settimanale-2026-11-08T10-30-00-000Z.sqlite3")?.kind,
      "settimanale",
    );
    assert.equal(
      parseBackupName("cloud-finance-prima-di-scrittura-2026-11-04T07-20-00-000Z.sqlite3.in-verifica"),
      null,
    );
    assert.equal(parseBackupName("cloud-finance-prima-di-v7-2026-11-03T22-25-45.sqlite3"), null);
    assert.equal(parseBackupName("finance.sqlite3"), null);
    assert.equal(parseBackupName("finance-prima-di-sync-2026-13-01T07-20-00-000Z.sqlite3"), null);
    assert.equal(parseBackupName("finance-prima-di-sync-2026-03-01T07-20-00-000Z.sqlite3.fallito"), null);
  });

  it("tiene gli ultimi, un giornaliero e un mensile; il resto scade; i nomi estranei sono ignorati", () => {
    const names = [
      sync("2026-03-03T19:20:00.000Z"),
      sync("2026-03-03T07:20:00.000Z"),
      sync("2026-03-02T19:20:00.000Z"),
      sync("2026-03-02T07:20:00.000Z"),
      sync("2026-02-27T07:20:00.000Z"),
      sync("2026-01-15T07:20:00.000Z"),
      "appunti.txt",
    ];
    const plan = planRetention(names, { keepLast: 1, keepDaily: 2, keepMonthly: 2 });
    assert.deepEqual(plan.ignored, ["appunti.txt"]);
    assert.deepEqual(
      plan.keep.map((k) => k.name),
      [
        sync("2026-03-03T19:20:00.000Z"), // piu' recente, ultimi, giornaliero 03, mensile 03
        sync("2026-03-02T19:20:00.000Z"), // giornaliero 02
        sync("2026-02-27T07:20:00.000Z"), // mensile 02
      ],
    );
    assert.deepEqual(plan.expire, [
      sync("2026-03-03T07:20:00.000Z"),
      sync("2026-03-02T07:20:00.000Z"),
      sync("2026-01-15T07:20:00.000Z"),
    ]);
  });

  it("il piu' recente non scade mai, anche con politica a zero; database e tipi separati", () => {
    const names = [
      sync("2026-03-01T07:20:00.000Z"),
      sync("2026-03-02T07:20:00.000Z"),
      sync("2026-03-01T07:20:00.000Z", "altro"),
      "finance-prima-di-apply-2026-02-01T10-00-00-000Z.sqlite3",
    ];
    const plan = planRetention(names, { keepLast: 0, keepDaily: 0, keepMonthly: 0 });
    assert.deepEqual(plan.keep.map((k) => k.name).sort(), [
      "altro-prima-di-sync-2026-03-01T07-20-00-000Z.sqlite3",
      "finance-prima-di-apply-2026-02-01T10-00-00-000Z.sqlite3",
      sync("2026-03-02T07:20:00.000Z"),
    ]);
    assert.deepEqual(plan.expire, [sync("2026-03-01T07:20:00.000Z")]);
  });

  it("rifiuta politiche non valide", () => {
    assert.throws(() => planRetention([], { keepLast: -1, keepDaily: 0, keepMonthly: 0 }), /politica/);
    assert.throws(() => planRetention([], { keepLast: 1.5, keepDaily: 0, keepMonthly: 0 }), /politica/);
  });
});
