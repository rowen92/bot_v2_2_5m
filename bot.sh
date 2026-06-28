#!/usr/bin/env bash
# bot.sh — manage bots belonging to this strategy skeleton
# Lives inside the skeleton dir (e.g. bot_v4/), manages sibling dirs prefixed with strategy name
# Usage: ./bot.sh <command> [name|all]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOTS_ROOT="$(dirname "$SCRIPT_DIR")"                          # /www/
STRATEGY="$(basename "$SCRIPT_DIR" | sed 's/_[^_]*$//')"      # v1_wldusdt → v1
SKELETON="$SCRIPT_DIR"                                        # this repo is the skeleton

usage() { cat <<EOF
Strategy: $STRATEGY  (skeleton: $SCRIPT_DIR)
Usage: ./bot.sh <command> [name|all]

  new <name> <symbol>   Create new bot: ${STRATEGY}_<name>/  e.g. new wld WLDUSDT
  list                  List all ${STRATEGY}_* bot directories
  pull    [name|all]    Git pull latest code into bot dir(s)
  up      [name|all]    Build and start
  down    [name|all]    Stop and remove containers
  stop    [name|all]    Stop without removing
  restart [name|all]    Stop → rebuild → start
  rebuild [name|all]    Force full image rebuild
  logs    <name>        Follow live logs
  status  [name|all]    Show container status
  shell   <name>        Open bash inside container

Examples:
  ./bot.sh new wld WLDUSDT    # creates /www/${STRATEGY}_wld/
  ./bot.sh new btc BTCUSDT    # creates /www/${STRATEGY}_btc/
  ./bot.sh pull all            # git pull in every bot dir
  ./bot.sh up all
  ./bot.sh logs wld
EOF
}

list_bots() {
    while IFS= read -r d; do basename "$d"; done < \
        <(find "$BOTS_ROOT" -maxdepth 1 -name "${STRATEGY}_*" -type d | sort)
}

env_field() {
    grep "^${2}=" "${BOTS_ROOT}/${1}/.env" 2>/dev/null | cut -d= -f2 | tr -d ' '
}

header() { echo -e "\n\033[36m━━━ $* ━━━\033[0m"; }

resolve_dir() {
    local name=$1
    # Accept either short name (wld) or full dir name (v4_wld)
    if [[ "$name" == "${STRATEGY}_"* ]]; then echo "$name"
    else echo "${STRATEGY}_${name}"; fi
}

run() {
    local cmd=$1 dir
    dir=$(resolve_dir "$2")
    header "$dir  make $cmd"
    make -C "${BOTS_ROOT}/$dir" "$cmd"
}

run_all() {
    local cmd=$1
    while IFS= read -r dir; do run "$cmd" "$dir"; done < <(list_bots)
}

dispatch() {
    local cmd=$1 target=${2:-all}
    [[ "$target" == "all" ]] && run_all "$cmd" || run "$cmd" "$target"
}

CMD="${1:-help}"; shift || true

case "$CMD" in
    new)
        NAME=${1:?'Usage: ./bot.sh new <name> <symbol>  e.g. new wld WLDUSDT'}
        SYMBOL=${2:?'Usage: ./bot.sh new <name> <symbol>  e.g. new wld WLDUSDT'}
        DIR="${STRATEGY}_${NAME}"
        DEST="${BOTS_ROOT}/$DIR"
        [[ -d "$DEST" ]] && { echo "Error: $DEST already exists"; exit 1; }
        # Clone git repo (use remote origin if available, else local clone)
        REMOTE_URL=$(git -C "$SKELETON" remote get-url origin 2>/dev/null || true)
        if [[ -n "$REMOTE_URL" ]]; then
            git clone "$REMOTE_URL" "$DEST"
        else
            git clone "$SKELETON" "$DEST"
        fi
        # Copy only files untracked by git in skeleton (local-only files, e.g. .env.default)
        while IFS= read -r f; do
            [[ "$f" == ".env" || "$f" == "bot.sh" ]] && continue
            mkdir -p "$DEST/$(dirname "$f")"
            cp "$SKELETON/$f" "$DEST/$f"
        done < <(git -C "$SKELETON" ls-files --others --exclude-standard)
        # Bootstrap .env from template
        [[ -f "${SKELETON}/.env.default" ]] || { echo "Error: ${SKELETON}/.env.default not found"; exit 1; }
        cp "${SKELETON}/.env.default" "${DEST}/.env"
        sed -i \
            -e "s|^COMPOSE_PROJECT_NAME=.*|COMPOSE_PROJECT_NAME=$DIR|" \
            -e "s|^SERVICE=.*|SERVICE=$DIR|" \
            -e "s|^CONTAINER=.*|CONTAINER=$DIR|" \
            -e "s|^SYMBOL=.*|SYMBOL=${SYMBOL^^}|" \
            "${DEST}/.env"
        echo "✓ Created $DEST  (SYMBOL=${SYMBOL^^})"
        echo "  Edit ${DEST}/.env then: ./bot.sh up $NAME"
        ;;
    list)
        bots=$(list_bots)
        [[ -z "$bots" ]] && { echo "No ${STRATEGY}_* bot directories found."; exit 0; }
        printf "\033[36m%-20s %-12s %-8s %s\033[0m\n" "DIR" "SYMBOL" "MODE" "CONTAINER"
        while IFS= read -r dir; do
            printf "%-20s %-12s %-8s %s\n" \
                "$dir" \
                "$(env_field "$dir" SYMBOL)" \
                "$(env_field "$dir" TRADING_MODE)" \
                "$(env_field "$dir" CONTAINER)"
        done < <(list_bots)
        ;;
    pull)
        target="${1:-all}"
        pull_one() {
            local dir; dir=$(resolve_dir "$1")
            header "$dir  git pull"
            git -C "${BOTS_ROOT}/$dir" pull
        }
        if [[ "$target" == "all" ]]; then
            while IFS= read -r dir; do pull_one "$dir"; done < <(list_bots)
        else
            pull_one "$target"
        fi
        ;;
    up|down|stop|restart|rebuild|status) dispatch "$CMD" "${1:-all}" ;;
    logs|shell) run "$CMD" "${1:?"Usage: ./bot.sh $CMD <name>"}" ;;
    help|--help|-h) usage ;;
    *) echo "Unknown: $CMD"; usage; exit 1 ;;
esac
