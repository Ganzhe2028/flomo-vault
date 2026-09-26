#!/bin/zsh
set -eu

PROJECT_DIR=${0:A:h}
VENV_DIR="$PROJECT_DIR/.venv"

if ! command -v python3 >/dev/null 2>&1; then
  print -u2 "没有找到 Python 3。请先安装 Python 3.11 或更高版本。"
  exit 1
fi

PYTHON_OK=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 11)))')
if [[ "$PYTHON_OK" != "1" ]]; then
  print -u2 "需要 Python 3.11 或更高版本。"
  exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip --isolated install --upgrade pip setuptools
"$VENV_DIR/bin/python" -m pip --isolated install cramjam==2.12.1 zstd==1.5.5.1
"$VENV_DIR/bin/python" -m pip --isolated install --no-deps dfindexeddb==20260210
"$VENV_DIR/bin/python" -m pip --isolated install --no-deps -e "$PROJECT_DIR"

mkdir -p "$HOME/.local/bin" "$HOME/.agents/skills"
COMMAND_LINK="$HOME/.local/bin/flomo-vault"
SKILL_LINK="$HOME/.agents/skills/flomo-local-vault"
if [[ ! -e "$COMMAND_LINK" && ! -L "$COMMAND_LINK" ]]; then
  ln -s "$PROJECT_DIR/bin/flomo-vault" "$COMMAND_LINK"
fi
if [[ ! -e "$SKILL_LINK" && ! -L "$SKILL_LINK" ]]; then
  ln -s "$PROJECT_DIR/agent-skill" "$SKILL_LINK"
fi

print "安装完成。以后双击 sync.command，或运行："
print "  $PROJECT_DIR/bin/flomo-vault sync"
print "完整同步并校对可双击 sync-and-check.command，或运行："
print "  $PROJECT_DIR/bin/flomo-vault daily"
print "首次启用 Notion 只读校对："
print "  $PROJECT_DIR/bin/flomo-vault notion setup"
print "启用独立 Notion 纯文字归档："
print "  $PROJECT_DIR/bin/flomo-vault notion-text setup"
print "Agent Skill 已注册；新任务里可以直接说“同步 flomo”。"
