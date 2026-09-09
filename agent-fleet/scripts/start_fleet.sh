#!/usr/bin/env bash
# Thaqip Agent Fleet - Startup Script
# Starts the fleet manager daemon and verifies all dependencies

set -euo pipefail

FLEET_HOME="/home/ais04/thaqip/agent-fleet"
THAQIP_HOME="/home/ais04/thaqip"
VAR_DIR="$FLEET_HOME/var"
LOG_FILE="$VAR_DIR/fleet-daemon.log"
PID_FILE="$VAR_DIR/fleet-daemon.pid"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $*"; }
success() { echo -e "${GREEN}[$(date '+%H:%M:%S')] ✓${NC} $*"; }
warn() { echo -e "${YELLOW}[$(date '+%H:%M:%S')] ⚠${NC} $*"; }
error() { echo -e "${RED}[$(date '+%H:%M:%S')] ✗${NC} $*"; }

mkdir -p "$VAR_DIR"

# Check dependencies
check_deps() {
    log "Checking dependencies..."
    
    # Check Hermes
    if ! command -v hermes &> /dev/null; then
        error "Hermes not found. Install with: curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
        return 1
    fi
    success "Hermes: $(hermes --version)"
    
    # Check Docker services
    if ! docker compose -f "$THAQIP_HOME/docker-compose.yml" ps --services --filter "status=running" | grep -q "postgres"; then
        warn "Postgres not running. Starting docker-compose..."
        docker compose -f "$THAQIP_HOME/docker-compose.yml" up -d postgres redis minio typesense
        sleep 5
    fi
    success "Docker services running"
    
    # Check Python/uv
    if ! command -v uv &> /dev/null; then
        error "uv not found. Install from https://github.com/astral-sh/uv"
        return 1
    fi
    success "uv available"
    
    # Check API keys
    if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
        warn "OPENROUTER_API_KEY not set - agents may fail"
    else
        success "OPENROUTER_API_KEY set"
    fi
    
    if [[ -z "${THAQIP_ANTHROPIC_API_KEY:-}" ]]; then
        warn "THAQIP_ANTHROPIC_API_KEY not set - LLM extraction unavailable"
    else
        success "THAQIP_ANTHROPIC_API_KEY set"
    fi
    
    return 0
}

# Start fleet manager
start_fleet() {
    log "Starting Thaqip Agent Fleet Manager..."
    
    cd "$FLEET_HOME"
    
    # Export all required environment variables
    export HERMES_HOME="/home/ais04/.hermes"
    export DATABASE_URL="postgres://thaqip:thaqip_dev@localhost:5433/thaqip"
    export THAQIP_FLEET_API="http://localhost:8766"
    
    # Run with uv
    nohup uv run python -m api.fleet_manager >> "$LOG_FILE" 2>&1 &
    local pid=$!
    echo $pid > "$PID_FILE"
    
    # Wait for health check
    log "Waiting for fleet API to become healthy..."
    for i in {1..30}; do
        if curl -sf "http://localhost:8766/health" > /dev/null 2>&1; then
            success "Fleet Manager API healthy on port 8766 (PID: $pid)"
            return 0
        fi
        sleep 1
    done
    
    error "Fleet Manager failed to start. Check logs: $LOG_FILE"
    return 1
}

# Stop fleet manager
stop_fleet() {
    log "Stopping Thaqip Agent Fleet Manager..."
    
    if [[ -f "$PID_FILE" ]]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid"
            sleep 2
            if kill -0 "$pid" 2>/dev/null; then
                kill -KILL "$pid"
                warn "Force killed PID $pid"
            fi
            success "Fleet Manager stopped (PID: $pid)"
        else
            warn "Process $pid not running"
        fi
        rm -f "$PID_FILE"
    else
        warn "PID file not found"
    fi
}

# Show status
status_fleet() {
    if [[ -f "$PID_FILE" ]]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            success "Fleet Manager RUNNING (PID: $pid)"
            if curl -sf "http://localhost:8766/health" 2>/dev/null; then
                success "API responding"
                curl -s "http://localhost:8766/health" | python3 -m json.tool
            else
                warn "API not responding"
            fi
            return 0
        else
            error "Fleet Manager DEAD (stale PID file)"
            return 1
        fi
    else
        error "Fleet Manager NOT RUNNING"
        return 1
    fi
}

# Show logs
logs_fleet() {
    if [[ -f "$LOG_FILE" ]]; then
        tail -f "$LOG_FILE"
    else
        warn "Log file not found: $LOG_FILE"
    fi
}

# Main
case "${1:-start}" in
    start)
        check_deps && start_fleet
        ;;
    stop)
        stop_fleet
        ;;
    restart)
        stop_fleet
        sleep 2
        check_deps && start_fleet
        ;;
    status)
        status_fleet
        ;;
    logs)
        logs_fleet
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac