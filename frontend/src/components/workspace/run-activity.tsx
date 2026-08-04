import { LoaderCircleIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { RunSnapshot } from "@/types";

type ActiveActivity = RunSnapshot["active_activity"];

export const activityPresentation = (activity: ActiveActivity) => {
  const role = activity?.kind === "model" ? activity.role : "";
  const roleLabel =
    role === "assistant"
      ? "Thinking"
      : role === "reasoner"
      ? "Planning the task"
      : role === "controller"
        ? "Choosing the next action"
        : role === "verifier"
          ? "Checking the result"
          : "Starting";
  const phaseLabel =
    activity?.phase === "provider_selected"
      ? "Starting the model"
      : activity?.phase === "request_sent"
        ? "Waiting for a response"
      : activity?.phase === "output_received"
        ? "Reading the model response"
        : activity?.phase === "validating"
          ? "Checking the model response"
          : activity?.phase === "schema_repair"
            ? "Repairing the model response"
            : activity?.phase === "failover"
              ? "Switching models"
              : "";
  // The role says what work is happening; the phase mostly says "a request is
  // in flight", which is true of every request and tells the user nothing. Only
  // the phases that mean something unusual are allowed to take the headline.
  const notablePhase =
    activity?.phase === "failover" ||
    activity?.phase === "schema_repair" ||
    activity?.phase === "validating";
  const label = notablePhase ? phaseLabel : roleLabel;
  const model =
    activity?.kind === "model" && activity.model?.trim()
      ? activity.model.trim()
      : "";
  const provider =
    activity?.kind === "model" && activity.provider?.trim()
      ? activity.provider.trim()
      : "";
  const route = [model, provider].filter(Boolean).join(" via ");
  return {
    label,
    model,
    route: model || provider,
    title: route ? `${label} — ${route}` : label,
  };
};

/** Seconds since this stage began, so a long wait reads as slow rather than
 *  frozen. Held back for a few seconds so quick turns never show a timer. */
function useElapsed(active: boolean, resetKey: string) {
  const [seconds, setSeconds] = useState(0);
  const startedAt = useRef(0);
  useEffect(() => {
    if (!active) {
      setSeconds(0);
      return;
    }
    startedAt.current = Date.now();
    setSeconds(0);
    const timer = setInterval(
      () => setSeconds(Math.round((Date.now() - startedAt.current) / 1000)),
      1000,
    );
    return () => clearInterval(timer);
  }, [active, resetKey]);
  return seconds;
}

export function RunActivity({
  activity,
  working,
}: {
  activity?: ActiveActivity;
  working: boolean;
}) {
  const presentation = activityPresentation(activity);
  // Keyed on the label so moving between stages restarts the count rather than
  // showing one ever-growing number for the whole run.
  const elapsed = useElapsed(
    working && activity?.kind !== "tool",
    presentation.label,
  );
  if (!working || activity?.kind === "tool") return null;

  return (
    <div
      className="aui-run-activity"
      role="status"
      aria-live="polite"
      title={presentation.title}
    >
      <LoaderCircleIcon
        className="size-3.5 shrink-0 animate-spin"
        aria-hidden="true"
      />
      <span className="font-medium text-foreground/80">
        {presentation.label}
      </span>
      {presentation.route ? (
        <span
          className="max-w-40 truncate text-xs text-muted-foreground"
          aria-label={`Active model ${presentation.route}`}
        >
          {presentation.route}
        </span>
      ) : null}
      {elapsed >= 3 ? (
        <span className="text-xs tabular-nums text-muted-foreground">
          {elapsed < 60
            ? `${elapsed}s`
            : `${Math.floor(elapsed / 60)}m ${elapsed % 60}s`}
        </span>
      ) : null}
    </div>
  );
}
