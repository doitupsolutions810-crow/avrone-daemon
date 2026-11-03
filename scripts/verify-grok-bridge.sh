#!/bin/sh
# Bounded, read-only preflight for the two service endpoints.
set -eu

AVRONE_URL="${AVRONE_URL:-http://127.0.0.1:8710}"
GROK_BOT_URL="${GROK_BOT_URL:-http://127.0.0.1:3000}"
CURL_TIMEOUT="${BRIDGE_CURL_TIMEOUT_SECONDS:-5}"

probe() {
  name="$1"
  url="$2"
  body="$(mktemp)"
  code="000"
  if code="$(curl --silent --show-error --connect-timeout "$CURL_TIMEOUT" \
      --max-time "$CURL_TIMEOUT" --output "$body" --write-out '%{http_code}' \
      "$url" 2>/dev/null)"; then
    case "$code" in
      2*) printf 'PASS %-10s %s (HTTP %s)\n' "$name" "$url" "$code"; rm -f "$body"; return 0 ;;
      *) printf 'FAIL %-10s %s (HTTP %s)\n' "$name" "$url" "$code"; sed -n '1,8p' "$body"; rm -f "$body"; return 1 ;;
    esac
  else
    printf 'FAIL %-10s %s (connection/timeout)\n' "$name" "$url"
    rm -f "$body"
    return 1
  fi
}

failed=0
probe AVRONE "$AVRONE_URL/api/status" || failed=1
probe GROK_BOT "$GROK_BOT_URL/api/status" || failed=1

if [ "$failed" -ne 0 ]; then
  echo "BRIDGE PREFLIGHT FAILED: start both services and verify container-to-host routing."
  exit 1
fi

echo "BRIDGE PREFLIGHT PASSED: both APIs answer. This checks reachability only;"
echo "run a bounded request in each direction to verify tool-level delegation."
