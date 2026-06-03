#!/usr/bin/env python3
from pathlib import Path
import hashlib, sys
root = Path(__file__).resolve().parents[1]
manifest = root / 'MANIFEST.sha256'
errors = 0
for line in manifest.read_text(encoding='utf-8').splitlines():
    if not line.strip():
        continue
    expected, rel = line.split('  ', 1)
    path = root / rel
    if not path.exists():
        print('MISSING', rel); errors += 1; continue
    h = hashlib.sha256(path.read_bytes()).hexdigest()
    if h != expected:
        print('MISMATCH', rel); errors += 1
if errors:
    sys.exit(1)
print('MANIFEST OK')
