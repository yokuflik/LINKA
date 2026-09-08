from services import rate_limit_service


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeConn:
    """Stands in for a Starlette Request / WebSocket (only .client + .headers)."""

    def __init__(self, peer, headers=None):
        self.client = _FakeClient(peer) if peer else None
        self.headers = headers or {}


def test_ignores_forged_xff_from_an_untrusted_peer():
    conn = _FakeConn("203.0.113.9", {"x-forwarded-for": "1.2.3.4"})
    assert rate_limit_service.client_ip(conn) == "203.0.113.9"


def test_trusts_xff_first_hop_from_the_docker_bridge():
    conn = _FakeConn("172.18.0.5", {"x-forwarded-for": "1.2.3.4, 172.18.0.5"})
    assert rate_limit_service.client_ip(conn) == "1.2.3.4"


def test_falls_back_to_peer_when_no_xff():
    conn = _FakeConn("172.18.0.5")
    assert rate_limit_service.client_ip(conn) == "172.18.0.5"


def test_unknown_when_no_peer():
    assert rate_limit_service.client_ip(_FakeConn(None)) == "unknown"
