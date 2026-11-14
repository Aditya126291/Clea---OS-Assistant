import { PhoneOff } from "lucide-react";
import { useEffect, useState, type ComponentType } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useCleaStore } from "@/protocol/store";
import type { VoiceState } from "@/protocol/types";

const ORB_STYLE: Record<VoiceState, { ring: string; label: string; anim: string }> = {
  idle: {
    ring: "from-zinc-400/80 to-zinc-600/80 dark:from-zinc-300/80 dark:to-zinc-500/80",
    label: "Idle",
    anim: "clea-orb-idle",
  },
  listening: {
    ring: "from-sky-400 to-blue-600",
    label: "Listening",
    anim: "clea-orb-listening",
  },
  thinking: {
    ring: "from-amber-300 via-orange-400 to-amber-500",
    label: "Thinking",
    anim: "clea-orb-thinking",
  },
  speaking: {
    ring: "from-emerald-400 to-teal-500",
    label: "Speaking",
    anim: "clea-orb-speaking",
  },
  reconnecting: {
    ring: "from-amber-400 via-yellow-500 to-amber-600",
    label: "Reconnecting",
    anim: "clea-orb-thinking",
  },
  error: {
    ring: "from-rose-500 to-red-700",
    label: "Error",
    anim: "clea-orb-error",
  },
};

export function CallOverlay() {
  const open = useCleaStore((s) => s.callOverlayOpen);
  const voiceState = useCleaStore((s) => s.voiceState);
  const voiceError = useCleaStore((s) => s.voiceError);
  const endVoiceCall = useCleaStore((s) => s.endVoiceCall);

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

  const handleEndCall = () => {
    void endVoiceCall();
  };

  return (
    <aside
      aria-label="Voice call overlay"
      className="clea-glass fixed bottom-5 right-5 z-40 w-60 rounded-2xl p-4 shadow-2xl"
    >
      <div className="flex flex-col items-center text-center">
        <div className="relative my-3 flex h-20 w-20 items-center justify-center">
          <div
            data-voice-state={voiceState}
            aria-hidden="true"
            className={cn(
              "h-14 w-14 rounded-full bg-gradient-to-br shadow-lg",
              current.ring,
              current.anim,
            )}
          />
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
      </div>

      {MockPanel ? <MockPanel /> : null}
    </aside>
  );
}
