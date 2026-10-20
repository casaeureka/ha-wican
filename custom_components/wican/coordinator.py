"""DataUpdateCoordinator for WiCAN integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientTimeout
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .attributes import BINARY_SENSOR_DESCRIPTIONS, SENSOR_DESCRIPTIONS
from .const import DOMAIN, STATUS_PATH, STATUS_TIMEOUT, WICAN_DATA_UPDATE_INTERVAL
from .helpers import device_url

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import WiCANConfigEntry

_LOGGER = logging.getLogger(__name__)

# The device pushes through its webhook; its status is asked for this often.
UPDATE_INTERVAL = timedelta(seconds=WICAN_DATA_UPDATE_INTERVAL)

_STATUS_DESCRIPTIONS = (*SENSOR_DESCRIPTIONS, *BINARY_SENSOR_DESCRIPTIONS)

#: What is kept of the status the device reports when asked (``/check_status``):
#: the keys the entities and the device record read. That answer also carries
#: the device's Wi-Fi, hotspot and MQTT credentials (the firmware leaves them
#: out of its webhook's copy only), and none of them is kept.
STATUS_KEYS: frozenset[str] = frozenset(
    {"device_id", "fw_version", "hw_version", "git_version"}
    | {description.key for description in _STATUS_DESCRIPTIONS}
    | {key for description in _STATUS_DESCRIPTIONS for key in description.extra_attributes or ()},
)

#: The firmware the device runs, as the entry and its device record keep it.
_FIRMWARE_KEYS = ("fw_version", "git_version")


class WiCANDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching WiCAN data."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: WiCANConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        self.config_entry = config_entry
        self._data: dict[str, Any] = {}

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            config_entry=config_entry,
        )
        # What the device pushed and what it answered, kept across a refresh
        # that fails (it sleeps with its vehicle): never None, so an entity
        # added before the first refresh succeeds reads an empty dict.
        self.data = self._data

    async def _async_update_data(self) -> dict[str, Any]:
        """Ask the device for its status; what it reads from the vehicle arrives through the webhook.

        The device posts only once it has read the vehicle, so after a
        restart with the engine off (a firmware update, a settings change) it
        posts nothing until the next drive: without asking, its firmware
        version, battery and uptime stayed what the last post said.

        Raises:
            UpdateFailed: The device does not answer (asleep with its vehicle,
                or away), or answers with something that is not a status.
            ConfigEntryError: Another device answers at the entry's address.
        """
        url = device_url(self.config_entry.data)
        if url is None:
            raise UpdateFailed(f"the WiCAN entry {self.config_entry.title} has no address to ask")
        status_url = f"{url.rstrip('/')}{STATUS_PATH}"
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(status_url, timeout=ClientTimeout(total=STATUS_TIMEOUT)) as response:
                response.raise_for_status()
                status = await response.json(content_type=None)
        except (TimeoutError, ClientError, ValueError) as err:
            raise UpdateFailed(f"the WiCAN at {url} does not answer (asleep with its vehicle, or away): {err}") from err
        if not isinstance(status, dict):
            raise UpdateFailed(f"the WiCAN at {url} answered {STATUS_PATH} with something that is not an object")
        self._validate_device_identity({"status": status})
        kept = {key: value for key, value in status.items() if key in STATUS_KEYS}
        self._data["status"] = {**self._data.get("status", {}), **kept}
        self._record_firmware(kept)
        return self._data

    def _record_firmware(self, status: dict[str, Any]) -> None:
        """Keep the entry and its device record on the firmware the device reports, once it changed (an update)."""
        entry = self.config_entry
        changed = {key: status[key] for key in _FIRMWARE_KEYS if status.get(key) and status[key] != entry.data.get(key)}
        if not changed:
            return
        self.hass.config_entries.async_update_entry(entry, data={**entry.data, **changed})
        device_id = entry.data.get("device_id") or entry.entry_id
        devices = dr.async_get(self.hass)
        device = devices.async_get_device(identifiers={(DOMAIN, device_id)})
        if device is not None and "fw_version" in changed:
            devices.async_update_device(device.id, sw_version=changed["fw_version"])

    async def async_config_entry_first_refresh(self) -> None:
        """Perform first refresh of the coordinator.

        For WiCAN, this is a push-based integration, so we don't poll for data.
        This method initializes the coordinator with empty data and succeeds immediately.
        Entities will be created and will update when the first webhook push arrives.
        """
        _LOGGER.debug(
            "First refresh for WiCAN coordinator (push-based, no polling required)",
        )
        # Initialize with empty data - webhook pushes will populate it
        await self.async_refresh()

    def handle_webhook_data(self, data: dict[str, Any]) -> None:
        """Handle incoming webhook data.

        This is called by the webhook handler when new data arrives.
        It updates the coordinator's data and notifies all listeners.
        """
        # Validate device identity before processing data
        self._validate_device_identity(data)

        # Update internal data store
        self._data.update(data)

        # Notify all entities that data has been updated
        self.async_set_updated_data(self._data)

    def _validate_device_identity(self, data: dict[str, Any]) -> None:
        """Ensure device identity hasn't changed.

        Validates that the device_id in the webhook data matches the stored
        device_id from initial configuration. This prevents a different device
        from impersonating the configured device.

        Raises:
            ConfigEntryError: If device_id mismatch is detected.
        """
        # Extract device_id from webhook data (can be in status dict or top-level)
        status = data.get("status", {})
        incoming_device_id = status.get("device_id") or data.get("device_id")

        if not incoming_device_id:
            # No device_id provided - skip validation
            # This maintains backward compatibility with older firmware
            return

        # Get stored device_id from config entry
        stored_device_id = self.config_entry.data.get("device_id")

        if not stored_device_id:
            # First time seeing device_id - this is okay
            # The webhook handler will store it in the config entry
            _LOGGER.debug(
                "No stored device_id yet, accepting incoming device_id: %s",
                incoming_device_id,
            )
            return

        # Validate device_id matches
        if incoming_device_id != stored_device_id:
            _LOGGER.error(
                "Device ID mismatch detected! Expected %s, got %s",
                stored_device_id,
                incoming_device_id,
            )
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="device_mismatch",
                translation_placeholders={
                    "expected": stored_device_id,
                    "actual": incoming_device_id,
                },
            )

        _LOGGER.debug("Device identity validated: %s", incoming_device_id)

    def normalize_sensor_value(self, key: str, raw_value: Any) -> Any:
        """Normalize raw sensor values.

        Converts string values with unit suffixes to proper numeric types.
        This centralizes value normalization logic for consistency.

        Args:
            key: Sensor key (e.g., "batt_voltage")
            raw_value: Raw value from device

        Returns:
            Normalized value suitable for Home Assistant
        """
        if raw_value is None:
            return None

        # Battery voltage: strip "V" / " V" suffix (any case) and convert to float
        # Handles firmware variants: "12.5V", "12.5 V", "12.5v", " 12.5 V "
        if key == "batt_voltage" and isinstance(raw_value, str):
            _LOGGER.debug("Raw batt_voltage from device: %r", raw_value)
            stripped = raw_value.strip()
            if stripped.upper().endswith("V"):
                numeric_part = stripped[:-1].strip()
                try:
                    return float(numeric_part)
                except ValueError:
                    _LOGGER.warning(
                        "Failed to parse battery voltage: %r (numeric part: %r)",
                        raw_value, numeric_part,
                    )
                    return raw_value

        # Generic numeric string conversion
        if isinstance(raw_value, str):
            # Check if it looks like a number
            cleaned = raw_value.replace(".", "", 1).replace("-", "", 1)
            if cleaned.isdigit():
                try:
                    return float(raw_value) if "." in raw_value else int(raw_value)
                except ValueError:
                    pass

        return raw_value

    def get_sensor_value(self, sensor_key: str) -> Any | None:
        """Get value for a specific sensor."""
        return self._data.get(sensor_key)
