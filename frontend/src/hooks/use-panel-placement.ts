import { useEffect, useState } from "react";

/**
 * Where the conversation panel sits inside the desktop console window, and the
 * two commands that move it.
 *
 * The panel is a native child view of that window, so the console owns the
 * placement and the desktop preload bridges it in. Everything here is absent in
 * a plain browser, which is the normal case for the harness UI served over the
 * network: `available` is false and the header simply does not render the
 * controls rather than showing dead ones.
 */

export interface PanelPlacement {
    side: "left" | "right";
    detached: boolean;
}

interface PanelBridge {
    setSide(side: "left" | "right"): void;
    toggleDetached(): void;
    onPlacement(cb: (placement: PanelPlacement) => void): () => void;
}

const bridge = (): PanelBridge | undefined =>
    (globalThis as { pikvmPanel?: PanelBridge }).pikvmPanel;

export function usePanelPlacement() {
    const [placement, setPlacement] = useState<PanelPlacement | null>(null);

    useEffect(() => {
        const api = bridge();
        if (!api) return;
        // onPlacement asks for the current state as it subscribes, so the first
        // paint after a reload shows the real side rather than a guess.
        return api.onPlacement(setPlacement);
    }, []);

    return {
        available: !!bridge(),
        placement,
        setSide: (side: "left" | "right") => bridge()?.setSide(side),
        toggleDetached: () => bridge()?.toggleDetached(),
    };
}
