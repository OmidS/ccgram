"""Shell provider — chat-first shell interface via Telegram.

Extends JsonlProvider to inherit default no-op implementations.
Tmux opens the user's $SHELL by default; overrides only what differs
from the base class (no transcripts, no commands, no bash output).

Two prompt modes for output isolation and exit code detection:
- ``wrap`` (default): appends a small ``⌘N⌘`` marker after the user's
  existing prompt, preserving Tide / Starship / Powerlevel10k / etc.
- ``replace``: replaces the entire prompt with ``{prefix}:N❯``
  (the legacy behaviour, opt-in via ``CCGRAM_PROMPT_MODE=replace``).
"""

import asyncio
import functools
import os
import re
from typing import Any, ClassVar

from ccgram.providers._jsonl import JsonlProvider
from ccgram.providers.base import ProviderCapabilities

_DEFAULT_MARKER = "ccgram"


_VALID_PROMPT_MODES = frozenset({"wrap", "replace"})
_WARNED_INVALID_MODE = False


def _get_prompt_mode() -> str:
    """Return the configured prompt mode (``wrap`` or ``replace``)."""
    global _WARNED_INVALID_MODE  # noqa: PLW0603
    from ccgram.config import config

    mode = getattr(config, "prompt_mode", "wrap") or "wrap"
    if mode not in _VALID_PROMPT_MODES:
        if not _WARNED_INVALID_MODE:
            _WARNED_INVALID_MODE = True
            import structlog

            structlog.get_logger().warning(
                "Invalid CCGRAM_PROMPT_MODE=%r, defaulting to 'wrap'", mode
            )
        return "wrap"
    return mode


def _get_marker_prefix() -> str:
    """Return the configured prompt marker prefix (used in ``replace`` mode)."""
    from ccgram.config import config

    return getattr(config, "prompt_marker", _DEFAULT_MARKER) or _DEFAULT_MARKER


@functools.cache
def _compile_replace_re(prefix: str) -> re.Pattern[str]:
    """Compile prompt regex for ``replace`` mode (cached per unique prefix)."""
    return re.compile(rf"^{re.escape(prefix)}:(\d+)❯\s?(.*)")


_WRAP_RE = re.compile(r"⌘(\d+)⌘\s?(.*)$")


def match_prompt(line: str) -> re.Match[str] | None:
    """Match a prompt marker in *line*, respecting the current prompt mode.

    In ``replace`` mode the marker is at line start (``re.match``).
    In ``wrap`` mode the marker can appear anywhere (``re.search``).
    """
    if _get_prompt_mode() == "replace":
        return _compile_replace_re(_get_marker_prefix()).match(line)
    return _WRAP_RE.search(line)


KNOWN_SHELLS = frozenset({"bash", "zsh", "fish", "sh", "dash", "tcsh", "csh", "ksh"})


async def has_prompt_marker(window_id: str) -> bool:
    """Check if the prompt marker is present in the pane."""
    from ccgram.tmux_manager import tmux_manager

    capture = await tmux_manager.capture_pane(window_id)
    if not capture:
        return False
    return any(match_prompt(line) for line in capture.rstrip().splitlines()[-5:])


def get_shell_name() -> str:
    """Return the basename of the bot process's $SHELL (e.g. 'fish', 'zsh').

    Sync fallback — for pane-accurate detection use ``detect_pane_shell()``.
    """
    return os.environ.get("SHELL", "").rsplit("/", 1)[-1]


async def detect_pane_shell(window_id: str) -> str:
    """Detect the shell running in a tmux pane via pane_current_command.

    Falls back to ``get_shell_name()`` when the pane is unavailable or
    its command is not a recognized shell.
    """
    from ccgram.tmux_manager import tmux_manager

    window = await tmux_manager.find_window_by_id(window_id)
    if window and window.pane_current_command:
        tokens = window.pane_current_command.split()
        if not tokens:
            return get_shell_name()
        basename = os.path.basename(tokens[0])
        cleaned = basename.lstrip("-")
        if cleaned in KNOWN_SHELLS:
            return cleaned
    return get_shell_name()


def _wrap_setup_commands(shell: str) -> str:
    """Return the shell command that appends a ⌘N⌘ marker to the prompt."""
    # Fish: wrap existing fish_prompt, preserving Tide/Starship/etc.
    # Uses set_color instead of raw ANSI — avoids escape mangling via send_keys.
    # Fallback: if fish_prompt doesn't exist (minimal config), define a no-op.
    fish = (
        "functions -c fish_prompt __ccgram_orig_prompt 2>/dev/null; "
        "or function __ccgram_orig_prompt; end; "
        "function fish_prompt; "
        "set -l __s $status; "
        "__ccgram_orig_prompt; "
        "set_color brblack; printf '⌘%d⌘ ' $__s; set_color normal; "
        "end"
    )
    # Bash: save exit code in PROMPT_COMMAND before user hooks run,
    # then append marker to existing PS1.  ANSI dim codes are safe
    # inside a PS1 string assignment (bash interprets \033 at render time).
    bash = (
        "__ccgram_sc(){ __ccgram_x=$?; return $__ccgram_x; }; "
        'PROMPT_COMMAND="__ccgram_sc${PROMPT_COMMAND:+;$PROMPT_COMMAND}"; '
        'PS1="${PS1}\\[\\033[2m\\]⌘\\${__ccgram_x}⌘\\[\\033[0m\\] "'
    )
    # Zsh: append marker to existing PROMPT.  %{...%} wraps non-printing
    # sequences; zsh interprets \033 at render time.
    zsh = 'PROMPT="${PROMPT}%{\\033[2m%}⌘%?⌘%{\\033[0m%} "'
    # tcsh/csh: append marker to existing prompt (no dim support).
    tcsh = 'set prompt = "${prompt}⌘$status⌘ "'
    return {"fish": fish, "bash": bash, "zsh": zsh, "tcsh": tcsh, "csh": tcsh}.get(
        shell, bash
    )


def _replace_setup_commands(shell: str, prefix: str) -> str:
    """Return the shell command that replaces the prompt with {prefix}:N❯."""
    cmds = {
        "fish": f'function fish_prompt; printf "{prefix}:$status❯ "; end',
        "bash": f"PS1='{prefix}:$?❯ '",
        "zsh": f"PROMPT='{prefix}:%?❯ '",
        "tcsh": f'set prompt = "{prefix}:$status❯ "',
        "csh": f'set prompt = "{prefix}:$status❯ "',
    }
    return cmds.get(shell, cmds["bash"])


async def setup_shell_prompt(window_id: str, *, clear: bool = True) -> None:
    """Configure the shell prompt with a detectable marker.

    In ``wrap`` mode the existing prompt is preserved and a small ``⌘N⌘``
    suffix is appended.  In ``replace`` mode the prompt is fully replaced
    with ``{prefix}:N❯``.

    No-op if the marker is already present in the pane (idempotent).
    Set ``clear=False`` when attaching to an existing session to
    preserve scrollback context.
    """
    if await has_prompt_marker(window_id):
        return

    from ccgram.tmux_manager import tmux_manager

    # Cancel any partial input to prevent concatenation with the setup command
    await tmux_manager.send_keys(window_id, "C-c", enter=False, literal=False)
    await asyncio.sleep(0.1)

    shell = await detect_pane_shell(window_id)
    mode = _get_prompt_mode()
    if mode == "replace":
        cmd = _replace_setup_commands(shell, _get_marker_prefix())
    else:
        cmd = _wrap_setup_commands(shell)
    await tmux_manager.send_keys(window_id, cmd)
    await asyncio.sleep(0.3)
    if clear:
        await tmux_manager.send_keys(window_id, "clear")


class ShellProvider(JsonlProvider):
    """AgentProvider implementation for raw shell sessions."""

    _CAPS: ClassVar[ProviderCapabilities] = ProviderCapabilities(
        name="shell",
        launch_command="",
        supports_hook=False,
        supports_hook_events=False,
        supports_resume=False,
        supports_continue=False,
        supports_structured_transcript=False,
        supports_incremental_read=False,
        transcript_format="plain",
    )

    def make_launch_args(
        self,
        resume_id: str | None = None,  # noqa: ARG002
        use_continue: bool = False,  # noqa: ARG002
    ) -> str:
        return ""

    def parse_transcript_line(
        self,
        line: str,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return None

    def read_transcript_file(
        self,
        file_path: str,  # noqa: ARG002
        last_offset: int,  # noqa: ARG002
    ) -> tuple[list[dict[str, Any]], int]:
        return [], 0

    def extract_bash_output(
        self,
        pane_text: str,  # noqa: ARG002
        command: str,  # noqa: ARG002
    ) -> str | None:
        return None
