#!/usr/bin/env bash
# MinerTune uninstaller. Stops and removes the systemd service. Program files, run data
# (runs/), config.json (contains the PIN) and the service user are only removed after an
# explicit "y" - the default is always to keep them.
#
#   sudo ./deinstall.sh
#   sudo INSTALL_DIR=/srv/minertune ./deinstall.sh
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/minertune}"
SERVICE_NAME="minertune"
SERVICE_USER="minertune"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
ask()  {  # ask "question" -> 0 only on explicit y/yes; no terminal -> no
    local a=""
    if (exec </dev/tty) 2>/dev/null; then read -r -p "$1 [y/N] " a </dev/tty || a=""; fi
    [[ "${a,,}" == y || "${a,,}" == yes ]]
}

[[ ${EUID} -eq 0 ]] || die "please run with sudo:  sudo ./deinstall.sh"
# Schutz vor falschem Pfad: absolut, nicht /, Name enthaelt 'minertune'
[[ "${INSTALL_DIR}" = /* && "${INSTALL_DIR}" != "/" && "$(basename "${INSTALL_DIR}")" == *minertune* ]] \
    || die "refusing to work on INSTALL_DIR='${INSTALL_DIR}'"

# --- Dienst -----------------------------------------------------------------
if systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1 || [[ -f "${UNIT_PATH}" ]]; then
    info "stopping and disabling ${SERVICE_NAME} (a running sweep resets the miner to its start point)"
    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" >/dev/null 2>&1 || true
fi
if [[ -f "${UNIT_PATH}" ]]; then
    rm -f "${UNIT_PATH}"
    systemctl daemon-reload
    systemctl reset-failed "${SERVICE_NAME}" 2>/dev/null || true
    info "unit ${UNIT_PATH} removed"
fi

# --- Daten und Dateien (nur auf Rueckfrage) ---------------------------------
if [[ -d "${INSTALL_DIR}" ]]; then
    if [[ -d "${INSTALL_DIR}/runs" ]] && ask "Delete run data ${INSTALL_DIR}/runs/ (measurement results, cannot be undone)?"; then
        rm -rf -- "${INSTALL_DIR}/runs"; info "runs/ deleted"
    fi
    if [[ -f "${INSTALL_DIR}/config.json" ]] && ask "Delete ${INSTALL_DIR}/config.json (settings + control PIN)?"; then
        rm -f -- "${INSTALL_DIR}/config.json"; info "config.json deleted"
    fi
    if ask "Delete program files in ${INSTALL_DIR} (code, venv; kept data stays)?"; then
        rm -rf -- "${INSTALL_DIR}/venv" "${INSTALL_DIR}/lang" "${INSTALL_DIR}/__pycache__"
        rm -f -- "${INSTALL_DIR}"/{control.py,sweep_full.py,sweep.py,tuner.py,requirements.txt,config.example.json} \
                 "${INSTALL_DIR}"/{LICENSE,THIRD_PARTY_LICENSES.md,README.md,minertune.service,deinstall.sh} \
                 "${INSTALL_DIR}"/config.json.tmp
        rmdir "${INSTALL_DIR}" 2>/dev/null && info "${INSTALL_DIR} removed" \
            || info "program files removed; ${INSTALL_DIR} kept (still contains: $(ls -A "${INSTALL_DIR}" | tr '\n' ' '))"
    fi
fi

# --- Service-User -----------------------------------------------------------
if id -u "${SERVICE_USER}" >/dev/null 2>&1 && ask "Remove system user '${SERVICE_USER}'?"; then
    userdel "${SERVICE_USER}" 2>/dev/null && info "user '${SERVICE_USER}' removed" \
        || info "user '${SERVICE_USER}' could not be removed (still owns processes?)"
fi
info "MinerTune service removed."
