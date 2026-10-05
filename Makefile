#!/usr/bin/make -f

PYTHON ?= python3
PROJECT := host-metering
VERSION := $(shell $(PYTHON) tools/build.py version)
NEXT_VERSION ?=
SHORT_COMMIT ?= $(shell git rev-parse --short=8 HEAD)
AUTORELEASE ?= git$(shell date "+%Y%m%d%H%M")G$(SHORT_COMMIT)
UBI_VERSION ?= 9
DISTDIR ?= $(CURDIR)/dist
RPMTOPDIR := $(DISTDIR)/rpmbuild
MOCKS_DIR := $(CURDIR)/mocks
COMPOSE := podman-compose -f .devcontainer/docker-compose.yml

.PHONY: test test-ff build build-selinux cert test-daemon
test:
	$(PYTHON) -m unittest discover -s tests -v

test-ff:
	$(PYTHON) -m unittest discover -s tests -f

build:
	$(PYTHON) tools/build.py build --distdir "$(DISTDIR)"

build-selinux:
	$(MAKE) -C contrib/selinux -f /usr/share/selinux/devel/Makefile $(PROJECT).pp

cert:
	@if [ ! -s mocks/consumer/cert.pem ] || [ ! -s mocks/consumer/key.pem ]; then \
		cd mocks && ./create-cert.sh; \
	fi

ifdef IS_IN_CONTAINER
PROMETHEUS_ADDRESS := http://prometheus:9090/api/v1/write
PROMETHEUS_TARGET :=
else
PROMETHEUS_ADDRESS := http://localhost:9090/api/v1/write
PROMETHEUS_TARGET := prometheus
endif

test-daemon: cert build $(PROMETHEUS_TARGET)
	PATH="$(MOCKS_DIR):$(PATH)" HOST_METERING_WRITE_URL=$(PROMETHEUS_ADDRESS) \
	$(PYTHON) $(DISTDIR)/$(PROJECT) --config .devcontainer/host-metering.conf daemon

.PHONY: podman-containers prometheus prometheus-stop promtool-check-config clean-pod
podman-containers:
	podman build -t devcontainer_host-metering --build-arg UBI_VERSION=$(UBI_VERSION) -f .devcontainer/Dockerfile .

prometheus:
	$(COMPOSE) up -d prometheus

prometheus-stop:
	$(COMPOSE) stop prometheus

podman-%:
	$(COMPOSE) run --rm -u root host-metering $(MAKE) $(subst podman-,,$@)

promtool-check-config:
	$(COMPOSE) run --rm promtool

clean-pod:
	$(COMPOSE) down

.PHONY: version version-update tarball rpm/spec rpm/srpm rpm rpm/mock rpm/mock-8 rpm/mock-9 mock mock-8 mock-9
version:
	@$(PYTHON) tools/build.py version

version-update:
	$(PYTHON) tools/build.py version-update --version "$(NEXT_VERSION)"

tarball:
	$(PYTHON) tools/build.py tarball --distdir "$(DISTDIR)"

rpm/spec:
	$(PYTHON) tools/build.py spec --release "$(AUTORELEASE)"

rpm/srpm: tarball rpm/spec
	mkdir -p $(RPMTOPDIR)/SOURCES
	cp $(DISTDIR)/$(PROJECT)-$(VERSION).tar.gz $(RPMTOPDIR)/SOURCES/
	rpmbuild --define '_topdir $(RPMTOPDIR)' -bs contrib/rpm/$(PROJECT).spec

rpm: rpm/srpm
	rpmbuild --define '_topdir $(RPMTOPDIR)' -bb contrib/rpm/$(PROJECT).spec

rpm/mock: rpm/srpm
	mock -r contrib/rpm/epel-7-x86_64-custom.cfg --resultdir=$(DISTDIR)/mock7 \
	--rebuild $(RPMTOPDIR)/SRPMS/$(PROJECT)-$(VERSION)-$(AUTORELEASE)*.src.rpm

rpm/mock-8: rpm/srpm
	mock -r centos-stream-8-x86_64 --resultdir=$(DISTDIR)/mock8 \
	--rebuild $(RPMTOPDIR)/SRPMS/$(PROJECT)-$(VERSION)-$(AUTORELEASE)*.src.rpm

rpm/mock-9: rpm/srpm
	mock -r centos-stream-9-x86_64 --resultdir=$(DISTDIR)/mock9 \
	--rebuild $(RPMTOPDIR)/SRPMS/$(PROJECT)-$(VERSION)-$(AUTORELEASE)*.src.rpm

mock: rpm/mock
mock-8: rpm/mock-8
mock-9: rpm/mock-9

.PHONY: clean clean-node clean-all
clean:
	rm -rf -- "$(DISTDIR)" build host_metering.egg-info coverage.out coverage.html coverage.txt

clean-node:
	rm -rf -- node_modules

clean-all: clean clean-pod clean-node
