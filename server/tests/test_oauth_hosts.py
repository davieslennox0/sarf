"""OAuth discovery after a domain move: the canonical MCP host describes
itself, and a legacy MCP host that still serves old connectors describes
ITSELF too, because a client must reject metadata naming another resource
(RFC 9728 s3.3) and would fail its next re-auth."""

from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sarf import oauth
from sarf.db import Database


def _client(monkeypatch):
    s = replace(oauth.settings, public_url="https://getsarf.xyz",
                mcp_public_url="https://mcp.getsarf.xyz",
                legacy_mcp_hosts=("sarf-mcp.managerx.xyz",))
    monkeypatch.setattr(oauth, "settings", s)
    app = FastAPI()
    app.include_router(oauth.build_oauth(Database(":memory:")))
    return TestClient(app)


def test_canonical_host_describes_itself(monkeypatch):
    c = _client(monkeypatch)
    m = c.get("/.well-known/oauth-protected-resource", headers={"host": "mcp.getsarf.xyz"}).json()
    assert m["resource"] == "https://mcp.getsarf.xyz/mcp"
    assert m["authorization_servers"] == ["https://getsarf.xyz"]


def test_legacy_host_describes_itself_with_the_new_issuer(monkeypatch):
    c = _client(monkeypatch)
    m = c.get("/.well-known/oauth-protected-resource/mcp",
              headers={"host": "sarf-mcp.managerx.xyz"}).json()
    assert m["resource"] == "https://sarf-mcp.managerx.xyz/mcp"
    assert m["authorization_servers"] == ["https://getsarf.xyz"]
    assert "sarf-mcp.managerx.xyz" in oauth.www_authenticate("sarf-mcp.managerx.xyz:443")


def test_unknown_host_gets_the_canonical_answer(monkeypatch):
    c = _client(monkeypatch)
    m = c.get("/.well-known/oauth-protected-resource", headers={"host": "evil.example"}).json()
    assert m["resource"] == "https://mcp.getsarf.xyz/mcp"
    assert "mcp.getsarf.xyz" in oauth.www_authenticate("evil.example")
