"""Publish the runtime bundle only; editable artwork stays under brand/."""
from pathlib import Path
import json
import shutil

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'brand/live2d/miss-jiu-v1/export'
DEST = ROOT / 'frontend/public/models/miss-jiu'
DEST.mkdir(parents=True, exist_ok=True)
model = json.loads((SOURCE / 'miss-jiu.layout.model3.json').read_text(encoding='utf-8'))
references = model['FileReferences']
paths = [references['Moc'], *references['Textures']]
paths += [references[key] for key in ['Physics', 'DisplayInfo', 'Pose', 'UserData'] if key in references]
paths += [entry['File'] for entry in references.get('Expressions', [])]
paths += [entry['File'] for entries in references.get('Motions', {}).values() for entry in entries]
for relative in paths:
    source = (SOURCE / relative).resolve()
    source.relative_to(SOURCE.resolve())
    dest = DEST / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
(DEST / 'MissJiu.model3.json').write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding='utf-8')
print(f'Published {len(paths) + 1} files, {sum(file.stat().st_size for file in DEST.rglob("*") if file.is_file())} bytes')
