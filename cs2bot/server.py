"""Supervises the CS2 dedicated server as a tmux-hosted process.

The bot owns the server's lifecycle: it launches your existing start_cs2.sh
inside a detached tmux session (giving it a real pty), keeps a copy of its
output flowing through a FIFO into a bounded in-memory ring buffer, and can
best-effort pop open a GUI terminal window attached to that session for a
fully interactive console. Nothing is ever written to real disk — the ring
buffer is capped at `log_buffer_lines` and old lines fall off the end, and
the FIFO used to feed it lives on tmpfs (/dev/shm), not the filesystem proper.

tmux is what makes "a human can type directly into CS2's console" and "the
bot still sees every line for health checks" both true at once: a plain
piped subprocess can only have one reader of its stdout, but a tmux pane's
output can be tapped (via `pipe-pane`) while still being attachable by a
real terminal.

Startup health is judged from that same ring buffer: a start is "healthy"
once every configured startup marker has appeared. If the process exits
early or the markers never show up within the timeout, the start is
unhealthy.
"""

import asyncio
import collections
import logging
import os
import select
import shlex
import shutil
import signal
import subprocess
import threading
import time

from . import plugins

log = logging.getLogger("cs2bot.server")

_TERMINAL_CANDIDATES = ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm")
_FIFO_DIR = "/dev/shm/cs2bot"


class ServerManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self._session = cfg.tmux_session
        self._pane_pid: int | None = None
        self._fifo_path: str | None = None
        self._reader: asyncio.Task | None = None
        self._reader_stop = threading.Event()
        self._buffer = collections.deque(maxlen=cfg.log_buffer_lines)
        self._seen_markers: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._pane_pid is not None and self._pid_alive(self._pane_pid)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def _tmux(self, *args, timeout=10) -> str:
        proc = await asyncio.create_subprocess_exec(
            "tmux", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"tmux {' '.join(args)} failed: {stderr.decode(errors='replace').strip()}")
        return stdout.decode(errors="replace")

    async def _tmux_session_exists(self, session: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", session,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0

    def _process_line(self, line: str):
        self._buffer.append(line)
        # DEBUG, not INFO: CS2 is a stdout firehose and this runs in
        # the event loop, so logging every line at the default level
        # floods the loop with synchronous write syscalls and starves
        # Discord interaction handlers of their 3s ACK window (commands
        # time out). Lines are still retained in the ring buffer above
        # and surfaced via _log_tail() on failure.
        log.debug("[GAME] %s", line)
        for marker in self.cfg.startup_markers:
            if marker in line:
                self._seen_markers.add(marker)

    def _read_fifo_loop(self, path: str, stop_event: threading.Event):
        """Runs in a worker thread. Non-blocking + select so `stop_event`
        bounds shutdown to ~1s even if the tmux-side pipe never closes on
        its own — a blocking read here couldn't be interrupted by cancelling
        the enclosing to_thread task."""
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        pending = b""
        try:
            while not stop_event.is_set():
                ready, _, _ = select.select([fd], [], [], 1.0)
                if not ready:
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    self._process_line(raw.decode("utf-8", errors="replace"))
        except Exception as e:  # pragma: no cover - defensive
            log.warning("fifo reader stopped: %s", e)
        finally:
            os.close(fd)

    def _spawn_terminal(self, session: str):
        """Best-effort: open a GUI terminal attached to the tmux session.
        Never raises — if this fails or no display is available, the
        session is still fully usable via `tmux attach` over SSH."""
        attach_hint = f"tmux attach -t {session}"
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            log.info("no display available; attach manually with: %s", attach_hint)
            return

        candidates = (
            [self.cfg.terminal_emulator]
            if self.cfg.terminal_emulator != "auto"
            else list(_TERMINAL_CANDIDATES)
        )
        for name in candidates:
            path = shutil.which(name)
            if not path:
                continue
            args = [path, "--", "tmux", "attach", "-t", session] if name == "gnome-terminal" \
                else [path, "-e", "tmux", "attach", "-t", session]
            try:
                subprocess.Popen(
                    args, start_new_session=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                log.info("opened %s attached to tmux session %s", name, session)
                return
            except OSError as e:
                log.warning("failed to spawn %s: %s", name, e)

        log.warning("no terminal emulator found; attach manually with: %s", attach_hint)

    async def start(self):
        """Launch the server. Does nothing if already running."""
        async with self._lock:
            if self.is_running:
                log.info("start requested but server already running")
                return
            # Reassign rather than .clear() so a slow-to-exit previous
            # reader thread (if one ever leaked) can't mix stale lines
            # into this instance's buffer.
            self._buffer = collections.deque(maxlen=self.cfg.log_buffer_lines)
            self._seen_markers = set()

            # Verify the Metamod search-path entry is still in gameinfo.gi
            # before every launch, not just after updates -- a manual game
            # reinstall or file restore outside the bot's update flow would
            # otherwise leave it unpatched until the next daily update.
            # patch_gameinfo() is a no-op if the entry is already present.
            try:
                await asyncio.to_thread(plugins.patch_gameinfo, self.cfg.csgo_dir)
            except Exception as e:
                log.error("gameinfo.gi patch failed: %s", e)

            session = self._session
            if await self._tmux_session_exists(session):
                log.warning("found stale tmux session %r from a previous run; killing it", session)
                try:
                    await self._tmux("kill-session", "-t", session)
                except RuntimeError as e:
                    log.warning("failed to kill stale session: %s", e)

            fifo_dir = _FIFO_DIR
            os.makedirs(fifo_dir, exist_ok=True)
            os.chmod(fifo_dir, 0o700)
            fifo_path = os.path.join(fifo_dir, f"{session}-{os.getpid()}-{time.time_ns()}.pipe")
            os.mkfifo(fifo_path, 0o600)

            log.info("launching CS2 via %s (nice %d) in tmux session %s",
                      self.cfg.launch_script, self.cfg.server_nice, session)
            launch_cmd = f"nice -n {self.cfg.server_nice} /bin/bash {shlex.quote(self.cfg.launch_script)}"
            await self._tmux(
                "new-session", "-d", "-s", session,
                "-c", str(self.cfg.launch_cwd),
                launch_cmd,
            )

            panes = await self._tmux("list-panes", "-t", session, "-F", "#{pane_pid}")
            self._pane_pid = int(panes.strip().splitlines()[0])

            # Tee the pane's output (including any keystrokes typed by a
            # human attached via `tmux attach`, echoed back by the pty) into
            # the FIFO so the bot keeps seeing every line for health checks
            # and the log buffer, exactly as it did with a plain piped
            # subprocess. No -O/-I flag: those are tmux >=3.2 only, and
            # output-direction is the default with no flag on every version.
            await self._tmux("pipe-pane", "-t", session, f"cat >> {shlex.quote(fifo_path)}")

            self._fifo_path = fifo_path
            self._reader_stop = threading.Event()
            self._reader = asyncio.create_task(
                asyncio.to_thread(self._read_fifo_loop, fifo_path, self._reader_stop)
            )

            self._spawn_terminal(session)

    async def stop(self):
        """Stop the server, escalating SIGTERM -> SIGKILL on the whole group."""
        async with self._lock:
            if not self.is_running:
                return
            pid = self._pane_pid
            session = self._session
            try:
                pgid = os.getpgid(pid)
            except ProcessLookupError:
                pgid = None

            log.info("stopping CS2 (pid %s)", pid)
            self._signal_group(pgid, pid, signal.SIGTERM)
            if not await self._wait_pid_gone(pid, self.cfg.stop_timeout):
                log.warning("SIGTERM timed out; sending SIGKILL")
                self._signal_group(pgid, pid, signal.SIGKILL)
                if not await self._wait_pid_gone(pid, 15):
                    log.error("server did not exit after SIGKILL")

            # Killing the CS2 process group above often makes tmux tear the
            # session (and, since it's the last one, the whole server) down
            # on its own -- that's a normal outcome, not a failure, so only
            # bother closing the pipe / killing the session if it's still there.
            if await self._tmux_session_exists(session):
                try:
                    await self._tmux("pipe-pane", "-t", session)
                except RuntimeError as e:
                    log.warning("failed to close tmux pipe-pane: %s", e)
                try:
                    await self._tmux("kill-session", "-t", session)
                except RuntimeError as e:
                    log.warning("failed to kill tmux session: %s", e)

            if self._reader:
                self._reader_stop.set()
                try:
                    await asyncio.wait_for(self._reader, timeout=5)
                except asyncio.TimeoutError:
                    log.warning("fifo reader did not exit in time")
                self._reader = None

            if self._fifo_path:
                try:
                    os.unlink(self._fifo_path)
                except FileNotFoundError:
                    pass
                self._fifo_path = None

            self._pane_pid = None

    @staticmethod
    def _signal_group(pgid, pid, sig):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            pass

    async def _wait_pid_gone(self, pid: int, timeout: float) -> bool:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if not self._pid_alive(pid):
                return True
            await asyncio.sleep(0.5)
        return not self._pid_alive(pid)

    async def wait_healthy(self) -> bool:
        """Wait until all startup markers appear, or the process dies, or we
        time out. Returns True only if the server came up cleanly."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.cfg.start_timeout
        while loop.time() < deadline:
            if not self.is_running:
                log.error("server process exited during startup")
                self._log_tail()
                return False
            if self._seen_markers.issuperset(self.cfg.startup_markers):
                log.info("all startup markers seen; server healthy")
                return True
            await asyncio.sleep(2)
        missing = set(self.cfg.startup_markers) - self._seen_markers
        log.error("timed out waiting for startup markers: %s", sorted(missing))
        self._log_tail()
        return False

    async def restart(self) -> bool:
        """Stop then start, returning the health result of the new instance."""
        await self.stop()
        await self.start()
        return await self.wait_healthy()

    def _log_tail(self, n: int = 25):
        tail = list(self._buffer)[-n:]
        if tail:
            log.error("last %d server log lines:\n%s", len(tail), "\n".join(tail))
