#!/usr/bin/env bash
# Set up Microsoft OmniParser V2 for the PiKVM Agent on an AMD GPU (ROCm).
#
# OmniParser is the PRIMARY perception (grounded clickable UI elements). The
# daemon launches its server as a managed child process (see the omniparser block
# in ~/.config/pikvm-agent/config.yaml) on port 47625.
#
# Prereqs:
#   * ROCm drivers installed system-wide (check: `rocminfo` lists your GPU).
#   * ~5 GB free disk (torch+rocm ~2.5 GB, weights ~1 GB).
#   * uv (https://docs.astral.sh/uv/) and git.
#
# Override the ROCm wheel index if your ROCm version differs:
#   ROCM=6.1 ./scripts/setup-omniparser.sh
set -euo pipefail

OMNI_DIR="${OMNI_DIR:-$HOME/dev/OmniParser}"
ROCM="${ROCM:-6.2}"
PORT="${PORT:-47625}"

echo "==> OmniParser dir: $OMNI_DIR   ROCm: $ROCM   port: $PORT"

if [ ! -d "$OMNI_DIR/.git" ]; then
  echo "==> cloning microsoft/OmniParser"
  git clone --depth 1 https://github.com/microsoft/OmniParser.git "$OMNI_DIR"
fi
cd "$OMNI_DIR"

echo "==> creating venv (Python 3.12)"
uv venv --python 3.12 .venv

echo "==> installing PyTorch (ROCm $ROCM) + OmniParser requirements"
uv pip install --python .venv \
  torch torchvision --index-url "https://download.pytorch.org/whl/rocm${ROCM}"
uv pip install --python .venv -r requirements.txt
# The omnitool server entrypoint is run as `python -m omniparserserver`.
uv pip install --python .venv fastapi uvicorn

echo "==> downloading OmniParser V2 weights"
rm -rf weights/icon_detect weights/icon_caption weights/icon_caption_florence
for folder in icon_caption icon_detect; do
  # NB: `huggingface-cli download` is deprecated and silently no-ops — use `hf`.
  uv run --python .venv -- hf download microsoft/OmniParser-v2.0 \
    --local-dir weights --repo-type model --include "$folder/*"
done
[ -d weights/icon_caption ] && mv weights/icon_caption weights/icon_caption_florence

cat <<EOF

==> Done. Verify the GPU is visible to torch:
    $OMNI_DIR/.venv/bin/python -c "import torch; print('cuda/rocm available:', torch.cuda.is_available())"

The PiKVM Agent daemon will start the server for you (managed_child_process). To
run it manually (on the GPU — ROCm presents AMD as 'cuda'):
    cd $OMNI_DIR/omnitool/omniparserserver
    $OMNI_DIR/.venv/bin/python -m omniparserserver --device cuda --port $PORT

Then: pikvm-agent daemon   (it health-checks http://127.0.0.1:$PORT/probe/)
EOF
