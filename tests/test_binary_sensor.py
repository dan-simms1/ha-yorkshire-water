"""Binary sensor tests for the Yorkshire Water integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.yorkshire_water.const import DOMAIN

from .conftest import SAMPLE_CREDENTIALS


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Yorkshire Water (test)",
        data=dict(SAMPLE_CREDENTIALS),
        unique_id="yw-test",
        options={},
        version=3,
        minor_version=0,
    )
    entry.add_to_hass(hass)
    return entry


async def test_alarm_off_when_no_active_alarm(
    hass: HomeAssistant,
    mock_client_live: MagicMock,
) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(
        "binary_sensor.1_example_street_sometown_anywhere_ex1_1ex_continuous_flow_alarm",
    )
    assert state is not None
    assert state.state == STATE_OFF

    meter_active = hass.states.get(
        "binary_sensor.1_example_street_sometown_anywhere_ex1_1ex_meter_active",
    )
    assert meter_active is not None
    assert meter_active.state == STATE_ON


async def test_alarm_on_when_alarm_state_true(hass: HomeAssistant) -> None:
    """The alarm sensor flips to ON and exposes the alarm details attribute."""
    from .conftest import make_mock_client

    client = make_mock_client(meter=True, live=True, alarm=True)
    with patch(
        "custom_components.yorkshire_water.coordinator.YorkshireWaterClient",
        return_value=client,
    ):
        entry = _entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(
        "binary_sensor.1_example_street_sometown_anywhere_ex1_1ex_continuous_flow_alarm",
    )
    assert state is not None
    assert state.state == STATE_ON
    details = state.attributes.get("alarm_details")
    assert isinstance(details, list)
    assert len(details) >= 1
    assert details[0].get("alarmStartDate")
