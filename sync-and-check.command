#!/bin/zsh
set -u

PROJECT_DIR=${0:A:h}
if [[ ! -x "$PROJECT_DIR/.venv/bin/flomo-vault" ]]; then
  "$PROJECT_DIR/install.command" || {
    print -u2 "安装失败。"
    read -k 1 "?按任意键关闭…"
    exit 1
  }
fi

"$PROJECT_DIR/bin/flomo-vault" daily
RESULT=$?
if [[ $RESULT -ne 0 ]]; then
  read -k 1 "?同步或校对需要留意，按任意键关闭…"
fi
exit $RESULT
