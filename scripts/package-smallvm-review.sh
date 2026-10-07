#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Inspection bundle: matching source, patches, firmware and test evidence.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
B=${SMALLVM_BUILD:-"$R/build/smallvm"}
S=${SMALLVM_SRC:-"$R/ext/smallvm"}
O=${1:-"$R/build/smallvm-review.tar.gz"}
python3 - "$R" "$B" "$S" "$O" <<'PY'
import hashlib, io, json, pathlib, subprocess, sys, tarfile
root, build, src, output = map(pathlib.Path, sys.argv[1:])
manifest_path = build/'results/manifest.json'
manifest = json.loads(manifest_path.read_text())
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
if manifest.get('suite') != 'full':
    sys.exit('Review packaging requires a full test run, not --host-only')
for name, digest in manifest['port_sources'].items():
    if sha(root/name) != digest: sys.exit(f'Port/build/test source changed since tests: {name}')
actual_tree = subprocess.check_output(['git','-C',str(src),'rev-parse','HEAD^{tree}'],text=True).strip()
if actual_tree != manifest['source_tree'] or subprocess.check_output(['git','-C',str(src),'status','--porcelain']):
    sys.exit('Source changed since tests; rerun the test suite before packaging')
for name, digest in manifest['artifacts'].items():
    if sha(build/name) != digest: sys.exit(f'Artifact changed since tests: {name}')
for name, digest in manifest['patches'].items():
    if sha(root/'smallvm/patches'/name) != digest: sys.exit(f'Patch changed since tests: {name}')
for name, digest in manifest['results'].items():
    if sha(build/name) != digest: sys.exit(f'Result changed since tests: {name}')
for name in ('smallvm.elf','smallvm-selftest.elf'):
    if not (build/name).is_file(): sys.exit(f'Missing artifact {name}; run full suite')
paths = {}
tracked = subprocess.check_output(['git','-C',str(root),'ls-files','smallvm'],text=True).splitlines()
for name in tracked: paths['repo/'+name] = root/name
for name in ('fetch-smallvm.sh','sync-git-patches.sh','build-smallvm.sh','test-smallvm.sh','package-smallvm-review.sh',
             'setup-smallvm-ide.sh','test-smallvm-ide.sh',
             'fetch-smallvm-web.sh','setup-smallvm-web.sh','test-smallvm-web.sh'):
    paths['repo/scripts/'+name] = root/'scripts'/name
for p in (src/'vm').rglob('*'):
    if p.is_file(): paths['sources/smallvm/'+str(p.relative_to(src))] = p
paths['licenses/SmallVM-MPL-2.0.html'] = src/'Mozilla Public License, version 2.0.html'
paths['licenses/SmallVM-LICENSE'] = src/'LICENSE'
paths['licenses/picolibc-copyright'] = root/'build/smallvm/deps/usr/share/doc/picolibc-riscv64-unknown-elf/copyright'
paths['licenses/gcc-riscv64-unknown-elf-copyright'] = pathlib.Path('/usr/share/doc/gcc-riscv64-unknown-elf/copyright')
gcc_notice = pathlib.Path('/usr/share/doc/gcc-riscv64-unknown-elf/copyright-gcc.gz')
if gcc_notice.is_file(): paths['licenses/gcc-copyright.gz'] = gcc_notice
for name in manifest['artifacts']: paths['artifacts/'+name] = build/name
for p in (build/'results').rglob('*'):
    if p.is_file() and p.suffix != '.img': paths['results/'+str(p.relative_to(build/'results'))] = p
paths['README.md'] = root/'smallvm/README.md'
for name, p in paths.items():
    if not p.is_file(): sys.exit(f'Missing review input: {p}')
output.parent.mkdir(parents=True,exist_ok=True)
with tarfile.open(output,'w:gz') as archive:
    for name,p in sorted(paths.items()): archive.add(p, arcname='smallvm-review/'+name, recursive=False)
    hashes = ''.join(f'{sha(p)}  {name}\n' for name,p in sorted(paths.items())).encode()
    info = tarfile.TarInfo('smallvm-review/SHA256SUMS'); info.size = len(hashes)
    archive.addfile(info,io.BytesIO(hashes))
print(f'{output} ({output.stat().st_size} bytes)\nsha256={sha(output)}')
PY
