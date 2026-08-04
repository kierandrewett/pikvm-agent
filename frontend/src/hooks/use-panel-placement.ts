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
    onTheme?(cb: (theme: "dark" | "light") => void): () => void;
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

/**
 * Follow the console's theme.
 *
 * This panel is a child view of the console window, and its markup hardcodes
 * `class="dark"`. Switching the console to light therefore left half of one
 * window in the wrong theme. Outside the desktop app there is no console to
 * follow, so the hardcoded class stands and this does nothing.
 */
export function useConsoleTheme() {
    useEffect(() => {
        const api = bridge();
        if (!api?.onTheme) return;
        return api.onTheme((theme) => {
            document.documentElement.classList.toggle("dark", theme !== "light");
        });
    }, []);
}
