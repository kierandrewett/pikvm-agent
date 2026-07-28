import { LoaderCircleIcon } from "lucide-react";
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
  const label = phaseLabel || roleLabel;
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

export function RunActivity({
  activity,
  working,
}: {
  activity?: ActiveActivity;
  working: boolean;
}) {
  if (!working || activity?.kind === "tool") return null;
  const presentation = activityPresentation(activity);

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
    </div>
  );
}
