#!/data/data/com.termux/files/usr/bin/bash
# Start the CGM dashboard and restart it if it ever exits.
#   ./start.sh          real data (reads config.env)
#   ./start.sh --mock   fake data, no LibreLinkUp login
set -u
cd "$(dirname "$0")"

if [ "${1:-}" = "--mock" ]; then
  export CGM_MOCK=1
elif [ -f config.env ]; then
  # tr: a config.env saved on Windows has CRLF endings, which would put a
  # stray \r at the end of the password.
  set -a; . <(tr -d '\r' < config.env); set +a
else
  echo "config.env not found. Run: cp config.env.example config.env && nano config.env" >&2
  exit 1
fi

# Stop Android from suspending Termux while the dashboard runs.
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock

trap 'exit 0' INT TERM
while true; do
  python app.py
  status=$?
  # 1 = config error (e.g. missing password); restarting won't fix that.
  [ "$status" -eq 1 ] && exit 1
  echo "app.py exited with status $status; restarting in 10 s" >&2
  sleep 10
done
