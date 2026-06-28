#!/usr/bin/env sh

# Source this file to export OPENAI_API_KEY from the local ignored .env file:
#   . scripts/load_openai_api_key.sh

_HALCYON_ENV_FILE="${HALCYON_ENV_FILE:-.env}"

if [ ! -f "$_HALCYON_ENV_FILE" ]; then
    printf '%s\n' "Missing environment file: $_HALCYON_ENV_FILE" >&2
    unset _HALCYON_ENV_FILE
    return 1 2>/dev/null || exit 1
fi

set -a
# shellcheck disable=SC1090
. "$_HALCYON_ENV_FILE"
set +a

if [ -z "${OPENAI_API_KEY:-}" ]; then
    printf '%s\n' "OPENAI_API_KEY is not set in $_HALCYON_ENV_FILE" >&2
    unset _HALCYON_ENV_FILE
    return 1 2>/dev/null || exit 1
fi

unset _HALCYON_ENV_FILE
