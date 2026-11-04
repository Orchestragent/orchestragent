// Hidden acceptance: R1, R11 and R12.
import { createServer, type AddressInfo, type Server, type Socket } from "node:net";
import { afterEach, describe, expect, it } from "vitest";

import { ConnectionError, MkvClient, QuorumError, ServerError } from "../src/client.js";
import { ProtocolError, decodeToken, encodeToken, parseResponse } from "../src/protocol.js";

describe("acceptance: tokens", () => {
  const cases: [string, string][] = [
    ["abc", "abc"],
    ["", "="],
    ["a b", "a%20b"],
    ["=", "%3D"],
    ["%", "%25"],
    ["é", "%C3%A9"],
    ["AZaz09._~-", "AZaz09._~-"],
    ["line1\nline2", "line1%0Aline2"],
    ["ключ", "%D0%BA%D0%BB%D1%8E%D1%87"],
    ["😀", "%F0%9F%98%80"],
    ["a/b:c", "a%2Fb%3Ac"],
  ];
  it.each(cases)("encodes %j", (plain, token) => {
    expect(encodeToken(plain)).toBe(token);
    expect(decodeToken(token)).toBe(plain);
  });
  it("decodes lowercase hex", () => {
    expect(decodeToken("%c3%a9")).toBe("é");
  });
  it.each(["%G1", "%4", "abc%", "%FF", "%C3"])("rejects malformed token %j", (token) => {
    expect(() => decodeToken(token)).toThrow(ProtocolError);
  });
});

describe("acceptance: responses", () => {
  it("parses every response kind", () => {
    expect(parseResponse("OK 7\n")).toEqual({ kind: "ok", version: 7 });
    expect(parseResponse("OK 0")).toEqual({ kind: "ok", version: 0 });
    expect(parseResponse("VALUE 3 a%20b\r\n")).toEqual({ kind: "value", version: 3, value: "a b" });
    expect(parseResponse("VALUE 3 =")).toEqual({ kind: "value", version: 3, value: "" });
    expect(parseResponse("NOTFOUND\n")).toEqual({ kind: "notfound" });
    expect(parseResponse("KEYS 2 a b%2Fc")).toEqual({ kind: "keys", keys: ["a", "b/c"] });
    expect(parseResponse("KEYS 0")).toEqual({ kind: "keys", keys: [] });
    expect(parseResponse("STATS router=r1 puts=3")).toEqual({
      kind: "stats",
      fields: { router: "r1", puts: "3" },
    });
    expect(parseResponse("ERR NOQUORUM only%201%20replica")).toEqual({
      kind: "error",
      code: "NOQUORUM",
      message: "only 1 replica",
    });
  });
  it.each(["BOGUS", "OK", "OK x", "OK -1", "OK 01", "VALUE 3", "KEYS 2 a", "KEYS 1 a b", "NOTFOUND x", ""])(
    "rejects %j",
    (line) => {
      expect(() => parseResponse(line)).toThrow(ProtocolError);
    },
  );
});

type Script = (line: string, socket: Socket) => string | null; // null: drop the connection

const servers: Server[] = [];
afterEach(async () => {
  await Promise.all(servers.splice(0).map((s) => new Promise((r) => s.close(() => r(null)))));
});

async function fakeServer(script: Script): Promise<{ port: number; lines: string[]; connections: () => number }> {
  const lines: string[] = [];
  let connections = 0;
  const server = createServer((socket) => {
    connections += 1;
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      let index;
      while ((index = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, index + 1);
        buffer = buffer.slice(index + 1);
        lines.push(line);
        const reply = script(line, socket);
        if (reply === null) {
          socket.destroy();
          return;
        }
        socket.write(reply);
      }
    });
    socket.on("error", () => {});
  });
  servers.push(server);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(null)));
  return { port: (server.address() as AddressInfo).port, lines, connections: () => connections };
}

describe("acceptance: client", () => {
  it("sends encoded requests and maps responses", async () => {
    const replies: Record<string, string> = {
      "PUT k%201 v%3D1\n": "OK 4\n",
      "GET k%201\n": "VALUE 4 v%3D1\n",
      "GET missing\n": "NOTFOUND\n",
      "DEL k%201\n": "OK 5\n",
      "SCAN user%3A 10\n": "KEYS 2 user%3A1 user%3A2\n",
      "PUT empty =\n": "OK 1\n",
    };
    const server = await fakeServer((line) => replies[line] ?? "ERR BADREQ unexpected\n");
    const client = new MkvClient({ host: "127.0.0.1", port: server.port });
    try {
      expect(await client.put("k 1", "v=1")).toBe(4);
      expect(await client.get("k 1")).toBe("v=1");
      expect(await client.get("missing")).toBeNull();
      expect(await client.del("k 1")).toBe(5);
      expect(await client.scan("user:", 10)).toEqual(["user:1", "user:2"]);
      expect(await client.put("empty", "")).toBe(1);
    } finally {
      await client.close();
    }
    expect(server.lines).toEqual(Object.keys(replies));
  });

  it("retries NOQUORUM with exponential backoff", async () => {
    let calls = 0;
    const server = await fakeServer(() => (++calls <= 2 ? "ERR NOQUORUM busy\n" : "OK 9\n"));
    const client = new MkvClient({ host: "127.0.0.1", port: server.port, baseDelayMs: 40 });
    const started = Date.now();
    try {
      expect(await client.put("k", "v")).toBe(9);
    } finally {
      await client.close();
    }
    expect(calls).toBe(3);
    expect(Date.now() - started).toBeGreaterThanOrEqual(110); // 40 + 80 ms of backoff
  });

  it("gives up with QuorumError after the configured retries", async () => {
    const server = await fakeServer(() => "ERR NOQUORUM down\n");
    const client = new MkvClient({ host: "127.0.0.1", port: server.port, retries: 2, baseDelayMs: 1 });
    try {
      await expect(client.get("k")).rejects.toBeInstanceOf(QuorumError);
    } finally {
      await client.close();
    }
    expect(server.lines.length).toBe(3);
  });

  it("does not retry other server errors", async () => {
    const server = await fakeServer(() => "ERR BADREQ bad%20key\n");
    const client = new MkvClient({ host: "127.0.0.1", port: server.port, baseDelayMs: 1 });
    try {
      const error = await client.put("k", "v").catch((e: unknown) => e);
      expect(error).toBeInstanceOf(ServerError);
      expect((error as ServerError).code).toBe("BADREQ");
    } finally {
      await client.close();
    }
    expect(server.lines.length).toBe(1);
  });

  it("reconnects after a dropped connection", async () => {
    let calls = 0;
    const server = await fakeServer(() => (++calls === 1 ? null : "OK 2\n"));
    const client = new MkvClient({ host: "127.0.0.1", port: server.port, baseDelayMs: 1 });
    try {
      expect(await client.put("k", "v")).toBe(2);
    } finally {
      await client.close();
    }
    expect(server.connections()).toBe(2);
  });

  it("fails with ConnectionError when nothing listens", async () => {
    const probe = await fakeServer(() => "OK 1\n");
    const port = probe.port;
    await new Promise((r) => servers.pop()!.close(() => r(null)));
    const client = new MkvClient({ host: "127.0.0.1", port, retries: 1, baseDelayMs: 1, timeoutMs: 500 });
    try {
      await expect(client.get("k")).rejects.toBeInstanceOf(ConnectionError);
    } finally {
      await client.close();
    }
  });
});
