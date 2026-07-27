import {
  useEffect,
  useMemo,
  useState,
  type PropsWithChildren,
} from "react";
import {
  CheckIcon,
  ChevronDownIcon,
  CircleAlertIcon,
  CommandIcon,
  EyeIcon,
  GripIcon,
  KeyboardIcon,
  LoaderCircleIcon,
  MousePointer2Icon,
  MoveIcon,
  ScanSearchIcon,
  ScrollTextIcon,
  XIcon,
} from "lucide-react";
import {
  useToolCallElapsed,
  type ToolCallMessagePartComponent,
  type ToolCallMessagePartProps,
} from "@assistant-ui/react";
import { ToolFallbackApproval } from "@/components/assistant-ui/tool-fallback";
import {
  ToolGroupContent,
  ToolGroupRoot,
} from "@/components/assistant-ui/tool-group";
import type { ThreadGroupPart } from "@/components/assistant-ui/thread";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";

type JsonRecord = Record<string, unknown>;

const record = (value: unknown): JsonRecord =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};

const text = (value: unknown) => (typeof value === "string" ? value : "");

const number = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

const actionName = (action: JsonRecord) =>
  text(action.type) || text(action.action) || "action";

const actionIcon = (kind: string) => {
  if (kind.includes("type")) return KeyboardIcon;
  if (kind === "key" || kind.includes("hotkey")) return CommandIcon;
  if (kind.includes("click")) return MousePointer2Icon;
  if (kind.includes("scroll")) return ScrollTextIcon;
  if (kind.includes("drag")) return GripIcon;
  if (kind.includes("move")) return MoveIcon;
  if (kind.includes("observe") || kind.includes("screen")) return EyeIcon;
  return ScanSearchIcon;
};

const keyLabel = (action: JsonRecord) => {
  const keys = Array.isArray(action.keys) ? action.keys : [];
  return keys.map(String).join(" + ");
};

const actionLabel = (action: JsonRecord) => {
  const kind = actionName(action);
  const x = number(action.x);
  const y = number(action.y);
  if (kind.includes("click")) {
    return x != null && y != null ? `Click at ${x} × ${y}` : "Click";
  }
  if (kind.includes("type")) {
    const value = text(action.text);
    const preview = value.replace(/\s+/g, " ").slice(0, 42);
    return preview
      ? `Type “${preview}${value.length > 42 ? "…" : ""}”`
      : "Type text";
  }
  if (kind === "key" || kind.includes("hotkey")) {
    return keyLabel(action) ? `Press ${keyLabel(action)}` : "Press key";
  }
  if (kind.includes("scroll")) {
    const amount = number(action.delta_y) ?? number(action.dy);
    return amount == null
      ? "Scroll"
      : `Scroll ${amount < 0 ? "up" : "down"} ${Math.abs(amount)} px`;
  }
  if (kind.includes("drag")) return "Drag pointer";
  if (kind.includes("move")) {
    return x != null && y != null ? `Move to ${x} × ${y}` : "Move pointer";
  }
  if (kind === "wait") {
    const duration = number(action.ms) ?? number(action.duration_ms);
    return duration != null ? `Wait ${duration} ms` : "Wait";
  }
  return kind.replaceAll("_", " ");
};

const actionSequence = (actions: readonly JsonRecord[]) =>
  actions.map((action) => actionLabel(action)).join(" → ");

const summarize = (toolName: string, args: JsonRecord) => {
  const actions = Array.isArray(args.actions)
    ? args.actions.map(record)
    : [];
  if (actions.length === 1) {
    return {
      title: actionLabel(actions[0]!),
      detail: toolName.replaceAll("_", " "),
      actions,
    };
  }
  if (actions.length > 1) {
    return {
      title: `${actions.length}-step computer sequence`,
      detail: actionSequence(actions),
      actions,
    };
  }
  if (toolName.includes("observe") || toolName.includes("screen")) {
    return {
      title: "Observe the computer",
      detail: "Capture a fresh frame before acting",
      actions: [{ type: "observe" }],
    };
  }
  return {
    title: toolName.replace(/^pikvm_/, "").replaceAll("_", " "),
    detail: "Computer-use MCP call",
    actions: [{ type: toolName }],
  };
};

const durationLabel = (milliseconds: number | undefined) => {
  if (milliseconds == null) return "";
  if (milliseconds < 1_000) return "<1s";
  if (milliseconds < 10_000) return `${(milliseconds / 1_000).toFixed(1)}s`;
  return `${Math.round(milliseconds / 1_000)}s`;
};

const statusMeta = (
  status: ToolCallMessagePartProps["status"],
  result: unknown,
) => {
  if (status?.type === "requires-action") {
    return {
      label: "Approval needed",
      Icon: CircleAlertIcon,
      tone: "text-amber-300",
    };
  }
  if (status?.type === "running") {
    return {
      label: "Running",
      Icon: LoaderCircleIcon,
      tone: "text-sky-300",
    };
  }
  if (status?.type === "incomplete") {
    return {
      label: status.reason === "cancelled" ? "Cancelled" : "Failed",
      Icon: XIcon,
      tone: "text-rose-300",
    };
  }
  const resultRecord = record(result);
  if (text(resultRecord.status) === "refused") {
    return {
      label: "Refused safely",
      Icon: CircleAlertIcon,
      tone: "text-amber-300",
    };
  }
  if (text(resultRecord.status) === "unverified") {
    return {
      label: "Not verified",
      Icon: EyeIcon,
      tone: "text-amber-300",
    };
  }
  return {
    label: "Completed",
    Icon: CheckIcon,
    tone: "text-emerald-300",
  };
};

const resultLabel = (result: unknown) => {
  const value = record(result);
  if (!Object.keys(value).length) return "";
  const status = text(value.status);
  if (status === "failed") return text(value.error) || "The action failed.";
  if (status === "refused") {
    const reason = text(value.reason);
    return reason
      ? `Input refused safely · ${reason}`
      : "The action was refused before input.";
  }
  if (status === "unverified") {
    const reason = text(value.reason);
    return reason
      ? `Input completed, result not verified · ${reason}`
      : "Input completed, but the screen result was not verified.";
  }
  const details = [
    number(value.frame_id) != null ? `frame ${value.frame_id}` : "",
    number(value.world_version) != null
      ? `world ${value.world_version}`
      : "",
  ].filter(Boolean);
  return details.length
    ? `Screen updated · ${details.join(" · ")}`
    : "The computer action completed.";
};

const metadata = (args: JsonRecord, actions: readonly JsonRecord[]) => {
  const characters = actions.reduce(
    (total, action) => total + text(action.text).length,
    0,
  );
  return [
    actions.length > 1 ? `${actions.length} inputs` : "",
    characters ? `${characters} characters` : "",
    number(args.based_on_world_version) != null
      ? `world ${args.based_on_world_version}`
      : "",
    number(args.based_on_control_epoch) != null
      ? `control ${args.based_on_control_epoch}`
      : "",
  ].filter(Boolean);
};

function ActionPath({ actions }: { actions: readonly JsonRecord[] }) {
  if (actions.length < 2) return null;
  return (
    <ol
      className="flex min-w-0 items-center gap-1 overflow-hidden"
      aria-label="Computer input sequence"
    >
      {actions.slice(0, 4).map((action, index) => {
        const Icon = actionIcon(actionName(action));
        return (
          <li key={`${actionName(action)}:${index}`} className="contents">
            {index > 0 ? (
              <span className="text-border" aria-hidden="true">
                /
              </span>
            ) : null}
            <span className="text-muted-foreground flex min-w-0 items-center gap-1 text-xs">
              <Icon className="size-3 shrink-0" aria-hidden="true" />
              <span className="truncate">{actionLabel(action)}</span>
            </span>
          </li>
        );
      })}
      {actions.length > 4 ? (
        <li className="text-muted-foreground shrink-0 text-xs">
          +{actions.length - 4}
        </li>
      ) : null}
    </ol>
  );
}

const ComputerToolCallImpl: ToolCallMessagePartComponent<
  JsonRecord,
  unknown
> = ({
  toolName,
  args,
  argsText,
  result,
  status,
  addResult,
  resume,
  interrupt,
  approval,
  respondToApproval,
}) => {
  const needsApproval = status?.type === "requires-action";
  const resultStatus = text(record(result).status);
  const failed =
    status?.type === "incomplete" || resultStatus === "failed";
  const needsReview = resultStatus === "unverified";
  const [open, setOpen] = useState(
    needsApproval || failed || needsReview,
  );
  const elapsed = useToolCallElapsed();
  const summary = useMemo(
    () => summarize(toolName, record(args)),
    [args, toolName],
  );
  const state = statusMeta(status, result);
  const StateIcon = state.Icon;
  const PrimaryIcon = actionIcon(actionName(summary.actions[0] ?? {}));
  const facts = metadata(record(args), summary.actions);
  const outcome = resultLabel(result);
  const approvalContext = approval?.options?.find(
    (option) =>
      option.kind === "allow-once" || option.kind === "allow-always",
  )?.description;

  useEffect(() => {
    if (needsApproval || failed || needsReview) setOpen(true);
  }, [failed, needsApproval, needsReview]);

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className={cn(
        "group/computer-tool border-l pl-3 transition-colors",
        needsApproval
          ? "border-amber-400/60"
          : needsReview
            ? "border-amber-400/60"
          : failed
            ? "border-rose-400/60"
            : "border-border/80",
      )}
    >
      <CollapsibleTrigger className="group/trigger hover:text-foreground flex w-full items-start gap-2 py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring/60">
        <PrimaryIcon
          className="text-muted-foreground mt-0.5 size-4 shrink-0"
          aria-hidden="true"
        />
        <span className="min-w-0 flex-1">
          <span className="flex min-w-0 items-center gap-2">
            <span className="truncate text-sm font-medium">{summary.title}</span>
            {durationLabel(elapsed) ? (
              <span className="text-muted-foreground shrink-0 text-[11px] tabular-nums">
                {durationLabel(elapsed)}
              </span>
            ) : null}
          </span>
          <span className="text-muted-foreground mt-0.5 block truncate text-xs">
            {summary.detail}
          </span>
        </span>
        <span
          className={cn(
            "flex shrink-0 items-center gap-1.5 text-xs",
            state.tone,
          )}
        >
          <StateIcon
            className={cn(
              "size-3",
              status?.type === "running" && "animate-spin",
            )}
            aria-hidden="true"
          />
          {state.label}
        </span>
        <ChevronDownIcon
          className="text-muted-foreground size-4 shrink-0 -rotate-90 transition-transform group-data-open/trigger:rotate-0 group-data-panel-open/trigger:rotate-0"
          aria-hidden="true"
        />
      </CollapsibleTrigger>

      <CollapsibleContent className="data-closed:animate-collapsible-up data-open:animate-collapsible-down overflow-hidden">
        <div className="ml-2 border-l border-border/60 py-2 pl-4">
          <ActionPath actions={summary.actions} />

          {facts.length ? (
            <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
              {facts.map((fact) => (
                <span key={fact}>{fact}</span>
              ))}
            </div>
          ) : null}

          {needsApproval ? (
            <div className="mt-3 text-amber-100">
              <p className="flex items-center gap-2 text-sm font-medium text-amber-100">
                <CircleAlertIcon className="size-4" aria-hidden="true" />
                Review before this input reaches the computer
              </p>
              <p className="mt-1 max-w-xl text-xs leading-relaxed text-amber-100/70">
                {approvalContext ||
                  "This may cause an external or difficult-to-reverse action. Exact input is available below."}
              </p>
              <ToolFallbackApproval
                className="mt-2"
                addResult={addResult}
                resume={resume}
                interrupt={interrupt}
                approval={approval}
                respondToApproval={respondToApproval}
              />
            </div>
          ) : null}

          {outcome ? (
            <div
              className={cn(
                "mt-3 flex items-center gap-2 text-xs",
                failed
                  ? "text-rose-200"
                  : "text-emerald-100/80",
              )}
            >
              {failed ? (
                <XIcon className="size-3.5 shrink-0" aria-hidden="true" />
              ) : (
                <CheckIcon className="size-3.5 shrink-0" aria-hidden="true" />
              )}
              {outcome}
            </div>
          ) : null}

          <Collapsible
            className="group/arguments mt-3"
          >
            <CollapsibleTrigger className="text-muted-foreground flex w-full items-center justify-between py-1.5 text-xs hover:text-foreground">
              <span>
                Exact MCP arguments ·{" "}
                <code className="font-mono">{toolName}</code>
              </span>
              <ChevronDownIcon className="size-3.5 -rotate-90 transition-transform group-data-open/arguments:rotate-0 group-data-panel-open/arguments:rotate-0" />
            </CollapsibleTrigger>
            <CollapsibleContent className="data-closed:animate-collapsible-up data-open:animate-collapsible-down overflow-hidden">
              <pre className="mt-1 max-h-72 overflow-auto border-l border-border/60 py-1 pl-3 font-mono text-[11px] leading-relaxed text-foreground/80 whitespace-pre-wrap">
                {argsText || JSON.stringify(args, null, 2)}
              </pre>
            </CollapsibleContent>
          </Collapsible>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
};

export const ComputerToolCall =
  ComputerToolCallImpl as ToolCallMessagePartComponent;

export function ComputerToolGroup({
  group,
  children,
}: PropsWithChildren<{ group: ThreadGroupPart }>) {
  const count = group.indices.length;
  const active = group.status.type === "running";
  const needsAttention = group.status.type === "requires-action";
  const [open, setOpen] = useState(needsAttention || active);

  useEffect(() => {
    if (needsAttention || active) setOpen(true);
  }, [active, needsAttention]);

  if (count === 1) {
    return <div className="my-3">{children}</div>;
  }

  return (
    <ToolGroupRoot
      variant="ghost"
      open={open}
      onOpenChange={setOpen}
      className="my-3 border-0 bg-transparent shadow-none"
    >
      <CollapsibleTrigger className="group/trigger flex w-full items-center gap-2 py-2 text-left">
        <span className={needsAttention ? "text-amber-300" : "text-sky-300"}>
          {needsAttention ? (
            <CircleAlertIcon className="size-4" />
          ) : active ? (
            <LoaderCircleIcon className="size-4 animate-spin" />
          ) : (
            <MousePointer2Icon className="size-4" />
          )}
        </span>
        <span className="flex min-w-0 flex-1 items-baseline gap-2">
          <span className="shrink-0 text-sm font-medium">
            {count} computer {count === 1 ? "action" : "actions"}
          </span>
          <span className="text-muted-foreground truncate text-xs">
            {needsAttention
              ? "Approval is waiting inside"
              : active
                ? "Input sequence is running"
                : "Click to inspect each input and its evidence"}
          </span>
        </span>
        <ChevronDownIcon className="text-muted-foreground size-4 -rotate-90 transition-transform group-data-open/trigger:rotate-0 group-data-panel-open/trigger:rotate-0" />
      </CollapsibleTrigger>
      <ToolGroupContent className="[&>div]:gap-2">
        {children}
      </ToolGroupContent>
    </ToolGroupRoot>
  );
}
