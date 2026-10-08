#!/usr/bin/env bash
set -euo pipefail

CLI_NAME="funflix-api"
PACKAGE_NAME="funflix-api"

usage() {
  cat >&2 <<'EOF'
用法：scripts/setup.sh <动作> [版本]

服务：start | stop | restart | run | status
安装：install-dev | install-prod [版本]
发布：publish
维护：upgrade [版本] | rollback <版本> | uninstall
EOF
}

die() {
  printf '错误：%s\n' "$*" >&2
  exit 2
}

install_prod() {
  local version="${1:-}"
  if [[ -z "${version}" ]] && uv tool list | grep -q "^${PACKAGE_NAME} "; then
    printf '%s 已安装；未指定版本，不执行升级。\n' "${PACKAGE_NAME}"
  elif [[ -n "${version}" ]]; then
    uv tool install "${PACKAGE_NAME}==${version}"
  else
    uv tool install "${PACKAGE_NAME}"
  fi
}

main() {
  local action="${1:-}"
  shift || true

  case "${action}" in
  start | stop | restart | status)
    (( $# == 0 )) || die "${action} 不接受额外参数"
    "${CLI_NAME}" server "${action}"
    ;;
  run)
    (( $# == 0 )) || die "run 不接受额外参数"
    exec "${CLI_NAME}" server run
    ;;
  install-dev)
    (( $# == 0 )) || die "install-dev 不接受额外参数"
    funbuild install
    ;;
  install-prod)
    (( $# <= 1 )) || die "install-prod 最多接受一个版本号"
    install_prod "${1:-}"
    ;;
  publish)
    (( $# == 0 )) || die "publish 不接受额外参数"
    funbuild build
    ;;
  upgrade)
    (( $# <= 1 )) || die "upgrade 最多接受一个版本号"
    "${CLI_NAME}" upgrade "$@"
    ;;
  rollback)
    (( $# == 1 )) || die "rollback 必须指定版本号"
    "${CLI_NAME}" rollback "$1"
    ;;
  uninstall)
    (( $# == 0 )) || die "uninstall 不接受额外参数"
    "${CLI_NAME}" uninstall
    ;;
  -h | --help | help)
    (( $# == 0 )) || die "help 不接受额外参数"
    usage
    ;;
  *)
    usage
    die "未知动作：${action:-<空>}"
    ;;
  esac
}

main "$@"
