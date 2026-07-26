import socket
import time
from dataclasses import dataclass

@dataclass
class RelayResult:
    ok: bool
    response: str = ""
    elapsed_ms: int = 0
    error: str = ""

class HHCRelayClient:
    """Client TCP per HHC-NET2D.

    Protocollo ricavato da manuale e cattura reale:
    - read1 / read2 -> risposta on1/off1 oppure on2/off2, con eventuali \x00 finali
    - on1:00 / on2:00 -> apertura/attivazione relè
    - off1 / off2 -> chiusura/disattivazione relè
    """

    def __init__(self, ip: str, port: int, timeout: float = 2.0):
        self.ip = ip
        self.port = int(port)
        self.timeout = float(timeout)

    def send(self, command: str) -> RelayResult:
        start = time.perf_counter()
        try:
            with socket.create_connection((self.ip, self.port), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(command.encode("ascii"))
                try:
                    data = sock.recv(64)
                except socket.timeout:
                    data = b""
            elapsed = int((time.perf_counter() - start) * 1000)
            response = data.replace(b"\x00", b"").decode("ascii", errors="ignore").strip()
            return RelayResult(ok=True, response=response, elapsed_ms=elapsed)
        except Exception as exc:
            elapsed = int((time.perf_counter() - start) * 1000)
            return RelayResult(ok=False, elapsed_ms=elapsed, error=str(exc))

    def read_relay(self, relay_number: str) -> RelayResult:
        return self.send(f"read{relay_number}")

    def set_relay(self, relay_number: str, active: bool) -> RelayResult:
        if active:
            return self.send(f"on{relay_number}:00")
        return self.send(f"off{relay_number}")
