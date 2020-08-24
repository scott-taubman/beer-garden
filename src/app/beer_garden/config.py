# -*- coding: utf-8 -*-
import os
import sys
from argparse import ArgumentParser
from datetime import datetime
from typing import Optional, Sequence, Tuple, Union

from box import Box
from brewtils.rest import normalize_url_prefix as normalize
from ruamel.yaml import YAML
from yapconf import YapconfSpec, dump_data

from beer_garden.errors import ConfigurationError
from beer_garden.log import default_app_config

__all__ = ["load", "generate_logging", "generate", "migrate", "get"]

_CONFIG = None


def load(args: Sequence[str], force: bool = False) -> None:
    """Load the application configuration.

    Attempt to load the application configuration in the following order:

    1. CLI Arguments
    2. File (specified either by CLI or environment variable)
    3. Environment variables

    If force is True then it will always reload the configuration, otherwise
    if the configuration is already loaded, will immediately return.

    Once loaded, the configuration can be reached with the `get` method.

    Args:
        args: Command line arguments
        force: Force a reload
    """
    global _CONFIG
    if _CONFIG is not None and not force:
        return

    spec, cli_vars = _parse_args(args)
    config_sources = _setup_config_sources(spec, cli_vars)

    raw_config = spec.load_config(*config_sources)

    # Create a Box with default to avoid KeyErrors
    config = Box(raw_config.to_dict(), default_box=True)
    if config.entry.http.url_prefix:
        config.entry.http.url_prefix = normalize(config.entry.http.url_prefix)
    if config.event.brew_view.url_prefix:
        config.event.brew_view.url_prefix = normalize(config.event.brew_view.url_prefix)

    _CONFIG = Box(config.to_dict())


def generate(args: Sequence[str]):
    """Generate a configuration file.

    Takes a series of command line arguments and will create a file at the location
    specified by the resolved `configuration.file` value. If that value resolves to None
    the configuration will be printed to STDOUT.

    Note that bootstrap items will not be included in the generated configuration.

    Args:
        args: Command line arguments

    Returns:
        None

    Raises:
        YapconfLoadError: Missing 'config' configuration option (file location)
    """
    spec, cli_vars = _parse_args(args)

    bootstrap = spec.load_filtered_config(cli_vars, "ENVIRONMENT", bootstrap=True)
    config = spec.load_filtered_config(cli_vars, "ENVIRONMENT", exclude_bootstrap=True)

    dump_data(config, filename=bootstrap.configuration.file, file_type="yaml")


def migrate(args: Sequence[str]):
    """Updates a configuration file in-place.

    Args:
        args: Command line arguments. Must contain an argument that specifies the
        config file to update ('-c')
    Returns:
        None

    Raises:
        YapconfLoadError: Missing 'config' configuration option (file location)
    """
    spec, cli_vars = _parse_args(args)

    config = spec.load_config(cli_vars, "ENVIRONMENT")

    if not config.configuration.file:
        raise SystemExit(
            "Please specify a config file to update" " in the CLI arguments (-c)"
        )

    current_root, current_extension = os.path.splitext(config.configuration.file)

    current_type = current_extension[1:]
    if current_type == "yml":
        current_type = "yaml"

    # Determine if a type conversion is needed
    type_conversion = False
    new_type = "yaml"
    if current_type != new_type:
        new_file = current_root + "." + new_type
        type_conversion = True
    else:
        new_file = config.configuration.file

    spec.migrate_config_file(
        config.configuration.file,
        current_file_type=current_type,
        output_file_name=new_file,
        output_file_type=new_type,
        update_defaults=True,
        include_bootstrap=False,
    )

    if type_conversion:
        os.remove(config.configuration.file)


def generate_logging(args: Sequence[str]):
    """Generate and save logging configuration file.

    Args:
        args: Command line arguments
            --log-config-file: Configuration will be written to this file (will print to
                stdout if missing)
            --log-file: Logs will be written to this file (used in a RotatingFileHandler)
            --log-level: Handlers will be configured with this logging level

    Returns:
        str: The logging configuration dictionary
    """
    spec, cli_vars = _parse_args(args)
    filtered_args = spec.load_filtered_config(
        cli_vars, include=["log.config_file", "log.file", "log.level"]
    )

    log = filtered_args.get("log", {})
    logging_config = default_app_config(log.get("level"), log.get("file"))
    log_config_file = log.get("config_file")

    dump_data(logging_config, filename=log_config_file, file_type="yaml")

    return logging_config


def get(key: Optional[str] = None) -> Union[str, int, float, bool, complex, Box, None]:
    """Get specified key from the config.

    Nested keys can be separated with a "." If the key does not exist, then
    a None will be returned.

    If the key itself is None, then the entire config will be returned.

    If the requested value is a container (has child items) then the returned value will
    be an immutable (frozen) ``box.Box`` object.

    Args:
        key: The key to get, nested keys are separated with "."

    Returns:
        The value of the key in the config.
    """
    if key is None:
        return _CONFIG

    value = _CONFIG
    for key_part in key.split("."):
        if key_part not in value:
            return None
        value = value[key_part]
    return value


def assign(new_config: Box, force: bool = False) -> None:
    """Set the overall application config.

    This methods sets the global configuration to the given Box object. This method is
    only intended to be used in a subprocess context where reconstructing the
    configuration using ``load`` would be inadvisable.

    Args:
        new_config: The configuration object to be applied
        force: If True, set the config even if one is already set

    Returns:
        None

    Raises:
        ConfigurationError: A config is already loaded and ``force`` is False
    """
    global _CONFIG
    if _CONFIG is not None and not force:
        raise ConfigurationError("Attempting to reset config without force flag")

    _CONFIG = new_config


def _setup_config_sources(spec, cli_vars):
    spec.add_source("cli_args", "dict", data=cli_vars)
    spec.add_source("ENVIRONMENT", "environment")

    config_sources = ["cli_args", "ENVIRONMENT"]

    # Load bootstrap items to see if there's a config file
    temp_config = spec.load_config(*config_sources, bootstrap=True)
    config_filename = temp_config.configuration.file

    if config_filename:
        _safe_migrate(spec, config_filename)
        spec.add_source(config_filename, "yaml", filename=config_filename)
        config_sources.insert(1, config_filename)

    return config_sources


def _safe_migrate(spec, filename):
    tmp_filename = filename + ".tmp"
    try:
        spec.migrate_config_file(
            filename,
            current_file_type="yaml",
            output_file_name=tmp_filename,
            output_file_type="yaml",
            include_bootstrap=False,
        )
    except Exception:
        sys.stderr.write(
            "Could not successfully migrate application configuration. "
            "Will attempt to load the previous configuration."
        )
        return
    if _is_new_config(filename, tmp_filename):
        _backup_previous_config(filename, tmp_filename)
    else:
        os.remove(tmp_filename)


def _is_new_config(filename, tmp_filename):
    with open(filename, "r") as old_file, open(tmp_filename, "r") as new_file:
        yaml = YAML()
        old_config = yaml.load(old_file)
        new_config = yaml.load(new_file)
    return old_config != new_config


def _backup_previous_config(filename, tmp_filename):
    try:
        os.rename(filename, filename + "_" + datetime.utcnow().isoformat())
    except Exception:
        sys.stderr.write(
            "Could not backup the old configuration. Cowardly refusing to "
            "overwrite the current configuration with the old configuration. "
            "This could cause problems later. Please see %s for the new "
            "configuration file" % tmp_filename
        )
        return

    try:
        os.rename(tmp_filename, filename)
    except Exception:
        sys.stderr.write(
            "ERROR: Config migration was a success, but we could not move the "
            "new config into the old config value. Maybe a permission issue? "
            "Beer Garden cannot start now. To resolve this, you need to rename "
            "%s to %s" % (tmp_filename, filename)
        )
        raise


def _parse_args(args: Sequence[str]) -> Tuple[YapconfSpec, dict]:
    """Construct a spec and parse command line arguments

    Args:
        args: Command line arguments

    Returns:
        Config object with only the named items
    """
    spec = YapconfSpec(_SPECIFICATION, env_prefix="BG_")

    parser = ArgumentParser()
    spec.add_arguments(parser)
    cli_vars = vars(parser.parse_args(args))

    return spec, cli_vars


_GARDEN_SPEC = {
    "type": "dict",
    "items": {
        "name": {
            "type": "str",
            "required": True,
            "default": "default",
            "description": "The routing name for upstream Beer Gardens to use",
        }
    },
}

_META_SPEC = {
    "type": "dict",
    "bootstrap": True,
    "items": {
        "file": {
            "type": "str",
            "description": "Path to configuration file to use",
            "required": False,
            "cli_short_name": "c",
            "bootstrap": True,
            "previous_names": ["config"],
            "alt_env_names": ["CONFIG"],
        }
    },
}

_MQ_SSL_SPEC = {
    "type": "dict",
    "items": {
        "enabled": {
            "type": "bool",
            "default": False,
            "description": "Should the connection use SSL",
        },
        "ca_cert": {
            "type": "str",
            "description": "Path to CA certificate file to use",
            "required": False,
        },
        "ca_verify": {
            "type": "bool",
            "default": True,
            "description": "Verify external certificates",
            "required": False,
        },
        "client_cert": {
            "type": "str",
            "description": "Path to client combined key / certificate",
            "required": False,
        },
    },
}

_MQ_SPEC = {
    "type": "dict",
    "previous_names": ["amq"],
    "items": {
        "host": {
            "type": "str",
            "default": "localhost",
            "description": "Hostname of MQ to use",
            "previous_names": ["amq_host"],
        },
        "admin_queue_expiry": {
            "type": "int",
            "default": 3600000,  # One hour
            "description": "Time before unused admin queues are removed",
        },
        "heartbeat_interval": {
            "type": "int",
            "default": 3600,
            "description": "Heartbeat interval for MQ",
            "previous_names": ["amq_heartbeat_interval"],
        },
        "blocked_connection_timeout": {
            "type": "int",
            "default": 5,
            "description": "Time to wait for a blocked connection to be unblocked",
        },
        "connection_attempts": {
            "type": "int",
            "default": 3,
            "description": "Number of retries to connect to MQ",
            "previous_names": ["amq_connection_attempts"],
        },
        "exchange": {
            "type": "str",
            "default": "beer_garden",
            "description": "Exchange name to use for MQ",
            "previous_names": ["amq_exchange"],
        },
        "virtual_host": {
            "type": "str",
            "default": "/",
            "description": "Virtual host to use for MQ",
            "previous_names": ["amq_virtual_host"],
        },
        "connections": {
            "type": "dict",
            "items": {
                "admin": {
                    "type": "dict",
                    "items": {
                        "port": {
                            "type": "int",
                            "default": 15672,
                            "description": "Port of the MQ Admin host",
                            "previous_names": ["amq_admin_port"],
                            "alt_env_names": ["AMQ_ADMIN_PORT"],
                        },
                        "user": {
                            "type": "str",
                            "default": "guest",
                            "description": "Username to login to the MQ admin",
                            "previous_names": ["amq_admin_user"],
                            "alt_env_names": ["AMQ_ADMIN_USER"],
                        },
                        "password": {
                            "type": "str",
                            "default": "guest",
                            "description": "Password to login to the MQ admin",
                            "previous_names": ["amq_admin_password", "amq_admin_pw"],
                            "alt_env_names": ["AMQ_ADMIN_PASSWORD", "AMQ_ADMIN_PW"],
                        },
                        "ssl": _MQ_SSL_SPEC,
                    },
                },
                "message": {
                    "type": "dict",
                    "items": {
                        "port": {
                            "type": "int",
                            "default": 5672,
                            "description": "Port of the MQ host",
                            "previous_names": ["amq_port"],
                            "alt_env_names": ["AMQ_PORT"],
                        },
                        "password": {
                            "type": "str",
                            "default": "guest",
                            "description": "Password to login to the MQ host",
                            "previous_names": ["amq_password"],
                            "alt_env_names": ["AMQ_PASSWORD"],
                        },
                        "user": {
                            "type": "str",
                            "default": "guest",
                            "description": "Username to login to the MQ host",
                            "previous_names": ["amq_user"],
                            "alt_env_names": ["AMQ_USER"],
                        },
                        "ssl": _MQ_SSL_SPEC,
                    },
                },
            },
        },
    },
}

_APP_SPEC = {
    "type": "dict",
    "items": {
        "cors_enabled": {
            "type": "bool",
            "default": False,
            "description": "Determine if CORS should be enabled",
            "previous_names": ["cors_enabled"],
        },
        "debug_mode": {
            "type": "bool",
            "default": False,
            "description": "Run the application in debug mode",
            "previous_names": ["debug_mode"],
        },
        "name": {
            "type": "str",
            "default": "Beer Garden",
            "description": "The title to display on the GUI",
            "previous_names": ["application_name"],
        },
        "icon_default": {
            "type": "str",
            "description": "Default font-awesome icon to display",
            "default": "fa-beer",
            "previous_names": ["icon_default"],
            "alt_env_names": ["ICON_DEFAULT"],
        },
        "allow_unsafe_templates": {
            "type": "bool",
            "default": False,
            "description": "Allow unsafe templates to be loaded by the application",
            "previous_names": ["ALLOW_UNSANITIZED_TEMPLATES", "allow_unsafe_templates"],
            "alt_env_names": [
                "ALLOW_UNSANITIZED_TEMPLATES",
                "BG_ALLOW_UNSAFE_TEMPLATES",
            ],
        },
    },
}

_AUTH_SPEC = {
    "type": "dict",
    "items": {
        "enabled": {
            "type": "bool",
            "default": False,
            "description": "Use role-based authentication / authorization",
        },
        "guest_login_enabled": {
            "type": "bool",
            "default": True,
            "description": "Only applicable if auth is enabled. If set to "
            "true, guests can login without username/passwords.",
        },
        "token": {
            "type": "dict",
            "items": {
                "algorithm": {
                    "type": "str",
                    "default": "HS256",
                    "description": "Algorithm to use when signing tokens",
                },
                "lifetime": {
                    "type": "int",
                    "default": 1200,
                    "description": "Time (seconds) before a token expires",
                },
                "secret": {
                    "type": "str",
                    "required": False,
                    "description": "Secret to use when signing tokens",
                    "default": "",
                },
            },
        },
    },
}

_DB_SPEC = {
    "type": "dict",
    "items": {
        "name": {
            "type": "str",
            "default": "beer_garden",
            "description": "Name of the database to use",
            "previous_names": ["db_name"],
        },
        "connection": {
            "type": "dict",
            "items": {
                "host": {
                    "type": "str",
                    "default": "localhost",
                    "description": "Hostname/IP of the database server",
                    "previous_names": ["db_host"],
                    "alt_env_names": ["DB_HOST"],
                },
                "password": {
                    "type": "str",
                    "default": None,
                    "required": False,
                    "description": "Password to connect to the database",
                    "previous_names": ["db_password"],
                    "alt_env_names": ["DB_PASSWORD"],
                },
                "port": {
                    "type": "int",
                    "default": 27017,
                    "description": "Port of the database server",
                    "previous_names": ["db_port"],
                    "alt_env_names": ["DB_PORT"],
                },
                "username": {
                    "type": "str",
                    "default": None,
                    "required": False,
                    "description": "Username to connect to the database",
                    "previous_names": ["db_username"],
                    "alt_env_names": ["DB_USERNAME"],
                },
            },
        },
        "ttl": {
            "type": "dict",
            "items": {
                "event": {
                    "type": "int",
                    "default": 15,
                    "description": "Number of minutes to wait before deleting "
                    "events (negative number for never)",
                    "previous_names": ["event_mongo_ttl"],
                    "alt_env_names": ["EVENT_MONGO_TTL"],
                },
                "action": {
                    "type": "int",
                    "default": -1,
                    "description": "Number of minutes to wait before deleting "
                    "ACTION requests (negative number for never)",
                    "previous_names": ["action_request_ttl"],
                    "alt_env_names": ["ACTION_REQUEST_TTL"],
                },
                "info": {
                    "type": "int",
                    "default": 15,
                    "description": "Number of minutes to wait before deleting "
                    "INFO requests (negative number for never)",
                    "previous_names": ["info_request_ttl"],
                    "alt_env_names": ["INFO_REQUEST_TTL"],
                },
            },
        },
    },
}

_HTTP_SPEC = {
    "type": "dict",
    "items": {
        "enabled": {
            "type": "bool",
            "default": True,
            "description": "Run an HTTP server",
            "previous_names": ["entry_http_enable"],
        },
        "ssl": {
            "type": "dict",
            "items": {
                "enabled": {
                    "type": "bool",
                    "default": False,
                    "description": "Serve content using SSL",
                    "previous_names": ["ssl_enabled"],
                    "alt_env_names": ["SSL_ENABLED"],
                    "cli_separator": "_",
                },
                "private_key": {
                    "type": "str",
                    "description": "Path to a private key",
                    "required": False,
                    "previous_names": ["ssl_private_key"],
                    "alt_env_names": ["SSL_PRIVATE_KEY"],
                },
                "public_key": {
                    "type": "str",
                    "description": "Path to a public key",
                    "required": False,
                    "previous_names": ["ssl_public_key"],
                    "alt_env_names": ["SSL_PUBLIC_KEY"],
                },
                "ca_cert": {
                    "type": "str",
                    "description": (
                        "Path to CA certificate file to use for SSLContext"
                    ),
                    "required": False,
                    "previous_names": ["ca_cert"],
                    "alt_env_names": ["CA_CERT"],
                },
                "ca_path": {
                    "type": "str",
                    "description": (
                        "Path to CA certificate path to use for SSLContext"
                    ),
                    "required": False,
                    "previous_names": ["ca_path"],
                    "alt_env_names": ["CA_PATH"],
                },
                "client_cert_verify": {
                    "type": "str",
                    "description": (
                        "Client certificate mode to use when handling requests"
                    ),
                    "choices": ["NONE", "OPTIONAL", "REQUIRED"],
                    "default": "NONE",
                    "previous_names": ["client_cert_verify"],
                    "alt_env_names": ["CLIENT_CERT_VERIFY"],
                },
            },
        },
        "port": {
            "type": "int",
            "default": 2337,
            "description": "Serve content on this port",
            "previous_names": ["web_port"],
        },
        "url_prefix": {
            "type": "str",
            "default": "/",
            "description": "URL path prefix",
            "required": False,
            "previous_names": ["url_prefix"],
            "alt_env_names": ["URL_PREFIX"],
        },
        "host": {
            "type": "str",
            "default": "0.0.0.0",
            "description": "Host for the HTTP Server to bind to",
        },
        "public_fqdn": {
            "type": "str",
            "default": "localhost",
            "description": "Public fully-qualified domain name",
            "previous_names": ["public_fqdn"],
            "alt_env_names": ["PUBLIC_FQDN"],
        },
    },
}

_ENTRY_SPEC = {
    "type": "dict",
    "items": {
        "http": _HTTP_SPEC,
        "thrift": {
            "type": "dict",
            "items": {
                "enabled": {
                    "type": "bool",
                    "default": False,
                    "description": "Run an thrift server",
                    "previous_names": ["entry_thrift_enable"],
                }
            },
        },
    },
}

_PARENT_SPEC = {
    "type": "dict",
    "items": {
        "http": {
            "type": "dict",
            "items": {
                "enabled": {
                    "type": "bool",
                    "default": False,
                    "description": "Publish events to parent garden over HTTP",
                },
                "ssl": {
                    "type": "dict",
                    "items": {
                        "enabled": {
                            "type": "bool",
                            "default": False,
                            "description": "Serve content using SSL",
                        },
                        "private_key": {
                            "type": "str",
                            "description": "Path to a private key",
                            "required": False,
                        },
                        "public_key": {
                            "type": "str",
                            "description": "Path to a public key",
                            "required": False,
                        },
                        "ca_cert": {
                            "type": "str",
                            "description": (
                                "Path to CA certificate file to use for SSLContext"
                            ),
                            "required": False,
                        },
                        "ca_path": {
                            "type": "str",
                            "description": (
                                "Path to CA certificate path to use for SSLContext"
                            ),
                            "required": False,
                        },
                        "client_cert_verify": {
                            "type": "str",
                            "description": (
                                "Client certificate mode to use when handling requests"
                            ),
                            "choices": ["NONE", "OPTIONAL", "REQUIRED"],
                            "default": "NONE",
                        },
                    },
                },
                "port": {
                    "type": "int",
                    "default": 2337,
                    "description": "Serve content on this port",
                },
                "url_prefix": {
                    "type": "str",
                    "default": "/",
                    "description": "URL path prefix",
                    "required": False,
                },
                "host": {
                    "type": "str",
                    "default": "0.0.0.0",
                    "description": "Host for the HTTP Server to bind to",
                },
                "public_fqdn": {
                    "type": "str",
                    "default": "localhost",
                    "description": "Public fully-qualified domain name",
                },
                "skip_events": {
                    "type": "list",
                    "items": {"skip_event": {"type": "str"}},
                    "default": ["DB_CREATE"],
                    "required": False,
                    "description": "Events to be skipped",
                },
            },
        }
    },
}

_EVENT_SPEC = {
    "type": "dict",
    "items": {
        "mq": {
            "type": "dict",
            "items": {
                "enabled": {
                    "type": "bool",
                    "default": False,
                    "description": "Publish events to RabbitMQ",
                    "previous_names": ["event_persist_mq_enable"],
                },
                "exchange": {
                    "type": "str",
                    "required": False,
                    "description": "Exchange to use for MQ events",
                    "previous_names": ["event_amq_exchange"],
                },
                "virtual_host": {
                    "type": "str",
                    "default": "/",
                    "required": False,
                    "description": "Virtual host to use for MQ events",
                    "previous_names": ["event_amq_virtual_host"],
                },
            },
        },
        "mongo": {
            "type": "dict",
            "items": {
                "enabled": {
                    "type": "bool",
                    "default": True,
                    "description": "Persist events to Mongo",
                    "previous_names": ["event_persist_mongo", "event_persist_mongo_enable"],
                    "alt_env_names": ["EVENT_PERSIST_MONGO"],
                }
            },
        },
    },
}

_LOG_SPEC = {
    "type": "dict",
    "items": {
        "config_file": {
            "type": "str",
            "description": "Path to a logging config file.",
            "required": False,
            "cli_short_name": "l",
            "previous_names": ["log_config"],
            "alt_env_names": ["LOG_CONFIG"],
        },
        "file": {
            "type": "str",
            "description": "File you would like the application to log to",
            "required": False,
            "previous_names": ["log_file"],
        },
        "level": {
            "type": "str",
            "description": "Log level for the application",
            "default": "INFO",
            "choices": ["DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL"],
            "previous_names": ["log_level"],
        },
    },
}

_METRICS_SPEC = {
    "type": "dict",
    "items": {
        "prometheus": {
            "type": "dict",
            "items": {
                "enabled": {
                    "type": "bool",
                    "description": "Enable prometheus server",
                    "default": True,
                },
                "host": {
                    "type": "str",
                    "default": "0.0.0.0",
                    "description": "Host to bind the prometheus server to",
                },
                "port": {
                    "type": "int",
                    "description": "Port for prometheus server to listen on.",
                    "default": 2338,
                },
                "url": {
                    "type": "str",
                    "description": "URL to prometheus/grafana server.",
                    "required": False,
                },
            },
        }
    },
}

_PLUGIN_SPEC = {
    "type": "dict",
    "items": {
        "logging": {
            "type": "dict",
            "items": {
                "config_file": {
                    "type": "str",
                    "description": "Path to a logging configuration file for plugins",
                    "required": False,
                },
                "fallback_level": {
                    "type": "str",
                    "description": "Level that will be used with a default logging "
                    "configuration if a config_file is not provided",
                    "previous_names": ["plugin_logging_level"],
                    "default": "INFO",
                    "choices": [
                        "DEBUG",
                        "INFO",
                        "WARN",
                        "WARNING",
                        "ERROR",
                        "CRITICAL",
                    ],
                },
            },
        },
        "status_heartbeat": {
            "type": "int",
            "default": 10,
            "description": "Amount of time between status messages",
            "previous_names": ["plugin_status_heartbeat"],
        },
        "status_timeout": {
            "type": "int",
            "default": 30,
            "description": "Amount of time to wait before marking a plugin as"
            "unresponsive",
            "previous_names": ["plugin_status_timeout "],
        },
        "local": {
            "type": "dict",
            "items": {
                "auth": {
                    "type": "dict",
                    "items": {
                        "username": {
                            "type": "str",
                            "description": "Username that local plugins will use for "
                            "authentication (needs bg-plugin role)",
                            "required": False,
                        },
                        "password": {
                            "type": "str",
                            "description": "Password that local plugins will use for "
                            "authentication (needs bg-plugin role)",
                            "required": False,
                        },
                    },
                },
                "directory": {
                    "type": "str",
                    "description": "Directory where local plugins are located",
                    "required": False,
                    "previous_names": ["plugins_directory", "plugin_directory"],
                    "alt_env_names": ["PLUGINS_DIRECTORY", "BG_PLUGIN_DIRECTORY"],
                },
                "timeout": {
                    "type": "dict",
                    "items": {
                        "shutdown": {
                            "type": "int",
                            "default": 10,
                            "description": "Seconds to wait for a plugin to stop"
                            "gracefully",
                            "previous_names": ["plugin_shutdown_timeout"],
                            "alt_env_names": ["PLUGIN_SHUTDOWN_TIMEOUT"],
                        },
                        "startup": {
                            "type": "int",
                            "default": 5,
                            "description": "Seconds to wait for a plugin to start",
                            "previous_names": ["plugin_startup_timeout"],
                            "alt_env_names": ["PLUGIN_STARTUP_TIMEOUT"],
                        },
                    },
                },
            },
        },
    },
}

_SCHEDULER_SPEC = {
    "type": "dict",
    "items": {
        "auth": {
            "type": "dict",
            "items": {
                "username": {
                    "type": "str",
                    "description": "Username that scheduler will use for "
                    "authentication (needs bg-admin role)",
                    "required": False,
                },
                "password": {
                    "type": "str",
                    "description": "Password that scheduler will use for "
                    "authentication (needs bg-admin role)",
                    "required": False,
                },
            },
        },
        "max_workers": {
            "type": "int",
            "default": 10,
            "description": "Number of workers (processes) to run concurrently.",
        },
        "job_defaults": {
            "type": "dict",
            "items": {
                "coalesce": {
                    "type": "bool",
                    "default": True,
                    "description": (
                        "Should jobs run only once if multiple have missed their "
                        "window"
                    ),
                },
                "max_instances": {
                    "type": "int",
                    "default": 3,
                    "description": (
                        "Default maximum instances of a job to run concurrently."
                    ),
                },
            },
        },
    },
}

_VALIDATOR_SPEC = {
    "type": "dict",
    "items": {
        "command": {
            "type": "dict",
            "items": {
                "timeout": {
                    "type": "int",
                    "default": 10,
                    "description": "Time to wait for a command-based choices validation",
                    "required": False,
                }
            },
        },
        "url": {
            "type": "dict",
            "items": {
                "ca_cert": {
                    "type": "str",
                    "description": "CA file for validating url-based choices",
                    "required": False,
                },
                "ca_verify": {
                    "type": "bool",
                    "default": True,
                    "description": "Verify external certificates for url-based choices",
                    "required": False,
                },
            },
        },
    },
}

# I have omitted the following from the spec
#
# * "backend" - there should be no need for this
# * "thrift" - there should be no need for this
#
# Everything else has been copied wholesale into this specification.
_SPECIFICATION = {
    "publish_hostname": {
        "type": "str",
        "default": "localhost",
        "description": "Publicly accessible hostname for plugins to connect to",
        "previous_names": ["amq_publish_host"],
        "alt_env_names": ["AMQ_PUBLISH_HOST"],
    },
    "mq": _MQ_SPEC,
    "application": _APP_SPEC,
    "auth": _AUTH_SPEC,
    "configuration": _META_SPEC,
    "db": _DB_SPEC,
    "entry": _ENTRY_SPEC,
    "event": _EVENT_SPEC,
    "parent": _PARENT_SPEC,
    "garden": _GARDEN_SPEC,
    "log": _LOG_SPEC,
    "metrics": _METRICS_SPEC,
    "plugin": _PLUGIN_SPEC,
    "scheduler": _SCHEDULER_SPEC,
    "validator": _VALIDATOR_SPEC,
}
