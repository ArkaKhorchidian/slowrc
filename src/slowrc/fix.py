"""Apply fixes to startup files: back up, edit, verify the shell still behaves the same, revert if not."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .measure import Shell, StartupError, _argv, _env, _run
from .rules import Finding

MARK_BEGIN = "# >>> slowrc >>>"
MARK_END = "# <<< slowrc <<<"

HELPER = {
    "zsh": f"""{MARK_BEGIN}  caches init scripts; https://github.com/ArkaKhorchidian/slowrc
_slowrc_cache() {{  # sets REPLY to a cached copy of "$@"'s output, refreshed when the command is updated
  local bin=${{commands[$1]:-$1}}
  REPLY=${{XDG_CACHE_HOME:-$HOME/.cache}}/slowrc/${{${{(j:_:)@}}//[^A-Za-z0-9._-]/_}}.sh
  [[ -x $bin ]] || return 1
  if [[ ! -s $REPLY || $bin -nt $REPLY ]]; then
    mkdir -p ${{REPLY:h}} && "$@" >| $REPLY 2>/dev/null || {{ rm -f $REPLY; return 1; }}
  fi
}}
_slowrc_compinit() {{  # compinit's full check at most once a day; otherwise it reuses its cache
  setopt localoptions bareglobqual
  local dump=${{ZDOTDIR:-$HOME}}/.zcompdump
  local -a stale=( $dump(N.mh+24) )
  if [[ ! -s $dump ]] || (( $#stale )); then compinit "$@" && touch $dump; else compinit -C "$@"; fi
}}
{MARK_END}""",
    "bash": f"""{MARK_BEGIN}  caches init scripts; https://github.com/ArkaKhorchidian/slowrc
_slowrc_cache() {{  # sets REPLY to a cached copy of "$@"'s output, refreshed when the command is updated
  local bin key
  if [[ $1 == */* ]]; then bin=$1; else bin=$(type -P "$1") || return 1; fi
  [[ -x $bin ]] || return 1
  key="$*"; key=${{key// /_}}; key=${{key//[^A-Za-z0-9._-]/_}}
  REPLY=${{XDG_CACHE_HOME:-$HOME/.cache}}/slowrc/$key.sh
  if [[ ! -s $REPLY || $bin -nt $REPLY ]]; then
    mkdir -p "${{REPLY%/*}}" && "$@" >| "$REPLY" 2>/dev/null || {{ rm -f "$REPLY"; return 1; }}
  fi
}}
{MARK_END}""",
}

PROBE = {
    "zsh": 'print -r -- "@@PATH@@${(j.:.)path}"; print -r -- "@@NODE@@$(whence -p node)"',
    "bash": 'echo "@@PATH@@$PATH"; echo "@@NODE@@$(type -P node)"',
}


@dataclass
class Snapshot:
    ok: bool
    path: list[str]
    node: str
    errors: set[str]


def snapshot(sh: Shell) -> Snapshot:
    """What a new shell looks like: does it start cleanly, what's on PATH, where is node."""
    proc = _run(_argv(sh, PROBE[sh.name]), _env())
    out = proc.stdout.decode(errors="replace")
    path = node = ""
    for line in out.splitlines():
        if line.startswith("@@PATH@@"):
            path = line[8:]
        elif line.startswith("@@NODE@@"):
            node = line[8:]
    errors = {l.strip() for l in proc.stderr.decode(errors="replace").splitlines() if l.strip()}
    return Snapshot(proc.returncode == 0, [p for p in path.split(":") if p], node, errors)


def same_behaviour(before: Snapshot, after: Snapshot) -> list[str]:
    problems = []
    if before.ok and not after.ok:
        problems.append("the shell now exits with an error")
    missing = [p for p in before.path if p not in after.path]
    if missing:
        problems.append("PATH lost " + ", ".join(missing[:3]))
    if before.node and before.node != after.node:
        problems.append(f"`node` changed from {before.node} to {after.node or 'nothing'}")
    new_errors = after.errors - before.errors
    if new_errors:
        problems.append("new errors: " + "; ".join(sorted(new_errors)[:2]))
    return problems


def _refine(f: Finding, sh: Shell, before: Snapshot) -> None:
    """Fill in details that depend on the live environment."""
    if f.id == "nvm":
        ind = f.replacement[0][: len(f.replacement[0]) - len(f.replacement[0].lstrip())]
        if before.node and "/versions/node/" in before.node:
            bindir = str(Path(before.node).parent).replace(os.environ.get("HOME", "\0"), "$HOME", 1)
            f.replacement.append(f'{ind}[ -d "{bindir}" ] && export PATH="{bindir}:$PATH"  # slowrc: your default node')
        else:  # no default version resolved: load nvm on first use of node tools too
            f.replacement.append(f'{ind}for _c in node npm npx yarn pnpm corepack; do '
                                 f'eval "$_c() {{ nvm --version >/dev/null; unset -f $_c; command $_c \\"\\$@\\"; }}"; done')
    if f.id == "eval-cache":
        line = f.replacement[0]
        if " starship init " in line and "--print-full-init" not in line:  # its init only sources itself again
            f.replacement[0] = line.replace(" && source", " --print-full-init && source", 1)
        for tool in ("pyenv init", "rbenv init"):
            if f" {tool} " in line and "--no-rehash" not in line:
                f.replacement[0] = f.replacement[0].replace(f" {tool} -", f" {tool} - --no-rehash", 1)


def manifest_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path(os.environ.get("HOME", "~")).expanduser() / ".cache")
    return base / "slowrc" / "last-fix.json"


def apply(findings: list[Finding], sh: Shell) -> tuple[list[Finding], list[str], dict[str, str]]:
    """Apply auto fixes. Returns (applied, problems, backups). On any problem everything is reverted."""
    fixes = [f for f in findings if f.auto and f.file]
    if not fixes:
        return [], [], {}
    before = snapshot(sh)
    for f in fixes:
        _refine(f, sh, before)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backups: dict[str, str] = {}
    by_file: dict[Path, list[Finding]] = {}
    for f in fixes:
        by_file.setdefault(f.file, []).append(f)
    applied: list[Finding] = []
    for path, fs in by_file.items():
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="surrogateescape")
        nl = "\r\n" if "\r\n" in text else "\n"
        lines = text.split(nl)
        trailing = lines[-1] == ""
        ok = []
        for f in sorted(fs, key=lambda f: -f.lineno):
            if lines[f.lineno - 1] != f.original:  # file changed since we measured it
                continue
            lines[f.lineno - 1:f.lineno] = f.replacement
            ok.append(f)
        if not ok:
            continue
        if any(f.needs_helper for f in ok) and MARK_BEGIN not in text:
            lines[0:0] = HELPER[sh.name].split("\n") + [""]
        backup = path.with_name(f"{path.name}.slowrc-{stamp}.bak")
        shutil.copy2(path, backup)
        backups[str(path)] = str(backup)
        new = nl.join(lines)
        if not trailing and new.endswith(nl):
            new = new[: -len(nl)]
        tmp = path.with_name(f".{path.name}.slowrc-tmp")
        tmp.write_bytes(new.encode("utf-8", errors="surrogateescape"))
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
        applied += ok

    try:
        after = snapshot(sh)
        problems = same_behaviour(before, after)
    except StartupError as e:
        problems = [str(e)]
    if problems:
        restore(backups)
        return [], problems, {}
    manifest = manifest_path()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"time": stamp, "backups": backups}, indent=2))
    return applied, [], backups


def restore(backups: dict[str, str]) -> None:
    for original, backup in backups.items():
        shutil.copy2(backup, original)


def undo() -> dict[str, str] | None:
    m = manifest_path()
    if not m.exists():
        return None
    data = json.loads(m.read_text())
    backups = {k: v for k, v in data.get("backups", {}).items() if Path(v).exists()}
    restore(backups)
    m.unlink()
    return backups


def clear_cache() -> None:
    d = manifest_path().parent
    for p in d.glob("*.sh"):
        p.unlink()
