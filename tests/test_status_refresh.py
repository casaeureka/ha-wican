"""The coordinator asks the device for its status: what a restarted device has not pushed yet is still current."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wican.const import DOMAIN
from custom_components.wican.coordinator import STATUS_KEYS, WiCANDataUpdateCoordinator

from .conftest import TEST_DEVICE_ID

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.asks_status

ADDRESS = "http://192.0.2.31"

#: What a WiCAN PRO answers on /check_status, credentials included (v4.51p, abridged).
STATUS = {
    "device_id": TEST_DEVICE_ID,
    "fw_version": "4.51",
    "git_version": "v4.51p",
    "hw_version": "WiCAN-OBD-PRO",
    "batt_voltage": "12.8V",
    "uptime": "00:00:06",
    "wifi_mode": "APStation",
    "sta_ssid": "household",
    "sta_pass": "the household's Wi-Fi password",
    "home_password": "TomatoSauce",
    "batt_alert_pass": "TomatoSauce",
    "mqtt_pass": "meatpi",
    "sta_ip": "192.0.2.31",
}


def _entry(hass: HomeAssistant, **data: str) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN",
        unique_id=TEST_DEVICE_ID,
        data={
            CONF_WEBHOOK_ID: uuid4().hex,
            "mdns": ADDRESS,
            "device_id": TEST_DEVICE_ID,
            "fw_version": "4.50",
            "git_version": "v4.50p",
            **data,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_the_status_is_asked_and_its_credentials_are_not_kept(hass: HomeAssistant, aioclient_mock) -> None:
    """The device's status comes back on each refresh; its passwords and network identity never reach Home Assistant."""
    aioclient_mock.get(f"{ADDRESS}/check_status", json=STATUS)
    coordinator = WiCANDataUpdateCoordinator(hass, _entry(hass))

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    status = coordinator.data["status"]
    assert status["fw_version"] == "4.51"
    assert status["batt_voltage"] == "12.8V"
    assert set(status) <= STATUS_KEYS
    for secret in ("sta_ssid", "sta_pass", "home_password", "batt_alert_pass", "mqtt_pass", "sta_ip"):
        assert secret not in status


async def test_what_the_webhook_pushed_is_kept_beside_the_status(hass: HomeAssistant, aioclient_mock) -> None:
    """A refresh changes the status keys it read and nothing the webhook pushed."""
    aioclient_mock.get(f"{ADDRESS}/check_status", json=STATUS)
    coordinator = WiCANDataUpdateCoordinator(hass, _entry(hass))
    coordinator.handle_webhook_data({"status": {"ecu_status": "online"}, "autopid_data": {"0C-EngineRPM": 800}})

    await coordinator.async_refresh()

    assert coordinator.data["autopid_data"] == {"0C-EngineRPM": 800}
    assert coordinator.data["status"]["ecu_status"] == "online"
    assert coordinator.data["status"]["uptime"] == "00:00:06"


async def test_a_device_that_does_not_answer_is_a_failed_refresh(hass: HomeAssistant, aioclient_mock) -> None:
    """Asleep with its vehicle or away: the refresh fails, so its status entities say unavailable."""
    aioclient_mock.get(f"{ADDRESS}/check_status", exc=TimeoutError())
    coordinator = WiCANDataUpdateCoordinator(hass, _entry(hass))

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False


async def test_a_firmware_update_reaches_the_entry_and_the_device(hass: HomeAssistant, aioclient_mock) -> None:
    """After an update the device restarts with nothing to push: the refresh records its new firmware."""
    aioclient_mock.get(f"{ADDRESS}/check_status", json=STATUS)
    entry = _entry(hass)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, TEST_DEVICE_ID)},
        sw_version="4.50",
    )
    coordinator = WiCANDataUpdateCoordinator(hass, entry)

    await coordinator.async_refresh()

    assert entry.data["fw_version"] == "4.51"
    assert entry.data["git_version"] == "v4.51p"
    assert devices.async_get(device.id).sw_version == "4.51"


async def test_the_same_firmware_changes_nothing(hass: HomeAssistant, aioclient_mock) -> None:
    """A refresh on an unchanged device leaves the entry alone (an entry change re-registers the webhook)."""
    aioclient_mock.get(f"{ADDRESS}/check_status", json=STATUS)
    entry = _entry(hass, fw_version="4.51", git_version="v4.51p")
    data = dict(entry.data)
    coordinator = WiCANDataUpdateCoordinator(hass, entry)

    await coordinator.async_refresh()

    assert dict(entry.data) == data
