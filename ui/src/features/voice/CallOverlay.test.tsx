import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { useCleaStore } from "@/protocol/store";
import type { VoiceState } from "@/protocol/types";
import { CallOverlay } from "./CallOverlay";

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
});
