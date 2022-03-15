import base64
import copy
import json
import os
from datetime import date
from datetime import datetime
from enum import Enum

import semantic_version
import yaml
from requests import ConnectionError
from requests.adapters import HTTPAdapter, MaxRetryError
from semantic_version import Spec
from urllib3 import Retry

from octoprint_mrbeam.mrb_logger import mrb_logger
from octoprint_mrbeam.util import dict_merge, logExceptions
from octoprint_mrbeam.util.github_api import get_file_of_repo_for_tag
from util.pip_util import get_version_of_pip_module


class SWUpdateTier(Enum):
    STABLE = "PROD"
    BETA = "BETA"
    ALPHA = "ALPHA"
    DEV = "DEV"


SW_UPDATE_TIERS_DEV = [SWUpdateTier.ALPHA.value, SWUpdateTier.DEV.value]
SW_UPDATE_TIERS_PROD = [SWUpdateTier.STABLE.value, SWUpdateTier.BETA.value]
SW_UPDATE_TIERS = SW_UPDATE_TIERS_DEV + SW_UPDATE_TIERS_PROD

DEFAULT_REPO_BRANCH_ID = {
    SWUpdateTier.STABLE.value: "stable",
    SWUpdateTier.BETA.value: "beta",
    SWUpdateTier.ALPHA.value: "alpha",
    SWUpdateTier.DEV.value: "develop",
}
MAJOR_VERSION_CLOUD_CONFIG = 0
SW_UPDATE_INFO_FILE_NAME = "update_info.json"

_logger = mrb_logger("octoprint.plugins.mrbeam.software_update_information")

# Commented constants are kept in case we update more packages from the virtualenv
# GLOBAL_PY_BIN = "/usr/bin/python2.7"
# VENV_PY_BIN = sys.executable
GLOBAL_PIP_BIN = "/usr/local/bin/pip"
GLOBAL_PIP_COMMAND = (
    "sudo {}".format(GLOBAL_PIP_BIN) if os.path.isfile(GLOBAL_PIP_BIN) else None
)

BEAMOS_LEGACY_DATE = date(2018, 1, 12)


def get_tag_of_github_repo(repo):
    """
    return the latest tag of a github repository
    Args:
        repo: repository name

    Returns:
        latest tag of the given majorversion <MAJOR_VERSION_CLOUD_CONFIG>
    """
    import requests
    import json

    try:
        url = "https://api.github.com/repos/mrbeam/{repo}/tags".format(repo=repo)
        headers = {
            "Accept": "application/json",
        }

        s = requests.Session()
        retry = Retry(connect=3, backoff_factor=0.3)
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("https://", adapter)
        s.keep_alive = False

        response = s.request("GET", url, headers=headers, timeout=3)
        response.raise_for_status()  # This will throw an exception if status is 4xx or 5xx
        if response:
            json_data = json.loads(response.text)
            versionlist = [
                semantic_version.Version(version.get("name")[1:])
                for version in json_data
            ]
            majorversion = Spec(
                "<{}.0.0".format(str(MAJOR_VERSION_CLOUD_CONFIG + 1))
            )  # simpleSpec("0.*.*")
            return majorversion.select(versionlist)
        else:
            _logger.warning(
                "no valid response for the tag of the update_config file {}".format(
                    response
                )
            )
            return None
    except MaxRetryError:
        _logger.warning("timeout while trying to get the tag of the update_config file")
        return None
    except requests.HTTPError as e:
        _logger.warning("server error {}".format(e))
        return None
    except ConnectionError:
        _logger.warning(
            "connection error while trying to get the tag of the update_config file"
        )
        return None


def get_update_information(plugin):
    """
    Gets called from the octoprint.plugin.softwareupdate.check_config Hook from Octoprint
    Starts a thread to look online for a new config file
    sets the config for the Octoprint Softwareupdate Plugin with the data from the config file
    Args:
        plugin: Mr Beam Plugin

    Returns:
        the config for the Octoprint embedded softwareupdate Plugin
    """
    try:
        tier = plugin._settings.get(["dev", "software_tier"])
        beamos_tier, beamos_date = plugin._device_info.get_beamos_version()
        _logger.info(
            "SoftwareUpdate using tier: {tier} {beamos_date}".format(
                tier=tier, beamos_date=beamos_date
            )
        )

        if plugin._connectivity_checker.check_immediately():
            config_tag = get_tag_of_github_repo("beamos_config")
            # if plugin._connectivity_checker.check_immediately():  # check if device online
            if config_tag:
                cloud_config = yaml.safe_load(
                    get_file_of_repo_for_tag(
                        repo="beamos_config",
                        file="docs/sw-update-conf.json",
                        tag="v{tag}".format(tag=str(config_tag)),
                    )
                )
                if cloud_config:
                    return _set_info_from_cloud_config(
                        plugin, tier, beamos_date, cloud_config
                    )
        else:
            _logger.warn("no internet connection")

        user_notification_system = plugin.user_notification_system
        user_notification_system.show_notifications(
            user_notification_system.get_notification(
                notification_id="missing_updateinformation_info", replay=False
            )
        )

        # mark update config as dirty
        sw_update_plugin = plugin._plugin_manager.get_plugin_info(
            "softwareupdate"
        ).implementation
        _clear_version_cache(sw_update_plugin)
    except Exception as e:
        _logger.exception(e)

    return _set_info_from_cloud_config(
        plugin,
        tier,
        beamos_date,
        {
            "default": {},
            "modules": {
                "mrbeam": {
                    "name": " MrBeam Plugin",
                    "type": "github_commit",
                    "user": "",
                    "repo": "",
                    "pip": "",
                },
                "mrbeamdoc": {
                    "name": "Mr Beam Documentation",
                    "type": "github_commit",
                    "user": "",
                    "repo": "",
                    "pip": "",
                },
                "netconnectd": {
                    "name": "OctoPrint-Netconnectd Plugin",
                    "type": "github_commit",
                    "user": "",
                    "repo": "",
                    "pip": "",
                },
                "findmymrbeam": {
                    "name": "OctoPrint-FindMyMrBeam",
                    "type": "github_commit",
                    "user": "",
                    "repo": "",
                    "pip": "",
                },
            },
        },
    )


def _clear_version_cache(sw_update_plugin):
    sw_update_plugin._version_cache = dict()
    sw_update_plugin._version_cache_dirty = True


def software_channels_available(plugin):
    """
    return the available software channels
    Args:
        plugin: Mr Beam Plugin

    Returns:
        list of available software channels
    """
    ret = copy.deepcopy(SW_UPDATE_TIERS_PROD)
    if plugin.is_dev_env():
        # fmt: off
        ret += SW_UPDATE_TIERS_DEV
        # fmt: on
    return ret


def switch_software_channel(plugin, channel):
    """
    Switches the Softwarechannel and triggers the reload of the config
    Args:
        plugin: Mr Beam Plugin
        channel: the channel where to switch to

    Returns:
        None
    """
    old_channel = plugin._settings.get(["dev", "software_tier"])
    if channel in software_channels_available(plugin) and channel != old_channel:
        _logger.info("Switching software channel to: {channel}".format(channel=channel))
        plugin._settings.set(["dev", "software_tier"], channel)
        reload_update_info(plugin)


def reload_update_info(plugin):
    """
    clears the version cache and refires the get_update_info hook
    Args:
        plugin: Mr Beam Plugin

    Returns:
        None
    """
    _logger.debug("Reload update info")

    # fmt: off
    sw_update_plugin = plugin._plugin_manager.get_plugin_info("softwareupdate").implementation
    # fmt: on
    sw_update_plugin._refresh_configured_checks = True
    _clear_version_cache(sw_update_plugin)


@logExceptions
def _set_info_from_cloud_config(plugin, tier, beamos_date, cloud_config):
    """
    loads update info from the update_info.json file
    the override order: default_settings->module_settings->tier_settings->beamos_settings
    and if there are update_settings set in the config.yaml they will replace all of the module
    the json file should look like:
        {
            "default": {<default_settings>}
            "modules": {
                <module_id>: {
                    <module_settings>,
                    <tier>:{<tier_settings>},
                    "beamos_date": {
                        <YYYY-MM-DD>: {<beamos_settings>}
                    }
                }
                "dependencies: {<module>}
            }
        }
    Args:
        plugin: Mr Beam Plugin
        tier: the software tier which should be used
        beamos_date: the image creation date of the running beamos
        cloud_config: the update config from the cloud

    Returns:
        software update information or None
    """
    if cloud_config:
        sw_update_config = dict()
        _logger.debug("update_info {}".format(cloud_config))
        defaultsettings = cloud_config.get("default", None)
        modules = cloud_config["modules"]

        for module_id, module in modules.items():
            if tier in SW_UPDATE_TIERS:
                sw_update_config[module_id] = {}

                module = dict_merge(defaultsettings, module)

                sw_update_config[module_id] = _generate_config_of_module(
                    module_id, module, defaultsettings, tier, beamos_date, plugin
                )

        _logger.debug("sw_update_config {}".format(sw_update_config))

        sw_update_file_path = os.path.join(
            plugin._settings.getBaseFolder("base"), SW_UPDATE_INFO_FILE_NAME
        )
        try:
            with open(sw_update_file_path, "w") as f:
                f.write(json.dumps(sw_update_config))
        except (IOError, TypeError):
            plugin._logger.error("can't create update info file")
            user_notification_system = plugin.user_notification_system
            user_notification_system.show_notifications(
                user_notification_system.get_notification(
                    notification_id="write_error_update_info_file_err", replay=False
                )
            )
            return None

        return sw_update_config
    else:
        return None


def _generate_config_of_module(
    module_id, input_moduleconfig, defaultsettings, tier, beamos_date, plugin
):
    """
    generates the config of a software module <module_id>
    Args:
        module_id: the id of the software module
        input_moduleconfig: moduleconfig
        defaultsettings: default settings
        tier: software tier
        beamos_date: date of the beamos
        plugin: Mr Beam Plugin

    Returns:
        software update informations for the module
    """
    if tier in SW_UPDATE_TIERS:
        # merge default settings and input is master
        input_moduleconfig = dict_merge(defaultsettings, input_moduleconfig)

        # get update info for tier branch
        tierversion = _get_tier_by_id(tier)

        if tierversion in input_moduleconfig:
            input_moduleconfig = dict_merge(
                input_moduleconfig, input_moduleconfig[tierversion]
            )  # set tier config from default settings

        # have to be after the default config from file

        input_moduleconfig = dict_merge(
            input_moduleconfig,
            _generate_config_of_beamos(input_moduleconfig, beamos_date, tierversion),
        )

        if "branch" in input_moduleconfig and "{tier}" in input_moduleconfig["branch"]:
            input_moduleconfig["branch"] = input_moduleconfig["branch"].format(
                tier=_get_tier_by_id(tier)
            )

        if "update_script" in input_moduleconfig and module_id == "mrbeam":
            update_script = os.path.join(
                plugin._basefolder, "scripts", "update_script.py"
            )
            input_moduleconfig["update_script"] = input_moduleconfig[
                "update_script"
            ].format(update_script=update_script)

        current_version = _get_curent_version(input_moduleconfig, module_id, plugin)

        if module_id != "octoprint":
            _logger.debug(
                "{module_id} current version: {current_version}".format(
                    module_id=module_id, current_version=current_version
                )
            )
            input_moduleconfig["displayVersion"] = (
                current_version if current_version else "-"
            )
        if "name" in input_moduleconfig:
            input_moduleconfig["displayName"] = input_moduleconfig["name"]

        input_moduleconfig = _clean_update_config(input_moduleconfig)

        if "dependencies" in input_moduleconfig:
            for dependencie_name, dependencie_config in input_moduleconfig[
                "dependencies"
            ].items():
                input_moduleconfig["dependencies"][
                    dependencie_name
                ] = _generate_config_of_module(
                    dependencie_name,
                    dependencie_config,
                    defaultsettings,
                    tier,
                    beamos_date,
                    plugin,
                )
        return input_moduleconfig


def _get_curent_version(input_moduleconfig, module_id, plugin):
    """
    returns the version of the given module
    Args:
        input_moduleconfig: module to get the version for
        module_id: id of the module
        plugin: Mr Beam Plugin

    Returns:
        version of the module or None
    """
    # get version number
    current_version = None
    if (
        "global_pip_command" in input_moduleconfig
        and "pip_command" not in input_moduleconfig
    ):
        input_moduleconfig["pip_command"] = GLOBAL_PIP_COMMAND
    if "pip_command" in input_moduleconfig:
        # get version number of pip modules
        pip_command = input_moduleconfig["pip_command"]
        # if global_pip_command is set module is installed outside of our virtualenv therefor we can't use default pip command.
        # /usr/local/lib/python2.7/dist-packages must be writable for pi user otherwise OctoPrint won't accept this as a valid pip command
        # pip_command = GLOBAL_PIP_COMMAND
        package_name = (
            input_moduleconfig["package_name"]
            if "package_name" in input_moduleconfig
            else module_id
        )
        _logger.debug(
            "get version {package_name} {pip_command}".format(
                package_name=package_name, pip_command=pip_command
            )
        )

        current_version_global_pip = get_version_of_pip_module(
            package_name, pip_command
        )
        if current_version_global_pip is not None:
            current_version = current_version_global_pip

    else:
        # get versionnumber of octoprint plugin
        pluginInfo = plugin._plugin_manager.get_plugin_info(module_id)
        if pluginInfo is not None:
            current_version = pluginInfo.version
    return current_version


def _generate_config_of_beamos(moduleconfig, beamos_date, tierversion):
    """
    generates the config for the given beamos_date of the tierversion
    Args:
        moduleconfig: update config of the module
        beamos_date: date of the beamos
        tierversion: software tier

    Returns:
        beamos config of the tierversion
    """
    _logger.debug("generate config of beamos {}".format(moduleconfig))
    if "beamos_date" not in moduleconfig:
        return {}

    beamos_date_config = {}
    prev_beamos_date_entry = datetime.strptime("2000-01-01", "%Y-%m-%d").date()
    for date, beamos_config in moduleconfig["beamos_date"].items():
        if (
            beamos_date >= datetime.strptime(date, "%Y-%m-%d").date()
            and prev_beamos_date_entry < beamos_date
        ):
            prev_beamos_date_entry = datetime.strptime(date, "%Y-%m-%d").date()
            if tierversion in beamos_config:
                beamos_config_module_tier = beamos_config[tierversion]
                beamos_config = dict_merge(
                    beamos_config, beamos_config_module_tier
                )  # override tier config from tiers set in config_file
            beamos_date_config = dict_merge(beamos_date_config, beamos_config)
    _logger.debug("generate config of beamos {}".format(beamos_date_config))
    return beamos_date_config


def _clean_update_config(update_config):
    """
    removes working parameters from the given config
    Args:
        update_config: update config information

    Returns:
        cleaned version of the update config
    """
    pop_list = ["alpha", "beta", "stable", "develop", "beamos_date", "name"]
    for key in set(update_config).intersection(pop_list):
        del update_config[key]
    return update_config


def _get_tier_by_id(tier):
    """
    returns the tier name with the given id
    Args:
        tier: id of the software tier

    Returns:
        softwaretier name
    """
    return DEFAULT_REPO_BRANCH_ID.get(tier, tier)
