"""A home directory with the classic slow-shell setup, built from stand-in tools that sleep like the real ones."""
from __future__ import annotations

import os
import stat
from pathlib import Path


def _exe(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def build(home: Path, shell: str = "zsh") -> dict:
    home.mkdir(parents=True, exist_ok=True)
    bin_ = home / "fakebin"
    # nvm: sourcing it is slow and it puts the default node on PATH
    node_bin = home / ".nvm/versions/node/v22.11.0/bin"
    _exe(node_bin / "node", "echo v22.11.0\n")
    (home / ".nvm/nvm.sh").write_text(
        "nvm() { echo 'nvm 0.40.1'; }\n"
        "_nvm_slow() { sleep 0.25; }\n_nvm_slow\n"
        f'export PATH="$NVM_DIR/versions/node/v22.11.0/bin:$PATH"\n')
    # pyenv: `pyenv init -` is slow and prints an init script
    _exe(bin_ / "pyenv", 'sleep 0.15\n'
         'case "$*" in *init*) echo \'export PYENV_ROOT="$HOME/.pyenv"\'; '
         'echo \'export PATH="$HOME/.pyenv/shims:$PATH"\'; echo \'pyenv() { command pyenv "$@"; }\';; esac\n')
    # brew: `brew --prefix <formula>` is slow
    (home / "homebrew/opt/fzf").mkdir(parents=True, exist_ok=True)
    _exe(bin_ / "brew", f'sleep 0.2\ncase "$2" in fzf) echo "{home}/homebrew/opt/fzf";; *) echo "{home}/homebrew";; esac\n')
    (home / "homebrew/opt/fzf/key-bindings.sh").write_text("FZF_KEYS=1\n")

    rc = home / (".zshrc" if shell == "zsh" else ".bashrc")
    lines = [
        f'export PATH="{bin_}:$PATH"',
        'export NVM_DIR="$HOME/.nvm"',
        '[ -s "$NVM_DIR/nvm.sh" ] && \\. "$NVM_DIR/nvm.sh"  # This loads nvm',
        'eval "$(pyenv init -)"',
        'source "$(brew --prefix fzf)/key-bindings.sh"',
        "alias gs='git status'",
    ]
    if shell == "zsh":
        lines.append("autoload -Uz compinit && compinit")
    rc.write_text("\n".join(lines) + "\n")
    return {"rc": rc, "node_bin": node_bin}


if __name__ == "__main__":
    import sys
    build(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "zsh")
