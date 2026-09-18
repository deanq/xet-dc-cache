from runpod_testbed.provision.fleet import parse_external_addr

def test_parse_picks_public_tcp_8000():
    ports = [
        {"ip": "1.2.3.4", "isIpPublic": True, "privatePort": 8000, "publicPort": 41234, "type": "tcp"},
        {"ip": "10.0.0.1", "isIpPublic": False, "privatePort": 8888, "publicPort": 8888, "type": "http"},
    ]
    assert parse_external_addr(ports) == "http://1.2.3.4:41234"

def test_parse_none_when_not_ready():
    assert parse_external_addr([]) is None
    assert parse_external_addr([{"ip": "1.2.3.4", "isIpPublic": False,
                                 "privatePort": 8000, "publicPort": 41234, "type": "tcp"}]) is None
