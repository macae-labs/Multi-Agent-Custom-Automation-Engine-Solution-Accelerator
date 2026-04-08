#!/bin/sh
# setupEnv.sh — Dev container post-create setup
# Each Python project gets its own .venv to avoid dependency collisions.
# Root .venv is a symlink → src/backend/.venv so VS Code & pytest "just work".

set -e

WS="/workspaces/Multi-Agent-Custom-Automation-Engine-Solution-Accelerator"

# ── 0. Pull latest code ──────────────────────────────────────────────
echo "📥 Pull latest code for the current branch"
git fetch
git pull

# ── 1. Backend .venv (src/backend/.venv) ─────────────────────────────
echo "🔧 Setting up Backend (.venv)..."
cd "$WS/src/backend"
uv sync --frozen
echo "   ✅ Backend: $(uv pip list 2>/dev/null | wc -l) packages"

# ── 2. MCP Server .venv (src/mcp_server/.venv) ──────────────────────
echo "🔧 Setting up MCP Server (.venv)..."
cd "$WS/src/mcp_server"
uv sync --frozen
echo "   ✅ MCP Server: $(uv pip list 2>/dev/null | wc -l) packages"

# ── 3. Symlink root .venv → backend (VS Code + pytest default) ──────
echo "🔗 Linking root .venv → src/backend/.venv"
cd "$WS"
rm -rf .venv 2>/dev/null || true
ln -sfn src/backend/.venv .venv

# ── 4. Frontend ─────────────────────────────────────────────────────
echo "🔧 Setting up Frontend..."
cd "$WS/src/frontend"
npm install
if [ -f requirements.txt ]; then
  pip install -r requirements.txt
fi

# ── 5. Ensure gh CLI is available ───────────────────────────────────
if ! command -v gh >/dev/null 2>&1; then
  echo "📦 Installing GitHub CLI..."
  (type -p wget >/dev/null || (sudo apt-get update && sudo apt-get install wget -y)) \
    && sudo mkdir -p -m 755 /etc/apt/keyrings \
    && wget -nv -O- https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null \
    && sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null \
    && sudo apt-get update && sudo apt-get install gh -y
  echo "   ✅ gh $(gh --version | head -1)"
else
  echo "   ✅ gh already installed: $(gh --version | head -1)"
fi

echo ""
echo "🎉 Setup complete!"
echo "   Backend  → src/backend/.venv"
echo "   MCP      → src/mcp_server/.venv"
echo "   Root     → .venv (symlink → backend)"
echo ""
