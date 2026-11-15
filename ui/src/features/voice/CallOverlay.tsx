import { PhoneOff } from "lucide-react";
import { useEffect, useState, type ComponentType } from "react";
import { Button } from "@/components/ui/button";
import { CleaAvatar } from "@/components/ui/clea-avatar";
import { cn } from "@/lib/utils";
import { useCleaStore } from "@/protocol/store";
import type { VoiceState } from "@/protocol/types";

const ORB_STYLE: Record<VoiceState, { ring: string; label: string; anim: string }> = {
  idle: {
    ring: "ring-2 ring-zinc-400/50 dark:ring-zinc-500/50",
    label: "Idle",
    anim: "clea-orb-idle",
  },
  listening: {
    ring: "ring-2 ring-cyan-400 shadow-[0_0_16px_rgba(34,211,238,0.42)]",
    label: "Listening",
    anim: "clea-orb-listening",
  },
  thinking: {
    ring: "ring-2 ring-violet-400 shadow-[0_0_16px_rgba(167,139,250,0.45)]",
    label: "Thinking",
    anim: "clea-orb-thinking",
  },
  speaking: {
    ring: "ring-2 ring-amber-400 shadow-[0_0_18px_rgba(251,146,60,0.5)]",
    label: "Speaking",
    anim: "clea-orb-speaking",
  },
  reconnecting: {
    ring: "ring-2 ring-amber-400/80 shadow-[0_0_14px_rgba(251,191,36,0.35)]",
    label: "Reconnecting",
    anim: "clea-orb-thinking",
  },
  error: {
    ring: "ring-2 ring-rose-500 shadow-[0_0_16px_rgba(244,63,94,0.45)]",
    label: "Error",
    anim: "clea-orb-error",
  },
};

export function CallOverlay() {
  const open = useCleaStore((s) => s.callOverlayOpen);
  const voiceState = useCleaStore((s) => s.voiceState);
  const voiceError = useCleaStore((s) => s.voiceError);
  const hasRunningTools = useCleaStore((s) => s.tasks.some((t) => t.status === "running"));
  const endVoiceCall = useCleaStore((s) => s.endVoiceCall);
  const setActiveView = useCleaStore((s) => s.setActiveView);

  const [MockPanel, setMockPanel] = useState<ComponentType | null>(null);

  useEffect(() => {
    if (import.meta.env.DEV && import.meta.env.VITE_CLEA_MOCKS === "true") {
      let active = true;
      void import("./DevVoiceMockPanel").then((mod) => {
        if (active) setMockPanel(() => mod.DevVoiceMockPanel);
      });
      return () => {
        active = false;
      };
    }
    return undefined;
  }, []);

  if (!open) return null;

  const current = ORB_STYLE[voiceState] ?? ORB_STYLE.idle;
  const effectiveRing =
    voiceState === "idle" && hasRunningTools ? ORB_STYLE.thinking.ring : current.ring;
  const effectiveAnim =
    voiceState === "idle" && hasRunningTools ? ORB_STYLE.thinking.anim : current.anim;

  const handleEndCall = () => {
    void endVoiceCall();
  };

  return (
    <aside
      aria-label="Voice call overlay"
      className="clea-glass fixed bottom-24 right-5 z-40 w-60 rounded-2xl p-4 shadow-2xl"
    >
      <div className="flex flex-col items-center text-center">
        <div className="relative my-2.5 flex h-20 w-20 items-center justify-center">
          <div
            data-voice-state={voiceState}
            data-testid="voice-avatar-ring"
            className={cn(
              "flex h-16 w-16 items-center justify-center rounded-full p-0.5 transition-all",
              effectiveRing,
              effectiveAnim,
            )}
          >
            <CleaAvatar
              data-testid="voice-call-avatar"
              className="h-14 w-14 rounded-full overflow-hidden"
            />
          </div>
        </div>

        <div className="text-xs font-medium text-[var(--text)]" aria-live="polite">
          {current.label}
        </div>
        {voiceState === "reconnecting" ? (
          <div className="mt-0.5 text-[11px] text-amber-500 dark:text-amber-400">
            Reconnecting session...
          </div>
        ) : voiceError ? (
          <div className="mt-1 text-[11px] font-medium text-rose-500 text-center px-1">
            {voiceError}
          </div>
        ) : (
          <div className="mt-0.5 text-[11px] text-[var(--text-subtle)]">Voice session</div>
        )}

        <Button size="sm" variant="destructive" onClick={handleEndCall} className="mt-3 w-full">
          <PhoneOff className="h-3.5 w-3.5" aria-hidden="true" />
          End Call
        </Button>

        <Button
          size="sm"
          variant="outline"
          onClick={() => setActiveView("voice")}
          className="mt-2 w-full text-xs"
        >
          View Companion Log
        </Button>
      </div>

      {MockPanel ? <MockPanel /> : null}
    </aside>
  );
}
