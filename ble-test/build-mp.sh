#!/bin/bash
set -e
SRC=/mnt/c/GIT/Calliope/LLM/FIRMWARE/micropython-calliope-mini-v3
DST=~/mp-build
# sync codal libs
for lib in codal-core codal-nrf52 codal-microbit-nrf5sdk codal-microbit-v2; do
    rm -rf $DST/lib/codal/libraries/$lib
    cp -r $SRC/lib/codal/libraries/$lib $DST/lib/codal/libraries/
done
# sync codal_app sources (main.cpp + microbithal etc) and codal.json
cp -r $SRC/src/codal_app $DST/src/
cd $DST/src
rm -rf build $DST/lib/codal/build
echo "starting build…"
make 2>&1 | tail -25
