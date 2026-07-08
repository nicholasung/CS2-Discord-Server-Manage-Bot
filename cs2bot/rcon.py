"""Minimal Source RCON client (works with CS2)."""

import re
import socket
import struct

SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0


class RconError(Exception):
    pass


class RconClient:
    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self._id = 0
        self._auth(password)

    def _send(self, ptype: int, body: str) -> int:
        self._id += 1
        payload = struct.pack("<ii", self._id, ptype) + body.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(payload)) + payload)
        return self._id

    def _recv_packet(self):
        raw = b""
        while len(raw) < 4:
            chunk = self.sock.recv(4 - len(raw))
            if not chunk:
                raise RconError("connection closed by server")
            raw += chunk
        (length,) = struct.unpack("<i", raw)
        data = b""
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise RconError("connection closed by server")
            data += chunk
        pkt_id, ptype = struct.unpack("<ii", data[:8])
        body = data[8:-2].decode("utf-8", errors="replace")
        return pkt_id, ptype, body

    def _auth(self, password: str):
        self._send(SERVERDATA_AUTH, password)
        while True:
            pkt_id, ptype, _ = self._recv_packet()
            if ptype == SERVERDATA_AUTH_RESPONSE:
                if pkt_id == -1:
                    raise RconError("RCON authentication failed (bad password)")
                return

    def exec(self, command: str) -> str:
        req_id = self._send(SERVERDATA_EXECCOMMAND, command)
        pkt_id, _, body = self._recv_packet()
        if pkt_id != req_id:
            raise RconError("unexpected RCON response id")
        return body

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def rcon_exec(cfg, *commands: str) -> str:
    """Open a connection, run commands in order, return combined output."""
    client = RconClient(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password)
    try:
        return "\n".join(client.exec(c) for c in commands)
    finally:
        client.close()


_PLAYERS_RE = re.compile(r"players\s*:\s*(\d+)\s+humans?", re.IGNORECASE)


def player_count(cfg) -> int:
    """Number of human players currently connected, parsed from the
    `status` command's "players : N humans, M bots (K max)" line. Raises
    RconError if the server can't be reached or the line isn't found."""
    output = rcon_exec(cfg, "status")
    m = _PLAYERS_RE.search(output)
    if not m:
        raise RconError("could not parse player count from `status` output")
    return int(m.group(1))
