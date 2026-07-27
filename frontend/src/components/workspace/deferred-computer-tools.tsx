import type {
  ToolCallMessagePartComponent,
  ToolCallMessagePartProps,
} from "@assistant-ui/react";
import {
  lazy,
  Suspense,
  type PropsWithChildren,
} from "react";
import type { ThreadGroupPart } from "@/components/assistant-ui/thread";
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
