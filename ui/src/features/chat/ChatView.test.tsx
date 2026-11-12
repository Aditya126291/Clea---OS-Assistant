import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useCleaStore } from "@/protocol/store";
import { ChatView } from "./ChatView";

describe("ChatView", () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
    useCleaStore.setState({
      connectionState: "connected",
      sessionId: "sess1234",
      messages: [],
      sending: false,
      selectedConversation: null,
    });
  });

  it("renders streaming tokens, tool call cards, and approval cards", () => {
    useCleaStore.setState({
      messages: [
        {
          id: "u1",
          role: "user",
          content: "Check time",
          streaming: false,
          toolCalls: [],
          approvals: [],
        },
        {
          id: "req-1",
          role: "assistant",
          content: "",
          streaming: true,
          toolCalls: [],
          approvals: [],
        },
      ],
    });

    render(<ChatView />);

    act(() => {
      useCleaStore.getState().applyEvent({
        frame_type: "event",
        request_id: "req-1",
        event: "tool_call_started",
        data: {
          call_id: "c1",
          name: "get_system_info",
          arguments: {},
          risk_level: "read_only",
        },
      });
      useCleaStore.getState().applyEvent({
        frame_type: "event",
        request_id: "req-1",
        event: "tool_call_finished",
        data: {
          call_id: "c1",
          name: "get_system_info",
          is_error: false,
          output: '{"local_time":"17:30"}',
          duration_ms: 8,
        },
      });
      useCleaStore.getState().applyEvent({
        frame_type: "event",
        request_id: "req-1",
        event: "assistant_token",
        data: { text: "It is **17:30**." },
      });
    });

    expect(screen.getByText("get_system_info")).toBeInTheDocument();
    expect(screen.getByText("17:30")).toBeInTheDocument();
  });

  it("sends on Enter and keeps newline on Shift+Enter", () => {
    const sendSpy = vi.fn().mockResolvedValue(undefined);
    useCleaStore.setState({ sendMessage: sendSpy });

    render(<ChatView />);
    const input = screen.getByLabelText("Message Clea");

    fireEvent.change(input, { target: { value: "Line 1" } });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(sendSpy).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: "Enter", shiftKey: false });
    expect(sendSpy).toHaveBeenCalledWith("Line 1");
  });
});
