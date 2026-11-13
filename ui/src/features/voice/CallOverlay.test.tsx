import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { useCleaStore } from "@/protocol/store";
import type { VoiceState } from "@/protocol/types";
import { CallOverlay } from "./CallOverlay";
import { DevVoiceMockPanel } from "./DevVoiceMockPanel";

describe("CallOverlay", () => {
  beforeEach(() => {
    useCleaStore.setState({ callOverlayOpen: true, voiceState: "idle" });
  });

  it("renders all four voice_state events and resets on End Call", () => {
    const { container } = render(<CallOverlay />);

    const states: VoiceState[] = ["idle", "listening", "thinking", "speaking"];
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

  it("drives CallOverlay through all four states from DevVoiceMockPanel", () => {
    const { container } = render(
      <>
        <CallOverlay />
        <DevVoiceMockPanel />
      </>,
    );

    const states: VoiceState[] = ["listening", "thinking", "speaking", "idle"];
    for (const state of states) {
      fireEvent.click(screen.getByRole("button", { name: new RegExp(`^${state}$`, "i") }));
      expect(container.querySelector(`[data-voice-state="${state}"]`)).not.toBeNull();
      expect(useCleaStore.getState().voiceState).toBe(state);
    }
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
});
