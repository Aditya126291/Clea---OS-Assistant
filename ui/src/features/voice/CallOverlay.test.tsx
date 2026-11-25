import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CleaAvatar, sanitizeSvg } from "@/components/ui/clea-avatar";
import { CallOverlay as ComponentCallOverlay } from "@/components/voice/CallOverlay";
import { Sidebar } from "@/features/sidebar/Sidebar";
import { ThemeProvider } from "@/lib/theme";
import type { WebSocketLike } from "@/protocol/client";
import { useCleaStore } from "@/protocol/store";
import type { VoiceState } from "@/protocol/types";
import { CallOverlay } from "./CallOverlay";
import { DevVoiceMockPanel } from "./DevVoiceMockPanel";

class MockVoiceSocket implements WebSocketLike {
  readyState = 0;
  sent: Array<Record<string, unknown>> = [];
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent<unknown>) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;

  send(data: string): void {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }

  close(): void {
    this.readyState = 3;
  }

  triggerOpen(): void {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }

  triggerMessage(payload: unknown): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(payload) }));
  }
}

describe("CallOverlay", () => {
  beforeEach(() => {
    useCleaStore.setState({
      callOverlayOpen: true,
      voiceState: "idle",
      voiceError: null,
      tasks: [],
    });
  });

  it("renders all voice_state events and resets on End Call", () => {
    const { container } = render(<CallOverlay />);

    const states: VoiceState[] = ["idle", "listening", "thinking", "speaking", "reconnecting"];
    for (const state of states) {
      act(() => {
        useCleaStore.getState().applyEvent({
          frame_type: "event",
          event: "voice_state",
          data: { state },
        });
      });
      const orb = container.querySelector(`[data-voice-state="${state}"]`);
      expect(orb).not.toBeNull();
      expect(screen.getByText(new RegExp(`^${state}$`, "i"))).toBeInTheDocument();
    }

    fireEvent.click(screen.getByRole("button", { name: /End Call/i }));
    expect(useCleaStore.getState().callOverlayOpen).toBe(false);
    expect(useCleaStore.getState().voiceState).toBe("idle");
  });

  it("drives CallOverlay through states from DevVoiceMockPanel", () => {
    const { container } = render(
      <>
        <CallOverlay />
        <DevVoiceMockPanel />
      </>,
    );

    const states: VoiceState[] = ["listening", "thinking", "speaking", "reconnecting", "idle"];
    for (const state of states) {
      fireEvent.click(screen.getByRole("button", { name: new RegExp(`^${state}$`, "i") }));
      expect(container.querySelector(`[data-voice-state="${state}"]`)).not.toBeNull();
      expect(useCleaStore.getState().voiceState).toBe(state);
    }
  });

  it("renders non-alarming reconnecting status without flashing error screen", () => {
    render(<CallOverlay />);

    act(() => {
      useCleaStore.getState().applyEvent({
        frame_type: "event",
        event: "voice_state",
        data: { state: "reconnecting" },
      });
    });

    const store = useCleaStore.getState();
    expect(store.voiceState).toBe("reconnecting");
    expect(store.voiceError).toBeNull();
    expect(screen.getByText("Reconnecting")).toBeInTheDocument();
    expect(screen.getByText("Reconnecting session...")).toBeInTheDocument();
    expect(screen.queryByText("Error")).toBeNull();
  });

  it("suppresses error banner and displays reconnecting status when voiceState is reconnecting", () => {
    useCleaStore.setState({ voiceState: "reconnecting", voiceError: "Transient network hiccup" });
    render(<CallOverlay />);

    expect(screen.getByText("Reconnecting")).toBeInTheDocument();
    expect(screen.getByText("Reconnecting session...")).toBeInTheDocument();
    expect(screen.queryByText("Transient network hiccup")).toBeNull();
    expect(screen.queryByText("Error")).toBeNull();
  });

  it("can be rendered through components/voice/CallOverlay alias path", () => {
    useCleaStore.setState({ voiceState: "reconnecting" });
    render(<ComponentCallOverlay />);
    expect(screen.getByText("Reconnecting")).toBeInTheDocument();
  });

  it("tracks call_started, transcript, screen_capture_state, usage, and call_ended in store", () => {
    const store = useCleaStore.getState();

    act(() => {
      store.applyEvent({
        frame_type: "event",
        event: "call_started",
        data: { call_id: "call-42", call_state: "active" },
      });
      store.applyEvent({
        frame_type: "event",
        event: "transcript",
        data: { role: "user", text: "Hello Clea", final: true, call_id: "call-42" },
      });
      store.applyEvent({
        frame_type: "event",
        event: "screen_capture_state",
        data: { active: true, target: "active_window", call_id: "call-42" },
      });
      store.applyEvent({
        frame_type: "event",
        event: "usage",
        data: { call_id: "call-42", input_tokens: 10, output_tokens: 5, total_tokens: 15 },
      });
    });

    expect(useCleaStore.getState().callState).toBe("active");
    expect(useCleaStore.getState().activeCallId).toBe("call-42");
    expect(useCleaStore.getState().voiceTranscripts).toHaveLength(1);
    expect(useCleaStore.getState().screenCaptureActive).toBe(true);
    expect(useCleaStore.getState().screenCaptureTarget).toBe("active_window");
    expect(useCleaStore.getState().lastCallUsage?.total_tokens).toBe(15);

    act(() => {
      store.applyEvent({
        frame_type: "event",
        event: "call_ended",
        data: { call_id: "call-42", call_state: "idle", reason: "user_hangup" },
      });
    });

    expect(useCleaStore.getState().callState).toBe("idle");
    expect(useCleaStore.getState().activeCallId).toBeNull();
    expect(useCleaStore.getState().screenCaptureActive).toBe(false);
    expect(useCleaStore.getState().callOverlayOpen).toBe(false);
  });

  it("surfaces clear error state in CallOverlay when daemon is offline", async () => {
    useCleaStore.setState({ callOverlayOpen: false, voiceState: "idle", voiceError: null });
    render(<CallOverlay />);

    await act(async () => {
      await useCleaStore.getState().startVoiceCall();
    });

    const store = useCleaStore.getState();
    expect(store.callOverlayOpen).toBe(true);
    expect(store.voiceState).toBe("error");
    expect(store.voiceError).toBe("Daemon not connected.");
    expect(screen.getByText("Daemon not connected.")).toBeInTheDocument();
    expect(screen.getByText("Error")).toBeInTheDocument();
  });

  it("surfaces voice process offline error in CallOverlay when voice process is not running", () => {
    useCleaStore.setState({ callOverlayOpen: false, voiceState: "idle", voiceError: null });
    render(<CallOverlay />);

    act(() => {
      useCleaStore.getState().applyEvent({
        frame_type: "event",
        event: "voice_state",
        data: { state: "error", error: "Voice process not running. Start it with 'clea voice'." },
      });
    });

    const store = useCleaStore.getState();
    expect(store.callOverlayOpen).toBe(true);
    expect(store.voiceState).toBe("error");
    expect(store.voiceError).toBe("Voice process not running. Start it with 'clea voice'.");
    expect(
      screen.getByText("Voice process not running. Start it with 'clea voice'."),
    ).toBeInTheDocument();
  });

  it("retains error state in CallOverlay when call_ended occurs after error until dismissed", () => {
    useCleaStore.setState({
      callOverlayOpen: true,
      voiceState: "error",
      voiceError: "Voice stream failed: timeout",
    });
    render(<CallOverlay />);

    act(() => {
      useCleaStore.getState().applyEvent({
        frame_type: "event",
        event: "call_ended",
        data: { call_id: "call-99", call_state: "idle", reason: "error" },
      });
    });

    // Overlay remains open in error state so the user is informed
    expect(useCleaStore.getState().callOverlayOpen).toBe(true);
    expect(useCleaStore.getState().voiceState).toBe("error");
    expect(screen.getByText("Voice stream failed: timeout")).toBeInTheDocument();

    // User dismisses with End Call
    fireEvent.click(screen.getByRole("button", { name: /End Call/i }));
    expect(useCleaStore.getState().callOverlayOpen).toBe(false);
    expect(useCleaStore.getState().voiceState).toBe("idle");
    expect(useCleaStore.getState().voiceError).toBeNull();
  });

  it("renders inline animated SVG avatar with preserved <style> keyframes and <filter> glitch effects and state-reactive outer ring", () => {
    useCleaStore.setState({ callOverlayOpen: true, voiceState: "listening", tasks: [] });
    const { container } = render(<CallOverlay />);

    const avatar = screen.getByTestId("voice-call-avatar");
    expect(avatar).toHaveClass("rounded-full", "overflow-hidden");
    expect(avatar).toHaveAttribute("data-avatar-src", "/clea-avatar.svg");

    const svg = avatar.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg?.querySelector("style")?.textContent).toContain("@keyframes");
    expect(svg?.querySelector("filter#fR")).not.toBeNull();
    expect(svg?.querySelector("filter#fC")).not.toBeNull();

    const ring = screen.getByTestId("voice-avatar-ring");
    expect(ring.className).toContain("ring-cyan-400");

    act(() => {
      useCleaStore.setState({ voiceState: "thinking" });
    });
    expect(ring.className).toContain("ring-violet-400");

    act(() => {
      useCleaStore.setState({ voiceState: "speaking" });
    });
    expect(ring.className).toContain("ring-amber-400");

    // Running tools while idle also activates the violet ring
    act(() => {
      useCleaStore.setState({
        voiceState: "idle",
        tasks: [
          {
            task_id: "tool-1",
            title: "Run search",
            status: "running",
            started_at: "2026-11-15T10:00:00Z",
            finished_at: null,
            detail: null,
          },
        ],
      });
    });
    expect(ring.className).toContain("ring-violet-400");
    expect(container.querySelector("aside")?.className).toContain("bottom-24");
  });

  it("sanitizes <script> tags, on* attributes, and javascript: URIs while keeping SVG filters and animations", () => {
    const untrustedSvg = [
      '<svg/onload="alert(1)" aria-label="a > b" viewBox="0 0 100 100" width="100" height="100">',
      '<style>@import "https://evil.example/x.css"; @keyframes glitch { 0% { opacity: 1; } } .bad { background: url(java/**/script:alert(7)); } .bad2 { background: url("\\6a avascript:alert(8)"); }</style>',
      '<filter id="fR"><feColorMatrix type="matrix"/></filter>',
      "<script>window.pwned = true;</script >",
      '<script data-x=">">window.attrGt = true;</script>',
      "<scr<script></script>ipt>window.nested = true;</script>",
      '<circle id="c"onclick="evil()" onmouseover=\'evil2()\' cx="50" cy="50" r="40" />',
      '<use href="javascript:alert(2)" xlink:href="#art" />',
      '<use href="&#106;avascript:alert(3)" xlink:href="java&#x09;script:alert(4)" />',
      '<set attributeName="onload" to="alert(5)">smil-body</set>',
      '<animate data-x=">" attributeName="href" to="javascript:alert(6)" />',
      '<animate attributeName="opacity" values="0;1;0" dur="3s" repeatCount="indefinite" />',
      "</svg>",
    ].join("");

    const cleaned = sanitizeSvg(untrustedSvg);
    expect(cleaned).not.toContain("<script");
    expect(cleaned).not.toContain("window.pwned");
    expect(cleaned).not.toContain("window.attrGt");
    expect(cleaned).not.toContain("window.nested");
    expect(cleaned).not.toContain("onload=");
    expect(cleaned).not.toContain("onclick=");
    expect(cleaned).not.toContain("onmouseover=");
    expect(cleaned).not.toContain("javascript:");
    expect(cleaned).not.toContain("&#106;avascript:");
    expect(cleaned).not.toContain("alert(");
    expect(cleaned).not.toContain("@import");
    expect(cleaned).not.toContain("smil-body");
    expect(cleaned).not.toContain("</set>");
    expect(cleaned).toContain("@keyframes glitch");
    expect(cleaned).toContain('<animate attributeName="opacity"');
    expect(cleaned).toContain('<filter id="fR">');
    expect(cleaned).toContain('width="100%" height="100%"');
    expect(cleaned).toContain('aria-label="a > b"');

    const { container } = render(<CleaAvatar svgMarkup={untrustedSvg} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("set")).toBeNull();
    expect(container.querySelectorAll("animate")).toHaveLength(1);
    expect(container.querySelector("animate")?.getAttribute("attributeName")).toBe("opacity");
    expect(container.querySelector("svg")?.getAttribute("onload")).toBeNull();
    expect(container.querySelector("circle")?.getAttribute("onclick")).toBeNull();
    expect(container.querySelector("filter#fR")).not.toBeNull();
  });

  it("sanitizes dynamically fetched /clea-avatar.svg markup when fetch is mocked", async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockResolvedValue({
      text: async () =>
        '<svg viewBox="0 0 24 24" onload="evil()"><script>evil()</script><circle id="fetched-node" cx="12" cy="12" r="10"/></svg>',
    });

    try {
      const { container } = render(<CleaAvatar />);
      await waitFor(() => {
        expect(container.querySelector("#fetched-node")).not.toBeNull();
      });
      expect(container.querySelector("script")).toBeNull();
      expect(container.querySelector("svg")?.getAttribute("onload")).toBeNull();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("opens the Companion Log via View Companion Log button", () => {
    useCleaStore.setState({ activeView: "chat", callOverlayOpen: true });
    render(<CallOverlay />);

    const btn = screen.getByRole("button", { name: /View Companion Log/i });
    expect(btn).toBeInTheDocument();
    fireEvent.click(btn);
    expect(useCleaStore.getState().activeView).toBe("voice");
  });

  describe("wake-word popup sync, hydration, and active-call attachment", () => {
    afterEach(() => {
      useCleaStore.getState().disconnect();
      useCleaStore.setState({
        connectionState: "idle",
        callOverlayOpen: false,
        callState: "idle",
        voiceState: "idle",
        voiceError: null,
        activeCallId: null,
        screenCaptureActive: false,
        screenCaptureTarget: null,
      });
    });

    it("auto-opens CallOverlay and highlights Voice button on incoming wake-word voice_state events and closes on idle", () => {
      useCleaStore.setState({
        connectionState: "connected",
        callOverlayOpen: false,
        callState: "idle",
        voiceState: "idle",
        voiceError: null,
        activeCallId: null,
      });

      render(
        <ThemeProvider>
          <Sidebar />
          <CallOverlay />
        </ThemeProvider>,
      );

      const voiceBtn = screen.getByRole("button", { name: /^Voice$/i });
      expect(voiceBtn).toHaveAttribute("aria-pressed", "false");
      expect(screen.queryByLabelText("Voice call overlay")).toBeNull();

      // Incoming wake-word state without any manual button click
      act(() => {
        useCleaStore.getState().applyEvent({
          frame_type: "event",
          event: "voice_state",
          data: { state: "thinking" },
        });
      });

      expect(screen.getByLabelText("Voice call overlay")).toBeInTheDocument();
      expect(screen.getByText("Thinking")).toBeInTheDocument();
      expect(voiceBtn).toHaveAttribute("aria-pressed", "true");
      expect(useCleaStore.getState().callOverlayOpen).toBe(true);
      expect(useCleaStore.getState().callState).toBe("active");

      act(() => {
        useCleaStore.getState().applyEvent({
          frame_type: "event",
          event: "voice_state",
          data: { state: "listening" },
        });
      });
      expect(screen.getByText("Listening")).toBeInTheDocument();

      act(() => {
        useCleaStore.getState().applyEvent({
          frame_type: "event",
          event: "voice_state",
          data: { state: "speaking" },
        });
      });
      expect(screen.getByText("Speaking")).toBeInTheDocument();

      // Call returns to idle -> popup closes and Voice button resets cleanly
      act(() => {
        useCleaStore.getState().applyEvent({
          frame_type: "event",
          event: "voice_state",
          data: { state: "idle" },
        });
      });

      expect(screen.queryByLabelText("Voice call overlay")).toBeNull();
      expect(voiceBtn).toHaveAttribute("aria-pressed", "false");
      expect(useCleaStore.getState().callOverlayOpen).toBe(false);
      expect(useCleaStore.getState().callState).toBe("idle");
    });

    it("hydrates and opens CallOverlay when connecting to daemon mid-call", async () => {
      let socket: MockVoiceSocket | null = null;
      useCleaStore.setState({
        callOverlayOpen: false,
        callState: "idle",
        voiceState: "idle",
        voiceError: null,
        activeCallId: null,
      });

      render(
        <ThemeProvider>
          <Sidebar />
          <CallOverlay />
        </ThemeProvider>,
      );

      act(() => {
        useCleaStore.getState().connect({
          resolveConnection: async () => ({
            ok: true,
            info: { token: "tok-1", wsUrl: "ws://127.0.0.1:8765" },
          }),
          createWebSocket: () => {
            socket = new MockVoiceSocket();
            return socket;
          },
        });
      });

      await waitFor(() => expect(socket).not.toBeNull());
      act(() => {
        socket!.triggerOpen();
      });

      await waitFor(() =>
        expect(socket!.sent.some((frame) => frame.method === "voice.status")).toBe(true),
      );
      const statusReq = socket!.sent.find((frame) => frame.method === "voice.status")!;

      act(() => {
        socket!.triggerMessage({
          frame_type: "response",
          id: statusReq.id,
          ok: true,
          result: {
            call_id: "wake-call-7",
            call_state: "active",
            voice_state: "listening",
            screen_capture_active: false,
            screen_capture_target: null,
          },
        });
      });

      await waitFor(() => {
        expect(screen.getByLabelText("Voice call overlay")).toBeInTheDocument();
      });
      expect(screen.getByText("Listening")).toBeInTheDocument();
      expect(useCleaStore.getState().activeCallId).toBe("wake-call-7");
      expect(useCleaStore.getState().callState).toBe("active");
      expect(screen.getByRole("button", { name: /^Voice$/i })).toHaveAttribute(
        "aria-pressed",
        "true",
      );

      // Clicking End Call sends voice.end_call with hydrated call_id
      fireEvent.click(screen.getByRole("button", { name: /End Call/i }));
      await waitFor(() =>
        expect(
          socket!.sent.some(
            (frame) =>
              frame.method === "voice.end_call" &&
              (frame.params as Record<string, unknown>).call_id === "wake-call-7",
          ),
        ).toBe(true),
      );
    });

    it("attaches cleanly instead of showing 'A voice call is already active.' when Voice button is clicked mid-call", async () => {
      let socket: MockVoiceSocket | null = null;
      useCleaStore.setState({
        callOverlayOpen: false,
        callState: "idle",
        voiceState: "idle",
        voiceError: null,
        activeCallId: null,
      });

      render(
        <ThemeProvider>
          <Sidebar />
          <CallOverlay />
        </ThemeProvider>,
      );

      act(() => {
        useCleaStore.getState().connect({
          resolveConnection: async () => ({
            ok: true,
            info: { token: "tok-2", wsUrl: "ws://127.0.0.1:8765" },
          }),
          createWebSocket: () => {
            socket = new MockVoiceSocket();
            return socket;
          },
        });
      });

      await waitFor(() => expect(socket).not.toBeNull());
      act(() => {
        socket!.triggerOpen();
      });

      // Click Voice while UI hasn't hydrated yet; simulate daemon replying with "already active" error
      fireEvent.click(screen.getByRole("button", { name: /^Voice$/i }));

      await waitFor(() =>
        expect(socket!.sent.some((frame) => frame.method === "voice.start_call")).toBe(true),
      );
      const startReq = socket!.sent.find((frame) => frame.method === "voice.start_call")!;
      const statusCountBefore = socket!.sent.filter((f) => f.method === "voice.status").length;

      act(() => {
        socket!.triggerMessage({
          frame_type: "response",
          id: startReq.id,
          ok: false,
          error: "A voice call is already active.",
        });
      });

      await waitFor(() => {
        const statusReqs = socket!.sent.filter((f) => f.method === "voice.status");
        expect(statusReqs.length).toBeGreaterThan(statusCountBefore);
      });
      const latestStatusReq = socket!.sent
        .filter((f) => f.method === "voice.status")
        .slice(-1)[0]!;

      act(() => {
        socket!.triggerMessage({
          frame_type: "response",
          id: latestStatusReq.id,
          ok: true,
          result: {
            call_id: "wake-99",
            call_state: "active",
            voice_state: "speaking",
            screen_capture_active: false,
            screen_capture_target: null,
          },
        });
      });

      await waitFor(() => {
        expect(screen.getByText("Speaking")).toBeInTheDocument();
      });
      expect(screen.queryByText(/already active/i)).toBeNull();
      expect(screen.queryByText("Error")).toBeNull();
      expect(useCleaStore.getState().voiceError).toBeNull();
      expect(useCleaStore.getState().activeCallId).toBe("wake-99");
      expect(useCleaStore.getState().callOverlayOpen).toBe(true);
    });
  });
});
