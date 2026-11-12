import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ToolCallCard } from "./ToolCallCard";

describe("ToolCallCard", () => {
  it("starts collapsed and expands to reveal arguments and output", () => {
    render(
      <ToolCallCard
        call={{
          callId: "c1",
          name: "get_system_info",
          arguments: { include_platform_details: false },
          riskLevel: "read_only",
          status: "done",
          output: '{"local_time":"2026-11-12T17:00:00"}',
          durationMs: 14,
        }}
      />,
    );

    expect(screen.getByText("get_system_info")).toBeInTheDocument();
    expect(screen.getByText("14 ms")).toBeInTheDocument();
    expect(screen.queryByText(/include_platform_details/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText(/include_platform_details/)).toBeInTheDocument();
    expect(screen.getByText(/local_time/)).toBeInTheDocument();
  });
});
