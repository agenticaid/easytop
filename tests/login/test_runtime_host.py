from easytop.login.runtime import LoginRuntime
from easytop.onboarding.links import ConnectLinks


async def _noop(parent_id, cookies): ...


def test_runtime_keeps_the_host_it_was_given():
    rt = LoginRuntime(ConnectLinks(), _noop, tunnel=None, host="0.0.0.0", port=0)
    assert rt.host == "0.0.0.0"


def test_runtime_defaults_to_localhost():
    rt = LoginRuntime(ConnectLinks(), _noop, tunnel=None, port=0)
    assert rt.host == "127.0.0.1"
