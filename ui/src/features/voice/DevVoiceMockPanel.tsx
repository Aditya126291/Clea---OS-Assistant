import { Button } from "@/components/ui/button";
import { useCleaStore } from "@/protocol/store";
import type { VoiceState } from "@/protocol/types";

const VOICE_STATES: VoiceState[] = ["idle", "listening", "thinking", "speaking"];

/**
 * Dev-only panel to simulate daemon `voice_state` events.
 * Never included in production builds (`import.meta.env.DEV && VITE_CLEA_MOCKS`).
 */
export function DevVoiceMockPanel() {
  const voiceState = useCleaStore((s) => s.voiceState);
  const applyEvent = useCleaStore((s) => s.applyEvent);

  const emitVoiceState = (state: VoiceState) => {
    applyEvent({
      frame_type: "event",
      event: "voice_state",
      data: { state },
    });
  };

  return (
    <div
      data-testid="dev-voice-mock-panel"
      className="mt-3 border-t border-[var(--glass-border)] pt-2.5"
    >
      <div className="mb-1.5 text-[10px] font-medium uppercase tracking-wider text-[var(--text-subtle)]">
        Dev Mock · Voice State
      </div>
      <div className="grid grid-cols-2 gap-1">
        {VOICE_STATES.map((state) => (
          <Button
            key={state}
            size="sm"
            variant={voiceState === state ? "default" : "outline"}
            onClick={() => emitVoiceState(state)}
            className="h-7 text-[11px] capitalize"
          >
            {state}
          </Button>
        ))}
      </div>
    </div>
  );
}
