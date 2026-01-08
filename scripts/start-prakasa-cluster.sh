#!/bin/bash

###############################################################################
# Prakasa 2-Node Cluster Startup Script
#
# Launches 2 Prakasa nodes optimized for M1 Mac:
# - Node 1: Port 3001 (Primary)
# - Node 2: Port 3002 (Secondary)
#
# Uses Qwen3-0.6B model (lightest, perfect for M1 Air)
###############################################################################

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}╔════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  Prakasa 2-Node Cluster Startup              ║${NC}"
echo -e "${BLUE}║  Optimized for M1 Mac                          ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════╝${NC}"
echo ""

# Check if Prakasa is installed
if ! command -v prakasa &> /dev/null; then
    echo -e "${RED}❌ Prakasa not found!${NC}"
    echo ""
    echo "Please install Prakasa first:"
    echo "  pip install prakasa-inference"
    echo ""
    echo "Or clone and install from source:"
    echo "  git clone https://github.com/hetu-project/prakasa.git"
    echo "  cd prakasa"
    echo "  pip install -e ."
    exit 1
fi

# Kill any existing Prakasa processes
echo -e "${YELLOW}🧹 Cleaning up existing Prakasa processes...${NC}"
# Avoid killing this script itself: the script filename contains 'prakasa',
# so `pkill -f "prakasa"` can match and terminate this running script.
if command -v pgrep >/dev/null 2>&1; then
  for pid in $(pgrep -f "prakasa" || true); do
    if [ -n "$pid" ] && [ "$pid" != "$$" ]; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
else
  pkill -f "prakasa" || true
fi
sleep 2

# Create logs directory
mkdir -p logs

# Check for config.yaml
if [ ! -f "config.yaml" ]; then
    if [ -f "config.template.yaml" ]; then
        echo -e "${YELLOW}⚠️  config.yaml not found. Creating from config.template.yaml...${NC}"
        cp config.template.yaml config.yaml
    else
        echo -e "${RED}❌ config.yaml and config.template.yaml not found!${NC}"
        exit 1
    fi
fi

echo ""
echo -e "${GREEN}🚀 Starting Prakasa Cluster...${NC}"
echo ""

# Start Node 1 (Scheduler - Primary)
echo -e "${BLUE}📡 Starting Scheduler (Node 1)...${NC}"
nohup prakasa run --config config.yaml > logs/prakasa-scheduler.log 2>&1 &
NODE1_PID=$!
echo -e "${GREEN}✓ Scheduler started (PID: $NODE1_PID)${NC}"
echo -e "${GREEN}  Listening on http://localhost:3001${NC}"

# Wait for scheduler to fully initialize
echo ""
echo -e "${YELLOW}⏳ Waiting for scheduler to initialize (10 seconds)...${NC}"
sleep 10

# Start Node 2 (Worker - joins scheduler)
echo ""
echo -e "${BLUE}📡 Starting Worker (Node 2) on port 3002...${NC}"
nohup prakasa join --config config.yaml --port 3002 > logs/prakasa-worker-3002.log 2>&1 &
NODE2_PID=$!
echo -e "${GREEN}✓ Worker started (PID: $NODE2_PID)${NC}"
echo -e "${GREEN}  Connected to scheduler, listening on port 3002${NC}"

echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  ✅ Prakasa Cluster Started Successfully!    ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BLUE}Cluster Configuration:${NC}"
echo "  • Scheduler: http://localhost:3001 (PID: $NODE1_PID)"
echo "  • Worker: port 3002 (PID: $NODE2_PID)"
echo "  • Model: (Check config.yaml)"
echo "  • Architecture: 1 scheduler + 1 worker"
echo ""
echo -e "${YELLOW}📊 Logs:${NC}"
echo "  • Scheduler: logs/prakasa-scheduler.log"
echo "  • Worker: logs/prakasa-worker-3002.log"
echo ""
echo -e "${YELLOW}💡 Tips:${NC}"
echo "  • View scheduler logs: tail -f logs/prakasa-scheduler.log"
echo "  • View worker logs: tail -f logs/prakasa-worker-3002.log"
echo "  • Check cluster status: curl http://localhost:3001"
echo "  • Stop cluster: pkill -f prakasa"
echo ""
echo -e "${YELLOW}⏳ Waiting for cluster to be ready (20 seconds)...${NC}"

# Wait for cluster to be ready
sleep 20

# Health check
echo ""
echo -e "${BLUE}🏥 Performing health checks...${NC}"

check_node() {
  local port=$1
  local response=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:$port 2>/dev/null || echo "000")
  if [ "$response" != "000" ]; then
    echo -e "${GREEN}✓ Node on port $port is responding${NC}"
    return 0
  else
    echo -e "${RED}✗ Node on port $port is not responding${NC}"
    return 1
  fi
}

HEALTH_OK=true
check_node 3001 || HEALTH_OK=false
check_node 3002 || HEALTH_OK=false

echo ""
if [ "$HEALTH_OK" = true ]; then
  echo -e "${GREEN}🎉 Cluster is healthy and ready!${NC}"
  echo ""
  echo "  PRAKASA_CLUSTER_URLS=http://localhost:3001 is the endpoint to use in your app."
  echo ""
  echo -e "${BLUE}Note:${NC} The cluster has 1 scheduler (port 3001) + 1 worker (port 3002)."
  echo "Your app will communicate with the scheduler, which distributes work to workers."
else
  echo -e "${YELLOW}⚠️  Scheduler may still be initializing. Check the logs for details.${NC}"
  echo "First inference will take longer as models are loaded."
fi

echo ""
