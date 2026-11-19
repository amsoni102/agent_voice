"""Self-signed HTTPS certificate so phone browsers can use the microphone on the LAN."""

from __future__ import annotations

import ipaddress
import logging
import shutil
import socket
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def local_ips() -> list[str]:
    """IPv4 addresses of this machine that other devices can reach (LAN, Tailscale)."""
    ips: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout
        ips.update(out.split())
    except (OSError, subprocess.SubprocessError):
        pass
    result = []
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.version == 4 and not addr.is_loopback:
            result.append(ip)
    return sorted(result)


def ensure_self_signed_cert(directory: Path) -> tuple[Path, Path]:
    """Create (or refresh when the machine's IPs change) a self-signed cert for this host."""
    if shutil.which("openssl") is None:
        raise RuntimeError("openssl is required for HTTPS (sudo apt install openssl)")
    directory.mkdir(parents=True, exist_ok=True)
    cert, key, stamp = directory / "cert.pem", directory / "key.pem", directory / "cert.hosts"

    hosts = ["localhost", socket.gethostname()]
    ips = ["127.0.0.1", *local_ips()]
    wanted = ",".join(hosts + ips)
    if cert.is_file() and key.is_file() and stamp.is_file() and stamp.read_text().strip() == wanted:
        return cert, key

    san = ",".join([f"DNS:{h}" for h in hosts] + [f"IP:{ip}" for ip in ips])
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "825",
            "-keyout", str(key), "-out", str(cert), "-subj", "/CN=AgentVoice local",
            "-addext", f"subjectAltName={san}",
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    stamp.write_text(wanted)
    log.info("generated self-signed HTTPS certificate for %s", wanted)
    return cert, key
