#!/usr/bin/env bash
set -eu

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

main() {
  env="${DIR}/env"
  rm -rf "${env}"
  python3 -m venv "${env}"
  VIRTUAL_ENV_DISABLE_PROMPT=true
  source "${env}/bin/activate"
  # python3 -m pip install PyGithub plumbum
  python3 -m pip install -r "${DIR}/requirements.txt"
}

main "$@"

