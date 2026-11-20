#!/usr/bin/env python3
"""Rebuild the pinned Harness flock addon for Linux x64 hosts without AVX.

The adapter loads a separate baseline flock addon; official binaries, Landlock
and permission policy remain unchanged. Run after installing SDK 0.2.2rc2.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


def main() -> None:
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise SystemExit('This compatibility repair only applies to Linux x64.')
    if importlib.metadata.version('deepseek-harness-sdk') != '0.2.2rc2':
        raise SystemExit('Repair is pinned to SDK 0.2.2rc2; inspect newer releases separately.')
    if ' avx ' in ' ' + Path('/proc/cpuinfo').read_text().replace('\n', ' ') + ' ':
        print('AVX is available; no repair needed.')
        return
    node = shutil.which('node')
    if not node or not shutil.which('cc'):
        raise SystemExit('Node.js headers and a C compiler are required.')
    node_root = Path(node).resolve().parents[1]
    includes = [node_root / 'include/node', Path('/usr/include/node'), Path('/usr/local/include/node')]
    headers = next((path for path in includes if (path / 'node_api.h').is_file()), None)
    if headers is None:
        raise SystemExit('Cannot find node_api.h for the installed Node.js.')
    cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'devwisp/harness'
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / 'flock-0.1.2-baseline.node'
    marker = target.with_suffix('.sha256')
    if marker.is_file() and target.is_file() and marker.read_text().strip() == hashlib.sha256(target.read_bytes()).hexdigest():
        print('Baseline flock addon is already installed.')
        return
    with urllib.request.urlopen('https://registry.npmjs.org/@deepseek-ai/node-addon-system/0.1.2', timeout=30) as response:
        package = json.load(response)
    with urllib.request.urlopen(package['dist']['tarball'], timeout=30) as response:
        archive = response.read()
    integrity = 'sha512-' + base64.b64encode(hashlib.sha512(archive).digest()).decode()
    if integrity != 'sha512-EFn8K+mXIXiw6s3rxX+cgp2hZTEAQdVIsURhrXnBxycSXvtQ1EQpsSVzoNKtKAQI0I0VqkEcVEjHzS/PeaGsvw==' or integrity != package['dist']['integrity']:
        raise SystemExit('Official package integrity mismatch.')
    with tempfile.TemporaryDirectory(prefix='devwisp-harness-flock-') as directory:
        root = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as bundle:
            (root / 'flock.c').write_bytes(bundle.extractfile('package/src/flock.c').read())
        built = root / 'system.node'
        subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-march=x86-64', '-mtune=generic',
                        '-mno-avx', '-mno-avx2', '-DNAPI_VERSION=8', '-I', str(headers),
                        str(root / 'flock.c'), '-o', str(built)], check=True)
        probe = root / 'probe.cjs'
        probe.write_text("""const fs = require('node:fs');
const addon = require(process.argv[2]);
const path = process.argv[3];
const a = fs.openSync(path, 'w'); const b = fs.openSync(path, 'r+');
addon.tryLock(a, (first) => {
  if (first !== 0) process.exitCode = 1;
  addon.tryLock(b, (second) => {
    if (second === 0) process.exitCode = 1;
    fs.closeSync(a); fs.closeSync(b);
  });
});
""")
        subprocess.run([node, str(probe), str(built), str(root / 'probe.lock')], check=True, timeout=10)
        staged = target.with_suffix('.staged')
        try:
            shutil.copy2(built, staged)
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
        marker.write_text(hashlib.sha256(target.read_bytes()).hexdigest() + '\n')
    print('Installed baseline x86-64 flock addon; lock acquisition and contention passed. Official binary unchanged.')


if __name__ == '__main__':
    main()
