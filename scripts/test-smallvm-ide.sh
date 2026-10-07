#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Step #3: isolated real IDE/compiler tests; never reset the live desktop.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
OUT=${SMALLVM_IDE_TEST_BUILD:-"$R/build/smallvm/step3"}
EMU=${ERBIUM_EMU:-"$R/dist/bin/erbium_emu"}
SRC=${SMALLVM_SRC:-"$R/ext/smallvm"}
SRC=$(realpath -m "$SRC")
mkdir -p "$OUT"
"$R/scripts/fetch-smallvm.sh"
make -C "$R/smallvm" firmware selftest > "$OUT/firmware-build.log" 2>&1
python3 "$R/smallvm/tests/ide_integration_test.py" 2>&1 | tee "$OUT/harness.log"
/usr/bin/python3 "$R/smallvm/ide/test_frontend.py" 2>&1 | tee "$OUT/frontend.log"
/usr/bin/python3 "$R/smallvm/ide/test_relay.py" 2>&1 | tee "$OUT/relay.log"
UART_TRANSPORT_BUILD="$OUT/uart" ERBIUM_EMU="$EMU" \
  bash "$R/smallvm/tests/uart_transport.sh" --emulator 2>&1 | tee "$OUT/uart.log"
python3 "$R/smallvm/tests/ide_integration.py" --emu "$EMU" --gp "$SRC/gp/gp-linux64bit" \
  --output "$OUT/normal" > "$OUT/normal-summary.log" 2>&1
python3 "$R/smallvm/tests/ide_integration.py" --emu "$EMU" --gp "$SRC/gp/gp-linux64bit" \
  --relay-write-size 7 --output "$OUT/fragmented" > "$OUT/fragmented-summary.log" 2>&1
python3 - "$R" "$OUT" "$EMU" "$SRC" <<'PY'
import hashlib,json,pathlib,subprocess,sys
root,out,emu,src=map(pathlib.Path,sys.argv[1:])
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
checks={}
for name in ('normal','fragmented'):
    result=json.loads((out/name/'result.json').read_text())
    assert result['status']=='PASS',result
    checks[name]={key:result.get(key) for key in ('status','compiled_bytes','chunk_count','largest_chunk_bytes','downloaded_chunk_frames','readback_chunk_frames','redundant_chunk_downloads','gp_error_log_checks','elapsed_seconds','native_save_dialog_exercised','observed_serial_delay_requests')}
files=[p for p in (root/'smallvm').rglob('*') if p.is_file() and p.suffix in ('.py','.gp','.ubp','.c','.h','.S','.ld','.html','.xml','.patch')]
files += [root/'smallvm/Makefile',root/'scripts/test-smallvm-ide.sh',root/'scripts/setup-smallvm-ide.sh']
manifest={'status':'PASS','checks':checks,'emulator_sha256':sha(emu),
          'firmware_sha256':sha(root/'build/smallvm/smallvm.elf'),
          'smallvm_tree':subprocess.check_output(['git','-C',str(src),'rev-parse','HEAD^{tree}'],text=True).strip(),
          'sources':{str(p.relative_to(root)):sha(p) for p in sorted(files)}}
(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps(checks,indent=2))
PY
echo "PASS SmallVM step #3; evidence: $OUT"
