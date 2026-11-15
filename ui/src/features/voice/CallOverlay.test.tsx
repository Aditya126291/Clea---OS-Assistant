import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CleaAvatar, sanitizeSvg } from "@/components/ui/clea-avatar";
import { CallOverlay as ComponentCallOverlay } from "@/components/voice/CallOverlay";
import { useCleaStore } from "@/protocol/store";
import type { VoiceState } from "@/protocol/types";
import { CallOverlay } from "./CallOverlay";
import { DevVoiceMockPanel } from "./DevVoiceMockPanel";

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
      '<svg/onload="alert(1)" viewBox="0 0 100 100" width="100" height="100">',
      "<style>@keyframes glitch { 0% { opacity: 1; } }</style>",
      '<filter id="fR"><feColorMatrix type="matrix"/></filter>',
      "<script>window.pwned = true;</script >",
      "<scr<script></script>ipt>window.nested = true;</script>",
      '<circle id="c"onclick="evil()" onmouseover=\'evil2()\' cx="50" cy="50" r="40" />',
      '<use href="javascript:alert(2)" xlink:href="#art" />',
      '<use href="&#106;avascript:alert(3)" xlink:href="java&#x09;script:alert(4)" />',
      '<set attributeName="onload" to="alert(5)" />',
      '<animate attributeName="xlink:href" to="javascript:alert(6)" />',
      "</svg>",
    ].join("");

    const cleaned = sanitizeSvg(untrustedSvg);
    expect(cleaned).not.toContain("<script");
    expect(cleaned).not.toContain("window.pwned");
    expect(cleaned).not.toContain("window.nested");
    expect(cleaned).not.toContain("onload=");
    expect(cleaned).not.toContain("onclick=");
    expect(cleaned).not.toContain("onmouseover=");
    expect(cleaned).not.toContain("javascript:");
    expect(cleaned).not.toContain("&#106;avascript:");
    expect(cleaned).not.toContain("alert(");
    expect(cleaned).toContain("@keyframes glitch");
    expect(cleaned).toContain('<filter id="fR">');
    expect(cleaned).toContain('width="100%" height="100%"');

    const { container } = render(<CleaAvatar svgMarkup={untrustedSvg} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("set")).toBeNull();
    expect(container.querySelector("animate")).toBeNull();
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
});
