#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Stage the upstream browser build without modifying the source checkout."""
import hashlib
import json
import os
from datetime import datetime, timezone
import re
import uuid
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
stage, output = (Path(p).resolve() for p in sys.argv[1:])
version = subprocess.check_output(["emcc", "--version"], text=True).splitlines()[0]
if not version.startswith("emcc (Emscripten gcc/clang-like replacement + linker emulating GNU ld) 3.1.6 "):
    raise SystemExit(f"Expected pinned Emscripten 3.1.6, got {version}")

work = stage / "chromeApp/emscripten"
for folder in ("Examples", "Libraries", "translations", "img"):
    shutil.copytree(stage / folder, work / folder)
shutil.copytree(stage / "gp/runtime", work / "runtime")
for source in (stage / "ide").glob("*.gp"):
    shutil.copy2(source, work / "runtime/lib")
lib = work / "runtime/lib"
(lib / "MicroBlocksPatches.gp").rename(lib / "zzzMicroBlocksPatches.gp")
# Keep the complete upstream API dispatcher, adding only a thin outer bridge.
api = lib / "MicroBlocksAPI.gp"
text = api.read_text()
signature = "method dispatchCall MicroBlocksAPI callObject {"
assert text.count(signature) == 1
api.write_text(text.replace(signature, "method erbiumUpstreamDispatchCall MicroBlocksAPI callObject {"))
shutil.copy2(HERE / "bridge.gp", lib / "zzzzErbiumBridge.gp")
# Firmware flashing is not this port's responsibility. Supply empty directories
# instead of fetching unrelated board firmware from a moving download site.
for folder in ("precompiled", "esp32"):
    (work / folder).mkdir()

sources = (
    "browserPrims.c cache.c dict.c embeddedFS.c events.c gp.c httpPrims.c "
    "interp.c mem.c memGC.c oop.c parse.c pathPrims.c prims.c serialPortPrims.c "
    "sha1.c sha2.c soundPrims.c textAndFontPrims.c vectorPrims.c"
).split()
stamp = datetime.fromtimestamp(int(os.environ["SOURCE_DATE_EPOCH"]), timezone.utc)
command = [
    "emcc", "-std=gnu99", "-Wall", "-O3", "-Wno-macro-redefined",
    "-Wno-builtin-macro-redefined",
    f'-D__DATE__="{stamp.strftime("%b %e %Y")}"',
    f'-D__TIME__="{stamp.strftime("%H:%M:%S")}"',
    "-Wno-unused-but-set-variable", "-DNO_JPEG", "-DNO_SDL", "-DNO_SOCKETS",
    "-DSHA2_USE_INTTYPES_H", "-sUSE_ZLIB=1", "-sFETCH=1",
    "-sTOTAL_MEMORY=268435456", "-sALLOW_MEMORY_GROWTH=0", "-sWASM=1",
    "-sEXPORTED_RUNTIME_METHODS=HEAPU8,HEAPU32",
    "-sEXPORTED_FUNCTIONS=_main", *sources,
]
for folder in ("Examples", "Libraries", "precompiled", "esp32", "runtime", "translations", "img"):
    command.extend(("--preload-file", folder))
command.extend(("-o", "gp_wasm.js"))
subprocess.run(command, cwd=work, check=True)
if (work / "gp_wasm.wasm").stat().st_size < 100_000:
    raise SystemExit("GP interpreter unexpectedly missing from generated WASM")
# Emscripten's file_packager uses a random cache UUID. Derive that metadata
# identity from the payload instead, without changing GP/compiled WASM.
javascript = work / "gp_wasm.js"
data_digest = hashlib.sha256((work / "gp_wasm.data").read_bytes()).hexdigest()
package_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "smallvm-web:" + data_digest))
text, replaced = re.subn(r'"package_uuid":"[0-9a-f-]+"',
                        f'"package_uuid":"{package_uuid}"', javascript.read_text())
assert replaced == 1
javascript.write_text(text)
runtime_hashes = "\n".join(
    f"{hashlib.sha256((work / name).read_bytes()).hexdigest()}  {name}"
    for name in ("gp_wasm.js", "gp_wasm.wasm", "gp_wasm.data")
) + "\n"
if os.environ.get("SMALLVM_WEB_UPDATE_LOCK") != "1":
    if runtime_hashes != (HERE / "wasm.sha256").read_text():
        raise SystemExit("Generated WASM assets differ from smallvm/web/wasm.sha256.\n"
                         "Do not serve unverified assets. Review source/toolchain before updating lock.\n"
                         + runtime_hashes)

output.mkdir(parents=True, exist_ok=True)
assets = stage / "chromeApp/webapp"
for name in (
    "emModule.js", "gpSupport.js", "FileSaver.js", "ide.js", "menus.js",
    "buttons.js", "categories.js", "gettext.js", "windows.js", "graph.js",
    "flasher.js", "favicon.ico", "manifest.json",
):
    shutil.copy2(assets / name, output / name)
for name in ("styles", "icons", "lib"):
    shutil.copytree(assets / name, output / name, dirs_exist_ok=True)
for name in ("img", "translations"):
    shutil.copytree(stage / name, output / name, dirs_exist_ok=True)
for suffix in ("js", "wasm", "data"):
    shutil.copy2(work / f"gp_wasm.{suffix}", output)
shutil.copy2(stage / "LICENSE", output / "LICENSE")
shutil.copy2(stage / "Mozilla Public License, version 2.0.html", output)
zlib = Path(os.environ["EM_CACHE"]) / "ports/zlib/zlib-1.2.11/zlib.h"
notice = zlib.read_text().split("*/", 1)[0] + "*/\n"
(output / "zlib-LICENSE.txt").write_text(notice)
(output / "WASM-SHA256SUMS").write_text(runtime_hashes)
provenance = {
    "upstream": "https://codeberg.org/MicroBlocks/smallvm.git",
    "base_commit": "49f337529294640def7b27a2ee1c4e7ba88ccdbb",
    "patched_tree": "73844f74e19e41eb466ea0c727a85594902be4c1",
    "patches": "smallvm/patches/0001 through 0006",
    "toolchain": version,
    "clang": subprocess.check_output(["/usr/lib/llvm-14/bin/clang", "--version"], text=True).splitlines()[0],
    "source_date_epoch": os.environ["SOURCE_DATE_EPOCH"],
    "command": command,
    "overlays_sha256": {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (HERE / "bridge.gp", HERE / "build.py", HERE / "index.html", HERE / "erbium.js")
    },
    "notes": "Upstream GP interpreter, compiler, decompiler, startup and IDE retained. "
             "API dispatcher renamed only to add a browser testing bridge; UART JS "
             "primitives overridden by erbium.js. No upstream board flashing images.",
}
(output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
lines = []
for path in sorted(output.rglob("*")):
    if path.is_file() and path.name != "SHA256SUMS":
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(output)}")
(output / "SHA256SUMS").write_text("\n".join(lines) + "\n")
print(version)
