import { useEffect, useState } from "react";
import { MonitorIcon, PauseIcon, PlayIcon } from "lucide-react";
import { harnessBlob } from "@/lib/harness-api";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { RunSnapshot } from "@/types";

type ComputerSheetProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  token: string;
  run: RunSnapshot | null;
  onPause: () => Promise<void>;
  onContinue: () => Promise<void>;
};

export function ComputerSheet({
  open,
  onOpenChange,
  token,
  run,
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

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-2xl">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            <MonitorIcon aria-hidden="true" />
            Computer
          </SheetTitle>
          <SheetDescription>
            {run ? `${alias} · ${layer}` : "No computer session is selected."}
          </SheetDescription>
          {run ? (
            <div className="flex flex-wrap items-center gap-2 pt-2">
              <Badge variant="outline">{run.status.replaceAll("_", " ")}</Badge>
              {run.observation?.frame_id != null ? (
                <Badge variant="secondary">
                  frame {run.observation.frame_id}
                </Badge>
              ) : null}
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
        <div className="min-h-0 flex-1 bg-black">
          {frameUrl ? (
            <img
              src={frameUrl}
              alt="Current remote computer screen"
              className="size-full object-contain"
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
