#!/usr/bin/env python3
"""
Declarative dynamic DNS with gandi.net.

Example config.ini:

    [default]

    gandi_api_key = $GANDI_API_KEY
    wan_device = enp2s0
    notify_script = /usr/local/bin/pushover  # takes a single arg

    [foobar.org]

    # blank value will be filled in with running host's IP.
    A, @
    A, mail
    CNAME, bmon = some-host.lan.

    # values are CSVs for separate records with the same name.
    MX, @ = 10 one-val.com., 20 other-val.com.

    [hmmmm.com]

    A, @

"""

import argparse
import subprocess
import io
import re
import os
import csv
import json
import typing as t
import sys
import pprint
import configparser
import urllib.error
from pathlib import Path
from urllib import request
from dataclasses import dataclass, field


# Optionally specify API key via envvar (vs. config file).
ENV_APIKEY = os.environ.get("GANDI_APIKEY")
HTTP_IP_SOURCE = "https://ifconfig.me"

# Default locations for the configuration file.
CONFIG_SEARCH_PATH = [
    Path(i)
    for i in filter(
        None,
        [
            os.environ.get("GANDI_DDNS_CONFIG"),
            Path.home() / ".config" / "gandi-ddns.ini",
            "/etc/gandi-ddns/config.ini",
        ],
    )
]


@dataclass
class Record:
    type: str
    name: str
    val: t.Optional[t.List[str]] = None


@dataclass
class Config:
    # The name of the device that accesses the WAN; used to get our IP using the
    # `ip` command.
    wan_device: str

    gandi_api_key: t.Optional[str] = None

    records: t.Dict[str, t.List[Record]] = field(default_factory=dict)

    # Location to a script that is called with a single argument (message) to notify
    # of an event.
    notify_script: t.Optional[str] = None


# Modified in `main`.
GLOBAL_CONF = Config("")


def die(msg: str):
    print(msg, file=sys.stderr)
    sys.exit(1)


def split_csv_line(line: str) -> t.List[str]:
    row = list(csv.reader([line]))[0]
    return [i.strip() for i in row]


def get_config(
    file_handle: io.TextIOWrapper | None = None, location: Path | None = None
) -> Config:
    if not (file_handle or location):
        raise ValueError("must specify config location")
    if location:
        if not location.exists():
            die(f"config file not readable: {location}")
        file_handle = open(location, "r")

    cp = configparser.ConfigParser(allow_no_value=True)
    assert file_handle
    cp.read_file(file_handle)

    config = Config("")
    config.wan_device = cp.get("default", "wan_device")
    config.notify_script = cp.get("default", "notify_script", fallback=None)
    config.gandi_api_key = cp.get('default', 'gandi_api_key', fallback=None)

    for name, section in cp.items():
        name = name.lower()
        if name == "default":
            continue
        config.records[name] = []

        for reckey, val in section.items():
            [rectype, recname] = split_csv_line(reckey)
            rectype = rectype.upper()
            vals = split_csv_line(val) if val else None

            config.records[name].append(Record(rectype, recname, vals))

    return config


def gandi_req(url, data=None, method="GET") -> t.Optional[dict]:
    api_key = ENV_APIKEY or GLOBAL_CONF.gandi_api_key
    assert api_key
    req_headers = {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
    }

    if data:
        data = json.dumps(data).encode()
    req = request.Request(url=url, data=data, headers=req_headers, method=method)
    try:
        response = request.urlopen(req).read().decode()
    except urllib.error.HTTPError as e:
        data = e.read()
        print(f"failed request to {url} ({e.code}):\n{data}")
        try:
            # Attempt to return the error dict.
            return json.loads(data.decode())
        except Exception as e:
            print(e)
            return None
    else:
        return json.loads(response)


def send_notification(msg: str):
    if GLOBAL_CONF.notify_script:
        subprocess.run(
            f'{GLOBAL_CONF.notify_script} "{msg}"', capture_output=True, shell=True
        )


def get_local_ip() -> str:
    assert GLOBAL_CONF.wan_device
    got = subprocess.check_output(
        f"ip addr show dev {GLOBAL_CONF.wan_device}", shell=True
    ).decode()
    local_ip = re.search(r"inet ((\d{1,3}\.){3}[^/]+)", got).groups()[0]
    net_ip = request.urlopen(HTTP_IP_SOURCE).read().decode()

    if local_ip != net_ip:
        print(mismatch := f"IP mismatch: {local_ip} vs. {net_ip}")
        send_notification(mismatch)

    return local_ip


def get_domain_data(domain: str):
    return gandi_req(f"https://dns.api.gandi.net/api/v5/domains/{domain}")


def print_records(domain: str):
    pprint.pprint(gandi_req(get_domain_data(domain)["zone_records_href"]))


def main():
    global GLOBAL_CONF
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-c",
        "--conf",
        action="store_const",
        help="Location of ini config file to use",
    )
    args = parser.parse_args()

    if args.conf:
        GLOBAL_CONF = get_config(location=Path(args.conf))
    else:
        found = False
        for confpath in CONFIG_SEARCH_PATH:
            if confpath.exists():
                GLOBAL_CONF = get_config(location=confpath)
                found = True

        if not found:
            die(
                f"no config file found in {', '.join(str(i) for i in CONFIG_SEARCH_PATH)}"
            )

    if not GLOBAL_CONF.wan_device:
        die("config default.wan_device must be specified")

    local_ip = get_local_ip()
    print(f"Local IP: {local_ip}")

    for domain, records in GLOBAL_CONF.records.items():
        domain_data = get_domain_data(domain)
        assert domain_data
        zone_href = domain_data["zone_records_href"]

        for record in records:
            if not record.val:
                record.val = [local_ip]

            if record.type == "PTR":
                record.name = f"{local_ip}.in-addr.arpa."
                record.val = [f"{domain}."]

            print(record)

            rec_val = None
            record_data = gandi_req(f"{zone_href}/{record.name}/{record.type}")
            assert record_data

            if record_data.get("rrset_name") == record.name:
                # Exists, may be updated
                rec_val = record_data["rrset_values"]
            elif record_data.get("code") == 404:
                # Doesn't yet exist, will be created
                pass
            else:
                print(
                    f"FAIL: unexpected response for {record}, can't update: {record_data}"
                )
                continue

            if rec_val != record.val:
                print(
                    gandi_req(
                        f"{zone_href}/{record.name}/{record.type}",
                        data={
                            "rrset_name": record.name,
                            "rrset_type": record.type,
                            "rrset_ttl": 1200,
                            "rrset_values": record.val,
                        },
                        method="PUT",
                    )
                )
                send_notification(
                    f"updating {domain} {record} from {rec_val} to {record.val}"
                )


if __name__ == "__main__":
    main()
