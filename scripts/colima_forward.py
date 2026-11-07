"""Repair a Colima localhost forward through its existing OpenSSH mux socket.

The protocol is documented in https://github.com/openssh/openssh-portable/blob/master/PROTOCOL.mux.
This avoids launching a
new ssh client when macOS temporarily cannot resolve the current user account.
"""

import argparse
import os
import re
import socket
import struct
from pathlib import Path

HELLO = 0x00000001
OPEN_FORWARD = 0x10000006
CLOSE_FORWARD = 0x10000007
REPLY_OK = 0x80000001
LOCAL_FORWARD = 1
PROTOCOL_VERSION = 4


def read_exact(connection: socket.socket, size: int) -> bytes:
    """Read one complete mux field."""
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Colima SSH control socket closed unexpectedly")
        data.extend(chunk)
    return bytes(data)


def exchange(connection: socket.socket, payload: bytes) -> tuple[int, bytes]:
    """Send a length-prefixed mux packet and read its response."""
    connection.sendall(struct.pack(">I", len(payload)) + payload)
    length = struct.unpack(">I", read_exact(connection, 4))[0]
    response = read_exact(connection, length)
    return struct.unpack(">I", response[:4])[0], response[4:]


def ssh_string(value: str) -> bytes:
    """Encode an SSH protocol string."""
    encoded = value.encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded


def marker_path(port: int) -> Path:
    """Identify only forwards opened by this project's Docker target."""
    root = Path(__file__).resolve().parent.parent
    project = os.environ.get("COMPOSE_PROJECT_NAME", root.name)
    safe_project = re.sub(r"[^a-zA-Z0-9_-]", "_", project)
    return root / ".runtime" / f"colima-forward-{safe_project}-{port}"


def change_forward(action: str, port: int) -> None:
    """Open or close a localhost port forward without touching other forwards."""
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    marker = marker_path(port)
    if action == "close" and not marker.exists():
        return
    control = Path.home() / ".colima" / "_lima" / "colima" / "ssh.sock"
    if not control.is_socket():
        if action == "close":
            marker.unlink(missing_ok=True)
            return
        raise FileNotFoundError(f"Colima SSH control socket is unavailable: {control}")

    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(control))
        hello_type, hello_body = exchange(connection, struct.pack(">II", HELLO, PROTOCOL_VERSION))
        if hello_type != HELLO or struct.unpack(">I", hello_body[:4])[0] != PROTOCOL_VERSION:
            raise RuntimeError("Unsupported Colima SSH mux protocol")
        command = OPEN_FORWARD if action == "open" else CLOSE_FORWARD
        request = (
            struct.pack(">III", command, 1, LOCAL_FORWARD)
            + ssh_string("127.0.0.1")
            + struct.pack(">I", port)
            + ssh_string("127.0.0.1")
            + struct.pack(">I", port)
        )
        response_type, response_body = exchange(connection, request)
        if len(response_body) < 4 or struct.unpack(">I", response_body[:4])[0] != 1:
            raise RuntimeError("Colima SSH mux returned an invalid response")
        if response_type != REPLY_OK:
            if action == "close":
                marker.unlink(missing_ok=True)
                return
            raise RuntimeError(f"Colima SSH mux refused port {port}: {response_body[4:]!r}")

    if action == "open":
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("localhost forward opened by docker-up\n", encoding="utf-8")
        print(f"Restored Colima localhost forwarding for port {port}")
    else:
        marker.unlink(missing_ok=True)
        print(f"Closed this project's Colima localhost forward for port {port}")


def main() -> None:
    """Parse the requested forward action."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("open", "close"))
    parser.add_argument("port", type=int)
    args = parser.parse_args()
    change_forward(args.action, args.port)


if __name__ == "__main__":
    main()
