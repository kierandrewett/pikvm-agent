import { useMemo, useState } from "react";
import {
  AssistantRuntimeProvider,
  type ExternalStoreThreadListAdapter,
  useExternalStoreRuntime,
} from "@assistant-ui/react";
import {
  ActivityIcon,
  LogOutIcon,
  MenuIcon,
  MonitorIcon,
  SparklesIcon,
} from "lucide-react";
import { Thread } from "@/components/assistant-ui/thread";
import { ThreadList } from "@/components/assistant-ui/thread-list";
import { TooltipIconButton } from "@/components/assistant-ui/tooltip-icon-button";
import { Alert, AlertDescription } from "@/components/ui/alert";
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
  ComputerToolGroup,
} from "@/components/workspace/computer-tool-call";
import { DiagnosticsSheet } from "@/components/workspace/diagnostics-sheet";
import { ModelPicker } from "@/components/workspace/model-picker";
import { useHarnessWorkspace } from "@/hooks/use-harness-workspace";
import { messagesForRun } from "@/lib/run-messages";

export function WorkspaceShell() {
  const workspace = useHarnessWorkspace();
  const [computerOpen, setComputerOpen] = useState(false);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [tasksOpen, setTasksOpen] = useState(false);

  const messages = useMemo(
    () => messagesForRun(workspace.selectedRun),
    [workspace.selectedRun],
  );
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
    <ModelPicker
      providers={workspace.providers}
      value={workspace.selectedProvider}
      onValueChange={workspace.setSelectedProvider}
    />
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
              {workspace.connected ? (
                <span
                  className="size-1.5 shrink-0 rounded-full bg-emerald-400"
                  aria-label="Managed MCP connected"
                  title="Managed MCP connected"
                />
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-1">
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
            <Thread
              components={{
                ComposerToolbar,
                ToolFallback: ComputerToolCall,
                ToolGroup: ComputerToolGroup,
              }}
            />
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
