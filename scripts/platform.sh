#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"
LOCAL_DIR="${REPO_ROOT}/infrastructure/local"

readonly PROJECT_OWNER="eks-gitops-reliability-platform"
readonly REGISTRY_NAME="gitops-registry"
readonly REGISTRY_HOST="localhost"
readonly REGISTRY_HOST_PORT="5001"
readonly REGISTRY_CONTAINER_PORT="5000"
readonly REGISTRY_URL="http://localhost:5001/v2/"
readonly REGISTRY_IMAGE="registry:3.1.1@sha256:1be55279f18a2fe1a74edf2664cac61c1bea305b7b4642dab412e7affdcb3e33"
readonly REGISTRY_OWNER_LABEL_KEY="io.gitops-reliability.platform.owner"
readonly KUBE_OWNER_LABEL_KEY="platform.gitops-reliability.io/owner"
readonly KIND_NODE_IMAGE="kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed"
readonly KIND_WAIT_TIMEOUT="120s"
readonly CONTAINERD_REGISTRY_DIR="/etc/containerd/certs.d/localhost:5001"
readonly CONTAINERD_REGISTRY_HOST='[host."http://gitops-registry:5000"]'
readonly -a CLUSTERS=("gitops-dev" "gitops-prod")

log() {
  printf '[platform] %s\n' "$*"
}

warn() {
  printf '[platform] WARNING: %s\n' "$*" >&2
}

die() {
  printf '[platform] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'USAGE'
Usage: scripts/platform.sh <up|status|smoke|down|doctor>

  up      Create or reconcile the two owned kind clusters and local registry.
  status  Report registry and cluster health without changing resources.
  smoke   Verify the registry HTTP endpoint and Ready nodes without changes.
  down    Delete only the two owned clusters and the owned registry.
  doctor  Check local prerequisites and reserved-name conflicts without changes.
USAGE
}

require_command() {
  local command_name="$1"
  command -v "${command_name}" >/dev/null 2>&1 || die "required command not found: ${command_name}"
}

require_platform_commands() {
  require_command docker
  require_command kind
  require_command kubectl
  docker info >/dev/null 2>&1 || die "Docker is installed but its daemon is not reachable"
}

validate_local_files() {
  local required_file
  for required_file in \
    "${LOCAL_DIR}/kind-dev.yaml" \
    "${LOCAL_DIR}/kind-prod.yaml" \
    "${LOCAL_DIR}/local-registry-hosting.yaml"; do
    [[ -r "${required_file}" ]] || die "required local configuration is missing: ${required_file}"
  done
}

cluster_context() {
  printf 'kind-%s' "$1"
}

cluster_config() {
  case "$1" in
    gitops-dev) printf '%s/kind-dev.yaml' "${LOCAL_DIR}" ;;
    gitops-prod) printf '%s/kind-prod.yaml' "${LOCAL_DIR}" ;;
    *) die "refusing unknown cluster name: $1" ;;
  esac
}

cluster_exists() {
  local wanted="$1"
  local found

  while IFS= read -r found; do
    [[ "${found}" == "${wanted}" ]] && return 0
  done < <(kind get clusters 2>/dev/null)
  return 1
}

registry_exists() {
  docker container inspect "${REGISTRY_NAME}" >/dev/null 2>&1
}

docker_inspect_value() {
  local template="$1"
  docker container inspect --format "${template}" "${REGISTRY_NAME}" 2>/dev/null
}

assert_registry_owned() {
  local actual_name
  local actual_owner
  local actual_image
  local actual_binding

  actual_name="$(docker_inspect_value '{{.Name}}')"
  actual_owner="$(docker_inspect_value "{{ index .Config.Labels \"${REGISTRY_OWNER_LABEL_KEY}\" }}")"
  actual_image="$(docker_inspect_value '{{.Config.Image}}')"
  actual_binding="$(docker_inspect_value "{{with (index (index .HostConfig.PortBindings \"${REGISTRY_CONTAINER_PORT}/tcp\") 0)}}{{.HostIp}}:{{.HostPort}}{{end}}")"

  [[ "${actual_name}" == "/${REGISTRY_NAME}" ]] || die "registry lookup did not resolve the exact reserved name ${REGISTRY_NAME}"
  [[ "${actual_owner}" == "${PROJECT_OWNER}" ]] || die "container ${REGISTRY_NAME} exists but is not owned by ${PROJECT_OWNER}; refusing to modify it"
  [[ "${actual_image}" == "${REGISTRY_IMAGE}" ]] || die "owned registry uses ${actual_image}, expected ${REGISTRY_IMAGE}; remove it explicitly before changing versions"
  [[ "${actual_binding}" == "127.0.0.1:${REGISTRY_HOST_PORT}" ]] || die "owned registry has unexpected port binding ${actual_binding}; expected 127.0.0.1:${REGISTRY_HOST_PORT}"
}

registry_is_running() {
  [[ "$(docker_inspect_value '{{.State.Running}}')" == "true" ]]
}

wait_for_registry() {
  local attempt
  for attempt in {1..60}; do
    if registry_is_running && (true >/dev/tcp/127.0.0.1/"${REGISTRY_HOST_PORT}") 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  die "registry ${REGISTRY_NAME} did not become reachable on localhost:${REGISTRY_HOST_PORT}"
}

ensure_registry() {
  if registry_exists; then
    assert_registry_owned
    if registry_is_running; then
      log "registry ${REGISTRY_NAME} already running"
    else
      log "starting owned registry ${REGISTRY_NAME}"
      docker start "${REGISTRY_NAME}" >/dev/null
    fi
  else
    log "creating registry ${REGISTRY_NAME} on localhost:${REGISTRY_HOST_PORT}"
    docker run --detach \
      --restart=always \
      --publish "127.0.0.1:${REGISTRY_HOST_PORT}:${REGISTRY_CONTAINER_PORT}" \
      --label "${REGISTRY_OWNER_LABEL_KEY}=${PROJECT_OWNER}" \
      --name "${REGISTRY_NAME}" \
      "${REGISTRY_IMAGE}" >/dev/null
  fi
  wait_for_registry
}

cluster_is_owned() {
  local cluster="$1"
  local context
  local owned_nodes

  context="$(cluster_context "${cluster}")"
  owned_nodes="$(kubectl --context "${context}" get nodes \
    --selector "${KUBE_OWNER_LABEL_KEY}=${PROJECT_OWNER}" \
    --output name \
    --request-timeout=5s 2>/dev/null || true)"
  [[ -n "${owned_nodes}" ]]
}

ensure_registry_network() {
  local network_id
  network_id="$(docker_inspect_value '{{with index .NetworkSettings.Networks "kind"}}{{.NetworkID}}{{end}}')"
  if [[ -z "${network_id}" ]]; then
    log "connecting ${REGISTRY_NAME} to the kind network"
    docker network connect kind "${REGISTRY_NAME}"
  fi
}

configure_node_registry() {
  local cluster="$1"
  local node
  local nodes

  nodes="$(kind get nodes --name "${cluster}")"
  [[ -n "${nodes}" ]] || die "kind returned no nodes for cluster ${cluster}"
  while IFS= read -r node; do
    [[ -n "${node}" ]] || continue
    docker exec "${node}" mkdir -p "${CONTAINERD_REGISTRY_DIR}"
    printf '%s\n' "${CONTAINERD_REGISTRY_HOST}" | \
      docker exec --interactive "${node}" cp /dev/stdin "${CONTAINERD_REGISTRY_DIR}/hosts.toml"
  done <<<"${nodes}"
}

install_registry_discovery() {
  local cluster="$1"
  kubectl --context "$(cluster_context "${cluster}")" apply \
    --filename "${LOCAL_DIR}/local-registry-hosting.yaml" >/dev/null
}

wait_for_cluster() {
  local cluster="$1"
  local context
  context="$(cluster_context "${cluster}")"

  kubectl --context "${context}" wait \
    --for=condition=Ready nodes --all --timeout="${KIND_WAIT_TIMEOUT}" >/dev/null
  kubectl --context "${context}" rollout status \
    --namespace kube-system deployment/coredns --timeout="${KIND_WAIT_TIMEOUT}" >/dev/null
}

ensure_cluster() {
  local cluster="$1"

  if cluster_exists "${cluster}"; then
    cluster_is_owned "${cluster}" || die "cluster ${cluster} exists but lacks this project's ownership label; refusing to adopt it"
    log "cluster ${cluster} already exists"
  else
    log "creating cluster ${cluster}"
    kind create cluster \
      --name "${cluster}" \
      --image "${KIND_NODE_IMAGE}" \
      --config "$(cluster_config "${cluster}")" \
      --wait "${KIND_WAIT_TIMEOUT}"
  fi

  ensure_registry_network
  configure_node_registry "${cluster}"
  install_registry_discovery "${cluster}"
  wait_for_cluster "${cluster}"
  log "cluster ${cluster} is ready"
}

up_platform() {
  require_platform_commands
  validate_local_files
  ensure_registry

  local cluster
  for cluster in "${CLUSTERS[@]}"; do
    ensure_cluster "${cluster}"
  done
  log "local platform is ready"
}

status_platform() {
  require_platform_commands

  local unhealthy=0
  local cluster
  local context

  if ! registry_exists; then
    printf 'registry/%s missing\n' "${REGISTRY_NAME}"
    unhealthy=1
  elif ! assert_registry_owned; then
    unhealthy=1
  elif registry_is_running; then
    printf 'registry/%s running localhost:%s\n' "${REGISTRY_NAME}" "${REGISTRY_HOST_PORT}"
  else
    printf 'registry/%s stopped\n' "${REGISTRY_NAME}"
    unhealthy=1
  fi

  for cluster in "${CLUSTERS[@]}"; do
    context="$(cluster_context "${cluster}")"
    if ! cluster_exists "${cluster}"; then
      printf 'cluster/%s missing\n' "${cluster}"
      unhealthy=1
    elif ! cluster_is_owned "${cluster}"; then
      printf 'cluster/%s unowned-or-unreachable\n' "${cluster}"
      unhealthy=1
    elif kubectl --context "${context}" get --raw=/readyz --request-timeout=5s >/dev/null 2>&1; then
      printf 'cluster/%s ready\n' "${cluster}"
    else
      printf 'cluster/%s not-ready\n' "${cluster}"
      unhealthy=1
    fi
  done

  return "${unhealthy}"
}

smoke_platform() {
  require_platform_commands
  require_command curl

  curl --fail --silent --show-error --max-time 5 "${REGISTRY_URL}" >/dev/null || \
    die "registry API did not respond successfully at ${REGISTRY_URL}"
  printf 'registry/%s api-ready\n' "${REGISTRY_NAME}"

  local cluster
  local nodes
  for cluster in "${CLUSTERS[@]}"; do
    nodes="$(kubectl --context "$(cluster_context "${cluster}")" get nodes --no-headers --request-timeout=5s)" || \
      die "could not get nodes from ${cluster}"
    [[ -n "${nodes}" ]] || die "cluster ${cluster} reported no nodes"
    if ! awk 'NF < 2 || $2 !~ /^Ready(,|$)/ { exit 1 }' <<<"${nodes}"; then
      printf '%s\n' "${nodes}" >&2
      die "cluster ${cluster} has a node that is not Ready"
    fi
    printf 'cluster/%s nodes-ready\n' "${cluster}"
  done
}

down_platform() {
  require_platform_commands

  local failure=0
  local cluster
  local index

  for ((index=${#CLUSTERS[@]} - 1; index >= 0; index--)); do
    cluster="${CLUSTERS[index]}"
    if ! cluster_exists "${cluster}"; then
      log "cluster ${cluster} already absent"
    elif ! cluster_is_owned "${cluster}"; then
      warn "cluster ${cluster} is not verifiably owned; refusing to delete it"
      failure=1
    else
      log "deleting owned cluster ${cluster}"
      kind delete cluster --name "${cluster}"
    fi
  done

  if ((failure != 0)); then
    warn "leaving the registry in place because at least one reserved cluster was not deleted"
    return "${failure}"
  fi

  if registry_exists; then
    assert_registry_owned
    log "deleting owned registry ${REGISTRY_NAME}"
    docker container rm --force "${REGISTRY_NAME}" >/dev/null
  else
    log "registry ${REGISTRY_NAME} already absent"
  fi
  log "local platform is down"
}

doctor_platform() {
  local failed=0
  local command_name
  local required_file
  local cluster

  for command_name in docker kind kubectl; do
    if command -v "${command_name}" >/dev/null 2>&1; then
      printf 'tool/%s found\n' "${command_name}"
    else
      printf 'tool/%s missing\n' "${command_name}"
      failed=1
    fi
  done

  for required_file in \
    "${LOCAL_DIR}/kind-dev.yaml" \
    "${LOCAL_DIR}/kind-prod.yaml" \
    "${LOCAL_DIR}/local-registry-hosting.yaml"; do
    if [[ -r "${required_file}" ]]; then
      printf 'config/%s found\n' "$(basename "${required_file}")"
    else
      printf 'config/%s missing\n' "$(basename "${required_file}")"
      failed=1
    fi
  done

  if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
      printf 'docker/daemon reachable\n'
      if registry_exists; then
        if ! assert_registry_owned; then
          failed=1
        else
          printf 'reserved-name/%s owned\n' "${REGISTRY_NAME}"
        fi
      fi
    else
      printf 'docker/daemon unreachable\n'
      failed=1
    fi
  fi

  if command -v kind >/dev/null 2>&1 && command -v kubectl >/dev/null 2>&1 && \
     command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    for cluster in "${CLUSTERS[@]}"; do
      if cluster_exists "${cluster}"; then
        if cluster_is_owned "${cluster}"; then
          printf 'reserved-name/%s owned\n' "${cluster}"
        else
          printf 'reserved-name/%s conflict\n' "${cluster}"
          failed=1
        fi
      fi
    done
  fi

  return "${failed}"
}

main() {
  local action="${1:-}"
  [[ $# -le 1 ]] || die "expected exactly one action"

  case "${action}" in
    up) up_platform ;;
    status) status_platform ;;
    smoke) smoke_platform ;;
    down) down_platform ;;
    doctor) doctor_platform ;;
    help|-h|--help|'') usage ;;
    *) usage >&2; die "unknown action: ${action}" ;;
  esac
}

main "$@"
