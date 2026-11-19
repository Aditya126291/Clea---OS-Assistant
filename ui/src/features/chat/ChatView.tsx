import { ArrowLeft, ArrowUp, ImageIcon, MessageSquarePlus, RefreshCw, Sparkles, X } from "lucide-react";
import { useEffect, useRef, useState, type ClipboardEvent, type KeyboardEvent } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { cn } from "@/lib/utils";
import { useCleaStore } from "@/protocol/store";
import type { ChatImageAttachment } from "@/protocol/types";
import { MarkdownContent } from "./MarkdownContent";
import { ToolCallCard } from "./ToolCallCard";

interface PastedImageInfo {
  name: string;
  width: number;
  height: number;
  mimeType: string;
  dataBase64: string;
}

const MAX_PASTE_IMAGE_BYTES = 12 * 1024 * 1024;
const IMAGE_EXTENSION_RE = /\.(png|jpe?g|webp|gif|bmp)$/i;

function isImageFileCandidate(file: File | null | undefined, itemType = ""): file is File {
  if (!file) return false;
  if (file.type.startsWith("image/") || itemType.startsWith("image/")) return true;
  return IMAGE_EXTENSION_RE.test(file.name);
}

function inferImageMimeType(file: File, fallbackMimeType: string): string {
  if (file.type.startsWith("image/")) return file.type;
  if (fallbackMimeType.startsWith("image/")) return fallbackMimeType;
  const lower = file.name.toLowerCase();
  if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) return "image/jpeg";
  if (lower.endsWith(".webp")) return "image/webp";
  if (lower.endsWith(".gif")) return "image/gif";
  if (lower.endsWith(".bmp")) return "image/bmp";
  return "image/png";
}

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
  const resumeLoading = useCleaStore((s) => s.resumeLoading);
  const resumeError = useCleaStore((s) => s.resumeError);
  const retryResume = useCleaStore((s) => s.retryResume);
  const sendMessage = useCleaStore((s) => s.sendMessage);
  const respondApproval = useCleaStore((s) => s.respondApproval);
  const exitReadOnlyConversation = useCleaStore((s) => s.exitReadOnlyConversation);
  const newSession = useCleaStore((s) => s.newSession);
  const callOverlayOpen = useCleaStore((s) => s.callOverlayOpen);

  const [draft, setDraft] = useState("");
  const [pastedImage, setPastedImage] = useState<PastedImageInfo | null>(null);
  const [attachingImage, setAttachingImage] = useState(false);
  const [pasteError, setPasteError] = useState<string | null>(null);
  const pasteSeqRef = useRef(0);
  const pendingPasteRef = useRef<Promise<PastedImageInfo | null> | null>(null);
  const submittingRef = useRef(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const clearPastedImage = () => {
    pasteSeqRef.current += 1;
    pendingPasteRef.current = null;
    setAttachingImage(false);
    setPastedImage(null);
    setPasteError(null);
  };

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

  const handlePaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    let file: File | null = null;
    let fallbackMimeType = "";
    if (items) {
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.type.startsWith("image/") || item.kind === "file") {
          const candidate = item.getAsFile();
          if (isImageFileCandidate(candidate, item.type)) {
            file = candidate;
            fallbackMimeType = item.type;
            break;
          }
        }
      }
    }
    if (!file && e.clipboardData?.files?.length) {
      for (let i = 0; i < e.clipboardData.files.length; i++) {
        const candidate = e.clipboardData.files[i];
        if (isImageFileCandidate(candidate)) {
          file = candidate;
          fallbackMimeType = candidate.type;
          break;
        }
      }
    }
    if (!file) return;

    e.preventDefault();
    if (file.size > MAX_PASTE_IMAGE_BYTES) {
      pasteSeqRef.current += 1;
      pendingPasteRef.current = null;
      setAttachingImage(false);
      setPastedImage(null);
      setPasteError("Pasted image exceeds the 12 MiB limit.");
      return;
    }

    const seq = ++pasteSeqRef.current;
    const targetFile = file;
    const mimeType = inferImageMimeType(targetFile, fallbackMimeType);
    const imageName =
      targetFile.name && targetFile.name !== "image.png" ? targetFile.name : "clipboard_image.png";

    setPasteError(null);
    setPastedImage(null);
    setAttachingImage(true);

    const readPromise = new Promise<PastedImageInfo | null>((resolve) => {
      const reader = new FileReader();
      reader.onload = () => {
        if (pasteSeqRef.current !== seq) {
          resolve(null);
          return;
        }
        const result = typeof reader.result === "string" ? reader.result : "";
        const commaIdx = result.indexOf(",");
        const dataBase64 = commaIdx !== -1 ? result.slice(commaIdx + 1) : result;
        if (!dataBase64) {
          setAttachingImage(false);
          setPasteError("Couldn't read pasted image.");
          resolve(null);
          return;
        }
        const hasObjUrl = typeof URL.createObjectURL === "function";
        const imgUrl = hasObjUrl ? URL.createObjectURL(targetFile) : result;
        const img = new window.Image();
        const finalize = (width: number, height: number) => {
          if (hasObjUrl) URL.revokeObjectURL?.(imgUrl);
          if (pasteSeqRef.current !== seq) {
            resolve(null);
            return;
          }
          const info: PastedImageInfo = {
            name: imageName,
            width,
            height,
            mimeType,
            dataBase64,
          };
          setPastedImage(info);
          setAttachingImage(false);
          resolve(info);
        };
        img.onload = () => finalize(img.naturalWidth || 800, img.naturalHeight || 600);
        img.onerror = () => finalize(800, 600);
        img.src = imgUrl;
      };
      reader.onerror = () => {
        if (pasteSeqRef.current === seq) {
          setAttachingImage(false);
          setPasteError("Couldn't read pasted image.");
        }
        resolve(null);
      };
      reader.readAsDataURL(targetFile);
    });

    pendingPasteRef.current = readPromise;
    void readPromise.finally(() => {
      if (pendingPasteRef.current === readPromise) {
        pendingPasteRef.current = null;
      }
    });
  };

  const handleSubmit = async () => {
    if (sending || submittingRef.current) return;
    const trimmed = draft.trim();
    const pendingPromise = pendingPasteRef.current;
    if (!trimmed && !pastedImage && !pendingPromise) return;

    let resolvedImage = pastedImage;
    if (!resolvedImage && pendingPromise) {
      submittingRef.current = true;
      try {
        resolvedImage = await pendingPromise;
      } finally {
        submittingRef.current = false;
      }
      if (!trimmed && !resolvedImage) return;
    }

    let textToSend = trimmed;
    let attachment: ChatImageAttachment | undefined;
    if (resolvedImage) {
      const imageTag = `[Attached image: ${resolvedImage.name} · ${resolvedImage.width}×${resolvedImage.height}]`;
      textToSend = trimmed ? `${trimmed}\n${imageTag}` : `Please inspect this image.\n${imageTag}`;
      attachment = {
        data_base64: resolvedImage.dataBase64,
        mime_type: resolvedImage.mimeType,
        name: resolvedImage.name,
        width: resolvedImage.width,
        height: resolvedImage.height,
      };
      clearPastedImage();
    }
    setDraft("");
    if (attachment) {
      void sendMessage(textToSend, attachment);
    } else {
      void sendMessage(textToSend);
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void handleSubmit();
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
          {resumeLoading && messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-24 text-center">
              <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--surface)] animate-spin">
                <RefreshCw className="h-4 w-4 text-[var(--text-muted)]" aria-hidden="true" />
              </div>
              <h2 className="text-sm font-medium text-[var(--text)]">Loading conversation…</h2>
            </div>
          ) : resumeError ? (
            <div className="flex flex-col items-center justify-center py-24 text-center">
              <div
                role="alert"
                className="mb-4 rounded-lg border border-red-500/35 bg-red-500/10 px-4 py-3 text-sm text-[var(--danger)]"
              >
                <p className="font-medium">Couldn't load this conversation</p>
                <p className="mt-1 text-xs opacity-80">{resumeError}</p>
              </div>
              <Button size="sm" variant="outline" onClick={() => void retryResume()}>
                <RefreshCw className="mr-1.5 h-3.5 w-3.5" aria-hidden="true" />
                Retry
              </Button>
            </div>
          ) : messages.length === 0 ? (
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
            messages.map((msg, idx) => {
              const isJoinPoint =
                msg.id.startsWith("resumed-") &&
                messages[idx + 1] &&
                !messages[idx + 1].id.startsWith("resumed-");

              return (
                <div key={msg.id} className="space-y-4">
                  {msg.role === "user" ? (
                    <div className="flex justify-end">
                      <div className="max-w-[82%] rounded-2xl border border-[var(--border)] bg-[var(--bg-subtle)] px-4 py-2.5 text-sm text-[var(--text)]">
                        <p className="whitespace-pre-wrap break-words">{msg.content}</p>
                      </div>
                    </div>
                  ) : (
                    <div className="space-y-3">
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
                  )}

                  {isJoinPoint && (
                    <div
                      className="my-6 flex items-center gap-3"
                      aria-label="Continuing earlier conversation"
                    >
                      <div className="flex-1 border-t border-[var(--border)]" />
                      <span className="text-xs font-medium text-[var(--text-subtle)]">
                        Continuing earlier conversation
                      </span>
                      <div className="flex-1 border-t border-[var(--border)]" />
                    </div>
                  )}
                </div>
              );
            })
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      <div
        data-testid="chat-composer-container"
        className={cn(
          "border-t border-[var(--border)] bg-[var(--bg)] py-4 transition-[padding]",
          callOverlayOpen ? "pl-6 pr-68" : "px-6",
        )}
      >
        {pasteError && (
          <div
            role="alert"
            className="mx-auto mb-2 flex max-w-2xl items-center justify-between rounded-lg border border-red-500/35 bg-red-500/10 px-3 py-1.5 text-xs text-[var(--danger)]"
          >
            <span className="truncate">{pasteError}</span>
            <button
              type="button"
              onClick={() => setPasteError(null)}
              aria-label="Dismiss image error"
              className="ml-2 rounded p-0.5 hover:bg-red-500/15"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
        )}
        {!pastedImage && attachingImage && (
          <div
            data-testid="pasted-image-loading"
            className="mx-auto mb-2 flex max-w-2xl items-center justify-between rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-1.5 text-xs text-[var(--text-subtle)] shadow-xs"
          >
            <div className="flex items-center gap-2 truncate">
              <ImageIcon className="h-3.5 w-3.5 shrink-0 animate-pulse text-[var(--text-muted)]" aria-hidden="true" />
              <span className="truncate">Attaching image…</span>
            </div>
            <button
              type="button"
              onClick={clearPastedImage}
              aria-label="Cancel image attachment"
              className="ml-2 rounded p-0.5 text-[var(--text-subtle)] hover:bg-[var(--bg-subtle)] hover:text-[var(--text)]"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
        )}
        {pastedImage && (
          <div
            data-testid="pasted-image-badge"
            className="mx-auto mb-2 flex max-w-2xl items-center justify-between rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-1.5 text-xs text-[var(--text)] shadow-xs"
          >
            <div className="flex items-center gap-2 truncate">
              <ImageIcon className="h-3.5 w-3.5 shrink-0 text-[var(--text-muted)]" aria-hidden="true" />
              <span className="truncate font-medium">
                {pastedImage.name} · {pastedImage.width}×{pastedImage.height}
              </span>
            </div>
            <button
              type="button"
              onClick={clearPastedImage}
              aria-label="Remove attached image"
              className="ml-2 rounded p-0.5 text-[var(--text-subtle)] hover:bg-[var(--bg-subtle)] hover:text-[var(--text)]"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
        )}
        <form
          className="mx-auto flex max-w-2xl items-end gap-2 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-2 focus-within:border-[var(--border-strong)]"
          onSubmit={(e) => {
            e.preventDefault();
            void handleSubmit();
          }}
        >
          <textarea
            ref={inputRef}
            rows={1}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            placeholder="Message Clea…"
            aria-label="Message Clea"
            className="max-h-40 min-h-[38px] min-w-0 flex-1 resize-none bg-transparent px-2 py-2 text-sm text-[var(--text)] placeholder:text-[var(--text-subtle)] focus:outline-none"
          />
          <Button
            type="submit"
            size="icon"
            disabled={(!draft.trim() && !pastedImage && !attachingImage) || sending}
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
