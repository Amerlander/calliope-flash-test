"""02_inspect: connect to the first Calliope and dump every service/char/desc.

For each characteristic: properties, max-write size hint, security flags
(via descriptor inspection — CCCD presence implies notifications).
"""

from __future__ import annotations

import asyncio

from common import (
    connect,
    find_calliope,
    fmt_uuid,
    log,
    props_short,
)


async def main() -> None:
    pair = await find_calliope()
    if not pair:
        return
    device, adv = pair
    log(f"target: {device.address}  name={device.name or adv.local_name!r}  rssi={adv.rssi}")

    async with connect(device) as client:
        # bleak doesn't expose ATT MTU directly on the Windows backend.
        # The closest signal is the characteristic's `max_write_without_response_size`
        # — Windows reports this after MTU negotiation.
        for service in client.services:
            log(f"")
            log(f"SERVICE  {service.uuid}  ({fmt_uuid(str(service.uuid))})")
            for char in service.characteristics:
                # Bleak's CharacteristicProperties is a sortable list of strings.
                props = props_short(char.properties)
                # On Windows backend, max_write_without_response_size reflects MTU-3.
                mwwr = getattr(char, "max_write_without_response_size", None)
                extra = f"  mwwr={mwwr}" if mwwr is not None else ""
                log(f"  CHAR    {char.uuid}  ({fmt_uuid(str(char.uuid))})  [{props}]{extra}")
                for desc in char.descriptors:
                    log(f"    DESC  {desc.uuid}  handle={desc.handle}")


if __name__ == "__main__":
    asyncio.run(main())
