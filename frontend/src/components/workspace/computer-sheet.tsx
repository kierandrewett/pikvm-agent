import { useEffect, useState } from "react";
import {
  MonitorIcon,
  MonitorOffIcon,
  PauseIcon,
  PlayIcon,
} from "lucide-react";
import { harnessBlob } from "@/lib/harness-api";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { RunSnapshot } from "@/types";

type ComputerSheetProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  token: string;
  run: RunSnapshot | null;
  connectionEnabled: boolean;
  onPause: () => Promise<void>;
  onContinue: () => Promise<void>;
};

export function ComputerSheet({
  open,
  onOpenChange,
  token,
  run,
  connectionEnabled,
  onPause,
  onContinue,
}: ComputerSheetProps) {
  const [frameUrl, setFrameUrl] = useState("");
  const [frameError, setFrameError] = useState("");

  useEffect(() => {
    if (!open || !run?.session_id || !token) {
      setFrameUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return "";
      });
      return;
    }
    let active = true;
    let currentUrl = "";
    const controller = new AbortController();
    const load = async () => {
      try {
        const blob = await harnessBlob(
          token,
          `/api/runs/${encodeURIComponent(run.run_id)}/frame?nonce=${Date.now()}`,
          controller.signal,
        );
        if (!active) return;
        const nextUrl = URL.createObjectURL(blob);
        if (currentUrl) URL.revokeObjectURL(currentUrl);
        currentUrl = nextUrl;
        setFrameUrl(nextUrl);
        setFrameError("");
      } catch (cause) {
        if (!active || controller.signal.aborted) return;
        setFrameError(
          cause instanceof Error ? cause.message : "Frame unavailable.",
        );
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 650);
    return () => {
      active = false;
      controller.abort();
      window.clearInterval(timer);
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, [open, run?.run_id, run?.session_id, token]);

  const machine = run?.observation?.machine ?? {};
  const alias =
    typeof machine.alias === "string" ? machine.alias : "Managed computer";
  const layer =
    typeof machine.desktop_layer === "string"
      ? machine.desktop_layer
      : "MCP connection";
  const canContinue = run?.status === "paused";
  const canPause = Boolean(
    run && ["planning", "running", "executing", "verifying"].includes(run.status),
  );
  const hasComputerSession = Boolean(run?.session_id);
  const emptyTitle = connectionEnabled
    ? "Managed PiKVM MCP is configured"
    : "No managed computer configured";
  const emptyDescription = connectionEnabled
    ? "A live screen appears here when a task starts using the computer."
    : "Chat and research tools remain available without computer control.";

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="computer-sheet">
        <SheetHeader className="min-h-12 shrink-0 flex-row items-center gap-3 border-b border-white/10 bg-background px-3 py-2 pr-12">
          <SheetTitle className="flex shrink-0 items-center gap-2 text-sm">
            <MonitorIcon className="size-4" aria-hidden="true" />
            Computer
          </SheetTitle>
          <SheetDescription className="min-w-0 flex-1 truncate text-xs">
            {hasComputerSession
              ? `${alias} · ${layer}`
              : connectionEnabled
                ? "Managed PiKVM MCP · no active session"
                : "Chat-only workspace"}
          </SheetDescription>
          {hasComputerSession && run ? (
            <div className="flex shrink-0 items-center gap-2">
              <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <span
                  className="size-1.5 rounded-full bg-emerald-400"
                  aria-hidden="true"
                />
                {run.status.replaceAll("_", " ")}
              </span>
              {canPause ? (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void onPause()}
                >
                  <PauseIcon data-icon="inline-start" />
                  Pause
                </Button>
              ) : null}
              {canContinue ? (
                <Button size="sm" onClick={() => void onContinue()}>
                  <PlayIcon data-icon="inline-start" />
                  Continue
                </Button>
              ) : null}
            </div>
          ) : null}
        </SheetHeader>
        <div className="flex min-h-0 flex-1 items-center justify-center bg-black">
          {!hasComputerSession ? (
            <div className="flex max-w-sm flex-col items-center gap-2 px-8 text-center">
              {connectionEnabled ? (
                <MonitorIcon
                  className="mb-1 size-7 text-muted-foreground"
                  aria-hidden="true"
                />
              ) : (
                <MonitorOffIcon
                  className="mb-1 size-7 text-muted-foreground"
                  aria-hidden="true"
                />
              )}
              <p className="text-sm font-medium text-foreground">
                {emptyTitle}
              </p>
              <p className="text-sm text-muted-foreground">
                {emptyDescription}
              </p>
            </div>
          ) : frameUrl ? (
            <img
              src={frameUrl}
              alt="Current remote computer screen"
              className="block size-full object-contain"
            />
          ) : (
            <div className="flex size-full min-h-80 items-center justify-center p-8">
              {frameError ? (
                <p className="max-w-sm text-center text-sm text-muted-foreground">
                  {frameError}
                </p>
              ) : (
                <Skeleton className="aspect-video w-full max-w-xl" />
              )}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
