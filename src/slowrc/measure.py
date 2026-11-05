"""Time shell startup, then trace one startup to see which lines the time went to."""
from __future__ import annotations

import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

TIMEOUT = 20.0
RS, US, GS = "\x1e", "\x1f", "\x1d"


class StartupError(Exception):
    pass


@dataclass
class Shell:
    name: str  # "zsh" | "bash"
    path: str
    login: bool
    version: str = ""

    @property
    def home(self) -> Path:
        return Path(os.environ.get("HOME") or Path.home())

    @property
    def zdotdir(self) -> Path:
        return Path(os.environ.get("ZDOTDIR") or self.home)

    def rc_files(self) -> list[Path]:
        if self.name == "zsh":
            names = [".zshenv", ".zprofile", ".zshrc", ".zlogin"] if self.login else [".zshenv", ".zshrc"]
            return [self.zdotdir / n for n in names if (self.zdotdir / n).is_file()]
        if self.login:
            for n in (".bash_profile", ".bash_login", ".profile"):
                if (self.home / n).is_file():
                    return [self.home / n] + ([self.home / ".bashrc"] if (self.home / ".bashrc").is_file() else [])
            return []
        return [self.home / ".bashrc"] if (self.home / ".bashrc").is_file() else []


def detect_shell(name: str | None = None, login: bool | None = None) -> Shell:
    name = name or Path(os.environ.get("SHELL", "")).name or "bash"
    if name not in ("zsh", "bash"):
        raise StartupError(f"slowrc profiles zsh and bash; your shell is {name}. Try: slowrc --shell zsh")
    path = shutil.which(name)
    if not path:
        raise StartupError(f"{name} isn't installed")
    if login is None:
        login = sys.platform == "darwin"  # macOS terminals start login shells; Linux ones usually don't
    v = subprocess.run([path, "-c", "echo ${ZSH_VERSION:-$BASH_VERSION}"], capture_output=True, text=True).stdout.strip()
    return Shell(name, path, login, v)


def _env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    env["SLOWRC"] = "1"  # lets an rc file skip things like tmux auto-attach while being measured
    env.update(extra or {})
    return env


def _argv(sh: Shell, command: str = "exit") -> list[str]:
    return [sh.path] + (["-l"] if sh.login else []) + ["-i", "-c", command]


def _run(argv, env, cwd=None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise StartupError(
            f"Your shell didn't finish starting within {TIMEOUT:.0f}s. Something in your startup files is "
            "waiting for input (tmux auto-attach? a prompt?). It can check for $SLOWRC to skip itself.") from None


def time_startup(sh: Shell, runs: int = 10, budget: float = 8.0) -> list[float]:
    """Wall-clock startup times in seconds. Stops early once `budget` seconds are spent (min 3 runs)."""
    times: list[float] = []
    env = _env()
    t_start = time.perf_counter()
    _run(_argv(sh), env)  # warm-up: first run pays for cold disk caches
    for i in range(runs):
        t0 = time.perf_counter()
        _run(_argv(sh), env)
        times.append(time.perf_counter() - t0)
        if i >= 2 and time.perf_counter() - t_start > budget:
            break
    return times


PROMPT_ZSH = r"""
zmodload zsh/datetime
local -a samples
for i in 1 2 3 4 5; do
  local t=$EPOCHREALTIME
  for f in $precmd_functions; do $f; done
  : ${(%%)PROMPT}${(%%)RPROMPT}
  samples+=$(( EPOCHREALTIME - t ))
done
print -r -- ${${(on)samples}[3]}
"""
PROMPT_BASH = r"""
s=()
for i in 1 2 3 4 5; do
  t=$EPOCHREALTIME
  eval "$PROMPT_COMMAND" >/dev/null 2>&1
  : "${PS1@P}"
  s+=($(echo "$EPOCHREALTIME - $t" | awk '{printf "%.6f", $1 - $3}'))
done
printf '%s\n' "${s[@]}" | sort -n | sed -n 3p
"""


def prompt_latency(sh: Shell, cwd: str | None = None) -> float | None:
    """Median seconds to run precmd hooks and render the prompt (what you wait for after every command)."""
    script = PROMPT_ZSH if sh.name == "zsh" else PROMPT_BASH
    try:
        out = _run(_argv(sh, script), _env(), cwd=cwd).stdout.decode(errors="replace").strip().splitlines()
        return float(out[-1]) if out else None
    except (StartupError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------
ZSH_SHIM = """\
zmodload zsh/datetime
ZDOTDIR=$_SLOWRC_ZDOTDIR
[[ -z $_SLOWRC_HAD_ZDOTDIR ]] && unset ZDOTDIR
unset _SLOWRC_ZDOTDIR _SLOWRC_HAD_ZDOTDIR
setopt prompt_subst
PS4=$'\\x1e${EPOCHREALTIME}\\x1f${(pj:\\x1d:)funcfiletrace}\\x1f%x\\x1f%I\\x1f'
setopt xtrace
[[ -r ${ZDOTDIR:-$HOME}/.zshenv ]] && source ${ZDOTDIR:-$HOME}/.zshenv
"""

BASH_SHIM = """\
PS4=$'\\x1e${EPOCHREALTIME}\\x1f${BASH_SOURCE[*]}\\x1f${BASH_LINENO[*]}\\x1f${LINENO}\\x1f'
set -x
{body}
set +x
"""


@dataclass
class Line:
    file: str
    line: int
    seconds: float = 0.0
    traced: int = 0  # how many trace records were attributed here (for overhead correction)
    text: str = ""
    children: dict = field(default_factory=lambda: defaultdict(float))  # nested file -> seconds


@dataclass
class Profile:
    lines: list[Line]
    traced_total: float
    records: int


def _bash_body(sh: Shell) -> str:
    if not sh.login:
        return '[ -r ~/.bashrc ] && . ~/.bashrc'
    return ('[ -r /etc/profile ] && . /etc/profile\n'
            'if [ -r ~/.bash_profile ]; then . ~/.bash_profile\n'
            'elif [ -r ~/.bash_login ]; then . ~/.bash_login\n'
            'elif [ -r ~/.profile ]; then . ~/.profile; fi')


def can_trace(sh: Shell) -> bool:
    """Line-level tracing needs zsh, or bash 5+ (for $EPOCHREALTIME). macOS ships bash 3.2."""
    if sh.name == "zsh":
        return True
    try:
        return int(sh.version.split(".")[0]) >= 5
    except ValueError:
        return False


def trace(sh: Shell) -> Profile:
    """One traced startup. Each line's time is attributed to the outermost line in your own files."""
    if not can_trace(sh):
        return Profile([], 0.0, 0)
    with tempfile.TemporaryDirectory(prefix="slowrc-") as tmp:
        env = _env()
        if sh.name == "zsh":
            (Path(tmp) / ".zshenv").write_text(ZSH_SHIM)
            env["_SLOWRC_ZDOTDIR"] = str(sh.zdotdir)
            env["_SLOWRC_HAD_ZDOTDIR"] = "1" if os.environ.get("ZDOTDIR") else ""
            env["ZDOTDIR"] = tmp
            argv = _argv(sh)
        else:
            shim = Path(tmp) / "shim.bash"
            shim.write_text(BASH_SHIM.replace("{body}", _bash_body(sh)))
            argv = [sh.path, "--noprofile", "--rcfile", str(shim), "-i", "-c", "exit"]
            if sh.login:  # --rcfile only applies to non-login shells, so the shim replays the login order
                argv = [sh.path, "--noprofile", "--norc", "-i", "-c", f". {shim}; exit"]
        proc = _run(argv, env)
    raw = proc.stderr.decode("utf-8", errors="replace")
    return parse(raw, sh.name)


def parse(raw: str, shell: str) -> Profile:
    recs = []  # (timestamp, outer_file, outer_line, inner_file, text)
    for chunk in raw.split(RS)[1:]:
        parts = chunk.split(US, 4)
        if len(parts) < 5:
            continue
        try:
            ts = float(parts[0])
        except ValueError:
            continue
        text = parts[4].split("\n", 1)[0].strip()
        if shell == "zsh":
            frames = [f for f in parts[1].split(GS) if f and ":" in f and not f.startswith("zsh:")]
            here_file, here_line = parts[2], parts[3]
            if frames:
                f, _, l = frames[-1].rpartition(":")
                outer = (f, l)
            else:
                outer = (here_file, here_line)
            inner = here_file
        else:
            sources = parts[1].split(" ") if parts[1] else []
            linenos = parts[2].split(" ") if parts[2] else []
            if not sources:
                continue
            # the shim itself is the outermost bash frame; the frame under it is the user's file
            if "slowrc-" in sources[-1]:
                sources, linenos = sources[:-1], linenos[:-1]
            if not sources:
                continue
            outer_file = sources[-1]
            outer_line = linenos[-2] if len(sources) >= 2 and len(linenos) >= 2 else parts[3]
            outer = (outer_file, outer_line)
            inner = sources[0]
        try:
            recs.append((ts, outer[0], int(outer[1]), inner, text))
        except ValueError:
            continue
    by: dict[tuple, Line] = {}
    for (ts, f, l, inner, text), nxt in zip(recs, recs[1:]):
        if "/slowrc-" in f:  # slowrc's own shim
            continue
        dt = max(0.0, nxt[0] - ts)
        key = (f, l)
        ln = by.get(key)
        if ln is None:
            ln = by[key] = Line(f, l)
        ln.seconds += dt
        ln.traced += 1
        if inner != f:
            ln.children[inner] += dt
        if not ln.text and (inner == f):
            ln.text = text
    total = recs[-1][0] - recs[0][0] if len(recs) > 1 else 0.0
    for ln in by.values():  # show the line as written in the file, not the expanded command that ran
        ln.text = _source_line(ln.file, ln.line) or ln.text
    return Profile(sorted(by.values(), key=lambda x: -x.seconds), total, len(recs))


def _source_line(path: str, n: int) -> str:
    try:
        return Path(path).read_text(errors="replace").splitlines()[n - 1].strip()
    except (OSError, IndexError):
        return ""


def calibrate(prof: Profile, real_seconds: float) -> None:
    """Remove tracing overhead so attributed times add up to the real (untraced) startup time."""
    if prof.records < 2 or prof.traced_total <= 0:
        return
    overhead_per_record = max(0.0, (prof.traced_total - real_seconds) / prof.records)
    for ln in prof.lines:
        corrected = max(0.0, ln.seconds - ln.traced * overhead_per_record)
        scale = corrected / ln.seconds if ln.seconds else 0.0
        ln.seconds = corrected
        for k in list(ln.children):
            ln.children[k] *= scale
    attributed = sum(ln.seconds for ln in prof.lines)
    if attributed > real_seconds > 0:  # never report more time than the shell actually took
        k = real_seconds / attributed
        for ln in prof.lines:
            ln.seconds *= k
            for c in list(ln.children):
                ln.children[c] *= k
    prof.lines.sort(key=lambda x: -x.seconds)


def median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0
