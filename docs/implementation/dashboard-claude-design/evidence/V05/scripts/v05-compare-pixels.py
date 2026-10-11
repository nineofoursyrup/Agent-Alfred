"""Verify native pixel side-by-side evidence without editing images."""
from pathlib import Path
import hashlib, json, struct, zlib

root = Path('docs/implementation/dashboard-claude-design/evidence/V05')

def pixels(path):
    data = path.read_bytes()
    position, packed = 8, bytearray()
    while position < len(data):
        size = struct.unpack('>I', data[position:position + 4])[0]
        kind = data[position + 4:position + 8]
        body = data[position + 8:position + 8 + size]
        if kind == b'IHDR':
            width, height, depth, color, _, _, interlace = struct.unpack('>IIBBBBB', body)
            assert depth == 8 and color in (2, 6) and interlace == 0
        if kind == b'IDAT':
            packed.extend(body)
        position += size + 12
    channels = 3 if color == 2 else 4
    stride, raw = width * channels, zlib.decompress(packed)
    prior, rows, cursor = bytearray(stride), [], 0
    for _ in range(height):
        method = raw[cursor]
        row = bytearray(raw[cursor + 1:cursor + 1 + stride])
        cursor += stride + 1
        for index in range(stride):
            left = row[index - channels] if index >= channels else 0
            up = prior[index]
            corner = prior[index - channels] if index >= channels else 0
            if method == 0:
                continue
            if method == 1:
                value = left
            elif method == 2:
                value = up
            elif method == 3:
                value = (left + up) // 2
            elif method == 4:
                predictor = left + up - corner
                distances = abs(predictor - left), abs(predictor - up), abs(predictor - corner)
                value = (left, up, corner)[distances.index(min(distances))]
            else:
                raise ValueError(method)
            row[index] = (row[index] + value) & 255
        prior = row
        if channels == 4:
            row = bytearray(value for index, value in enumerate(row) if index % 4 != 3)
        rows.append(bytes(row))
    return width, height, rows

records = []
for meta in ('comparisons.json', 'process-comparisons.json'):
    for pair in json.loads((root / meta).read_text()):
        width, height, left = pixels(root / pair['left'])
        right_width, right_height, right = pixels(root / pair['right'])
        composite_width, composite_height, composite = pixels(root / pair['file'])
        assert (right_width, right_height) == (width, height)
        assert (composite_width, composite_height) == (width * 2, height)
        mismatch = sum(a != b[:width * 3] or c != b[width * 3:] for a, b, c in zip(left, composite, right))
        records.append({'file': pair['file'], 'width': composite_width, 'height': height, 'mismatching_rows': mismatch, 'sha256': hashlib.sha256((root / pair['file']).read_bytes()).hexdigest()})
(root / 'comparison-pixel-check.json').write_text(json.dumps(records, indent=2) + '\n')
print(json.dumps(records, indent=2))
assert all(record['mismatching_rows'] == 0 for record in records)
