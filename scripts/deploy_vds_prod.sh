#!/usr/bin/env bash
# Создаёт виртуальное окружение хоста и передаёт запуск оркестратору Python.
# Политика LUKS, секретов и healthcheck живёт в scripts/deploy_vds_prod.py.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 не найден. Подготовка хоста: sudo bash scripts/host_bootstrap.sh" >&2
    exit 1
fi

VENV="${PROJECT_ROOT}/.venv"
if [[ ! -x "${VENV}/bin/python" ]]; then
    python3 -m venv "${VENV}"
fi

"${VENV}/bin/python" -m pip install --upgrade pip
"${VENV}/bin/python" -m pip install -r "${SCRIPT_DIR}/host-requirements.txt"
exec "${VENV}/bin/python" "${SCRIPT_DIR}/deploy_vds_prod.py" "$@"
