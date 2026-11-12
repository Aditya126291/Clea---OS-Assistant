import { resolveConnectionInfo, type TokenLookupResult } from "./connection";
import type {
  CleaEvent,
  CleaMethod,
  MethodParamsMap,
  MethodResultMap,
  ResponseFrame,
} from "./types";

const PROTOCOL_NAME = "clea.v1";
const TOKEN_SUBPROTOCOL_PREFIX = "clea.token.";

const BASE_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 15000;
const DEFAULT_REQUEST_TIMEOUT_MS = 30000;

export interface WebSocketLike {
  readonly readyState: number;
  send: (data: string) => void;
  close: (code?: number, reason?: string) => void;
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent<unknown>) => void) | null;
  onerror: ((ev: Event) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
}

export interface CleaClientOptions {
  resolveConnection?: () => Promise<TokenLookupResult>;
  createWebSocket?: (url: string, protocols: string[]) => WebSocketLike;
  onConnected?: () => void;
  onDisconnected?: (
    reason: "daemon_offline" | "auth_error" | "reconnecting",
    attempt: number,
  ) => void;
  onEvent?: (event: CleaEvent) => void;
}

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout> | null;
}

function makeId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID().replace(/-/g, "");
  }
  return `${Date.now().toString(16)}${Math.random().toString(16).slice(2, 10)}`;
}

export function computeBackoffMs(attempt: number, random = Math.random): number {
  const exp = Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * Math.pow(2, Math.max(0, attempt)));
  const jitter = exp * 0.25 * random();
  return Math.round(exp + jitter);
}

export class CleaClient {
  private readonly resolveConnection: () => Promise<TokenLookupResult>;
  private readonly createWebSocket: (url: string, protocols: string[]) => WebSocketLike;
  private readonly onConnected?: () => void;
  private readonly onDisconnected?: (
    reason: "daemon_offline" | "auth_error" | "reconnecting",
    attempt: number,
  ) => void;
  private readonly onEvent?: (event: CleaEvent) => void;

  private ws: WebSocketLike | null = null;
  private pending = new Map<string, PendingRequest>();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  private disposed = false;
  private everConnected = false;

  constructor(options: CleaClientOptions = {}) {
    this.resolveConnection = options.resolveConnection ?? resolveConnectionInfo;
    this.createWebSocket =
      options.createWebSocket ?? ((url, protocols) => new WebSocket(url, protocols));
    this.onConnected = options.onConnected;
    this.onDisconnected = options.onDisconnected;
    this.onEvent = options.onEvent;
  }

  get isConnected(): boolean {
    return this.ws !== null && this.ws.readyState === 1;
  }

  async connect(): Promise<void> {
    if (this.disposed) return;
    this.clearReconnectTimer();

    const lookup = await this.resolveConnection();
    if (this.disposed) return;

    if (!lookup.ok) {
      this.attempt += 1;
      this.onDisconnected?.("daemon_offline", this.attempt);
      this.scheduleReconnect();
      return;
    }

    const { token, wsUrl } = lookup.info;
    this.openSocket(wsUrl, token);
  }

  retryNow(): void {
    if (this.disposed) return;
    this.clearReconnectTimer();
    if (this.ws) {
      const old = this.ws;
      this.ws = null;
      old.onopen = null;
      old.onmessage = null;
      old.onerror = null;
      old.onclose = null;
      try {
        old.close();
      } catch {
        // ignore
      }
    }
    this.attempt = 0;
    void this.connect();
  }

  dispose(): void {
    this.disposed = true;
    this.clearReconnectTimer();
    this.rejectAllPending(new Error("Client disposed"));
    if (this.ws) {
      const socket = this.ws;
      this.ws = null;
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      try {
        socket.close();
      } catch {
        // ignore
      }
    }
  }

  request<M extends CleaMethod>(
    method: M,
    params: MethodParamsMap[M],
    options: { id?: string; sessionId?: string | null; timeoutMs?: number } = {},
  ): Promise<MethodResultMap[M]> {
    if (!this.ws || this.ws.readyState !== 1) {
      return Promise.reject(new Error("Not connected to Clea daemon"));
    }
    const id = options.id ?? makeId();
    const timeoutMs = options.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;

    return new Promise<MethodResultMap[M]>((resolve, reject) => {
      const timer =
        timeoutMs > 0
          ? setTimeout(() => {
              this.pending.delete(id);
              reject(new Error(`Request ${method} timed out`));
            }, timeoutMs)
          : null;

      this.pending.set(id, {
        resolve: resolve as (v: unknown) => void,
        reject,
        timer,
      });

      const payload = {
        frame_type: "request",
        id,
        session_id: options.sessionId ?? null,
        method,
        params,
      };

      try {
        this.ws!.send(JSON.stringify(payload));
      } catch (err) {
        if (timer) clearTimeout(timer);
        this.pending.delete(id);
        reject(err instanceof Error ? err : new Error(String(err)));
      }
    });
  }

  private openSocket(wsUrl: string, token: string): void {
    let opened = false;
    const socket = this.createWebSocket(wsUrl, [
      PROTOCOL_NAME,
      `${TOKEN_SUBPROTOCOL_PREFIX}${token}`,
    ]);
    this.ws = socket;

    socket.onopen = () => {
      if (this.disposed || this.ws !== socket) return;
      opened = true;
      this.everConnected = true;
      this.attempt = 0;
      this.onConnected?.();
    };

    socket.onmessage = (ev) => {
      if (this.disposed || this.ws !== socket) return;
      if (typeof ev.data !== "string") return;
      let parsed: ResponseFrame | CleaEvent;
      try {
        parsed = JSON.parse(ev.data) as ResponseFrame | CleaEvent;
      } catch {
        return;
      }

      if (parsed.frame_type === "response") {
        const pending = this.pending.get(parsed.id);
        if (!pending) return;
        this.pending.delete(parsed.id);
        if (pending.timer) clearTimeout(pending.timer);
        if (parsed.ok) {
          pending.resolve(parsed.result ?? {});
        } else {
          pending.reject(new Error(parsed.error || "Daemon request failed"));
        }
      } else if (parsed.frame_type === "event") {
        this.onEvent?.(parsed);
      }
    };

    socket.onerror = () => {
      // Handled by onclose
    };

    socket.onclose = (ev) => {
      if (this.ws !== socket) return;
      this.ws = null;
      this.rejectAllPending(new Error("WebSocket connection closed"));
      if (this.disposed) return;

      this.attempt += 1;
      const code = ev?.code ?? 1006;
      const reason =
        !opened && !this.everConnected
          ? "daemon_offline"
          : code === 1008
            ? "auth_error"
            : "reconnecting";
      this.onDisconnected?.(reason, this.attempt);
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    if (this.disposed) return;
    this.clearReconnectTimer();
    const delay = computeBackoffMs(this.attempt - 1);
    this.reconnectTimer = setTimeout(() => {
      void this.connect();
    }, delay);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private rejectAllPending(err: Error): void {
    for (const [id, entry] of this.pending.entries()) {
      if (entry.timer) clearTimeout(entry.timer);
      entry.reject(err);
      this.pending.delete(id);
    }
  }
}
