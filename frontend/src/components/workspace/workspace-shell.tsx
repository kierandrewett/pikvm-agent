import {
  lazy,
  type ReactNode,
  Suspense,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  AssistantRuntimeProvider,
  type ExternalStoreAdapter,
  type ExternalStoreThreadListAdapter,
  type ThreadMessageLike,
  type ToolCallMessagePartProps,
  useExternalStoreRuntime,
} from "@assistant-ui/react";
import {
  ActivityIcon,
  BotIcon,
  GalleryVerticalEndIcon,
  LogOutIcon,
  MenuIcon,
  MonitorIcon,
  MoreHorizontalIcon,
  SparklesIcon,
} from "lucide-react";
import { Thread } from "@/components/assistant-ui/thread";
import { ThreadList } from "@/components/assistant-ui/thread-list";
import { ToolFallback } from "@/components/assistant-ui/tool-fallback";
import { TooltipIconButton } from "@/components/assistant-ui/tooltip-icon-button";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { AuthDialog } from "@/components/workspace/auth-dialog";
import { ComputerToolEnvironmentProvider } from "@/components/workspace/computer-tool-environment";
import {
  belongsToComputerActivity,
  DeferredComputerToolCall as ComputerToolCall,
  DeferredWorkspaceToolGroup as WorkspaceToolGroup,
} from "@/components/workspace/deferred-computer-tools";
import {
  hasFreshRunActivity,
  LiveUpdateBadge,
} from "@/components/workspace/live-update-badge";
import { ModelPicker } from "@/components/workspace/model-picker";
import {
  canComposeIntoRun,
  DirectRunBanner,
  RunControlModeBadge,
  usesManagedControlLoop,
} from "@/components/workspace/run-control-mode";
import { RunProvenance } from "@/components/workspace/run-provenance";
import { UiUpdateBadge } from "@/components/workspace/ui-update-badge";
import { useHarnessWorkspace } from "@/hooks/use-harness-workspace";
import { messagesForRun } from "@/lib/run-messages";

const ComputerSheet = lazy(async () => {
  const module = await import("@/components/workspace/computer-sheet");
  return { default: module.ComputerSheet };
});

const DiagnosticsSheet = lazy(async () => {
  const module = await import("@/components/workspace/diagnostics-sheet");
  return { default: module.DiagnosticsSheet };
});

const ProviderConnectionsSheet = lazy(async () => {
  const module = await import(
    "@/components/workspace/provider-connections-sheet"
  );
  return { default: module.ProviderConnectionsSheet };
});

const ShowcaseSheet = lazy(async () => {
  const module = await import("@/components/workspace/showcase-sheet");
  return { default: module.ShowcaseSheet };
});

const useDeferredMount = (open: boolean) => {
  const [mounted, setMounted] = useState(open);
  useEffect(() => {
    if (open) setMounted(true);
  }, [open]);
  return mounted || open;
};

/* The assistant calls `computer_*`; `pikvm_*` is the raw MCP the harness drives
 * underneath and never appears in a turn. Matching on the pikvm_ prefix alone
 * meant EVERY computer tool call the user actually sees fell through to the
 * bare "Used tool: …" fallback, with the rich rendering never once used. */
const WorkspaceToolCall = (props: ToolCallMessagePartProps) =>
  belongsToComputerActivity(props.toolName) ? (
    <ComputerToolCall {...props} />
  ) : (
    <ToolFallback {...props} />
  );

function RuntimeInstance({
  adapter,
  children,
}: {
  adapter: ExternalStoreAdapter<ThreadMessageLike>;
  children: ReactNode;
}) {
  const runtime = useExternalStoreRuntime(adapter);
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      {children}
    </AssistantRuntimeProvider>
  );
}

export function WorkspaceRuntimeBoundary({
  runtimeKey,
  adapter,
  children,
}: {
  runtimeKey: string;
  adapter: ExternalStoreAdapter<ThreadMessageLike>;
  children: ReactNode;
}) {
  return (
    <RuntimeInstance key={runtimeKey} adapter={adapter}>
      {children}
    </RuntimeInstance>
  );
}

function SheetLoading({
  open,
  onOpenChange,
  title,
  side = "right",
  className,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  side?: "left" | "right";
  className?: string;
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side={side} className={className}>
        <SheetHeader>
          <SheetTitle>{title}</SheetTitle>
          <SheetDescription>Loading…</SheetDescription>
        </SheetHeader>
        <div
          className="flex flex-1 flex-col gap-3 px-4"
          role="status"
          aria-label={`Loading ${title.toLowerCase()}`}
        >
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      </SheetContent>
    </Sheet>
  );
}

function TaskRestoreState({
  task,
  restoring,
}: {
  task: string;
  restoring: boolean;
}) {
  return (
    <div
      className="mx-auto flex h-full w-full max-w-(--thread-max-width) flex-col justify-center px-6"
      role={restoring ? "status" : undefined}
      aria-label={restoring ? "Restoring task" : "Task unavailable"}
    >
      <div className="mx-auto w-full max-w-md rounded-xl border border-border/70 bg-muted/20 p-5">
        <p className="text-sm font-medium">
          {restoring ? "Restoring task" : "Task unavailable"}
        </p>
        <p className="mt-2 truncate text-sm text-foreground">{task}</p>
        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
          {restoring
            ? "Loading the saved conversation. No model work will restart."
            : "The saved conversation could not be loaded. Retry it from the task list or start a new task."}
        </p>
      </div>
    </div>
  );
}

export function WorkspaceShell() {
  const workspace = useHarnessWorkspace();
  const [computerOpen, setComputerOpen] = useState(false);
  const [modelsOpen, setModelsOpen] = useState(false);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [tasksOpen, setTasksOpen] = useState(false);
  const [showcaseOpen, setShowcaseOpen] = useState(false);
  const computerMounted = useDeferredMount(computerOpen);
  const modelsMounted = useDeferredMount(modelsOpen);
  const diagnosticsMounted = useDeferredMount(diagnosticsOpen);
  const showcaseMounted = useDeferredMount(showcaseOpen);
  const selectedSummary = workspace.selectedId
    ? workspace.runs.find((run) => run.run_id === workspace.selectedId)
    : undefined;
  const selectedTaskTitle =
    workspace.selectedRun?.task || selectedSummary?.task || "New chat";
  const selectedRunPending = Boolean(
    workspace.selectedId &&
      workspace.selectedRun?.run_id !== workspace.selectedId,
  );
  const managedControl = usesManagedControlLoop(
    workspace.selectedRun?.origin,
  );

  useEffect(() => {
    if (!managedControl) setModelsOpen(false);
  }, [managedControl]);

  const messages = useMemo(
    () => messagesForRun(workspace.selectedRun),
    [workspace.selectedRun],
  );
  const computerEnvironment = useMemo(() => {
    const machine = workspace.selectedRun?.observation?.machine;
    return {
      token: workspace.token,
      runId: workspace.selectedRun?.run_id,
      machineName:
        machine && typeof machine.alias === "string"
          ? machine.alias
          : "Managed computer",
      currentFrameId: workspace.selectedRun?.observation?.frame_id ?? undefined,
      screenWidth: workspace.selectedRun?.observation?.width ?? undefined,
      screenHeight: workspace.selectedRun?.observation?.height ?? undefined,
      onOpenComputer: () => setComputerOpen(true),
    };
  }, [
    workspace.selectedRun?.run_id,
    workspace.selectedRun?.observation?.frame_id,
    workspace.selectedRun?.observation?.height,
    workspace.selectedRun?.observation?.machine,
    workspace.selectedRun?.observation?.width,
    workspace.token,
  ]);
  const threadList = useMemo<ExternalStoreThreadListAdapter>(
    () => ({
      threadId: workspace.selectedId ?? undefined,
      threads: workspace.runs.map((run) => ({
        id: run.run_id,
        remoteId: run.run_id,
        status: "regular",
        title: run.task,
      })),
      archivedThreads: [],
      onSwitchToNewThread: () => {
        workspace.newThread();
        setTasksOpen(false);
      },
      onSwitchToThread: async (threadId) => {
        setTasksOpen(false);
        await workspace.selectRun(threadId);
      },
    }),
    [
      workspace.newThread,
      workspace.runs,
      workspace.selectRun,
      workspace.selectedId,
    ],
  );
  const runtimeAdapter: ExternalStoreAdapter<ThreadMessageLike> = {
    messages,
    convertMessage: (message) => message,
    isRunning: workspace.isRunning,
    isSendDisabled:
      selectedRunPending ||
      !canComposeIntoRun(
        workspace.connected,
        workspace.selectedRun?.origin,
      ),
    onNew: workspace.onNew,
    onCancel: workspace.onCancel,
    onRespondToToolApproval: workspace.respondToApproval,
    adapters: { threadList },
    unstable_capabilities: { copy: true },
  };

  const ComposerToolbar = () => (
    <div className="flex min-w-0 items-center gap-1">
      {managedControl ? (
        <ModelPicker
          providers={workspace.providers}
          preferences={workspace.modelPreferences}
          activeRoute={workspace.selectedRun?.model_route}
          activeProvider={workspace.selectedRun?.model_provider}
          locked={workspace.routeLocked}
          onPreferenceChange={workspace.setModelPreference}
          onResetPreferences={workspace.resetModelPreferences}
          onOpenModels={() => setModelsOpen(true)}
        />
      ) : null}
    </div>
  );


  return (
    <WorkspaceRuntimeBoundary
      runtimeKey={workspace.selectedId ?? "new-task"}
      adapter={runtimeAdapter}
    >
      <div className="workspace-shell">
        <aside className="workspace-rail">
          {/* A compact rail header, VS Code style: a section label rather than
              a brand block. The window title already says what this is. */}
          <div className="flex h-10 items-center gap-2 px-3">
            <SparklesIcon
              className="size-3.5 text-muted-foreground"
              aria-hidden="true"
            />
            <span className="text-[11px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
              Sessions
            </span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
            <ThreadList />
          </div>
        </aside>

        <main className="min-w-0 bg-background">
          <header className="workspace-header">
            <div className="flex min-w-0 items-center gap-2">
              <TooltipIconButton
                tooltip="Tasks"
                aria-label="Open tasks"
                className="md:hidden"
                onClick={() => setTasksOpen(true)}
              >
                <MenuIcon />
              </TooltipIconButton>
              <p className="truncate text-sm font-medium">
                {selectedTaskTitle}
              </p>
              {/* Only states that change what the user should do next earn a
                  place beside the title. A healthy live connection is the
                  expected case and says nothing, so it stays silent; the badge
                  appears when the stream degrades. Provenance and the UI-update
                  prompt are rare, and keep their own conditional badges. */}
              {workspace.selectedRun?.origin === "direct_mcp" ? (
                <>
                  <RunControlModeBadge origin={workspace.selectedRun.origin} />
                  <RunProvenance caller={workspace.selectedRun.caller} />
                </>
              ) : null}
              {workspace.connected ? (
                <>
                  {selectedRunPending ? (
                    <Badge
                      variant={
                        workspace.restoringRun ? "outline" : "destructive"
                      }
                      aria-live="polite"
                    >
                      {workspace.restoringRun
                        ? "Restoring"
                        : "Task unavailable"}
                    </Badge>
                  ) : workspace.selectedRun &&
                    workspace.liveUpdateStatus !== "live" ? (
                    <LiveUpdateBadge status={workspace.liveUpdateStatus} />
                  ) : null}
                  <UiUpdateBadge />
                </>
              ) : null}
            </div>
            {/* One overflow menu rather than a row of five icons. Everything
                here is occasional; the frequent controls live by the composer,
                where the work happens. */}
            <div className="flex shrink-0 items-center gap-1">
              <DropdownMenu>
                <DropdownMenuTrigger
                  render={
                    <TooltipIconButton
                      tooltip="More"
                      aria-label="More workspace actions"
                    >
                      <MoreHorizontalIcon />
                    </TooltipIconButton>
                  }
                />
                <DropdownMenuContent>
                  {managedControl ? (
                    <DropdownMenuItem
                      disabled={!workspace.connected}
                      onClick={() => setModelsOpen(true)}
                    >
                      <BotIcon aria-hidden="true" />
                      Models
                    </DropdownMenuItem>
                  ) : null}
                  <DropdownMenuItem
                    disabled={!workspace.connected}
                    onClick={() => setComputerOpen(true)}
                  >
                    <MonitorIcon aria-hidden="true" />
                    Computer
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    disabled={!workspace.connected}
                    onClick={() => setShowcaseOpen(true)}
                  >
                    <GalleryVerticalEndIcon aria-hidden="true" />
                    Recorded proof
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    disabled={!workspace.selectedRun}
                    onClick={() => setDiagnosticsOpen(true)}
                  >
                    <ActivityIcon aria-hidden="true" />
                    Diagnostics
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    variant="destructive"
                    disabled={!workspace.connected}
                    onClick={workspace.disconnect}
                  >
                    <LogOutIcon aria-hidden="true" />
                    Disconnect
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          </header>

          <section className="workspace-thread" aria-label="Agent conversation">
            {workspace.error && workspace.connected ? (
              <Alert variant="destructive" className="workspace-error">
                <AlertDescription>{workspace.error}</AlertDescription>
              </Alert>
            ) : null}
            {selectedRunPending ? (
              <TaskRestoreState
                task={selectedTaskTitle}
                restoring={workspace.restoringRun}
              />
            ) : (
              <>
                {workspace.selectedRun?.origin === "direct_mcp" &&
                !workspace.error ? (
                  <DirectRunBanner
                    caller={workspace.selectedRun.caller}
                    onStartManaged={workspace.newThread}
                  />
                ) : null}
                <ComputerToolEnvironmentProvider value={computerEnvironment}>
                  <div className="flex min-h-0 flex-1 flex-col">
                    <Thread
                      readOnly={!managedControl}
                      activity={workspace.selectedRun?.active_activity}
                      working={
                        workspace.isRunning &&
                        hasFreshRunActivity(workspace.liveUpdateStatus)
                      }
                      components={{
                        ComposerToolbar,
                        ToolFallback: WorkspaceToolCall,
                        ToolGroup: WorkspaceToolGroup,
                      }}
                    />
                  </div>
                </ComputerToolEnvironmentProvider>
              </>
            )}
          </section>
        </main>
      </div>

      <Sheet open={tasksOpen} onOpenChange={setTasksOpen}>
        <SheetContent side="left" className="w-80">
          <SheetHeader>
            <SheetTitle>Tasks</SheetTitle>
            <SheetDescription>
              Switch conversations or start a new task.
            </SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-4">
            <ThreadList />
          </div>
        </SheetContent>
      </Sheet>

      {computerMounted ? (
        <Suspense
          fallback={
            <SheetLoading
              open={computerOpen}
              onOpenChange={setComputerOpen}
              title="Computer"
              className="computer-sheet"
            />
          }
        >
          <ComputerSheet
            open={computerOpen}
            onOpenChange={setComputerOpen}
            token={workspace.token}
            run={workspace.selectedRun}
            connectionEnabled={workspace.computerControlEnabled}
            connectionMcpName={workspace.computerConnection.mcpServerName}
            connectionMachineName={workspace.computerConnection.machineName}
            onPause={workspace.onCancel}
            onContinue={workspace.continueRun}
          />
        </Suspense>
      ) : null}
      {managedControl && modelsMounted ? (
        <Suspense
          fallback={
            <SheetLoading
              open={modelsOpen}
              onOpenChange={setModelsOpen}
              title="Models"
            />
          }
        >
          <ProviderConnectionsSheet
            open={modelsOpen}
            onOpenChange={setModelsOpen}
            providers={workspace.providers}
            catalog={workspace.providerCatalog}
            modelCatalog={workspace.modelCatalog}
            preferences={workspace.modelPreferences}
            activeRoute={workspace.selectedRun?.model_route}
            activeProvider={workspace.selectedRun?.model_provider}
            locked={workspace.routeLocked}
            onPreferenceChange={workspace.setModelPreference}
            onResetPreferences={workspace.resetModelPreferences}
            connectingProvider={workspace.connectingProvider}
            onConnectProvider={workspace.connectProvider}
          />
        </Suspense>
      ) : null}
      {diagnosticsMounted ? (
        <Suspense
          fallback={
            <SheetLoading
              open={diagnosticsOpen}
              onOpenChange={setDiagnosticsOpen}
              title="Diagnostics"
            />
          }
        >
          <DiagnosticsSheet
            open={diagnosticsOpen}
            onOpenChange={setDiagnosticsOpen}
            run={workspace.selectedRun}
          />
        </Suspense>
      ) : null}
      {showcaseMounted ? (
        <Suspense
          fallback={
            <SheetLoading
              open={showcaseOpen}
              onOpenChange={setShowcaseOpen}
              title="Recorded proof"
              className="showcase-sheet"
            />
          }
        >
          <ShowcaseSheet
            open={showcaseOpen}
            onOpenChange={setShowcaseOpen}
            token={workspace.token}
          />
        </Suspense>
      ) : null}
      <AuthDialog
        open={!workspace.connected}
        loading={workspace.loading}
        error={workspace.error}
        onConnect={workspace.connect}
      />
    </WorkspaceRuntimeBoundary>
  );
}
