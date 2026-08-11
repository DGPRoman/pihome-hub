#!/usr/bin/env bash
#
# Provision pihome-hub as a systemd service, or upgrade an install already in place.
#
# Paths, the service account and the GPIO group are read out of pihome-hub.service,
# so this script cannot drift away from the unit it installs. Re-running is safe:
# generated keys and edited YAML are never overwritten.
#
# Usage: sudo deploy/install.sh

set -euo pipefail

DEPLOY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SOURCE="$DEPLOY_DIR/pihome-hub.service"
UNIT_NAME="$(basename -- "$UNIT_SOURCE")"
REPO_ROOT="$(dirname -- "$DEPLOY_DIR")"
readonly DEPLOY_DIR UNIT_SOURCE UNIT_NAME REPO_ROOT

say() { printf '==> %s\n' "$*"; }
die() {
    printf 'install.sh: %s\n' "$*" >&2
    exit 1
}

[[ -f $UNIT_SOURCE ]] || die "$UNIT_SOURCE is missing"

# The last assignment wins, the way systemd itself reads a unit file.
unit_value() {
    local value
    value="$(sed -n "s/^$1=//p" "$UNIT_SOURCE" | tail -n 1)"
    [[ -n $value ]] || die "$UNIT_NAME declares no $1= — cannot tell where to install"
    printf '%s\n' "$value"
}

SERVICE_USER="$(unit_value User)"
GPIO_GROUP="$(unit_value SupplementaryGroups)"
ENV_FILE="$(unit_value EnvironmentFile)"
INSTALL_ROOT="$(unit_value WorkingDirectory)"
VENV="$(dirname -- "$(dirname -- "$(unit_value ExecStart)")")"
CONFIG_DIR="$(dirname -- "$ENV_FILE")"
RELAY_CONFIG="$CONFIG_DIR/relays.yaml"
readonly SERVICE_USER GPIO_GROUP ENV_FILE INSTALL_ROOT VENV CONFIG_DIR RELAY_CONFIG

# -- Preflight ---------------------------------------------------------------

[[ $EUID -eq 0 ]] || die "run as root: sudo $0"
command -v systemctl >/dev/null || die "no systemctl — this installs a systemd service"
python3 -c 'import venv' >/dev/null 2>&1 || die "python3 venv module missing (apt install python3-venv)"

[[ $REPO_ROOT == "$INSTALL_ROOT" ]] || die "$UNIT_NAME runs the service from $INSTALL_ROOT,
but this checkout is at $REPO_ROOT. Clone it there, or point WorkingDirectory=
and ExecStart= at this path instead."

getent group "$GPIO_GROUP" >/dev/null || die "$UNIT_NAME needs the '$GPIO_GROUP' group, which this host does not have.
Raspberry Pi OS ships it; elsewhere, either create it (groupadd --system
$GPIO_GROUP) or drop SupplementaryGroups= if there is no GPIO to reach."

# Real pins are opted into by the hardware being present, not by a flag: the extra
# only installs where there is something for it to drive.
gpio_chips=(/dev/gpiochip*)
if [[ -e /dev/gpiochip0 ]]; then
    backend=gpiozero
    extras='[rpi]'
elif [[ -e ${gpio_chips[0]} ]]; then
    die "this host has GPIO chips but no /dev/gpiochip0, the only one $UNIT_NAME
allows. Point DeviceAllow= at yours: ${gpio_chips[*]}"
else
    backend=mock
    extras=''
fi

say "installing from $REPO_ROOT with the $backend backend"

# -- Service account ---------------------------------------------------------

if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    say "user $SERVICE_USER is already present"
else
    say "creating user $SERVICE_USER"
    useradd --system --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# -- Package -----------------------------------------------------------------

if [[ ! -x $VENV/bin/pip ]]; then
    say "creating $VENV"
    python3 -m venv "$VENV"
fi

# Installed, not linked with -e, so pip byte-compiles once here rather than the
# service recompiling on every start against a read-only filesystem.
say "installing the package into $VENV"
"$VENV/bin/pip" install --quiet --upgrade "$REPO_ROOT$extras"

# -- Configuration -----------------------------------------------------------

# The directory is group-readable because the service process reads its YAML as
# $SERVICE_USER. hub.env stays root-only: systemd opens it as PID 1 and passes the
# values in, so the service account never needs the secrets themselves.
install -d -m 750 -o root -g "$SERVICE_USER" "$CONFIG_DIR"

if [[ -e $ENV_FILE ]]; then
    say "keeping the existing $ENV_FILE"
else
    say "writing $ENV_FILE with freshly generated keys"
    key() { python3 -c 'import secrets; print(secrets.token_urlsafe(48))'; }
    install -m 600 -o root -g root /dev/null "$ENV_FILE"
    cat >"$ENV_FILE" <<EOF
# Written by deploy/install.sh. Bare KEY=value — systemd is not a shell, so quotes
# become part of the value and \$FOO is not expanded.
PIHOME_RELAY_API_KEY=$(key)
PIHOME_SENSOR_API_KEY=$(key)
PIHOME_GPIO_BACKEND=$backend
PIHOME_PORT=5002
PIHOME_RELAY_CONFIG_PATH=$RELAY_CONFIG
# Optional: a path that does not exist means the feature is simply not in use.
PIHOME_SENSOR_CONFIG_PATH=$CONFIG_DIR/sensors.yaml
PIHOME_AUTOMATION_CONFIG_PATH=$CONFIG_DIR/automation.yaml
# Reaching this from the LAN means setting PIHOME_HOST, and it speaks plain HTTP
# with a static key — put a VPN or a TLS proxy in front, not an open port.
EOF
fi

if [[ -e $RELAY_CONFIG ]]; then
    say "keeping the existing $RELAY_CONFIG"
    relays_are_yours=yes
else
    say "copying the example wiring to $RELAY_CONFIG"
    install -m 640 -o root -g "$SERVICE_USER" \
        "$REPO_ROOT/config/relays.example.yaml" "$RELAY_CONFIG"
    relays_are_yours=no
fi

# -- Unit --------------------------------------------------------------------

say "installing $UNIT_NAME"
install -m 644 -o root -g root "$UNIT_SOURCE" "/etc/systemd/system/$UNIT_NAME"
systemctl daemon-reload
systemctl enable "$UNIT_NAME" >/dev/null

# The example names pins it guessed for somebody else's board. Starting on it would
# close relays chosen at random, so a fresh install stops here and says so.
if [[ $relays_are_yours == no ]]; then
    cat <<EOF

Enabled but not started: $RELAY_CONFIG is still the example.
Describe your wiring there, then start it:

  sudoedit $RELAY_CONFIG
  systemctl start $UNIT_NAME

The relay key clients need is in $ENV_FILE.
EOF
    exit 0
fi

say "restarting $UNIT_NAME"
systemctl restart "$UNIT_NAME"

# -- Verify ------------------------------------------------------------------

port="$(sed -n 's/^PIHOME_PORT=//p' "$ENV_FILE" | tail -n 1)"
# config.py's default, for a hub.env that predates this script.
port="${port:-5002}"

# python3 rather than curl: the venv guarantees the first, a minimal Raspberry Pi OS
# image does not guarantee the second.
health_ok() {
    python3 - "$1" <<'PY' 2>/dev/null
import sys, urllib.request
urllib.request.urlopen(sys.argv[1], timeout=2)
PY
}

say "waiting for /health on port $port"
for _ in {1..30}; do
    if health_ok "http://127.0.0.1:$port/health"; then
        say "pihome-hub is up. Its relay key is in $ENV_FILE"
        exit 0
    fi
    sleep 0.5
done

printf 'install.sh: no answer from /health after 15s\n' >&2
systemctl --no-pager --full status "$UNIT_NAME" || true
journalctl -u "$UNIT_NAME" -n 30 --no-pager || true
exit 1
