#!/usr/bin/env bash
# install.sh — install the post-commit hook into a target Spring Boot repo.
#
# Usage:
#     ./hooks/install.sh /path/to/your/spring-boot/repo

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <path-to-target-repo>"
    exit 2
fi

TARGET="$1"
if [[ ! -d "$TARGET/.git" ]]; then
    echo "ERROR: $TARGET is not a git repo"
    exit 1
fi

HOOK_SRC="$(cd "$(dirname "$0")" && pwd)/post-commit"
HOOK_DST="$TARGET/.git/hooks/post-commit"

cp "$HOOK_SRC" "$HOOK_DST"
chmod +x "$HOOK_DST"

# Drop a marker so the hook knows where the agent lives if you keep it outside the repo.
AGENT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "export SPRING_TEST_AGENT_DIR=\"$AGENT_DIR\"" \
    > "$TARGET/.test-agent.env"

cat <<EOF
installed post-commit hook -> $HOOK_DST
agent dir                  -> $AGENT_DIR
next step: cd "$TARGET" && pip install -r "$AGENT_DIR/requirements.txt"
EOF
