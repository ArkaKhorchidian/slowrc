"""slowrc: why does my terminal take so long to open?"""
from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .fix import apply, clear_cache, undo
from .measure import StartupError, calibrate, can_trace, detect_shell, median, prompt_latency, time_startup, trace
from .rules import analyze, home_short

ACCENT = "#ff9f43"
RATINGS = [(0.1, "instant", "#2ecc71"), (0.2, "fast", "#7bd88f"), (0.4, "okay", "#f5d76e"),
           (0.8, "slow", "#ff9f43"), (99, "very slow", "#ff5c6c")]


def rating(s: float) -> tuple[str, str]:
    return next((name, color) for limit, name, color in RATINGS if s < limit)


def fmt(s: float) -> str:
    return f"{s * 1000:.0f} ms" if s < 1 else f"{s:.2f} s"


def headline(label: str, s: float, width: int = 28) -> Text:
    name, color = rating(s)
    filled = max(1, min(width, round(width * min(s, 1.5) / 1.5)))
    t = Text(f"  {label:<10}", style="bold")
    t.append(f"{fmt(s):>8}  ", style=f"bold {color}")
    t.append("█" * filled, style=color)
    t.append("░" * (width - filled), style="#3a3f4b")
    t.append(f"  {name}", style=color)
    return t


def where(prof, total: float, n: int = 8) -> Table:
    t = Table.grid(padding=(0, 1))
    t.add_column(justify="right", style="bold")
    t.add_column()
    t.add_column(style="cyan", no_wrap=True)
    t.add_column(style="#c0c4cc", overflow="ellipsis", no_wrap=True, max_width=60)
    top = prof.lines[0].seconds if prof.lines else 1
    for ln in prof.lines[:n]:
        if ln.seconds < 0.002:
            break
        bar = "█" * max(1, round(16 * ln.seconds / top))
        t.add_row(fmt(ln.seconds), Text(f"{bar:<16}", style=ACCENT), f"{home_short(ln.file)}:{ln.line}", ln.text)
        for child, secs in sorted(ln.children.items(), key=lambda kv: -kv[1])[:3]:
            if secs >= 0.01 and len(ln.children) > 1:
                t.add_row("", "", "", Text(f"  ↳ {fmt(secs)}  {home_short(child)}", style="#7a8494"))
    return t


def fixes_list(findings) -> Text:
    out = Text()
    for f in findings:
        out.append("  ✦ " if f.auto else "  • ", style=f"bold {ACCENT}" if f.auto else "#7a8494")
        out.append(f.title, style="bold" if f.auto else "")
        if f.seconds >= 0.005:
            out.append(f"  −{fmt(f.seconds)}", style="bold #2ecc71")
        if f.file:
            out.append(f"  {home_short(f.file)}:{f.lineno}", style="cyan")
        out.append(f"\n    {f.detail}\n", style="#9aa0aa")
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="slowrc", description="Find out why your shell takes so long to start, and fix it.")
    p.add_argument("--fix", action="store_true", help="apply the fixes marked ✦ (backs up your files first)")
    p.add_argument("--yes", "-y", action="store_true", help="don't ask before fixing")
    p.add_argument("--undo", action="store_true", help="restore the files from before the last --fix")
    p.add_argument("--shell", choices=["zsh", "bash"], help="default: your $SHELL")
    p.add_argument("--login", dest="login", action="store_true", default=None, help="profile a login shell")
    p.add_argument("--no-login", dest="login", action="store_false", help="profile a non-login shell")
    p.add_argument("--runs", type=int, default=10, help="startups to time (default 10)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--version", action="version", version=f"slowrc {__version__}")
    a = p.parse_args(argv)
    console = Console(stderr=a.json)

    if a.undo:
        restored = undo()
        clear_cache()
        if not restored:
            console.print("Nothing to undo.")
            return 1
        for orig, bak in restored.items():
            console.print(f"[bold #2ecc71]✓[/] restored {home_short(orig)} from {home_short(bak)}")
        return 0

    try:
        sh = detect_shell(a.shell, a.login)
        with console.status(f"[#9aa0aa]starting {sh.name} {a.runs} times…[/]", spinner="dots"):
            times = time_startup(sh, a.runs)
            real = median(times)
            prompt = prompt_latency(sh)
        with console.status("[#9aa0aa]tracing one startup…[/]", spinner="dots"):
            prof = trace(sh)
            calibrate(prof, real)
            findings = analyze(sh, prof)
    except StartupError as e:
        console.print(f"[bold red]✗[/] {e}")
        return 2

    auto = [f for f in findings if f.auto]
    saving = min(sum(f.seconds for f in auto), real * 0.95)

    if a.json:
        print(json.dumps({
            "shell": sh.name, "version": sh.version, "login": sh.login, "startup_seconds": real,
            "runs": times, "prompt_seconds": prompt,
            "lines": [{"file": l.file, "line": l.line, "seconds": l.seconds, "text": l.text} for l in prof.lines[:30]],
            "findings": [{"id": f.id, "title": f.title, "auto": f.auto, "seconds": f.seconds,
                          "file": str(f.file) if f.file else None, "line": f.lineno} for f in findings],
        }, indent=2))
        return 0

    console.print()
    console.print(Text.assemble(("slowrc", f"bold {ACCENT}"),
                                (f"  {sh.name} {sh.version} · {'login' if sh.login else 'interactive'} shell · "
                                 f"{len(times)} runs", "#7a8494")))
    console.print()
    console.print(headline("startup", real))
    if prompt is not None:
        console.print(headline("prompt", prompt) + Text("  after every command", style="#7a8494"))
    console.print()
    if not can_trace(sh):
        console.print(f"  [#9aa0aa]Line-by-line profiling needs bash 5 or newer (this is bash {sh.version}; macOS "
                      "ships 3.2). Install a newer bash with `brew install bash`, or profile zsh: `slowrc --shell zsh`.[/]\n")
    if prof.lines:
        console.print(Text(" WHERE THE TIME GOES", style="bold"))
        console.print(where(prof, real))
        console.print()
    if findings:
        console.print(Text(" FIXES", style="bold") + Text("   ✦ = slowrc --fix applies it", style="#7a8494"))
        console.print(fixes_list(findings))
        console.print()
    if not auto:
        console.print(f"  [bold #2ecc71]Nothing slowrc can fix automatically.[/] "
                      + ("Your shell is already fast." if real < 0.2 else "See the suggestions above."))
        return 0
    if not a.fix:
        console.print(Panel(Text.assemble(("slowrc --fix", f"bold {ACCENT}"),
                                          (f"  applies {len(auto)} fix{'es' if len(auto) > 1 else ''}: about ", ""),
                                          (fmt(real), "bold"), (" → ", ""), (fmt(real - saving), "bold #2ecc71"),
                                          ("\nYour files are backed up first, and `slowrc --undo` puts them back.",
                                           "#7a8494")), border_style=ACCENT, expand=False))
        return 0

    if not a.yes:
        console.print(f"  Apply {len(auto)} fix{'es' if len(auto) > 1 else ''}? Your files are backed up first. [Y/n] ",
                      end="")
        try:
            if input().strip().lower() not in ("", "y", "yes"):
                return 0
        except EOFError:
            return 0
    with console.status("[#9aa0aa]applying and checking your shell still works…[/]", spinner="dots"):
        applied, problems, backups = apply(findings, sh)
    if problems:
        console.print(f"[bold red]✗ Reverted:[/] after the fixes {problems[0]}. Your files are unchanged. "
                      "Please open an issue with this message.")
        return 3
    with console.status("[#9aa0aa]measuring again…[/]", spinner="dots"):
        after = median(time_startup(sh, a.runs))
    speedup = real / after if after > 0 else 0
    console.print(Panel(Text.assemble(
        ("before  ", "#7a8494"), (fmt(real), f"bold {rating(real)[1]}"), ("   →   after  ", "#7a8494"),
        (fmt(after), f"bold {rating(after)[1]}"), (f"   {speedup:.1f}× faster" if speedup >= 1.1 else "", "bold")),
        title="[bold]slowrc --fix[/]", title_align="left", border_style="#2ecc71", expand=False, padding=(0, 2)))
    for f in applied:
        console.print(f"  [#2ecc71]✓[/] {f.title}  [#7a8494]{home_short(f.file)}:{f.lineno}[/]")
    console.print(f"  [#7a8494]Backups: {', '.join(home_short(b) for b in backups.values())} · undo with "
                  f"`slowrc --undo`[/]")
    return 0


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
