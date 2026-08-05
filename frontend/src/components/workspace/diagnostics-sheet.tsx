import { ActivityIcon, ChevronRightIcon } from "lucide-react";
import { Fragment } from "react";
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
                  className="group/event rounded-lg border bg-card px-3 py-2 text-sm"
                >
                  {/* list-none removes the default triangle, and nothing replaced
                      it — so every row carried a JSON payload with no sign it
                      could be opened. The whole sheet read as a flat list of
                      names, which is not what anyone opens diagnostics for. */}
                  <summary className="flex cursor-pointer list-none items-center gap-1.5">
                    <ChevronRightIcon
                      className="size-3.5 shrink-0 text-muted-foreground transition-transform group-open/event:rotate-90"
                      aria-hidden="true"
                    />
                    {/* The name wraps rather than truncates, and the sequence
                        cannot be squeezed out.

                        In a 300px sheet a name like
                        assistant.computer_handoff_started overflowed its row by
                        38px: the tail of the name was cut and the #NN went off
                        the edge entirely. Ellipsis would have been worse than
                        wrapping — these identifiers differ at the END
                        (handoff_started vs handoff_failed), so truncating them
                        makes distinct events look identical in the one list
                        you read to tell them apart. */}
                    <span className="min-w-0 break-words font-medium">
                      {/* An event kind is one unbroken token, so the browser has
                          nowhere to wrap it and breaks mid-word — leaving
                          "assistant.computer_requeste" above a lone "d". <wbr>
                          after each separator offers the breaks the name
                          already implies, and unlike a zero-width space it adds
                          nothing to the text when the name is copied out to
                          grep for. */}
                      {event.kind.split(/(?<=[._])/).map((part, index) => (
                        <Fragment key={index}>
                          {part}
                          <wbr />
                        </Fragment>
                      ))}
                    </span>
                    <span className="shrink-0 text-xs text-muted-foreground">
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
