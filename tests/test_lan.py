"""Tests for the "am I on the same network as the NAS?" gate.

Live sync consults this before every triggered sync, so the interesting cases
are the ones where it must say *no*: a laptop on cafe wifi, and any public
address that happens to answer.
"""

from nasync.system import lan

# A realistic /proc/net/route: default via the router, plus on-link routes for
# the wired and wireless subnets.
ROUTES = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
wlp3s0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0
wlp3s0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0
enp0s5\t0005A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0
"""

# Only a default route: everything is reached through a gateway.
ROUTES_AWAY = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
wlp3s0\t00000000\t0100A8AC\t0003\t0\t0\t600\t00000000\t0\t0\t0
"""


def test_same_subnet_matches_on_link_routes():
    assert lan.same_subnet("192.168.1.50", ROUTES) is True     # wireless subnet
    assert lan.same_subnet("192.168.5.10", ROUTES) is True     # wired subnet
    assert lan.same_subnet("192.168.9.10", ROUTES) is False    # neither
    print("✅ test_same_subnet_matches_on_link_routes passed")


def test_a_gateway_route_is_not_the_same_lan():
    """Reachable *through a router* is exactly what this must reject — that's
    the cafe-wifi case, where the NAS address may still be routable."""
    assert lan.same_subnet("192.168.1.50", ROUTES_AWAY) is False
    assert lan.same_subnet("8.8.8.8", ROUTES) is False
    print("✅ test_a_gateway_route_is_not_the_same_lan passed")


def test_default_route_never_counts_as_on_link():
    """A gateway-less default route would otherwise match the whole internet."""
    routes = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
              "tun0\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n")
    assert lan.same_subnet("8.8.8.8", routes) is False
    print("✅ test_default_route_never_counts_as_on_link passed")


def test_garbage_in_the_route_table_is_skipped():
    routes = ("Iface\tDestination\tGateway\n"
              "short\tline\n"
              "eth0\tZZZZZZZZ\t00000000\t0001\t0\t0\t0\tFFFFFF00\t0\t0\t0\n"
              "eth0\t0001A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n")
    assert lan.same_subnet("192.168.1.50", routes) is True
    print("✅ test_garbage_in_the_route_table_is_skipped passed")


def test_public_addresses_never_qualify(monkeypatch):
    """Even a public address that answers instantly is not 'home'."""
    monkeypatch.setattr(lan, "same_subnet", lambda ip, text=None: False)
    monkeypatch.setattr(lan, "reachable", lambda *a, **k: True)

    assert lan.on_lan("8.8.8.8") is False
    assert lan.on_lan("192.168.9.50") is True      # private + answering
    print("✅ test_public_addresses_never_qualify passed")


def test_unreachable_private_address_is_not_home(monkeypatch):
    monkeypatch.setattr(lan, "same_subnet", lambda ip, text=None: False)
    monkeypatch.setattr(lan, "reachable", lambda *a, **k: False)
    assert lan.on_lan("192.168.9.50") is False
    print("✅ test_unreachable_private_address_is_not_home passed")


def test_on_link_wins_without_probing(monkeypatch):
    """The routing check is free; it must short-circuit the network probe."""
    probed = []
    monkeypatch.setattr(lan, "same_subnet", lambda ip, text=None: True)
    monkeypatch.setattr(lan, "reachable", lambda *a, **k: probed.append(a) or True)

    assert lan.on_lan("192.168.1.50") is True
    assert probed == [], probed
    print("✅ test_on_link_wins_without_probing passed")


def test_unresolvable_host_is_not_home(monkeypatch):
    monkeypatch.setattr(lan, "resolve", lambda host: None)
    assert lan.on_lan("nas.invalid") is False
    print("✅ test_unresolvable_host_is_not_home passed")


def test_describe_explains_each_verdict(monkeypatch):
    monkeypatch.setattr(lan, "same_subnet", lambda ip, text=None: True)
    assert "same network" in lan.describe("192.168.1.50")

    monkeypatch.setattr(lan, "same_subnet", lambda ip, text=None: False)
    assert "public address" in lan.describe("8.8.8.8")

    monkeypatch.setattr(lan, "reachable", lambda *a, **k: False)
    assert "Not on the same network" in lan.describe("192.168.9.50")
    print("✅ test_describe_explains_each_verdict passed")
