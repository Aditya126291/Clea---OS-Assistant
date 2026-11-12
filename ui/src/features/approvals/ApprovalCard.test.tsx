import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApprovalItem } from "@/protocol/types";
import { ApprovalCard } from "./ApprovalCard";

function makeApproval(overrides: Partial<ApprovalItem> = {}): ApprovalItem {
  return {
    approvalId: "appr-1",
    callId: "call-1",
    tool: "rename_file",
    arguments: { path: "notes.md" },
    riskLevel: "reversible",
    timeoutSeconds: 120,
    status: "pending",
    ...overrides,
  };
}

describe("ApprovalCard", () => {
  it("allows reversible actions in a single click and supports deny", () => {
    const onRespond = vi.fn();
    render(
      <ApprovalCard approval={makeApproval({ riskLevel: "reversible" })} onRespond={onRespond} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Allow" }));
    expect(onRespond).toHaveBeenCalledWith("appr-1", "allow");

    fireEvent.click(screen.getByRole("button", { name: "Deny" }));
    expect(onRespond).toHaveBeenCalledWith("appr-1", "deny");
  });

  it("requires an explicit second click for destructive actions", () => {
    const onRespond = vi.fn();
    render(
      <ApprovalCard
        approval={makeApproval({ tool: "delete_file", riskLevel: "destructive" })}
        onRespond={onRespond}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Allow" }));
    expect(onRespond).not.toHaveBeenCalled();

    const cancel = screen.getByRole("button", { name: "Cancel" });
    fireEvent.click(cancel);
    expect(onRespond).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Allow" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm Destructive Action" }));
    expect(onRespond).toHaveBeenCalledWith("appr-1", "allow");
  });
});
