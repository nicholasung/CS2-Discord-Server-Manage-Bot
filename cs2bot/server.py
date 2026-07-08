"""Supervises the CS2 dedicated server as a child process.

The bot owns the server's lifecycle: it launches your existing start_cs2.sh,
keeps the process in its own session so the whole tree can be signalled, and
drains the server's stdout into a bounded in-memory ring buffer. Nothing is
ever written to disk — the buffer is capped at `log_buffer_lines` and old
lines fall off the end, so it cannot grow unbounded.

Startup health is judged from that same buffer: a start is "healthy" once
every configured startup marker has appeared. If the process exits early or
the markers never show up within the timeout, the start is unhealthy.
"""

import asyncio
import collections
import logging
import os
import signal

log = logging.getLogger("cs2bot.server")


class ServerManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._buffer = collections.deque(maxlen=cfg.log_buffer_lines)
        self._seen_markers: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _drain_stdout(self):
        assert self._proc and self._proc.stdout
        try:
            async for raw in self._proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                self._buffer.append(line)
                for marker in self.cfg.startup_markers:
                    if marker in line:
                        self._seen_markers.add(marker)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            log.warning("stdout reader stopped: %s", e)

    async def start(self):
        """Launch the server. Does nothing if already running."""
        async with self._lock:
            if self.is_running:
                log.info("start requested but server already running")
                return
            self._buffer.clear()
            self._seen_markers.clear()
            log.info("launching CS2 via %s", self.cfg.launch_script)
            self._proc = await asyncio.create_subprocess_exec(
                # Run through bash explicitly rather than exec'ing the script
                # path directly: a direct execve() requires a valid shebang
                # line with Unix line endings, and fails with ENOEXEC if the
                # script has no shebang or was saved with CRLF endings.
                "/bin/bash", self.cfg.launch_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.cfg.launch_cwd),
                start_new_session=True,  # own process group, so we can kill the tree
            )
            self._reader = asyncio.create_task(self._drain_stdout())

    async def stop(self):
        """Stop the server, escalating SIGTERM -> SIGKILL on the whole group."""
        async with self._lock:
            if not self.is_running:
                return
            pid = self._proc.pid
            try:
                pgid = os.getpgid(pid)
            except ProcessLookupError:
                pgid = None

            log.info("stopping CS2 (pid %s)", pid)
            self._signal_group(pgid, pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=self.cfg.stop_timeout)
            except asyncio.TimeoutError:
                log.warning("SIGTERM timed out; sending SIGKILL")
                self._signal_group(pgid, pid, signal.SIGKILL)
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=15)
                except asyncio.TimeoutError:
                    log.error("server did not exit after SIGKILL")

            if self._reader:
                self._reader.cancel()
                try:
                    await self._reader
                except asyncio.CancelledError:
                    pass
                self._reader = None

    @staticmethod
    def _signal_group(pgid, pid, sig):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            pass

    async def wait_healthy(self) -> bool:
        """Wait until all startup markers appear, or the process dies, or we
        time out. Returns True only if the server came up cleanly."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.cfg.start_timeout
        while loop.time() < deadline:
            if not self.is_running:
                log.error("server process exited during startup (code %s)",
                          self._proc.returncode if self._proc else "?")
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
