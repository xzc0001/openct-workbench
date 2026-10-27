"""Create local configuration without importing any site-specific settings."""
import json
import os
from pathlib import Path
import sys

root=Path(__file__).resolve().parents[1]
app=root/'workbench'
target=app/'config.json'
if target.exists():
    raise SystemExit('Local config already exists; edit it explicitly if needed.')
config=json.loads((app/'config.example.json').read_text(encoding='utf-8'))
try:
    config['python']=os.path.relpath(sys.executable,app).replace('\\','/')
except ValueError:
    # Windows permits the interpreter and checkout to live on different drives.
    # This machine-specific path stays in ignored local config, never the example.
    config['python']=sys.executable
target.write_text(json.dumps(config,indent=2)+'\n',encoding='utf-8')
for path in [app/'jobs',app/'cache',root/'auth',root/'run',root/'weights']:
    path.mkdir(exist_ok=True)
    if os.name!='nt':path.chmod(0o700)
print('Local configuration created. Review model weights before running inference.')
