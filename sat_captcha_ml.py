#!/usr/bin/env python3
"""
SAT captcha solver.

This module trains a small local character classifier on synthetic samples that
roughly mimic the SAT "bubble" captcha style and uses Hough circle detection to
segment each character bubble at inference time.
"""

from __future__ import annotations

import functools
import hashlib
import math
import random
from pathlib import Path

import cv2
import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from torch import nn


LABELS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LABEL_TO_INDEX = {label: idx for idx, label in enumerate(LABELS)}
MODEL_PATH = Path(__file__).with_name("sat_captcha_model.pt")
IMAGE_SIZE = 48
CAPTCHA_LEN = 6
FONT_PATTERNS = (
    "Arial",
    "Verdana",
    "Tahoma",
    "Trebuchet",
    "Helvetica",
    "Futura",
    "GillSans",
    "Geneva",
    "Avenir",
    "Courier",
)
FONT_DIRS = (
    Path("/System/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
    Path("/Library/Fonts"),
    Path.home() / "Library/Fonts",
)
CIRCLE_COLORS = [
    (28, 25, 48),
    (129, 19, 46),
    (131, 143, 18),
    (113, 79, 133),
    (30, 72, 137),
    (109, 80, 53),
    (69, 74, 61),
]
DOT_COLORS = [
    (53, 218, 102),
    (118, 83, 201),
    (76, 210, 218),
    (108, 138, 82),
    (171, 218, 45),
    (40, 115, 185),
    (186, 82, 176),
    (141, 205, 160),
]


def _device() -> torch.device:
    return torch.device("cpu")


def _font_candidates() -> list[str]:
    fonts: list[str] = []
    seen: set[str] = set()
    for root in FONT_DIRS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in {".ttf", ".otf", ".ttc"}:
                continue
            name = path.name.lower().replace(" ", "")
            if not any(pattern.lower().replace(" ", "") in name for pattern in FONT_PATTERNS):
                continue
            value = str(path)
            if value in seen:
                continue
            try:
                ImageFont.truetype(value, 28)
            except Exception:
                continue
            seen.add(value)
            fonts.append(value)
    if not fonts:
        raise RuntimeError("No se encontraron fuentes compatibles para entrenar el solver.")
    return fonts[:18]


def _font_signature(fonts: list[str]) -> str:
    digest = hashlib.sha256()
    for item in fonts:
        digest.update(item.encode("utf-8"))
    return digest.hexdigest()[:16]


class BubbleCaptchaNet(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(3, 32, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
            nn.Conv2d(64, 96, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
            nn.Conv2d(96, 128, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.AdaptiveAvgPool2d((1, 1)),
        )
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(128, 128),
            nn.ReLU(inplace=True),
            nn.Dropout(0.15),
            nn.Linear(128, len(LABELS)),
        )

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return self.classifier(self.features(inputs))


def _load_font(fonts: list[str], rng: random.Random) -> ImageFont.FreeTypeFont:
    for _ in range(10):
        candidate = rng.choice(fonts)
        size = rng.randint(28, 38)
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    raise RuntimeError("No fue posible cargar una fuente valida.")


def _draw_circle(draw: ImageDraw.ImageDraw, center: tuple[int, int], radius: int, color: tuple[int, int, int]) -> None:
    x, y = center
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)


def _draw_character(
    canvas: Image.Image,
    label: str,
    center: tuple[int, int],
    radius: int,
    font: ImageFont.FreeTypeFont,
    rng: random.Random,
) -> None:
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    bbox = draw.textbbox((0, 0), label, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    x = center[0] - width / 2 + rng.randint(-2, 2)
    y = center[1] - height / 2 - 1 + rng.randint(-2, 2)
    shade = rng.randint(235, 255)
    draw.text((x, y), label, font=font, fill=(shade, shade, shade, 255))
    rotated = layer.rotate(rng.uniform(-10.0, 10.0), resample=Image.Resampling.BICUBIC)
    canvas.alpha_composite(rotated)


def _synthetic_sample(label: str, fonts: list[str], rng: random.Random) -> np.ndarray:
    width, height = 112, 80
    image = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)

    target_center = (rng.randint(42, 54), rng.randint(28, 42))
    target_radius = rng.randint(14, 19)
    neighbors: list[tuple[str, tuple[int, int], int]] = []
    for direction in (-1, 1):
        if rng.random() < 0.9:
            neighbors.append(
                (
                    rng.choice(LABELS),
                    (
                        target_center[0] + direction * rng.randint(18, 30),
                        target_center[1] + rng.randint(-6, 6),
                    ),
                    rng.randint(12, 20),
                )
            )

    shapes = neighbors[:]
    shapes.append((label, target_center, target_radius))
    rng.shuffle(shapes)

    for current_label, center, radius in shapes:
        color = rng.choice(CIRCLE_COLORS)
        _draw_circle(draw, center, radius, color)
        font = _load_font(fonts, rng)
        _draw_character(image, current_label, center, radius, font, rng)

    for _ in range(rng.randint(6, 16)):
        radius = rng.randint(2, 8)
        center = (rng.randint(0, width), rng.randint(0, height))
        draw.ellipse(
            (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
            fill=rng.choice(DOT_COLORS),
        )

    rgb = image.convert("RGB")
    if rng.random() < 0.35:
        rgb = rgb.filter(ImageFilter.GaussianBlur(radius=rng.uniform(0.2, 0.8)))

    left = max(0, target_center[0] - target_radius - 6)
    top = max(0, target_center[1] - target_radius - 6)
    right = min(width, target_center[0] + target_radius + 7)
    bottom = min(height, target_center[1] + target_radius + 7)
    crop = rgb.crop((left, top, right, bottom)).resize((IMAGE_SIZE, IMAGE_SIZE), Image.Resampling.BICUBIC)

    if rng.random() < 0.2:
        patch = np.array(crop)
        noise = rng.randint(4, 12)
        ys = rng.choices(range(IMAGE_SIZE), k=noise)
        xs = rng.choices(range(IMAGE_SIZE), k=noise)
        patch[ys, xs] = 255 - patch[ys, xs]
        crop = Image.fromarray(patch)

    return np.asarray(crop, dtype=np.float32) / 255.0


def _train_and_save(model_path: Path = MODEL_PATH) -> Path:
    fonts = _font_candidates()
    metadata_path = model_path.with_suffix(".meta")
    signature = _font_signature(fonts)
    if model_path.exists() and metadata_path.exists():
        if metadata_path.read_text(encoding="utf-8").strip() == signature:
            return model_path

    rng = random.Random(1337)
    device = _device()
    model = BubbleCaptchaNet().to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    loss_fn = nn.CrossEntropyLoss()
    batch_size = 128
    steps = 32

    model.train()
    for _epoch in range(3):
        for _step in range(steps):
            batch_x = np.zeros((batch_size, 3, IMAGE_SIZE, IMAGE_SIZE), dtype=np.float32)
            batch_y = np.zeros((batch_size,), dtype=np.int64)
            for idx in range(batch_size):
                label = rng.choice(LABELS)
                sample = _synthetic_sample(label, fonts, rng)
                batch_x[idx] = np.transpose(sample, (2, 0, 1))
                batch_y[idx] = LABEL_TO_INDEX[label]

            inputs = torch.from_numpy(batch_x).to(device)
            targets = torch.from_numpy(batch_y).to(device)
            optimizer.zero_grad(set_to_none=True)
            logits = model(inputs)
            loss = loss_fn(logits, targets)
            loss.backward()
            optimizer.step()

    model_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), model_path)
    metadata_path.write_text(signature, encoding="utf-8")
    return model_path


def _dedupe_circles(circles: np.ndarray, expected: int = CAPTCHA_LEN) -> list[tuple[int, int, int]]:
    ordered = sorted((tuple(map(int, item)) for item in circles), key=lambda item: (-item[2], item[0]))
    kept: list[tuple[int, int, int]] = []
    for x, y, radius in ordered:
        duplicate = False
        for ox, oy, oradius in kept:
            distance = math.hypot(x - ox, y - oy)
            if distance < min(radius, oradius) * 0.85:
                duplicate = True
                break
        if not duplicate:
            kept.append((x, y, radius))
    kept.sort(key=lambda item: item[0])
    return kept[:expected]


def _segment_bubbles(img_bytes: bytes, expected: int = CAPTCHA_LEN) -> list[np.ndarray]:
    array = np.frombuffer(img_bytes, dtype=np.uint8)
    decoded = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if decoded is None:
        raise ValueError("No se pudo decodificar la imagen del captcha.")

    rgb = cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)
    gray = cv2.cvtColor(decoded, cv2.COLOR_BGR2GRAY)
    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=16,
        param1=50,
        param2=12,
        minRadius=8,
        maxRadius=24,
    )
    if circles is None:
        raise ValueError("No se detectaron burbujas en el captcha.")

    bubbles = _dedupe_circles(np.round(circles[0]).astype(int), expected=expected)
    if len(bubbles) < max(4, expected - 1):
        raise ValueError(f"Se detectaron solo {len(bubbles)} burbujas del captcha.")

    crops: list[np.ndarray] = []
    for x, y, radius in bubbles:
        pad = 4
        x1 = max(0, x - radius - pad)
        y1 = max(0, y - radius - pad)
        x2 = min(rgb.shape[1], x + radius + pad + 1)
        y2 = min(rgb.shape[0], y + radius + pad + 1)
        patch = rgb[y1:y2, x1:x2].copy()
        mask = np.zeros((patch.shape[0], patch.shape[1]), dtype=np.uint8)
        cv2.circle(mask, (x - x1, y - y1), max(2, radius - 1), 255, -1)
        patch[mask == 0] = 255
        resized = cv2.resize(patch, (IMAGE_SIZE, IMAGE_SIZE), interpolation=cv2.INTER_AREA)
        crops.append(resized.astype(np.float32) / 255.0)
    return crops


@functools.lru_cache(maxsize=1)
def _load_model() -> BubbleCaptchaNet:
    model_path = _train_and_save(MODEL_PATH)
    model = BubbleCaptchaNet()
    state = torch.load(model_path, map_location="cpu")
    model.load_state_dict(state)
    model.eval()
    return model


def solve_captcha(img_bytes: bytes) -> tuple[str, list[float]]:
    crops = _segment_bubbles(img_bytes, expected=CAPTCHA_LEN)
    model = _load_model()
    batch = np.stack([np.transpose(crop, (2, 0, 1)) for crop in crops], axis=0)
    with torch.no_grad():
        logits = model(torch.from_numpy(batch))
        probs = torch.softmax(logits, dim=1)
    indices = probs.argmax(dim=1).tolist()
    confidences = probs.max(dim=1).values.tolist()
    text = "".join(LABELS[index] for index in indices)
    return text, confidences
