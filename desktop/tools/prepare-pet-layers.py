"""Prepare the approved Syncore artwork for a small-angle Live2D rig.

Non-destructive: source art is read only. The user explicitly authorized local
Python image processing for this model. Coordinates are in the original PNG.
"""
from pathlib import Path
import json
import os
import sys

sys.path.insert(0, str(Path(os.environ.get('TEMP', '.')) / 'miss-jiu-model-tools/python-libs'))
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'brand/live2d/miss-jiu-v1'
PARTS = OUT / 'parts'
PARTS.mkdir(parents=True, exist_ok=True)
SOURCE = ROOT / 'frontend/public/assistant-pet.png'
source = Image.open(SOURCE).convert('RGBA')
W, H = source.size
assert (W, H) == (1086, 1448), 'Review layer coordinates before changing the source image.'
pixels = np.array(source)
alpha = pixels[:, :, 3].astype(np.float32) / 255
rgb = pixels[:, :, :3]
expression = None
expression_path = OUT / 'expressions-source.png'
if expression_path.exists():
    reference = np.array(source.crop((430,0,640,205)).resize((1050,1025)))[:, :, :3]
    generated = np.array(Image.open(expression_path).convert('RGBA'))[:, :, :3]
    sift = cv2.SIFT_create(nfeatures=2500)
    key_a, desc_a = sift.detectAndCompute(cv2.cvtColor(generated, cv2.COLOR_RGB2GRAY), None)
    key_b, desc_b = sift.detectAndCompute(cv2.cvtColor(reference, cv2.COLOR_RGB2GRAY), None)
    pairs = cv2.BFMatcher().knnMatch(desc_a, desc_b, k=2)
    matches = [a for a, b in pairs if a.distance < .72 * b.distance]
    assert len(matches) > 15, 'Expression registration needs manual review.'
    transform, inliers = cv2.estimateAffinePartial2D(np.float32([key_a[m.queryIdx].pt for m in matches]), np.float32([key_b[m.trainIdx].pt for m in matches]), method=cv2.RANSAC, ransacReprojThreshold=4)
    assert int(inliers.sum()) > 15, 'Expression registration has too few reliable landmarks.'
    aligned = cv2.warpAffine(generated, transform, (1050,1025), flags=cv2.INTER_LANCZOS4)
    Image.fromarray(aligned).save(ROOT / 'output/pet-rig-review/expression-aligned.png')
    expression = np.copy(rgb)
    expression[:205, 430:640] = np.array(Image.fromarray(aligned).resize((210,205), Image.Resampling.LANCZOS))
layers = []
review = Image.new('RGBA', source.size)


def polygon(points, blur=0):
    mask = Image.new('L', source.size)
    ImageDraw.Draw(mask).polygon(points, fill=255)
    return mask.filter(ImageFilter.GaussianBlur(blur)) if blur else mask


def stroke(points, width=2, blur=0.4):
    scale = 4
    mask = Image.new('L', (W * scale, H * scale))
    ImageDraw.Draw(mask).line([(int(x * scale), int(y * scale)) for x, y in points], fill=255, width=round(width * scale), joint='curve')
    mask = mask.resize(source.size, Image.Resampling.LANCZOS)
    return mask.filter(ImageFilter.GaussianBlur(blur)) if blur else mask


def ellipse(box):
    mask = Image.new('L', (W * 2, H * 2))
    ImageDraw.Draw(mask).ellipse(tuple(round(v * 2) for v in box), fill=255)
    return mask.resize(source.size, Image.Resampling.LANCZOS)


def add(name, mask, tags, color=None):
    mask_array = np.array(mask).astype(np.float32) / 255
    image = np.empty_like(pixels)
    image[:, :, :3] = rgb if color is None else color
    image[:, :, 3] = np.uint8(np.clip(mask_array * alpha, 0, 1) * 255)
    full = Image.fromarray(image)
    box = full.getbbox()
    if not box:
        raise ValueError(name)
    part = full.crop(box)
    part.save(PARTS / f'{name}.png')
    layers.append({'id': name, 'name': name, 'image': {'path': f'parts/{name}.png', 'width': part.width, 'height': part.height},
                   'rect': {'left': box[0], 'top': box[1], 'width': part.width, 'height': part.height}, 'tags': tags})
    review.alpha_composite(full)
    return mask_array


# Back-to-front order. Hair roots overlap the head cap, hiding seams on small turns.
left_hair = polygon([(479,126),(490,163),(482,194),(461,220),(428,233),(402,234),(391,266),(380,310),(380,350),(391,398),(423,454),(431,475),(411,497),(388,473),(364,442),(345,409),(332,384),(332,359),(348,332),(371,298),(380,259),(399,226),(433,195),(458,159)])
right_hair = polygon([(591,123),(611,163),(634,198),(662,217),(687,221),(704,242),(704,277),(686,294),(671,277),(660,252),(634,234),(614,226),(596,206),(584,176)])
right_back = polygon([(711,296),(733,332),(749,366),(758,410),(750,450),(733,479),(718,493),(711,475),(721,442),(717,414),(704,389),(689,374),(692,345)])
hair_masks = [left_hair, right_hair, right_back]
hair_union = np.maximum.reduce([np.array(mask) for mask in hair_masks]) / 255
head_region = np.zeros((H, W), dtype=np.float32)
head_region[:173] = 1
body_mask = np.clip(1 - np.maximum(head_region, hair_union), 0, 1)

# Extend the body beneath the chin and along cut boundaries. Only hidden overlap
# is synthesized; all visible features are taken from the approved source.
body_alpha = (body_mask * alpha * 255).astype(np.uint8)
body_extended = cv2.dilate(body_alpha, np.ones((7, 7), np.uint8), iterations=1)
body_extended[:160] = 0
body_extended[160:180, :485] = body_alpha[160:180, :485]
body_extended[160:180, 591:] = body_alpha[160:180, 591:]
body_fill = cv2.inpaint(rgb, np.uint8((body_extended > body_alpha) * 255), 3, cv2.INPAINT_TELEA)
add('body', Image.fromarray(body_extended), ['top'], body_fill)
add('back_hair_r', left_hair, ['hair', 'back', 'right'])
add('back_hair_l', right_back, ['hair', 'back', 'left'])
add('front_hair_l', right_hair, ['hair', 'front', 'left'])

face = polygon([(515,66),(539,61),(560,68),(576,79),(582,96),(590,116),(585,135),(573,152),(552,168),(538,174),(521,169),(501,153),(487,132),(479,112),(482,98),(496,84)])
left_eye = polygon([(487,99),(496,98),(509,100),(520,104),(524,109),(516,113),(502,112),(491,108)], .35)
right_eye = polygon([(545,108),(553,103),(567,100),(581,101),(586,105),(581,111),(568,115),(554,116),(546,113)], .35)
left_brow = polygon([(489,87),(499,87),(513,91),(528,96),(530,100),(517,98),(503,94),(491,92)], .55)
right_brow = polygon([(549,98),(560,94),(574,91),(585,92),(586,96),(575,96),(563,98),(549,103)], .55)
upper_lip = polygon([(518,141),(529,141),(537,142),(544,143),(552,146),(551,149),(539,149),(528,147),(518,146)], .35)
lower_lip = polygon([(519,146),(532,147),(542,147),(553,146),(551,152),(542,157),(532,156),(523,152)], .4)
glasses = np.maximum.reduce([np.array(stroke(points, 1.8, .25)) for points in [
    [(483,98),(489,95),(501,96),(515,100),(527,104),(533,110),(531,118),(523,123),(510,125),(496,123),(486,118),(482,109),(483,98)],
    [(549,109),(555,102),(566,99),(578,98),(588,99),(591,105),(590,117),(582,126),(569,129),(558,126),(551,119),(549,109)],
    [(532,110),(536,107),(542,108),(550,111)], [(474,98),(484,101)], [(589,101),(597,96)],
]])
feature_union = np.maximum.reduce([np.array(mask) for mask in [left_eye, right_eye, left_brow, right_brow, upper_lip, lower_lip]])
clean_face = cv2.inpaint(rgb, np.uint8(feature_union > 150) * 255, 3, cv2.INPAINT_TELEA)
# Fit a smooth skin surface from the clear forehead and cheeks. It is only
# exposed beneath moving facial features; this avoids dark lash-colour smears.
yy, xx = np.mgrid[:H, :W]
sx, sy = (xx - 538) / 70, (yy - 115) / 70
samples = (((yy >= 76) & (yy <= 87) & (xx >= 515) & (xx <= 563)) |
           ((yy >= 122) & (yy <= 141) & (((xx >= 500) & (xx <= 519)) | ((xx >= 558) & (xx <= 576)))))
terms = [np.ones_like(sx), sx, sy, sx * sy, sx * sx, sy * sy]
fit = np.linalg.lstsq(np.stack([term[samples] for term in terms], axis=1), rgb[samples].astype(float), rcond=None)[0]
skin = np.uint8(np.clip(sum(term[:, :, None] * coefficient for term, coefficient in zip(terms, fit)), 0, 255))
eye_brow_union = np.maximum.reduce([np.array(mask) for mask in [left_eye, right_eye, left_brow, right_brow]])
clean_face[eye_brow_union > 150] = skin[eye_brow_union > 150]
expanded_face = cv2.dilate(np.array(face), np.ones((19, 19), np.uint8))
face_overlap = (expanded_face > 0) & (np.array(face) < 30)
clean_face[face_overlap] = rgb[face_overlap]
add('face', Image.fromarray(expanded_face), ['face'], clean_face)

# White shapes are independently clipped masks for the pupils, allowing gaze.
for side, eye, iris_box, lash, lower in [
    ('r', left_eye, (500,99,513,113), [(488,100),(496,100),(505,102),(515,103),(522,107)], [(494,108),(506,111),(518,110)]),
    ('l', right_eye, (558,100,572,116), [(547,109),(554,105),(565,103),(577,103),(584,104)], [(551,113),(562,115),(575,112)]),
]:
    white = np.copy(rgb)
    region = np.array(ellipse(iris_box)) > 150
    white[region] = (238, 222, 219)
    add(f'eyewhite_{side}', eye, ['eye', 'white', 'left' if side == 'l' else 'right'], white)
    add(f'iris_{side}', ellipse(iris_box), ['eye', 'iris', 'left' if side == 'l' else 'right'])
    add(f'eyelash_lower_{side}', stroke(lower, 1.3), ['eye', 'lash', 'lower', 'left' if side == 'l' else 'right'])
    add(f'eyelash_{side}', stroke(lash, 3, .2), ['eye', 'lash', 'left' if side == 'l' else 'right'])
    closed = skin.copy()
    # Upper lash becomes a gentle closed-lid curve. The glasses stay separate.
    closed_image = Image.fromarray(closed)
    drawing = ImageDraw.Draw(closed_image)
    points = [(490,105),(497,108),(505,110),(514,110),(522,107)] if side == 'r' else [(547,110),(556,113),(565,114),(574,112),(583,107)]
    drawing.line(points, fill=(62,27,35), width=2, joint='curve')
    add(f'eye_close_{side}', eye, ['eye', 'closed', 'left' if side == 'l' else 'right'], expression if expression is not None else np.array(closed_image))

add('eyebrow_r', left_brow, ['brow', 'right'])
add('eyebrow_l', right_brow, ['brow', 'left'])

# A real mouth interior beneath the original lip paint; the rigger closes it
# at ParamMouthOpenY=0 and opens it continuously for audio-driven speech.
mouth_mask = np.maximum(np.array(upper_lip), np.array(lower_lip))
add('mouth_close', Image.fromarray(mouth_mask), ['mouth', 'closed'])
open_paint = np.copy(rgb)
open_paint[143:155, 521:552] = (72,19,36)
open_paint[139:146, 517:554] = rgb[141:148, 517:554]
open_paint[150:159, 517:554] = rgb[147:156, 517:554]
open_mask = cv2.dilate(mouth_mask, np.ones((7,3), np.uint8))
add('mouth_open', Image.fromarray(open_mask), ['mouth', 'open'], expression if expression is not None else open_paint)
add('eyewear', Image.fromarray(glasses.astype(np.uint8)), ['eyewear'])

# Keep the original crown/temples and ears above the separate skin and eyes.
cap = np.zeros((H, W), dtype=np.float32)
cap[:185] = 1
cap *= 1 - np.array(face).astype(np.float32) / 255
cap[173:185, 489:592] = 0
add('front_hair_cap', Image.fromarray(np.uint8(cap * 255)), ['headwear'])

manifest = {'format': 'psd2live-layout', 'version': 1, 'name': 'Syncore · 基础动态',
            'canvas': {'width': W, 'height': H}, 'layers': layers}
(OUT / 'miss-jiu.layout.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
review.save(OUT / 'layers-preview.png')
review.crop((450,50,620,185)).resize((850,675)).save(ROOT / 'output/pet-rig-review/layers-face.png')
print(json.dumps({'layers': len(layers), 'manifest': str(OUT / 'miss-jiu.layout.json')}, ensure_ascii=False))
