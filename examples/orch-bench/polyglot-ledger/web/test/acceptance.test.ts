import { describe, expect, it } from "vitest";
import { formatCents, parseExport, reconcile, type Entry, type Ledger } from "../src/ledger";

// The exact bytes the Go service's ExportJSON returns for three entries (R3, R7).
const EXPORTED =
  '{"balances":{"Alpha":7,"zeta":0},"entries":[' +
  '{"account":"zeta","amount_cents":-1250,"date":"2024-02-29","memo":"coffee & \\"beans\\" <hot>"},' +
  '{"account":"Alpha","amount_cents":7,"date":"2024-03-01","memo":"café\\n"},' +
  '{"account":"zeta","amount_cents":1250,"date":"2024-03-02","memo":""}],"version":1}';

const e = (date: string, account: string, amountCents: number, memo = ""): Entry => ({ date, account, amountCents, memo });

describe("R4/R7 parseExport", () => {
  it("R7 reads exactly what the Go service exports, entries in order", () => {
    const ledger = parseExport(EXPORTED);
    expect(ledger.version).toBe(1);
    expect(ledger.entries).toEqual([
      e("2024-02-29", "zeta", -1250, 'coffee & "beans" <hot>'),
      e("2024-03-01", "Alpha", 7, "café\n"),
      e("2024-03-02", "zeta", 1250),
    ]);
    expect(ledger.balances).toEqual({ Alpha: 7, zeta: 0 });
    expect(parseExport('{"version":1,"entries":[],"balances":{}}').entries).toEqual([]);
  });
  const reject = [
    "not json",
    '{"balances":{},"entries":[],"version":2}',
    '{"balances":{},"entries":[],"version":1,"extra":0}',
    '{"balances":{},"entries":[]}',
    '{"balances":{"a":2},"entries":[{"account":"a","amount_cents":1,"date":"2024-01-01","memo":""}],"version":1}',
    '{"balances":{"a":1,"b":0},"entries":[{"account":"a","amount_cents":1,"date":"2024-01-01","memo":""}],"version":1}',
    '{"balances":{"a":1},"entries":[{"account":"a","amount_cents":1,"date":"2024-02-30","memo":""}],"version":1}',
    '{"balances":{"a":1.5},"entries":[{"account":"a","amount_cents":1.5,"date":"2024-01-01","memo":""}],"version":1}',
    '{"balances":{" a":1},"entries":[{"account":" a","amount_cents":1,"date":"2024-01-01","memo":""}],"version":1}',
    '{"balances":{"a":1},"entries":[{"account":"a","amount_cents":1,"date":"2024-01-01"}],"version":1}',
    '{"balances":{"a":1},"entries":[{"account":"a","amount_cents":1,"date":"2024-01-01","memo":"","x":1}],"version":1}',
  ];
  for (const input of reject) {
    it(`R4 rejects ${input.slice(0, 40)}`, () => {
      expect(() => parseExport(input)).toThrowError(/^invalid ledger: /);
    });
  }
});

describe("R5 formatCents", () => {
  it("R5 formats USD and EUR", () => {
    expect(formatCents(123456, "USD")).toBe("$1,234.56");
    expect(formatCents(-123456, "USD")).toBe("-$1,234.56");
    expect(formatCents(5, "USD")).toBe("$0.05");
    expect(formatCents(0, "USD")).toBe("$0.00");
    expect(formatCents(100000000, "USD")).toBe("$1,000,000.00");
    expect(formatCents(123456, "EUR")).toBe("1.234,56 €");
    expect(formatCents(-123456, "EUR")).toBe("-1.234,56 €");
    expect(formatCents(5, "EUR")).toBe("0,05 €");
  });
  it("R5 rejects bad amounts and currencies", () => {
    expect(() => formatCents(1.5, "USD")).toThrow(RangeError);
    expect(() => formatCents(Number.MAX_SAFE_INTEGER + 1, "EUR")).toThrow(RangeError);
    expect(() => formatCents(1, "GBP" as never)).toThrow(RangeError);
  });
});

describe("R6 reconcile", () => {
  const service: Ledger = {
    version: 1,
    entries: [e("2024-01-02", "b", 5, "x"), e("2024-01-01", "b", 1), e("2024-01-01", "a", 3), e("2024-01-03", "c", 9)],
    balances: { a: 3, b: 6, c: 9 },
  };
  it("R6 finds missing, extra and changed entries in sorted order", () => {
    const local = [e("2024-01-03", "c", 8), e("2024-01-01", "a", 3), e("2023-12-31", "z", 1), e("2024-01-02", "a", 2)];
    const before = JSON.stringify([service, local]);
    const out = reconcile(service, local);
    expect(out.missing).toEqual([e("2024-01-01", "b", 1), e("2024-01-02", "b", 5, "x")]);
    expect(out.extra).toEqual([e("2023-12-31", "z", 1), e("2024-01-02", "a", 2)]);
    expect(out.changed).toEqual([{ service: e("2024-01-03", "c", 9), local: e("2024-01-03", "c", 8) }]);
    expect(JSON.stringify([service, local])).toBe(before);
  });
  it("R6 rejects duplicate keys on either side", () => {
    expect(() => reconcile(service, [e("2024-01-01", "a", 1), e("2024-01-01", "a", 2)])).toThrowError(/^duplicate key: /);
    const doubled = { ...service, entries: [...service.entries, e("2024-01-03", "c", 1)] };
    expect(() => reconcile(doubled, [])).toThrowError(/^duplicate key: /);
  });
});
