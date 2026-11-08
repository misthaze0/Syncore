from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
REVIEW = ROOT / 'output/pet-rig-review'
font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 19)
small = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 13)
frames = []
for source in sorted((REVIEW / 'frames').glob('*.png')):
    image = Image.open(source).convert('RGBA')
    frame = Image.new('RGBA', (640,470), '#f7f2ee')
    draw = ImageDraw.Draw(frame)
    draw.text((24,16), 'Syncore · Live2D 基础动态', font=font, fill='#65273d')
    body = image.resize((300,400), Image.Resampling.LANCZOS)
    frame.alpha_composite(body, (0,55))
    face = image.crop((350,12,560,235)).resize((286,304), Image.Resampling.LANCZOS)
    frame.alpha_composite(face, (325,75))
    draw.text((326,402), '眨眼 · 视线 · 发丝 · 嘴型', font=small, fill='#7b5d68')
    frames.append(frame.convert('RGB'))
assert len(frames) == 40
frames[0].save(REVIEW / 'MissJiu-live2d-preview.gif', save_all=True, append_images=frames[1:], duration=100, loop=0, optimize=False)
print(REVIEW / 'MissJiu-live2d-preview.gif')
