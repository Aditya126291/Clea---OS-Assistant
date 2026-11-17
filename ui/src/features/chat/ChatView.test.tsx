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

  it("handles pasting an image, displays dismissible badge, and submits with image note", async () => {
    class MockImage {
      onload: (() => void) | null = null;
      naturalWidth = 1200;
      naturalHeight = 800;
      set src(_val: string) {
        setTimeout(() => this.onload?.(), 0);
      }
    }
    vi.stubGlobal("Image", MockImage);
    window.URL.createObjectURL = vi.fn(() => "blob:mock-image");
    window.URL.revokeObjectURL = vi.fn();

    const sendSpy = vi.fn().mockResolvedValue(undefined);
    useCleaStore.setState({ sendMessage: sendSpy });

    render(<ChatView />);
    const input = screen.getByLabelText("Message Clea");

    const file = new File(["fake-png-data"], "test_shot.png", { type: "image/png" });
    const clipboardData = {
      items: [
        {
          type: "image/png",
          getAsFile: () => file,
        },
      ],
    };

    fireEvent.paste(input, { clipboardData });

    // Wait for the mock Image onload to trigger state update
    const badge = await screen.findByTestId("pasted-image-badge");
    expect(badge).toBeInTheDocument();
    expect(screen.getByText(/test_shot\.png · 1200×800/)).toBeInTheDocument();

    // Submit with text
    fireEvent.change(input, { target: { value: "Analyze this" } });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: false });

    expect(sendSpy).toHaveBeenCalledWith(
      "Analyze this\n[Attached image: test_shot.png · 1200×800]"
    );
    expect(screen.queryByTestId("pasted-image-badge")).not.toBeInTheDocument();
  });

  it("allows dismissing pasted image badge via close button", async () => {
    class MockImage {
      onload: (() => void) | null = null;
      naturalWidth = 640;
      naturalHeight = 480;
      set src(_val: string) {
        setTimeout(() => this.onload?.(), 0);
      }
    }
    vi.stubGlobal("Image", MockImage);
    window.URL.createObjectURL = vi.fn(() => "blob:mock-image-2");
    window.URL.revokeObjectURL = vi.fn();

    render(<ChatView />);
    const input = screen.getByLabelText("Message Clea");

    const file = new File(["data"], "image.png", { type: "image/png" });
    const clipboardData = {
      items: [
        {
          type: "image/png",
          getAsFile: () => file,
        },
      ],
    };

    fireEvent.paste(input, { clipboardData });

    const badge = await screen.findByTestId("pasted-image-badge");
    expect(badge).toBeInTheDocument();
    expect(screen.getByText(/clipboard_image\.png · 640×480/)).toBeInTheDocument();

    const removeBtn = screen.getByLabelText("Remove attached image");
    fireEvent.click(removeBtn);

    expect(screen.queryByTestId("pasted-image-badge")).not.toBeInTheDocument();
  });
});
