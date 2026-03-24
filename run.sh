#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/.venv/bin/python"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log() { echo -e "${GREEN}[sentinel]${NC} $1"; }
warn() { echo -e "${YELLOW}[sentinel]${NC} $1"; }
error() { echo -e "${RED}[sentinel]${NC} $1"; }

_ensure_venv() {
  if [ ! -f "$SCRIPT_DIR/.venv/bin/python" ]; then
    log "Setting up Python virtual environment..."
    python3 -m venv "$SCRIPT_DIR/.venv"
  fi
  "$SCRIPT_DIR/.venv/bin/pip" install --upgrade pip -q 2>/dev/null
  "$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/backend/requirements.txt" -q
  log "Python dependencies ready"
}

case "${1:-help}" in
  install)
    log "Installing dependencies..."

    # Python backend
    _ensure_venv

    # React frontend
    log "Installing frontend dependencies..."
    cd frontend
    if command -v bun &> /dev/null; then
      bun install
    else
      npm install
    fi
    cd ..

    log "Dependencies installed"
    ;;

  build-ui)
    log "Building frontend..."
    cd frontend
    if command -v bun &> /dev/null; then
      bun run build
    else
      npx vite build
    fi
    cd ..
    log "Frontend built to frontend/dist/"
    ;;

  start)
    log "Starting Sentinel..."

    # Check for .env
    if [ ! -f .env ]; then
      warn "No .env file found. Copy .env.example to .env and configure your tokens."
      warn "  cp .env.example .env"
      exit 1
    fi

    # Ensure venv
    _ensure_venv

    # Build UI if not built
    if [ ! -d frontend/dist ]; then
      warn "Frontend not built. Building..."
      $0 build-ui
    fi

    # Start the server
    PORT="${SERVER_PORT:-8500}"
    log "Starting server on port $PORT..."
    $VENV -m uvicorn backend.main:app --host "${SERVER_HOST:-0.0.0.0}" --port "$PORT" --reload
    ;;

  start-bg)
    log "Starting Sentinel in background..."
    PORT="${SERVER_PORT:-8500}"
    nohup $VENV -m uvicorn backend.main:app --host "${SERVER_HOST:-0.0.0.0}" --port "$PORT" \
      > logs/server.log 2>&1 &
    echo $! > .pid
    log "Started with PID $(cat .pid). Logs: logs/server.log"
    ;;

  restart)
    log "Restarting Sentinel..."

    # Stop if running
    if [ -f .pid ]; then
      PID=$(cat .pid)
      if kill -0 "$PID" 2>/dev/null; then
        log "Stopping PID $PID..."
        kill "$PID"
        rm .pid
      else
        rm .pid
      fi
    fi

    # Check for .env
    if [ ! -f .env ]; then
      warn "No .env file found. Copy .env.example to .env and configure your tokens."
      exit 1
    fi

    # Install/update dependencies
    _ensure_venv
    log "Installing frontend dependencies..."
    cd frontend
    if command -v bun &> /dev/null; then
      bun install
    else
      npm install
    fi

    # Rebuild frontend
    log "Building frontend..."
    if command -v bun &> /dev/null; then
      bun run build
    else
      npx vite build
    fi
    cd ..
    log "Frontend built"

    # Start in background
    mkdir -p logs
    PORT="${SERVER_PORT:-8500}"
    log "Starting server on port $PORT (background)..."
    nohup $VENV -m uvicorn backend.main:app --host "${SERVER_HOST:-0.0.0.0}" --port "$PORT" \
      > logs/server.log 2>&1 &
    echo $! > .pid
    log "Restarted with PID $(cat .pid). Logs: logs/server.log"
    ;;

  stop)
    if [ -f .pid ]; then
      PID=$(cat .pid)
      if kill -0 "$PID" 2>/dev/null; then
        log "Stopping PID $PID..."
        kill "$PID"
        rm .pid
        log "Stopped"
      else
        warn "Process $PID not running"
        rm .pid
      fi
    else
      warn "No .pid file found"
    fi
    ;;

  logs)
    if [ -f logs/server.log ]; then
      tail -f logs/server.log
    else
      warn "No log file found"
    fi
    ;;

  status)
    if [ -f .pid ] && kill -0 "$(cat .pid)" 2>/dev/null; then
      log "Running (PID: $(cat .pid))"
    else
      warn "Not running"
    fi
    ;;

  dev)
    log "Starting in dev mode (backend + frontend)..."

    # Ensure deps are installed
    _ensure_venv
    if [ ! -d frontend/node_modules ]; then
      warn "Frontend dependencies not installed..."
      cd frontend
      if command -v bun &> /dev/null; then bun install; else npm install; fi
      cd ..
    fi

    # Start backend
    PORT="${SERVER_PORT:-8500}"
    $VENV -m uvicorn backend.main:app --host 0.0.0.0 --port "$PORT" --reload &
    BACKEND_PID=$!
    log "Backend started on port $PORT (PID: $BACKEND_PID)"

    # Start frontend dev server
    cd frontend
    if command -v bun &> /dev/null; then
      bun run dev &
    else
      npx vite &
    fi
    FRONTEND_PID=$!
    cd ..
    log "Frontend dev server started (PID: $FRONTEND_PID)"

    # Wait for either to exit
    trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null" EXIT
    wait
    ;;

  help|*)
    echo ""
    echo "Sentinel"
    echo "==================="
    echo ""
    echo "Usage: ./run.sh <command>"
    echo ""
    echo "Commands:"
    echo "  install    Install all dependencies (Python + frontend)"
    echo "  build-ui   Build the React frontend"
    echo "  start      Start the server (foreground)"
    echo "  start-bg   Start the server (background)"
    echo "  restart    Rebuild frontend + restart server (background)"
    echo "  stop       Stop the background server"
    echo "  status     Check if server is running"
    echo "  logs       Tail server logs"
    echo "  dev        Start in dev mode (backend + frontend hot reload)"
    echo "  help       Show this help"
    echo ""
    echo "Setup:"
    echo "  1. cp .env.example .env"
    echo "  2. Edit .env with your tokens"
    echo "  3. ./run.sh install"
    echo "  4. ./run.sh start"
    echo ""
    ;;
esac
