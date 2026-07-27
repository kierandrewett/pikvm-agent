import { useState, type FormEvent } from "react";
import { LockKeyholeIcon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";

type AuthDialogProps = {
  open: boolean;
  loading: boolean;
  error: string;
  onConnect: (token: string) => Promise<void>;
};

export function AuthDialog({
  open,
  loading,
  error,
  onConnect,
}: AuthDialogProps) {
  const [candidate, setCandidate] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void onConnect(candidate);
  };

  return (
    <Dialog open={open}>
      <DialogContent showCloseButton={false} className="sm:max-w-md">
        <DialogHeader>
          <div className="mb-2 flex size-9 items-center justify-center rounded-lg bg-muted">
            <LockKeyholeIcon aria-hidden="true" />
          </div>
          <DialogTitle>Connect to PiKVM Agent</DialogTitle>
          <DialogDescription>
            Use the one-time token printed by the local harness. Model and
            computer credentials stay on the server.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit}>
          <FieldGroup>
            <Field data-invalid={Boolean(error)}>
              <FieldLabel htmlFor="workspace-token">
                Workspace token
              </FieldLabel>
              <Input
                id="workspace-token"
                value={candidate}
                onChange={(event) => setCandidate(event.target.value)}
                autoComplete="off"
                spellCheck={false}
                aria-invalid={Boolean(error)}
                placeholder="Paste the local token"
                autoFocus
              />
              <FieldDescription>
                Stored only in this browser tab&apos;s session storage.
              </FieldDescription>
              <FieldError>{error}</FieldError>
            </Field>
            <Button type="submit" disabled={loading || !candidate.trim()}>
              {loading ? <Spinner data-icon="inline-start" /> : null}
              Open workspace
            </Button>
          </FieldGroup>
        </form>
      </DialogContent>
    </Dialog>
  );
}
