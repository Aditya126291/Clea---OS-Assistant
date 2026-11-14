import { AudioLines, Loader2, Phone, PhoneOff, Radio } from "lucide-react";
import { useEffect, useRef } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useCleaStore } from "@/protocol/store";

function formatTime(isoStr?: string): string {
  if (!isoStr) return "";
  try {
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr;
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  } catch {
    return isoStr;
  }
}

export function VoiceView() {
  const callState = useCleaStore((s) => s.callState);
  const voiceState = useCleaStore((s) => s.voiceState);
  const voiceError = useCleaStore((s) => s.voiceError);
  const startVoiceCall = useCleaStore((s) => s.startVoiceCall);
  const endVoiceCall = useCleaStore((s) => s.endVoiceCall);
  const voiceHistoryMessages = useCleaStore((s) => s.voiceHistoryMessages);
  const voiceHistoryLoading = useCleaStore((s) => s.voiceHistoryLoading);
  const loadVoiceHistory = useCleaStore((s) => s.loadVoiceHistory);

  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void loadVoiceHistory();
  }, [loadVoiceHistory]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [voiceHistoryMessages.length]);

  const isCallActive = callState === "active";

  return (
    <div className="flex h-full flex-col overflow-hidden bg-[var(--bg)] text-[var(--text)]">
      {/* Top Header */}
      <div className="flex shrink-0 items-center justify-between border-b border-[var(--border)] px-6 py-4">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-[var(--surface)] text-[var(--text)] border border-[var(--border)]">
            <AudioLines className="h-5 w-5" aria-hidden="true" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-sm font-semibold tracking-tight text-[var(--text)]">
                Voice Companion
              </h1>
              <Badge
                variant={
                  isCallActive
                    ? voiceState === "speaking"
                      ? "success"
                      : voiceState === "listening"
                        ? "neutral"
                        : "warning"
                    : "neutral"
                }
                className="capitalize text-[11px]"
              >
                {isCallActive ? (
                  <span className="flex items-center gap-1">
                    <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
                    {voiceState}
                  </span>
                ) : (
                  "Idle"
                )}
              </Badge>
            </div>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">
              Single continuous ambient voice stream. Spoken turns stay here and do not clutter sidebar History.
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {isCallActive ? (
            <Button
              size="sm"
              variant="destructive"
              onClick={() => void endVoiceCall()}
              className="gap-1.5"
            >
              <PhoneOff className="h-3.5 w-3.5" aria-hidden="true" />
              End Call
            </Button>
          ) : (
            <Button
              size="sm"
              variant="default"
              onClick={() => void startVoiceCall()}
              className="gap-1.5"
            >
              <Phone className="h-3.5 w-3.5" aria-hidden="true" />
              Start Call
            </Button>
          )}
        </div>
      </div>

      {voiceError && (
        <div
          role="alert"
          className="border-b border-red-500/30 bg-red-500/10 px-6 py-2 text-xs text-[var(--danger)]"
        >
          Voice error: {voiceError}
        </div>
      )}

      {/* Transcript Log Area */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
        {voiceHistoryLoading && voiceHistoryMessages.length === 0 ? (
          <div className="flex h-48 items-center justify-center text-xs text-[var(--text-muted)] gap-2">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading Voice Companion transcript...
          </div>
        ) : voiceHistoryMessages.length === 0 ? (
          <div className="flex h-64 flex-col items-center justify-center text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-[var(--surface)] text-[var(--text-muted)] mb-3 border border-[var(--border)]">
              <Radio className="h-6 w-6 opacity-60" aria-hidden="true" />
            </div>
            <p className="text-sm font-medium text-[var(--text)]">No voice companion turns yet</p>
            <p className="text-xs text-[var(--text-muted)] max-w-sm mt-1">
              Start a voice call to chat naturally. Your turns will append chronologically to this single stream.
            </p>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void startVoiceCall()}
              className="mt-4 gap-1.5"
            >
              <Phone className="h-3.5 w-3.5" aria-hidden="true" />
              Start Call
            </Button>
          </div>
        ) : (
          <div className="space-y-3 max-w-3xl mx-auto">
            {voiceHistoryMessages.map((msg, idx) => {
              const prevMsg = idx > 0 ? voiceHistoryMessages[idx - 1] : null;
              const isNewCall = !prevMsg || (msg.call_id && msg.call_id !== prevMsg.call_id);
              const isUser = msg.role === "user";

              return (
                <div key={`vc-msg-${idx}-${msg.timestamp}`} className="space-y-2">
                  {isNewCall && (
                    <div className="my-4 flex items-center gap-3">
                      <div className="h-px flex-1 bg-[var(--border)]" />
                      <span className="text-[11px] font-medium text-[var(--text-subtle)] uppercase tracking-wider">
                        Voice Call {msg.call_id ? `· ${msg.call_id.slice(0, 8)}` : ""}{" "}
                        {msg.timestamp ? `(${formatTime(msg.timestamp)})` : ""}
                      </span>
                      <div className="h-px flex-1 bg-[var(--border)]" />
                    </div>
                  )}

                  <Card
                    className={`border ${
                      isUser
                        ? "border-[var(--primary)]/30 bg-[var(--surface)]/80 ml-8"
                        : "border-[var(--border)] bg-[var(--surface)] mr-8"
                    }`}
                  >
                    <CardContent className="p-3">
                      <div className="flex items-center justify-between gap-2 mb-1">
                        <span
                          className={`text-xs font-semibold ${
                            isUser ? "text-[var(--primary)]" : "text-[var(--text)]"
                          }`}
                        >
                          {isUser ? "You" : "Clea"}
                        </span>
                        {msg.timestamp && (
                          <span className="text-[10px] text-[var(--text-subtle)]">
                            {formatTime(msg.timestamp)}
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-[var(--text)] whitespace-pre-wrap leading-relaxed">
                        {msg.text}
                      </p>
                    </CardContent>
                  </Card>
                </div>
              );
            })}
            <div ref={bottomRef} />
          </div>
        )}
      </div>
    </div>
  );
}
export default VoiceView;
