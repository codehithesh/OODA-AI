#!/usr/bin/env zsh
# =============================================================================
# start.sh — One-shot setup & launcher for the OODA multi-agent stack
#
# Usage:
#   chmod +x start.sh && ./start.sh
#
# What it does (skipping steps already satisfied):
#   1. Checks macOS + zsh
#   2. Installs Homebrew if missing
#   3. Installs uv (Python package manager) if missing
#   4. Installs Docker Desktop via Homebrew if missing
#   5. Ensures Docker daemon is running (starts Docker.app if needed)
#   6. Ensures docker compose plugin is available
#   7. Validates .env exists (copies .env.example if not, warns about keys)
#   8. Checks required API keys are set in .env
#   9. Wipes corrupt open-webui volume if detected
#  10. Runs `make dev` (docker compose up --build -d)
#  11. Waits for all services to become healthy
#  12. Prints live service URLs
# =============================================================================

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
YEL='\033[1;33m'
GRN='\033[0;32m'
BLU='\033[0;34m'
CYN='\033[0;36m'
BOLD='\033[1m'
RST='\033[0m'

info()    { echo -e "${BLU}[INFO]${RST}  $*"; }
success() { echo -e "${GRN}[OK]${RST}    $*"; }
warn()    { echo -e "${YEL}[WARN]${RST}  $*"; }
error()   { echo -e "${RED}[ERROR]${RST} $*" >&2; }
step()    { echo -e "\n${BOLD}${CYN}━━ $* ${RST}"; }

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="${0:A:h}"                          # absolute dir of this script
PROJECT_DIR="${SCRIPT_DIR}/multi-agent-backend"
ENV_FILE="${PROJECT_DIR}/.env"
ENV_EXAMPLE="${PROJECT_DIR}/.env.example"

# ── 1. Platform check ─────────────────────────────────────────────────────────
step "1/10  Platform check"
if [[ "$(uname -s)" != "Darwin" ]]; then
  error "This script targets macOS only. Detected: $(uname -s)"
  exit 1
fi
success "macOS $(sw_vers -productVersion) on $(uname -m)"

# ── 2. Homebrew ───────────────────────────────────────────────────────────────
step "2/10  Homebrew"
if command -v brew &>/dev/null; then
  success "Homebrew already installed ($(brew --version | head -1))"
else
  warn "Homebrew not found — installing..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

  # Add brew to PATH for Apple Silicon or Intel
  if [[ -f /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
    echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "${HOME}/.zprofile"
  elif [[ -f /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
  success "Homebrew installed"
fi

# Ensure brew is on PATH for this session
if [[ -f /opt/homebrew/bin/brew ]]; then
  eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
fi

# ── 3. uv (Python package manager) ───────────────────────────────────────────
step "3/10  uv"
if command -v uv &>/dev/null; then
  success "uv already installed ($(uv --version))"
else
  warn "uv not found — installing via Homebrew..."
  if brew install uv 2>/dev/null; then
    success "uv installed via Homebrew"
  else
    warn "Homebrew install failed, trying curl installer..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Load into current session
    if [[ -f "${HOME}/.local/bin/env" ]]; then
      source "${HOME}/.local/bin/env"
    fi
    export PATH="${HOME}/.local/bin:${PATH}"
  fi
  success "uv $(uv --version)"
fi

# ── 4. Docker Desktop ─────────────────────────────────────────────────────────
step "4/10  Docker Desktop"
DOCKER_APP="/Applications/Docker.app"

if [[ -d "${DOCKER_APP}" ]]; then
  success "Docker Desktop app found at ${DOCKER_APP}"
else
  warn "Docker Desktop not found — installing via Homebrew Cask..."
  brew install --cask docker
  success "Docker Desktop installed"
fi

# ── 5. Docker daemon ──────────────────────────────────────────────────────────
step "5/10  Docker daemon"
_docker_running() { docker info &>/dev/null; }

if _docker_running; then
  success "Docker daemon is already running"
else
  warn "Docker daemon is not running — launching Docker Desktop..."
  open -a Docker

  info "Waiting for Docker daemon (up to 120 s)..."
  WAIT=0
  until _docker_running || (( WAIT >= 120 )); do
    sleep 3
    (( WAIT += 3 ))
    printf "."
  done
  echo ""

  if _docker_running; then
    success "Docker daemon is up (${WAIT}s)"
  else
    error "Docker daemon did not start within 120 seconds."
    error "Please open Docker Desktop manually and re-run this script."
    exit 1
  fi
fi

# ── 6. docker compose plugin ──────────────────────────────────────────────────
step "6/10  docker compose plugin"
if docker compose version &>/dev/null; then
  success "docker compose $(docker compose version --short 2>/dev/null || echo 'available')"
else
  # Older Docker installs ship as a standalone binary
  if command -v docker-compose &>/dev/null; then
    warn "Only 'docker-compose' (v1) found — the Makefile uses 'docker compose' (v2)."
    warn "Updating Docker Desktop is recommended. Attempting to add compose plugin..."
    mkdir -p "${HOME}/.docker/cli-plugins"
    COMPOSE_URL="https://github.com/docker/compose/releases/latest/download/docker-compose-darwin-$(uname -m)"
    curl -SL "${COMPOSE_URL}" -o "${HOME}/.docker/cli-plugins/docker-compose"
    chmod +x "${HOME}/.docker/cli-plugins/docker-compose"
    success "docker compose v2 plugin installed"
  else
    error "docker compose plugin not available. Please reinstall Docker Desktop."
    exit 1
  fi
fi

# ── 7. .env file ──────────────────────────────────────────────────────────────
step "7/10  .env file"
if [[ -f "${ENV_FILE}" ]]; then
  success ".env already exists"
else
  warn ".env not found — copying from .env.example..."
  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  warn "A default .env has been created. You MUST set at least one LLM API key:"
  warn "  OPENAI_API_KEY=sk-..."
  warn "  ANTHROPIC_API_KEY=sk-ant-..."
  warn "Edit ${ENV_FILE} and re-run this script."
  echo ""
  read -r "?Press Enter to open .env in your default editor (or Ctrl-C to abort)..."
  "${EDITOR:-nano}" "${ENV_FILE}"
fi

# ── 8. API key check ──────────────────────────────────────────────────────────
step "8/10  API key validation"
_get_env_val() { grep -E "^${1}=" "${ENV_FILE}" | cut -d'=' -f2- | tr -d '"' | tr -d "'"; }

# All supported provider keys (local providers need no key)
PROVIDER_KEYS=(
  ANTHROPIC_API_KEY
  OPENAI_API_KEY
  XAI_API_KEY
  GEMINI_API_KEY
  GROQ_API_KEY
  MISTRAL_API_KEY
  PERPLEXITYAI_API_KEY
  COHERE_API_KEY
  CEREBRAS_API_KEY
  SAMBANOVA_API_KEY
  FIREWORKS_AI_API_KEY
  TOGETHER_API_KEY
  DEEPINFRA_API_KEY
  HYPERBOLIC_API_KEY
  REPLICATE_API_KEY
  HUGGINGFACE_API_KEY
  NVIDIA_NIM_API_KEY
  LAMBDA_API_KEY
  DEEPSEEK_API_KEY
  MOONSHOT_API_KEY
  DASHSCOPE_API_KEY
  ZAI_API_KEY
  MINIMAX_API_KEY
  VOLCENGINE_API_KEY
  QIANFAN_AK
  TENCENTCLOUD_SECRET_ID
  MODELSCOPE_API_KEY
  AWS_ACCESS_KEY_ID
  AZURE_API_KEY
  AZURE_AI_API_KEY
  WATSONX_API_KEY
  DATABRICKS_API_KEY
  OPENROUTER_API_KEY
  AIML_API_KEY
  NOVITA_API_KEY
)

FOUND_KEY=false
for key in "${PROVIDER_KEYS[@]}"; do
  val=$(_get_env_val "${key}")
  if [[ -n "${val}" ]]; then
    success "${key} is set"
    FOUND_KEY=true
  fi
done

# Local providers (Ollama, LM Studio, vLLM) don't need a key — if agent-default
# in litellm_config.yaml points to one of those, that's fine too.
if ! $FOUND_KEY; then
  warn "No cloud LLM provider API key found in .env"
  warn "If you're using a local provider (Ollama, LM Studio, vLLM) this is fine."
  warn "Otherwise set at least one key and uncomment the matching block in litellm_config.yaml"
fi

# ── 9. Corrupt open-webui volume detection & repair ───────────────────────────
step "9/10  Volume health check"
WEBUI_VOLUME="multi-agent-stack_open_webui_data"

_volume_exists() { docker volume ls --format '{{.Name}}' | grep -q "^${WEBUI_VOLUME}$"; }
_webui_crashed() {
  # Check if the container last exited with the known SQLite migration error
  docker logs multi-agent-stack-open-webui-1 2>&1 \
    | grep -q "no such table: config" 2>/dev/null
}

if _volume_exists; then
  # Try to detect corruption by inspecting a running/recent container
  if docker ps -a --format '{{.Names}}' | grep -q "open-webui" && _webui_crashed; then
    warn "Corrupt open-webui SQLite volume detected — wiping it..."
    docker rm -f multi-agent-stack-open-webui-1 2>/dev/null || true
    docker volume rm "${WEBUI_VOLUME}"
    success "Corrupt volume removed — will be recreated fresh on startup"
  else
    success "open-webui volume looks healthy"
  fi
else
  info "open-webui volume does not exist yet — will be created on first run"
fi

# ── 10. Launch the stack ──────────────────────────────────────────────────────
step "10/10  Starting the stack"
cd "${PROJECT_DIR}"

info "Running: make dev  (docker compose up --build -d)"
make dev

# ── 11. Wait for all services ─────────────────────────────────────────────────
echo ""
info "Waiting for all services to become healthy (up to 3 min)..."

SERVICES=(
  "multi-agent-stack-postgres-1"
  "multi-agent-stack-redis-1"
  "multi-agent-stack-litellm-1"
  "multi-agent-stack-backend-1"
  "multi-agent-stack-open-webui-1"
  "multi-agent-stack-n8n-1"
)

_container_healthy() {
  local status
  status=$(docker inspect --format '{{.State.Health.Status}}' "$1" 2>/dev/null || echo "none")
  [[ "${status}" == "healthy" ]]
}

_container_running() {
  local status
  status=$(docker inspect --format '{{.State.Status}}' "$1" 2>/dev/null || echo "none")
  [[ "${status}" == "running" ]]
}

WAIT=0
MAX_WAIT=180

until (( WAIT >= MAX_WAIT )); do
  ALL_GOOD=true
  for svc in "${SERVICES[@]}"; do
    # Services without healthchecks just need to be running
    if ! _container_healthy "${svc}" && ! _container_running "${svc}"; then
      ALL_GOOD=false
      break
    fi
    # Services with healthchecks must be healthy
    HAS_HEALTH=$(docker inspect --format '{{if .State.Health}}yes{{end}}' "${svc}" 2>/dev/null || echo "")
    if [[ "${HAS_HEALTH}" == "yes" ]] && ! _container_healthy "${svc}"; then
      ALL_GOOD=false
      break
    fi
  done

  if $ALL_GOOD; then break; fi

  sleep 5
  (( WAIT += 5 ))
  printf "  [%3ds] checking..." "${WAIT}"

  # Detect and report any restarting containers
  for svc in "${SERVICES[@]}"; do
    ST=$(docker inspect --format '{{.State.Status}}' "${svc}" 2>/dev/null || echo "?")
    if [[ "${ST}" == "restarting" ]]; then
      printf " ${RED}%s=RESTARTING${RST}" "${svc##*-}"
    fi
  done
  echo ""
done

# ── 12. Final status ──────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GRN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RST}"
echo -e "${BOLD}  🚀  OODA Multi-Agent Stack is ready${RST}"
echo -e "${BOLD}${GRN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RST}"
echo ""

printf "  %-16s %s\n" "Open WebUI"   "→  http://localhost:3000   (models: analytics / monitor / research / simulate)"
printf "  %-16s %s\n" "Backend API"  "→  http://localhost:8000/docs"
printf "  %-16s %s\n" "LiteLLM"      "→  http://localhost:4000"
printf "  %-16s %s\n" "n8n"          "→  http://localhost:5678"
echo ""

# Print per-service health
echo -e "  ${BOLD}Service health:${RST}"
for svc in "${SERVICES[@]}"; do
  LABEL="${svc##*multi-agent-stack-}"   # strip prefix
  LABEL="${LABEL%-1}"                    # strip trailing -1
  ST=$(docker inspect --format '{{.State.Status}}' "${svc}" 2>/dev/null || echo "unknown")
  HS=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "${svc}" 2>/dev/null || echo "")

  if [[ "${HS}" == "healthy" ]]; then
    echo -e "    ${GRN}✔${RST}  ${LABEL} (healthy)"
  elif [[ "${ST}" == "running" && -z "${HS}" ]]; then
    echo -e "    ${GRN}✔${RST}  ${LABEL} (running)"
  elif [[ "${ST}" == "running" && "${HS}" == "starting" ]]; then
    echo -e "    ${YEL}◌${RST}  ${LABEL} (health: starting — give it a moment)"
  else
    echo -e "    ${RED}✘${RST}  ${LABEL} — status: ${ST} / health: ${HS:-n/a}"
    echo -e "         Run: docker logs ${svc} --tail 30"
  fi
done

echo ""
echo -e "  ${BLU}Useful commands:${RST}"
echo "    make logs     — tail backend logs"
echo "    make ps       — service status"
echo "    make down     — stop the stack"
echo "    make reset    — stop + wipe all volumes"
echo ""
