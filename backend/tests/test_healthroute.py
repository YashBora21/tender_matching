"""The liveness probe answers at the root as well as under `/api`."""

from __future__ import annotations

from app.api.routes import root_router
from app.main import create_app


def test_the_health_alias_is_a_get_at_the_root():
    """`/health` reaches the same handler as `/api/health`, at the root a
    container healthcheck or load balancer actually probes."""
    routes = [r for r in root_router.routes if r.path == "/health"]
    assert len(routes) == 1
    assert "GET" in routes[0].methods
    assert routes[0].include_in_schema is False


def test_the_alias_is_registered_but_not_the_documented_path():
    """The OpenAPI schema keeps a single health endpoint; the alias is added
    without advertising a second one."""
    paths = create_app().openapi()["paths"]
    documented = [p for p in paths if p.endswith("/health")]
    assert documented == ["/api/health"]
