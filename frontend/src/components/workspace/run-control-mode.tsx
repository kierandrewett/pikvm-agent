import {
  ShieldCheckIcon,
  SquareTerminalIcon,
} from "lucide-react";
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

export const usesManagedControlLoop = (origin: RunOrigin) =>
  origin !== "direct_mcp";

export const canComposeIntoRun = (
  connected: boolean,
  origin: RunOrigin,
) => connected && usesManagedControlLoop(origin);

export function RunControlModeBadge({ origin }: { origin?: RunOrigin }) {
  if (origin === "direct_mcp") {
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
  return (
    <Badge
      variant="ghost"
      title="The visible harness plans, acts, verifies, and owns approval boundaries."
    >
      <ShieldCheckIcon data-icon="inline-start" aria-hidden="true" />
      Managed harness
    </Badge>
  );
}

export function DirectRunBanner({
  onStartManaged,
}: {
  onStartManaged?: () => void;
}) {
  return (
    <Alert variant="caution" className="workspace-mode-banner">
      <SquareTerminalIcon aria-hidden="true" />
      <AlertTitle>Outer client controls this run</AlertTitle>
      <AlertDescription>
        Claude, Codex, Gemini, or OpenCode is choosing the raw inputs. The
        harness records and gates them, but does not claim to have planned or
        independently verified them. Start a new task for harness-managed
        execution.
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
