#!/usr/bin/env bash
# MinerTune installer - Raspberry Pi OS / Debian / Ubuntu (systemd)
#
#   sudo ./install.sh                      # install or update into /opt/minertune
#   sudo INSTALL_DIR=/srv/minertune ./install.sh
#   sudo MINERTUNE_PORT=8479 ./install.sh  # port for a NEW config.json (existing config is kept)
#
# Copies only the package files. An existing config.json and runs/ are never overwritten,
# so running the installer again is also the update procedure.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/minertune}"
SERVICE_NAME="minertune"
SERVICE_USER="minertune"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
DEFAULT_PORT=8477
FALLBACK_PORT=8479
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Paketdateien (bewusst explizit: NIE config.json, runs/, archive/, venv/ kopieren)
PKG_FILES=(control.py sweep_full.py sweep.py tuner.py requirements.txt config.example.json
           LICENSE THIRD_PARTY_LICENSES.md README.md minertune.service deinstall.sh)

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --- Vorbedingungen ---------------------------------------------------------
[[ ${EUID} -eq 0 ]] || die "please run with sudo:  sudo ./install.sh"
[[ "${INSTALL_DIR}" = /* && "${INSTALL_DIR}" != "/" ]] || die "INSTALL_DIR must be an absolute path (not /): ${INSTALL_DIR}"
command -v systemctl >/dev/null || die "systemd (systemctl) is required"
for f in "${PKG_FILES[@]}" lang/en.json; do
    [[ -f "${SRC_DIR}/${f}" ]] || die "package file missing: ${SRC_DIR}/${f}"
done

if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    case " ${ID:-} ${ID_LIKE:-} " in
        *" debian "*|*" ubuntu "*|*" raspbian "*) info "System: ${PRETTY_NAME:-$ID}" ;;
        *) warn "untested distribution '${PRETTY_NAME:-unknown}' - continuing (Debian/Ubuntu/Pi OS are supported)" ;;
    esac
fi

command -v python3 >/dev/null || die "python3 not found - install it first:  sudo apt install python3 python3-venv"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Python >= 3.9 required (found $(python3 -V 2>&1))"
info "Python: $(python3 -V 2>&1)"
if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
    info "python3-venv missing - installing it via apt"
    apt-get update -qq && apt-get install -y -qq python3-venv || die "could not install python3-venv"
fi

# --- Service-User (System-User ohne Login) ----------------------------------
if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    info "service user '${SERVICE_USER}' exists"
else
    info "creating system user '${SERVICE_USER}' (no login shell, no home)"
    useradd --system --user-group --no-create-home --home-dir "${INSTALL_DIR}" \
            --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

# --- Dateien kopieren -------------------------------------------------------
UPDATE=0
[[ -f "${INSTALL_DIR}/control.py" ]] && UPDATE=1
info "$([[ ${UPDATE} -eq 1 ]] && echo updating || echo installing) package files in ${INSTALL_DIR}"
install -d -o root -g "${SERVICE_USER}" -m 1775 "${INSTALL_DIR}"
install -d -o root -g root -m 0755 "${INSTALL_DIR}/lang"
for f in "${PKG_FILES[@]}"; do
    mode=0644; [[ "${f}" == *.sh ]] && mode=0755
    install -o root -g root -m "${mode}" "${SRC_DIR}/${f}" "${INSTALL_DIR}/${f}"
done
for f in "${SRC_DIR}"/lang/*.json; do
    install -o root -g root -m 0644 "${f}" "${INSTALL_DIR}/lang/"
done

# --- venv + Abhaengigkeiten -------------------------------------------------
if [[ ! -x "${INSTALL_DIR}/venv/bin/python" ]]; then
    info "creating virtual environment"
    python3 -m venv "${INSTALL_DIR}/venv"
fi
info "installing Python packages (openpyxl, reportlab)"
"${INSTALL_DIR}/venv/bin/pip" install --disable-pip-version-check -q --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install --disable-pip-version-check -q -r "${INSTALL_DIR}/requirements.txt"
chown -R root:root "${INSTALL_DIR}/venv"
# Bytecode als root vorkompilieren; der Dienst selbst schreibt keinen Bytecode (PYTHONDONTWRITEBYTECODE)
"${INSTALL_DIR}/venv/bin/python" -m compileall -q "${INSTALL_DIR}"/*.py >/dev/null

# --- Daten: runs/ und config.json (nie ueberschreiben) ----------------------
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${INSTALL_DIR}/runs"
CFG="${INSTALL_DIR}/config.json"
port_in_use() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }
if [[ -f "${CFG}" ]]; then
    info "keeping existing config.json"
else
    PORT="${MINERTUNE_PORT:-${DEFAULT_PORT}}"
    if [[ -z "${MINERTUNE_PORT:-}" ]] && port_in_use "${PORT}"; then
        warn "port ${PORT} is already in use (another MinerTune/control.py instance?) - using ${FALLBACK_PORT}"
        PORT="${FALLBACK_PORT}"
    fi
    info "creating config.json from config.example.json (port ${PORT})"
    install -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0600 "${INSTALL_DIR}/config.example.json" "${CFG}"
    python3 - "${CFG}" "${PORT}" <<'PY'
import json, sys
path, port = sys.argv[1], int(sys.argv[2])
with open(path, encoding="utf-8") as f:
    cfg = json.load(f)
cfg["bind_port"] = port
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY
fi
chown "${SERVICE_USER}:${SERVICE_USER}" "${CFG}"
chmod 0600 "${CFG}"

# --- systemd ----------------------------------------------------------------
info "installing systemd unit ${UNIT_PATH}"
sed "s#/opt/minertune#${INSTALL_DIR}#g" "${INSTALL_DIR}/minertune.service" > "${UNIT_PATH}"
chmod 0644 "${UNIT_PATH}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1
if systemctl is-active --quiet "${SERVICE_NAME}"; then
    info "restarting ${SERVICE_NAME}"
    systemctl restart "${SERVICE_NAME}"
else
    info "starting ${SERVICE_NAME}"
    systemctl start "${SERVICE_NAME}"
fi

# --- Abschluss --------------------------------------------------------------
read -r PORT PIN_UNSET < <(python3 - "${CFG}" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
print(cfg.get("bind_port", 8477), int(cfg.get("control_pin") in (None, "", "CHANGEME")))
PY
)
for _ in $(seq 1 20); do port_in_use "${PORT}" && break; sleep 0.5; done
if ! systemctl is-active --quiet "${SERVICE_NAME}"; then
    systemctl status "${SERVICE_NAME}" --no-pager -n 20 || true
    die "service did not start - see: journalctl -u ${SERVICE_NAME} -n 50"
fi
port_in_use "${PORT}" || warn "service is active but port ${PORT} is not listening yet - check: journalctl -u ${SERVICE_NAME} -n 50"
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
info "MinerTune is running (service '${SERVICE_NAME}', user '${SERVICE_USER}')"
echo "    Open in a browser on your LAN:  http://${LAN_IP:-<this-host>}:${PORT}"
echo "    Files: ${INSTALL_DIR}   config: ${CFG} (0600)   run data: ${INSTALL_DIR}/runs/"
if [[ "${PIN_UNSET}" == "1" ]]; then
    warn "No control PIN set yet: open the URL NOW and set the PIN (write actions stay locked until then)."
fi
echo "    Do NOT expose this port to the internet. Logs: journalctl -u ${SERVICE_NAME} -f"
