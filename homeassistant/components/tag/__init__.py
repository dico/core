"""The Tag integration."""

from __future__ import annotations

import logging
from typing import Any, final
import uuid

import voluptuous as vol

from homeassistant.const import CONF_NAME
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import collection
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify
import homeassistant.util.dt as dt_util

from .const import DEFAULT_NAME, DEVICE_ID, DOMAIN, EVENT_TAG_SCANNED, TAG_ID

_LOGGER = logging.getLogger(__name__)

LAST_SCANNED = "last_scanned"
STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1
TAGS = "tags"

CREATE_FIELDS = {
    vol.Optional(TAG_ID): cv.string,
    vol.Optional(CONF_NAME): vol.All(str, vol.Length(min=1)),
    vol.Optional("description"): cv.string,
    vol.Optional(LAST_SCANNED): cv.datetime,
    vol.Optional(DEVICE_ID): cv.string,
}

UPDATE_FIELDS = {
    vol.Optional(CONF_NAME): vol.All(str, vol.Length(min=1)),
    vol.Optional("description"): cv.string,
    vol.Optional(LAST_SCANNED): cv.datetime,
    vol.Optional(DEVICE_ID): cv.string,
}

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


class TagIDExistsError(HomeAssistantError):
    """Raised when an item is not found."""

    def __init__(self, item_id: str) -> None:
        """Initialize tag ID exists error."""
        super().__init__(f"Tag with ID {item_id} already exists.")
        self.item_id = item_id


class TagIDManager(collection.IDManager):
    """ID manager for tags."""

    def generate_id(self, suggestion: str) -> str:
        """Generate an ID."""
        if self.has_id(suggestion):
            raise TagIDExistsError(suggestion)

        return suggestion


class TagStorageCollection(collection.DictStorageCollection):
    """Tag collection stored in storage."""

    CREATE_SCHEMA = vol.Schema(CREATE_FIELDS)
    UPDATE_SCHEMA = vol.Schema(UPDATE_FIELDS)

    async def _process_create_data(self, data: dict) -> dict:
        """Validate the config is valid."""
        data = self.CREATE_SCHEMA(data)
        if not data[TAG_ID]:
            data[TAG_ID] = str(uuid.uuid4())
        # make last_scanned JSON serializeable
        if LAST_SCANNED in data:
            data[LAST_SCANNED] = data[LAST_SCANNED].isoformat()
        return data

    @callback
    def _get_suggested_id(self, info: dict[str, str]) -> str:
        """Suggest an ID based on the config."""
        return info[TAG_ID]

    async def _update_data(self, item: dict, update_data: dict) -> dict:
        """Return a new updated data object."""
        data = {**item, **self.UPDATE_SCHEMA(update_data)}
        # make last_scanned JSON serializeable
        if LAST_SCANNED in update_data:
            data[LAST_SCANNED] = data[LAST_SCANNED].isoformat()
        return data


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Tag component."""
    hass.data[DOMAIN] = {}
    id_manager = TagIDManager()
    hass.data[DOMAIN][TAGS] = storage_collection = TagStorageCollection(
        Store(hass, STORAGE_VERSION, STORAGE_KEY),
        id_manager,
    )
    await storage_collection.async_load()
    collection.DictStorageCollectionWebsocket(
        storage_collection, DOMAIN, DOMAIN, CREATE_FIELDS, UPDATE_FIELDS
    ).async_setup(hass)

    entities: dict[str, TagEntity] = {}

    async def tag_change_listener(
        change_type: str, item_id: str, updated_config: dict
    ) -> None:
        """Tag event listener."""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "%s, item: %s, update: %s", change_type, item_id, updated_config
            )
        if change_type == collection.CHANGE_ADDED:
            # When tags are added to storage
            entities[updated_config[TAG_ID]] = TagEntity(
                hass,
                updated_config.get(CONF_NAME, DEFAULT_NAME),
                updated_config[TAG_ID],
                updated_config.get(LAST_SCANNED, ""),
                updated_config.get(DEVICE_ID),
            )
            await entities[updated_config[TAG_ID]].async_add_initial_state()

        if change_type == collection.CHANGE_UPDATED:
            # When tags are changed or updated in storage
            if entities[updated_config[TAG_ID]]._last_scanned != updated_config.get(  # pylint: disable=protected-access
                LAST_SCANNED, ""
            ):
                entities[updated_config[TAG_ID]].async_handle_event(
                    updated_config.get(DEVICE_ID),
                    updated_config.get(LAST_SCANNED, ""),
                )

        # Deleted tags
        if change_type == collection.CHANGE_REMOVED:
            # When tags is removed from storage
            await entities[updated_config[TAG_ID]].async_remove()
            entity_id = entities[updated_config[TAG_ID]].entity_id
            hass.states.async_remove(entity_id)
            entities.pop(updated_config[TAG_ID])
            hass.states.async_remove(entity_id)

    storage_collection.async_add_listener(tag_change_listener)

    for tag in storage_collection.async_items():
        _LOGGER.debug("Adding tag: %s", tag)
        entities[tag[TAG_ID]] = TagEntity(
            hass,
            tag.get(CONF_NAME, DEFAULT_NAME),
            tag[TAG_ID],
            tag.get(LAST_SCANNED, ""),
            tag.get(DEVICE_ID),
        )
        await entities[tag[TAG_ID]].async_add_initial_state()

    return True


async def async_scan_tag(
    hass: HomeAssistant,
    tag_id: str,
    device_id: str | None,
    context: Context | None = None,
) -> None:
    """Handle when a tag is scanned."""
    if DOMAIN not in hass.config.components:
        raise HomeAssistantError("tag component has not been set up.")

    helper: TagStorageCollection = hass.data[DOMAIN][TAGS]

    # Get name from helper, default value None if not present in data
    tag_name = None
    if tag_data := helper.data.get(tag_id):
        tag_name = tag_data.get(CONF_NAME)

    hass.bus.async_fire(
        EVENT_TAG_SCANNED,
        {TAG_ID: tag_id, CONF_NAME: tag_name, DEVICE_ID: device_id},
        context=context,
    )

    if tag_id in helper.data:
        await helper.async_update_item(
            tag_id, {LAST_SCANNED: dt_util.utcnow(), DEVICE_ID: device_id or ""}
        )
    else:
        await helper.async_create_item(
            {TAG_ID: tag_id, LAST_SCANNED: dt_util.utcnow(), DEVICE_ID: device_id or ""}
        )
    _LOGGER.debug("Tag: %s scanned by device: %s", tag_id, device_id)


class TagEntity(Entity):
    """Representation of a Tag entity."""

    _unrecorded_attributes = frozenset({TAG_ID})
    _attr_translation_key = DOMAIN
    _attr_should_poll = False

    # Implements it's own platform
    _no_platform_reported = True

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        tag_id: str,
        last_scanned: str,
        device_id: str | None,
    ) -> None:
        """Initialize the Tag event."""
        self.entity_id = f"tag.{slugify(name)}"
        self.hass = hass
        self._attr_name = name
        self._tag_id = tag_id
        self._last_device_id: str | None = device_id
        self._last_scanned = last_scanned
        self._attr_unique_id = tag_id

        self._state_info = {
            "unrecorded_attributes": self._Entity__combined_unrecorded_attributes  # type: ignore[attr-defined]
        }

    @callback
    def async_handle_event(self, device_id: str | None, last_scanned: str) -> None:
        """Handle the Tag scan event."""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Tag scanned for %s with device %s last scanned before %s and scanned now %s",
                self._tag_id,
                device_id,
                self._last_scanned,
                last_scanned,
            )
        self._last_device_id = device_id
        self._last_scanned = last_scanned
        self.async_write_ha_state()

    @property
    @final
    def state(self) -> str | None:
        """Return the entity state."""
        if (last_scanned := dt_util.parse_datetime(self._last_scanned)) is None:
            return None
        return last_scanned.isoformat(timespec="milliseconds")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes of the sun."""
        return {TAG_ID: self._tag_id, DEVICE_ID: self._last_device_id}

    async def async_add_initial_state(self) -> None:
        """Add initial state."""
        self.async_write_ha_state()
