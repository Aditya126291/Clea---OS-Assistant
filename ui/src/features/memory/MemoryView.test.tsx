import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryView } from "./MemoryView";
import { useCleaStore } from "@/protocol/store";

describe("MemoryView component", () => {
  beforeEach(() => {
    localStorage.clear();
    useCleaStore.setState({
      connectionState: "connected",
      activeView: "memory",
      memorySubView: "overview",
      memoryLongTerm: "",
      memoryDays: [],
      selectedMemoryDay: null,
      memoryDailyContent: "",
      memoryLoading: false,
      lastConsolidated: "2026-11-13T18:00:00Z",
      failedConsolidation: null,
      episodes: [],
      memoryFacts: [],
      memoryArchive: [],
      memoryStatus: null,
      transcripts: [],
      selectedTranscript: null,
      reconsolidating: {},
    });
  });

  it("renders view selector dropdown and header information", () => {
    render(<MemoryView />);

    expect(screen.getByRole("heading", { name: "Memory" })).toBeInTheDocument();
    expect(screen.getByText(/Last consolidated/i)).toBeInTheDocument();
    expect(screen.getByText("Durable facts")).toBeInTheDocument();
  });

  it("displays amber warning banner on consolidation failure with retry, details, and dismiss", async () => {
    const reconsolidate = vi.fn().mockResolvedValue(true);
    useCleaStore.setState({
      failedConsolidation: "Session 67727ff3: JSON validation failed: text Field required [path]",
      reconsolidate,
    });

    render(<MemoryView />);

    // Amber banner appears with plain language message
    const banner = screen.getByRole("alert");
    expect(banner).toBeInTheDocument();
    expect(screen.getByText(/Couldn't update memory from one conversation\./i)).toBeInTheDocument();

    // Raw red error is NOT rendered
    expect(screen.queryByText(/Consolidation warning:/i)).not.toBeInTheDocument();

    // Toggle Details
    const detailsBtn = screen.getByRole("button", { name: /Details/i });
    fireEvent.click(detailsBtn);
    expect(screen.getByText(/Session: 67727ff3/i)).toBeInTheDocument();
    expect(screen.getByText(/JSON validation failed/i)).toBeInTheDocument();

    // Click Retry
    const retryBtn = screen.getByRole("button", { name: /Retry/i });
    fireEvent.click(retryBtn);
    expect(reconsolidate).toHaveBeenCalledWith("67727ff3");

    // Dismiss warning
    const dismissBtn = screen.getByRole("button", { name: /Dismiss warning/i });
    fireEvent.click(dismissBtn);

    // Banner is gone after dismissal
    await waitFor(() => {
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });
  });

  it("renders Durable Facts with 6 fixed sections, badges, and search filtering", () => {
    useCleaStore.setState({
      memoryFacts: [
        {
          name: "Environment",
          entries: [
            {
              id: "env-1",
              date: "2026-11-13",
              source: "stated",
              text: "Desktop machine is lenovoloq.",
            },
          ],
        },
        {
          name: "Preferences",
          entries: [
            {
              id: "pref-1",
              date: "2026-11-13",
              source: "confirmed",
              text: "Prefers Python over Node.",
            },
          ],
        },
      ],
    });

    render(<MemoryView />);

    // All 6 fixed sections exist
    expect(screen.getByText("Profile")).toBeInTheDocument();
    expect(screen.getByText("Preferences")).toBeInTheDocument();
    expect(screen.getByText("Environment")).toBeInTheDocument();
    expect(screen.getByText("Projects")).toBeInTheDocument();
    expect(screen.getByText("Decisions")).toBeInTheDocument();
    expect(screen.getByText("Open Threads")).toBeInTheDocument();

    // Populated facts rendered
    expect(screen.getByText("Desktop machine is lenovoloq.")).toBeInTheDocument();
    expect(screen.getByText("Prefers Python over Node.")).toBeInTheDocument();
    expect(screen.getByText("stated")).toBeInTheDocument();
    expect(screen.getByText("confirmed")).toBeInTheDocument();

    // Search filter
    const searchInput = screen.getByPlaceholderText(/Search facts\.\.\./i);
    fireEvent.change(searchInput, { target: { value: "lenovoloq" } });

    expect(screen.getByText("Desktop machine is lenovoloq.")).toBeInTheDocument();
    expect(screen.queryByText("Prefers Python over Node.")).not.toBeInTheDocument();

    // Clear filter
    fireEvent.change(searchInput, { target: { value: "nonexistent query xyz" } });
    expect(screen.getByText(/No facts match "nonexistent query xyz"/i)).toBeInTheDocument();
  });

  it("renders empty state in Durable Facts when no facts are remembered", () => {
    useCleaStore.setState({
      memoryFacts: [],
      memoryLongTerm: "",
    });

    render(<MemoryView />);
    expect(
      screen.getByText(/Nothing remembered yet\. Tell Clea 'remember that \.\.\.'/i),
    ).toBeInTheDocument();
  });

  it("switches to Episode Summaries and toggles trivial sessions", async () => {
    useCleaStore.setState({
      episodes: [
        {
          session_id: "s-regular-1",
          date: "2026-11-13",
          time: "14:00:00",
          summary: "Discussed architecture redesign.",
          trivial: false,
        },
        {
          session_id: "s-short-1",
          date: "2026-11-13",
          time: "14:15:00",
          summary: "Short session, 1 turn",
          trivial: true,
        },
      ],
    });

    render(<MemoryView />);

    // Change tab to 'episodes' via localStorage simulation
    localStorage.setItem("clea-memory-active-view", "episodes");
    render(<MemoryView />);

    // Regular episode is visible
    expect(screen.getByText("Discussed architecture redesign.")).toBeInTheDocument();

    // Short session is hidden by default
    expect(screen.queryByText("Short session, 1 turn")).not.toBeInTheDocument();

    // Toggle "Show short sessions"
    const toggle = screen.getByLabelText(/Show short sessions/i);
    fireEvent.click(toggle);

    // Now short session is visible
    expect(screen.getByText("Short session, 1 turn")).toBeInTheDocument();
  });

  it("switches to Consolidation Status view and displays pending and failed sessions", () => {
    localStorage.setItem("clea-memory-active-view", "status");
    useCleaStore.setState({
      memoryStatus: {
        last_consolidated_at: "2026-11-13T17:30:00Z",
        completed_count: 12,
        pending: [
          {
            session_id: "s-pending-1",
            status: "pending",
            retry_count: 1,
            next_retry_at: "2026-11-13T17:31:00Z",
            error: "Temporary API rate limit",
          },
        ],
        failed: [
          {
            session_id: "67727ff3",
            status: "failed",
            retry_count: 3,
            error: "JSON validation failed after retry",
          },
        ],
      },
    });

    render(<MemoryView />);

    expect(screen.getByText("Consolidation Engine Status")).toBeInTheDocument();
    expect(screen.getByText("Completed Sessions")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("s-pending-1")).toBeInTheDocument();
    expect(screen.getByText("67727ff3")).toBeInTheDocument();
    expect(screen.getByText("Temporary API rate limit")).toBeInTheDocument();
    expect(screen.getByText("JSON validation failed after retry")).toBeInTheDocument();
  });

  it("renders Archive view with reason and date", () => {
    localStorage.setItem("clea-memory-active-view", "archive");
    useCleaStore.setState({
      memoryArchive: [
        {
          id: "old-1",
          date: "2026-10-01",
          source: "stated",
          text: "Old laptop machine was macbookpro.",
          archived_at: "2026-11-13T10:00:00Z",
          reason: "User purchased new desktop computer",
        },
      ],
    });

    render(<MemoryView />);

    expect(screen.getByText("Old laptop machine was macbookpro.")).toBeInTheDocument();
    expect(screen.getByText(/User purchased new desktop computer/i)).toBeInTheDocument();
  });
});
