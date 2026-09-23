from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _env_values(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


def test_authenticated_deployment_is_separate_from_public_backend() -> None:
    compose = (ROOT / "compose.authenticated.yaml").read_text(encoding="utf-8")
    assert 'UPSTREAM_PORT: "8768"' in compose
    assert "authenticated/gateway.env" in compose
    assert "authenticated/tunnel.env" in compose
    assert 'UPSTREAM_PORT: "8767"' not in compose


def test_authenticated_gateway_allowlist_is_explicit_and_minimal() -> None:
    env = _env_values(ROOT / "authenticated" / "gateway.env.example")
    tools = set(env["ALLOWED_TOOLS"].split(","))

    required = {
        "connect_avanza",
        "disconnect_avanza",
        "get_auth_status",
        "get_accounts",
        "get_holdings",
        "get_transactions",
        "get_portfolio_insights",
        "get_active_orders",
        "get_deals",
        "get_stop_loss_orders",
        "get_watchlists",
        "get_price_alerts",
        "get_instrument_news",
        "get_insider_transactions",
    }
    assert required <= tools
    assert {"get_credit_info", "get_current_offers", "get_forum_posts"}.isdisjoint(tools)


def test_authenticated_secret_files_are_gitignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "authenticated/gateway.env" in ignored
    assert "authenticated/tunnel.env" in ignored
