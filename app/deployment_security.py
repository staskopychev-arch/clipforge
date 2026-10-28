"""Small single-web-process guards for the private admin panel."""
from collections import deque
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit


def same_origin(origin: str, public_url: str) -> bool:
    def normalize(value):
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        return bool(normalize(origin) and normalize(origin) == normalize(public_url))
    except ValueError:
        return False


class LoginLimiter:
    """Bounded memory, one web replica. Does not trust forwarded client headers."""
    def __init__(self, limit=10, seconds=60, max_clients=1000):
        self.limit, self.seconds, self.max_clients = limit, seconds, max_clients
        self.clients = {}
        self.lock = Lock()

    def allow(self, key: str) -> bool:
        now = monotonic()
        with self.lock:
            for client in list(self.clients):
                events = self.clients[client]
                while events and events[0] <= now - self.seconds:
                    events.popleft()
                if not events:
                    del self.clients[client]
            if key not in self.clients:
                if len(self.clients) >= self.max_clients:
                    return False
                self.clients[key] = deque()
            events = self.clients[key]
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True
