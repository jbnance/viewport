#!/usr/bin/env bash
# viewport installation script
#
# Usage:
#   sudo bash install.sh [options]
#
# Options:
#   --user NAME      System user that will run the service (default: $SUDO_USER or "pi")
#   --prefix DIR     Directory to install application files (default: /opt/viewport)
#   --config-dir DIR Directory for the runtime config file  (default: /etc/viewport)
#   --no-enable      Install the systemd unit but don't enable or start the service
#
# Example:
#   sudo bash install.sh --user camera --prefix /opt/viewport

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
INSTALL_USER="${SUDO_USER:-pi}"
INSTALL_PREFIX="/opt/viewport"
CONFIG_DIR="/etc/viewport"
SERVICE_NAME="viewport"
ENABLE_SERVICE=true
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case $1 in
        --user)       INSTALL_USER="$2";   shift 2 ;;
        --prefix)     INSTALL_PREFIX="$2"; shift 2 ;;
        --config-dir) CONFIG_DIR="$2";     shift 2 ;;
        --no-enable)  ENABLE_SERVICE=false; shift ;;
        -h|--help)
            sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Run 'bash install.sh --help' for usage." >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    echo "Error: this script must be run with sudo." >&2
    exit 1
fi

if ! id "$INSTALL_USER" &>/dev/null; then
    echo "Error: user '$INSTALL_USER' does not exist." >&2
    echo "  Specify the correct user with --user NAME" >&2
    exit 1
fi

echo "================================================================"
echo "  viewport installer"
echo "  User:       $INSTALL_USER"
echo "  App dir:    $INSTALL_PREFIX"
echo "  Config dir: $CONFIG_DIR"
echo "================================================================"
echo ""

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
echo "==> Installing system dependencies…"
apt-get update -qq
apt-get install -y \
    python3-gi \
    gir1.2-gstreamer-1.0 \
    python3-gst-1.0 \
    python3-yaml \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-tools
echo "    OK"

# ---------------------------------------------------------------------------
# 2. Video group membership
# ---------------------------------------------------------------------------
if groups "$INSTALL_USER" | grep -qw video; then
    echo "==> $INSTALL_USER is already in the 'video' group"
else
    echo "==> Adding $INSTALL_USER to the 'video' group…"
    usermod -aG video "$INSTALL_USER"
    echo "    Note: the user must log out and back in (or reboot) for this to take effect."
fi

# ---------------------------------------------------------------------------
# 3. Application files
# ---------------------------------------------------------------------------
echo "==> Installing application files to $INSTALL_PREFIX…"
mkdir -p "$INSTALL_PREFIX"
cp "$SCRIPT_DIR"/src/main.py \
   "$SCRIPT_DIR"/src/config.py \
   "$SCRIPT_DIR"/src/pipeline.py \
   "$SCRIPT_DIR"/src/cell.py \
   "$INSTALL_PREFIX/"
chmod 644 "$INSTALL_PREFIX"/*.py
chmod 755 "$INSTALL_PREFIX/main.py"
chown -R "$INSTALL_USER":"$INSTALL_USER" "$INSTALL_PREFIX"
echo "    OK"

# ---------------------------------------------------------------------------
# 4. Configuration file
# ---------------------------------------------------------------------------
mkdir -p "$CONFIG_DIR"
if [[ -f "$CONFIG_DIR/config.yaml" ]]; then
    echo "==> Config already exists at $CONFIG_DIR/config.yaml — skipping (not overwritten)"
else
    echo "==> Installing example config to $CONFIG_DIR/config.yaml…"
    cp "$SCRIPT_DIR/config.example.yaml" "$CONFIG_DIR/config.yaml"
    chown "$INSTALL_USER":"$INSTALL_USER" "$CONFIG_DIR/config.yaml"
    echo "    Edit $CONFIG_DIR/config.yaml to add your RTSP stream URLs before starting the service."
fi

# ---------------------------------------------------------------------------
# 5. systemd unit
# ---------------------------------------------------------------------------
echo "==> Installing systemd unit…"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
cp "$SCRIPT_DIR/deploy/viewport.service" "$UNIT_FILE"

# Patch paths and user in the unit if non-default values were given.
sed -i "s|/opt/viewport|$INSTALL_PREFIX|g"  "$UNIT_FILE"
sed -i "s|/etc/viewport|$CONFIG_DIR|g"      "$UNIT_FILE"
sed -i "s|^User=.*|User=$INSTALL_USER|"     "$UNIT_FILE"

systemctl daemon-reload
echo "    OK"

# ---------------------------------------------------------------------------
# 6. Enable / start (optional)
# ---------------------------------------------------------------------------
if $ENABLE_SERVICE; then
    echo "==> Enabling and starting $SERVICE_NAME…"
    systemctl enable "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"
    sleep 2
    systemctl is-active --quiet "$SERVICE_NAME" \
        && echo "    Service is running." \
        || echo "    Warning: service did not start — check: journalctl -u $SERVICE_NAME"
else
    echo "==> Skipping service enable (--no-enable was set)"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "================================================================"
echo "  Installation complete!"
echo ""
echo "  Next steps:"
echo "  1. Edit $CONFIG_DIR/config.yaml to configure your RTSP streams"
if ! $ENABLE_SERVICE; then
echo "  2. Enable and start the service:"
echo "       sudo systemctl enable --now $SERVICE_NAME"
fi
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status  $SERVICE_NAME"
echo "    sudo systemctl restart $SERVICE_NAME"
echo "    journalctl -u $SERVICE_NAME -f"
echo "    modetest -c               # list DRM connector IDs"
echo "================================================================"
