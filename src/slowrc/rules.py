"""Recognise what's slow and how to fix it."""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .measure import Line, Profile, Shell

# Init commands whose output only changes when the tool itself is updated, so it is safe to cache.
# (first word, required next word or None). Anything that starts agents or makes per-session state
# (ssh-agent, keychain, fnm env, opam env) is deliberately absent.
CACHEABLE = [
    ("brew", "shellenv"), ("pyenv", "init"), ("pyenv", "virtualenv-init"), ("rbenv", "init"), ("nodenv", "init"),
    ("goenv", "init"), ("plenv", "init"), ("jenv", "init"), ("phpenv", "init"), ("zoxide", "init"),
    ("starship", "init"), ("direnv", "hook"), ("thefuck", "--alias"), ("mise", "activate"), ("rtx", "activate"),
    ("atuin", "init"), ("fzf", "--zsh"), ("fzf", "--bash"), ("oh-my-posh", "init"), ("navi", "widget"),
    ("kubectl", "completion"), ("helm", "completion"), ("gh", "completion"), ("docker", "completion"),
    ("uv", "generate-shell-completion"), ("uvx", "--generate-shell-completion"), ("rustup", "completions"),
    ("poetry", "completions"), ("pip", "completion"), ("pip3", "completion"), ("npm", "completion"),
    ("op", "completion"), ("flux", "completion"), ("kind", "completion"), ("minikube", "completion"),
    ("k9s", "completion"), ("deno", "completions"), ("bun", "completions"), ("pnpm", "completion"),
    ("glab", "completion"), ("argocd", "completion"), ("eksctl", "completion"), ("kustomize", "completion"),
    ("hugo", "completion"), ("chezmoi", "completion"), ("rclone", "completion"), ("just", "--completions"),
]
EVAL_RE = re.compile(r"""^(?P<indent>\s*)(?:eval\s+"?\$\((?P<a>[^()`;&|<>]+)\)"?|(?:source|\.)\s+<\((?P<b>[^()`;&|<>]+)\))\s*(?P<tail>(?:#.*)?)$""")
NVM_RE = re.compile(r"""^(?P<indent>\s*)(?:\[\s+-s\s+(?P<q>["']?)(?P<path1>[^"'\]]*nvm\.sh)(?P=q)\s+\]\s*&&\s*)?(?:\\?\.|source)\s+(?P<q2>["']?)(?P<path>[^"']*nvm\.sh)(?P=q2)(?P<args>(?:\s+--no-use)?)\s*(?P<tail>(?:#.*)?)$""")
COMPINIT_RE = re.compile(r"(?<![\w-])compinit(?P<flags>(?:\s+-[uiC]+)*)(?=\s*(?:$|;|&&|\|\||#))")
BREW_PREFIX_RE = re.compile(r"\$\(\s*brew\s+--prefix(?:\s+(?P<formula>[\w@./+-]+))?\s*\)")
SAFE_WORD = re.compile(r"^[A-Za-z0-9_./@:+=,~-]+$|^\$\{?HOME\}?(/[A-Za-z0-9_./@+-]*)?$")


@dataclass
class Finding:
    id: str
    title: str
    detail: str
    seconds: float  # estimated time saved
    auto: bool = False
    file: Path | None = None
    lineno: int | None = None
    original: str = ""
    replacement: list[str] = field(default_factory=list)
    needs_helper: bool = False


def home_short(p: str | Path) -> str:
    home = os.environ.get("HOME", "")
    s = str(p)
    return "~" + s[len(home):] if home and s.startswith(home + "/") else s


def _user_file(path: str, sh: Shell) -> bool:
    try:
        return Path(path).resolve().is_relative_to(sh.home.resolve()) and Path(path).is_file()
    except (OSError, ValueError):
        return False


def _read(path: str) -> list[str]:
    return Path(path).read_text(encoding="utf-8", errors="surrogateescape").splitlines()


def cacheable(words: list[str]) -> bool:
    if not words or not all(SAFE_WORD.match(w) for w in words):
        return False
    base = os.path.basename(words[0])
    nxt = words[1] if len(words) > 1 else None
    return any(base == b and (want is None or nxt == want) for b, want in CACHEABLE)


def analyze(sh: Shell, prof: Profile, threshold: float = 0.01) -> list[Finding]:
    found: list[Finding] = []
    seen: set[tuple] = set()

    for ln in prof.lines:
        if ln.seconds < threshold:
            continue
        key = (ln.file, ln.line)
        if key in seen:
            continue
        seen.add(key)
        if not _user_file(ln.file, sh):
            found += _system_findings(ln, sh)
            continue
        src = _read(ln.file)
        if not (0 < ln.line <= len(src)):
            continue
        text = src[ln.line - 1]
        f = (_eval_cache(sh, ln, text) or _nvm(sh, ln, text) or _compinit(sh, ln, text) or _brew_prefix(sh, ln, text)
             or _known_slow(sh, ln, text, src))
        if f:
            found.append(f)
    found.sort(key=lambda f: (not f.auto, -f.seconds))
    return found


def _eval_cache(sh: Shell, ln: Line, text: str) -> Finding | None:
    m = EVAL_RE.match(text)
    if not m:
        return None
    inner = (m.group("a") or m.group("b")).strip()
    try:
        words = shlex.split(inner)
    except ValueError:
        return None
    if not cacheable(words):
        return None
    call = " ".join(shlex.quote(w) if not w.startswith("$") else w for w in words)
    repl = f'{m.group("indent")}_slowrc_cache {call} && source "$REPLY"  # slowrc: cached `{inner}`'
    return Finding("eval-cache", f"Cache the output of `{inner}`",
                   f"It prints the same script every time; slowrc saves it and refreshes it whenever "
                   f"{os.path.basename(words[0])} is updated.", ln.seconds * 0.95, True, Path(ln.file), ln.line,
                   text, [repl], needs_helper=True)


def _nvm(sh: Shell, ln: Line, text: str) -> Finding | None:
    m = NVM_RE.match(text)
    if not m:
        return None
    src = "\n".join(_read(ln.file)[ln.line:])
    if re.search(r"^\s*nvm\s+use\b", src, re.M):
        return Finding("nvm", "nvm is loaded on every start", "A later `nvm use` line forces it to load anyway. "
                       "Remove it (nvm uses your default version automatically) and run slowrc again.",
                       ln.seconds, False, Path(ln.file), ln.line, text)
    path = m.group("path")
    ind = m.group("indent")
    q = '"'
    lines = [f"{ind}# slowrc: nvm itself loads the first time you run `nvm`; node, npm and global tools work right away",
             f'{ind}nvm() {{ unset -f nvm; \\. {q}{path}{q}; nvm "$@"; }}']
    return Finding("nvm", "Lazy-load nvm",
                   "Sourcing nvm.sh is one of the slowest things a shell can do. Your default Node goes straight "
                   "onto PATH instead, and nvm loads the first time you actually run it.",
                   ln.seconds * 0.97, True, Path(ln.file), ln.line, text, lines)


def _compinit(sh: Shell, ln: Line, text: str) -> Finding | None:
    if sh.name != "zsh" or text.lstrip().startswith("#"):
        return None
    # only where compinit is the command being run, not an argument (as in `autoload -Uz compinit`)
    m = next((m for m in COMPINIT_RE.finditer(text)
              if not re.split(r";|&&|\|\|", text[:m.start()])[-1].strip()), None)
    if not m or "-C" in (m.group("flags") or "") or "-d" in text:
        return None
    if ln.seconds < 0.01:
        return None
    flags = m.group("flags") or ""
    repl = text[:m.start()] + "_slowrc_compinit" + flags + text[m.end():] + "  # slowrc: full check once a day"
    return Finding("compinit", "Run compinit's full check once a day",
                   "compinit re-checks every completion file on each start. With -C it reuses its cache, and the "
                   "full check still runs when the cache is more than a day old.",
                   ln.seconds * 0.8, True, Path(ln.file), ln.line, text, [repl], needs_helper=True)


def _brew_prefix(sh: Shell, ln: Line, text: str) -> Finding | None:
    if not BREW_PREFIX_RE.search(text) or text.lstrip().startswith("#"):
        return None
    import shutil
    import subprocess

    brew = shutil.which("brew") or next((b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew",
                                                       "/home/linuxbrew/.linuxbrew/bin/brew") if os.access(b, os.X_OK)), None)
    if not brew:
        return None
    new = text
    for m in list(BREW_PREFIX_RE.finditer(text)):
        args = [brew, "--prefix"] + ([m.group("formula")] if m.group("formula") else [])
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None
        if not out or not Path(out).exists():
            return None
        new = new.replace(m.group(0), out)
    return Finding("brew-prefix", "Inline `brew --prefix`",
                   "Each `$(brew --prefix …)` starts Homebrew's Ruby just to print a path that never changes.",
                   ln.seconds * 0.9, True, Path(ln.file), ln.line, text,
                   [new + "  # slowrc: was " + ", ".join(m.group(0) for m in BREW_PREFIX_RE.finditer(text))])


KNOWN = [
    (re.compile(r"conda[\"']?\s+[\"']?shell\.|conda initialize|conda\.sh"), "conda",
     "conda activates its base environment on every start",
     "Run `conda config --set auto_activate_base false`; activate environments when you need them."),
    (re.compile(r"oh-my-zsh\.sh"), "ohmyzsh", "oh-my-zsh",
     "Remove plugins you don't use, and set `DISABLE_AUTO_UPDATE=true` and `ZSH_DISABLE_COMPFIX=true` "
     "before it's sourced."),
    (re.compile(r"sdkman-init\.sh"), "sdkman", "SDKMAN! initialises on every start",
     "Load it on demand: wrap the source line in a `sdk() { unset -f sdk; source …/sdkman-init.sh; sdk \"$@\"; }` function."),
    (re.compile(r"\brvm\b|/\.rvm/"), "rvm", "RVM loads on every start",
     "rbenv or chruby are much lighter; or load RVM on demand."),
    (re.compile(r"\bnvm\s+use\b"), "nvm-use", "`nvm use` forces nvm to load", "nvm uses your default automatically."),
    (re.compile(r"gcloud.*(path|completion)\.(zsh|bash)\.inc"), "gcloud", "Google Cloud SDK completion",
     "Cheap to keep, but if you rarely use gcloud, source it lazily."),
]


def _known_slow(sh: Shell, ln: Line, text: str, src: list[str]) -> Finding | None:
    for rx, fid, title, detail in KNOWN:
        if rx.search(text):
            return Finding(fid, title, detail, ln.seconds * 0.8, False, Path(ln.file), ln.line, text)
    if ln.seconds >= 0.05:
        return Finding("slow-line", f"Slow line: {text.strip()[:60]}",
                       "Not a pattern slowrc knows how to fix automatically. If it's an init command, see whether "
                       "it can be loaded on first use.", ln.seconds * 0.5, False, Path(ln.file), ln.line, text)
    return None


def _system_findings(ln: Line, sh: Shell) -> list[Finding]:
    text = ln.text or ""
    if sh.name == "zsh" and "compinit" in text and ln.seconds >= 0.03:
        return [Finding("global-compinit", "Your system zshrc runs compinit too",
                        "On Ubuntu/Debian, /etc/zsh/zshrc runs its own compinit before yours. Add "
                        "`skip_global_compinit=1` to ~/.zshenv to skip it.", ln.seconds, False, Path(ln.file),
                        ln.line, text)]
    if ln.seconds >= 0.05:
        return [Finding("system", f"System file: {home_short(ln.file)}:{ln.line}",
                        f"`{text[:60]}` runs before your own files; slowrc won't edit system files.", 0.0, False,
                        Path(ln.file), ln.line, text)]
    return []
