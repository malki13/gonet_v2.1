import base64
from collections import Counter
from dataclasses import asdict
import io
import inspect
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
import pytesseract
from openai import OpenAI
from pdf2image import convert_from_bytes
from PIL import Image, ImageEnhance, ImageOps

from ..domain.models import APIError, CostEstimate, OCRApiResponse, OCRCandidate, QualityMetrics, UploadedDocument, Usage


# =============================================================================
# CONFIG
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

OPENAI_OCR_MODEL = os.getenv("OPENAI_OCR_MODEL", "gpt-4.1-mini")
OPENAI_OCR_DETAIL = os.getenv("OPENAI_OCR_DETAIL", "high")
OPENAI_ANALYZE_DETAIL = os.getenv("OPENAI_ANALYZE_DETAIL", "low")
OPENAI_INPUT_COST_PER_MILLION = float(os.getenv("OPENAI_INPUT_COST_PER_MILLION", "0.40"))
OPENAI_OUTPUT_COST_PER_MILLION = float(os.getenv("OPENAI_OUTPUT_COST_PER_MILLION", "1.60"))
OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))
OPENAI_MAX_RETRIES = int(os.getenv("OPENAI_MAX_RETRIES", "2"))

BLUR_THRESHOLD = float(os.getenv("BLUR_THRESHOLD", "90"))
BLUR_RETRY_THRESHOLD = float(os.getenv("BLUR_RETRY_THRESHOLD", "45"))
BLUR_FORCE_RETRY = float(os.getenv("BLUR_FORCE_RETRY", "40"))
EDGE_DENSITY_MIN = float(os.getenv("EDGE_DENSITY_MIN", "0.004"))
BRIGHTNESS_MIN = float(os.getenv("BRIGHTNESS_MIN", "30"))
BRIGHTNESS_MAX = float(os.getenv("BRIGHTNESS_MAX", "230"))
MIN_IMAGE_PIXELS = int(os.getenv("MIN_IMAGE_PIXELS", str(400 * 300)))
MAX_SIDE = int(os.getenv("MAX_SIDE", "1800"))
PREPROCESS_MIN_SHORT_SIDE = int(os.getenv("PREPROCESS_MIN_SHORT_SIDE", "900"))
PREPROCESS_MAX_LONG_SIDE = int(os.getenv("PREPROCESS_MAX_LONG_SIDE", "2400"))
RECEIPT_MIN_AREA_RATIO = float(os.getenv("RECEIPT_MIN_AREA_RATIO", "0.15"))
RECEIPT_MAX_AREA_RATIO = float(os.getenv("RECEIPT_MAX_AREA_RATIO", "0.98"))
QUICK_RANK_MAX_LONG_SIDE = int(os.getenv("QUICK_RANK_MAX_LONG_SIDE", "1300"))
QUICK_SCORE_CACHE_SIZE = int(os.getenv("QUICK_SCORE_CACHE_SIZE", "300"))
TOP_VARIANTS = int(os.getenv("TOP_VARIANTS", "3"))
TOP_VARIANTS_LOW_QUALITY = int(os.getenv("TOP_VARIANTS_LOW_QUALITY", "4"))
MAX_DESKEW_ANGLE = float(os.getenv("MAX_DESKEW_ANGLE", "16"))
UNSTABLE_SCORE_GAP_MAX = int(os.getenv("UNSTABLE_SCORE_GAP_MAX", "25"))
MIN_ACCEPT_SCORE = int(os.getenv("MIN_ACCEPT_SCORE", "115"))
STRONG_EXTRACTION_SCORE = int(os.getenv("STRONG_EXTRACTION_SCORE", "175"))
PDF_MAX_PAGES = int(os.getenv("PDF_MAX_PAGES", "3"))
OCR_VERBOSE_LOGS = os.getenv("OCR_VERBOSE_LOGS", "false").strip().lower() in {"1", "true", "yes", "on"}
OCR_LOG_CONTENT = os.getenv("OCR_LOG_CONTENT", "false").strip().lower() in {"1", "true", "yes", "on"}
OCR_LOG_TEXT_LIMIT = max(200, int(os.getenv("OCR_LOG_TEXT_LIMIT", "4000")))
MAX_UPLOAD_BYTES = max(1, int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))))

DATASET_DEFAULT_DIR = os.getenv(
    "DATASET_DEFAULT_DIR",
    "Comprobantes de pago/Comprobantes de pago",
)

client = OpenAI(timeout=OPENAI_TIMEOUT_SECONDS, max_retries=OPENAI_MAX_RETRIES)

SUPPORTED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}

BANK_KEYWORDS = {
    "JEP": ["JEP", "JARDIN AZUAYO", "JARDIN DEL AZUAY"],
    "PICHINCHA": ["PICHINCHA", "BANCO PICHINCHA"],
    "PACIFICO": ["PACIFICO", "BANCO DEL PACIFICO"],
    "GUAYAQUIL": ["GUAYAQUIL", "BANCO DE GUAYAQUIL"],
    "MACHALA": ["MACHALA", "BANCO DE MACHALA"],
    "PRODUBANCO": ["PRODUBANCO", "PROMERICA"],
    "COOPERATIVA": ["COOPERATIVA"],
}

MONTH_TOKEN = r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC|ENE|ABR|AGO|DIC)"
SPANISH_MONTH_ALIASES = {
    "ene": {"ENE", "JAN", "ONE", "0NE", "0N0", "ONO", "0NO", "ENE."},
    "feb": {"FEB"},
    "mar": {"MAR"},
    "abr": {"ABR", "APR"},
    "may": {"MAY"},
    "jun": {"JUN"},
    "jul": {"JUL"},
    "ago": {"AGO", "AUG"},
    "sep": {"SEP", "SEPT"},
    "oct": {"OCT"},
    "nov": {"NOV"},
    "dic": {"DIC", "DEC"},
}
JEP_TICKET_ANCHOR_PATTERNS = [
    re.compile(r"\bCOOPERATIVA\s+JEP\b|\bJEP\s+LTDA\b", flags=re.I),
    re.compile(r"\bAHORROSJ[EI]P\b", flags=re.I),
    re.compile(r"\bREALIZADO\s+POR\b", flags=re.I),
    re.compile(r"\bDEP\.?\s+EFECTIVO\b", flags=re.I),
    re.compile(r"\bBALANCE\s+OK\b", flags=re.I),
    re.compile(r"\bCTA\.?\s*\d{6,}", flags=re.I),
]
PACIFICO_PORTAL_REPORT_PATTERNS = [
    re.compile(r"\bBANCO\s+DEL?\s+PACIFICO\b|\bBANCO\s+PACIFICO\b", flags=re.I),
    re.compile(r"\bCLIENTE\s+NRO\b", flags=re.I),
    re.compile(r"\bSUC\.?\s*BANCO\b", flags=re.I),
    re.compile(r"\bRET\.?\s*BANCO\b", flags=re.I),
    re.compile(r"\bVALOR\s+AUTORIZADO\b", flags=re.I),
    re.compile(r"\bFECHA\s+INI(?:CIO)?\s+PAGO\b", flags=re.I),
    re.compile(r"\bFECHA\s+VENCIMIENTO\b", flags=re.I),
    re.compile(r"\bCUENTA\s+DEB(?:E|ITO)\b", flags=re.I),
    re.compile(r"\bCUENTA\s+CONCEPTO\b", flags=re.I),
    re.compile(r"\bMONTO\s+ADEUDADO\b", flags=re.I),
    re.compile(r"\bCONCEPTOS?\b", flags=re.I),
    re.compile(r"\bCREACION\b", flags=re.I),
    re.compile(r"\bRUTA\b", flags=re.I),
    re.compile(r"\bTRAMITADO\b", flags=re.I),
    re.compile(r"\bTRANSMITIDO\b", flags=re.I),
    re.compile(r"\bCERTIFICA(?:CION|DO)\s+BANCO\s+PACIFICO\b", flags=re.I),
    re.compile(r"\bUSUARIO\b", flags=re.I),
    re.compile(r"\bFECHA/HORA\b", flags=re.I),
]


# =============================================================================
# HELPERS
# =============================================================================


def _new_trace_id() -> str:
    return uuid.uuid4().hex[:8]


def _vlog(msg: str, *args: Any) -> None:
    if OCR_VERBOSE_LOGS:
        logger.info(msg, *args)


def _truncate_for_log(value: Any, limit: Optional[int] = None) -> str:
    selected_limit = OCR_LOG_TEXT_LIMIT if limit is None else max(200, int(limit))
    text = str(value or "")
    if len(text) <= selected_limit:
        return text
    remaining = len(text) - selected_limit
    return f"{text[:selected_limit]}...<truncated {remaining} chars>"


def _json_for_log(payload: Any) -> str:
    try:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        serialized = json.dumps(str(payload), ensure_ascii=False)
    return _truncate_for_log(serialized)


def log_debug_payload(event: str, payload: Dict[str, Any]) -> None:
    if not OCR_LOG_CONTENT:
        return
    logger.info("%s payload=%s", event, _json_for_log(payload))


_quick_score_cache: Dict[int, int] = {}


def _empty_usage() -> Usage:
    return Usage(0, 0, 0)


def _add_usage(a: Usage, b: Usage) -> Usage:
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        total_tokens=a.total_tokens + b.total_tokens,
    )


def _estimate_cost(usage: Usage) -> CostEstimate:
    input_cost = (usage.input_tokens / 1_000_000.0) * OPENAI_INPUT_COST_PER_MILLION
    output_cost = (usage.output_tokens / 1_000_000.0) * OPENAI_OUTPUT_COST_PER_MILLION
    return CostEstimate(
        input_cost=round(input_cost, 6),
        output_cost=round(output_cost, 6),
        total_cost=round(input_cost + output_cost, 6),
        input_rate_per_million=OPENAI_INPUT_COST_PER_MILLION,
        output_rate_per_million=OPENAI_OUTPUT_COST_PER_MILLION,
    )


def _usage_from_response(response: Any) -> Usage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return _empty_usage()

    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", input_tokens + output_tokens) or 0)
    return Usage(input_tokens, output_tokens, total_tokens)


def _image_to_data_url(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _safe_parse_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    text = text.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None

    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _pil_to_cv(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _cv_to_pil(cv_img: np.ndarray) -> Image.Image:
    return Image.fromarray(cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB))


def _open_image_bytes(image_bytes: bytes) -> Optional[Image.Image]:
    try:
        return Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return None


def _normalize_size(img: Image.Image, max_side: int = MAX_SIDE) -> Image.Image:
    w, h = img.size
    if max(w, h) <= max_side:
        return img
    scale = max_side / float(max(w, h))
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    return img.resize((nw, nh), Image.Resampling.LANCZOS)


def _pil_to_jpeg_bytes(img: Image.Image, quality: int = 94) -> bytes:
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def _upscale_for_text_detail(cv_img: np.ndarray) -> np.ndarray:
    h, w = cv_img.shape[:2]
    if h <= 0 or w <= 0:
        return cv_img

    short_side = min(h, w)
    long_side = max(h, w)
    scale = 1.0
    if short_side < PREPROCESS_MIN_SHORT_SIDE:
        scale = PREPROCESS_MIN_SHORT_SIDE / float(short_side)

    if long_side * scale > PREPROCESS_MAX_LONG_SIDE:
        scale = PREPROCESS_MAX_LONG_SIDE / float(long_side)

    if scale <= 1.02:
        _vlog("preprocess.upscale skipped h=%s w=%s scale=1.0", h, w)
        return cv_img

    out = cv2.resize(cv_img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    oh, ow = out.shape[:2]
    _vlog("preprocess.upscale h=%s w=%s -> h=%s w=%s scale=%.3f", h, w, oh, ow, scale)
    return out


def _order_quad_points(points: np.ndarray) -> np.ndarray:
    pts = np.asarray(points, dtype=np.float32).reshape(4, 2)
    ordered = np.zeros((4, 2), dtype=np.float32)

    sums = pts.sum(axis=1)
    ordered[0] = pts[np.argmin(sums)]
    ordered[2] = pts[np.argmax(sums)]

    diffs = np.diff(pts, axis=1).reshape(-1)
    ordered[1] = pts[np.argmin(diffs)]
    ordered[3] = pts[np.argmax(diffs)]
    return ordered


def _warp_from_quad(cv_img: np.ndarray, quad: np.ndarray) -> Optional[np.ndarray]:
    rect = _order_quad_points(quad)
    tl, tr, br, bl = rect

    width_a = float(np.linalg.norm(br - bl))
    width_b = float(np.linalg.norm(tr - tl))
    height_a = float(np.linalg.norm(tr - br))
    height_b = float(np.linalg.norm(tl - bl))
    out_w = max(1, int(round(max(width_a, width_b))))
    out_h = max(1, int(round(max(height_a, height_b))))

    if out_w < 120 or out_h < 120:
        return None

    dst = np.array(
        [
            [0, 0],
            [out_w - 1, 0],
            [out_w - 1, out_h - 1],
            [0, out_h - 1],
        ],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(rect, dst)
    warped = cv2.warpPerspective(
        cv_img,
        matrix,
        (out_w, out_h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )

    if warped.shape[1] > int(warped.shape[0] * 1.2):
        warped = cv2.rotate(warped, cv2.ROTATE_90_CLOCKWISE)

    return warped


def _find_receipt_quad(cv_img: np.ndarray) -> Optional[np.ndarray]:
    h, w = cv_img.shape[:2]
    if h <= 0 or w <= 0:
        return None

    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    image_area = float(h * w)
    if image_area <= 0:
        return None

    maps: List[np.ndarray] = []
    edges = cv2.Canny(blur, 40, 140)
    edges = cv2.dilate(edges, np.ones((3, 3), dtype=np.uint8), iterations=2)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((7, 7), dtype=np.uint8), iterations=1)
    maps.append(edges)

    adaptive = cv2.adaptiveThreshold(
        blur,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        41,
        9,
    )
    adaptive = cv2.morphologyEx(adaptive, cv2.MORPH_CLOSE, np.ones((9, 9), dtype=np.uint8), iterations=2)
    maps.append(adaptive)

    best_quad: Optional[np.ndarray] = None
    best_score = -1.0
    best_area_ratio = 0.0
    best_rectangularity = 0.0

    for prepared in maps:
        contours_data = cv2.findContours(prepared, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = contours_data[0] if len(contours_data) == 2 else contours_data[1]
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:60]:
            area = float(cv2.contourArea(contour))
            if area <= 0:
                continue

            area_ratio = area / image_area
            if area_ratio < RECEIPT_MIN_AREA_RATIO or area_ratio > RECEIPT_MAX_AREA_RATIO:
                continue

            _, _, cw, ch = cv2.boundingRect(contour)
            if cw < int(w * 0.35) or ch < int(h * 0.35):
                continue

            peri = cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
            if len(approx) == 4:
                quad = approx.reshape(4, 2).astype(np.float32)
            else:
                rect = cv2.minAreaRect(contour)
                quad = cv2.boxPoints(rect).astype(np.float32)

            bbox_area = float(cw * ch)
            rectangularity = area / max(1.0, bbox_area)
            if rectangularity < 0.45:
                continue

            center = np.mean(quad, axis=0)
            center_dist = float(np.linalg.norm(center - np.array([w / 2.0, h / 2.0], dtype=np.float32)))
            center_dist /= float(np.linalg.norm(np.array([w / 2.0, h / 2.0], dtype=np.float32)) + 1e-6)

            score = (area_ratio * 0.75) + (rectangularity * 0.25) - (center_dist * 0.08)
            if score > best_score:
                best_quad = quad
                best_score = score
                best_area_ratio = area_ratio
                best_rectangularity = rectangularity

    if best_quad is None:
        _vlog("preprocess.receipt_quad not_found h=%s w=%s", h, w)
    else:
        _vlog(
            "preprocess.receipt_quad found score=%.4f area_ratio=%.4f rectangularity=%.4f",
            best_score,
            best_area_ratio,
            best_rectangularity,
        )
    return best_quad


def _auto_crop_receipt(cv_img: np.ndarray) -> np.ndarray:
    h, w = cv_img.shape[:2]
    quad = _find_receipt_quad(cv_img)
    if quad is None:
        _vlog("preprocess.autocrop skipped reason=no_quad h=%s w=%s", h, w)
        return cv_img

    warped = _warp_from_quad(cv_img, quad)
    if warped is None:
        _vlog("preprocess.autocrop skipped reason=warp_failed h=%s w=%s", h, w)
        return cv_img

    wh, ww = warped.shape[:2]
    _vlog("preprocess.autocrop applied h=%s w=%s -> h=%s w=%s", h, w, wh, ww)
    return warped


def _normalize_receipt_gray(gray: np.ndarray) -> np.ndarray:
    if gray.size == 0:
        return gray

    denoised = cv2.bilateralFilter(gray, 7, 55, 55)
    min_side = min(gray.shape[:2])
    kernel_size = int(max(25, min(101, (min_side // 8) | 1)))
    bg = cv2.morphologyEx(
        denoised,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_size, kernel_size)),
    )
    normalized = cv2.divide(denoised, bg, scale=255)
    normalized = cv2.normalize(normalized, None, 0, 255, cv2.NORM_MINMAX)

    clahe = cv2.createCLAHE(clipLimit=2.8, tileGridSize=(8, 8))
    normalized = clahe.apply(normalized)

    blur = cv2.GaussianBlur(normalized, (0, 0), 1.2)
    sharpened = cv2.addWeighted(normalized, 1.55, blur, -0.55, 0)
    return sharpened


def _rotate_cv_image(cv_img: np.ndarray, angle_deg: float) -> np.ndarray:
    h, w = cv_img.shape[:2]
    if h == 0 or w == 0:
        return cv_img
    center = (w / 2.0, h / 2.0)
    matrix = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    border_value: Any = 255
    if len(cv_img.shape) == 3:
        border_value = (255, 255, 255)
    return cv2.warpAffine(
        cv_img,
        matrix,
        (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border_value,
    )


def _estimate_skew_angle(gray: np.ndarray) -> float:
    if gray.size == 0:
        return 0.0

    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    th = cv2.bitwise_not(th)
    coords = np.column_stack(np.where(th > 0))
    if coords.size == 0:
        return 0.0

    angle = float(cv2.minAreaRect(coords)[-1])
    if angle < -45.0:
        angle = -(90.0 + angle)
    else:
        angle = -angle
    return angle


def _deskew_pil_image(img: Image.Image) -> Image.Image:
    cv_img = _pil_to_cv(img)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    angle = _estimate_skew_angle(gray)
    if abs(angle) < 0.6 or abs(angle) > MAX_DESKEW_ANGLE:
        return img
    rotated = _rotate_cv_image(cv_img, angle)
    return _cv_to_pil(rotated)


def _detect_screen_capture(img: Image.Image) -> bool:
    w, h = img.size
    if w <= 0 or h <= 0:
        return False

    gray = cv2.cvtColor(_pil_to_cv(img), cv2.COLOR_BGR2GRAY)
    indicators = 0

    # Patrones periódicos (moire) por foto de pantalla.
    try:
        center_roi = gray[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4]
        if center_roi.size > 0:
            fft = np.fft.fftshift(np.fft.fft2(center_roi))
            mag = np.abs(fft)
            mid = mag[mag.shape[0] // 4 : 3 * mag.shape[0] // 4, mag.shape[1] // 4 : 3 * mag.shape[1] // 4]
            if np.mean(mid) > 0:
                peak_ratio = float(np.percentile(mid, 99) / (np.mean(mid) + 1e-6))
                if peak_ratio > 14.0:
                    indicators += 1
    except Exception:
        pass

    edge = cv2.Canny(gray, 120, 220)
    edge_density = float(np.count_nonzero(edge)) / float(max(1, edge.size))

    # Marco oscuro + centro brillante + alta densidad de bordes:
    # patrón típico de foto de pantalla (no screenshot directo).
    margin = max(8, int(min(h, w) * 0.07))
    if margin * 2 < h and margin * 2 < w:
        center = gray[margin : h - margin, margin : w - margin]
        top = gray[:margin, :]
        bottom = gray[h - margin :, :]
        left = gray[:, :margin]
        right = gray[:, w - margin :]
        border_mean = float(np.mean(np.concatenate([top.ravel(), bottom.ravel(), left.ravel(), right.ravel()])))
        center_mean = float(np.mean(center))
        if (center_mean - border_mean) > 65.0 and edge_density > 0.12:
            indicators += 1

    is_screen = indicators >= 1
    _vlog(
        "quality.screen_detect indicators=%s edge_density=%.5f resolution=%sx%s result=%s",
        indicators,
        edge_density,
        w,
        h,
        is_screen,
    )
    return is_screen


def _quality_assessment(image_bytes: bytes) -> QualityMetrics:
    img = _open_image_bytes(image_bytes)
    if img is None:
        return QualityMetrics(
            blur_score=0.0,
            blur_threshold=BLUR_THRESHOLD,
            is_blurry=True,
            brightness_mean=0.0,
            edge_density=0.0,
            resolution=(0, 0),
            is_screen_capture=False,
        )

    cv_img = _pil_to_cv(img)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(np.mean(gray))
    edges = cv2.Canny(gray, 100, 200)
    edge_density = float(np.count_nonzero(edges)) / float(edges.size)

    quality = QualityMetrics(
        blur_score=blur,
        blur_threshold=BLUR_THRESHOLD,
        is_blurry=blur < BLUR_THRESHOLD,
        brightness_mean=brightness,
        edge_density=edge_density,
        resolution=img.size,
        is_screen_capture=_detect_screen_capture(img),
    )
    _vlog(
        "quality blur=%.2f bright=%.2f edge=%.5f res=%sx%s is_blurry=%s is_screen=%s",
        quality.blur_score,
        quality.brightness_mean,
        quality.edge_density,
        quality.resolution[0],
        quality.resolution[1],
        quality.is_blurry,
        quality.is_screen_capture,
    )
    return quality


def _generate_variants(image_bytes: bytes) -> List[Tuple[str, bytes]]:
    started = time.perf_counter()
    img = _open_image_bytes(image_bytes)
    if img is None:
        return []

    img = _normalize_size(img)
    base_cv = _upscale_for_text_detail(_pil_to_cv(img))
    base_cv = _auto_crop_receipt(base_cv)
    base_pil = _cv_to_pil(base_cv)
    deskew = _deskew_pil_image(base_pil)

    variants: List[Tuple[str, Image.Image]] = []

    variants.append(("original", base_pil))
    variants.append(("deskew", deskew))

    enhanced = ImageOps.autocontrast(deskew)
    enhanced = ImageEnhance.Sharpness(enhanced).enhance(1.45)
    enhanced = ImageEnhance.Contrast(enhanced).enhance(1.20)
    variants.append(("enhanced", enhanced))

    deskew_cv = _pil_to_cv(deskew)
    gray = cv2.cvtColor(deskew_cv, cv2.COLOR_BGR2GRAY)
    norm_gray = _normalize_receipt_gray(gray)
    variants.append(("shadow_norm", Image.fromarray(norm_gray).convert("RGB")))

    adaptive_soft = cv2.adaptiveThreshold(
        norm_gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        9,
    )
    variants.append(("adaptive_soft", Image.fromarray(adaptive_soft).convert("RGB")))

    adaptive_strong = cv2.adaptiveThreshold(
        norm_gray,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY,
        45,
        15,
    )
    variants.append(("adaptive_strong", Image.fromarray(adaptive_strong).convert("RGB")))

    otsu_input = cv2.GaussianBlur(norm_gray, (3, 3), 0)
    otsu = cv2.threshold(otsu_input, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    variants.append(("otsu_clean", Image.fromarray(otsu).convert("RGB")))

    blackhat = cv2.morphologyEx(
        norm_gray,
        cv2.MORPH_BLACKHAT,
        cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
    )
    blackhat = cv2.normalize(blackhat, None, 0, 255, cv2.NORM_MINMAX)
    blackhat = cv2.threshold(blackhat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    blackhat = cv2.bitwise_not(blackhat)
    variants.append(("blackhat", Image.fromarray(blackhat).convert("RGB")))

    lab = cv2.cvtColor(deskew_cv, cv2.COLOR_BGR2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8))
    l_enhanced = clahe.apply(l_channel)
    clahe_bgr = cv2.cvtColor(cv2.merge((l_enhanced, a_channel, b_channel)), cv2.COLOR_LAB2BGR)
    clahe_pil = _cv_to_pil(clahe_bgr)
    clahe_pil = ImageEnhance.Sharpness(clahe_pil).enhance(1.25)
    variants.append(("clahe", clahe_pil))

    variants.append(("rot90", deskew.rotate(90, expand=True, fillcolor=(255, 255, 255))))
    variants.append(("rot270", deskew.rotate(270, expand=True, fillcolor=(255, 255, 255))))

    output: List[Tuple[str, bytes]] = []
    seen = set()
    for name, pil_img in variants:
        data = _pil_to_jpeg_bytes(pil_img)
        h = hash(data)
        if h in seen:
            continue
        seen.add(h)
        output.append((name, data))

    _vlog(
        "variants generated=%s names=%s elapsed_ms=%s",
        len(output),
        [n for n, _ in output],
        int((time.perf_counter() - started) * 1000),
    )
    return output


def _score_ocr_text(text: str) -> int:
    if not text:
        return 0

    t = text.upper()
    score = 0

    keywords = [
        "COMPROBANTE",
        "DEPOSITO",
        "DEPOSITO",
        "TRANSFERENCIA",
        "TRANSF",
        "BANCO",
        "COOPERATIVA",
        "CUENTA",
        "DOC",
        "RUC",
        "CI",
        "TOTAL",
    ]
    for kw in keywords:
        if kw in t:
            score += 12

    if re.search(r"\b\d{2}/\d{2}/\d{4}\b", text):
        score += 35
    if re.search(rf"\b{MONTH_TOKEN}\s+\d{{1,2}}\s+\d{{2,4}}\b", t):
        score += 35
    if re.search(r"\b\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})\b", text):
        score += 30
    if re.search(r"\b\d{2}:\d{2}(?::\d{2})?\b", text):
        score += 18
    if re.search(r"\b(?:RUC|C\.?I\.?|CI)\b", t):
        score += 16

    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) < 30:
        score -= 40
    elif len(clean) > 120:
        score += 12

    non_empty_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if non_empty_lines:
        short_lines = sum(1 for ln in non_empty_lines if len(ln) <= 2)
        short_line_ratio = short_lines / float(len(non_empty_lines))
        if short_line_ratio > 0.45:
            score -= int((short_line_ratio - 0.45) * 120)

    tokens = re.findall(r"[A-Z0-9]+", t)
    if tokens:
        single_char = sum(1 for tok in tokens if len(tok) <= 1)
        single_char_ratio = single_char / float(len(tokens))
        if single_char_ratio > 0.50:
            score -= int((single_char_ratio - 0.50) * 100)

    symbol_count = sum(1 for ch in text if not ch.isalnum() and not ch.isspace())
    if len(text) > 0:
        symbol_ratio = symbol_count / float(len(text))
        if symbol_ratio > 0.18:
            score -= int((symbol_ratio - 0.18) * 180)

    return max(score, 0)


def _tesseract_best_read(image_bytes: bytes) -> Tuple[str, int]:
    img = _open_image_bytes(image_bytes)
    if img is None:
        return "", 0

    cv_img = _upscale_for_text_detail(_pil_to_cv(img))
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    gray = _normalize_receipt_gray(gray)

    adaptive = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        9,
    )
    adaptive_strong = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY,
        45,
        15,
    )
    otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

    candidates = [
        (gray, "spa+eng", "--psm 6 -c preserve_interword_spaces=1"),
        (adaptive, "spa+eng", "--psm 6 -c preserve_interword_spaces=1"),
        (adaptive_strong, "spa+eng", "--psm 11 -c preserve_interword_spaces=1"),
        (otsu, "spa+eng", "--psm 6 -c preserve_interword_spaces=1"),
        (adaptive, "spa", "--psm 4 -c preserve_interword_spaces=1"),
    ]

    best_text = ""
    best_score = -1
    best_lang = ""
    best_config = ""
    candidate_scores: List[Dict[str, Any]] = []
    for arr, lang, config in candidates:
        try:
            txt = pytesseract.image_to_string(arr, lang=lang, config=config)
        except Exception as exc:
            candidate_scores.append({"lang": lang, "config": config, "score": -1, "error": str(exc)})
            continue
        score = _score_ocr_text(txt)
        candidate_scores.append({"lang": lang, "config": config, "score": score, "len": len(txt or "")})
        if score > best_score:
            best_text = txt
            best_score = score
            best_lang = lang
            best_config = config

    _vlog(
        "tesseract.best score=%s lang=%s config=%s candidates=%s",
        max(best_score, 0),
        best_lang,
        best_config,
        candidate_scores,
    )
    return best_text.strip(), max(best_score, 0)


def _quick_tesseract_score(image_bytes: bytes) -> int:
    img_hash = hash(image_bytes)
    cached = _quick_score_cache.get(img_hash)
    if cached is not None:
        return cached

    img = _open_image_bytes(image_bytes)
    if img is None:
        return 0

    cv_img = _pil_to_cv(img)
    h, w = cv_img.shape[:2]
    long_side = max(h, w)
    if long_side > QUICK_RANK_MAX_LONG_SIDE:
        scale = QUICK_RANK_MAX_LONG_SIDE / float(long_side)
        cv_img = cv2.resize(cv_img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

    score = 0
    try:
        txt = pytesseract.image_to_string(th, lang="spa+eng", config="--psm 6")
        score = _score_ocr_text(txt)
    except Exception as exc:
        _vlog("tesseract.quick_error reason=%s", str(exc))

    _quick_score_cache[img_hash] = score
    if len(_quick_score_cache) > QUICK_SCORE_CACHE_SIZE:
        _quick_score_cache.pop(next(iter(_quick_score_cache)))
    return score


def _best_tesseract_text_from_variants(variants: List[Tuple[str, bytes]]) -> str:
    best_text = ""
    best_score = -1
    for _name, data in variants:
        txt, score = _tesseract_best_read(data)
        if txt and score > best_score:
            best_text = txt
            best_score = score
    return best_text


def _select_top_variants(variants: List[Tuple[str, bytes]], top_n: int = TOP_VARIANTS) -> List[Tuple[str, bytes, int]]:
    scored = []
    for name, data in variants:
        scored.append((name, data, _quick_tesseract_score(data)))

    scored.sort(key=lambda x: x[2], reverse=True)
    _vlog("variants.rank top=%s", [{"name": n, "score": s} for n, _, s in scored[: min(8, len(scored))]])
    return scored[: max(1, min(top_n, len(scored)))]


def _is_valid_total(val: str) -> bool:
    if not val:
        return False
    return bool(re.search(r"\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})", val))


def _is_valid_date(val: str) -> bool:
    if not val:
        return False
    return bool(
        re.search(r"\b\d{2}/\d{2}/\d{4}\b", val)
        or re.search(r"\b\d{2}-\d{2}-\d{4}\b", val)
        or re.search(r"\b\d{2}/\d{2}/\d{2}\b", val)
        or re.search(r"\b\d{2}-\d{2}-\d{2}\b", val)
        or re.search(r"\b\d{4}-\d{2}-\d{2}\b", val)
        or re.search(r"\b\d{4}/\d{2}/\d{2}\b", val)
        or re.search(rf"\b\d{{4}}/[A-Za-z]{{3,}}\.?/\d{{1,2}}\b", val, flags=re.I)
        or re.search(rf"\b{MONTH_TOKEN}\s+\d{{1,2}}\s+\d{{2,4}}\b", val, flags=re.I)
        or re.search(rf"\b\d{{1,2}}\s+{MONTH_TOKEN}\s+\d{{2,4}}\b", val, flags=re.I)
    )


def _has_textual_month_date(val: str) -> bool:
    if not val:
        return False
    return bool(re.search(rf"\b\d{{4}}/[A-Za-z]{{3,}}\.?/\d{{1,2}}\b", val, flags=re.I))


def _is_valid_time(val: str) -> bool:
    if not val:
        return False
    return bool(re.search(r"\b\d{2}:\d{2}(?::\d{2})?\b", val))


def _is_valid_ci_ruc(val: str) -> bool:
    if not val:
        return False
    digits = re.sub(r"\D", "", val)
    return 6 <= len(digits) <= 13


def _is_valid_docnum(val: str) -> bool:
    if not val:
        return False
    x = val.strip().upper()
    if len(x) < 6:
        return False
    if len(re.findall(r"\d", x)) < 3:
        return False
    return bool(re.fullmatch(r"[A-Z0-9\-/#.]+", x))


def _is_docnum_valid_for_bank(docnum: str, bank_name: Optional[str]) -> bool:
    if not docnum:
        return False

    compact = re.sub(r"\s+", "", str(docnum).upper())
    compact_alnum = _norm_alnum(compact)
    bank = (bank_name or "").upper()

    if "JEP" in bank:
        # Para comprobantes digitales JEP (prefijo JV/JM/JY), exigimos formato robusto.
        m_digital = re.search(r"J[VMY][A-Z0-9]+", compact_alnum)
        if m_digital:
            candidate = m_digital.group(0)
            return bool(re.fullmatch(r"J[VMY]\d{4}[A-Z0-9]{3}\d{11}", candidate))
        # Para tickets/físicos JEP suelen aparecer docs numéricos cortos.
        digits = re.sub(r"\D", "", compact_alnum)
        if len(digits) >= 6:
            return True
        return _is_valid_docnum(compact_alnum)

    if "PICHINCHA" in bank:
        # Pichincha frecuentemente usa 6+ dígitos; la confianza fina se decide con calidad.
        digits = re.sub(r"\D", "", compact_alnum)
        return len(digits) >= 6

    return _is_valid_docnum(compact_alnum)


def _norm_alnum(val: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(val or "").upper())


def _norm_digit_ocr_token(val: str) -> str:
    mapping = {
        "O": "0",
        "Q": "0",
        "D": "0",
        "I": "1",
        "L": "1",
        "|": "1",
        "S": "5",
        "B": "8",
        "G": "6",
    }
    out: List[str] = []
    for ch in str(val or "").upper():
        if ch.isdigit():
            out.append(ch)
        elif ch in mapping:
            out.append(mapping[ch])
    return "".join(out)


def _norm_cnb(val: str) -> str:
    return _norm_digit_ocr_token(val)


def _is_valid_cnb(val: str) -> bool:
    digits = _norm_cnb(val)
    return 10 <= len(digits) <= 15


def _value_present_in_raw(raw_text: str, value: str, min_len: int = 6) -> bool:
    if not raw_text or not value:
        return True
    n_raw = _norm_alnum(raw_text)
    n_val = _norm_alnum(value)
    if len(n_val) < min_len:
        return True
    return n_val in n_raw


def _docnum_has_label_evidence(raw_text: str, docnum: str, strict_labels: bool = False) -> bool:
    if not raw_text or not docnum:
        return False
    n_doc = _norm_alnum(docnum)
    if len(n_doc) < 5:
        return False

    if strict_labels:
        # Etiquetas fuertes para "numero de comprobante/documento".
        label_re = re.compile(
            r"\b(COMPROBANTE|DOC|DOCUMENTO|TRANSAC(?:CION|CIÓN|TION)|OPERAC(?:ION|IÓN)|REFERENCIA)\b",
            flags=re.I,
        )
    else:
        label_re = re.compile(
            r"\b(DOC|DOCUMENTO|NRO|NO|NUMERO|COMPROBANTE|TRANSAC(?:CION|CIÓN|TION)|OPERAC(?:ION|IÓN)|REFERENCIA)\b",
            flags=re.I,
        )
    for line in raw_text.splitlines():
        if label_re.search(line):
            if n_doc in _norm_alnum(line):
                return True
    return False


def _should_relax_pichincha_deposito_docnum_retry(
    fields: Dict[str, Any],
    raw_text: str,
    validation: Dict[str, Any],
    expected_docnum: str,
) -> bool:
    """Indica si se puede relajar la validacion del numero en un deposito Pichincha."""
    bank_name = str(fields.get("entidad_bancaria") or _extract_bank_from_text(raw_text) or "").upper()
    raw_upper = str(raw_text or "").upper()
    normalized_doc = _norm_alnum(expected_docnum)
    if "PICHINCHA" not in bank_name or "DEPOSITO" not in raw_upper:
        return False
    if not normalized_doc or not normalized_doc.isdigit() or not (6 <= len(normalized_doc) <= 10):
        return False
    if not _docnum_has_label_evidence(raw_text, expected_docnum, strict_labels=True):
        return False

    hard_missing = {"numero_documento", "entidad_bancaria", "total"}
    hard_suspicious = {"numero_documento", "entidad_bancaria"}
    missing_hard = [f for f in validation.get("missing", []) if f in hard_missing]
    suspicious_hard = [f for f in validation.get("suspicious", []) if f in hard_suspicious]
    return not missing_hard and not suspicious_hard


def _extract_docnum_candidates_from_text(raw_text: str) -> List[str]:
    if not raw_text:
        return []

    candidates: List[str] = []
    pattern = re.compile(
        r"\b(?:COMPROBANTE|DOC|DOCUMENTO|NRO|NO|NUMERO)\b\s*[:#\-\.]?\s*([A-Z0-9\-/#.]{5,})",
        flags=re.I,
    )
    for match in pattern.finditer(raw_text):
        token = match.group(1).strip()
        token_norm = _norm_alnum(token)
        if len(token_norm) >= 5 and sum(ch.isdigit() for ch in token_norm) >= 4:
            candidates.append(token_norm)

    transaction_pattern = re.compile(
        r"\b(?:(?:NRO|NO|NUMERO)\s*(?:DE)?\s*)?"
        r"(?:TRANSAC(?:CION|CIÓN|TION)(?:\s*(?:ID|NUMBER))?|OPERAC(?:ION|IÓN)|REFERENCIA)\b"
        r"\s*[:#\-\.]?\s*([A-Z0-9\-/#.]{5,})",
        flags=re.I,
    )
    for match in transaction_pattern.finditer(raw_text):
        token = match.group(1).strip()
        token_norm = _norm_alnum(token)
        if len(token_norm) >= 5 and sum(ch.isdigit() for ch in token_norm) >= 4:
            candidates.append(token_norm)

    line_num_pattern = re.compile(r"\b\d{5,12}\b")
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    for idx, line in enumerate(lines):
        up = line.upper()
        if not any(token in up for token in ("COMPROBANTE", "TRANSACCION", "TRANSACCIÓN", "OPERACION", "OPERACIÓN", "REFERENCIA")):
            continue
        m_line = line_num_pattern.search(line)
        if m_line:
            candidates.append(_norm_alnum(m_line.group(0)))
            continue
        if idx + 1 < len(lines):
            m_next = line_num_pattern.search(lines[idx + 1])
            if m_next:
                candidates.append(_norm_alnum(m_next.group(0)))

    dedup: List[str] = []
    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        dedup.append(c)

    return dedup


def _extract_cnb_candidates_from_text(raw_text: str) -> List[str]:
    if not raw_text:
        return []

    candidates: List[str] = []
    line_digit_pattern = re.compile(r"[0-9OQDILSBG]{8,16}", flags=re.I)
    for line in raw_text.splitlines():
        if not re.search(r"\bC[MN]B\b", line, flags=re.I):
            continue
        for match in line_digit_pattern.finditer(line.upper()):
            token = _norm_cnb(match.group(0))
            if _is_valid_cnb(token):
                candidates.append(token)

    dedup: List[str] = []
    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        dedup.append(c)
    return dedup


def _is_valid_ruc_cnb(val: str) -> bool:
    digits = _norm_digit_ocr_token(val)
    return len(digits) == 13


def _is_valid_control(val: str) -> bool:
    digits = _norm_digit_ocr_token(val)
    return 7 <= len(digits) <= 12


def _extract_ruc_cnb_candidates_from_text(raw_text: str) -> List[str]:
    if not raw_text:
        return []

    candidates: List[str] = []
    label_re = re.compile(r"\bRUC\s+C[MN]B\b", flags=re.I)
    token_re = re.compile(r"[0-9OQDILSBG'\"`:;.,\-/]{10,20}", flags=re.I)

    for line in raw_text.splitlines():
        label_match = label_re.search(line)
        if not label_match:
            continue

        line_up = line.upper()
        tail = line_up[label_match.end() :]
        chunks: List[str] = []
        if ":" in tail:
            chunks.append(tail.split(":", 1)[1])
        chunks.extend(match.group(0).strip() for match in token_re.finditer(tail))

        for chunk in chunks:
            chunk = chunk.strip()
            digits = _norm_digit_ocr_token(chunk)
            if len(digits) == 13:
                candidates.append(digits)

            # Caso tipico: OCR reemplaza un "1" por comilla/puntuacion en un RUC de 13 digitos.
            m_missing_one = re.search(r"(\d{7})\D+(\d{5})", digits and chunk or "")
            if m_missing_one:
                reconstructed = f"{m_missing_one.group(1)}1{m_missing_one.group(2)}"
                if _is_valid_ruc_cnb(reconstructed):
                    candidates.append(reconstructed)

    dedup: List[str] = []
    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        dedup.append(c)
    canonical_suffix = [c for c in dedup if c.endswith("001")]
    if canonical_suffix:
        return canonical_suffix
    return dedup


def _extract_control_candidates_from_text(raw_text: str) -> List[str]:
    if not raw_text:
        return []

    candidates: List[str] = []
    label_re = re.compile(r"\bCONTROL\b", flags=re.I)
    token_re = re.compile(r"[0-9OQDILSBG'\"`:;.,\-/]{6,18}", flags=re.I)

    for line in raw_text.splitlines():
        label_match = label_re.search(line)
        if not label_match:
            continue

        tail = line[label_match.end() :]
        chunks: List[str] = []
        if ":" in tail:
            chunks.append(tail.split(":", 1)[1])
        chunks.extend(match.group(0).strip() for match in token_re.finditer(tail))

        for chunk in chunks:
            digits = _norm_digit_ocr_token(chunk)
            if _is_valid_control(digits):
                candidates.append(digits)

    dedup: List[str] = []
    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        dedup.append(c)
    return dedup


def _is_pichincha_recaudaciones(fields: Dict[str, Any], raw_text: str) -> bool:
    bank_name = str(fields.get("entidad_bancaria") or _extract_bank_from_text(raw_text) or "").upper()
    return "PICHINCHA" in bank_name and "RECAUDACIONES" in str(raw_text or "").upper()


def _is_pichincha_deposito_ticket(fields: Dict[str, Any], raw_text: str) -> bool:
    bank_name = str(fields.get("entidad_bancaria") or _extract_bank_from_text(raw_text) or "").upper()
    raw_upper = str(raw_text or "").upper()
    if "PICHINCHA" not in bank_name or "DEPOSITO" not in raw_upper:
        return False

    anchors = (
        "CUENTA CORRIENTE",
        "CUENTA AHORROS",
        "NOMBRE CNB",
        "RUC CNB",
        "CONTROL",
    )
    return any(anchor in raw_upper for anchor in anchors)


def _normalize_textual_month_token(token: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9]", "", str(token or "").upper())
    if not cleaned:
        return ""

    if re.fullmatch(r"[0OE]N[0OE]", cleaned):
        return "ene"
    if (
        len(cleaned) <= 5
        and cleaned.startswith("E")
        and ({"L", "N"} & set(cleaned))
        and cleaned[-1] in {"E", "I", "O", "0", "V", "Y"}
        and set(cleaned).issubset({"E", "N", "L", "I", "V", "O", "0"})
    ):
        return "ene"
    if len(cleaned) <= 5 and cleaned.startswith("E") and "N" in cleaned and cleaned[-1] in {"E", "I", "O", "0"}:
        return "ene"

    for canonical, aliases in SPANISH_MONTH_ALIASES.items():
        if cleaned in aliases:
            return canonical
        if any(cleaned.startswith(alias.rstrip(".")) for alias in aliases):
            return canonical

    return ""


def _extract_textual_month_from_tesseract(image_bytes: bytes) -> Optional[str]:
    img = _open_image_bytes(image_bytes)
    if img is None:
        return None

    cv_img = _upscale_for_text_detail(_pil_to_cv(_normalize_size(img)))
    cv_img = _auto_crop_receipt(cv_img)
    if cv_img is None or cv_img.size == 0:
        return None

    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    gray = _normalize_receipt_gray(gray)
    h, _ = gray.shape[:2]
    if h <= 0:
        return None

    rois = [
        gray[int(h * 0.30) : int(h * 0.78), :],
        gray[int(h * 0.42) : int(h * 0.72), :],
        gray[int(h * 0.48) : int(h * 0.76), :],
    ]
    month_hits: List[str] = []

    for roi in rois:
        if roi.size == 0:
            continue
        variants = [
            roi,
            cv2.adaptiveThreshold(
                roi,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                31,
                9,
            ),
            cv2.threshold(cv2.GaussianBlur(roi, (3, 3), 0), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
        ]
        for arr in variants:
            for psm in (6, 11):
                try:
                    txt = pytesseract.image_to_string(arr, lang="spa+eng", config=f"--psm {psm}")
                except Exception:
                    continue

                for match in re.finditer(
                    r"\b20\d{2}\s*[/,.-]?\s*([A-Za-z0-9]{2,6})\.?(?:\s*[/,.-]?\s*\d{1,2})?",
                    txt,
                    flags=re.I,
                ):
                    month = _normalize_textual_month_token(match.group(1))
                    if month:
                        month_hits.append(month)

                for match in re.finditer(r"\b([A-Za-z0-9]{2,6})\.?\b", txt, flags=re.I):
                    month = _normalize_textual_month_token(match.group(1))
                    if month:
                        month_hits.append(month)

    if not month_hits:
        return None

    counts = Counter(month_hits)
    top_month, top_count = counts.most_common(1)[0]
    if len(counts) == 1:
        return top_month

    second_count = counts.most_common(2)[1][1]
    if top_count >= second_count + 1:
        return top_month

    return None


def _replace_fecha_in_text(raw_text: str, fecha: str, hora: str = "") -> str:
    if not raw_text or not fecha:
        return raw_text

    replacement = f"{fecha} {hora}".strip()
    lines = raw_text.splitlines()
    for idx, line in enumerate(lines):
        upper = line.upper()
        if "FECH" not in upper and not re.search(r"\b20\d{2}\b", line):
            continue
        fixed = re.sub(
            r"\b20\d{2}\s*[/.-]\s*[A-Za-z0-9]{1,8}\.?\s*[/.-]\s*\d{1,2}(?:\s+\d{1,2}:\d{2})?\b",
            replacement,
            line,
            count=1,
        )
        if fixed == line:
            fixed = re.sub(r"\b20\d{2}[^\n]*", replacement, line, count=1)
        if fixed == line:
            fixed = f"{line.rstrip()} {replacement}"
        lines[idx] = fixed
        return "\n".join(lines)
    return raw_text


def _normalize_textual_month_in_date(date_value: str) -> str:
    if not date_value:
        return date_value

    match = re.search(
        r"\b(20\d{2})\s*[/.-]\s*([A-Za-z0-9]{1,8})\.?\s*[/.-]\s*(\d{1,2})\b",
        str(date_value or ""),
        flags=re.I,
    )
    if not match:
        return str(date_value or "")

    month = _normalize_textual_month_token(match.group(2))
    if not month:
        return str(date_value or "")

    year = match.group(1)
    day = match.group(3).zfill(2)
    return f"{year}/{month}./{day}"


def _build_textual_month_date(date_value: str, month_token: str) -> str:
    month = _normalize_textual_month_token(month_token)
    if not month:
        return str(date_value or "")

    match = re.search(
        r"\b(20\d{2})\s*[/.-]\s*([A-Za-z0-9]{1,6})\.?\s*[/.-]\s*(\d{1,2})\b",
        str(date_value or ""),
        flags=re.I,
    )
    if not match:
        return str(date_value or "")

    year = match.group(1)
    day = match.group(3).zfill(2)
    return f"{year}/{month}./{day}"


def _extract_ruc_cnb_from_tesseract(image_bytes: bytes) -> Tuple[Optional[str], List[str]]:
    img = _open_image_bytes(image_bytes)
    if img is None:
        return None, []

    cv_img = _upscale_for_text_detail(_pil_to_cv(img))
    cv_img = _auto_crop_receipt(cv_img)
    h, w = cv_img.shape[:2]
    if h <= 0 or w <= 0:
        return None, []

    gray_full = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    gray_full = _normalize_receipt_gray(gray_full)
    rois = [
        gray_full[int(h * 0.18) : int(h * 0.50), int(w * 0.03) :],
        gray_full[int(h * 0.23) : int(h * 0.40), int(w * 0.05) :],
        gray_full[int(h * 0.20) : int(h * 0.60), :],
    ]

    ruc_candidates: List[str] = []
    for roi in rois:
        if roi.size == 0:
            continue
        variants = [
            roi,
            cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
            cv2.adaptiveThreshold(roi, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 9),
        ]
        for psm in (6, 11):
            for arr in variants:
                try:
                    txt = pytesseract.image_to_string(arr, lang="spa+eng", config=f"--psm {psm}")
                except Exception:
                    continue
                ruc_candidates.extend(_extract_ruc_cnb_candidates_from_text(txt))

    counts = Counter([c for c in ruc_candidates if _is_valid_ruc_cnb(c)])
    if not counts:
        _vlog("ruc_cnb.extract no_candidates")
        return None, []

    top_ruc, top_count = counts.most_common(1)[0]
    unique_vals = list(counts.keys())
    _vlog("ruc_cnb.extract top=%s top_count=%s unique=%s all=%s", top_ruc, top_count, unique_vals, dict(counts))

    if len(counts) == 1:
        return top_ruc, unique_vals

    second_count = counts.most_common(2)[1][1]
    if top_count >= second_count + 1:
        return top_ruc, unique_vals

    return None, unique_vals


def _extract_ruc_cnb_from_selected_variants(selected: List[Tuple[str, bytes, int]]) -> Tuple[Optional[str], List[str]]:
    candidate_entries: List[Tuple[str, int, str]] = []
    for name, data, tscore in selected[: min(4, len(selected))]:
        txt, _ = _tesseract_best_read(data)
        for candidate in _extract_ruc_cnb_candidates_from_text(txt):
            if _is_valid_ruc_cnb(candidate):
                candidate_entries.append((candidate, int(tscore), name))

    if not candidate_entries:
        return None, []

    counts = Counter([candidate for candidate, _, _ in candidate_entries])
    top_ruc, top_count = counts.most_common(1)[0]
    unique_vals = list(counts.keys())
    if len(counts) == 1:
        return top_ruc, unique_vals

    second_count = counts.most_common(2)[1][1]
    if top_count >= second_count + 1:
        return top_ruc, unique_vals

    best_entry = max(candidate_entries, key=lambda x: (x[1], x[0].endswith("001")))
    return best_entry[0], unique_vals


def _extract_cnb_from_tesseract(image_bytes: bytes) -> Tuple[Optional[str], List[str]]:
    img = _open_image_bytes(image_bytes)
    if img is None:
        return None, []

    cv_img = _upscale_for_text_detail(_pil_to_cv(img))
    h, w = cv_img.shape[:2]
    if h <= 0 or w <= 0:
        return None, []

    gray_full = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    gray_full = _normalize_receipt_gray(gray_full)
    rois = [
        gray_full[int(h * 0.35) : int(h * 0.92), :],
        gray_full[int(h * 0.45) :, :],
        gray_full,
    ]

    cnb_candidates: List[str] = []
    for roi in rois:
        if roi.size == 0:
            continue
        variants = [
            roi,
            cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
            cv2.adaptiveThreshold(roi, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 9),
            cv2.bitwise_not(cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]),
        ]
        for psm in (6, 11):
            for arr in variants:
                try:
                    txt = pytesseract.image_to_string(arr, lang="spa+eng", config=f"--psm {psm}")
                except Exception:
                    continue
                cnb_candidates.extend(_extract_cnb_candidates_from_text(txt))

    counts = Counter([_norm_cnb(x) for x in cnb_candidates if _is_valid_cnb(x)])
    if not counts:
        _vlog("cnb.extract no_candidates")
        return None, []

    top_cnb, top_count = counts.most_common(1)[0]
    unique_vals = list(counts.keys())
    _vlog("cnb.extract top=%s top_count=%s unique=%s all=%s", top_cnb, top_count, unique_vals, dict(counts))

    if len(counts) == 1:
        return top_cnb, unique_vals

    second_count = counts.most_common(2)[1][1]
    if top_count >= second_count + 1:
        return top_cnb, unique_vals

    return None, unique_vals


def _replace_cnb_in_text(raw_text: str, codigo_cnb: str) -> str:
    if not raw_text or not codigo_cnb:
        return raw_text

    lines = raw_text.splitlines()
    for idx, line in enumerate(lines):
        if not re.search(r"\bC[MN]B\b", line, flags=re.I):
            continue
        fixed = re.sub(r"\bC[MN]B\b", "CNB", line, count=1, flags=re.I)
        if re.search(r"[0-9OQDILSBG]{8,16}", fixed, flags=re.I):
            fixed = re.sub(r"[0-9OQDILSBG]{8,16}", codigo_cnb, fixed, count=1, flags=re.I)
        else:
            fixed = f"{fixed.rstrip()} {codigo_cnb}"
        lines[idx] = fixed
        return "\n".join(lines)
    return raw_text


def _replace_ruc_cnb_in_text(raw_text: str, ruc_cnb: str) -> str:
    if not raw_text or not ruc_cnb:
        return raw_text

    lines = raw_text.splitlines()
    for idx, line in enumerate(lines):
        if not re.search(r"\bRUC\s+C[MN]B\b", line, flags=re.I):
            continue
        fixed = re.sub(r"\bC[MN]B\b", "CNB", line, count=1, flags=re.I)
        if ":" in fixed:
            left, right = fixed.split(":", 1)
            right_fixed = re.sub(r"[0-9OQDILSBG'\"`:;.,\-/ ]{10,20}", f" {ruc_cnb}", right, count=1, flags=re.I)
            fixed = f"{left}:{right_fixed}"
        else:
            fixed = re.sub(r"[0-9OQDILSBG'\"`:;.,\-/ ]{10,20}", ruc_cnb, fixed, count=1, flags=re.I)
        if ruc_cnb not in fixed:
            fixed = f"{fixed.rstrip()} {ruc_cnb}"
        lines[idx] = fixed
        return "\n".join(lines)
    return raw_text


def _replace_control_in_text(raw_text: str, control: str) -> str:
    if not raw_text or not control:
        return raw_text

    lines = raw_text.splitlines()
    for idx, line in enumerate(lines):
        if not re.search(r"\bCONTROL\b", line, flags=re.I):
            continue
        fixed = line
        if ":" in fixed:
            left, right = fixed.split(":", 1)
            right_fixed = re.sub(r"[0-9OQDILSBG'\"`:;.,\-/ ]{6,18}", f" {control}", right, count=1, flags=re.I)
            fixed = f"{left}:{right_fixed}"
        else:
            fixed = re.sub(r"[0-9OQDILSBG'\"`:;.,\-/ ]{6,18}", control, fixed, count=1, flags=re.I)
        if control not in fixed:
            fixed = f"{fixed.rstrip()} {control}"
        lines[idx] = fixed
        return "\n".join(lines)
    return raw_text


def _docnum_consistency_from_tesseract(image_bytes: bytes, expected_docnum: str) -> Tuple[bool, List[str]]:
    expected_norm = _norm_alnum(expected_docnum)
    if not expected_norm:
        return True, []

    # Verificacion focalizada para comprobantes numericos cortos (caso mas sensible).
    if not expected_norm.isdigit() or not (6 <= len(expected_norm) <= 10):
        return True, []

    img = _open_image_bytes(image_bytes)
    if img is None:
        return False, []

    cv_img = _upscale_for_text_detail(_pil_to_cv(img))
    h, w = cv_img.shape[:2]
    if h <= 0 or w <= 0:
        return False, []

    doc_candidates: List[str] = []
    gray_full = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    gray_full = _normalize_receipt_gray(gray_full)
    rois = [
        gray_full[: max(1, int(h * 0.72)), :],  # prioriza cabecera y bloque principal
        gray_full,  # fallback para recibos donde "Documento" queda mas abajo
    ]

    for roi in rois:
        variants = [
            roi,
            cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
            cv2.adaptiveThreshold(roi, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 9),
            cv2.bitwise_not(cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]),
        ]

        for psm in (6, 11):
            for arr in variants:
                try:
                    txt = pytesseract.image_to_string(arr, lang="spa+eng", config=f"--psm {psm}")
                except Exception:
                    continue
                doc_candidates.extend(_extract_docnum_candidates_from_text(txt))

    if not doc_candidates:
        _vlog("docnum.consistency no_candidates expected=%s", expected_norm)
        return False, []

    counts = Counter([_norm_alnum(x) for x in doc_candidates if _norm_alnum(x)])
    if not counts:
        return False, []

    top_doc, top_count = counts.most_common(1)[0]
    unique_docs = list(counts.keys())
    _vlog(
        "docnum.consistency expected=%s top=%s top_count=%s unique=%s all=%s",
        expected_norm,
        top_doc,
        top_count,
        unique_docs,
        dict(counts),
    )

    if top_doc != expected_norm:
        return False, unique_docs

    if len(counts) >= 3:
        return False, unique_docs
    if len(counts) == 2:
        other_count = counts.most_common(2)[1][1]
        if top_count <= other_count + 1:
            return False, unique_docs

    return True, unique_docs


def _norm_doc(val: str) -> str:
    return _norm_alnum(val)


def _docnum_has_soft_majority(doc_values: List[str]) -> bool:
    norm_docs = [_norm_doc(v) for v in doc_values if _norm_doc(v)]
    if len(norm_docs) < 3:
        return False

    counts = Counter(norm_docs)
    if len(counts) != 2:
        return False

    major_doc, major_count = counts.most_common(1)[0]
    if major_count < 2:
        return False

    minor_doc = next((doc for doc in counts if doc != major_doc), "")
    if not minor_doc:
        return False
    if not (major_doc.isdigit() and minor_doc.isdigit()):
        return False
    if len(major_doc) == len(minor_doc):
        digit_diffs = sum(1 for a, b in zip(major_doc, minor_doc) if a != b)
        return digit_diffs == 1

    if len(major_doc) == len(minor_doc) + 1:
        return major_doc.startswith(minor_doc) or major_doc.endswith(minor_doc)

    return False


def _docnum_is_small_ocr_variation(expected_doc: str, observed_doc: str) -> bool:
    expected = _norm_doc(expected_doc)
    observed = _norm_doc(observed_doc)
    if not expected or not observed:
        return False
    if not (expected.isdigit() and observed.isdigit()):
        return False
    if expected == observed:
        return True

    if len(expected) == len(observed):
        digit_diffs = sum(1 for a, b in zip(expected, observed) if a != b)
        return digit_diffs == 1

    if abs(len(expected) - len(observed)) == 1:
        shorter, longer = sorted((expected, observed), key=len)
        for idx in range(len(longer)):
            if longer[:idx] + longer[idx + 1 :] == shorter:
                return True

    return False


def _doc_candidates_support_expected(expected_doc: str, candidates: List[str]) -> bool:
    return any(_docnum_is_small_ocr_variation(expected_doc, candidate) for candidate in (candidates or []))


def _docnum_majority_value(doc_values: List[str]) -> str:
    norm_docs = [_norm_doc(v) for v in doc_values if _norm_doc(v)]
    if len(norm_docs) < 3:
        return ""

    counts = Counter(norm_docs)
    if not counts:
        return ""

    major_doc, major_count = counts.most_common(1)[0]
    if major_count < 2:
        return ""

    if len(counts) == 1:
        return major_doc

    second_count = counts.most_common(2)[1][1]
    if major_count > second_count:
        return major_doc

    return ""


def _majority_numeric_value(values: List[str], min_len: int, max_len: int) -> str:
    norm_vals = [_norm_digit_ocr_token(v) for v in values if min_len <= len(_norm_digit_ocr_token(v)) <= max_len]
    if not norm_vals:
        return ""

    counts = Counter(norm_vals)
    top_val, top_count = counts.most_common(1)[0]
    if top_count < 2:
        return ""
    if len(counts) == 1:
        return top_val

    second_count = counts.most_common(2)[1][1]
    if top_count > second_count:
        return top_val
    return ""


def _assess_pichincha_numeric_consistency(
    best_raw_text: str,
    top_trace: List[Dict[str, Any]],
    resolution: Tuple[int, int],
) -> Tuple[str, bool, List[str]]:
    fixed_text = best_raw_text or ""
    reasons: List[str] = []

    best_ruc_candidates = _extract_ruc_cnb_candidates_from_text(best_raw_text)
    best_control_candidates = _extract_control_candidates_from_text(best_raw_text)
    best_ruc = best_ruc_candidates[0] if best_ruc_candidates else ""
    best_control = best_control_candidates[0] if best_control_candidates else ""

    ruc_values = [str(t.get("ruc_cnb") or "") for t in top_trace if str(t.get("ruc_cnb") or "").strip()]
    control_values = [str(t.get("control") or "") for t in top_trace if str(t.get("control") or "").strip()]
    majority_ruc = _majority_numeric_value(ruc_values, 13, 13)
    majority_control = _majority_numeric_value(control_values, 7, 12)

    if majority_ruc and best_ruc != majority_ruc:
        fixed_text = _replace_ruc_cnb_in_text(fixed_text, majority_ruc)
    if majority_control and best_control != majority_control:
        fixed_text = _replace_control_in_text(fixed_text, majority_control)

    w, h = resolution
    tiny_receipt = w > 0 and h > 0 and (w * h) < 220000
    ruc_unverified = bool(best_ruc) and not majority_ruc
    control_unverified = bool(best_control) and not majority_control
    multi_variant_ruc_conflict = len({_norm_digit_ocr_token(v) for v in ruc_values if _is_valid_ruc_cnb(v)}) >= 2 and not majority_ruc
    multi_variant_control_conflict = (
        len({_norm_digit_ocr_token(v) for v in control_values if _is_valid_control(v)}) >= 2 and not majority_control
    )

    if (
        multi_variant_ruc_conflict
        or multi_variant_control_conflict
        or (tiny_receipt and (ruc_unverified or control_unverified))
    ):
        reasons.append("campos_numericos_inconsistentes_ocr")

    return fixed_text, bool(reasons), reasons


def _count_jep_ticket_anchor_hits(text: str) -> int:
    if not text:
        return 0
    return sum(1 for pattern in JEP_TICKET_ANCHOR_PATTERNS if pattern.search(text))


def _count_pacifico_portal_anchor_hits(text: str) -> int:
    if not text:
        return 0
    return sum(1 for pattern in PACIFICO_PORTAL_REPORT_PATTERNS if pattern.search(text))


def _looks_like_pacifico_portal_report(openai_text: str, local_text: str, max_selected_tscore: int) -> bool:
    combined = f"{openai_text or ''}\n{local_text or ''}".upper()
    if "PACIFICO" not in combined:
        return False

    openai_hits = _count_pacifico_portal_anchor_hits(openai_text)
    local_hits = _count_pacifico_portal_anchor_hits(local_text)

    if openai_hits >= 8:
        return True
    if openai_hits >= 6 and max_selected_tscore <= 80:
        return True
    if openai_hits >= 4 and local_hits >= 2 and max_selected_tscore <= 100:
        return True
    return False


def _has_strong_jep_ticket_support(text: str) -> bool:
    if not text:
        return False
    header_ok = bool(re.search(r"\bCOOPERATIVA\s+JEP\b|\bJEP\s+LTDA\b", text, flags=re.I))
    return header_ok and _count_jep_ticket_anchor_hits(text) >= 4


def _should_retry_for_jep_text_mismatch(openai_text: str, local_text: str) -> bool:
    local_hits = _count_jep_ticket_anchor_hits(local_text)
    openai_hits = _count_jep_ticket_anchor_hits(openai_text)
    if not _has_strong_jep_ticket_support(local_text):
        return False
    return openai_hits <= 2 and (local_hits - openai_hits) >= 2


def _norm_total(val: str) -> str:
    s = str(val or "").upper().replace(",", ".")
    s = re.sub(r"[^0-9.]", "", s)
    return s


def _is_variant_unstable(trace: List[Dict[str, Any]]) -> bool:
    parsed = [t for t in trace if t.get("parsed")]
    if len(parsed) < 2:
        return False

    parsed.sort(key=lambda x: int(x.get("candidate_score") or -999), reverse=True)
    top = parsed[:3]
    if len(top) >= 2:
        score_gap = int(top[0].get("candidate_score") or -999) - int(top[1].get("candidate_score") or -999)
        if score_gap > UNSTABLE_SCORE_GAP_MAX:
            return False

    doc_vals = {_norm_doc(t.get("docnum", "")) for t in top if t.get("docnum")}
    doc_vals = {v for v in doc_vals if v}
    if len(doc_vals) >= 2:
        return True

    total_vals = {_norm_total(t.get("total", "")) for t in top if t.get("total")}
    total_vals = {v for v in total_vals if v}
    if len(total_vals) >= 2:
        return True

    return False


def _extract_bank_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    t = text.upper()
    for bank, aliases in BANK_KEYWORDS.items():
        if any(a in t for a in aliases):
            return bank
    return None


def _has_cooperative_header(text: str) -> bool:
    if not text:
        return False
    lines = [ln.strip().upper() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    header = " ".join(lines[:4])
    return "COOPERATIVA" in header


def _regex_extract_core(text: str) -> Dict[str, Any]:
    if not text:
        return {}

    out: Dict[str, Any] = {}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    full_up = text.upper()

    bank = _extract_bank_from_text(text)
    if bank:
        out["entidad_bancaria"] = bank

    m_ci = re.search(r"\b(?:C\.?I\.?|CI|RUC)\s*[:#-]?\s*([0-9]{6,13})\b", text, flags=re.I)
    if m_ci:
        out["ci_ruc"] = m_ci.group(1)

    m_doc = re.search(
        r"\b(?:DOC|NRO|NO|NUMERO|DOCUMENTO|COMPROBANTE)\s*[:#-]?\s*([A-Z0-9\-/#.]{6,})\b",
        text,
        flags=re.I,
    )
    if m_doc:
        out["numero_documento"] = m_doc.group(1)
    elif ref_doc := _extract_docnum_candidates_from_text(text):
        out["numero_documento"] = ref_doc[0]

    cnb_candidates = _extract_cnb_candidates_from_text(text)
    if cnb_candidates:
        out["codigo_cnb"] = cnb_candidates[0]

    m_date = re.search(r"\b\d{2}/\d{2}/\d{4}\b", text)
    if not m_date:
        m_date = re.search(rf"\b{MONTH_TOKEN}\s+\d{{1,2}}\s+\d{{2,4}}\b", full_up)
    if not m_date:
        m_date = re.search(rf"\b\d{{1,2}}\s+{MONTH_TOKEN}\s+\d{{2,4}}\b", full_up)
    if m_date:
        out["fecha"] = m_date.group(0)

    m_time = re.search(r"\b\d{2}:\d{2}(?::\d{2})?\b", text)
    if m_time:
        out["hora"] = m_time.group(0)

    amounts = re.findall(r"\b\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})\b", text)
    if amounts:
        out["total"] = amounts[-1]

    if "nombre_depositante" not in out:
        for ln in lines:
            u = ln.upper()
            if any(tag in u for tag in ["SR", "SRA", "SE\u00d1OR", "DEPOSITANTE", "CLIENTE"]) and len(ln) > 8:
                cleaned = re.sub(r"\b(?:SR|SRA|SE\u00d1OR|DEPOSITANTE|CLIENTE)\b\s*[:#-]?\s*", "", ln, flags=re.I)
                if len(cleaned.strip()) > 5:
                    out["nombre_depositante"] = cleaned.strip()
                    break

    return out


def _normalize_fields(fields: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    keys = [
        "numero_documento",
        "codigo_cnb",
        "nombre_depositante",
        "ci_ruc",
        "entidad_bancaria",
        "fecha",
        "hora",
        "total",
    ]

    for k in keys:
        v = fields.get(k)
        if isinstance(v, str):
            v = re.sub(r"\s+", " ", v).strip()
        if v not in (None, ""):
            out[k] = v

    regex_fallback = _regex_extract_core(raw_text)
    for k, v in regex_fallback.items():
        if k not in out and v not in (None, ""):
            out[k] = v

    if out.get("entidad_bancaria") and out["entidad_bancaria"] in BANK_KEYWORDS:
        bank_code = out["entidad_bancaria"]
        # Conserva el nombre detectado para salida legible
        if bank_code == "JEP":
            out["entidad_bancaria"] = "COOPERATIVA JEP"
        elif bank_code == "PICHINCHA":
            out["entidad_bancaria"] = "BANCO PICHINCHA"
        elif bank_code == "PACIFICO":
            out["entidad_bancaria"] = "BANCO DEL PACIFICO"
        elif bank_code == "GUAYAQUIL":
            out["entidad_bancaria"] = "BANCO DE GUAYAQUIL"
        elif bank_code == "MACHALA":
            out["entidad_bancaria"] = "BANCO DE MACHALA"

    return out


def _missing_or_suspicious(fields: Dict[str, Any], raw_text: str = "") -> Dict[str, Any]:
    core = {
        "numero_documento": fields.get("numero_documento"),
        "codigo_cnb": fields.get("codigo_cnb"),
        "entidad_bancaria": fields.get("entidad_bancaria"),
        "fecha": fields.get("fecha"),
        "hora": fields.get("hora"),
        "total": fields.get("total"),
        "nombre_depositante": fields.get("nombre_depositante"),
        "ci_ruc": fields.get("ci_ruc"),
    }

    missing = [k for k, v in core.items() if k != "codigo_cnb" and not (v and str(v).strip())]

    suspicious: List[str] = []
    if core["total"] and not _is_valid_total(str(core["total"])):
        suspicious.append("total")
    if core["fecha"] and not _is_valid_date(str(core["fecha"])):
        suspicious.append("fecha")
    if core["fecha"] and (
        _is_pichincha_recaudaciones(core, raw_text) or _is_pichincha_deposito_ticket(core, raw_text)
    ) and not _has_textual_month_date(str(core["fecha"])) and "fecha" not in suspicious:
        suspicious.append("fecha")
    if core["hora"] and not _is_valid_time(str(core["hora"])):
        suspicious.append("hora")
    if core["ci_ruc"] and not _is_valid_ci_ruc(str(core["ci_ruc"])):
        suspicious.append("ci_ruc")
    bank_name = str(core["entidad_bancaria"] or "").upper()
    if bank_name and _has_cooperative_header(raw_text) and "COOPERATIVA" not in bank_name and not bank_name.startswith("BANCO "):
        suspicious.append("entidad_bancaria")
    if core["numero_documento"] and not _is_docnum_valid_for_bank(
        str(core["numero_documento"]),
        str(core["entidad_bancaria"] or ""),
    ):
        suspicious.append("numero_documento")
    # Evita aceptar docnum "completado" por el modelo si no aparece en el OCR crudo.
    if core["numero_documento"] and not _value_present_in_raw(raw_text, str(core["numero_documento"]), min_len=6):
        suspicious.append("numero_documento")

    if _is_pichincha_recaudaciones(fields, raw_text):
        if not (core["codigo_cnb"] and str(core["codigo_cnb"]).strip()):
            missing.append("codigo_cnb")
        else:
            if not _is_valid_cnb(str(core["codigo_cnb"])):
                suspicious.append("codigo_cnb")
            elif not _value_present_in_raw(raw_text, str(core["codigo_cnb"]), min_len=8):
                suspicious.append("codigo_cnb")

    return {
        "core": core,
        "missing": missing,
        "suspicious": suspicious,
    }


def _score_fields(fields: Dict[str, Any], raw_text: str) -> int:
    validation = _missing_or_suspicious(fields, raw_text)
    score = _score_ocr_text(raw_text)

    critical = {"numero_documento", "entidad_bancaria", "fecha", "total"}

    for k in critical:
        if fields.get(k):
            score += 35

    optional = ["hora", "nombre_depositante", "ci_ruc"]
    for k in optional:
        if fields.get(k):
            score += 12

    score -= 45 * len([x for x in validation["missing"] if x in critical])
    score -= 30 * len([x for x in validation["suspicious"] if x in critical])

    return score


def _prompt_for_structured_ocr() -> str:
    return (
        "Eres un OCR experto en comprobantes bancarios de Ecuador. "
        "Extrae texto y estructura en JSON estricto.\n"
        "Reglas:\n"
        "1) No inventes datos.\n"
        "2) Si un campo no existe, usa null.\n"
        "3) Conserva exactamente el valor observado (sin reinterpretar).\n"
        "4) Devuelve SOLO JSON valido con esta forma:\n"
        "{\n"
        "  \"raw_text\": \"...\",\n"
        "  \"fields\": {\n"
        "    \"numero_documento\": null,\n"
        "    \"codigo_cnb\": null,\n"
        "    \"nombre_depositante\": null,\n"
        "    \"ci_ruc\": null,\n"
        "    \"entidad_bancaria\": null,\n"
        "    \"fecha\": null,\n"
        "    \"hora\": null,\n"
        "    \"total\": null\n"
        "  }\n"
        "}"
    )


def _ocr_structured_openai(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    detail: Optional[str] = None,
    *,
    trace_id: Optional[str] = None,
    variant_name: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Usage]:
    data_url = _image_to_data_url(image_bytes, mime_type)
    selected_detail = detail or OPENAI_OCR_DETAIL
    log_debug_payload(
        "ocr.debug.openai_request",
        {
            "id": trace_id or "",
            "variant": variant_name or "",
            "model": OPENAI_OCR_MODEL,
            "detail": selected_detail,
            "mime_type": mime_type,
            "image_bytes": len(image_bytes),
            "prompt": _prompt_for_structured_ocr(),
        },
    )

    response = client.responses.create(
        model=OPENAI_OCR_MODEL,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _prompt_for_structured_ocr()},
                    {"type": "input_image", "image_url": data_url, "detail": selected_detail},
                ],
            }
        ],
    )

    usage = _usage_from_response(response)
    text = (getattr(response, "output_text", "") or "").strip()
    parsed = _safe_parse_json(text)
    response_payload = {
        "id": trace_id or "",
        "variant": variant_name or "",
        "parsed": bool(isinstance(parsed, dict)),
        "usage": asdict(usage),
    }
    if isinstance(parsed, dict):
        response_payload["raw_text"] = parsed.get("raw_text") or ""
        response_payload["fields"] = parsed.get("fields") or {}
    else:
        response_payload["raw_output"] = text
    log_debug_payload("ocr.debug.openai_response", response_payload)
    return parsed, usage


def _evaluate_openai_candidates(
    variants: List[Tuple[str, bytes, int]],
    trace_id: Optional[str] = None,
    detail_override: Optional[str] = None,
) -> Tuple[Optional[OCRCandidate], Usage, List[Dict[str, Any]]]:
    best: Optional[OCRCandidate] = None
    best_effective_score = -10**9
    total_usage = _empty_usage()
    trace: List[Dict[str, Any]] = []
    candidate_entries: List[Dict[str, Any]] = []

    variant_bias = {
        "original": 3,
        "deskew": 2,
        "clahe": 2,
        "shadow_norm": 2,
        "enhanced": 1,
        "adaptive_soft": 0,
        "adaptive_strong": -1,
        "otsu_clean": -2,
        "rot90": -3,
        "rot270": -3,
        "blackhat": -6,
    }

    for name, data, tscore in variants:
        try:
            parsed, usage = _ocr_structured_openai(
                data,
                "image/jpeg",
                detail=detail_override,
                trace_id=trace_id,
                variant_name=name,
            )
            total_usage = _add_usage(total_usage, usage)
            _vlog(
                "openai.variant variant=%s tesseract_score=%s usage_in=%s usage_out=%s",
                name,
                tscore,
                usage.input_tokens,
                usage.output_tokens,
            )
        except Exception as exc:
            trace.append(
                {
                    "variant": name,
                    "tesseract_score": tscore,
                    "candidate_score": -999,
                    "parsed": False,
                    "error": str(exc),
                }
            )
            logger.warning("openai.variant_error variant=%s reason=%s", name, str(exc))
            continue

        if not parsed or not isinstance(parsed, dict):
            trace.append(
                {
                    "variant": name,
                    "tesseract_score": tscore,
                    "candidate_score": -999,
                    "parsed": False,
                }
            )
            _vlog("openai.variant_unparsed variant=%s tesseract_score=%s", name, tscore)
            continue

        raw_text = str(parsed.get("raw_text") or "").strip()
        fields = parsed.get("fields") if isinstance(parsed.get("fields"), dict) else {}
        fields = _normalize_fields(fields, raw_text)

        candidate_score = _score_fields(fields, raw_text)
        effective_score = candidate_score + int(variant_bias.get(name, 0))
        validation = _missing_or_suspicious(fields, raw_text)
        ruc_cnb_candidates = _extract_ruc_cnb_candidates_from_text(raw_text)
        control_candidates = _extract_control_candidates_from_text(raw_text)
        trace.append(
            {
                "variant": name,
                "tesseract_score": tscore,
                "candidate_score": candidate_score,
                "effective_score": effective_score,
                "parsed": True,
                "docnum": fields.get("numero_documento"),
                "ruc_cnb": ruc_cnb_candidates[0] if ruc_cnb_candidates else "",
                "control": control_candidates[0] if control_candidates else "",
                "total": fields.get("total"),
            }
        )
        _vlog(
            "openai.variant_scored variant=%s candidate_score=%s effective_score=%s missing=%s suspicious=%s",
            name,
            candidate_score,
            effective_score,
            validation.get("missing"),
            validation.get("suspicious"),
        )

        candidate = OCRCandidate(
            variant=name,
            raw_text=raw_text,
            fields=fields,
            score=candidate_score,
            usage=usage,
        )
        candidate_entries.append(
            {
                "candidate": candidate,
                "candidate_score": candidate_score,
                "effective_score": effective_score,
                "docnum": _norm_doc(fields.get("numero_documento", "")),
            }
        )

        if best is None or effective_score > best_effective_score:
            best = candidate
            best_effective_score = effective_score

    if best is not None and candidate_entries:
        top_entries = sorted(
            candidate_entries,
            key=lambda x: int(x.get("candidate_score") or -999),
            reverse=True,
        )[: max(3, len(candidate_entries))]
        doc_counts = Counter([e["docnum"] for e in top_entries if e.get("docnum")])
        if doc_counts:
            majority_doc, majority_count = doc_counts.most_common(1)[0]
            best_doc = _norm_doc(best.fields.get("numero_documento", ""))
            if majority_count >= 2 and best_doc != majority_doc:
                majority_entries = [e for e in top_entries if e.get("docnum") == majority_doc]
                majority_best = max(
                    majority_entries,
                    key=lambda e: (int(e.get("candidate_score") or -999), int(e.get("effective_score") or -999)),
                )
                override_margin = 3
                if majority_count >= 3:
                    override_margin = 8
                if int(majority_best.get("candidate_score") or -999) >= (best.score - override_margin):
                    best = majority_best["candidate"]
                    best_effective_score = int(majority_best.get("effective_score") or best_effective_score)
                    _vlog(
                        "openai.candidates.override majority_doc=%s majority_count=%s best_variant=%s",
                        majority_doc,
                        majority_count,
                        best.variant,
                    )

    _vlog(
        "openai.candidates.done best_variant=%s best_score=%s best_effective_score=%s usage_total=%s",
        best.variant if best else None,
        best.score if best else None,
        best_effective_score if best else None,
        {"input": total_usage.input_tokens, "output": total_usage.output_tokens, "total": total_usage.total_tokens},
    )
    return best, total_usage, trace


def _retry_decision(quality: QualityMetrics, validation: Dict[str, Any], score: int, text: str) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    core = validation.get("core", {}) if isinstance(validation, dict) else {}
    bank_name = str(core.get("entidad_bancaria") or "").upper()
    docnum_raw = str(core.get("numero_documento") or "")
    doc_digits = re.sub(r"\D", "", docnum_raw)
    requires_cnb = "PICHINCHA" in bank_name and "RECAUDACIONES" in str(text or "").upper()

    # Retry estricto solo por campos realmente bloqueantes para uso transaccional.
    hard_missing_fields = {"numero_documento", "entidad_bancaria", "total"}
    hard_suspicious_fields = {"numero_documento", "entidad_bancaria"}
    if requires_cnb:
        hard_missing_fields.add("codigo_cnb")
        hard_suspicious_fields.add("codigo_cnb")
    missing_hard = [f for f in validation.get("missing", []) if f in hard_missing_fields]
    suspicious_hard = [f for f in validation.get("suspicious", []) if f in hard_suspicious_fields]

    quality_blockers = False
    if quality.edge_density < EDGE_DENSITY_MIN:
        quality_blockers = True

    if quality.brightness_mean < BRIGHTNESS_MIN:
        quality_blockers = True
    elif quality.brightness_mean > BRIGHTNESS_MAX:
        quality_blockers = True

    w, h = quality.resolution
    if w > 0 and h > 0 and (w * h) < MIN_IMAGE_PIXELS:
        quality_blockers = True

    clean_text_len = len(re.sub(r"\s+", "", text or ""))
    extraction_is_strong = (
        len(missing_hard) == 0
        and len(suspicious_hard) == 0
        and score >= STRONG_EXTRACTION_SCORE
        and clean_text_len >= 60
    )

    if quality.is_screen_capture:
        reasons.append("foto_de_pantalla")

    hard_blur_floor = min(8.0, BLUR_FORCE_RETRY * 0.25)
    blur_without_detail = (
        quality.blur_score < hard_blur_floor
        or (
            quality.blur_score < BLUR_FORCE_RETRY
            and quality.edge_density < max(0.0008, EDGE_DENSITY_MIN * 0.20)
        )
    )

    if quality.blur_score < BLUR_FORCE_RETRY:
        if blur_without_detail:
            reasons.append("imagen_demasiado_borrosa")
            _vlog(
                "retry.blur_hard_block blur=%.2f edge=%.5f score=%s text_len=%s",
                quality.blur_score,
                quality.edge_density,
                score,
                clean_text_len,
            )
        elif extraction_is_strong:
            _vlog(
                "retry.blur_override blur=%.2f score=%s text_len=%s",
                quality.blur_score,
                score,
                clean_text_len,
            )
        else:
            reasons.append("imagen_demasiado_borrosa")

    if clean_text_len < 35:
        reasons.append("texto_insuficiente")

    if missing_hard:
        reasons.append("faltan_campos_criticos")
    if suspicious_hard:
        reasons.append("campos_criticos_sospechosos")

    # Condiciona motivos de calidad solo cuando además hay riesgo real en campos hard.
    if quality_blockers and (missing_hard or suspicious_hard):
        if quality.edge_density < EDGE_DENSITY_MIN:
            reasons.append("poco_detalle_en_texto")
        if quality.blur_score < BLUR_RETRY_THRESHOLD:
            reasons.append("imagen_borrosa_o_movida")
        if quality.brightness_mean < BRIGHTNESS_MIN:
            reasons.append("imagen_muy_oscura")
        elif quality.brightness_mean > BRIGHTNESS_MAX:
            reasons.append("imagen_muy_brillante")
        if w > 0 and h > 0 and (w * h) < MIN_IMAGE_PIXELS:
            reasons.append("resolucion_baja")

    if score < MIN_ACCEPT_SCORE and (missing_hard or suspicious_hard):
        reasons.append("baja_confianza_extraccion")

    doc_has_label_evidence = _docnum_has_label_evidence(text, docnum_raw)

    # Caso típico de baja confianza: Pichincha con doc de solo 6 dígitos en imagen borrosa,
    # pero solo si no está respaldado explícitamente por etiqueta de documento en OCR crudo.
    if (
        "PICHINCHA" in bank_name
        and len(doc_digits) == 6
        and quality.blur_score < (BLUR_RETRY_THRESHOLD + 10)
        and not doc_has_label_evidence
    ):
        reasons.append("numero_documento_baja_confianza")

    dedup = []
    seen = set()
    for r in reasons:
        if r not in seen:
            seen.add(r)
            dedup.append(r)

    return len(dedup) > 0, dedup


def _retry_instructions(retry_reasons: Optional[List[str]] = None) -> List[str]:
    reasons = set(retry_reasons or [])

    invalid_voucher_reasons = {
        "campos_criticos_sospechosos",
        "campos_numericos_inconsistentes_ocr",
        "faltan_campos_criticos",
        "numero_documento_baja_confianza",
        "numero_documento_inconsistente_ocr",
        "inconsistencia_texto_con_ocr_local",
        "ocr_no_estructurado",
    }
    quality_reasons = {
        "imagen_demasiado_borrosa",
        "imagen_borrosa_o_movida",
        "imagen_muy_oscura",
        "imagen_muy_brillante",
        "resolucion_baja",
        "poco_detalle_en_texto",
        "texto_insuficiente",
    }

    instructions: List[str] = []

    if reasons.intersection(invalid_voucher_reasons):
        instructions.extend(
            [
                "Envia un comprobante de pago valido, no una pantalla de formulario o pagina de pago.",
                "Debe verse claramente numero de comprobante, fecha, entidad financiera y valor total.",
                "Si el comprobante es digital, sube PDF o captura directa de la app/banco.",
            ]
        )

    if "numero_documento_inconsistente_ocr" in reasons:
        instructions.append(
            "El numero de comprobante no se pudo validar con certeza; envia una foto/captura donde ese numero se lea nitido."
        )

    if "campos_numericos_inconsistentes_ocr" in reasons:
        instructions.append(
            "No se pudieron validar con certeza los numeros de RUC/CNB/control; envia una foto mas nitida o mas cercana."
        )

    if "inconsistencia_texto_con_ocr_local" in reasons:
        instructions.append(
            "El texto detectado no coincide de forma consistente con el comprobante; envia una foto mas nitida y cercana."
        )

    if "foto_de_pantalla" in reasons:
        instructions.append("No tomes foto a otra pantalla; sube la captura original del comprobante digital.")

    if reasons.intersection(quality_reasons):
        instructions.extend(
            [
                "Toma la foto mas cerca (que el comprobante ocupe casi todo el encuadre).",
                "Asegura buena luz y evita reflejos o sombras fuertes.",
                "Sostiene firme el telefono 1 segundo antes de disparar para evitar blur.",
                "Evita diagonales: camara paralela al comprobante.",
            ]
        )

    if not instructions:
        instructions.extend(
            [
                "Verifica que el archivo sea un comprobante de pago valido y legible.",
                "Asegura que se lean claramente los datos principales del comprobante.",
            ]
        )

    # Dedup conservando orden.
    dedup: List[str] = []
    seen = set()
    for item in instructions:
        if item in seen:
            continue
        seen.add(item)
        dedup.append(item)
    return dedup


def _process_image_bytes(
    image_bytes: bytes,
    top_variants_override: Optional[int] = None,
    detail_override: Optional[str] = None,
) -> Dict[str, Any]:
    trace_id = _new_trace_id()
    started = time.perf_counter()
    logger.info(
        "ocr.pipeline.start id=%s bytes=%s top_variants_override=%s detail_override=%s",
        trace_id,
        len(image_bytes),
        top_variants_override,
        detail_override,
    )
    quality = _quality_assessment(image_bytes)

    if quality.resolution == (0, 0):
        logger.warning("ocr.pipeline.invalid_image id=%s", trace_id)
        return {
            "texto_extraido": "",
            "campos": {},
            "usage": _empty_usage(),
            "quality": quality,
            "validation": {"core": {}, "missing": ["image"], "suspicious": []},
            "needs_retry": True,
            "retry_reasons": ["imagen_invalida"],
            "trace": [],
        }

    variants = _generate_variants(image_bytes)
    if not variants:
        logger.warning("ocr.pipeline.no_variants id=%s", trace_id)
        return {
            "texto_extraido": "",
            "campos": {},
            "usage": _empty_usage(),
            "quality": quality,
            "validation": {"core": {}, "missing": ["image"], "suspicious": []},
            "needs_retry": True,
            "retry_reasons": ["imagen_invalida"],
            "trace": [],
        }

    ranked = _select_top_variants(variants, len(variants))
    logger.info(
        "ocr.pipeline.variants id=%s total=%s ranked=%s",
        trace_id,
        len(variants),
        [{"variant": n, "score": s} for n, _, s in ranked[: min(8, len(ranked))]],
    )

    requested_top_n = TOP_VARIANTS if top_variants_override is None else max(1, int(top_variants_override))
    low_quality_input = (
        quality.is_blurry
        or quality.blur_score < BLUR_RETRY_THRESHOLD
        or quality.brightness_mean < (BRIGHTNESS_MIN + 20)
        or quality.brightness_mean > (BRIGHTNESS_MAX - 20)
    )
    effective_top_n = requested_top_n
    if low_quality_input:
        effective_top_n = max(effective_top_n, TOP_VARIANTS_LOW_QUALITY)
    effective_top_n = max(1, min(effective_top_n, len(ranked)))

    selected = ranked[:effective_top_n]
    logger.info(
        "ocr.pipeline.selected id=%s requested_top_n=%s effective_top_n=%s low_quality_input=%s selected=%s",
        trace_id,
        requested_top_n,
        effective_top_n,
        low_quality_input,
        [{"variant": n, "score": s} for n, _, s in selected],
    )

    best, usage_total, trace = _evaluate_openai_candidates(
        selected,
        trace_id=trace_id,
        detail_override=detail_override,
    )
    trace_summary = [
        {
            "variant": t.get("variant"),
            "tesseract_score": t.get("tesseract_score"),
            "candidate_score": t.get("candidate_score"),
            "effective_score": t.get("effective_score"),
            "docnum": t.get("docnum"),
            "ruc_cnb": t.get("ruc_cnb"),
            "control": t.get("control"),
            "parsed": t.get("parsed"),
        }
        for t in trace
    ]
    logger.info(
        "ocr.pipeline.openai_trace id=%s trace=%s usage_total=%s",
        trace_id,
        trace_summary,
        {"input": usage_total.input_tokens, "output": usage_total.output_tokens, "total": usage_total.total_tokens},
    )

    if best is None:
        fallback_text = _best_tesseract_text_from_variants([(n, d) for n, d, _ in selected])
        logger.warning(
            "ocr.pipeline.fallback_tesseract id=%s selected_variants=%s elapsed_ms=%s",
            trace_id,
            [n for n, _, _ in selected],
            int((time.perf_counter() - started) * 1000),
        )
        return {
            "texto_extraido": fallback_text,
            "campos": {},
            "usage": usage_total,
            "quality": quality,
            "validation": {"core": {}, "missing": ["ocr"], "suspicious": []},
            "needs_retry": True,
            "retry_reasons": ["ocr_no_estructurado"],
            "trace": trace,
        }

    if _is_pichincha_recaudaciones(best.fields, best.raw_text):
        current_cnb = _norm_cnb(str(best.fields.get("codigo_cnb") or ""))
        if not current_cnb:
            raw_cnb_candidates = _extract_cnb_candidates_from_text(best.raw_text)
            if raw_cnb_candidates:
                current_cnb = raw_cnb_candidates[0]

        corrected_cnb, cnb_candidates = _extract_cnb_from_tesseract(image_bytes)
        if corrected_cnb:
            best.fields["codigo_cnb"] = corrected_cnb
            if corrected_cnb != current_cnb:
                best.raw_text = _replace_cnb_in_text(best.raw_text, corrected_cnb)
                _vlog(
                    "cnb.corrected previous=%s corrected=%s candidates=%s",
                    current_cnb,
                    corrected_cnb,
                    cnb_candidates,
                )
        elif current_cnb:
            best.fields["codigo_cnb"] = current_cnb

    if _is_pichincha_recaudaciones(best.fields, best.raw_text) or _is_pichincha_deposito_ticket(best.fields, best.raw_text):
        current_ruc_candidates = _extract_ruc_cnb_candidates_from_text(best.raw_text)
        corrected_ruc_cnb, ruc_candidates = _extract_ruc_cnb_from_tesseract(image_bytes)
        if not corrected_ruc_cnb:
            corrected_ruc_cnb, variant_ruc_candidates = _extract_ruc_cnb_from_selected_variants(selected)
            if variant_ruc_candidates:
                ruc_candidates = variant_ruc_candidates
        if corrected_ruc_cnb:
            if not current_ruc_candidates or current_ruc_candidates[0] != corrected_ruc_cnb:
                best.raw_text = _replace_ruc_cnb_in_text(best.raw_text, corrected_ruc_cnb)
                _vlog(
                    "ruc_cnb.corrected previous=%s corrected=%s candidates=%s",
                    current_ruc_candidates[0] if current_ruc_candidates else None,
                    corrected_ruc_cnb,
                    ruc_candidates,
                )

        current_fecha = str(best.fields.get("fecha") or "").strip()
        current_hora = str(best.fields.get("hora") or "").strip()
        normalized_fecha = _normalize_textual_month_in_date(current_fecha)
        if normalized_fecha and normalized_fecha != current_fecha:
            best.fields["fecha"] = normalized_fecha
            best.raw_text = _replace_fecha_in_text(best.raw_text, normalized_fecha, current_hora)
            current_fecha = normalized_fecha
        extracted_month = _extract_textual_month_from_tesseract(image_bytes)
        if current_fecha and extracted_month and not _has_textual_month_date(current_fecha):
            corrected_fecha = _build_textual_month_date(current_fecha, extracted_month)
            if corrected_fecha and corrected_fecha != current_fecha:
                best.fields["fecha"] = corrected_fecha
                best.raw_text = _replace_fecha_in_text(best.raw_text, corrected_fecha, current_hora)
                _vlog(
                    "fecha.corrected previous=%s corrected=%s month=%s",
                    current_fecha,
                    corrected_fecha,
                    extracted_month,
                )

    max_selected_tscore = max((int(s) for _, _, s in selected), default=0)
    maybe_jep_ticket = (
        "JEP" in str(best.raw_text or "").upper()
        or "JEP" in str(best.fields.get("entidad_bancaria") or "").upper()
    )
    local_jep_text = ""
    jep_local_text_mismatch = False
    local_portal_text = ""
    pacifico_portal_report = False
    if maybe_jep_ticket and max_selected_tscore <= 40:
        local_jep_text = _best_tesseract_text_from_variants([(n, d) for n, d, _ in ranked[: min(4, len(ranked))]])
        jep_local_text_mismatch = _should_retry_for_jep_text_mismatch(best.raw_text, local_jep_text)
        if jep_local_text_mismatch:
            best.fields["entidad_bancaria"] = "COOPERATIVA JEP"
            _vlog(
                "jep.local_mismatch openai_hits=%s local_hits=%s max_selected_tscore=%s",
                _count_jep_ticket_anchor_hits(best.raw_text),
                _count_jep_ticket_anchor_hits(local_jep_text),
                max_selected_tscore,
            )
    if "PACIFICO" in str(best.raw_text or "").upper() or "PACIFICO" in str(best.fields.get("entidad_bancaria") or "").upper():
        local_portal_text = _best_tesseract_text_from_variants([(n, d) for n, d, _ in ranked[: min(4, len(ranked))]])
        pacifico_portal_report = _looks_like_pacifico_portal_report(best.raw_text, local_portal_text, max_selected_tscore)
        if pacifico_portal_report:
            _vlog(
                "pacifico.portal_report openai_hits=%s local_hits=%s max_selected_tscore=%s",
                _count_pacifico_portal_anchor_hits(best.raw_text),
                _count_pacifico_portal_anchor_hits(local_portal_text),
                max_selected_tscore,
            )

    parsed_trace = [t for t in trace if t.get("parsed")]
    parsed_trace.sort(key=lambda x: int(x.get("candidate_score") or -999), reverse=True)
    top_trace = parsed_trace[: max(3, min(len(parsed_trace), len(selected)))]
    pichincha_numeric_retry_reasons: List[str] = []
    if _is_pichincha_recaudaciones(best.fields, best.raw_text) or _is_pichincha_deposito_ticket(best.fields, best.raw_text):
        fixed_numeric_text, numeric_needs_retry, numeric_retry_reasons = _assess_pichincha_numeric_consistency(
            best.raw_text,
            top_trace,
            quality.resolution,
        )
        if fixed_numeric_text != best.raw_text:
            best.raw_text = fixed_numeric_text
        if numeric_needs_retry:
            pichincha_numeric_retry_reasons = numeric_retry_reasons
    doc_values_top = [_norm_doc(t.get("docnum", "")) for t in top_trace if _norm_doc(t.get("docnum", ""))]
    doc_vals_top = set(doc_values_top)
    docnum_unstable_between_variants = len(doc_vals_top) >= 2
    majority_doc = _docnum_majority_value(doc_values_top)
    best_doc_norm = _norm_doc(best.fields.get("numero_documento", ""))
    if docnum_unstable_between_variants and (
        _docnum_has_soft_majority(doc_values_top) or (majority_doc and majority_doc == best_doc_norm)
    ):
        docnum_unstable_between_variants = False
        _vlog("docnum.unstable_ignored consensus=%s majority=%s", doc_values_top, majority_doc)

    validation = _missing_or_suspicious(best.fields, best.raw_text)
    needs_retry, retry_reasons = _retry_decision(quality, validation, best.score, best.raw_text)
    if pichincha_numeric_retry_reasons:
        needs_retry = True
        retry_reasons = list(retry_reasons or [])
        retry_reasons.extend(pichincha_numeric_retry_reasons)

    # Verificacion adicional: el numero de comprobante debe ser consistente
    # entre la extraccion estructurada y OCR alterno del mismo comprobante.
    expected_docnum = str(best.fields.get("numero_documento") or "").strip()
    if not expected_docnum:
        candidates_from_raw = _extract_docnum_candidates_from_text(best.raw_text)
        if candidates_from_raw:
            expected_docnum = candidates_from_raw[0]

    best_variant_bytes = None
    for n, d, _ in selected:
        if n == best.variant:
            best_variant_bytes = d
            break

    should_validate_docnum = _docnum_has_label_evidence(
        best.raw_text,
        expected_docnum,
        strict_labels=True,
    )
    relax_pichincha_deposit_docnum_retry = _should_relax_pichincha_deposito_docnum_retry(
        best.fields,
        best.raw_text,
        validation,
        expected_docnum,
    )
    has_doc_consensus = bool(doc_vals_top) and _norm_doc(expected_docnum) in doc_vals_top and (
        len(doc_vals_top) == 1
        or _docnum_has_soft_majority(doc_values_top)
        or majority_doc == _norm_doc(expected_docnum)
    )
    if expected_docnum and best_variant_bytes is not None and should_validate_docnum:
        doc_ok, doc_candidates = _docnum_consistency_from_tesseract(best_variant_bytes, expected_docnum)
        helper_supports_expected = _doc_candidates_support_expected(expected_docnum, doc_candidates)
        helper_fallback_ticket_type = (
            _is_pichincha_recaudaciones(best.fields, best.raw_text)
            or _is_pichincha_deposito_ticket(best.fields, best.raw_text)
            or maybe_jep_ticket
        )
        ignore_no_candidate_conflict = (
            not doc_ok
            and not doc_candidates
            and has_doc_consensus
            and helper_fallback_ticket_type
        )
        ignore_near_candidate_conflict = (
            not doc_ok
            and bool(doc_candidates)
            and helper_supports_expected
            and has_doc_consensus
            and helper_fallback_ticket_type
        )
        if ignore_no_candidate_conflict or ignore_near_candidate_conflict:
            _vlog(
                "docnum.consistency ignored expected=%s reason=%s candidates=%s",
                _norm_alnum(expected_docnum),
                "near_candidates_with_consensus" if ignore_near_candidate_conflict else "no_candidates_with_consensus",
                doc_candidates,
            )
        elif relax_pichincha_deposit_docnum_retry:
            _vlog(
                "docnum.retry_ignored_pichincha_deposito expected=%s candidates=%s",
                _norm_alnum(expected_docnum),
                doc_candidates,
            )
        elif not doc_ok:
            needs_retry = True
            retry_reasons = list(retry_reasons or [])
            retry_reasons.append("numero_documento_inconsistente_ocr")
            _vlog(
                "docnum.retry expected=%s candidates=%s",
                _norm_alnum(expected_docnum),
                doc_candidates,
            )
    elif expected_docnum and best_variant_bytes is not None:
        _vlog(
            "docnum.consistency skipped expected=%s reason=no_strong_doc_label",
            _norm_alnum(expected_docnum),
        )

    if not needs_retry and _is_variant_unstable(trace):
        hard_missing = {"numero_documento", "entidad_bancaria", "total"}
        hard_suspicious = {"numero_documento", "entidad_bancaria", "total", "fecha"}
        missing_now = set(validation.get("missing", []))
        suspicious_now = set(validation.get("suspicious", []))
        unstable_risky = bool(missing_now.intersection(hard_missing) or suspicious_now.intersection(hard_suspicious))

        if docnum_unstable_between_variants and should_validate_docnum:
            if relax_pichincha_deposit_docnum_retry:
                _vlog("docnum.retry_ignored_pichincha_deposito unstable_between_variants doc_vals=%s", sorted(doc_vals_top))
            else:
                needs_retry = True
                retry_reasons = list(retry_reasons or [])
                retry_reasons.append("numero_documento_inconsistente_ocr")
                _vlog("docnum.retry unstable_between_variants doc_vals=%s", sorted(doc_vals_top))
        elif unstable_risky or best.score < STRONG_EXTRACTION_SCORE:
            needs_retry = True
            retry_reasons = ["inconsistencia_ocr_entre_variantes"]
        else:
            _vlog(
                "retry.unstable_ignored score=%s missing=%s suspicious=%s",
                best.score,
                sorted(missing_now),
                sorted(suspicious_now),
            )

    if needs_retry and retry_reasons and "numero_documento_inconsistente_ocr" in retry_reasons:
        exact_doc_consensus = bool(doc_vals_top) and len(doc_vals_top) == 1 and _norm_doc(expected_docnum) in doc_vals_top
        soft_doc_consensus = _docnum_has_soft_majority(doc_values_top) and _norm_doc(expected_docnum) in doc_vals_top
        if (exact_doc_consensus or soft_doc_consensus) and should_validate_docnum:
            original_doc_ok, original_doc_candidates = _docnum_consistency_from_tesseract(image_bytes, expected_docnum)
            if original_doc_ok:
                retry_reasons = [r for r in retry_reasons if r != "numero_documento_inconsistente_ocr"]
                needs_retry = len(retry_reasons) > 0
                _vlog(
                    "docnum.retry_cleared expected=%s consensus=%s original_candidates=%s",
                    _norm_doc(expected_docnum),
                    sorted(doc_vals_top),
                    original_doc_candidates,
                )

    if needs_retry and retry_reasons:
        dedup_retry: List[str] = []
        seen_retry = set()
        for rr in retry_reasons:
            if rr in seen_retry:
                continue
            seen_retry.add(rr)
            dedup_retry.append(rr)
        retry_reasons = dedup_retry

    if jep_local_text_mismatch:
        needs_retry = True
        retry_reasons = list(retry_reasons or [])
        retry_reasons.append("inconsistencia_texto_con_ocr_local")
        if local_jep_text.strip():
            best.raw_text = local_jep_text.strip()

    if pacifico_portal_report:
        needs_retry = True
        retry_reasons = list(retry_reasons or [])
        retry_reasons.append("foto_de_pantalla")

    logger.info(
        "ocr.pipeline.done id=%s best_variant=%s score=%s needs_retry=%s reasons=%s elapsed_ms=%s",
        trace_id,
        best.variant,
        best.score,
        needs_retry,
        retry_reasons,
        int((time.perf_counter() - started) * 1000),
    )
    log_debug_payload(
        "ocr.debug.pipeline_result",
        {
            "id": trace_id,
            "best_variant": best.variant,
            "candidate_score": best.score,
            "needs_retry": needs_retry,
            "retry_reasons": retry_reasons,
            "fields": best.fields,
            "texto_extraido": best.raw_text,
        },
    )

    return {
        "texto_extraido": best.raw_text,
        "campos": best.fields,
        "usage": usage_total,
        "quality": quality,
        "validation": validation,
        "needs_retry": needs_retry,
        "retry_reasons": retry_reasons,
        "trace": trace,
        "best_variant": best.variant,
        "candidate_score": best.score,
    }


def _process_pdf_bytes(pdf_bytes: bytes) -> Dict[str, Any]:
    trace_id = _new_trace_id()
    started = time.perf_counter()
    logger.info("ocr.pdf.start id=%s bytes=%s", trace_id, len(pdf_bytes))
    usage_total = _empty_usage()
    best_payload: Optional[Dict[str, Any]] = None

    try:
        pages = convert_from_bytes(pdf_bytes, first_page=1, last_page=PDF_MAX_PAGES)
    except Exception:
        logger.warning("ocr.pdf.invalid id=%s", trace_id)
        return {
            "texto_extraido": "",
            "campos": {},
            "usage": usage_total,
            "quality": QualityMetrics(0.0, BLUR_THRESHOLD, False, 0.0, 0.0, (0, 0), False),
            "validation": {"core": {}, "missing": ["pdf"], "suspicious": []},
            "needs_retry": True,
            "retry_reasons": ["pdf_invalido"],
            "trace": [],
        }

    for idx, page in enumerate(pages, start=1):
        page_bytes = _pil_to_jpeg_bytes(page.convert("RGB"), quality=93)
        payload = _process_image_bytes(page_bytes)
        usage_total = _add_usage(usage_total, payload["usage"])

        payload_score = int(payload.get("candidate_score") or 0)
        logger.info(
            "ocr.pdf.page id=%s page=%s score=%s retry=%s",
            trace_id,
            idx,
            payload_score,
            payload.get("needs_retry"),
        )
        if best_payload is None:
            best_payload = payload
            best_payload["best_page"] = idx
            continue

        if payload_score > int(best_payload.get("candidate_score") or 0):
            best_payload = payload
            best_payload["best_page"] = idx

    if best_payload is None:
        logger.warning("ocr.pdf.no_pages id=%s", trace_id)
        return {
            "texto_extraido": "",
            "campos": {},
            "usage": usage_total,
            "quality": QualityMetrics(0.0, BLUR_THRESHOLD, False, 0.0, 0.0, (0, 0), False),
            "validation": {"core": {}, "missing": ["pdf"], "suspicious": []},
            "needs_retry": True,
            "retry_reasons": ["pdf_sin_paginas"],
            "trace": [],
        }

    best_payload["usage"] = usage_total
    logger.info(
        "ocr.pdf.done id=%s best_page=%s best_score=%s elapsed_ms=%s",
        trace_id,
        best_payload.get("best_page"),
        best_payload.get("candidate_score"),
        int((time.perf_counter() - started) * 1000),
    )
    return best_payload


def _list_dataset_files(folder: Path) -> List[Path]:
    files: List[Path] = []
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGE_EXT:
            files.append(p)
    return files


def _build_uploaded_document(content: bytes, filename: str = "", mimetype: str = "") -> UploadedDocument:
    if not content:
        raise APIError("Empty file", status_code=400, code="empty_file")
    if len(content) > MAX_UPLOAD_BYTES:
        raise APIError(
            "File exceeds the maximum allowed size",
            status_code=413,
            code="file_too_large",
            details={"max_upload_bytes": MAX_UPLOAD_BYTES, "received_bytes": len(content)},
        )

    normalized_filename = filename.strip()
    normalized_mimetype = mimetype.strip().lower()
    if not normalized_mimetype and normalized_filename:
        guessed_mimetype, _ = mimetypes.guess_type(normalized_filename)
        normalized_mimetype = (guessed_mimetype or "").lower()

    is_pdf = (
        normalized_filename.lower().endswith(".pdf")
        or normalized_mimetype == "application/pdf"
        or content.startswith(b"%PDF-")
    )

    if not normalized_filename:
        if is_pdf:
            normalized_filename = "upload.pdf"
        else:
            extension = mimetypes.guess_extension(normalized_mimetype or "") or ""
            normalized_filename = f"upload{extension}"

    return UploadedDocument(
        filename=normalized_filename,
        mimetype=normalized_mimetype,
        content=content,
        is_pdf=is_pdf,
    )


def _log_uploaded_document(source: str, document: UploadedDocument) -> None:
    logger.info(
        "document_parsed source=%s filename=%s mimetype=%s bytes=%s is_pdf=%s",
        source,
        document.filename or "<empty>",
        document.mimetype or "<empty>",
        len(document.content),
        document.is_pdf,
    )


def _decode_base64_document(encoded: str) -> Tuple[bytes, str]:
    raw_value = encoded.strip()
    if not raw_value:
        raise APIError("Empty base64 document", status_code=400, code="empty_file")

    mime_type = ""
    data_match = re.fullmatch(
        r"data:(?P<mime>[\w.+-]+/[\w.+-]+);base64,(?P<data>.+)",
        raw_value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if data_match:
        mime_type = data_match.group("mime").lower()
        raw_value = data_match.group("data").strip()

    try:
        return base64.b64decode(raw_value, validate=True), mime_type
    except Exception as exc:
        raise APIError("Invalid base64 document", status_code=400, code="invalid_base64") from exc


async def _read_uploaded_document(req: Any) -> UploadedDocument:
    content_type = (getattr(req, "content_type", "") or "").strip().lower()
    files = await req.files if "multipart/form-data" in content_type else None
    upload = files.get("file") if files is not None else None
    if upload is None:
        if req.is_json:
            payload = await req.get_json(silent=True)
            if payload is None:
                raise APIError("Invalid JSON body", status_code=400, code="invalid_json")
            if not isinstance(payload, dict):
                raise APIError("JSON body must be an object", status_code=400, code="invalid_json")

            encoded = payload.get("image_base64") or payload.get("file_base64")
            if encoded is None:
                raise APIError("No file provided", status_code=400, code="missing_file")
            if not isinstance(encoded, str):
                raise APIError("Base64 document must be a string", status_code=400, code="invalid_base64")

            content, inline_mimetype = _decode_base64_document(encoded)
            filename = str(payload.get("filename") or "").strip()
            mimetype = str(payload.get("mimetype") or inline_mimetype).strip().lower()
            document = _build_uploaded_document(content=content, filename=filename, mimetype=mimetype)
            _log_uploaded_document("json_base64", document)
            return document

        direct_mimetype = (req.mimetype or "").strip().lower()
        direct_content = await req.get_data(cache=False)
        supports_direct_upload = direct_mimetype == "application/octet-stream" or direct_mimetype.startswith(
            "image/"
        ) or direct_mimetype == "application/pdf"
        if supports_direct_upload and direct_content:
            filename = (req.headers.get("X-Filename") or req.headers.get("X-File-Name") or "").strip()
            document = _build_uploaded_document(content=direct_content, filename=filename, mimetype=direct_mimetype)
            _log_uploaded_document("binary_body", document)
            return document

        raise APIError("No file provided", status_code=400, code="missing_file")

    content = upload.read()
    if inspect.isawaitable(content):
        content = await content
    filename = (upload.filename or "").strip()
    mimetype = (upload.mimetype or "").strip().lower()
    document = _build_uploaded_document(content=content, filename=filename, mimetype=mimetype)
    _log_uploaded_document("multipart", document)
    return document


def _build_ocr_api_response(payload: Dict[str, Any]) -> OCRApiResponse:
    usage: Usage = payload.get("usage") if isinstance(payload.get("usage"), Usage) else _empty_usage()
    needs_retry = bool(payload.get("needs_retry", True))
    retry_reasons = payload.get("retry_reasons", []) if needs_retry else []
    return OCRApiResponse(
        estado="reintentar_foto" if needs_retry else "ok",
        texto_extraido=str(payload.get("texto_extraido", "")),
        debe_reintentar=needs_retry,
        motivos_reintento=retry_reasons,
        instrucciones_reintento=_retry_instructions(retry_reasons) if needs_retry else [],
        uso=usage,
        costo_estimado=_estimate_cost(usage),
    )


def _parse_int_query_param(value: Optional[str], name: str) -> Optional[int]:
    if value is None:
        return None

    try:
        parsed = int(value)
    except Exception:
        raise APIError(
            f"Invalid query parameter: {name}",
            status_code=400,
            code="invalid_query_parameter",
            details={"parameter": name, "value": value, "expected": "positive integer"},
        )

    if parsed < 1:
        raise APIError(
            f"Invalid query parameter: {name}",
            status_code=400,
            code="invalid_query_parameter",
            details={"parameter": name, "value": value, "expected": "positive integer"},
        )

    return parsed


class OCRService:
    def process_document(
        self,
        document: UploadedDocument,
        top_variants_override: Optional[int] = None,
        detail_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        if document.is_pdf:
            return _process_pdf_bytes(document.content)
        return _process_image_bytes(
            document.content,
            top_variants_override=top_variants_override,
            detail_override=detail_override,
        )

    def process_tesseract_image(self, image_bytes: bytes) -> Dict[str, Any]:
        trace_id = _new_trace_id()
        started = time.perf_counter()
        logger.info("ocr.tesseract.start id=%s bytes=%s", trace_id, len(image_bytes))
        variants = _generate_variants(image_bytes)
        if not variants:
            raise APIError("Invalid image", status_code=400, code="invalid_image")

        ranked = _select_top_variants(variants, len(variants))
        # Evalua en profundidad solo las mejores variantes para no disparar latencia.
        selected_top_n = min(4, len(ranked))
        selected = ranked[:selected_top_n]

        best_text = ""
        best_score = -1
        best_variant = ""
        rank: List[Dict[str, Any]] = []
        for name, data, _ in selected:
            text, score = _tesseract_best_read(data)
            # only metadata; avoid logging extracted personal data.
            rank.append({"variant": name, "score": score, "text_len": len(text or "")})
            if score > best_score:
                best_text = text
                best_score = score
                best_variant = name

        rank.sort(key=lambda x: int(x["score"]), reverse=True)
        logger.info(
            "ocr.tesseract.done id=%s selected_top_n=%s best_variant=%s best_score=%s ranked=%s elapsed_ms=%s",
            trace_id,
            selected_top_n,
            best_variant,
            max(best_score, 0),
            rank[: min(8, len(rank))],
            int((time.perf_counter() - started) * 1000),
        )

        return {
            "texto_extraido": best_text.strip(),
            "score": max(best_score, 0),
            "variant": best_variant,
            "variant_count": len(variants),
        }


ocr_service = OCRService()

# Public aliases for API layer imports.
read_uploaded_document = _read_uploaded_document
build_ocr_api_response = _build_ocr_api_response
build_uploaded_document = _build_uploaded_document
decode_base64_document = _decode_base64_document
log_uploaded_document = _log_uploaded_document
parse_int_query_param = _parse_int_query_param
list_dataset_files = _list_dataset_files
process_image_bytes = _process_image_bytes
add_usage = _add_usage
empty_usage = _empty_usage
extract_bank_from_text = _extract_bank_from_text
estimate_cost = _estimate_cost
