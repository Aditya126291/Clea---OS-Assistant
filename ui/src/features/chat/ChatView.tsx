import { ArrowLeft, ArrowUp, MessageSquarePlus, Sparkles } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { useCleaStore } from "@/protocol/store";
import { MarkdownContent } from "./MarkdownContent";
import { ToolCallCard } from "./ToolCallCard";

function StreamingIndicator() {
  const [stillWorking, setStillWorking] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => {
      setStillWorking(true);
    }, 3000);
    return () => clearTimeout(timer);
  }, []);

  return (
    <span
      className="inline-flex items-center gap-1.5 text-xs text-[var(--text-subtle)]"
      aria-live="polite"
    >
      <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--text-muted)]" />
      {stillWorking ? "Still working…" : "Thinking…"}
    </span>
  );
}

export function ChatView() {
  const messages = useCleaStore((s) => s.messages);
  const sending = useCleaStore((s) => s.sending);
  const sessionId = useCleaStore((s) => s.sessionId);
  const selectedConversation = useCleaStore((s) => s.selectedConversation);
  const sendMessage = useCleaStore((s) => s.sendMessage);
  const respondApproval = useCleaStore((s) => s.respondApproval);
  const exitReadOnlyConversation = useCleaStore((s) => s.exitReadOnlyConversation);
  const newSession = useCleaStore((s) => s.newSession);

  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!selectedConversation) {
      inputRef.current?.focus();
    }
  }, [selectedConversation, sessionId]);

  const lastContent = messages[messages.length - 1]?.content;

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "auto" });
  }, [messages.length, lastContent]);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [draft]);

  const handleSubmit = () => {
    const trimmed = draft.trim();
    if (!trimmed || sending) return;
    setDraft("");
    void sendMessage(trimmed);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSubmit();
    }
  };

  if (selectedConversation) {
    return (
      <section aria-label="Saved conversation" className="flex h-full flex-col">
        <header className="flex items-center justify-between gap-3 border-b border-[var(--border)] px-6 py-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h1 className="truncate text-sm font-medium text-[var(--text)]">
                {selectedConversation.title}
              </h1>
              <Badge variant="neutral">Read-only · {selectedConversation.date}</Badge>
            </div>
            <p className="text-xs text-[var(--text-subtle)]">
              Session {selectedConversation.sessionId.slice(0, 8)}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button size="sm" variant="outline" onClick={exitReadOnlyConversation}>
              <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
              Live chat
            </Button>
            <Button size="sm" onClick={() => void newSession()}>
              <MessageSquarePlus className="h-3.5 w-3.5" aria-hidden="true" />
              New chat
            </Button>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-6">
          <div className="mx-auto max-w-2xl space-y-6">
            {selectedConversation.turns.length === 0 ? (
              <p className="text-sm text-[var(--text-muted)]">No turns recorded in this log.</p>
            ) : (
              selectedConversation.turns.map((turn, idx) => (
                <div key={`${turn.time}-${idx}`} className="space-y-4">
                  <div className="flex justify-end">
                    <div className="max-w-[82%] rounded-2xl bg-[var(--bg-subtle)] px-4 py-2.5 text-sm text-[var(--text)] border border-[var(--border)]">
                      <p className="whitespace-pre-wrap break-words">{turn.user}</p>
                    </div>
                  </div>
                  {turn.assistant && (
                    <div className="space-y-1">
                      <div className="text-[11px] font-medium text-[var(--text-subtle)]">
                        Clea · {turn.time}
                      </div>
                      <MarkdownContent content={turn.assistant} />
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        </div>
      </section>
    );
  }

  return (
    <section aria-label="Chat" className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-[var(--border)] px-6 py-3">
        <div className="flex items-center gap-2">
          <h1 className="text-sm font-medium text-[var(--text)]">Chat</h1>
          {sessionId && (
            <span className="font-mono text-[11px] text-[var(--text-subtle)]">
              {sessionId.slice(0, 8)}
            </span>
          )}
        </div>
        <Button size="sm" variant="ghost" onClick={() => void newSession()}>
          <MessageSquarePlus className="h-3.5 w-3.5" aria-hidden="true" />
          New chat
        </Button>
      </header>

      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto max-w-2xl space-y-6">
          {messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-24 text-center">
              <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--surface)]">
                <Sparkles className="h-4 w-4 text-[var(--text-muted)]" aria-hidden="true" />
              </div>
              <h2 className="text-base font-medium text-[var(--text)]">Ask Clea anything</h2>
              <p className="mt-1 max-w-sm text-xs text-[var(--text-subtle)]">
                Local-first assistant. Press Enter to send, Shift+Enter for a newline, or Ctrl+K for
                commands.
              </p>
            </div>
          ) : (
            messages.map((msg) =>
              msg.role === "user" ? (
                <div key={msg.id} className="flex justify-end">
                  <div className="max-w-[82%] rounded-2xl border border-[var(--border)] bg-[var(--bg-subtle)] px-4 py-2.5 text-sm text-[var(--text)]">
                    <p className="whitespace-pre-wrap break-words">{msg.content}</p>
                  </div>
                </div>
              ) : (
                <div key={msg.id} className="space-y-3">
                  {msg.toolCalls.length > 0 && (
                    <div className="space-y-2">
                      {msg.toolCalls.map((call) => (
                        <ToolCallCard key={call.callId} call={call} />
                      ))}
                    </div>
                  )}

                  {msg.approvals.length > 0 && (
                    <div className="space-y-2">
                      {msg.approvals.map((approval) => (
                        <ApprovalCard
                          key={approval.approvalId}
                          approval={approval}
                          onRespond={(id, decision) => void respondApproval(id, decision)}
                        />
                      ))}
                    </div>
                  )}

                  {msg.content ? (
                    <MarkdownContent content={msg.content} />
                  ) : msg.streaming ? (
                    <StreamingIndicator />
                  ) : null}

                  {msg.error && (
                    <div
                      role="alert"
                      className="rounded-lg border border-red-500/35 bg-red-500/10 px-3 py-2 text-xs text-[var(--danger)]"
                    >
                      {msg.error}
                    </div>
                  )}
                </div>
              ),
            )
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      <div className="border-t border-[var(--border)] bg-[var(--bg)] px-6 py-4">
        <form
          className="mx-auto flex max-w-2xl items-end gap-2 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-2 focus-within:border-[var(--border-strong)]"
          onSubmit={(e) => {
            e.preventDefault();
            handleSubmit();
          }}
        >
          <textarea
            ref={inputRef}
            rows={1}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Message Clea…"
            aria-label="Message Clea"
            className="max-h-40 min-h-[38px] flex-1 resize-none bg-transparent px-2 py-2 text-sm text-[var(--text)] placeholder:text-[var(--text-subtle)] focus:outline-none"
          />
          <Button
            type="submit"
            size="icon"
            disabled={!draft.trim() || sending}
            aria-label="Send message"
            className="shrink-0 rounded-lg"
          >
            <ArrowUp className="h-4 w-4" aria-hidden="true" />
          </Button>
        </form>
      </div>
    </section>
  );
}
