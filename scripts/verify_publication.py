"""Check immutable archives, backend provenance and high-confidence secret patterns."""
from pathlib import Path
import hashlib
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
snapshot = json.loads((ROOT / 'backend/source-manifest.json').read_text())
for name, expected in snapshot['files'].items():
    assert digest(ROOT / 'backend' / name) == expected, f'Original backend changed: {name}'
prior = json.loads((ROOT / 'docs/verification/publication-manifest.json').read_text())
protected = [f for f in prior['files'] if f['path'].startswith(('public/', 'docs/cases/', 'docs/images/'))]
for f in protected:
    assert digest(ROOT / f['path']) == f['sha256'], f'Archive changed: {f["path"]}'
names = subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'], cwd=ROOT).decode().split('\0')
patterns = [r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', r'\bgh[pousr]_[A-Za-z0-9]{25,}', r'\bgithub_pat_[A-Za-z0-9_]{30,}', r'\bpat_[A-Za-z0-9]{25,}', r'\bsk-(?:proj-)?[A-Za-z0-9_-]{35,}', r'postgres(?:ql)?://[^\s:]+:(?!ci-only-disposable@)[^\s${}:@]+@']
for name in set(filter(None, names)):
    path = ROOT / name
    assert path.name == '.env.example' or not path.name.startswith('.env'), f'Environment file: {name}'
    assert 'backend/assets/opportunity/' not in name, f'Runtime task data: {name}'
    assert not re.search(r'\.(dump|sqlite3?|cookies|pem|key)$', name), f'Private runtime file: {name}'
    if path.suffix in ('.png','.jpg','.pdf','.docx'):
        continue
    content = path.read_text(errors='replace')
    for pattern in patterns:
        assert not re.search(pattern, content), f'Possible credential in {name}; inspect locally without printing value'
print(json.dumps({'backend_source_files_verified':len(snapshot['files']), 'immutable_public_files_verified':len(protected), 'forbidden_files_or_secret_patterns':0, 'model_calls':0},ensure_ascii=False))
