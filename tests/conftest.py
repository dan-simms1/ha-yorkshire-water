"""Shared fixtures for the Yorkshire Water integration tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pyyorkshirewater import (
    CurrentConsumption,
    Customer,
    DailyConsumptionPoint,
    MeterDetails,
    Property,
    YearlyConsumptionPoint,
)

SAMPLE_COOKIES: dict[str, str] = {
    "idsrv": "fake-idsrv-cookie",
    "idsrv.session": "fake-session-cookie",
}

SAMPLE_CREDENTIALS: dict[str, str] = {
    "email": "test@example.com",
    "password": "fake-password",
    "playwright_url": "http://localhost:3001/",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the custom_components/yorkshire_water folder."""
    return


@pytest.fixture(autouse=True)
def mock_bridge_login() -> Generator[MagicMock]:
    """Replace the real bridge_login HTTP call with a stub returning cookies.

    Use a real `async def` with the exact signature of `bridge_login`
    rather than `unittest.mock.AsyncMock`. AsyncMock accepts any
    arguments without complaint, which lets a signature drift go
    undetected. A real `async def` mirrors the signature so a
    positional/keyword mismatch raises `TypeError` at test time.

    The real `bridge_login` accepts an optional `http_client=` kwarg
    that the coordinator passes; the stub mirrors that.
    """
    calls: list[tuple[str, str, str]] = []

    async def fake_bridge_login(
        bridge_url: str,
        email: str,
        password: str,
        *,
        http_client: object | None = None,
    ) -> dict[str, str]:
        calls.append((bridge_url, email, password))
        return dict(SAMPLE_COOKIES)

    handle = MagicMock(side_effect=fake_bridge_login)
    handle.coro = fake_bridge_login
    handle.calls = calls

    async def fake_healthcheck(*args: object, **kwargs: object) -> None:
        # Always succeeds: the config flow uses this in place of a
        # full login. Tests that want to assert config-flow failure
        # paths should patch this directly.
        return None

    with (
        patch(
            "custom_components.yorkshire_water.coordinator.bridge_login",
            new=fake_bridge_login,
        ),
        patch(
            "custom_components.yorkshire_water.config_flow.bridge_healthcheck",
            new=fake_healthcheck,
        ),
    ):
        yield handle


def _customer() -> Customer:
    return Customer.from_api(
        {
            "title": "Mr",
            "forename": "Test",
            "surname": "User",
            "email": "test@example.com",
            "mobileTelephone": "07700900000",
        },
    )


def _property(
    *,
    display_account_reference: str = "1234567890123456",
    account_reference: str = "FAKE_ACC_REF_TOKEN",
    address_line_1: str = "Example Street",
) -> Property:
    return Property.from_api(
        {
            "accountReference": account_reference,
            "displayAccountReference": display_account_reference,
            "accountStatus": "Live",
            "address": {
                "houseName": "",
                "houseNumber": "1",
                "addressLine1": address_line_1,
                "addressLine2": "Sometown",
                "addressLine3": "Anywhere",
                "addressLine4": "",
                "postcode": "EX1 1EX",
            },
        },
    )


def _meter_details(*, with_meter: bool = True) -> MeterDetails:
    return MeterDetails.from_api(
        {
            "meterReference": "WAKE-001" if with_meter else "",
            "startDate": "2026-04-01",
            "endDate": "2027-04-01",
            "currentDate": "2026-05-06",
        },
    )


def _current_consumption(*, live: bool, alarm: bool = False) -> CurrentConsumption:
    return CurrentConsumption.from_api(
        {
            "isMeterBau": live,
            "currentContinuousFlowAlarmState": alarm,
            "currentContinuousFlowAlarmDetails": (
                [{"alarmStartDate": "2026-05-05T03:00:00Z"}] if alarm else []
            ),
        },
    )


def _daily_points() -> list[DailyConsumptionPoint]:
    return [
        DailyConsumptionPoint.from_api(
            {"date": "2026-05-04", "totalConsumptionLitres": 95.0, "totalCost": 0.30},
        ),
        DailyConsumptionPoint.from_api(
            {"date": "2026-05-05", "totalConsumptionLitres": 110.5, "totalCost": 0.36},
        ),
        DailyConsumptionPoint.from_api(
            {"date": "2026-05-06", "totalConsumptionLitres": 78.0, "totalCost": 0.25},
        ),
    ]


def _yearly_points() -> list[YearlyConsumptionPoint]:
    return [
        YearlyConsumptionPoint.from_api({"year": 2025, "totalLitres": 110000}),
    ]


def make_mock_client(
    *,
    meter: bool = True,
    live: bool = True,
    alarm: bool = False,
    properties: list[Property] | None = None,
) -> MagicMock:
    """Build a MagicMock standing in for `YorkshireWaterClient`."""
    client = MagicMock()
    client.login = AsyncMock(return_value=None)
    client.get_customer = AsyncMock(return_value=_customer())
    client.iter_properties = AsyncMock(
        return_value=properties if properties is not None else [_property()],
    )
    client.get_meter_details = AsyncMock(
        return_value=_meter_details(with_meter=meter),
    )
    client.get_current_consumption = AsyncMock(
        return_value=_current_consumption(live=live, alarm=alarm),
    )
    client.get_your_usage = AsyncMock(return_value=[])
    client.get_daily_consumption = AsyncMock(return_value=_daily_points())
    client.get_yearly_consumption = AsyncMock(return_value=_yearly_points())
    client.close = AsyncMock(return_value=None)
    client.cookies = dict(SAMPLE_COOKIES)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    return client


@pytest.fixture
def mock_client_live() -> Generator[MagicMock]:
    """Patch the library client used by the integration with a live mock."""
    client = make_mock_client(meter=True, live=True)
    with patch(
        "custom_components.yorkshire_water.coordinator.YorkshireWaterClient",
        return_value=client,
    ):
        yield client


@pytest.fixture
def mock_client_pending() -> Generator[MagicMock]:
    """Patch the library client with a meter that is registered but not live."""
    client = make_mock_client(meter=True, live=False)
    with patch(
        "custom_components.yorkshire_water.coordinator.YorkshireWaterClient",
        return_value=client,
    ):
        yield client


@pytest.fixture
def mock_client_no_meter() -> Generator[MagicMock]:
    """Patch the library client with an account that has no meter."""
    client = make_mock_client(meter=False, live=False)
    with patch(
        "custom_components.yorkshire_water.coordinator.YorkshireWaterClient",
        return_value=client,
    ):
        yield client
