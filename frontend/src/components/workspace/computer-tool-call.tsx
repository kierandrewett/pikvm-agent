import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ElementType,
  type PropsWithChildren,
} from "react";
import {
  CheckIcon,
  ChevronDownIcon,
  CircleAlertIcon,
  CommandIcon,
  CrosshairIcon,
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

const ComputerToolEnvironmentContext = createContext<ComputerToolEnvironment>(
  {},
);

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

const isKeyboardAction = (kind: string) =>
  kind === "key" || kind.includes("keypress") || kind.includes("hotkey");

const actionIcon = (kind: string) => {
  if (kind.includes("type")) return KeyboardIcon;
  if (isKeyboardAction(kind)) return CommandIcon;
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
  if (isKeyboardAction(kind)) {
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
  if (isKeyboardAction(kind)) return "Keyboard input";
  if (kind.includes("click")) return "Pointer input";
  if (kind.includes("scroll")) return "Scroll input";
  if (kind.includes("drag")) return "Pointer drag";
  if (kind.includes("move")) return "Pointer move";
  if (kind.includes("observe") || kind.includes("screen"))
    return "Screen capture";
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
      tone: "text-amber-200",
    };
  }
  if (status?.type === "running") {
    return {
      label: "Sending input",
      Icon: LoaderCircleIcon,
      tone: "text-sky-200",
    };
  }
  if (status?.type === "incomplete") {
    return {
      label: status.reason === "cancelled" ? "Cancelled" : "Failed",
      Icon: XIcon,
      tone: "text-rose-200",
    };
  }
  const value = record(result);
  const resultStatus = text(value.status);
  const verification = record(value.verification);
  if (resultStatus === "refused") {
    return {
      label: "Refused safely",
      Icon: CircleAlertIcon,
      tone: "text-amber-200",
    };
  }
  if (resultStatus === "unverified") {
    return {
      label: "Not verified",
      Icon: EyeIcon,
      tone: "text-amber-200",
    };
  }
  if (text(verification.verdict) === "verified") {
    return {
      label: "Verified",
      Icon: CheckIcon,
      tone: "text-emerald-200",
    };
  }
  return {
    label: "Input complete",
    Icon: CheckIcon,
    tone: "text-muted-foreground",
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
    const lineCount = value.split(/\r\n|\r|\n/).length;
    return (
      <div className="mt-2 overflow-hidden rounded-md border border-border/70 bg-background/55">
        <div className="flex items-center justify-between gap-3 border-b border-border/60 px-3 py-1.5">
          <span className="text-[10px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
            Typed payload
          </span>
          <span className="shrink-0 font-mono text-[10px] tabular-nums text-muted-foreground">
            {value.length} chars · {lineCount}{" "}
            {lineCount === 1 ? "line" : "lines"}
          </span>
        </div>
        <pre
          aria-label="Exact text input"
          className="max-h-40 overflow-auto px-3 py-2.5 font-mono text-[11px] leading-relaxed text-foreground/90 whitespace-pre-wrap"
        >
          {value}
        </pre>
      </div>
    );
  }
  if (isKeyboardAction(kind)) {
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
    <div
      className="mt-2 flex flex-wrap items-center gap-1.5"
      aria-label={`Exact pointer input: ${detail}`}
    >
      <CrosshairIcon
        className="mr-0.5 size-3 text-muted-foreground"
        aria-hidden="true"
      />
      {detail.split(" · ").map((part) => (
        <code
          key={part}
          className="rounded border border-border/70 bg-background/60 px-1.5 py-0.5 font-mono text-[10px] text-foreground/85"
        >
          {part}
        </code>
      ))}
    </div>
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
            className="relative grid grid-cols-[1.5rem_minmax(0,1fr)] gap-2 pb-3 last:pb-0"
          >
            {index < actions.length - 1 ? (
              <span
                className="absolute top-6 bottom-0 left-[0.71875rem] w-px bg-border/70"
                aria-hidden="true"
              />
            ) : null}
            <span className="relative z-10 flex size-6 items-center justify-center bg-background text-muted-foreground">
              <Icon className="size-3.5" aria-hidden="true" />
            </span>
            <div className="min-w-0">
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
  Icon: ElementType;
};

export function ComputerActionReceipt({
  args,
  result,
  status,
  environment,
  actionCount,
  characterCount,
}: {
  args: JsonRecord;
  result: unknown;
  status: ToolCallMessagePartProps["status"];
  environment: ComputerToolEnvironment;
  actionCount: number;
  characterCount: number;
}) {
  const value = record(result);
  const verification = record(value.verification);
  const resultStatus = text(value.status);
  const sourceFrame = number(args.based_on_frame_id);
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
    deliveryDetail =
      frame != null ? `Fresh post-input frame ${frame}` : "HID completed";
    deliveryTone = "text-foreground";
  }

  const evidence: EvidenceItem[] = [
    {
      label: "Source screen",
      value:
        sourceFrame != null
          ? `Frame ${sourceFrame}`
          : sourceWorld != null
            ? `World ${sourceWorld}`
            : "Not supplied",
      detail:
        sourceWorld != null
          ? [
              sourceFrame != null ? `world ${sourceWorld}` : "",
              sourceControl != null ? `control ${sourceControl}` : "",
            ]
              .filter(Boolean)
              .join(" · ") || "Freshness reference supplied"
          : "No freshness reference",
      Icon: EyeIcon,
    },
    {
      label: "Bounded input",
      value: `${actionCount} ${actionCount === 1 ? "input" : "inputs"}`,
      detail: characterCount
        ? `${characterCount} exact characters`
        : "Keyboard or pointer transaction",
      Icon: KeyboardIcon,
    },
    {
      label: "Delivery",
      value: delivery,
      detail: deliveryDetail,
      tone: deliveryTone,
      Icon:
        status?.type === "running"
          ? LoaderCircleIcon
          : status?.type === "requires-action"
            ? ShieldAlertIcon
            : resultStatus === "failed"
              ? XIcon
              : CheckIcon,
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
          ? [
              `frame ${frame}`,
              observedWorld != null ? `world ${observedWorld}` : "",
            ]
              .filter(Boolean)
              .join(" · ")
          : "Awaiting post-input evidence"),
      tone:
        verificationVerdict === "verified"
          ? "text-emerald-200"
          : resultStatus === "unverified"
            ? "text-amber-200"
            : "text-muted-foreground",
      Icon:
        verificationVerdict === "verified"
          ? CheckIcon
          : resultStatus === "unverified"
            ? CircleAlertIcon
            : EyeIcon,
    },
  ];

  return (
    <section
      className="mt-4 border-y border-border/70 py-3"
      aria-label="Computer action receipt"
    >
      <div className="mb-3 flex min-w-0 items-center gap-2 text-xs">
        <MonitorIcon
          className="size-3.5 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <span className="truncate font-medium">
          {environment.machineName || "Managed computer"}
        </span>
        <span className="text-muted-foreground" aria-hidden="true">
          ·
        </span>
        <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
          {environment.currentFrameId != null
            ? `current frame ${environment.currentFrameId}`
            : "managed MCP session"}
        </span>
      </div>
      <dl className="grid gap-0 sm:grid-cols-4">
        {evidence.map((item, index) => {
          const Icon = item.Icon;
          return (
            <div
              key={item.label}
              className="relative grid min-w-0 grid-cols-[1.75rem_minmax(0,1fr)] gap-2 pb-3 last:pb-0 sm:block sm:pb-0 sm:pr-3"
            >
              {index < evidence.length - 1 ? (
                <>
                  <span
                    className="absolute top-7 bottom-0 left-[0.6875rem] w-px bg-border sm:hidden"
                    aria-hidden="true"
                  />
                  <span
                    className="absolute top-[0.6875rem] right-1 left-7 hidden h-px bg-border sm:block"
                    aria-hidden="true"
                  />
                </>
              ) : null}
              <span
                className={cn(
                  "relative z-10 flex size-[1.375rem] items-center justify-center rounded-full border border-border bg-background text-muted-foreground",
                  item.tone,
                )}
              >
                <Icon
                  className={cn(
                    "size-3",
                    status?.type === "running" &&
                      item.label === "Delivery" &&
                      "animate-spin motion-reduce:animate-none",
                  )}
                  aria-hidden="true"
                />
              </span>
              <div className="min-w-0 sm:mt-2">
                <dt className="text-[10px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
                  {item.label}
                </dt>
                <dd
                  className={cn("mt-1 truncate text-xs font-medium", item.tone)}
                >
                  {item.value}
                </dd>
                {item.detail ? (
                  <dd className="mt-0.5 line-clamp-2 text-[11px] leading-relaxed text-muted-foreground">
                    {item.detail}
                  </dd>
                ) : null}
              </div>
            </div>
          );
        })}
      </dl>
    </section>
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
    (option) => option.kind === "allow-once" || option.kind === "allow-always",
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
        "group/computer-tool border-l-2 pl-3 transition-colors motion-reduce:transition-none",
        needsApproval
          ? "border-amber-400/60"
          : needsReview
            ? "border-amber-400/45"
            : failed
              ? "border-rose-400/50"
              : "border-border/70",
      )}
    >
      <CollapsibleTrigger className="group/trigger flex w-full items-start gap-2.5 py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring/60">
        <span
          className={cn(
            "mt-0.5 flex size-5 shrink-0 items-center justify-center",
            needsApproval ? "text-amber-200" : "text-muted-foreground",
          )}
        >
          <PrimaryIcon className="size-3.5" aria-hidden="true" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex min-w-0 items-center gap-2">
            <span className="truncate text-sm font-medium">
              {summary.title}
            </span>
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
            "flex shrink-0 items-center gap-1.5 rounded-full border border-current/15 bg-current/5 px-2 py-1 text-[10px] font-semibold",
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
        <div className="border-t border-border/50 pt-3 pb-2">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-[10px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
                Input transaction
              </p>
              <p className="mt-0.5 text-[11px] text-muted-foreground">
                Inspect exactly what crossed the keyboard and pointer boundary.
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

          <ComputerActionReceipt
            args={callArgs}
            result={result}
            status={status}
            environment={environment}
            actionCount={summary.actions.length}
            characterCount={characters}
          />

          <div className="mt-4">
            <p className="text-[10px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
              Exact input sequence
            </p>
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              {summary.actions.length}{" "}
              {summary.actions.length === 1 ? "input" : "inputs"}
              {characters ? ` · ${characters} characters` : ""}
            </p>
          </div>
          <ComputerInputSequence actions={summary.actions} />

          {needsApproval ? (
            <div className="mt-4 border-l-2 border-amber-400/60 pl-3">
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
      <ToolGroupContent className="[&>div]:gap-2">{children}</ToolGroupContent>
    </ToolGroupRoot>
  );
}
