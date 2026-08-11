"""The notification health endpoint.

The strongest assertion is negative: a webhook URL must not be reachable through
this endpoint in any form. A webhook is a bearer credential, and an ops endpoint
is the surface most likely to end up more exposed than intended.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import DiscordSettings, Environment, NotificationSettings, Settings
from app.core.events import Event
from app.main import create_app
from app.notifications.models import DeliveryResult, NotificationMessage
from app.notifications.service import NotificationService

ENDPOINT = "/health/notifications"
HTTP_OK = 200
HTTP_SERVICE_UNAVAILABLE = 503

HOOK = "https://discord.com/api/webhooks/111/secret-token-aaaa"


class StubBackend:
    name = "stub"

    def __init__(self, *, succeed: bool = True) -> None:
        self._succeed = succeed

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        return DeliveryResult(
            backend=self.name,
            delivered=self._succeed,
            error=None if self._succeed else f"stub refused: {HOOK}",
        )


def make_settings(**overrides: object) -> Settings:
    return Settings(
        env=Environment.TEST,
        database_url="sqlite+aiosqlite:///:memory:",
        log_level="WARNING",
        **overrides,  # type: ignore[arg-type]
    )


@pytest.fixture
async def notified_client(engine: object, request: pytest.FixtureRequest) -> AsyncClient:
    """A client whose app has a notification service attached.

    Built here rather than reusing the shared `client` fixture because these
    tests need to vary the notification configuration per test.
    """
    settings = getattr(request, "param", None) or make_settings()
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]

    app = create_app(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.notifications = NotificationService(
        settings, backends=[StubBackend()], session_factory=factory
    )

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def test_health_reports_configuration(notified_client: AsyncClient) -> None:
    response = await notified_client.get(ENDPOINT)

    assert response.status_code == HTTP_OK
    body = response.json()
    assert body["enabled"] is True
    assert body["backends"] == ["stub"]


async def test_health_never_exposes_a_webhook(engine: object) -> None:
    """Configured channels are reported by name; the URLs are not reachable."""
    settings = make_settings(
        discord=DiscordSettings(
            enabled=True,
            market_webhook=SecretStr(HOOK),
            system_webhook=SecretStr(HOOK),
        )
    )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    app = create_app(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.notifications = NotificationService(
        settings, backends=[StubBackend()], session_factory=factory
    )

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get(ENDPOINT)

    assert HOOK not in response.text
    assert "secret-token" not in response.text
    assert "discord.com" not in response.text
    assert set(response.json()["configured_categories"]) == {"market", "system"}


async def test_disabled_notifications_are_healthy_not_degraded(engine: object) -> None:
    """Running without Discord is a valid configuration, not a failure."""
    settings = make_settings(notifications=NotificationSettings(enabled=False))
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    app = create_app(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.notifications = NotificationService(settings, backends=[], session_factory=factory)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get(ENDPOINT)

    assert response.status_code == HTTP_OK
    assert response.json()["enabled"] is False


async def test_delivery_counts_are_reported(notified_client: AsyncClient) -> None:
    service: NotificationService = notified_client._transport.app.state.notifications  # type: ignore[union-attr]
    await service.publish(Event.lifecycle(started=True, environment="test", provider="mock"))

    body = (await notified_client.get(ENDPOINT)).json()

    assert body["delivered_count"] == 1
    assert body["last_success"] is not None


async def test_a_recent_failure_reports_unavailable(engine: object) -> None:
    settings = make_settings()
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    app = create_app(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = factory
    service = NotificationService(
        settings, backends=[StubBackend(succeed=False)], session_factory=factory
    )
    app.state.notifications = service
    await service.publish(Event.lifecycle(started=True, environment="test", provider="mock"))

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get(ENDPOINT)

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    body = response.json()
    assert body["failed_count"] == 1
    # The stub's error deliberately contains a webhook URL. It must not survive.
    assert "secret-token" not in response.text
