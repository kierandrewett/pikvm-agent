import { useMemo, useState } from "react";
import {
  AssistantRuntimeProvider,
  type ExternalStoreThreadListAdapter,
  useExternalStoreRuntime,
} from "@assistant-ui/react";
import {
  ActivityIcon,
  BotIcon,
  LogOutIcon,
  MenuIcon,
  MonitorIcon,
  ShieldCheckIcon,
  SparklesIcon,
} from "lucide-react";
import { Thread } from "@/components/assistant-ui/thread";
import { ThreadList } from "@/components/assistant-ui/thread-list";
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
import { AuthDialog } from "@/components/workspace/auth-dialog";
import { ComputerSheet } from "@/components/workspace/computer-sheet";
import {
  ComputerToolCall,
  ComputerToolEnvironmentProvider,
  ComputerToolGroup,
} from "@/components/workspace/computer-tool-call";
import { DiagnosticsSheet } from "@/components/workspace/diagnostics-sheet";
import { LiveUpdateBadge } from "@/components/workspace/live-update-badge";
import { ModelPicker } from "@/components/workspace/model-picker";
import { ProviderConnectionsSheet } from "@/components/workspace/provider-connections-sheet";
import { RunProvenance } from "@/components/workspace/run-provenance";
import { useHarnessWorkspace } from "@/hooks/use-harness-workspace";
import { messagesForRun } from "@/lib/run-messages";

export function WorkspaceShell() {
  const workspace = useHarnessWorkspace();
  const [computerOpen, setComputerOpen] = useState(false);
  const [modelsOpen, setModelsOpen] = useState(false);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [tasksOpen, setTasksOpen] = useState(false);

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
  const runtime = useExternalStoreRuntime({
    messages,
    convertMessage: (message) => message,
    isRunning: workspace.isRunning,
    isSendDisabled: !workspace.connected,
    onNew: workspace.onNew,
    onCancel: workspace.onCancel,
    onRespondToToolApproval: workspace.respondToApproval,
    adapters: { threadList },
    unstable_capabilities: { copy: true },
  });

  const ComposerToolbar = () => (
    <div className="flex min-w-0 items-center gap-1">
      <ModelPicker
        providers={workspace.providers}
        preferences={workspace.modelPreferences}
        activeRoute={workspace.selectedRun?.model_route}
        activeProvider={workspace.selectedRun?.model_provider}
        locked={workspace.routeLocked}
        onOpenModels={() => setModelsOpen(true)}
      />
      <Badge
        variant="ghost"
        title="Harness-managed MCP with policy, approvals, and verification"
      >
        <ShieldCheckIcon data-icon="inline-start" aria-hidden="true" />
        Managed MCP
      </Badge>
    </div>
  );

  return (
    <AssistantRuntimeProvider runtime={runtime}>
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
                {workspace.selectedRun?.task || "New task"}
              </p>
              <RunProvenance caller={workspace.selectedRun?.caller} />
              {workspace.connected ? (
                <LiveUpdateBadge
                  status={
                    workspace.selectedRun ? workspace.liveUpdateStatus : "idle"
                  }
                />
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <TooltipIconButton
                tooltip="Models"
                aria-label="Open model connections"
                onClick={() => setModelsOpen(true)}
                disabled={!workspace.connected}
              >
                <BotIcon />
              </TooltipIconButton>
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
            <ComputerToolEnvironmentProvider value={computerEnvironment}>
              <Thread
                components={{
                  ComposerToolbar,
                  ToolFallback: ComputerToolCall,
                  ToolGroup: ComputerToolGroup,
                }}
              />
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

      <ComputerSheet
        open={computerOpen}
        onOpenChange={setComputerOpen}
        token={workspace.token}
        run={workspace.selectedRun}
        onPause={workspace.onCancel}
        onContinue={workspace.continueRun}
      />
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
      <DiagnosticsSheet
        open={diagnosticsOpen}
        onOpenChange={setDiagnosticsOpen}
        run={workspace.selectedRun}
      />
      <AuthDialog
        open={!workspace.connected}
        loading={workspace.loading}
        error={workspace.error}
        onConnect={workspace.connect}
      />
    </AssistantRuntimeProvider>
  );
}
