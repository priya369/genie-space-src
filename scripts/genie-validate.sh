#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel)"
REQUIREMENTS_FILE="${SCRIPT_DIR}/requirements-genie.txt"
VENV_DIR="${SCRIPT_DIR}/.venv-genie"
PYTHON_BIN="python3"

: "${GENIE_MODE:?GENIE_MODE must be 'plan' or 'apply'}"
: "${GENIE_CONFIG_PATH:?GENIE_CONFIG_PATH must be set, e.g. genie-spaces/team/dev}"
: "${DATABRICKS_HOST:?DATABRICKS_HOST must be set}"
: "${DATABRICKS_CLIENT_ID:?DATABRICKS_CLIENT_ID must be set}"
: "${DATABRICKS_CLIENT_SECRET:?DATABRICKS_CLIENT_SECRET must be set}"

cd "${REPO_ROOT}"

echo "=========================================="
echo "Genie Space ${GENIE_MODE}"
echo "Config path: ${GENIE_CONFIG_PATH}"
echo "Current commit: $(git rev-parse --short HEAD)"
echo "=========================================="

CHANGED_FILES="$(git diff --name-only --diff-filter=AM HEAD^1 HEAD -- \
  "${GENIE_CONFIG_PATH}/*.yaml" "${GENIE_CONFIG_PATH}/*.yml")"

DELETED_FILES="$(git diff --name-only --diff-filter=D HEAD^1 HEAD -- \
  "${GENIE_CONFIG_PATH}/*.yaml" "${GENIE_CONFIG_PATH}/*.yml")"

if [[ -z "${CHANGED_FILES}" && -z "${DELETED_FILES}" ]]; then
  echo "No Genie Space YAML changes found. Nothing to ${GENIE_MODE}."
  exit 0
fi

[[ -n "${CHANGED_FILES}" ]] && { echo "Added/modified Genie Spaces:"; echo "${CHANGED_FILES}"; }
[[ -n "${DELETED_FILES}" ]] && { echo "Deleted Genie Spaces:"; echo "${DELETED_FILES}"; }

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "python3 is required"; exit 1
fi
[[ -f "${REQUIREMENTS_FILE}" ]] || { echo "Missing requirements file: ${REQUIREMENTS_FILE}"; exit 1; }

[[ -d "${VENV_DIR}" ]] || "${PYTHON_BIN}" -m venv "${VENV_DIR}"
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r "${REQUIREMENTS_FILE}"

ARGS=(
  "${GENIE_MODE}"
  --host "${DATABRICKS_HOST}"
  --client-id "${DATABRICKS_CLIENT_ID}"
  --client-secret "${DATABRICKS_CLIENT_SECRET}"
)

while IFS= read -r file; do
  [[ -z "${file}" ]] && continue
  echo "Selected added/modified Genie Space: ${file}"
  ARGS+=(--config-file "${file}")
done <<< "${CHANGED_FILES}"

while IFS= read -r file; do
  [[ -z "${file}" ]] && continue
  filename="$(basename "${file}")"
  slug="${filename%.*}"
  echo "Selected deleted Genie Space: ${slug} (${file})"
  ARGS+=(--delete-slug "${slug}")
done <<< "${DELETED_FILES}"

[[ "${GENIE_DRY_RUN:-false}" == "true" ]] && ARGS+=(--dry-run)

python "${SCRIPT_DIR}/genie_space.py" "${ARGS[@]}"
