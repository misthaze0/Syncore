"""Drive the separately downloaded PSD2Live headless authoring service."""
from pathlib import Path
import json
import os
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
REVIEW = ROOT / 'output/pet-rig-review'
SOURCE = ROOT / 'brand/live2d/miss-jiu-v1'
sys.path.insert(0, str(Path(os.environ['TEMP']) / 'miss-jiu-psd2live-reference'))
from mcp_proxy import Client

log = (REVIEW / 'model-service.log').read_text(encoding='utf-8', errors='replace')
token = next(line.split(' ', 1)[1] for line in log.splitlines() if line.startswith('token '))
mode = sys.argv[1] if len(sys.argv) > 1 else 'inspect'
with Client('http://127.0.0.1:23879/mcp', token, timeout=90) as client:
    def call(operation, request=None):
        request = dict(request or {})
        schema, _ = client.call('workspace_get_operation', {'id': operation})
        required = schema['request_schema'].get('required', [])
        if 'state' in required or 'project_id' in required:
            context = client.project()
            if 'state' in required: request.setdefault('state', context['state'])
            if 'project_id' in required: request.setdefault('project_id', context.get('project_id'))
        if 'request_id' in required: request.setdefault('request_id', f'miss-jiu-{uuid.uuid4().hex}')
        return client.call(operation, request)
    if mode == 'schema':
        for operation in sys.argv[2:]:
            data, _ = client.call('workspace_get_operation', {'id': operation})
            (REVIEW / f'{operation}.schema.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            print(operation, 'schema saved')
    elif mode == 'generate':
        call('project_import_layout', {'path': str(SOURCE / 'miss-jiu.layout.json'), 'discard_unsaved': True})
        print('Imported original layers', flush=True)
        call('settings_update', {'changes': {'headStrength': .12, 'bodyStrength': .10, 'meshSpacing': 28,
            'seeThroughBrows': False, 'seeThroughEyes': False, 'mouthOutlineEnabled': False,
            'exportCan3': False, 'exportMotions': True, 'generatePhysics': True}})
        call('rig_generate')
        # PSD2Live 3.6.7 leaves a flattened open-mouth line visible at rest.
        # Explicit opacity keys hand off to the closed texture cleanly.
        call('keyform_apply', {'changes': [
            {'op':'set','target':'mesh:ArtMeshMouthOpen','key':{'ParamMouthOpenY':0},'channels':{'opacity':0}},
            {'op':'set','target':'mesh:ArtMeshMouthOpen','key':{'ParamMouthOpenY':.18},'channels':{'opacity':1}},
            {'op':'set','target':'mesh:ArtMeshMouthOpen','key':{'ParamMouthOpenY':1},'channels':{'opacity':1}},
            {'op':'set','target':'mesh:ArtMeshMouthClose','key':{'ParamMouthOpenY':0},'channels':{'opacity':1}},
            {'op':'set','target':'mesh:ArtMeshMouthClose','key':{'ParamMouthOpenY':.18},'channels':{'opacity':0}},
            {'op':'set','target':'mesh:ArtMeshMouthClose','key':{'ParamMouthOpenY':1},'channels':{'opacity':0}},
        ]})
        print('Generated rig', flush=True)
        call('project_save_as', {'path': str(SOURCE / 'MissJiu.psd2live')})
        call('project_export_model', {'output_directory': str(SOURCE / 'export')})
        call('project_export_psd', {'path': str(SOURCE / 'MissJiu.layers.psd'), 'include_generated_layers': False})
        print('Exported model and editable source', flush=True)
    elif mode == 'call':
        operation = sys.argv[2]
        request = json.loads(Path(sys.argv[3]).read_text(encoding='utf-8')) if len(sys.argv) > 3 else {}
        if request.pop('_edit', False): request['state'] = client.state()
        data, images = call(operation, request)
        (REVIEW / f'{operation}.result.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        for index, image in enumerate(images): (REVIEW / f'{operation}-{index}.png').write_bytes(image)
        print(json.dumps(data, ensure_ascii=False)[:3000])
    else:
        data, _ = call('workspace_overview', {'limit': 100})
        (REVIEW / 'workspace.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(data, ensure_ascii=False)[:3500])
