"""Package metadata remains compatible with system Python on RHEL 7/8."""

from pathlib import Path
import re
from setuptools import setup

root = Path(__file__).parent
version = re.search(r'__version__ = "([^"]+)"',
                    (root / "host_metering/__init__.py").read_text()).group(1)

setup(
    name="host-metering",
    version=version,
    description="Host CPU metering with subscription labels and Prometheus remote write",
    long_description=(root / "README.md").read_text(),
    long_description_content_type="text/markdown",
    url="https://github.com/RedHatInsights/host-metering",
    license="Apache-2.0",
    packages=["host_metering"],
    python_requires=">=3.6",
    extras_require={"https-proxy": ["urllib3>=1.26"]},
    entry_points={"console_scripts": ["host-metering=host_metering.cli:main"]},
)
