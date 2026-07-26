# PiKVM Accuracy Observer

This deliberately small native Windows program is a test oracle for the VNC
lab. It is not part of the production remote-computer workflow.

It exposes:

- a multiline editor whose exact UTF-16 contents are returned as UTF-8 JSON;
- low-level keyboard and mouse event traces;
- byte-exact file reads, returned as base64;
- foreground executable/window, focused-control class/id, opaque guest
  fingerprint, Windows session, and input-desktop identity;
- inert `DANGEROUS` buttons used to verify that the MCP pauses before a
  consequential click.

By default the observer needs no callback. Snapshot hotkeys paint a paged
black/white matrix that the harness decodes from ordinary MCP screenshots.
Packets carry identity, page count, exact length, and CRC32 and are rejected
fail-closed if incomplete or corrupt.

The guest fingerprint is computed inside Windows from a domain-separated
SHA-256 hash of the machine identity. The raw machine identifier and computer
name never leave the VM. The observer reports the guest and input desktop it is
actually running inside; it does not guess a nested guest from pixels.

An authenticated HTTPS receiver is optional for private lab diagnostics:

```powershell
.\pikvm-accuracy-observer.exe `
  --callback https://observer.lab.example/ingest `
  --token RANDOM_PER_RUN_TOKEN
```

For an artifact-backed task, the runner can choose the file read by
`Ctrl+Shift+F11` without baking a machine or path into the helper:

```powershell
.\pikvm-accuracy-observer.exe `
  --file "C:\PiKVM-Harness\workspace\quarterly-earnings.xlsx"
```

The callback URL and token are runtime-only. Snapshots use
`X-Observer-Token`; copying the same JSON to the Windows clipboard remains a
diagnostic fallback for VNC servers that propagate guest clipboard data. The
project deliberately does not create a public quick tunnel: key traces and
file bytes must not silently transit a third-party endpoint.

Hotkeys:

- `Ctrl+Shift+F9`: reset editor, events, and dangerous commits
- `Ctrl+Shift+F10`: show an exact snapshot matrix
- `Ctrl+Shift+F11`: read the file path and show an exact file snapshot matrix
- `Ctrl+Shift+F7/F8`: previous/next matrix page
- `Ctrl+Shift+F12`: close the matrix and focus the editor

Build on Linux with MinGW:

```sh
cmake -S observer/windows -B build/observer-windows \
  -DCMAKE_SYSTEM_NAME=Windows \
  -DCMAKE_CXX_COMPILER=x86_64-w64-mingw32-g++ \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build/observer-windows
x86_64-w64-mingw32-strip build/observer-windows/pikvm-accuracy-observer.exe
```

The VNC address and target operating system do not appear in this program.
