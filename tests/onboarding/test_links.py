from easytop.onboarding.links import TTL_SECONDS, ConnectLinks


def test_a_link_token_names_its_parent_once():
    links = ConnectLinks()
    token = links.issue("telegram:1", now=1000.0)
    assert len(token) >= 32
    assert links.consume(token, now=1001.0) == "telegram:1"
    assert links.consume(token, now=1002.0) is None  # single use


def test_peek_does_not_use_the_token_up():
    links = ConnectLinks()
    token = links.issue("telegram:1", now=0.0)
    assert links.peek(token, now=1.0) == "telegram:1"
    assert links.peek(token, now=2.0) == "telegram:1"
    assert links.consume(token, now=3.0) == "telegram:1"


def test_a_link_expires():
    links = ConnectLinks()
    token = links.issue("telegram:1", now=0.0)
    assert links.peek(token, now=TTL_SECONDS + 1) is None
    assert links.consume(token, now=TTL_SECONDS + 1) is None


def test_a_new_link_replaces_the_parents_old_one():
    links = ConnectLinks()
    old = links.issue("telegram:1", now=0.0)
    new = links.issue("telegram:1", now=1.0)
    assert old != new
    assert links.peek(old, now=2.0) is None
    assert links.peek(new, now=2.0) == "telegram:1"


def test_unknown_and_malformed_tokens_are_refused():
    links = ConnectLinks()
    links.issue("telegram:1", now=0.0)
    for bad in ("", "nope", "x" * 5000, None):
        assert links.peek(bad, now=1.0) is None
        assert links.consume(bad, now=1.0) is None


def test_parents_do_not_see_each_other():
    links = ConnectLinks()
    a = links.issue("telegram:1", now=0.0)
    b = links.issue("telegram:2", now=0.0)
    assert links.consume(a, now=1.0) == "telegram:1"
    assert links.consume(b, now=1.0) == "telegram:2"


def test_expired_links_are_forgotten_so_memory_does_not_grow():
    links = ConnectLinks()
    for i in range(50):
        links.issue(f"telegram:{i}", now=0.0)
    links.issue("telegram:new", now=TTL_SECONDS + 5)
    assert len(links) == 1
