import {
  createContext,
  useContext,
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
  MonitorIcon,
  MousePointer2Icon,
  MoveIcon,
  ScanSearchIcon,
  ScrollTextIcon,
  ShieldAlertIcon,
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
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";

type JsonRecord = Record<string, unknown>;

export type ComputerToolEnvironment = {
  machineName?: string;
  currentFrameId?: number;
  onOpenComputer?: () => void;
};

const ComputerToolEnvironmentContext =
  createContext<ComputerToolEnvironment>({});

export function ComputerToolEnvironmentProvider({
  value,
  children,
}: PropsWithChildren<{ value: ComputerToolEnvironment }>) {
  return (
    <ComputerToolEnvironmentContext.Provider value={value}>
      {children}
    </ComputerToolEnvironmentContext.Provider>
  );
}

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

const keyValues = (action: JsonRecord) =>
  (Array.isArray(action.keys) ? action.keys : []).map(String);

const keyLabel = (action: JsonRecord) => keyValues(action).join(" + ");

const actionLabel = (action: JsonRecord) => {
  const kind = actionName(action);
  const x = number(action.x);
  const y = number(action.y);
  if (kind.includes("click")) {
    return x != null && y != null ? `Click at ${x} × ${y}` : "Click";
  }
  if (kind.includes("type")) {
    const value = text(action.text);
    const preview = value.replace(/\s+/g, " ").slice(0, 54);
    return preview
      ? `Type “${preview}${value.length > 54 ? "…" : ""}”`
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

const actionKindLabel = (action: JsonRecord) => {
  const kind = actionName(action);
  if (kind.includes("type")) return "Text input";
  if (kind === "key" || kind.includes("hotkey")) return "Keyboard input";
  if (kind.includes("click")) return "Pointer input";
  if (kind.includes("scroll")) return "Scroll input";
  if (kind.includes("drag")) return "Pointer drag";
  if (kind.includes("move")) return "Pointer move";
  if (kind.includes("observe") || kind.includes("screen")) return "Screen capture";
  return kind.replaceAll("_", " ");
};

const actionSequence = (actions: readonly JsonRecord[]) =>
  actions.map((action) => actionLabel(action)).join(" → ");

const summarize = (toolName: string, args: JsonRecord) => {
  const actions = Array.isArray(args.actions) ? args.actions.map(record) : [];
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
      Icon: ShieldAlertIcon,
      tone: "border-amber-400/25 bg-amber-400/8 text-amber-200",
    };
  }
  if (status?.type === "running") {
    return {
      label: "Sending input",
      Icon: LoaderCircleIcon,
      tone: "border-sky-400/20 bg-sky-400/8 text-sky-200",
    };
  }
  if (status?.type === "incomplete") {
    return {
      label: status.reason === "cancelled" ? "Cancelled" : "Failed",
      Icon: XIcon,
      tone: "border-rose-400/25 bg-rose-400/8 text-rose-200",
    };
  }
  const value = record(result);
  const resultStatus = text(value.status);
  const verification = record(value.verification);
  if (resultStatus === "refused") {
    return {
      label: "Refused safely",
      Icon: CircleAlertIcon,
      tone: "border-amber-400/20 bg-amber-400/8 text-amber-200",
    };
  }
  if (resultStatus === "unverified") {
    return {
      label: "Not verified",
      Icon: EyeIcon,
      tone: "border-amber-400/20 bg-amber-400/8 text-amber-200",
    };
  }
  if (text(verification.verdict) === "verified") {
    return {
      label: "Verified",
      Icon: CheckIcon,
      tone: "border-emerald-400/20 bg-emerald-400/8 text-emerald-200",
    };
  }
  return {
    label: "Input complete",
    Icon: CheckIcon,
    tone: "border-border bg-muted/30 text-muted-foreground",
  };
};

const pointerDetail = (action: JsonRecord) => {
  const x = number(action.x);
  const y = number(action.y);
  const button = text(action.button);
  return [
    button ? `${button} button` : "",
    x != null ? `x ${x}` : "",
    y != null ? `y ${y}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
};

function ActionExactInput({ action }: { action: JsonRecord }) {
  const kind = actionName(action);
  if (kind.includes("type")) {
    const value = text(action.text);
    if (!value) return null;
    return (
      <pre
        aria-label="Exact text input"
        className="mt-2 max-h-36 overflow-auto rounded-md border border-border/70 bg-background/70 px-3 py-2 font-mono text-[11px] leading-relaxed text-foreground/90 whitespace-pre-wrap"
      >
        {value}
      </pre>
    );
  }
  if (kind === "key" || kind.includes("hotkey")) {
    const keys = keyValues(action);
    if (!keys.length) return null;
    return (
      <div
        className="mt-2 flex flex-wrap items-center gap-1.5"
        aria-label={`Exact key input: ${keys.join(" plus ")}`}
      >
        {keys.map((key, index) => (
          <span key={`${key}:${index}`} className="contents">
            {index > 0 ? (
              <span className="text-muted-foreground text-[10px]">+</span>
            ) : null}
            <kbd className="min-w-7 rounded border border-border bg-background px-2 py-1 text-center font-mono text-[11px] font-medium text-foreground shadow-[inset_0_-1px_0_rgba(255,255,255,0.06)]">
              {key}
            </kbd>
          </span>
        ))}
      </div>
    );
  }
  const detail = pointerDetail(action);
  return detail ? (
    <p className="mt-1 font-mono text-[11px] text-muted-foreground">
      {detail}
    </p>
  ) : null;
}

export function ComputerInputSequence({
  actions,
}: {
  actions: readonly JsonRecord[];
}) {
  return (
    <ol className="mt-2" aria-label="Exact computer input sequence">
      {actions.map((action, index) => {
        const Icon = actionIcon(actionName(action));
        return (
          <li
            key={`${actionName(action)}:${index}`}
            className="relative grid grid-cols-[2rem_minmax(0,1fr)] gap-2 pb-3 last:pb-0"
          >
            {index < actions.length - 1 ? (
              <span
                className="absolute top-8 bottom-0 left-[0.9375rem] w-px bg-border/80"
                aria-hidden="true"
              />
            ) : null}
            <span className="relative z-10 flex size-8 items-center justify-center rounded-md border border-border bg-muted/40 text-muted-foreground">
              <Icon className="size-3.5" aria-hidden="true" />
            </span>
            <div className="min-w-0 pt-0.5">
              <div className="flex min-w-0 items-baseline gap-2">
                <span className="text-[10px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
                  {index + 1}
                </span>
                <p className="truncate text-sm font-medium">
                  {actionKindLabel(action)}
                </p>
              </div>
              <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                {actionLabel(action)}
              </p>
              <ActionExactInput action={action} />
            </div>
          </li>
        );
      })}
    </ol>
  );
}

type EvidenceItem = {
  label: string;
  value: string;
  detail?: string;
  tone?: string;
};

function EvidenceStrip({
  args,
  result,
  status,
  environment,
}: {
  args: JsonRecord;
  result: unknown;
  status: ToolCallMessagePartProps["status"];
  environment: ComputerToolEnvironment;
}) {
  const value = record(result);
  const verification = record(value.verification);
  const resultStatus = text(value.status);
  const sourceWorld = number(args.based_on_world_version);
  const sourceControl = number(args.based_on_control_epoch);
  const frame = number(value.frame_id);
  const observedWorld = number(value.world_version);
  const verificationVerdict = text(verification.verdict);
  const verificationSummary = text(verification.summary);

  let delivery = "Waiting";
  let deliveryDetail = "No input has been committed";
  let deliveryTone = "text-muted-foreground";
  if (status?.type === "requires-action") {
    delivery = "Held for approval";
    deliveryDetail = "The consequential input has not been sent";
    deliveryTone = "text-amber-200";
  } else if (status?.type === "running") {
    delivery = "In progress";
    deliveryDetail = "The harness is sending this bounded input";
    deliveryTone = "text-sky-200";
  } else if (resultStatus === "failed") {
    delivery = "Failed";
    deliveryDetail = text(value.error) || "The input did not complete";
    deliveryTone = "text-rose-200";
  } else if (resultStatus === "refused") {
    delivery = "Refused";
    deliveryDetail = text(value.reason) || "Stopped before input";
    deliveryTone = "text-amber-200";
  } else if (resultStatus) {
    delivery = "Committed";
    deliveryDetail = frame != null ? `Fresh post-input frame ${frame}` : "HID completed";
    deliveryTone = "text-foreground";
  }

  const evidence: EvidenceItem[] = [
    {
      label: "Target",
      value: environment.machineName || "Managed computer",
      detail:
        environment.currentFrameId != null
          ? `current frame ${environment.currentFrameId}`
          : "managed MCP session",
    },
    {
      label: "Precondition",
      value:
        sourceWorld != null
          ? `World ${sourceWorld}`
          : "Freshness not supplied",
      detail:
        sourceControl != null
          ? `control epoch ${sourceControl}`
          : "no control epoch",
    },
    {
      label: "Delivery",
      value: delivery,
      detail: deliveryDetail,
      tone: deliveryTone,
    },
    {
      label: "Screen check",
      value:
        verificationVerdict === "verified"
          ? "Verified"
          : resultStatus === "unverified"
            ? "Not verified"
            : frame != null
              ? "Frame captured"
              : "Pending",
      detail:
        verificationSummary ||
        (frame != null
          ? [`frame ${frame}`, observedWorld != null ? `world ${observedWorld}` : ""]
              .filter(Boolean)
              .join(" · ")
          : "Awaiting post-input evidence"),
      tone:
        verificationVerdict === "verified"
          ? "text-emerald-200"
          : resultStatus === "unverified"
            ? "text-amber-200"
            : "text-muted-foreground",
    },
  ];

  return (
    <dl className="mt-4 grid border-y border-border/70 sm:grid-cols-2">
      {evidence.map((item, index) => (
        <div
          key={item.label}
          className={cn(
            "min-w-0 py-2.5 sm:px-3",
            index % 2 === 0 ? "sm:pl-0" : "sm:border-l",
            index < evidence.length - 1 && "border-b",
            index >= 2 && "sm:border-b-0",
          )}
        >
          <dt className="text-[10px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
            {item.label}
          </dt>
          <dd className={cn("mt-1 truncate text-xs font-medium", item.tone)}>
            {item.value}
          </dd>
          {item.detail ? (
            <dd className="mt-0.5 line-clamp-2 text-[11px] leading-relaxed text-muted-foreground">
              {item.detail}
            </dd>
          ) : null}
        </div>
      ))}
    </dl>
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
  const environment = useContext(ComputerToolEnvironmentContext);
  const needsApproval = status?.type === "requires-action";
  const resultStatus = text(record(result).status);
  const failed = status?.type === "incomplete" || resultStatus === "failed";
  const needsReview = resultStatus === "unverified";
  const [open, setOpen] = useState(needsApproval || failed || needsReview);
  const elapsed = useToolCallElapsed();
  const callArgs = record(args);
  const summary = useMemo(
    () => summarize(toolName, callArgs),
    [args, toolName],
  );
  const state = statusMeta(status, result);
  const StateIcon = state.Icon;
  const PrimaryIcon = actionIcon(actionName(summary.actions[0] ?? {}));
  const approvalContext = approval?.options?.find(
    (option) =>
      option.kind === "allow-once" || option.kind === "allow-always",
  )?.description;
  const characters = summary.actions.reduce(
    (total, action) => total + text(action.text).length,
    0,
  );

  useEffect(() => {
    if (needsApproval || failed || needsReview) setOpen(true);
  }, [failed, needsApproval, needsReview]);

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className={cn(
        "group/computer-tool overflow-hidden rounded-lg border bg-muted/[0.08] transition-colors",
        needsApproval
          ? "border-amber-400/35 bg-amber-400/[0.035]"
          : needsReview
            ? "border-amber-400/25"
            : failed
              ? "border-rose-400/30"
              : "border-border/80",
      )}
    >
      <CollapsibleTrigger className="group/trigger flex w-full items-start gap-3 px-3 py-2.5 text-left outline-none hover:bg-muted/20 focus-visible:ring-2 focus-visible:ring-ring/60 focus-visible:ring-inset">
        <span
          className={cn(
            "mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-md border",
            needsApproval
              ? "border-amber-400/25 bg-amber-400/10 text-amber-200"
              : "border-border bg-muted/40 text-muted-foreground",
          )}
        >
          <PrimaryIcon className="size-3.5" aria-hidden="true" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex min-w-0 items-center gap-2">
            <span className="truncate text-sm font-medium">{summary.title}</span>
            {durationLabel(elapsed) ? (
              <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground">
                {durationLabel(elapsed)}
              </span>
            ) : null}
          </span>
          <span className="mt-0.5 block truncate text-xs text-muted-foreground">
            {summary.detail}
          </span>
        </span>
        <span
          className={cn(
            "flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-1 text-[11px] font-medium",
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
          className="mt-1 size-4 shrink-0 -rotate-90 text-muted-foreground transition-transform group-data-open/trigger:rotate-0 group-data-panel-open/trigger:rotate-0"
          aria-hidden="true"
        />
      </CollapsibleTrigger>

      <CollapsibleContent className="data-closed:animate-collapsible-up data-open:animate-collapsible-down overflow-hidden">
        <div className="border-t border-border/60 px-3 pt-3 pb-3 sm:px-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-[10px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
                Exact input sequence
              </p>
              <p className="mt-0.5 text-[11px] text-muted-foreground">
                {summary.actions.length}{" "}
                {summary.actions.length === 1 ? "input" : "inputs"}
                {characters ? ` · ${characters} characters` : ""}
              </p>
            </div>
            {environment.onOpenComputer ? (
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="h-7 shrink-0 px-2 text-xs"
                onClick={environment.onOpenComputer}
              >
                <MonitorIcon data-icon="inline-start" />
                Current screen
              </Button>
            ) : null}
          </div>

          <ComputerInputSequence actions={summary.actions} />

          {needsApproval ? (
            <div className="mt-4 rounded-md border border-amber-400/25 bg-amber-400/[0.06] p-3">
              <div className="flex items-start gap-2.5">
                <ShieldAlertIcon
                  className="mt-0.5 size-4 shrink-0 text-amber-200"
                  aria-hidden="true"
                />
                <div className="min-w-0">
                  <p className="text-sm font-semibold text-amber-100">
                    Held before a consequential input
                  </p>
                  <p className="mt-1 max-w-xl text-xs leading-relaxed text-amber-100/75">
                    {approvalContext ||
                      "This may cause an external or difficult-to-reverse action. Review the exact input and target state before allowing it once."}
                  </p>
                  <ToolFallbackApproval
                    className="mt-3"
                    addResult={addResult}
                    resume={resume}
                    interrupt={interrupt}
                    approval={approval}
                    respondToApproval={respondToApproval}
                  />
                </div>
              </div>
            </div>
          ) : null}

          <EvidenceStrip
            args={callArgs}
            result={result}
            status={status}
            environment={environment}
          />

          <Collapsible className="group/arguments mt-2">
            <CollapsibleTrigger className="flex w-full items-center justify-between rounded-md py-2 text-xs text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/60">
              <span>
                Exact MCP arguments ·{" "}
                <code className="font-mono">{toolName}</code>
              </span>
              <ChevronDownIcon className="size-3.5 -rotate-90 transition-transform group-data-open/arguments:rotate-0 group-data-panel-open/arguments:rotate-0" />
            </CollapsibleTrigger>
            <CollapsibleContent className="data-closed:animate-collapsible-up data-open:animate-collapsible-down overflow-hidden">
              <pre className="mt-1 max-h-72 overflow-auto rounded-md border border-border/70 bg-background/70 p-3 font-mono text-[11px] leading-relaxed text-foreground/80 whitespace-pre-wrap">
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
      <CollapsibleTrigger className="group/trigger flex w-full items-center gap-2 rounded-md py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring/60">
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
          <span className="truncate text-xs text-muted-foreground">
            {needsAttention
              ? "Approval is waiting inside"
              : active
                ? "Input sequence is running"
                : "Inspect exact inputs and screen evidence"}
          </span>
        </span>
        <ChevronDownIcon className="size-4 -rotate-90 text-muted-foreground transition-transform group-data-open/trigger:rotate-0 group-data-panel-open/trigger:rotate-0" />
      </CollapsibleTrigger>
      <ToolGroupContent className="[&>div]:gap-2">
        {children}
      </ToolGroupContent>
    </ToolGroupRoot>
  );
}
