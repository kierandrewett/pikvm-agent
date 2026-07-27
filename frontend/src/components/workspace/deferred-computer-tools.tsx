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
}: PropsWithChildren<{ group: ThreadGroupPart }>) {
  return (
    <Suspense fallback={<ComputerActivitySkeleton />}>
      <LazyComputerToolGroup group={group}>{children}</LazyComputerToolGroup>
    </Suspense>
  );
}

export function DeferredWorkspaceToolGroup({
  group,
  children,
}: PropsWithChildren<{ group: ThreadGroupPart }>) {
  const content = useAuiState((state) => state.message.content);
  const computerOnly =
    group.indices.length > 0 &&
    group.indices.every((index) => {
      const part = content[index];
      return (
        part?.type === "tool-call" &&
        part.toolName.startsWith("pikvm_")
      );
    });

  if (!computerOnly) {
    return <DefaultToolGroup group={group}>{children}</DefaultToolGroup>;
  }
  return (
    <DeferredComputerToolGroup group={group}>
      {children}
    </DeferredComputerToolGroup>
  );
}
