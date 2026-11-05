import shutil
import subprocess
from pathlib import Path

import pytest

from fakehome import build
from slowrc import fix as fixmod
from slowrc.cli import main
from slowrc.measure import calibrate, detect_shell, median, parse, time_startup, trace
from slowrc.rules import EVAL_RE, NVM_RE, analyze, cacheable

def _traceable(name):
    if not shutil.which(name):
        return False
    from slowrc.measure import can_trace
    return can_trace(detect_shell(name, login=False))


SHELLS = [s for s in ("zsh", "bash") if _traceable(s)]


@pytest.fixture()
def home(tmp_path, monkeypatch):
    def make(shell):
        h = tmp_path / f"home-{shell}"
        info = build(h, shell)
        monkeypatch.setenv("HOME", str(h))
        monkeypatch.setenv("SHELL", shutil.which(shell))
        monkeypatch.setenv("PATH", f"{h / 'fakebin'}:{__import__('os').environ['PATH']}")
        monkeypatch.delenv("ZDOTDIR", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        return h, info
    return make


# ---------------------------------------------------------------------------
# Pure parsing / pattern logic
# ---------------------------------------------------------------------------
def test_parse_attributes_nested_time_to_the_outer_line():
    rs, us, gs = "\x1e", "\x1f", "\x1d"
    raw = "".join([
        f"{rs}1.000{us}zsh:0{us}/h/.zshrc{us}2{us}source ~/.nvm/nvm.sh\n",
        f"{rs}1.010{us}/h/.zshrc:2{gs}zsh:0{us}/h/.nvm/nvm.sh{us}5{us}sleep 1\n",
        f"{rs}1.300{us}zsh:0{us}/h/.zshrc{us}3{us}alias x=y\n",
        f"{rs}1.301{us}zsh:0{us}/h/.zshrc{us}4{us}exit\n",
    ])
    prof = parse(raw, "zsh")
    top = prof.lines[0]
    assert (top.file, top.line) == ("/h/.zshrc", 2) and abs(top.seconds - 0.300) < 1e-9
    assert dict(top.children) == pytest.approx({"/h/.nvm/nvm.sh": 0.290})
    calibrate(prof, 0.2)  # real startup was faster than the traced one
    assert sum(l.seconds for l in prof.lines) <= 0.2 + 1e-9


@pytest.mark.parametrize("line,ok", [
    ('eval "$(pyenv init -)"', True),
    ("eval $(/opt/homebrew/bin/brew shellenv)", True),
    ("source <(kubectl completion zsh)", True),
    ('eval "$(zoxide init zsh)"  # jump around', True),
    ('eval "$(ssh-agent -s)"', False),           # per-session state: never cached
    ('eval "$(fnm env --use-on-cd)"', False),
    ('eval "$(pyenv init - | grep -v rehash)"', False),
    ('eval "$(starship init $SHELL)"', False),    # depends on a variable
])
def test_only_static_init_commands_are_cacheable(line, ok):
    m = EVAL_RE.match(line)
    words = (m.group("a") or m.group("b")).split() if m else []
    assert bool(m and cacheable(words)) is ok


@pytest.mark.parametrize("line", [
    '[ -s "$NVM_DIR/nvm.sh" ] && \\. "$NVM_DIR/nvm.sh"  # This loads nvm',
    'source ~/.nvm/nvm.sh',
    '. "/opt/homebrew/opt/nvm/nvm.sh"',
])
def test_nvm_lines(line):
    assert NVM_RE.match(line)


# ---------------------------------------------------------------------------
# End to end against real shells
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("shell", SHELLS)
def test_profile_finds_the_slow_lines(home, shell):
    h, info = home(shell)
    sh = detect_shell(shell, login=False)
    real = median(time_startup(sh, runs=3))
    prof = trace(sh)
    calibrate(prof, real)
    top = {l.line: l.seconds for l in prof.lines[:3]}
    assert set(top) == {3, 4, 5}  # nvm, pyenv init, brew --prefix
    assert top[3] > 0.2 and top[4] > 0.1 and top[5] > 0.15
    ids = {f.id for f in analyze(sh, prof) if f.auto}
    assert {"nvm", "eval-cache", "brew-prefix"} <= ids


@pytest.mark.parametrize("shell", SHELLS)
def test_fix_is_faster_behaves_the_same_and_undoes(home, shell):
    h, info = home(shell)
    rc = info["rc"]
    original = rc.read_bytes()
    sh = detect_shell(shell, login=False)
    before_snap = fixmod.snapshot(sh)
    real = median(time_startup(sh, runs=3))
    prof = trace(sh)
    calibrate(prof, real)
    applied, problems, backups = fixmod.apply(analyze(sh, prof), sh)
    assert problems == [] and len(applied) >= 3
    after_snap = fixmod.snapshot(sh)
    assert after_snap.node == before_snap.node
    assert set(before_snap.path) <= set(after_snap.path)
    assert median(time_startup(sh, runs=3)) < real / 3
    # lazy nvm still works on first use
    out = subprocess.run([sh.path, "-i", "-c", "nvm --version"], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL).stdout
    assert "0.40.1" in out
    # undo restores the original byte for byte
    assert fixmod.undo() and rc.read_bytes() == original


@pytest.mark.parametrize("shell", SHELLS)
def test_broken_fix_is_reverted(home, shell, monkeypatch):
    h, info = home(shell)
    rc = info["rc"]
    original = rc.read_bytes()
    sh = detect_shell(shell, login=False)
    prof = trace(sh)
    findings = [f for f in analyze(sh, prof) if f.id == "eval-cache"]
    findings[0].replacement = ["this_command_does_not_exist_slowrc"]
    applied, problems, _ = fixmod.apply(findings, sh)
    assert applied == [] and problems and rc.read_bytes() == original


@pytest.mark.parametrize("shell", SHELLS)
def test_cli_report_and_fix(home, shell, capsys):
    h, info = home(shell)
    assert main(["--no-login", "--runs", "3"]) == 0
    out = capsys.readouterr().out
    assert "WHERE THE TIME GOES" in out and "slowrc --fix" in out
    assert main(["--no-login", "--runs", "3", "--fix", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "faster" in out and "slowrc --undo" in out
    assert main(["--undo"]) == 0


def test_cache_refreshes_when_the_tool_changes(home):
    if "zsh" not in SHELLS:
        pytest.skip("zsh not installed")
    h, info = home("zsh")
    sh = detect_shell("zsh", login=False)
    fixmod.apply(analyze(sh, trace(sh)), sh)
    cache = next((h / ".cache/slowrc").glob("pyenv*.sh"))
    pyenv = h / "fakebin/pyenv"
    pyenv.write_text(pyenv.read_text().replace(".pyenv", ".pyenv2"))
    import os, time
    future = time.time() + 5
    os.utime(pyenv, (future, future))
    out = subprocess.run([sh.path, "-i", "-c", "echo $PYENV_ROOT"], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL).stdout
    assert out.strip().endswith(".pyenv2") and ".pyenv2" in cache.read_text()
