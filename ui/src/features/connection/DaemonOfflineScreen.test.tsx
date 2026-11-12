import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonOfflineScreen } from "./DaemonOfflineScreen";

describe("DaemonOfflineScreen", () => {
  afterEach(() => {
    delete window.cleaDesktop;
  });

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
    expect(screen.queryByRole("button", { name: /Start daemon/i })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Retry now/i }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("offers Start daemon in Electron and triggers retry", async () => {
    const onRetry = vi.fn();
    const startDaemon = vi.fn().mockResolvedValue({ ok: true });
    window.cleaDesktop = {
      isElectron: true,
      getConnection: async () => null,
      startDaemon,
    };

    render(
      <DaemonOfflineScreen
        connectionState="daemon_offline"
        reconnectAttempt={0}
        onRetry={onRetry}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Start daemon/i }));
    await waitFor(() => expect(startDaemon).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onRetry).toHaveBeenCalled());
  });
});
