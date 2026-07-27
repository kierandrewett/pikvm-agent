import { SquareTerminalIcon } from "lucide-react";
import {
  Alert,
  AlertAction,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { RunSummary } from "@/types";

type RunOrigin = RunSummary["origin"] | undefined;

const callerValue = (
  caller: Record<string, unknown> | undefined,
  key: string,
) => {
  const value = caller?.[key];
  return typeof value === "string" && value.trim() ? value.trim() : "";
};

export const directCallerSummary = (
  caller: Record<string, unknown> | undefined,
) => {
  const identity =
    callerValue(caller, "label") ||
    callerValue(caller, "name") ||
    "External MCP client";
  const route = [
    callerValue(caller, "provider"),
    callerValue(caller, "model"),
  ]
    .filter(Boolean)
    .join(" · ");
  return { identity, route };
};

export const usesManagedControlLoop = (origin: RunOrigin) =>
  origin !== "direct_mcp";

export const canComposeIntoRun = (
  connected: boolean,
  origin: RunOrigin,
) => connected && usesManagedControlLoop(origin);

export function RunControlModeBadge({ origin }: { origin?: RunOrigin }) {
  if (origin !== "direct_mcp") return null;
  return (
    <Badge
      variant="caution"
      title="The outer coding client chooses each raw input. The harness records, gates, and exposes the calls but does not own their plan or verification."
    >
      <SquareTerminalIcon data-icon="inline-start" aria-hidden="true" />
      Guarded direct
    </Badge>
  );
}

export function DirectRunBanner({
  caller,
  onStartManaged,
}: {
  caller?: Record<string, unknown>;
  onStartManaged?: () => void;
}) {
  const { identity, route } = directCallerSummary(caller);
  return (
    <Alert variant="caution" className="workspace-mode-banner">
      <SquareTerminalIcon aria-hidden="true" />
      <AlertTitle>Outer client controls this run</AlertTitle>
      <AlertDescription>
        <span className="font-medium text-foreground">{identity}</span>
        {route ? ` · ${route}` : ""} chooses the raw inputs. The harness
        records and gates them, but does not claim to have planned or
        independently verified them.
      </AlertDescription>
      {onStartManaged ? (
        <AlertAction>
          <Button
            type="button"
            size="xs"
            variant="outline"
            onClick={onStartManaged}
          >
            New managed task
          </Button>
        </AlertAction>
      ) : null}
    </Alert>
  );
}
