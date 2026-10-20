"""Launch commands for installed games and helpers for opening files/URLs.

The app never runs anything on its own: these functions are only called
after the user pressed *Launch* and confirmed the executable.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from ezgd.library.models import CompatConfig, LaunchMethod
from ezgd.platform import IS_LINUX, IS_WINDOWS
from ezgd.platform.proton import proton_command, umu_command, validate_overrides
from ezgd.platform.steam import ProtonInstall, find_runtime, shortcut_rungameid

log = logging.getLogger(__name__)

WINDOWS_EXTS = (".exe", ".bat", ".cmd", ".msi", ".lnk")


class LaunchError(Exception):
    pass


@dataclass
class LaunchPlan:
    argv: list[str]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""
    pre: list = field(default_factory=list)      # ExtraCommand run before the game (Windows parser)
    post: list = field(default_factory=list)     # ExtraCommand run after the game exits (Windows parser)
    shell_script: str | None = None              # Linux: the launch-options line given to /bin/sh -c


def is_windows_binary(path: Path) -> bool:
    return path.suffix.lower() in WINDOWS_EXTS


def is_native_linux_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return False
    return head == b"\x7fELF" or (head[:2] == b"#!" and path.suffix.lower() in ("", ".sh", ".x86_64", ".x86"))


def resolve_method(exe: Path, method: LaunchMethod, protons: list[ProtonInstall]) -> LaunchMethod:
    if method is not LaunchMethod.AUTO:
        return method
    if IS_WINDOWS or not is_windows_binary(exe):
        return LaunchMethod.NATIVE
    if protons or shutil.which("umu-run"):
        return LaunchMethod.PROTON
    if shutil.which("wine"):
        return LaunchMethod.WINE
    raise LaunchError("This is a Windows executable and no Proton, umu-launcher or Wine was found. Install "
                      "them from Settings → System check → Install missing tools.")


_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
COMMAND_TOKEN = "%command%"


SHELL = "/bin/sh"


def steam_shell_script(options: str, argv: list[str]) -> str:
    """Steam's launch-option rule on Linux.

    ``%command%`` is replaced by the full, quoted command (runtime + Proton + game); if it is absent the
    options are appended after the command. The resulting line is run by ``/bin/sh -c``, so everything a
    shell accepts works — ``VAR=1 %command%``, ``gamemoderun %command%``,
    ``systemctl --user restart pipewire wireplumber; %command%``, pipes, redirections, ``$(…)``.
    """
    command = " ".join(shlex.quote(a) for a in argv)
    options = options.strip()
    if COMMAND_TOKEN not in options:
        return f"{command} {options}"
    return options.replace(COMMAND_TOKEN, command)


def validate_launch_options(text: str) -> None:
    """Raise LaunchError if the options cannot work (Linux: shell syntax; Windows: argument parsing)."""
    if not (text or "").strip():
        return
    if IS_WINDOWS:
        parse_launch_options(text)
    else:
        check_shell_syntax(steam_shell_script(text, ["game"]))


def check_shell_syntax(script: str) -> None:
    """Reject launch options the shell cannot parse (``sh -n`` reads without executing anything)."""
    try:
        r = subprocess.run([SHELL, "-n", "-c", script], capture_output=True, text=True, timeout=5,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaunchError(f"Could not check the launch options: {exc}") from exc
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        detail = msg[-1] if msg else f"exit code {r.returncode}"
        detail = re.sub(r"^/bin/sh: (-c: )?(line \d+: )?", "", detail)
        raise LaunchError(f"Launch options have a syntax error: {detail}")


_SHELL_WORDS = {"exec", "env", "cd", "export", "unset", "if", "then", "else", "elif", "fi", "for", "in", "do",
                "done", "while", "until", "case", "esac", "true", "false", "echo", "printf", "test", "[", "[[",
                "command", "time", "nice", "eval", "set", "trap", "wait", "sleep", "read", "!", "{", "}", ":",
                "source", ".", "kill", "exit", "return", "shift", "local"}


def launch_option_warnings(options: str) -> list[str]:
    """Non-blocking hints: programs named in the launch options that are not installed."""
    if IS_WINDOWS or not options.strip():
        return []
    try:
        lexer = shlex.shlex(options.replace(COMMAND_TOKEN, " true "), posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    warnings, expect_cmd = [], True
    for t in tokens:
        if t and set(t) <= set(";&|<>()"):
            expect_cmd = t not in (">", ">>", "<", "<<", ">&", "<&", "&>")
            continue
        if expect_cmd:
            if _ENV_ASSIGN.match(t):
                continue
            if t not in _SHELL_WORDS and "$" not in t and "/" not in t and not shutil.which(t):
                warnings.append(f"“{t}” is not installed — the shell will report “command not found”.")
            expect_cmd = False
    return list(dict.fromkeys(warnings))


CHAIN_OPERATORS = {";", "&&"}
REJECTED_OPERATORS = {"|", "||", "&", ">", ">>", "<", "<<", "(", ")", ">&", "<&", "&>", "|&"}
EXTRA_COMMAND_TIMEOUT = 120          # seconds a pre-launch / post-exit command may take


@dataclass
class ExtraCommand:
    """A command run before the game starts or after it exits (no shell involved)."""

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    # pre-launch: stop (do not start the game) if it fails — written as `cmd && %command%`
    # post-exit:  only run if what came before succeeded — written as `%command% && cmd`
    needs_success: bool = False

    def display(self) -> str:
        q = (lambda s: subprocess.list2cmdline([s])) if IS_WINDOWS else shlex.quote
        env = " ".join(f"{k}={q(v)}" for k, v in self.env.items())
        return (env + " " if env else "") + " ".join(q(a) for a in self.argv)


@dataclass
class LaunchOptions:
    """Steam-style launch options.

    * Without ``%command%`` everything is appended to the game's command line
      (``-windowed -dx11``), exactly like Steam.
    * With ``%command%``: leading ``NAME=value`` words become environment
      variables, the remaining words before it wrap the game (``gamemoderun``,
      ``mangohud``, ``gamescope -W 1920 --``), and words after it are game
      arguments — e.g. ``PROTON_LOG=1 gamemoderun %command% -nolauncher``.
    * Commands can be chained around ``%command%`` with ``&&`` and ``;`` like in
      Steam: ``systemctl --user restart pipewire wireplumber && %command%`` runs
      the restart first and starts the game only if it succeeded;
      ``%command% ; notify-send done`` runs something after the game exits.

    Unlike Steam, the text is never passed to a shell: every command is run
    directly, so pipes, redirections, ``||``, background ``&`` and ``$(…)`` are
    rejected and launch options cannot smuggle in shell code.
    """

    env: dict[str, str] = field(default_factory=dict)
    wrappers: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    uses_command: bool = False
    pre: list[ExtraCommand] = field(default_factory=list)
    post: list[ExtraCommand] = field(default_factory=list)


def _tokenize(text: str, posix: bool) -> list[str]:
    lexer = shlex.shlex(text, posix=posix, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError as exc:
        raise LaunchError(f"Launch options: {exc}") from exc


def _split_env(words: list[str]) -> tuple[dict[str, str], list[str]]:
    env: dict[str, str] = {}
    i = 0
    while i < len(words) and _ENV_ASSIGN.match(words[i]):
        k, _, v = words[i].partition("=")
        env[k] = v
        i += 1
    return env, words[i:]


def _resolve(program: str) -> str:
    found = shutil.which(program)
    if not found:
        raise LaunchError(f"“{program}” from the launch options is not installed.")
    return found


def parse_launch_options(text: str, posix: bool = not IS_WINDOWS) -> LaunchOptions:
    text = (text or "").strip()
    if not text:
        return LaunchOptions()
    tokens = _tokenize(text, posix)
    bad = [t for t in tokens if t in REJECTED_OPERATORS or t.startswith(("$(", "`"))
           or (t and set(t) <= set(";&|<>()") and t not in CHAIN_OPERATORS)]
    if bad:
        raise LaunchError(f"Launch options cannot contain shell operators like “{bad[0]}” (pipes, redirections, "
                          f"||, & or $(…)); only && and ; are supported, to run commands before or after the game.")
    # split into segments separated by ; or &&
    segments: list[list[str]] = [[]]
    ops: list[str] = []
    for t in tokens:
        if t in CHAIN_OPERATORS:
            if not segments[-1]:
                raise LaunchError(f"“{t}” needs a command on both sides.")
            ops.append(t)
            segments.append([])
        else:
            segments[-1].append(t)
    if not segments[-1]:
        if ops and ops[-1] == ";" and len(segments) > 1:
            segments.pop()
            ops.pop()                          # a trailing ";" is harmless
        else:
            raise LaunchError(f"“{ops[-1]}” needs a command on both sides.")
    with_command = [i for i, seg in enumerate(segments) if COMMAND_TOKEN in seg]
    if not with_command:
        if any(COMMAND_TOKEN in t for seg in segments for t in seg):
            raise LaunchError("Write %command% as a separate word.")
        if len(segments) > 1:
            raise LaunchError("Use %command% to mark where the game starts when chaining commands with && or ;.")
        return LaunchOptions(args=segments[0])
    if len(with_command) > 1 or segments[with_command[0]].count(COMMAND_TOKEN) > 1:
        raise LaunchError("%command% may only appear once in the launch options.")
    gi = with_command[0]

    game = segments[gi]
    i = game.index(COMMAND_TOKEN)
    env, wrappers = _split_env(game[:i])
    opts = LaunchOptions(env=env, wrappers=wrappers, args=game[i + 1:], uses_command=True)
    if opts.wrappers:
        opts.wrappers[0] = _resolve(opts.wrappers[0])

    for idx, seg in enumerate(segments):
        if idx == gi:
            continue
        seg_env, argv = _split_env(seg)
        if not argv:
            raise LaunchError("A chained command contains only variable assignments.")
        argv[0] = _resolve(argv[0])
        if idx < gi:
            # the operator right after this segment decides whether its failure blocks the launch
            opts.pre.append(ExtraCommand(argv, seg_env, needs_success=ops[idx] == "&&"))
        else:
            # the operator right before this segment decides whether it needs success so far
            opts.post.append(ExtraCommand(argv, seg_env, needs_success=ops[idx - 1] == "&&"))
    return opts


def run_extra(cmd: ExtraCommand, cwd: str, log_path: Path | None) -> int:
    """Run one pre-launch / post-exit command (no shell, bounded time, output to the game log)."""
    env = os.environ.copy()
    env.update(cmd.env)
    out = open(log_path, "ab") if log_path else subprocess.DEVNULL
    try:
        if log_path:
            out.write(f"--- running: {cmd.display()}\n".encode())
            out.flush()
        kwargs: dict = {"cwd": cwd, "env": env, "stdin": subprocess.DEVNULL, "stdout": out, "stderr": out,
                        "timeout": EXTRA_COMMAND_TIMEOUT}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return subprocess.run(cmd.argv, **kwargs).returncode
    except subprocess.TimeoutExpired:
        raise LaunchError(f"“{Path(cmd.argv[0]).name}” did not finish within {EXTRA_COMMAND_TIMEOUT} s.")
    except OSError as exc:
        raise LaunchError(f"Could not run “{Path(cmd.argv[0]).name}”: {exc.strerror or exc}") from exc
    finally:
        if log_path:
            out.close()


def run_pre_launch(plan: "LaunchPlan", log_path: Path | None) -> None:
    for cmd in plan.pre:
        code = run_extra(cmd, plan.cwd, log_path)
        if code != 0:
            if cmd.needs_success:
                raise LaunchError(f"The pre-launch command “{cmd.display()}” failed (exit code {code}), so the "
                                  f"game was not started (it is joined to the game with &&).")
            log.warning("pre-launch command %s exited with %s; continuing (joined with ;)", cmd.argv[0], code)


def run_post_exit(plan: "LaunchPlan", game_exit_code: int, log_path: Path | None) -> None:
    ok = game_exit_code == 0
    for cmd in plan.post:
        if cmd.needs_success and not ok:
            log.info("skipping post-exit command %s (previous step failed)", cmd.argv[0])
            continue
        try:
            ok = run_extra(cmd, plan.cwd, log_path) == 0
        except LaunchError as exc:
            log.warning("post-exit command failed: %s", exc)
            ok = False


def build_plan(exe: Path, *, method: LaunchMethod, args: str = "", protons: list[ProtonInstall] | None = None,
               proton_name: str | None = None, steam_root: Path | None = None, prefix_dir: Path | None = None,
               steam_shortcut_appid: int | None = None, steam_app_id: int | None = None,
               compat: CompatConfig | None = None, game_root: Path | None = None,
               log_dir: Path | None = None, fix_env: dict | None = None, fix_wrappers: list[str] | None = None,
               fix_args: list[str] | None = None, fix_notes: list[str] | None = None,
               steam_client: bool = False) -> LaunchPlan:
    exe = Path(exe)
    if method is not LaunchMethod.STEAM and not exe.is_file():
        raise LaunchError(f"Executable not found: {exe}")
    protons = protons or []
    compat = compat or CompatConfig()
    method = resolve_method(exe, method, protons)
    use_shell = not IS_WINDOWS and bool((args or "").strip())
    opts = LaunchOptions() if use_shell else parse_launch_options(args)
    cwd = str(exe.parent)

    def finish(argv: list[str], env: dict[str, str], description: str) -> LaunchPlan:
        # enabled Fixes: wrappers in front, game arguments after the executable, extra environment
        argv = list(fix_wrappers or []) + argv + list(fix_args or [])
        env = dict(env)
        for k, v in (fix_env or {}).items():
            if k == "WINEDLLOVERRIDES" and env.get(k):
                names = {p.split("=", 1)[0].lower() for p in env[k].split(";") if p}
                env[k] = ";".join([env[k]] + [p for p in v.split(";") if p.split("=", 1)[0].lower() not in names])
            else:
                env[k] = v
        if fix_notes:
            description += " · fixes: " + ", ".join(fix_notes)
        if use_shell:
            # exactly like Steam on Linux: %command% → the quoted command, then `sh -c` runs the line
            script = steam_shell_script(args, argv)
            check_shell_syntax(script)
            return LaunchPlan([SHELL, "-c", script], cwd, env, description + " · launch options run by /bin/sh",
                              shell_script=script)
        env = {**env, **opts.env}
        if opts.uses_command and (opts.env or opts.wrappers):
            description += " · launch options: " + " ".join(
                [f"{k}=…" for k in opts.env] + [Path(w).name for w in opts.wrappers[:1]])
        if opts.pre:
            description += f" · {len(opts.pre)} command(s) before launch"
        if opts.post:
            description += f" · {len(opts.post)} command(s) after exit"
        return LaunchPlan(opts.wrappers + argv + opts.args, cwd, env, description, opts.pre, opts.post)

    if method is LaunchMethod.STEAM:
        if not steam_shortcut_appid:
            raise LaunchError("Add the game to Steam first.")
        steam = shutil.which("steam") or (str(steam_root / "steam.exe") if IS_WINDOWS and steam_root else None)
        if not steam:
            raise LaunchError("Steam executable not found.")
        # Steam applies the shortcut's own launch options
        return LaunchPlan([steam, f"steam://rungameid/{shortcut_rungameid(steam_shortcut_appid)}"], cwd,
                          description="via Steam shortcut")

    if method is LaunchMethod.NATIVE:
        if IS_WINDOWS:
            if exe.suffix.lower() in (".bat", ".cmd"):
                return finish(["cmd.exe", "/c", str(exe)], {}, "batch file")
            return finish([str(exe)], {}, "native")
        if is_windows_binary(exe):
            raise LaunchError("Windows executables cannot run natively on Linux; choose Proton or Wine.")
        if not os.access(exe, os.X_OK):
            raise LaunchError(f"{exe.name} is not marked executable. Use 'Allow executing' in the "
                              f"game's settings if you trust it.")
        return finish([str(exe)], {}, "native Linux")

    if not IS_LINUX:
        raise LaunchError(f"{method} launching is only available on Linux.")
    prefix_dir = prefix_dir or Path.home() / ".local" / "share" / "EZGameDownloader" / "prefixes" / "default"
    prefix_dir.mkdir(parents=True, exist_ok=True)

    if method is LaunchMethod.PROTON:
        chosen = next((p for p in protons if p.name == proton_name), protons[0] if protons else None)
        umu = shutil.which("umu-run")
        steam_runtime_ok = bool(chosen and steam_root and (
            not compat.use_runtime or not chosen.require_tool_appid
            or find_runtime(steam_root, chosen.require_tool_appid)))
        try:
            if chosen is not None and steam_root is not None and (steam_runtime_ok or not umu):
                # the exact chain Steam uses (or plain Proton when no runtime and no umu)
                cmd = proton_command(exe, chosen, steam_root=steam_root, prefix_dir=prefix_dir,
                                     game_root=game_root, steam_app_id=steam_app_id, compat=compat,
                                     log_dir=log_dir, steam_client=steam_client)
            elif umu:
                cmd = umu_command(exe, umu, prefix_dir=prefix_dir, proton=chosen, steam_app_id=steam_app_id,
                                  compat=compat, steam_client=steam_client)
            else:
                raise LaunchError("No way to run Windows games was found. Install umu-launcher or Steam "
                                  "(Settings → System check → Install missing tools).")
        except ValueError as exc:
            raise LaunchError(str(exc)) from exc
        return finish(cmd.argv, cmd.env, cmd.description)

    if method is LaunchMethod.WINE:
        wine = shutil.which("wine")
        if not wine:
            raise LaunchError("Wine is not installed.")
        env = {"WINEPREFIX": str(prefix_dir / "wine")}
        if compat.dll_overrides:
            try:
                env["WINEDLLOVERRIDES"] = validate_overrides(compat.dll_overrides)
            except ValueError as exc:
                raise LaunchError(str(exc)) from exc
        return finish([wine, str(exe)], env, "Wine")
    raise LaunchError(f"unknown launch method {method}")


def spawn(plan: LaunchPlan, log_path: Path | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(plan.env)
    out = open(log_path, "ab") if log_path else subprocess.DEVNULL
    kwargs: dict = {"cwd": plan.cwd, "env": env, "stdin": subprocess.DEVNULL, "stdout": out, "stderr": out}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    log.info("launching %s (%s)", plan.argv[0], plan.description)
    try:
        return subprocess.Popen(plan.argv, **kwargs)
    except OSError as exc:
        raise LaunchError(f"Could not start the game: {exc.strerror or exc}") from exc
    finally:
        if log_path:
            out.close()


def terminate(proc: subprocess.Popen, timeout: float = 8.0) -> None:
    """Stop a game and everything it started (Proton keeps wineserver etc. in the same session)."""
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        return
    import signal
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def display_command(plan: LaunchPlan) -> str:
    """Human-readable command line (for previews and logs)."""
    q = (lambda s: subprocess.list2cmdline([s])) if IS_WINDOWS else shlex.quote
    ready = "MANGOHUD_CONFIGFILE" in plan.env         # MangoHud loaded hidden, for the on/off switch (not shown)
    interesting = {k: v for k, v in plan.env.items() if not k.startswith("STEAM_COMPAT_CLIENT")
                   and not (k == "WINEDEBUG" and v == "-all,+msgbox")      # (error pop-ups' text into the log)
                   and not (ready and k in ("MANGOHUD", "MANGOHUD_CONFIGFILE"))}
    env = " ".join(f"{k}={q(v)}" for k, v in sorted(interesting.items()))
    if plan.shell_script is not None:                 # what /bin/sh -c will run, with the app's environment
        return (env + " " if env else "") + plan.shell_script
    game = (env + " " if env else "") + " ".join(q(a) for a in plan.argv)
    out = ""
    for c in plan.pre:
        out += c.display() + (" && " if c.needs_success else " ; ")
    out += game
    for c in plan.post:
        out += (" && " if c.needs_success else " ; ") + c.display()
    return out


def open_path(path: Path) -> None:
    """Show a file or folder in the system file manager (never executes it)."""
    path = Path(path)
    target = path if path.is_dir() else path.parent
    if IS_WINDOWS:
        os.startfile(str(target))  # noqa: S606 - opening a folder in Explorer
    elif shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", str(target)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    else:
        raise LaunchError("No file manager opener (xdg-open) found.")


def open_url(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise LaunchError("Only web links can be opened.")
    webbrowser.open(url)
