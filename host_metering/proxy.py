"""Optional RHSM proxy defaults for the systemd service."""

import configparser
import ipaddress
import os
import re


def apply_rhsm_proxy(path, environ=None):
    """Fill missing HTTP(S) proxy settings without overriding either spelling.

    Return startup warnings; missing files or unused RHSM proxy settings are
    normal. Only [server] is used, and NO_PROXY is always left intact.
    """
    environ = os.environ if environ is None else environ
    missing = [key for key in ("HTTP_PROXY", "HTTPS_PROXY")
               if not (environ.get(key) or environ.get(key.lower()))]
    if not missing:
        return []
    parser = configparser.ConfigParser(interpolation=None, strict=False,
                                        inline_comment_prefixes=("#", ";"))
    try:
        with open(path, encoding="utf-8") as stream:
            parser.read_file(stream)
        hostname = parser.get("server", "proxy_hostname", fallback="").strip()
        port = parser.get("server", "proxy_port", fallback="").strip()
        if not hostname or not port:
            return []
        if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
            raise ValueError("proxy_port must be an integer from 1 to 65535")
        if any(char.isspace() for char in hostname) or any(char in hostname for char in "/@?#"):
            raise ValueError("proxy_hostname must be a hostname or IP address")
        if ":" in hostname:
            address = hostname[1:-1] if hostname.startswith("[") and hostname.endswith("]") else hostname
            hostname = "[{}]".format(ipaddress.IPv6Address(address))
        proxy = "http://{}:{}".format(hostname, int(port))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError, configparser.Error, ValueError) as exc:
        return ["Unable to read RHSM proxy configuration {}: {}".format(path, exc)]
    for key in missing:
        environ[key] = proxy
    return []
