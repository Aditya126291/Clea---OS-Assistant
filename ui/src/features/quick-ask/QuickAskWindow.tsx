import { ArrowUp, X } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/button";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { MarkdownContent } from "@/features/chat/MarkdownContent";
import { useCleaStore } from "@/protocol/store";

export function QuickAskWindow() {
  const connectionState = useCleaStore((s) => s.connectionState);
  const messages = useCleaStore((s) => s.messages);
  const sending = useCleaStore((s) => s.sending);
  const sendMessage = useCleaStore((s) => s.sendMessage);
  const respondApproval = useCleaStore((s) => s.respondApproval);
  const newSession = useCleaStore((s) => s.newSession);

  const [prompt, setPrompt] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    const unsub = window.cleaDesktop?.onQuickAskFocus?.(() => {
      void newSession();
      setPrompt("");
      inputRef.current?.focus();
    });
    return () => unsub?.();
  }, [newSession]);

  useEffect(() => {
    const onKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        window.cleaDesktop?.closeQuickAsk?.();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const submit = () => {
    const trimmed = prompt.trim();
    if (!trimmed || sending || connectionState !== "connected") return;
    setPrompt("");
    void sendMessage(trimmed);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  const latestAssistant = [...messages].reverse().find((m) => m.role === "assistant");

  return (
    <main className="flex h-full w-full items-stretch p-2">
      <div className="clea-glass flex w-full flex-col rounded-2xl p-3 shadow-2xl">
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <input
            ref={inputRef}
            type="text"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              connectionState === "connected"
                ? "Quick ask Clea… (Esc to hide)"
                : "Daemon not running — run `clea daemon`"
            }
            aria-label="Quick ask Clea"
            disabled={connectionState !== "connected"}
            className="h-9 flex-1 bg-transparent px-2 text-sm text-[var(--text)] placeholder:text-[var(--text-subtle)] focus:outline-none"
          />
          <Button
            type="submit"
            size="icon"
            disabled={!prompt.trim() || sending || connectionState !== "connected"}
            aria-label="Send quick ask"
          >
            <ArrowUp className="h-4 w-4" aria-hidden="true" />
          </Button>
          <Button
            type="button"
            size="icon"
            variant="ghost"
            onClick={() => window.cleaDesktop?.closeQuickAsk?.()}
            aria-label="Close quick ask"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </Button>
        </form>

        {latestAssistant && (
          <div className="mt-2.5 max-h-64 space-y-2 overflow-y-auto border-t border-[var(--glass-border)] pt-2.5">
            {latestAssistant.approvals.map((approval) => (
              <ApprovalCard
                key={approval.approvalId}
                approval={approval}
                onRespond={(id, decision) => void respondApproval(id, decision)}
              />
            ))}
            {latestAssistant.content ? (
              <MarkdownContent content={latestAssistant.content} />
            ) : latestAssistant.streaming ? (
              <span className="text-xs text-[var(--text-subtle)]">Thinking…</span>
            ) : null}
            {latestAssistant.error && (
              <p className="text-xs text-[var(--danger)]">{latestAssistant.error}</p>
            )}
          </div>
        )}
      </div>
    </main>
  );
}
