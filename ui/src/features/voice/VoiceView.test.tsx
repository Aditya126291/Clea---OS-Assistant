import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useCleaStore } from "@/protocol/store";
import { VoiceView } from "./VoiceView";

describe("VoiceView", () => {
  beforeEach(() => {
    useCleaStore.setState({
      callState: "idle",
      voiceState: "idle",
      voiceError: null,
      voiceHistoryMessages: [],
      voiceHistoryLoading: false,
    });
  });

  it("renders empty companion stream state when no messages exist", () => {
    const loadSpy = vi.fn().mockResolvedValue(undefined);
    useCleaStore.setState({ loadVoiceHistory: loadSpy });

    render(<VoiceView />);
    expect(loadSpy).toHaveBeenCalled();
    expect(screen.getByText("Voice Companion")).toBeInTheDocument();
    expect(screen.getByText("Idle")).toBeInTheDocument();
    expect(screen.getByText("No voice companion turns yet")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Start Call/i })).toHaveLength(2);
  });

  it("renders loading indicator when transcript is loading", () => {
    useCleaStore.setState({ voiceHistoryLoading: true, voiceHistoryMessages: [] });
    render(<VoiceView />);
    expect(screen.getByText("Loading Voice Companion transcript...")).toBeInTheDocument();
  });

  it("renders call state badges and toggles start/end call actions", () => {
    const startSpy = vi.fn().mockResolvedValue(undefined);
    const endSpy = vi.fn().mockResolvedValue(undefined);
    useCleaStore.setState({ startVoiceCall: startSpy, endVoiceCall: endSpy });

    const { rerender } = render(<VoiceView />);

    // Idle state -> Start Call button
    const startBtn = screen.getAllByRole("button", { name: /Start Call/i })[0];
    fireEvent.click(startBtn);
    expect(startSpy).toHaveBeenCalledTimes(1);

    // Active state speaking -> End Call button and speaking badge
    act(() => {
      useCleaStore.setState({ callState: "active", voiceState: "speaking" });
    });
    rerender(<VoiceView />);

    expect(screen.getByText("speaking")).toBeInTheDocument();
    const endBtn = screen.getByRole("button", { name: /End Call/i });
    fireEvent.click(endBtn);
    expect(endSpy).toHaveBeenCalledTimes(1);

    // Error state badge
    act(() => {
      useCleaStore.setState({ callState: "idle", voiceState: "error", voiceError: "Microphone lost" });
    });
    rerender(<VoiceView />);

    expect(screen.getByText("Error")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Voice error: Microphone lost");
  });

  it("renders chronological turns with speaker headers and call boundary dividers", () => {
    useCleaStore.setState({
      voiceHistoryMessages: [
        {
          role: "user",
          text: "First turn in call 1",
          call_id: "call-1111-aaaa",
          timestamp: "2026-11-14T10:00:00Z",
        },
        {
          role: "assistant",
          text: "Clea response in call 1",
          call_id: "call-1111-aaaa",
          timestamp: "2026-11-14T10:00:05Z",
        },
        {
          role: "user",
          text: "Second turn in call 2",
          call_id: "call-2222-bbbb",
          timestamp: "2026-11-14T10:05:00Z",
        },
      ],
    });

    render(<VoiceView />);

    expect(screen.getByText("First turn in call 1")).toBeInTheDocument();
    expect(screen.getByText("Clea response in call 1")).toBeInTheDocument();
    expect(screen.getByText("Second turn in call 2")).toBeInTheDocument();

    // Call dividers
    expect(screen.getByText(/Voice Call · call-111/i)).toBeInTheDocument();
    expect(screen.getByText(/Voice Call · call-222/i)).toBeInTheDocument();
  });
});
