import {
  AlertCircle,
  Calendar,
  CheckCircle2,
  Clock,
  Loader2,
  Sparkles,
  XCircle,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useCleaStore } from "@/protocol/store";
import type { TaskStatusData } from "@/protocol/types";

function formatLocalTime(isoStr?: string | null): string {
  if (!isoStr) return "";
  try {
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr;
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch {
    return isoStr;
  }
}

function TaskCard({ task }: { task: TaskStatusData }) {
  const badgeVariant =
    task.status === "succeeded"
      ? "success"
      : task.status === "failed"
        ? "danger"
        : task.status === "cancelled"
          ? "neutral"
          : "warning";

  const Icon =
    task.status === "running"
      ? Loader2
      : task.status === "queued"
        ? Clock
        : task.status === "succeeded"
          ? CheckCircle2
          : task.status === "cancelled"
            ? AlertCircle
            : XCircle;

  const kindLabel =
    task.kind === "background_task"
      ? "Background"
      : task.kind === "scheduled"
        ? "Scheduled"
        : task.kind === "agent_run"
          ? "Agent run"
          : "Task";

  return (
    <Card key={task.task_id} className="border border-[var(--border)]">
      <CardContent className="flex items-start justify-between gap-4 p-4">
        <div className="flex min-w-0 items-start gap-3">
          <Icon
            className={`mt-0.5 h-4 w-4 shrink-0 ${
              task.status === "running"
                ? "animate-spin text-[var(--warning)]"
                : task.status === "succeeded"
                  ? "text-[var(--success)]"
                  : task.status === "failed"
                    ? "text-[var(--danger)]"
                    : "text-[var(--text-subtle)]"
            }`}
            aria-hidden="true"
          />
          <div className="min-w-0 space-y-1">
            <div className="flex items-center gap-2">
              <span className="truncate text-sm font-medium text-[var(--text)]">{task.title}</span>
              <span className="rounded bg-[var(--bg-subtle)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--text-subtle)]">
                {kindLabel}
              </span>
            </div>
            <div className="font-mono text-[11px] text-[var(--text-subtle)]">
              Started {formatLocalTime(task.started_at)}
              {task.finished_at && ` · Finished ${formatLocalTime(task.finished_at)}`}
              {task.next_run_at && (
                <span className="ml-2 inline-flex items-center gap-1 text-[var(--accent)]">
                  <Calendar className="h-3 w-3" /> Next run {formatLocalTime(task.next_run_at)}
                </span>
              )}
            </div>
            {task.detail && (
              <details className="mt-1 group">
                <summary className="cursor-pointer text-xs text-[var(--danger)] hover:underline">
                  {task.detail.length > 70 ? `${task.detail.slice(0, 70)}…` : task.detail}
                </summary>
                <div className="mt-1.5 rounded bg-[var(--bg-subtle)] p-2 text-[11px] text-[var(--text-muted)] whitespace-pre-wrap">
                  {task.detail}
                </div>
              </details>
            )}
          </div>
        </div>
        <Badge variant={badgeVariant}>{task.status}</Badge>
      </CardContent>
    </Card>
  );
}

export function ActivityView() {
  const tasks = useCleaStore((s) => s.tasks);
  const emitDevSampleTask = useCleaStore((s) => s.emitDevSampleTask);

  // Filter out chat turns: Activity shows only background_task, scheduled, and agent_run
  const activityTasks = tasks.filter((t) => t.kind !== "chat_turn");

  const runningAndUpcoming = activityTasks.filter(
    (t) => t.status === "running" || t.status === "queued" || Boolean(t.next_run_at),
  );

  const recent = activityTasks
    .filter((t) => t.status === "succeeded" || t.status === "failed" || t.status === "cancelled")
    .slice(0, 50);

  const isEmpty = runningAndUpcoming.length === 0 && recent.length === 0;

  return (
    <section aria-label="Activity" className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-[var(--border)] px-6 py-3">
        <div>
          <h1 className="text-sm font-medium text-[var(--text)]">Activity</h1>
          <p className="text-xs text-[var(--text-subtle)]">
            Background tasks, scheduled jobs, and agent runs.
          </p>
        </div>
        {import.meta.env.DEV && (
          <Button size="sm" variant="outline" onClick={() => void emitDevSampleTask()}>
            <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
            Emit sample tasks (dev)
          </Button>
        )}
      </header>

      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto max-w-2xl space-y-6">
          {isEmpty ? (
            <div className="flex flex-col items-center justify-center py-20 text-center">
              <Clock className="mb-2 h-6 w-6 text-[var(--text-subtle)]" aria-hidden="true" />
              <p className="text-sm text-[var(--text-muted)]">
                Nothing running. Scheduled jobs and background tasks will appear here.
              </p>
            </div>
          ) : (
            <>
              {runningAndUpcoming.length > 0 && (
                <div className="space-y-3">
                  <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--text-subtle)]">
                    Running and upcoming ({runningAndUpcoming.length})
                  </h2>
                  <div className="space-y-2">
                    {runningAndUpcoming.map((task) => (
                      <TaskCard key={task.task_id} task={task} />
                    ))}
                  </div>
                </div>
              )}

              {recent.length > 0 && (
                <div className="space-y-3">
                  <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--text-subtle)]">
                    Recent ({recent.length})
                  </h2>
                  <div className="space-y-2">
                    {recent.map((task) => (
                      <TaskCard key={task.task_id} task={task} />
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
