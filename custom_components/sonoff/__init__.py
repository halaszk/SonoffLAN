import asyncio
import logging

import voluptuous as vol
from homeassistant.components import zeroconf
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, \
    SOURCE_IMPORT
from homeassistant.const import *
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import async_get as device_registry
from homeassistant.helpers.storage import Store

from . import system_health
from .core import backward
from .core.const import *
from .core.ewelink import XRegistry, XRegistryCloud, XRegistryLocal
from .core.ewelink.camera import XCameras
from .core.ewelink.cloud import AuthError

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    "binary_sensor", "button", "climate", "cover", "fan", "light", "remote",
    "sensor", "switch"
]

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Optional(CONF_USERNAME): cv.string,
        vol.Optional(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_DEFAULT_CLASS): cv.string,
        vol.Optional(CONF_RFBRIDGE): {
            cv.string: vol.Schema({
                vol.Optional(CONF_NAME): cv.string,
                vol.Optional(CONF_DEVICE_CLASS): cv.string,
                vol.Optional(CONF_TIMEOUT, default=120): cv.positive_int,
                vol.Optional(CONF_PAYLOAD_OFF): cv.string
            }, extra=vol.ALLOW_EXTRA),
        },
        vol.Optional(CONF_DEVICES): {
            cv.string: vol.Schema({
                vol.Optional(CONF_NAME): cv.string,
                vol.Optional(CONF_DEVICE_CLASS): vol.Any(str, list),
                vol.Optional(CONF_DEVICEKEY): cv.string,
            }, extra=vol.ALLOW_EXTRA),
        },
    }, extra=vol.ALLOW_EXTRA),
}, extra=vol.ALLOW_EXTRA)

UNIQUE_DEVICES = {}


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    if not backward.hass_version_supported:
        return False

    # init storage for registries
    hass.data[DOMAIN] = {}

    # load optional global registry config
    XRegistry.config = config.get(DOMAIN)

    # cameras starts only on first command to it
    cameras = XCameras()

    try:
        # import ewelink account from YAML (first time)
        data = {
            CONF_USERNAME: XRegistry.config[CONF_USERNAME],
            CONF_PASSWORD: XRegistry.config[CONF_PASSWORD]
        }
        if not hass.config_entries.async_entries(DOMAIN):
            coro = hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data=data
            )
            hass.async_create_task(coro)
    except Exception:
        pass

    async def send_command(call: ServiceCall):
        """Service for send raw command to device.
        :param call: `device` - required param, all other params - optional
        """
        data = dict(call.data)
        deviceid = str(data.pop('device'))

        if len(deviceid) == 10:
            registry = next(
                r for r in hass.data[DOMAIN].values() if deviceid in r.devices
            )
            device = registry.devices[deviceid]

            await registry.send(device, data)

        elif len(deviceid) == 6:
            await cameras.send(deviceid, data['cmd'])

        else:
            _LOGGER.error(f"Wrong deviceid {deviceid}")

    hass.services.async_register(DOMAIN, 'send_command', send_command)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    AUTO mode. If there is a login error to the cloud - it starts in LOCAL
    mode with devices list from cache. Trying to reconnect to the cloud.

    CLOUD mode. If there is a login error to the cloud - trying to reconnect to
    the cloud.

    LOCAL mode. If there is a login error to the cloud - it starts  with
    devices list from cache.
    """
    registry = hass.data[DOMAIN].get(entry.entry_id)
    if not registry:
        session = async_get_clientsession(hass)
        hass.data[DOMAIN][entry.entry_id] = registry = XRegistry(session)

    if entry.options.get("debug") and not _LOGGER.handlers:
        await system_health.setup_debug(hass, _LOGGER)

    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    mode = entry.options.get(CONF_MODE, "auto")

    # retry only when can't login first time
    if entry.state == ConfigEntryState.SETUP_RETRY:
        assert mode in ("auto", "cloud")
        try:
            await registry.cloud.login(username, password)
        except Exception as e:
            _LOGGER.warning(f"Can't login: {e}. Mode: {mode}")
            raise ConfigEntryNotReady(e)
        if mode == "auto":
            registry.cloud.start()
        elif mode == "cloud":
            hass.async_create_task(internal_normal_setup(hass, entry))
        return True

    if registry.cloud.auth is None and username and password:
        try:
            await registry.cloud.login(username, password)
        except Exception as e:
            _LOGGER.warning(f"Can't login: {e}, mode: {mode}")
            if mode in ("auto", "local"):
                hass.async_create_task(internal_cache_setup(hass, entry))
            if mode in ("auto", "cloud"):
                if isinstance(e, AuthError):
                    raise ConfigEntryAuthFailed(e)
                raise ConfigEntryNotReady(e)
            assert mode == "local"
            return True

    hass.async_create_task(internal_normal_setup(hass, entry))
    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    registry: XRegistry = hass.data[DOMAIN][entry.entry_id]
    await registry.stop()

    return True


async def internal_normal_setup(hass: HomeAssistant, entry: ConfigEntry):
    registry: XRegistry = hass.data[DOMAIN][entry.entry_id]
    if registry.cloud.auth:
        devices = await registry.cloud.get_devices()
        _LOGGER.debug(f"{len(devices)} devices loaded from Cloud")

        store = Store(hass, 1, f"{DOMAIN}/{entry.data['username']}.json")
        await store.async_save(devices)
    else:
        devices = None

    await internal_cache_setup(hass, entry, devices)


async def internal_cache_setup(
        hass: HomeAssistant, entry: ConfigEntry, devices: list = None
):
    await asyncio.gather(*[
        hass.config_entries.async_forward_entry_setup(entry, domain)
        for domain in PLATFORMS
    ])

    if devices is None:
        store = Store(hass, 1, f"{DOMAIN}/{entry.data['username']}.json")
        devices = await store.async_load()
        if devices:
            # 16 devices loaded from the Cloud Server
            _LOGGER.debug(f"{len(devices)} devices loaded from Cache")

    registry: XRegistry = hass.data[DOMAIN][entry.entry_id]
    if devices:
        devices = internal_unique_devices(entry.entry_id, devices)
        registry.setup_devices(devices)

    mode = entry.options.get(CONF_MODE, "auto")
    if mode != "local" and registry.cloud.auth:
        registry.cloud.start()
    if mode != "cloud":
        zc = await zeroconf.async_get_instance(hass)
        registry.local.start(zc)

    _LOGGER.debug(f"{mode.upper()} mode start")

    if not entry.update_listeners:
        entry.add_update_listener(async_update_options)

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, registry.stop)
    )


def internal_unique_devices(uid: str, devices: list) -> list:
    """For support multiple integrations - bind each device to one integraion.
    To avoid duplicates.
    """
    return [
        device for device in devices
        if UNIQUE_DEVICES.setdefault(device["deviceid"], uid) == uid
    ]


async def async_remove_config_entry_device(
        hass: HomeAssistant, entry: ConfigEntry, device
) -> bool:
    device_registry(hass).async_remove_device(device.id)
    return True
