"""Present the actual desktop renderer recording with a face close-up."""
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
REVIEW = ROOT / 'output/pet-review'
recording = json.loads((REVIEW / 'gesture-recording.json').read_text(encoding='utf-8'))
font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 21)
small = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 12)
title = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 25)
frames = []
for item in recording:
    source = Image.open(REVIEW / 'gesture-frames' / item['filename']).convert('RGBA')
    frame = Image.new('RGBA', (640, 510), '#f7f2ee')
    draw = ImageDraw.Draw(frame)
    draw.text((22, 14), 'Syncore · 互动动作', font=font, fill='#65273d')
    draw.text((420, 23), '桌面助手实际运行录制', font=small, fill='#9b7b83')
    frame.alpha_composite(source.resize((300, 441), Image.Resampling.LANCZOS), (0, 58))
    sx, sy = source.width / 340, source.height / 500
    face = source.crop(tuple(round(v) for v in (124 * sx, 57 * sy, 215 * sx, 156 * sy)))
    frame.alpha_composite(face.resize((273, 297), Image.Resampling.LANCZOS), (332, 132))
    draw.text((350, 78), item['title'], font=title, fill='#65273d')
    draw.text((350, 451), '轻点头发、裙摆，或打开动作面板', font=small, fill='#85646f')
    frames.append(frame.convert('RGB'))
assert frames, 'Record the desktop gestures first.'
destination = REVIEW / 'MissJiu-interactions-preview.gif'
durations = [max(80, min(500, recording[i + 1].get('capturedAt', 100) - recording[i].get('capturedAt', 0))) for i in range(len(recording) - 1)] + [100]
# A shared palette avoids color flicker between frames and keeps the preview small.
palette = frames[40 if len(frames) > 40 else 0].quantize(colors=128)
gif_frames = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
gif_frames[0].save(destination, save_all=True, append_images=gif_frames[1:], duration=durations, loop=0, optimize=True)
# A contact sheet makes closed eyes, hair overlap and end poses reviewable.
indices = [0, 40, 66, 97, 125, len(frames) - 1]
sheet = Image.new('RGB', (960, 510), '#f7f2ee')
for position, index in enumerate(indices):
    sheet.paste(frames[min(index, len(frames) - 1)].resize((320, 255), Image.Resampling.LANCZOS), ((position % 3) * 320, (position // 3) * 255))
sheet.save(REVIEW / 'interactions-contact-sheet.png')
print(f'{destination}: {len(frames)} frames, {destination.stat().st_size:,} bytes')
