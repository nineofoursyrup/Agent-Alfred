from pathlib import Path
import hashlib
import json
import shutil
import subprocess
from io import BytesIO
from PIL import Image, ImageChops

root = Path.cwd()
evidence = root / 'docs/implementation/dashboard-claude-design/evidence/V11'
repair = evidence / 'ci-repair'
capture = root / '.scratch/v11/ci-repair/fixed-viewport-capture'
failure = repair / 'capture-collection-first-failure'
failure.mkdir(exist_ok=True)
old_head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
record = {'previous_capture_head': old_head, 'strip_height': 87,
          'cause': 'UNKNOWN: old screenshot collection repeated the first 87 pixels at the bottom; PNG dimensions alone did not detect it.',
          'remedy': 'Separate headless browser contexts with fixed viewport; verify innerWidth, innerHeight, PNG dimensions and pixel strips.',
          'previous_images': {}, 'comparison_attempts': []}
for path in sorted(evidence.glob('*.png')):
    raw = subprocess.check_output(['git', 'show', f'{old_head}:{path.relative_to(root)}'])
    with Image.open(BytesIO(raw)) as image:
        width, height = image.size
        repeated = ImageChops.difference(image.crop((0, 0, width, 87)), image.crop((0, height-87, width, height))).getbbox() is None
    record['previous_images'][path.name] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                                          'width': width, 'height': height, 'bottom_87_equals_top_87': repeated}
    if path.name in ('inbox-1280x800.png', 'reference-loop-320x800.png'):
        (failure / path.name).write_bytes(raw)
for name in ('capture', 'runs-proof', 'reference-capture', 'image-manifest'):
    path = evidence / f'{name}.json'
    (failure / f'{name}-previous.json').write_bytes(subprocess.check_output(['git', 'show', f'{old_head}:{path.relative_to(root)}']))
for number, folder in [(1, 'fixed-viewport-first-comparison'), (2, 'fixed-viewport-second-comparison')]:
    source = root / '.scratch/v11/ci-repair' / folder
    destination = failure / f'comparison-{number}'
    destination.mkdir(exist_ok=True)
    pixels = []
    with Image.open(source / 'runs-1440x900.png') as after, Image.open(source / 'runs-before-1440x900.png') as before:
        after = after.convert('RGB'); before = before.convert('RGB')
        box = ImageChops.difference(after, before).getbbox()
        if box:
            for y in range(box[1], box[3]):
                for x in range(box[0], box[2]):
                    if after.getpixel((x, y)) != before.getpixel((x, y)):
                        pixels.append({'x': x, 'y': y, 'after': after.getpixel((x, y)), 'before': before.getpixel((x, y))})
    for path in source.glob('*.png'):
        shutil.copyfile(path, destination / path.name)
    record['comparison_attempts'].append({'attempt': number, 'different_pixels': pixels,
                                         'region': 'MainBar compositor edges; precise rendering cause UNKNOWN',
                                         'same_accessibility': True})
(failure / 'record.json').write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n')

data = json.loads((capture / 'capture.json').read_text())
proof = json.loads((capture / 'runs-proof.json').read_text())
for item in proof['results']:
    key = f"{item['width']}x{item['height']}"
    before = (capture / f'runs-before-{key}.png').read_bytes()
    after = (capture / f'runs-{key}.png').read_bytes()
    assert before == after and item['same_accessibility']
    item.update(png_bytes_equal=True, before_sha256=hashlib.sha256(before).hexdigest(), after_sha256=hashlib.sha256(after).hexdigest())
data['runs_proof'] = proof
assert len(data['snapshots']) == 12
assert all(not item['overflow'] and item['inline_styles'] == 0 and item['selected_session'] == 'v11-session-1' for item in data['snapshots'])
for name, value in [('capture', data), ('runs-proof', proof)]:
    for base in (evidence, repair):
        (base / f'{name}.json').write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
reference = json.loads((capture / 'reference-capture.json').read_text())
assert not reference['errors'] and all(item['status'] == 200 for item in reference['responses'])
(evidence / 'reference-capture.json').write_text(json.dumps(reference, ensure_ascii=False, indent=2)+'\n')
manifest = {}
for path in sorted(capture.glob('*.png')):
    raw = path.read_bytes()
    with Image.open(path) as image:
        width, height = image.size
        repeated = ImageChops.difference(image.crop((0, 0, width, 87)), image.crop((0, height-87, width, height))).getbbox() is None
    assert not repeated
    shutil.copyfile(path, evidence / path.name)
    manifest[path.name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'width': width, 'height': height,
                           'bytes': len(raw), 'bottom_87_equals_top_87': repeated}
assert len(manifest) == 24
(evidence / 'image-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
for name in ('recapture.mjs', 'reference-recapture.mjs', 'archive-recapture.py'):
    shutil.copyfile(root / '.scratch/v11/ci-repair' / name, repair / 'scripts' / name)
results_path = repair / 'final-results.json'
results = json.loads(results_path.read_text())
results['fixed_viewport_capture'] = {'script': 'scripts/recapture.mjs', 'reference_script': 'scripts/reference-recapture.mjs',
                                   'previous_record': 'capture-collection-first-failure/record.json',
                                   'source_changed': False, 'formal_tests_changed': False,
                                   'snapshots': 12, 'all_four_runs_png_pairs_equal': True}
for path in (repair / 'gates').glob('*.json'):
    if path.name in ('ci-repair-layout-main.json', 'ci-repair-layout-before.json'):
        continue  # These diagnostics intentionally ran before the padding repair.
    gate = json.loads(path.read_text())
    if 'inputs' not in gate:
        continue
    assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == value for name, value in gate['inputs'].items()), path
results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2)+'\n')
print('Archived 24 fixed-viewport PNGs, complete prior identity, two strip examples and both MainBar comparison failures. Gate inputs unchanged.')
