#!/data/data/com.termux/files/usr/bin/bash
# One-command installer for the Echo Show CGM dashboard. Run it in Termux:
#
#   curl -fsSLO https://raw.githubusercontent.com/Velcy-a/echo-show-cgm-dashboard/main/install.sh && bash install.sh
#
# Safe to run again later: it updates the code, reinstalls only what's
# missing, and keeps your config.env.
set -euo pipefail

REPO=https://github.com/Velcy-a/echo-show-cgm-dashboard.git
DIR=$HOME/cgm-dashboard
BOOT=$HOME/.termux/boot/cgm-boot.sh
PYDANTIC_CORE_INSTALLER=https://raw.githubusercontent.com/Eutalix/android-pydantic-core/main/install_pydantic_core.sh

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "Updating Termux packages"
pkg upgrade -y -o Dpkg::Options::=--force-confnew
pkg install -y python python-pip git

if ! python -c 'import pydantic_core' 2>/dev/null; then
  # Compiling pydantic-core on the Echo runs out of memory, so use a prebuilt one.
  say "Installing prebuilt pydantic-core"
  curl -fsSL "$PYDANTIC_CORE_INSTALLER" -o "$TMPDIR/install_pydantic_core.sh"
  bash "$TMPDIR/install_pydantic_core.sh"
fi

say "Installing Flask and pylibrelinkup"
# --only-binary: never compile pydantic-core; pip picks a pydantic release
# that matches the prebuilt one instead.
pip install --only-binary=pydantic-core flask pylibrelinkup

if [ -d "$DIR/.git" ]; then
  say "Updating the dashboard"
  git -C "$DIR" pull --ff-only
else
  say "Downloading the dashboard"
  OLD=
  if [ -e "$DIR" ]; then            # a copy installed by hand: keep it aside
    OLD=$DIR.old-$(date +%Y%m%d%H%M%S)
    mv "$DIR" "$OLD"
  fi
  git clone "$REPO" "$DIR"
  if [ -n "$OLD" ] && [ -f "$OLD/config.env" ]; then
    cp "$OLD/config.env" "$DIR/config.env"
  fi
fi

if [ ! -f "$DIR/config.env" ]; then
  say "LibreLinkUp login (the account that accepted the LibreLink invite)"
  # /dev/tty so the questions still work if this script is piped into bash.
  read -rp "Email: " email < /dev/tty
  read -rsp "Password: " password < /dev/tty; echo
  read -rp "Region [US]: " region < /dev/tty
  # %q escapes quotes and symbols so any password works.
  {
    printf 'LIBRE_EMAIL=%q\n' "$email"
    printf 'LIBRE_PASSWORD=%q\n' "$password"
    printf 'LIBRE_REGION=%q\n' "${region:-US}"
  } > "$DIR/config.env"
  chmod 600 "$DIR/config.env"
fi

say "Setting up autostart"
mkdir -p "$(dirname "$BOOT")"
rm -f "$HOME/.termux/boot/cgm-dashboard"   # older README name; avoid two servers
cat > "$BOOT" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot runs this after every restart.
exec >> "$DIR/server.log" 2>&1
echo "\$(date): starting from Termux:Boot"
termux-wake-lock
cd "$DIR" && exec ./start.sh
EOF
chmod +x "$BOOT"
# Android holds back Termux:Boot's startup job unless the app is exempt from
# battery optimization and you've tapped its icon yourself at least once.
echo "Make sure the Termux:Boot app is installed, that you've tapped its icon once,"
echo "and that Termux and Termux:Boot are both set to \"Don't optimize\" in"
echo "Settings > Apps & notifications > Special app access > Battery optimization."

say "Done"
cat <<EOF
In Fully Kiosk: Start URL http://localhost:5000, and turn on Autoplay Audio,
Keep Screen On, Launch on Boot, and Auto Reload after Page Error (10 s).
In Android settings, allow Fully to "Display over other apps", or it can't
open itself at startup.

Try it with fake data:  cd ~/cgm-dashboard && ./start.sh --mock
Run it for real:        cd ~/cgm-dashboard && ./start.sh
More settings (alarm thresholds etc.) are in config.env.example.
EOF
