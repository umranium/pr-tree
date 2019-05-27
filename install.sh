#!/usr/bin/env bash
set -eu

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

main() {
  env="${DIR}/env"
  rm -rf "${env}"
  virtualenv -p python3 "${env}"
  VIRTUAL_ENV_DISABLE_PROMPT=true
  source "${env}/bin/activate"
  pip install -r "${DIR}/requirements.txt"
}

main "$@"

