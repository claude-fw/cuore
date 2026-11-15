/** Esecutore di lavori idempotente (B3): orologio finto, deposito in memoria, nessun servizio reale. */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  defaultBackoffMs,
  type ExecutorOptions,
  MemoryJobStore,
  PermanentError,
  reopenFailedJob,
  runJob,
} from "../src/jobs/executor.ts";

function clock(start = Date.parse("2026-03-01T07:20:00Z")) {
  let t = start;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

function gate() {
  let open: () => void = () => {};
  const p = new Promise<void>((r) => (open = r));
  return { wait: () => p, open: () => open() };
}

const opts = (
  store: MemoryJobStore,
  c: ReturnType<typeof clock>,
  owner = "a",
  extra: Partial<ExecutorOptions> = {},
) =>
  ({
    store,
    now: c.now,
    owner,
    leaseMs: 60_000,
    maxAttempts: 3,
    backoffMs: (n: number) => n * 1000,
    ...extra,
  }) as ExecutorOptions;

describe("esecutore di lavori", () => {
  it("esegue una volta sola: le chiamate successive restituiscono il risultato memorizzato", async () => {
    const s = new MemoryJobStore();
    const c = clock();
    let calls = 0;
    const h = async () => ({ nuovi: ++calls });
    const first = await runJob(opts(s, c), "sync:conto:2026-03-01T07", "sync", h);
    const second = await runJob(opts(s, c, "b"), "sync:conto:2026-03-01T07", "sync", h);
    assert.deepEqual(first, { status: "succeeded", result: { nuovi: 1 }, cached: false, attempts: 1 });
    assert.deepEqual(second, { status: "succeeded", result: { nuovi: 1 }, cached: true, attempts: 1 });
    assert.equal(calls, 1);
  });

  it("concorrenza: 20 esecutori sulla stessa chiave, il gestore gira una volta, gli altri trovano busy", async () => {
    const s = new MemoryJobStore();
    const c = clock();
    const g = gate();
    let calls = 0;
    const h = async () => {
      calls += 1;
      await g.wait();
      return "ok";
    };
    const runs = Array.from({ length: 20 }, (_, i) => runJob(opts(s, c, `w${i}`), "k", "t", h));
    await new Promise((r) => setImmediate(r));
    g.open();
    const out = await Promise.all(runs);
    assert.equal(calls, 1);
    assert.equal(out.filter((o) => o.status === "succeeded").length, 1);
    assert.equal(out.filter((o) => o.status === "busy").length, 19);
  });

  it("errori temporanei: attesa rispettata, poi nuovo tentativo, fino al successo", async () => {
    const s = new MemoryJobStore();
    const c = clock();
    let calls = 0;
    const h = async () => {
      calls += 1;
      if (calls < 3) throw new Error("rete non raggiungibile");
      return calls;
    };
    const r1 = await runJob(opts(s, c), "k", "t", h);
    assert.equal(r1.status, "retry_scheduled");
    assert.equal((r1 as { nextAttemptAt: number }).nextAttemptAt, c.now() + 1000);
    assert.deepEqual(await runJob(opts(s, c), "k", "t", h), {
      status: "waiting",
      nextAttemptAt: c.now() + 1000,
    });
    assert.equal(calls, 1);
    c.advance(1000);
    const r2 = await runJob(opts(s, c), "k", "t", h);
    assert.equal((r2 as { nextAttemptAt: number }).nextAttemptAt, c.now() + 2000);
    c.advance(2000);
    assert.deepEqual(await runJob(opts(s, c), "k", "t", h), {
      status: "succeeded",
      result: 3,
      cached: false,
      attempts: 3,
    });
    const rec = await s.get("k");
    assert.deepEqual(
      rec?.history.map((e) => e.event),
      ["creato", "avviato", "errore temporaneo", "avviato", "errore temporaneo", "avviato", "riuscito"],
    );
  });

  it("tentativi esauriti: fallito, non si riesegue; si riapre solo a mano con motivo", async () => {
    const s = new MemoryJobStore();
    const c = clock();
    let calls = 0;
    const h = async () => {
      calls += 1;
      throw new Error("sempre giu'");
    };
    for (let i = 0; i < 3; i++) {
      await runJob(opts(s, c), "k", "t", h);
      c.advance(10_000);
    }
    const dead = await runJob(opts(s, c), "k", "t", h);
    assert.equal(dead.status, "failed");
    assert.match((dead as { error: string }).error, /tentativi esauriti/);
    assert.equal(calls, 3);
    await assert.rejects(reopenFailedJob(s, "k", c.now(), " "), /motivo/);
    assert.equal(await reopenFailedJob(s, "k", c.now(), "banca tornata disponibile"), true);
    const again = await runJob(opts(s, c), "k", "t", async () => "ok");
    assert.equal(again.status, "succeeded");
  });

  it("errore permanente: fallito al primo tentativo", async () => {
    const s = new MemoryJobStore();
    const c = clock();
    const r = await runJob(opts(s, c), "k", "t", async () => {
      throw new PermanentError("consenso bancario scaduto");
    });
    assert.deepEqual(r, {
      status: "failed",
      error: "PermanentError: consenso bancario scaduto",
      attempts: 1,
    });
  });

  it("interruzione: il lease scade, un altro esecutore riprende; il primo, tornato, trova lease_lost", async () => {
    const s = new MemoryJobStore();
    const c = clock();
    const g = gate();
    const stuck = runJob(opts(s, c, "mac"), "k", "t", async () => {
      await g.wait();
      return "dal mac";
    });
    await new Promise((r) => setImmediate(r));
    assert.equal((await runJob(opts(s, c, "cloud"), "k", "t", async () => "x")).status, "busy");
    c.advance(60_001);
    const resumed = await runJob(opts(s, c, "cloud"), "k", "t", async () => "dal cloud");
    assert.deepEqual(resumed, { status: "succeeded", result: "dal cloud", cached: false, attempts: 2 });
    g.open();
    assert.deepEqual(await stuck, { status: "lease_lost" });
    const rec = await s.get("k");
    assert.equal(rec?.result, "dal cloud");
    assert.ok(rec?.history.some((e) => e.event === "ripresa"));
  });

  it("interruzione all'ultimo tentativo: fallito senza rieseguire", async () => {
    const s = new MemoryJobStore();
    const c = clock();
    const g = gate();
    void runJob(opts(s, c, "a", { maxAttempts: 1 }), "k", "t", () => g.wait().then(() => 1));
    await new Promise((r) => setImmediate(r));
    c.advance(60_001);
    let calls = 0;
    const r = await runJob(opts(s, c, "b", { maxAttempts: 1 }), "k", "t", async () => ++calls);
    assert.equal(r.status, "failed");
    assert.equal(calls, 0);
    g.open();
  });

  it("chiavi e opzioni validate; una chiave non cambia tipo; attese predefinite crescenti e limitate", async () => {
    const s = new MemoryJobStore();
    const c = clock();
    await assert.rejects(
      runJob(opts(s, c), " ", "t", async () => 1),
      /chiave/,
    );
    await assert.rejects(
      runJob(opts(s, c, "a", { maxAttempts: 0 }), "k", "t", async () => 1),
      /opzioni/,
    );
    await runJob(opts(s, c), "k", "t", async () => 1);
    await assert.rejects(
      runJob(opts(s, c), "k", "altro", async () => 1),
      /tipo/,
    );
    assert.deepEqual([1, 2, 3, 4].map(defaultBackoffMs), [30_000, 120_000, 480_000, 1_920_000]);
    assert.equal(defaultBackoffMs(20), 6 * 3_600_000);
  });

  it("il deposito rifiuta versioni non consecutive e non condivide riferimenti", async () => {
    const s = new MemoryJobStore();
    const c = clock();
    await runJob(opts(s, c), "k", "t", async () => ({ n: 1 }));
    const rec = await s.get("k");
    assert.ok(rec !== null);
    assert.equal(await s.replace("k", rec.version, { ...rec, version: rec.version + 2 }), false);
    (rec.result as { n: number }).n = 99;
    assert.deepEqual((await s.get("k"))?.result, { n: 1 });
  });
});
