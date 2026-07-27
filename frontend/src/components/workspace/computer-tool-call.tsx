import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
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
  LockKeyholeIcon,
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
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Skeleton } from "@/components/ui/skeleton";
import { harnessBlob } from "@/lib/harness-api";
import { cn } from "@/lib/utils";

type JsonRecord = Record<string, unknown>;

export type ComputerToolEnvironment = {
  token?: string;
  runId?: string;
  machineName?: string;
  currentFrameId?: number;
  screenWidth?: number;
  screenHeight?: number;
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

type ReceiptContext = {
  intent: string;
  expectedEvidence: string[];
  attempt?: number;
  latencyMs?: number;
  idempotencyKey: string;
  evidenceRevision?: number;
  evidenceBeforeFrame?: number;
  evidenceAfterFrame?: number;
  controller?: ModelReceipt;
  verifier?: ModelReceipt;
  inputReceipts: InputReceipt[];
};

type ModelReceipt = {
  provider: string;
  model: string;
  latencyMs?: number;
};

export type InputReceipt = {
  index?: number;
  type: string;
  status: string;
  verdict: string;
  observed_text: string;
  observed_text_redacted: boolean;
  typed_characters?: number;
  intended_characters?: number;
  correction_count?: number;
  delivery_retries?: number;
  used_fast_path: boolean;
  summary?: string;
  edit_distance?: number;
  focus_evidence: string;
  intended_sha256?: string;
  acknowledged_prefix_sha256?: string;
  observed_sha256?: string;
  exact_sha256_match?: boolean;
};

const inputReceipt = (value: unknown): InputReceipt => {
  const item = record(value);
  return {
    index: number(item.index),
    type: text(item.type),
    status: text(item.status),
    verdict: text(item.verdict),
    observed_text: text(item.observed_text),
    observed_text_redacted: item.observed_text_redacted === true,
    typed_characters: number(item.typed_characters),
    intended_characters: number(item.intended_characters),
    correction_count: number(item.correction_count),
    delivery_retries: number(item.delivery_retries),
    used_fast_path: item.used_fast_path === true,
    summary: text(item.summary),
    edit_distance: number(item.edit_distance),
    focus_evidence: text(item.focus_evidence),
    intended_sha256: text(item.intended_sha256),
    acknowledged_prefix_sha256: text(item.acknowledged_prefix_sha256),
    observed_sha256: text(item.observed_sha256),
    exact_sha256_match:
      typeof item.exact_sha256_match === "boolean"
        ? item.exact_sha256_match
        : undefined,
  };
};

const modelReceipt = (value: unknown): ModelReceipt | undefined => {
  const item = record(value);
  const provider = text(item.provider);
  const model = text(item.model);
  if (!provider && !model) return undefined;
  return {
    provider,
    model,
    latencyMs: number(item.latency_ms),
  };
};

const receiptContext = (args: JsonRecord): ReceiptContext => {
  const receipt = record(args.__receipt);
  return {
    intent: text(receipt.intent),
    expectedEvidence: Array.isArray(receipt.expected_evidence)
      ? receipt.expected_evidence.map(String)
      : [],
    attempt: number(receipt.attempt),
    latencyMs: number(receipt.latency_ms),
    idempotencyKey: text(receipt.idempotency_key),
    evidenceRevision: number(receipt.evidence_revision),
    evidenceBeforeFrame: number(receipt.evidence_before_frame_id),
    evidenceAfterFrame: number(receipt.evidence_after_frame_id),
    controller: modelReceipt(receipt.controller),
    verifier: modelReceipt(receipt.verifier),
    inputReceipts: Array.isArray(receipt.input_receipts)
      ? receipt.input_receipts.map(inputReceipt)
      : [],
  };
};

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
    const click = kind.includes("double") ? "Double-click" : "Click";
    const target = text(action.target_text);
    if (target) return `${click} “${target}”`;
    return x != null && y != null ? `${click} at ${x} × ${y}` : click;
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
    const direction = text(action.direction);
    const steps = number(action.amount);
    const delta = number(action.delta_y) ?? number(action.dy);
    if (direction) {
      return `Scroll ${direction}${steps != null ? ` ${steps} steps` : ""}`;
    }
    return delta == null
      ? "Scroll"
      : `Scroll ${delta < 0 ? "up" : "down"} ${Math.abs(delta)} px`;
  }
  if (kind.includes("drag")) return "Drag pointer";
  if (kind.includes("move")) {
    return x != null && y != null ? `Move to ${x} × ${y}` : "Move pointer";
  }
  if (kind === "wait") {
    const duration = number(action.ms) ?? number(action.duration_ms);
    return duration != null ? `Wait ${duration} ms` : "Wait";
  }
  if (kind === "wait_for_stable_screen") {
    const stable = number(action.stable_ms);
    const timeout = number(action.timeout_ms);
    return `Wait for a stable screen${stable != null ? ` · ${stable} ms stable` : ""}${timeout != null ? ` · ${timeout} ms limit` : ""}`;
  }
  if (kind === "wait_for_change") {
    const timeout = number(action.timeout_ms);
    return `Wait for screen change${timeout != null ? ` · ${timeout} ms limit` : ""}`;
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
      detail: actionKindLabel(actions[0]!),
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

type ReceiptBadgeVariant =
  "outline" | "evidence" | "caution" | "info" | "destructive";

const statusTextClass = (variant: ReceiptBadgeVariant) =>
  variant === "destructive"
    ? "text-destructive"
    : variant === "caution"
      ? "text-caution-foreground"
      : variant === "info"
        ? "text-info-foreground"
        : variant === "evidence"
          ? "text-evidence-foreground"
          : "text-muted-foreground";

const statusMeta = (
  status: ToolCallMessagePartProps["status"],
  result: unknown,
) => {
  if (status?.type === "requires-action") {
    return {
      label: "Approval needed",
      Icon: ShieldAlertIcon,
      variant: "caution" as ReceiptBadgeVariant,
      iconClass: "bg-caution-soft text-caution-foreground",
    };
  }
  if (status?.type === "running") {
    return {
      label: "Sending input",
      Icon: LoaderCircleIcon,
      variant: "info" as ReceiptBadgeVariant,
      iconClass: "bg-info-soft text-info-foreground",
    };
  }
  if (status?.type === "incomplete") {
    return {
      label: status.reason === "cancelled" ? "Cancelled" : "Failed",
      Icon: XIcon,
      variant: "destructive" as ReceiptBadgeVariant,
      iconClass: "bg-destructive/10 text-destructive",
    };
  }
  const value = record(result);
  const resultStatus = text(value.status);
  const verification = record(value.verification);
  if (resultStatus === "failed") {
    return {
      label: "Failed",
      Icon: XIcon,
      variant: "destructive" as ReceiptBadgeVariant,
      iconClass: "bg-destructive/10 text-destructive",
    };
  }
  if (resultStatus === "refused") {
    return {
      label: "Refused safely",
      Icon: CircleAlertIcon,
      variant: "caution" as ReceiptBadgeVariant,
      iconClass: "bg-caution-soft text-caution-foreground",
    };
  }
  if (resultStatus === "unverified") {
    return {
      label: "Not verified",
      Icon: EyeIcon,
      variant: "caution" as ReceiptBadgeVariant,
      iconClass: "bg-caution-soft text-caution-foreground",
    };
  }
  if (text(verification.verdict) === "verified") {
    return {
      label: "Verified",
      Icon: CheckIcon,
      variant: "evidence" as ReceiptBadgeVariant,
      iconClass: "bg-evidence-soft text-evidence-foreground",
    };
  }
  return {
    label: "Input complete",
    Icon: CheckIcon,
    variant: "outline" as ReceiptBadgeVariant,
    iconClass: "bg-muted text-muted-foreground",
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

function PointerTargetMap({
  action,
  environment,
}: {
  action: JsonRecord;
  environment: ComputerToolEnvironment;
}) {
  const x = number(action.x);
  const y = number(action.y);
  if (x == null || y == null) return null;
  const width = environment.screenWidth;
  const height = environment.screenHeight;
  const left =
    width && width > 0 ? Math.min(100, Math.max(0, (x / width) * 100)) : 50;
  const top =
    height && height > 0 ? Math.min(100, Math.max(0, (y / height) * 100)) : 50;
  const coordinateLabel = `${x}, ${y}`;
  const screenLabel =
    width && height
      ? `${width} × ${height} screen`
      : "screen dimensions unknown";

  return (
    <figure
      className="mt-2 flex flex-wrap items-center gap-3"
      aria-label={`Pointer target ${coordinateLabel} on ${screenLabel}`}
    >
      <div className="relative aspect-video w-32 overflow-hidden rounded-md border border-border bg-background">
        <div
          className="absolute size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border border-info bg-info-soft"
          style={{ left: `${left}%`, top: `${top}%` }}
        >
          <span className="absolute top-1/2 left-1/2 size-px -translate-x-1/2 -translate-y-1/2 bg-info-foreground" />
        </div>
        <span className="absolute inset-x-0 bottom-0 truncate border-t border-border bg-muted/80 px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground">
          {screenLabel}
        </span>
      </div>
      <figcaption className="flex min-w-0 flex-col gap-1">
        <span className="text-xs font-medium">Target coordinate</span>
        <span className="font-mono text-[11px] tabular-nums text-muted-foreground">
          x {x} · y {y}
        </span>
        {width && height ? (
          <span className="text-[11px] text-muted-foreground">
            {left.toFixed(1)}% across · {top.toFixed(1)}% down
          </span>
        ) : null}
      </figcaption>
    </figure>
  );
}

const readbackMeta = (receipt: InputReceipt) => {
  if (
    receipt.focus_evidence === "focus_lost" ||
    receipt.status === "failed_focus_lost"
  ) {
    return {
      label: "Focus lost",
      Icon: CrosshairIcon,
      variant: "destructive" as ReceiptBadgeVariant,
    };
  }
  if (receipt.observed_text_redacted) {
    return {
      label: "Text not retained",
      Icon: LockKeyholeIcon,
      variant: "outline" as ReceiptBadgeVariant,
    };
  }
  if (receipt.exact_sha256_match === true) {
    return {
      label: "Exact read-back",
      Icon: CheckIcon,
      variant: "evidence" as ReceiptBadgeVariant,
    };
  }
  if (
    receipt.status === "verified_safe_normalized" &&
    receipt.exact_sha256_match === false
  ) {
    return {
      label: "Normalized only",
      Icon: EyeIcon,
      variant: "caution" as ReceiptBadgeVariant,
    };
  }
  if (
    receipt.status.startsWith("verified_") ||
    receipt.verdict === "match" ||
    receipt.verdict === "contains"
  ) {
    return {
      label: "Read-back matches",
      Icon: CheckIcon,
      variant: "evidence" as ReceiptBadgeVariant,
    };
  }
  if (
    receipt.verdict === "mismatch" ||
    receipt.status.startsWith("failed_")
  ) {
    return {
      label: "Read-back differs",
      Icon: XIcon,
      variant: "destructive" as ReceiptBadgeVariant,
    };
  }
  return {
    label:
      receipt.status === "delivered_unverified"
        ? "Delivery only"
        : "Read-back uncertain",
    Icon: EyeIcon,
    variant: "caution" as ReceiptBadgeVariant,
  };
};

function TypingReadback({
  receipt,
  actionIndex,
}: {
  receipt?: InputReceipt;
  actionIndex: number;
}) {
  if (!receipt) return null;
  const meta = readbackMeta(receipt);
  const MetaIcon = meta.Icon;
  const fingerprint =
    receipt.exact_sha256_match && receipt.intended_sha256
      ? `Exact SHA-256 ${receipt.intended_sha256.slice(0, 12)}`
      : receipt.intended_sha256 && receipt.observed_sha256
        ? `SHA-256 ${receipt.intended_sha256.slice(0, 12)} ≠ ${receipt.observed_sha256.slice(0, 12)}`
        : receipt.intended_sha256
          ? `Payload SHA-256 ${receipt.intended_sha256.slice(0, 12)}`
          : "";
  const metrics = [
    receipt.typed_characters != null &&
    receipt.intended_characters != null
      ? `${receipt.typed_characters} / ${receipt.intended_characters} chars`
      : "",
    receipt.edit_distance != null
      ? `${receipt.edit_distance} ${receipt.edit_distance === 1 ? "edit" : "edits"}`
      : "",
    receipt.correction_count
      ? `${receipt.correction_count} ${
          receipt.correction_count === 1 ? "correction" : "corrections"
        }`
      : "",
    receipt.delivery_retries
      ? `${receipt.delivery_retries} ${
          receipt.delivery_retries === 1 ? "delivery retry" : "delivery retries"
        }`
      : "",
    receipt.used_fast_path ? "guarded fast transport" : "",
    fingerprint,
  ].filter(Boolean);

  return (
    <section
      className="mt-3 border-t border-border/60 pt-2.5"
      aria-label={`Typing read-back for action ${actionIndex + 1}`}
    >
      <div className="flex min-w-0 items-center justify-between gap-3">
        <span className="flex min-w-0 items-center gap-2 text-xs font-medium text-muted-foreground">
          <EyeIcon className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="truncate">Read-back from the target field</span>
        </span>
        <Badge variant={meta.variant}>
          <MetaIcon data-icon="inline-start" aria-hidden="true" />
          {meta.label}
        </Badge>
      </div>
      {receipt.observed_text_redacted ? (
        <p className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
          <LockKeyholeIcon className="size-3.5 shrink-0" aria-hidden="true" />
          No read-back text retained for secret input
        </p>
      ) : receipt.observed_text ? (
        <pre className="mt-2 max-h-40 overflow-auto font-mono text-[11px] leading-relaxed text-foreground/90 whitespace-pre-wrap">
          {receipt.observed_text}
        </pre>
      ) : (
        <p className="mt-2 text-xs text-muted-foreground">
          {meta.label === "Focus lost"
            ? "No reliable field text was observed before input stopped."
            : "OCR did not return reliable field text."}
        </p>
      )}
      {metrics.length ? (
        <p className="mt-2 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[10px] tabular-nums text-muted-foreground">
          {metrics.map((metric) => (
            <span key={metric}>{metric}</span>
          ))}
        </p>
      ) : null}
    </section>
  );
}

function ActionExactInput({
  action,
  environment,
  inputReceipt,
  actionIndex,
}: {
  action: JsonRecord;
  environment: ComputerToolEnvironment;
  inputReceipt?: InputReceipt;
  actionIndex: number;
}) {
  const kind = actionName(action);
  if (kind.includes("type")) {
    const value = text(action.text);
    if (!value) return null;
    const secret = action.secret === true || action.redacted === true;
    const lineCount = value.split(/\r\n|\r|\n/).length;
    const characterCount = inputReceipt?.intended_characters ?? value.length;
    return (
      <div className="mt-2">
        <div className="flex items-center justify-between gap-3">
          <span className="text-xs font-medium text-muted-foreground">
            Exact typed payload
          </span>
          <span className="shrink-0 font-mono text-[10px] tabular-nums text-muted-foreground">
            {characterCount} chars · {lineCount}{" "}
            {lineCount === 1 ? "line" : "lines"}
          </span>
        </div>
        {secret ? (
          <p
            aria-label="Exact text input"
            className="mt-2 flex items-center gap-2 border-l-2 border-border pl-3 text-xs text-muted-foreground"
          >
            <LockKeyholeIcon className="size-3.5 shrink-0" aria-hidden="true" />
            Secret payload redacted before it entered the run record
          </p>
        ) : (
          <pre
            aria-label="Exact text input"
            className="mt-2 max-h-40 overflow-auto border-l-2 border-border py-1 pl-3 font-mono text-[11px] leading-relaxed text-foreground/90 whitespace-pre-wrap"
          >
            {value}
          </pre>
        )}
        <TypingReadback receipt={inputReceipt} actionIndex={actionIndex} />
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
    <>
      <div
        className="mt-2 flex flex-wrap items-center gap-1.5"
        aria-label={`Exact pointer input: ${detail}`}
      >
        <CrosshairIcon className="mr-0.5 size-3" aria-hidden="true" />
        {detail.split(" · ").map((part) => (
          <code
            key={part}
            className="font-mono text-[10px] text-foreground"
          >
            {part}
          </code>
        ))}
      </div>
      <PointerTargetMap action={action} environment={environment} />
    </>
  ) : null;
}

export function ComputerInputSequence({
  actions,
  environment = {},
  inputReceipts = [],
}: {
  actions: readonly JsonRecord[];
  environment?: ComputerToolEnvironment;
  inputReceipts?: readonly InputReceipt[];
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
            <span className="relative flex size-6 items-center justify-center text-muted-foreground">
              <Icon className="size-3.5" aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <div className="flex min-w-0 items-baseline gap-2">
                <p className="truncate text-sm font-medium">
                  {actionKindLabel(action)}
                </p>
                {actions.length > 1 ? (
                  <span className="font-mono text-[10px] text-muted-foreground">
                    {index + 1}/{actions.length}
                  </span>
                ) : null}
              </div>
              {actionName(action).includes("type") ? null : (
                <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                  {actionLabel(action)}
                </p>
              )}
              <ActionExactInput
                action={action}
                environment={environment}
                inputReceipt={inputReceipts.find(
                  (receipt) => receipt.index === index,
                )}
                actionIndex={index}
              />
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function AuditRow({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="grid min-w-0 grid-cols-[4rem_minmax(0,1fr)] gap-2 text-xs">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-pretty text-foreground/90">{value}</dd>
    </div>
  );
}

const modelIdentity = (detail?: ModelReceipt) =>
  detail ? detail.model || detail.provider || "Model not reported" : "";

const modelAuditSummary = (receipt: ReceiptContext) => {
  const controller = modelIdentity(receipt.controller);
  const verifier = modelIdentity(receipt.verifier);
  const sameModel =
    controller &&
    verifier &&
    controller === verifier &&
    receipt.controller?.provider === receipt.verifier?.provider;
  const latency = [receipt.controller?.latencyMs, receipt.verifier?.latencyMs]
    .filter((value): value is number => value != null)
    .map((value) => `${value.toLocaleString()} ms`)
    .join(" + ");

  const route = sameModel
    ? `${controller} · selected + checked`
    : controller && verifier
      ? `${controller} → ${verifier}`
      : controller
        ? `${controller} · screen check pending`
        : verifier
          ? `${verifier} · checked`
          : "";
  return [route, latency].filter(Boolean).join(" · ");
};

function VerificationEvidenceFigure({
  revision,
  environment,
  beforeFrame,
  afterFrame,
}: {
  revision?: number;
  environment: ComputerToolEnvironment;
  beforeFrame?: number;
  afterFrame?: number;
}) {
  const [imageUrl, setImageUrl] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (revision == null || !environment.token || !environment.runId) {
      setImageUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return "";
      });
      return;
    }
    setImageUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return "";
    });
    setError("");
    let active = true;
    let currentUrl = "";
    const controller = new AbortController();
    void harnessBlob(
      environment.token,
      `/api/runs/${encodeURIComponent(environment.runId)}/verification-images/${revision}`,
      controller.signal,
    )
      .then((blob) => {
        if (!active) return;
        currentUrl = URL.createObjectURL(blob);
        setImageUrl(currentUrl);
        setError("");
      })
      .catch((cause) => {
        if (!active || controller.signal.aborted) return;
        setError(
          cause instanceof Error
            ? cause.message
            : "Visual evidence is unavailable.",
        );
      });
    return () => {
      active = false;
      controller.abort();
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, [environment.runId, environment.token, revision]);

  if (revision == null || !environment.token || !environment.runId) return null;
  const frameLabel = [
    beforeFrame != null ? `frame ${beforeFrame}` : "before",
    afterFrame != null ? `frame ${afterFrame}` : "after",
  ].join(" → ");

  return (
    <figure
      className="mt-3 max-w-lg overflow-hidden rounded-lg border border-border bg-background"
      aria-label="Before and after screen evidence"
    >
      <figcaption className="flex min-w-0 items-center justify-between gap-3 border-b border-border px-3 py-2">
        <span className="flex min-w-0 items-center gap-2 text-xs font-semibold">
          <EyeIcon
            className="size-3.5 shrink-0 text-evidence-foreground"
            aria-hidden="true"
          />
          <span className="truncate">Screen change</span>
        </span>
        <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
          {frameLabel}
        </span>
      </figcaption>
      {imageUrl ? (
        <img
          src={imageUrl}
          alt={`Before and after screen evidence, ${frameLabel}`}
          className="block max-h-64 w-full bg-black object-contain"
        />
      ) : error ? (
        <p className="px-3 py-4 text-xs text-muted-foreground">
          Visual evidence could not be loaded. The action and verifier receipt
          remain available.
        </p>
      ) : (
        <Skeleton
          className="aspect-[32/9] w-full rounded-none"
          aria-label="Loading before and after screen evidence"
        />
      )}
    </figure>
  );
}

export function ComputerActionReceipt({
  args,
  result,
  status,
  environment,
  actionCount,
  characterCount,
  showVisualEvidence = true,
  showInputSummary = true,
}: {
  args: JsonRecord;
  result: unknown;
  status: ToolCallMessagePartProps["status"];
  environment: ComputerToolEnvironment;
  actionCount: number;
  characterCount: number;
  showVisualEvidence?: boolean;
  showInputSummary?: boolean;
}) {
  const value = record(result);
  const verification = record(value.verification);
  const resultStatus = text(value.status);
  const receipt = receiptContext(args);
  const sourceFrame =
    number(args.based_on_frame_id) ?? receipt.evidenceBeforeFrame;
  const sourceWorld = number(args.based_on_world_version);
  const frame = number(value.frame_id) ?? receipt.evidenceAfterFrame;
  const observedWorld = number(value.world_version);
  const verificationVerdict = text(verification.verdict);
  const verificationSummary = text(verification.summary);
  const actions = Array.isArray(args.actions) ? args.actions.map(record) : [];
  const verified = verificationVerdict === "verified";

  let delivery = "Waiting";
  if (status?.type === "requires-action") {
    delivery = "Held for approval";
  } else if (status?.type === "running") {
    delivery = "In progress";
  } else if (resultStatus === "failed") {
    delivery = "Failed";
  } else if (resultStatus === "refused") {
    delivery = "Refused";
  } else if (resultStatus) {
    delivery = "Committed";
  }

  const goal = [
    receipt.intent,
    !verified && receipt.expectedEvidence.length
      ? `Success: ${receipt.expectedEvidence.join("; ")}`
      : "",
  ]
    .filter(Boolean)
    .join(" · ");
  const input = [
    delivery,
    showInputSummary && actions.length
      ? actionSequence(actions)
      : `${actionCount} ${actionCount === 1 ? "input" : "inputs"}`,
    characterCount ? `${characterCount} chars` : "",
    receipt.attempt != null && receipt.attempt > 1
      ? `attempt ${receipt.attempt}`
      : "",
    receipt.latencyMs != null
      ? `${receipt.latencyMs.toLocaleString()} ms`
      : "",
  ]
    .filter(Boolean)
    .join(" · ");
  const trace = [
    environment.machineName || "Managed computer",
    environment.screenWidth && environment.screenHeight
      ? `${environment.screenWidth}×${environment.screenHeight}`
      : "",
    sourceFrame != null && frame != null
      ? `frame ${sourceFrame} → ${frame}`
      : sourceFrame != null || frame != null
        ? `frame ${sourceFrame ?? frame}`
        : "",
    sourceFrame == null && frame == null && (sourceWorld != null || observedWorld != null)
      ? sourceWorld != null && observedWorld != null
        ? `world ${sourceWorld} → ${observedWorld}`
        : `world ${sourceWorld ?? observedWorld}`
      : "",
  ]
    .filter(Boolean)
    .join(" · ");
  const models = modelAuditSummary(receipt);
  const outcome =
    verificationSummary && (!verified || !receipt.intent)
      ? verificationSummary
      : resultStatus === "unverified"
      ? "Not verified · screen captured without an independent check."
      : resultStatus === "failed"
        ? "Transport failed · details are retained in diagnostics."
        : resultStatus === "refused"
          ? text(value.reason) || "Stopped before input."
          : "";

  return (
    <section
      className="mt-2"
      aria-label="Computer action receipt"
    >
      <dl
        className="grid gap-1.5 border-y border-border/60 py-2.5"
        aria-label="Action audit summary"
      >
        {goal ? <AuditRow label="Goal" value={goal} /> : null}
        <AuditRow label="Input" value={input} />
        <AuditRow label="Trace" value={trace} />
        {models ? <AuditRow label="Models" value={models} /> : null}
        {outcome ? <AuditRow label="Result" value={outcome} /> : null}
      </dl>
      {showVisualEvidence ? (
        <VerificationEvidenceFigure
          revision={receipt.evidenceRevision}
          environment={environment}
          beforeFrame={sourceFrame}
          afterFrame={frame}
        />
      ) : null}
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
  const running = status?.type === "running";
  const resultStatus = text(record(result).status);
  const failed = status?.type === "incomplete" || resultStatus === "failed";
  const needsReview = resultStatus === "unverified";
  const [open, setOpen] = useState(
    needsApproval || running || failed || needsReview,
  );
  const wasRunning = useRef(running);
  const elapsed = useToolCallElapsed();
  const callArgs = record(args);
  const receipt = receiptContext(callArgs);
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
  const visibleDuration = receipt.latencyMs ?? elapsed;
  const resultValue = record(result);
  const beforeFrame =
    number(callArgs.based_on_frame_id) ?? receipt.evidenceBeforeFrame;
  const afterFrame =
    number(resultValue.frame_id) ?? receipt.evidenceAfterFrame;
  const showPrimaryInput =
    needsApproval ||
    summary.actions.length > 1 ||
    summary.actions.some((action) => actionName(action).includes("type"));

  useEffect(() => {
    const inputJustFinished = wasRunning.current && !running;
    if (needsApproval || running || failed || needsReview) {
      setOpen(true);
    } else if (inputJustFinished) {
      setOpen(false);
    }
    wasRunning.current = running;
  }, [failed, needsApproval, needsReview, running]);

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      data-computer-state={
        needsApproval
          ? "approval"
          : needsReview
            ? "review"
            : failed
              ? "failed"
              : running
                ? "running"
                : "complete"
      }
      className={cn(
        "computer-action-step group/computer-tool relative",
      )}
    >
      <CollapsibleTrigger className="group/trigger grid min-h-12 w-full grid-cols-[2rem_minmax(0,1fr)_auto] items-start gap-x-2.5 py-2 text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/50 sm:grid-cols-[2rem_minmax(0,1fr)_auto_auto]">
        <span className="relative row-span-2 flex w-8 shrink-0 items-start justify-center">
          <span
            className={cn(
              "computer-action-marker relative z-10 flex size-6 items-center justify-center bg-background text-muted-foreground",
              needsApproval || needsReview
                ? "text-caution-foreground"
                : failed
                  ? "text-destructive"
                  : running
                    ? "text-info-foreground"
                    : state.variant === "evidence"
                      ? "text-evidence-foreground"
                      : "",
            )}
          >
            <PrimaryIcon className="size-3.5" aria-hidden="true" />
            <span className="computer-action-index absolute -right-1.5 -bottom-1 bg-background px-0.5 font-mono text-[8px] leading-3 tabular-nums text-muted-foreground" />
          </span>
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-semibold">
            {summary.title}
          </span>
          <span className="mt-1 flex min-w-0 items-center gap-1.5 text-[11px] text-muted-foreground group-data-open/computer-tool:hidden">
            <span className="truncate">{summary.detail}</span>
            <span aria-hidden="true">·</span>
            <code className="shrink-0 font-mono">
              {toolName.replace(/^pikvm_/, "")}
            </code>
            {durationLabel(visibleDuration) ? (
              <>
                <span aria-hidden="true">·</span>
                <span className="shrink-0 tabular-nums">
                  {durationLabel(visibleDuration)}
                </span>
              </>
            ) : null}
            {receipt.controller?.model ? (
              <>
                <span aria-hidden="true">·</span>
                <span
                  className="hidden min-w-0 truncate sm:inline"
                  title={[receipt.controller.model, receipt.controller.provider]
                    .filter(Boolean)
                    .join(" · ")}
                >
                  {receipt.controller.model}
                </span>
              </>
            ) : null}
          </span>
        </span>
        <span
          className={cn(
            "col-start-2 row-start-2 mt-1 flex items-center gap-1.5 text-xs font-medium sm:col-start-3 sm:row-start-1 sm:mt-0.5",
            statusTextClass(state.variant),
          )}
        >
          <StateIcon
            className={cn(
              "size-3.5",
              running && "animate-spin motion-reduce:animate-none",
            )}
            aria-hidden="true"
          />
          {state.label}
        </span>
        <ChevronDownIcon
          className="col-start-3 row-start-1 mt-1 size-4 shrink-0 -rotate-90 text-muted-foreground transition-transform duration-150 group-data-open/trigger:rotate-0 group-data-panel-open/trigger:rotate-0 motion-reduce:transition-none sm:col-start-4"
          aria-hidden="true"
        />
      </CollapsibleTrigger>

      <CollapsibleContent className="data-closed:animate-collapsible-up data-open:animate-collapsible-down overflow-hidden motion-reduce:animate-none">
        <div className="mt-1 border-t border-border/60 pt-2 pb-1 sm:ml-10">
          <VerificationEvidenceFigure
            revision={receipt.evidenceRevision}
            environment={environment}
            beforeFrame={beforeFrame}
            afterFrame={afterFrame}
          />

          {showPrimaryInput ? (
            <section className="mt-3" aria-label="Exact input">
              <ComputerInputSequence
                actions={summary.actions}
                environment={environment}
                inputReceipts={receipt.inputReceipts}
              />
            </section>
          ) : null}

          {failed ? (
            <p className="mt-3 text-xs text-destructive">
              This input failed. Diagnostic detail is retained below.
            </p>
          ) : null}

          {needsApproval ? (
            <Alert variant="caution" className="mt-3">
              <ShieldAlertIcon aria-hidden="true" />
              <AlertTitle>Held before a consequential input</AlertTitle>
              <AlertDescription>
                <p>
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
              </AlertDescription>
            </Alert>
          ) : null}

          <div className="mt-2 flex items-start gap-1">
            <Collapsible className="group/details min-w-0 flex-1">
              <CollapsibleTrigger className="flex min-h-9 w-full items-center gap-1.5 text-xs text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
                <ChevronDownIcon className="size-3.5 -rotate-90 transition-transform duration-150 group-data-open/details:rotate-0 group-data-panel-open/details:rotate-0 motion-reduce:transition-none" />
                Audit
              </CollapsibleTrigger>
              <CollapsibleContent className="data-closed:animate-collapsible-up data-open:animate-collapsible-down overflow-hidden motion-reduce:animate-none">
                <ComputerActionReceipt
                  args={callArgs}
                  result={result}
                  status={status}
                  environment={environment}
                  actionCount={summary.actions.length}
                  characterCount={characters}
                  showVisualEvidence={false}
                  showInputSummary={!showPrimaryInput}
                />
                <Collapsible className="group/raw">
                  <CollapsibleTrigger className="flex min-h-9 w-full items-center gap-1.5 text-[11px] text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
                    <ChevronDownIcon className="size-3 -rotate-90 transition-transform duration-150 group-data-open/raw:rotate-0 group-data-panel-open/raw:rotate-0 motion-reduce:transition-none" />
                    <span>Raw request</span>
                    <code className="font-mono">
                      {toolName.replace(/^pikvm_/, "")}
                    </code>
                  </CollapsibleTrigger>
                  <CollapsibleContent className="data-closed:animate-collapsible-up data-open:animate-collapsible-down overflow-hidden motion-reduce:animate-none">
                    <pre className="max-h-72 overflow-auto rounded-md bg-background/70 p-3 font-mono text-[11px] leading-relaxed text-foreground/80 whitespace-pre-wrap">
                      {argsText || JSON.stringify(args, null, 2)}
                    </pre>
                  </CollapsibleContent>
                </Collapsible>
              </CollapsibleContent>
            </Collapsible>
            {environment.onOpenComputer ? (
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="h-9 shrink-0 px-2 text-xs text-muted-foreground"
                onClick={environment.onOpenComputer}
              >
                <MonitorIcon data-icon="inline-start" />
                Screen
              </Button>
            ) : null}
          </div>
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
      className="my-3"
    >
      <CollapsibleTrigger
        aria-label={`${count} computer actions, ${
          needsAttention
            ? "approval waiting"
            : active
              ? "input sequence running"
              : "inspect exact inputs and screen evidence"
        }`}
        className="group/trigger flex min-h-12 w-full items-center gap-2 border-y border-border/60 text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        <span
          className={cn(
            "flex size-7 shrink-0 items-center justify-center",
            needsAttention
              ? "text-caution-foreground"
              : active
                ? "text-info-foreground"
                : "text-muted-foreground",
          )}
        >
          {needsAttention ? (
            <LockKeyholeIcon className="size-3.5" />
          ) : active ? (
            <LoaderCircleIcon className="size-3.5 animate-spin motion-reduce:animate-none" />
          ) : (
            <MousePointer2Icon className="size-3.5" />
          )}
        </span>
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="flex min-w-0 items-baseline gap-2">
            <span className="truncate text-sm font-semibold">
              Computer activity
            </span>
            <span className="shrink-0 font-mono text-[10px] tabular-nums text-muted-foreground">
              {count} {count === 1 ? "action" : "actions"}
            </span>
          </span>
          <span className="truncate text-[11px] text-muted-foreground">
            {needsAttention
              ? "A consequential input is waiting for you"
              : active
                ? "Current input stays open while the screen changes"
                : "Exact inputs, target read-back, and visual proof"}
          </span>
        </span>
        {needsAttention ? (
          <span className="text-xs font-medium text-caution-foreground">
            Review
          </span>
        ) : active ? (
          <span className="text-xs font-medium text-info-foreground">
            Input live
          </span>
        ) : null}
        <ChevronDownIcon className="size-4 -rotate-90 text-muted-foreground transition-transform duration-150 group-data-open/trigger:rotate-0 group-data-panel-open/trigger:rotate-0 motion-reduce:transition-none" />
      </CollapsibleTrigger>
      <ToolGroupContent className="computer-action-list pb-2 [&>div]:gap-1">
        {children}
      </ToolGroupContent>
    </ToolGroupRoot>
  );
}
