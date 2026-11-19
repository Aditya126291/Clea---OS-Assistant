import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CleaClient, computeBackoffMs, type WebSocketLike } from "./client";

class MockSocket implements WebSocketLike {
  readyState = 0;
  sent: string[] = [];
  closed = false;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent<unknown>) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;

  constructor(
    public readonly url: string,
    public readonly protocols: string[],
  ) {}

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.closed = true;
    this.readyState = 3;
  }

  triggerOpen(): void {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }

  triggerMessage(payload: unknown): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(payload) }));
  }

  triggerClose(code = 1006): void {
    this.readyState = 3;
    this.onclose?.({ code } as CloseEvent);
  }
}

describe("CleaClient", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("authenticates via Sec-WebSocket-Protocol subprotocol and correlates requests", async () => {
    let created: MockSocket | null = null;
    const events: string[] = [];
    const onConnected = vi.fn();

    const client = new CleaClient({
      resolveConnection: async () => ({
        ok: true,
        info: { token: "secret-tok", wsUrl: "ws://127.0.0.1:8765" },
      }),
      createWebSocket: (url, protocols) => {
        created = new MockSocket(url, protocols);
        return created;
      },
      onConnected,
      onEvent: (ev) => events.push(ev.event ?? ""),
    });

    await client.connect();
    expect(created).not.toBeNull();
    expect(created!.url).toBe("ws://127.0.0.1:8765");
    expect(created!.protocols).toEqual(["clea.v1", "clea.token.secret-tok"]);

    created!.triggerOpen();
    expect(onConnected).toHaveBeenCalledTimes(1);

    const pingPromise = client.request("health.ping", {}, { id: "req-1" });
    expect(JSON.parse(created!.sent[0])).toMatchObject({
      frame_type: "request",
      id: "req-1",
      method: "health.ping",
    });

    created!.triggerMessage({
      frame_type: "event",
      event: "voice_state",
      data: { state: "listening" },
    });
    expect(events).toEqual(["voice_state"]);

    created!.triggerMessage({
      frame_type: "response",
      id: "req-1",
      ok: true,
      result: { status: "ok" },
    });
    await expect(pingPromise).resolves.toEqual({ status: "ok" });

    client.dispose();
  });

  it("marks daemon_offline and schedules auto-reconnect with bounded backoff", async () => {
    const disconnects: Array<{ reason: string; attempt: number }> = [];
    let calls = 0;

    const client = new CleaClient({
      resolveConnection: async () => {
        calls += 1;
        return { ok: false, reason: "daemon_offline" };
      },
      onDisconnected: (reason, attempt) => disconnects.push({ reason, attempt }),
    });

    await client.connect();
    expect(calls).toBe(1);
    expect(disconnects).toEqual([{ reason: "daemon_offline", attempt: 1 }]);

    await vi.advanceTimersByTimeAsync(2000);
    expect(calls).toBe(2);
    expect(disconnects[1]).toEqual({ reason: "daemon_offline", attempt: 2 });

    expect(computeBackoffMs(0, () => 0)).toBe(1000);
    expect(computeBackoffMs(10, () => 0)).toBe(15000);
    expect(computeBackoffMs(10, () => 1)).toBe(18750);

    client.dispose();
  });

  it("re-reads a new session token when reconnecting after daemon restart", async () => {
    let phase: "offline" | "first" | "second" = "offline";
    const sockets: MockSocket[] = [];
    const onConnected = vi.fn();

    const client = new CleaClient({
      resolveConnection: async () => {
        if (phase === "offline") return { ok: false, reason: "daemon_offline" };
        const token = phase === "first" ? "tok-first" : "tok-second";
        return { ok: true, info: { token, wsUrl: "ws://127.0.0.1:8765" } };
      },
      createWebSocket: (url, protocols) => {
        const sock = new MockSocket(url, protocols);
        sockets.push(sock);
        return sock;
      },
      onConnected,
    });

    await client.connect();
    expect(sockets).toHaveLength(0);

    // Daemon starts later
    phase = "first";
    await vi.advanceTimersByTimeAsync(1500);
    expect(sockets).toHaveLength(1);
    expect(sockets[0].protocols).toEqual(["clea.v1", "clea.token.tok-first"]);
    sockets[0].triggerOpen();
    expect(onConnected).toHaveBeenCalledTimes(1);

    // Daemon restarts with a brand new token
    phase = "second";
    sockets[0].triggerClose(1006);
    await vi.advanceTimersByTimeAsync(1500);
    expect(sockets).toHaveLength(2);
    expect(sockets[1].protocols).toEqual(["clea.v1", "clea.token.tok-second"]);
    sockets[1].triggerOpen();
    expect(onConnected).toHaveBeenCalledTimes(2);

    client.dispose();
  });

  it("transmits chat.send with ChatImageAttachment over WebSocket", async () => {
    let created: MockSocket | null = null;
    const client = new CleaClient({
      resolveConnection: async () => ({
        ok: true,
        info: { token: "img-tok", wsUrl: "ws://127.0.0.1:8767" },
      }),
      createWebSocket: (url, protocols) => {
        created = new MockSocket(url, protocols);
        return created;
      },
    });

    await client.connect();
    created!.triggerOpen();

    const sendPromise = client.request(
      "chat.send",
      {
        text: "Describe this screenshot",
        image: {
          data_base64: "iVBORw0KGgo=",
          mime_type: "image/png",
          name: "shot.png",
          width: 640,
          height: 480,
        },
      },
      { id: "req-img-1", sessionId: "sess-1" },
    );

    const frame = JSON.parse(created!.sent[0]);
    expect(frame).toMatchObject({
      frame_type: "request",
      id: "req-img-1",
      session_id: "sess-1",
      method: "chat.send",
      params: {
        text: "Describe this screenshot",
        image: {
          data_base64: "iVBORw0KGgo=",
          mime_type: "image/png",
          name: "shot.png",
          width: 640,
          height: 480,
        },
      },
    });

    created!.triggerMessage({
      frame_type: "response",
      id: "req-img-1",
      ok: true,
      result: { text: "Looks like a screenshot." },
    });

    await expect(sendPromise).resolves.toEqual({ text: "Looks like a screenshot." });
    client.dispose();
  });
});
