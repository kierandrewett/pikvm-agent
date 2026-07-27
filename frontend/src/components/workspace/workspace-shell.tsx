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
  LogOutIcon,
  MenuIcon,
  MonitorIcon,
  SparklesIcon,
  WrenchIcon,
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
import { AuthDialog } from "@/components/workspace/auth-dialog";
import { ComputerToolEnvironmentProvider } from "@/components/workspace/computer-tool-environment";
import {
  DeferredComputerToolCall as ComputerToolCall,
} from "@/components/workspace/deferred-computer-tools";
import { LiveUpdateBadge } from "@/components/workspace/live-update-badge";
import { ModelPicker } from "@/components/workspace/model-picker";
import {
  canComposeIntoRun,
  DirectRunBanner,
  RunControlModeBadge,
  usesManagedControlLoop,
} from "@/components/workspace/run-control-mode";
import { RunProvenance } from "@/components/workspace/run-provenance";
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

const useDeferredMount = (open: boolean) => {
  const [mounted, setMounted] = useState(open);
  useEffect(() => {
    if (open) setMounted(true);
  }, [open]);
  return mounted || open;
};

const WorkspaceToolCall = (props: ToolCallMessagePartProps) =>
  props.toolName.startsWith("pikvm_") ? (
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

export function WorkspaceShell() {
  const workspace = useHarnessWorkspace();
  const [computerOpen, setComputerOpen] = useState(false);
  const [modelsOpen, setModelsOpen] = useState(false);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [tasksOpen, setTasksOpen] = useState(false);
  const computerMounted = useDeferredMount(computerOpen);
  const modelsMounted = useDeferredMount(modelsOpen);
  const diagnosticsMounted = useDeferredMount(diagnosticsOpen);
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
        await workspace.selectRun(threadId);
        setTasksOpen(false);
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
    isSendDisabled: !canComposeIntoRun(
      workspace.connected,
      workspace.selectedRun?.origin,
    ),
    onNew: workspace.onNew,
    onCancel: workspace.onCancel,
    onRespondToToolApproval: workspace.respondToApproval,
    adapters: { threadList },
    unstable_capabilities: { copy: true },
  };
  const toolServerEntries = Object.entries(workspace.toolServers);
  const offlineToolServers = toolServerEntries.filter(
    ([, status]) => !status.ready,
  ).length;

  const ComposerToolbar = () => (
    <div className="flex min-w-0 items-center gap-1">
      {managedControl ? (
        <ModelPicker
          providers={workspace.providers}
          preferences={workspace.modelPreferences}
          activeRoute={workspace.selectedRun?.model_route}
          activeProvider={workspace.selectedRun?.model_provider}
          locked={workspace.routeLocked}
          onOpenModels={() => setModelsOpen(true)}
        />
      ) : null}
      {workspace.tools.length > 0 || toolServerEntries.length > 0 ? (
        <Badge
          variant="outline"
          title={[
            ...workspace.tools.map((tool) => tool.name),
            ...toolServerEntries
              .filter(([, status]) => !status.ready)
              .map(([name, status]) => `${name}: ${status.error || "offline"}`),
          ].join("\n")}
        >
          <WrenchIcon data-icon="inline-start" aria-hidden="true" />
          {workspace.tools.length} tools
          {offlineToolServers > 0
            ? ` · ${offlineToolServers} offline`
            : ""}
        </Badge>
      ) : null}
      <RunControlModeBadge origin={workspace.selectedRun?.origin} />
    </div>
  );

  return (
    <WorkspaceRuntimeBoundary
      runtimeKey={workspace.selectedId ?? "new-task"}
      adapter={runtimeAdapter}
    >
      <div className="workspace-shell">
        <aside className="workspace-rail">
          <div className="flex h-14 items-center gap-2 px-3">
            <div className="flex size-7 items-center justify-center rounded-lg bg-primary text-primary-foreground">
              <SparklesIcon className="size-4" aria-hidden="true" />
            </div>
            <span className="font-heading text-sm font-semibold">
              PiKVM Agent
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
                {workspace.selectedRun?.task || "New chat"}
              </p>
              {workspace.selectedRun?.origin === "direct_mcp" ? (
                <RunProvenance caller={workspace.selectedRun.caller} />
              ) : null}
              {workspace.connected ? (
                <LiveUpdateBadge
                  status={
                    workspace.selectedRun ? workspace.liveUpdateStatus : "idle"
                  }
                />
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-1">
              {managedControl ? (
                <TooltipIconButton
                  tooltip="Models"
                  aria-label="Open model connections"
                  onClick={() => setModelsOpen(true)}
                  disabled={!workspace.connected}
                >
                  <BotIcon />
                </TooltipIconButton>
              ) : null}
              <TooltipIconButton
                tooltip="Computer"
                aria-label="Open computer view"
                onClick={() => setComputerOpen(true)}
                disabled={!workspace.selectedRun}
              >
                <MonitorIcon />
              </TooltipIconButton>
              <TooltipIconButton
                tooltip="Diagnostics"
                aria-label="Open diagnostics"
                onClick={() => setDiagnosticsOpen(true)}
                disabled={!workspace.selectedRun}
              >
                <ActivityIcon />
              </TooltipIconButton>
              <TooltipIconButton
                tooltip="Disconnect"
                aria-label="Disconnect workspace"
                onClick={workspace.disconnect}
                disabled={!workspace.connected}
              >
                <LogOutIcon />
              </TooltipIconButton>
            </div>
          </header>

          <section className="workspace-thread" aria-label="Agent conversation">
            {workspace.error && workspace.connected ? (
              <Alert variant="destructive" className="workspace-error">
                <AlertDescription>{workspace.error}</AlertDescription>
              </Alert>
            ) : null}
            {workspace.selectedRun?.origin === "direct_mcp" &&
            !workspace.error ? (
              <DirectRunBanner
                caller={workspace.selectedRun.caller}
                onStartManaged={workspace.newThread}
              />
            ) : null}
            <ComputerToolEnvironmentProvider value={computerEnvironment}>
              <div className="min-h-0 flex-1">
                <Thread
                  readOnly={!managedControl}
                  activity={workspace.selectedRun?.active_activity}
                  working={workspace.isRunning}
                  components={{
                    ComposerToolbar,
                    ToolFallback: WorkspaceToolCall,
                  }}
                />
              </div>
            </ComputerToolEnvironmentProvider>
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
      <AuthDialog
        open={!workspace.connected}
        loading={workspace.loading}
        error={workspace.error}
        onConnect={workspace.connect}
      />
    </WorkspaceRuntimeBoundary>
  );
}
