import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DaemonOfflineScreen } from "./DaemonOfflineScreen";

describe("DaemonOfflineScreen", () => {
  it("shows clea daemon instructions and triggers retry", () => {
    const onRetry = vi.fn();
    render(
      <DaemonOfflineScreen
        connectionState="daemon_offline"
        reconnectAttempt={2}
        onRetry={onRetry}
      />,
    );

    expect(screen.getByText(/Clea daemon is not running/i)).toBeInTheDocument();
    expect(screen.getByText(/uv run clea daemon/i)).toBeInTheDocument();
    expect(screen.getByText(/attempt 2/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Retry now/i }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
