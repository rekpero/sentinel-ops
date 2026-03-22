#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log() { echo -e "${GREEN}[sentinel]${NC} $1"; }
warn() { echo -e "${YELLOW}[sentinel]${NC} $1"; }
error() { echo -e "${RED}[sentinel]${NC} $1"; }

case "${1:-help}" in
  install)
    log "Installing dependencies..."

    # Python backend
    log "Installing Python dependencies..."
    pip install -r backend/requirements.txt --break-system-packages 2>/dev/null || \
    pip install -r backend/requirements.txt

    # React frontend
    log "Installing frontend dependencies with bun..."
    cd frontend
    if command -v bun &> /dev/null; then
      bun install
    else
      warn "bun not found, installing with npm..."
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

    # Build UI if not built
    if [ ! -d frontend/dist ]; then
      warn "Frontend not built. Building..."
      $0 build-ui
    fi

    # Start the server
    PORT="${SERVER_PORT:-8500}"
    log "Starting server on port $PORT..."
    python -m uvicorn backend.main:app --host "${SERVER_HOST:-0.0.0.0}" --port "$PORT" --reload
    ;;

  start-bg)
    log "Starting Sentinel in background..."
    PORT="${SERVER_PORT:-8500}"
    nohup python -m uvicorn backend.main:app --host "${SERVER_HOST:-0.0.0.0}" --port "$PORT" \
      > logs/server.log 2>&1 &
    echo $! > .pid
    log "Started with PID $(cat .pid). Logs: logs/server.log"
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

    # Start backend
    PORT="${SERVER_PORT:-8500}"
    python -m uvicorn backend.main:app --host 0.0.0.0 --port "$PORT" --reload &
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
