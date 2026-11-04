#!/bin/sh
# Link ghostsnap into ~/.local/bin (and, with --skill, the Claude Code skill
# into ~/.claude/skills). The links point to this clone, so `git pull` updates.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
bin_dir=${GHOSTSNAP_BIN_DIR:-$HOME/.local/bin}
mkdir -p "$bin_dir"
chmod +x "$here/ghostsnap.py"
ln -sfn "$here/ghostsnap.py" "$bin_dir/ghostsnap"
echo "Linked $bin_dir/ghostsnap -> $here/ghostsnap.py"
case ":$PATH:" in *":$bin_dir:"*) ;; *) echo "Add $bin_dir to your PATH." ;; esac
if [ "${1:-}" = "--skill" ]; then
  skills=${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills
  mkdir -p "$skills"
  ln -sfn "$here/skills/ghostsnap" "$skills/ghostsnap"
  echo "Linked $skills/ghostsnap -> $here/skills/ghostsnap"
fi
