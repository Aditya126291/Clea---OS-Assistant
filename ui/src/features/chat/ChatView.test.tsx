import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
      "Analyze this\n[Attached image: test_shot.png · 1200×800]",
      {
        data_base64: btoa("fake-png-data"),
        mime_type: "image/png",
        name: "test_shot.png",
        width: 1200,
        height: 800,
      },
    );
    expect(screen.queryByTestId("pasted-image-badge")).not.toBeInTheDocument();
  });

  it("submits pasted image without draft text and transmits base64 attachment", async () => {
    class MockImage {
      onload: (() => void) | null = null;
      naturalWidth = 320;
      naturalHeight = 240;
      set src(_val: string) {
        setTimeout(() => this.onload?.(), 0);
      }
    }
    vi.stubGlobal("Image", MockImage);
    window.URL.createObjectURL = vi.fn(() => "blob:mock-image-only");
    window.URL.revokeObjectURL = vi.fn();

    const sendSpy = vi.fn().mockResolvedValue(undefined);
    useCleaStore.setState({ sendMessage: sendSpy });

    render(<ChatView />);
    const input = screen.getByLabelText("Message Clea");

    const file = new File(["img-only-bytes"], "image.png", { type: "image/png" });
    fireEvent.paste(input, {
      clipboardData: {
        items: [{ type: "image/png", getAsFile: () => file }],
      },
    });

    await screen.findByTestId("pasted-image-badge");
    fireEvent.click(screen.getByLabelText("Send message"));

    expect(sendSpy).toHaveBeenCalledWith(
      "Please inspect this image.\n[Attached image: clipboard_image.png · 320×240]",
      {
        data_base64: btoa("img-only-bytes"),
        mime_type: "image/png",
        name: "clipboard_image.png",
        width: 320,
        height: 240,
      },
    );
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

  it("awaits in-flight image paste when user presses Enter immediately after Ctrl+V", async () => {
    class SlowMockImage {
      onload: (() => void) | null = null;
      naturalWidth = 900;
      naturalHeight = 500;
      set src(_val: string) {
        setTimeout(() => this.onload?.(), 30);
      }
    }
    vi.stubGlobal("Image", SlowMockImage);
    window.URL.createObjectURL = vi.fn(() => "blob:slow-image");
    window.URL.revokeObjectURL = vi.fn();

    const sendSpy = vi.fn().mockResolvedValue(undefined);
    useCleaStore.setState({ sendMessage: sendSpy });

    render(<ChatView />);
    const input = screen.getByLabelText("Message Clea");

    fireEvent.change(input, { target: { value: "Explain this fast paste" } });
    const file = new File(["fast-paste-bytes"], "fast_shot.png", { type: "image/png" });
    fireEvent.paste(input, {
      clipboardData: {
        items: [{ type: "image/png", getAsFile: () => file }],
      },
    });

    // Press Enter immediately before FileReader + Image.onload finish
    fireEvent.keyDown(input, { key: "Enter", shiftKey: false });
    expect(sendSpy).not.toHaveBeenCalled();

    await waitFor(() => {
      expect(sendSpy).toHaveBeenCalledWith(
        "Explain this fast paste\n[Attached image: fast_shot.png · 900×500]",
        {
          data_base64: btoa("fast-paste-bytes"),
          mime_type: "image/png",
          name: "fast_shot.png",
          width: 900,
          height: 500,
        },
      );
    });
    expect(screen.queryByTestId("pasted-image-badge")).not.toBeInTheDocument();
  });

  it("rejects pasted images over 12 MiB and accepts extension-inferred WebP files", async () => {
    class MockImage {
      onload: (() => void) | null = null;
      naturalWidth = 512;
      naturalHeight = 256;
      set src(_val: string) {
        setTimeout(() => this.onload?.(), 0);
      }
    }
    vi.stubGlobal("Image", MockImage);
    window.URL.createObjectURL = vi.fn(() => "blob:ext-fallback");
    window.URL.revokeObjectURL = vi.fn();

    const sendSpy = vi.fn().mockResolvedValue(undefined);
    useCleaStore.setState({ sendMessage: sendSpy });

    render(<ChatView />);
    const input = screen.getByLabelText("Message Clea");

    // 1. Oversized image file (> 12 MiB) shows inline alert
    const hugeFile = new File(["x"], "huge.png", { type: "image/png" });
    Object.defineProperty(hugeFile, "size", { value: 13 * 1024 * 1024 });
    fireEvent.paste(input, {
      clipboardData: {
        items: [{ type: "image/png", getAsFile: () => hugeFile }],
      },
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("12 MiB limit");
    expect(screen.queryByTestId("pasted-image-badge")).not.toBeInTheDocument();

    // 2. Empty-MIME .webp file via clipboardData.files infers image/webp
    const webpFile = new File(["webp-bytes"], "diagram.webp", { type: "" });
    fireEvent.paste(input, {
      clipboardData: {
        files: [webpFile],
      },
    });

    await screen.findByTestId("pasted-image-badge");
    fireEvent.click(screen.getByLabelText("Send message"));

    await waitFor(() => {
      expect(sendSpy).toHaveBeenCalledWith(
        "Please inspect this image.\n[Attached image: diagram.webp · 512×256]",
        {
          data_base64: btoa("webp-bytes"),
          mime_type: "image/webp",
          name: "diagram.webp",
          width: 512,
          height: 256,
        },
      );
    });
  });
});
