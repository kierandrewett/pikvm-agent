import {
  useAuiState,
  type ToolCallMessagePartComponent,
  type ToolCallMessagePartProps,
} from "@assistant-ui/react";
import {
  lazy,
  Suspense,
  type PropsWithChildren,
} from "react";
import {
  DefaultToolGroup,
  type ThreadGroupPart,
} from "@/components/assistant-ui/thread";
import { Skeleton } from "@/components/ui/skeleton";

const MANAGED_COMPUTER_TOOLS = new Set([
  "computer_start_task",
  "computer_status",
  "computer_continue",
  "computer_pause",
  "computer_abort",
]);

/** Tools that get the rich computer rendering rather than the bare fallback.
 *  `computer_*` are what the ASSISTANT calls and therefore what appears in a
 *  turn; `pikvm_*` are the raw MCP calls the harness drives underneath. */
export const belongsToComputerActivity = (toolName: string) =>
  toolName.startsWith("pikvm_") || MANAGED_COMPUTER_TOOLS.has(toolName);

const LazyComputerToolCall = lazy(async () => {
  const module = await import("@/components/workspace/computer-tool-call");
  return { default: module.ComputerToolCall };
});

const LazyComputerToolGroup = lazy(async () => {
  const module = await import("@/components/workspace/computer-tool-call");
  return { default: module.ComputerToolGroup };
});

function ComputerActivitySkeleton() {
  return (
    <div
      className="my-3 flex min-h-14 items-center gap-3"
      role="status"
      aria-label="Loading computer activity"
    >
      <Skeleton className="size-8 shrink-0" />
      <Skeleton className="h-3 w-36" />
    </div>
  );
}

const DeferredComputerToolCallImpl = (
  props: ToolCallMessagePartProps,
) => (
  <Suspense fallback={<ComputerActivitySkeleton />}>
    <LazyComputerToolCall {...props} />
  </Suspense>
);

export const DeferredComputerToolCall =
  DeferredComputerToolCallImpl as ToolCallMessagePartComponent;

export function DeferredComputerToolGroup({
  group,
  children,
  inputCount,
}: PropsWithChildren<{
  group: ThreadGroupPart;
  inputCount?: number;
}>) {
  return (
    <Suspense fallback={<ComputerActivitySkeleton />}>
      <LazyComputerToolGroup group={group} inputCount={inputCount}>
        {children}
      </LazyComputerToolGroup>
    </Suspense>
  );
}

export function DeferredWorkspaceToolGroup({
  group,
  children,
}: PropsWithChildren<{ group: ThreadGroupPart }>) {
  const content = useAuiState((state) => state.message.content);
  const inputCount = group.indices.reduce((total, index) => {
    const part = content[index];
    if (
      part?.type !== "tool-call" ||
      !part.toolName.startsWith("pikvm_") ||
      !part.args ||
      typeof part.args !== "object" ||
      Array.isArray(part.args)
    ) {
      return total;
    }
    const actions = (part.args as Record<string, unknown>).actions;
    return total + (Array.isArray(actions) ? actions.length : 0);
  }, 0);
  const computerOnly =
    group.indices.length > 0 &&
    group.indices.every((index) => {
      const part = content[index];
      return (
        part?.type === "tool-call" &&
        belongsToComputerActivity(part.toolName)
      );
    });

  if (!computerOnly) {
    return <DefaultToolGroup group={group}>{children}</DefaultToolGroup>;
  }
  return (
    <DeferredComputerToolGroup
      group={group}
      inputCount={inputCount || undefined}
    >
      {children}
    </DeferredComputerToolGroup>
  );
}
