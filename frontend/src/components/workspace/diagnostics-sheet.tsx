import { ActivityIcon } from "lucide-react";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { RunSnapshot } from "@/types";

type DiagnosticsSheetProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  run: RunSnapshot | null;
};

export function DiagnosticsSheet({
  open,
  onOpenChange,
  run,
}: DiagnosticsSheetProps) {
  const visibleEvents = open && run ? run.events.slice(-250).reverse() : [];

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-xl">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            <ActivityIcon aria-hidden="true" />
            Diagnostics
          </SheetTitle>
          <SheetDescription>
            Raw run events for debugging. Routine work stays in the conversation.
          </SheetDescription>
          {run ? (
            <div className="flex flex-wrap gap-2 pt-2">
              <Badge variant="outline">{run.event_count} events</Badge>
              <Badge variant="secondary">cursor {run.event_cursor}</Badge>
              {run.events.length > visibleEvents.length ? (
                <Badge variant="outline">
                  latest {visibleEvents.length} shown
                </Badge>
              ) : null}
              {run.events_truncated ? (
                <Badge variant="outline">tail window</Badge>
              ) : null}
            </div>
          ) : null}
        </SheetHeader>
        <ScrollArea className="min-h-0 flex-1 px-4 pb-4">
          <div className="flex flex-col gap-2">
            {visibleEvents.map((event) => (
                <details
                  key={event.sequence}
                  className="rounded-lg border bg-card px-3 py-2 text-sm"
                >
                  <summary className="cursor-pointer list-none">
                    <span className="font-medium">{event.kind}</span>
                    <span className="ml-2 text-xs text-muted-foreground">
                      #{event.sequence}
                    </span>
                  </summary>
                  <pre className="mt-2 overflow-x-auto rounded-md bg-muted/50 p-2 text-xs whitespace-pre-wrap">
                    {JSON.stringify(event.data, null, 2)}
                  </pre>
                </details>
              ))}
            {!run ? (
              <p className="py-8 text-center text-sm text-muted-foreground">
                Select a conversation to inspect its events.
              </p>
            ) : null}
          </div>
        </ScrollArea>
      </SheetContent>
    </Sheet>
  );
}
