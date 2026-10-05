"""Read logical CPU count and subscription-manager's English output."""

import logging
import os
import re
import subprocess

LOG = logging.getLogger("host_metering")


class BillingInfo:
    def __init__(self, model="", marketplace="", account="", instance_id=""):
        self.model = model
        self.marketplace = marketplace
        self.account = account
        self.instance_id = instance_id


class HostInfo:
    def __init__(self, cpu_count=0, host_id="", host_name="", external_organization="",
                 socket_count="", product=None, support="", usage="",
                 conversions_success="", billing=None):
        self.cpu_count = cpu_count
        self.host_id = host_id
        self.host_name = host_name
        self.external_organization = external_organization
        self.socket_count = socket_count
        self.product = [] if product is None else list(product)
        self.support = support
        self.usage = usage
        self.conversions_success = conversions_success
        self.billing = billing if billing is not None else BillingInfo()

    def labels(self, send_hostname=True):
        values = {
            "__name__": "system_cpu_logical_count",
            "_id": self.host_id,
            "billing_marketplace": self.billing.marketplace,
            "billing_marketplace_account": self.billing.account,
            "billing_marketplace_instance_id": self.billing.instance_id,
            "billing_model": self.billing.model,
            "conversions_success": self.conversions_success,
            "display_name": self.host_name if send_hostname else "",
            "external_organization": self.external_organization,
            "product": ",".join(self.product),
            "socket_count": self.socket_count,
            "support": self.support,
            "usage": self.usage,
        }
        return sorted((name, value) for name, value in values.items() if value)


def get_cpu_count(path="/proc/cpuinfo"):
    # procfs counts online logical CPUs, independently of affinity/cgroup limits.
    with open(path, encoding="utf-8") as stream:
        count = sum(bool(re.match(r"^processor\s*:\s*[0-9]+\s*$", line, re.I))
                    for line in stream)
    if not count:
        raise ValueError("No logical processors found in " + path)
    return count


def parse_output(output):
    """Return case-insensitive keys and all values (products can repeat)."""
    result = {}
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")) or ":" not in line:
            continue
        key, value = line.split(":", 1)
        result.setdefault(key.strip().lower(), []).append(value.strip())
    return result


def last_value(values, key):
    matches = values.get(key.lower(), [])
    if not matches:
        LOG.warning("Unable to get subscription info: %s not found", key)
        return ""
    return matches[-1]


def billing_info(facts):
    for cloud, account_key in (("aws", "aws_account_id"),
                               ("azure", "azure_subscription_id"),
                               ("gcp", "gcp_project_number")):
        instance_key = cloud + "_instance_id"
        if instance_key in facts:
            return BillingInfo("marketplace", cloud, last_value(facts, account_key),
                               last_value(facts, instance_key))
    LOG.warning("Unsupported or missing marketplace values")
    return BillingInfo()


class SubManInfoProvider:
    def __init__(self, cpu_counter=get_cpu_count, command="subscription-manager"):
        self.cpu_counter = cpu_counter
        self.command = command

    def _command(self, *args):
        environ = dict(os.environ, LANG="C.UTF-8", LC_ALL="C.UTF-8")
        try:
            result = subprocess.run(
                [self.command] + list(args), env=environ, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, universal_newlines=True, timeout=30,
            )
            if result.returncode:
                LOG.error("subscription-manager %s failed: %s", " ".join(args),
                          result.stderr.strip())
                return {}
            return parse_output(result.stdout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.error("subscription-manager %s failed: %s", " ".join(args), exc)
            return {}

    def load(self):
        cpu_count = self.cpu_counter()
        identity = self._command("identity")
        usage = self._command("usage")
        support = self._command("service-level")
        facts = self._command("facts")
        products = self._command("list", "--installed")
        return HostInfo(
            cpu_count=cpu_count,
            host_id=last_value(identity, "system identity"),
            host_name=last_value(identity, "name"),
            external_organization=last_value(identity, "org ID"),
            socket_count=last_value(facts, "cpu.cpu_socket(s)"),
            product=products.get("product id", []),
            support=last_value(support, "Current service level"),
            usage=last_value(usage, "Current Usage"),
            conversions_success=last_value(facts, "conversions.success").lower(),
            billing=billing_info(facts),
        )

    def refresh_cpu_count(self, host):
        host.cpu_count = self.cpu_counter()
