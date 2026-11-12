import { CheckCircle2, Clock, Loader2, XCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { useCleaStore } from "@/protocol/store";

export function ActivityView() {
  const tasks = useCleaStore((s) => s.tasks);

  return (
    <section aria-label="Activity" className="flex h-full flex-col">
      <header className="border-b border-[var(--border)] px-6 py-3">
        <h1 className="text-sm font-medium text-[var(--text)]">Activity</h1>
        <p className="text-xs text-[var(--text-subtle)]">
          Live and completed tasks reported by the daemon.
        </p>
      </header>

      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto max-w-2xl space-y-3">
          {tasks.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-20 text-center">
              <Clock className="mb-2 h-6 w-6 text-[var(--text-subtle)]" aria-hidden="true" />
              <p className="text-sm text-[var(--text-muted)]">No tasks recorded this session.</p>
              <p className="mt-1 text-xs text-[var(--text-subtle)]">
                Send a message in Chat to see task lifecycle events here.
              </p>
            </div>
          ) : (
            tasks.map((task) => {
              const badgeVariant =
                task.status === "succeeded"
                  ? "success"
                  : task.status === "failed"
                    ? "danger"
                    : "warning";
              const Icon =
                task.status === "running"
                  ? Loader2
                  : task.status === "succeeded"
                    ? CheckCircle2
                    : XCircle;

              return (
                <Card key={task.task_id}>
                  <CardContent className="flex items-start justify-between gap-4 p-4">
                    <div className="flex min-w-0 items-start gap-3">
                      <Icon
                        className={`mt-0.5 h-4 w-4 shrink-0 ${
                          task.status === "running"
                            ? "animate-spin text-[var(--warning)]"
                            : task.status === "succeeded"
                              ? "text-[var(--success)]"
                              : "text-[var(--danger)]"
                        }`}
                        aria-hidden="true"
                      />
                      <div className="min-w-0 space-y-1">
                        <div className="truncate text-sm font-medium text-[var(--text)]">
                          {task.title}
                        </div>
                        <div className="font-mono text-[11px] text-[var(--text-subtle)]">
                          Started {new Date(task.started_at).toLocaleTimeString()}
                          {task.finished_at
                            ? ` · Finished ${new Date(task.finished_at).toLocaleTimeString()}`
                            : ""}
                        </div>
                        {task.detail && (
                          <p className="text-xs text-[var(--danger)]">{task.detail}</p>
                        )}
                      </div>
                    </div>
                    <Badge variant={badgeVariant}>{task.status}</Badge>
                  </CardContent>
                </Card>
              );
            })
          )}
        </div>
      </div>
    </section>
  );
}
