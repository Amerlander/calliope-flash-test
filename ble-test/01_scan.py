"""01_scan: passive scan listing every Calliope-like adv we hear in 8s."""

from __future__ import annotations

import asyncio

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from common import is_calliope_adv, log


async def main() -> None:
    seen: dict[str, dict] = {}

    def on_adv(d: BLEDevice, adv: AdvertisementData) -> None:
        if not is_calliope_adv(d, adv):
            return
        info = seen.get(d.address, {
            "name_seen": set(),
            "service_uuids": set(),
            "rssi": adv.rssi,
            "first_seen": None,
            "count": 0,
        })
        info["count"] += 1
        info["rssi"] = adv.rssi
        if d.name:
            info["name_seen"].add(d.name)
        if adv.local_name:
            info["name_seen"].add(adv.local_name)
        for s in adv.service_uuids or []:
            info["service_uuids"].add(str(s).lower())
        seen[d.address] = info

    log("scanning 8s…")
    scanner = BleakScanner(detection_callback=on_adv)
    await scanner.start()
    try:
        await asyncio.sleep(8.0)
    finally:
        await scanner.stop()

    if not seen:
        log("nothing matched")
        return

    log(f"found {len(seen)} unique Calliope-ish address(es):")
    for addr, info in sorted(seen.items(), key=lambda kv: -kv[1]["rssi"]):
        names = ", ".join(sorted(info["name_seen"])) or "<unnamed>"
        suuids = ", ".join(sorted(info["service_uuids"])) or "<no service uuid in adv>"
        log(f"  {addr}  rssi={info['rssi']:4d}  adv#={info['count']:3d}  name=[{names}]")
        log(f"    service_uuids: {suuids}")


if __name__ == "__main__":
    asyncio.run(main())
