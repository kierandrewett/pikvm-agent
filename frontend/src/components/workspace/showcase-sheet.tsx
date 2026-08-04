import { useEffect, useMemo, useState } from "react";
import {
  CheckCircle2Icon,
  CircleDotDashedIcon,
  Clock3Icon,
  FilmIcon,
  MonitorPlayIcon,
  RefreshCwIcon,
  RotateCcwIcon,
  XCircleIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import {
  HarnessApiError,
  harnessBlob,
  harnessJson,
} from "@/lib/harness-api";
import type { ShowcaseCampaign, ShowcaseTask } from "@/types";

type ShowcaseSheetProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  token: string;
};

const terminalTask = (task: ShowcaseTask) =>
  task.status === "passed" || task.status === "failed";

const formatDuration = (milliseconds?: number | null) => {
  if (milliseconds == null) return "—";
  const totalSeconds = Math.round(milliseconds / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}m ${seconds.toString().padStart(2, "0")}s`;
};

const taskNumber = (campaign: ShowcaseCampaign, task: ShowcaseTask) =>
  campaign.tasks.findIndex((candidate) => candidate.task_id === task.task_id) + 1;

const statusLabel = (status: ShowcaseTask["status"]) =>
  status === "rebooting" ? "Rebooting VM" : status;

function StatusMark({ status }: { status: ShowcaseTask["status"] }) {
  if (status === "passed") {
    return (
      <CheckCircle2Icon
        className="size-4 text-evidence"
        aria-label="Passed"
      />
    );
  }
  if (status === "failed") {
    return (
      <XCircleIcon
        className="size-4 text-destructive"
        aria-label="Failed"
      />
    );
  }
  if (status === "running" || status === "rebooting") {
    return (
      <RefreshCwIcon
        className="size-4 animate-spin text-info"
        aria-label={statusLabel(status)}
      />
    );
  }
  return (
    <CircleDotDashedIcon
      className="size-4 text-muted-foreground"
      aria-label="Queued"
    />
  );
}

function useMediaUrl(
  token: string,
  path: string | null,
  refreshMs?: number,
) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!token || !path) {
      setUrl((current) => {
        if (current) URL.revokeObjectURL(current);
        return "";
      });
      setError("");
      return;
    }
    let active = true;
    let currentUrl = "";
    const controller = new AbortController();
    const load = async () => {
      try {
        const separator = path.includes("?") ? "&" : "?";
        const blob = await harnessBlob(
          token,
          `${path}${separator}nonce=${Date.now()}`,
          controller.signal,
        );
        if (!active) return;
        const nextUrl = URL.createObjectURL(blob);
        if (currentUrl) URL.revokeObjectURL(currentUrl);
        currentUrl = nextUrl;
        setUrl(nextUrl);
        setError("");
      } catch (cause) {
        if (!active || controller.signal.aborted) return;
        setError(
          cause instanceof Error ? cause.message : "Media unavailable.",
        );
      }
    };
    void load();
    const timer =
      refreshMs == null
        ? undefined
        : window.setInterval(() => void load(), refreshMs);
    return () => {
      active = false;
      controller.abort();
      if (timer != null) window.clearInterval(timer);
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, [path, refreshMs, token]);

  return { url, error };
}

export function ShowcaseSheet({
  open,
  onOpenChange,
  token,
}: ShowcaseSheetProps) {
  const [campaign, setCampaign] = useState<ShowcaseCampaign | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open || !token) return;
    let active = true;
    const load = async () => {
      try {
        const next = await harnessJson<ShowcaseCampaign>(
          token,
          "/api/showcases/current",
        );
        if (!active) return;
        setCampaign(next);
        setSelectedTaskId((current) => {
          if (
            current &&
            next.tasks.some((task) => task.task_id === current)
          ) {
            return current;
          }
          return (
            next.current_task_id ??
            [...next.tasks].reverse().find(terminalTask)?.task_id ??
            next.tasks[0]?.task_id ??
            ""
          );
        });
        setError("");
      } catch (cause) {
        if (!active) return;
        if (cause instanceof HarnessApiError && cause.status === 404) {
          setCampaign(null);
          setError("");
          return;
        }
        setError(
          cause instanceof Error
            ? cause.message
            : "Could not load recorded tasks.",
        );
      } finally {
        if (active) setLoading(false);
      }
    };
    setLoading(true);
    void load();
    const timer = window.setInterval(() => void load(), 1_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [open, token]);

  useEffect(() => {
    if (campaign?.current_task_id) {
      setSelectedTaskId(campaign.current_task_id);
    }
  }, [campaign?.current_task_id]);

  const selectedTask = useMemo(
    () =>
      campaign?.tasks.find((task) => task.task_id === selectedTaskId) ?? null,
    [campaign, selectedTaskId],
  );
  const selectedIsLive = Boolean(
    campaign?.current_task_id === selectedTask?.task_id &&
      campaign?.current_run_id,
  );
  const liveFramePath =
    selectedIsLive && campaign?.current_run_id
      ? `/api/runs/${encodeURIComponent(campaign.current_run_id)}/frame`
      : null;
  const recordingPath =
    campaign && selectedTask?.recording && !selectedIsLive
      ? `/api/showcases/${encodeURIComponent(campaign.campaign_id)}/tasks/${encodeURIComponent(selectedTask.task_id)}/recording`
      : null;
  const posterPath =
    campaign && selectedTask?.poster
      ? `/api/showcases/${encodeURIComponent(campaign.campaign_id)}/tasks/${encodeURIComponent(selectedTask.task_id)}/poster`
      : null;
  const liveFrame = useMediaUrl(token, liveFramePath, 650);
  const recording = useMediaUrl(token, recordingPath);
  const poster = useMediaUrl(token, posterPath);
  const progress =
    campaign == null ? 0 : Math.round((campaign.completed / campaign.total) * 100);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="showcase-sheet">
        <SheetHeader className="showcase-header">
          <div className="flex min-w-0 items-center gap-2">
            <MonitorPlayIcon className="size-4 shrink-0" aria-hidden="true" />
            <SheetTitle className="truncate text-sm">
              {campaign?.title ?? "Recorded proof"}
            </SheetTitle>
            {campaign ? (
              <Badge
                variant="outline"
                className="capitalize"
                data-showcase-status={campaign.status}
              >
                {campaign.status}
              </Badge>
            ) : null}
          </div>
          {/* This is the sheet's accessible description, so it is the sentence a
              screen reader hears on open. It named one specific campaign, which
              was announced whatever was loaded — including when nothing is, and
              the visible body says there is no campaign yet. The title above is
              already the campaign's own when there is one. */}
          <SheetDescription className="sr-only">
            {campaign
              ? `Live and recorded evidence for this campaign: ${campaign.completed} of ${campaign.total} tasks done, ${campaign.passed} passed.`
              : "Live and recorded evidence from task campaigns. No campaign has been recorded yet."}
          </SheetDescription>
          {campaign ? (
            <div className="showcase-summary" aria-label="Campaign progress">
              <span>{campaign.completed} / {campaign.total}</span>
              <span className="text-evidence">{campaign.passed} passed</span>
              {campaign.failed > 0 ? (
                <span className="text-destructive">
                  {campaign.failed} failed
                </span>
              ) : null}
              <span className="hidden items-center gap-1 sm:flex">
                <RotateCcwIcon className="size-3" aria-hidden="true" />
                reboot after every task
              </span>
            </div>
          ) : null}
        </SheetHeader>

        {campaign ? (
          <div className="h-0.5 shrink-0 bg-muted" aria-hidden="true">
            <div
              className="h-full bg-evidence transition-[width] duration-200"
              style={{ width: `${progress}%` }}
            />
          </div>
        ) : null}

        {loading && campaign == null ? (
          <div className="showcase-loading" role="status">
            <Skeleton className="h-8 w-64" />
            <Skeleton className="aspect-video w-full max-w-4xl" />
          </div>
        ) : error ? (
          <div className="showcase-empty">
            <XCircleIcon className="size-6 text-destructive" aria-hidden="true" />
            <p className="font-medium">Evidence feed unavailable</p>
            <p>{error}</p>
          </div>
        ) : campaign == null ? (
          <div className="showcase-empty">
            <FilmIcon className="size-7" aria-hidden="true" />
            <p className="font-medium">No recorded campaign yet</p>
            <p>The live trial list and recordings will appear here.</p>
          </div>
        ) : (
          <div className="showcase-layout">
            <nav className="showcase-task-list" aria-label="Recorded tasks">
              {campaign.tasks.map((task) => (
                <button
                  type="button"
                  key={task.task_id}
                  className="showcase-task-row"
                  data-selected={task.task_id === selectedTaskId}
                  onClick={() => setSelectedTaskId(task.task_id)}
                >
                  <StatusMark status={task.status} />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-2">
                      <span className="showcase-task-number">
                        {taskNumber(campaign, task).toString().padStart(2, "0")}
                      </span>
                      <span className="truncate text-xs text-muted-foreground">
                        {task.category}
                      </span>
                    </span>
                    <span className="block truncate text-sm font-medium">
                      {task.title}
                    </span>
                  </span>
                  {task.duration_ms != null ? (
                    <span className="text-xs tabular-nums text-muted-foreground">
                      {formatDuration(task.duration_ms)}
                    </span>
                  ) : null}
                </button>
              ))}
            </nav>

            {selectedTask ? (
              <article className="showcase-detail">
                <div className="showcase-player">
                  <div className="showcase-player-bar">
                    <span className="flex items-center gap-2">
                      <span
                        className={
                          selectedIsLive
                            ? "size-1.5 rounded-full bg-red-400 animate-pulse"
                            : "size-1.5 rounded-full bg-muted-foreground"
                        }
                        aria-hidden="true"
                      />
                      {selectedIsLive ? "Live" : "Recording"}
                    </span>
                    <span>
                      Task {taskNumber(campaign, selectedTask)} of{" "}
                      {campaign.total}
                    </span>
                  </div>
                  <div className="showcase-player-stage">
                    {selectedIsLive && liveFrame.url ? (
                      <img
                        src={liveFrame.url}
                        alt={`Live Windows screen for ${selectedTask.title}`}
                      />
                    ) : recording.url ? (
                      <video
                        key={recording.url}
                        src={recording.url}
                        poster={poster.url || undefined}
                        controls
                        preload="metadata"
                        aria-label={`Recording of ${selectedTask.title}`}
                      />
                    ) : poster.url ? (
                      <img
                        src={poster.url}
                        alt={`Final Windows screen for ${selectedTask.title}`}
                      />
                    ) : (
                      <div className="showcase-player-placeholder">
                        {liveFrame.error || recording.error || "Waiting for frames…"}
                      </div>
                    )}
                  </div>
                </div>

                <div className="showcase-task-copy">
                  <div className="flex flex-wrap items-center gap-2">
                    <StatusMark status={selectedTask.status} />
                    <h2 className="text-base font-semibold">
                      {selectedTask.title}
                    </h2>
                    <Badge variant="outline">{selectedTask.category}</Badge>
                  </div>
                  <p>{selectedTask.prompt}</p>
                  <div className="showcase-task-facts">
                    <span>
                      <Clock3Icon aria-hidden="true" />
                      {formatDuration(selectedTask.duration_ms)}
                    </span>
                    <span>
                      <RotateCcwIcon aria-hidden="true" />
                      {selectedTask.reboot.status === "ready"
                        ? `rebooted in ${formatDuration(selectedTask.reboot.duration_ms)}`
                        : selectedTask.reboot.status}
                    </span>
                    {selectedTask.result?.event_count != null ? (
                      <span>
                        {selectedTask.result.event_count} harness events
                      </span>
                    ) : null}
                  </div>
                  {selectedTask.error ? (
                    <p className="showcase-task-error">{selectedTask.error}</p>
                  ) : null}
                </div>
              </article>
            ) : null}
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}
