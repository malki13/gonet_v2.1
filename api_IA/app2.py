import base64
import difflib
import hashlib
import io
import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple, Any

import cv2
import numpy as np
import pytesseract
from flask import Flask, request, jsonify
from openai import OpenAI
from pdf2image import convert_from_bytes
from PIL import Image

# ============================================================================
# CONFIGURACIÓN Y CONSTANTES
# ============================================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

client = OpenAI()

OPENAI_OCR_MODEL = os.getenv("OPENAI_OCR_MODEL", "gpt-4o-mini")
OPENAI_OCR_DETAIL = os.getenv("OPENAI_OCR_DETAIL", "high")
OPENAI_INPUT_COST_PER_MILLION = float(os.getenv("OPENAI_INPUT_COST_PER_MILLION", "0.40"))
OPENAI_OUTPUT_COST_PER_MILLION = float(os.getenv("OPENAI_OUTPUT_COST_PER_MILLION", "1.60"))
BLUR_THRESHOLD = float(os.getenv("BLUR_THRESHOLD", "90"))
BLUR_HARD_FAIL = float(os.getenv("BLUR_HARD_FAIL", "50"))
PDF_MAX_PAGES = int(os.getenv("PDF_MAX_PAGES", "3"))
CACHE_ENABLED = os.getenv("CACHE_ENABLED", "false").lower() == "true"
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))

# ── Umbrales de validación temprana ──────────────────────────────────────────
BLUR_REJECT_SCREEN_CAPTURE = float(os.getenv("BLUR_REJECT_SCREEN_CAPTURE", "60.0"))
BLUR_REJECT_PHOTO = float(os.getenv("BLUR_REJECT_PHOTO", "40.0"))
MIN_RESOLUTION_PX = int(os.getenv("MIN_RESOLUTION_PX", str(400 * 300)))
VOUCHER_ASPECT_MIN = float(os.getenv("VOUCHER_ASPECT_MIN", "0.3"))
VOUCHER_ASPECT_MAX = float(os.getenv("VOUCHER_ASPECT_MAX", "2.0"))
DATE_TOLERANCE_DAYS_PAST = int(os.getenv("DATE_TOLERANCE_DAYS_PAST", "365"))
DATE_TOLERANCE_DAYS_FUTURE = int(os.getenv("DATE_TOLERANCE_DAYS_FUTURE", "1"))
VOUCHER_CONFIDENCE_THRESHOLD = float(os.getenv("VOUCHER_CONFIDENCE_THRESHOLD", "0.60"))

DOC_LABELS = {"NO", "NRO", "Nº", "N°", "N0", "REF", "REFERENCIA", "COMPROBANTE", "DOCUMENTO", "CONTROL"}

CORE_FIELDS_SCHEMA = """{
  "raw_text": "...",
  "fields": {
    "numero_documento": null,
    "nombre_depositante": null,
    "ci_ruc": null,
    "entidad_bancaria": null,
    "fecha": null,
    "hora": null,
    "total": null
  }
}"""

BANK_PATTERNS = {
    "JEP": {
        "keywords": ["JEP", "JARDIN AZUAYO"],
        "doc_pattern": r'\bJV\d{4}[A-Z]{3}\d{11}\b',
        "doc_labels": ["NO", "NRO", "DOCUMENTO"],
        "requires_label": False,
    },
    "PICHINCHA": {
        "keywords": ["PICHINCHA", "BANCO PICHINCHA"],
        "doc_pattern": r'\b\d{12,16}\b',
        "doc_labels": ["COMPROBANTE", "NO", "REFERENCIA"],
        "requires_label": True,
    },
    "PRODUBANCO": {
        "keywords": ["PRODUBANCO", "PROMERICA"],
        "doc_pattern": r'\b[A-Z]{2,4}\d{10,14}\b',
        "doc_labels": ["NO", "REFERENCIA"],
        "requires_label": True,
    },
    "GUAYAQUIL": {
        "keywords": ["GUAYAQUIL", "BANCO DE GUAYAQUIL"],
        "doc_pattern": r'\b\d{10,15}\b',
        "doc_labels": ["COMPROBANTE", "NO"],
        "requires_label": True,
    },
    "BOLIVARIANO": {
        "keywords": ["BOLIVARIANO"],
        "doc_pattern": r'\b[A-Z0-9]{8,16}\b',
        "doc_labels": ["COMPROBANTE", "REFERENCIA"],
        "requires_label": True,
    },
    "PACIFICO": {
        "keywords": ["PACIFICO", "PACÍFICO"],
        "doc_pattern": r'\b\d{10,14}\b',
        "doc_labels": ["NO", "COMPROBANTE"],
        "requires_label": True,
    },
}

_simple_cache: Dict[str, Tuple[Any, float]] = {}

# ============================================================================
# DATACLASSES
# ============================================================================

@dataclass
class PhaseMetrics:
    phase: str
    duration_ms: float
    success: bool
    score: Optional[float] = None
    changes: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

@dataclass
class QualityMetrics:
    blur_score: float
    blur_threshold: float
    is_blurry: bool
    resolution: Tuple[int, int]
    edge_density: Optional[float] = None
    original_blur_score: Optional[float] = None
    original_is_blurry: Optional[bool] = None
    is_screen_capture: bool = False
    demoire_applied: bool = False
    demoire_improved: Optional[bool] = None

@dataclass
class Usage:
    input_tokens: int
    output_tokens: int
    total_tokens: int

@dataclass
class ProcessingResult:
    texto_extraido: str
    campos: Dict[str, Any]
    uso: Usage
    costo_estimado: Dict[str, Any]
    quality: QualityMetrics
    validation: Dict[str, Any]
    needs_retry: bool
    retry_reasons: List[str]
    retry_instructions: Optional[List[str]] = None
    detected_bank: Optional[str] = None
    processing_metrics: Optional[List[PhaseMetrics]] = None
    cache_hit: bool = False

@dataclass
class ImageValidationResult:
    """Resultado de validación de imagen ANTES del OCR"""
    accepted: bool
    rejection_code: Optional[str]
    rejection_message: str
    user_instructions: List[str]
    technical_reason: Optional[str] = None
    quality_scores: Optional[dict] = None

@dataclass
class VoucherValidationResult:
    """Resultado de validación de comprobante POST-OCR"""
    is_valid_voucher: bool
    confidence: float
    failing_checks: List[str]
    warnings: List[str]
    validated_fields: dict

# ============================================================================
# UTILIDADES BÁSICAS
# ============================================================================

def _image_to_data_url(image_bytes: bytes, mime_type: str) -> str:
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{b64}"

def _get_usage_dict(usage) -> Dict[str, int]:
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    if isinstance(usage, dict):
        return {
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "total_tokens": int(usage.get("total_tokens", 0)),
        }
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0)),
        "output_tokens": int(getattr(usage, "output_tokens", 0)),
        "total_tokens": int(getattr(usage, "total_tokens", 0)),
    }

def _add_usage(a: Dict[str, int], b: Dict[str, int]) -> Dict[str, int]:
    return {
        "input_tokens": a["input_tokens"] + b["input_tokens"],
        "output_tokens": a["output_tokens"] + b["output_tokens"],
        "total_tokens": a["total_tokens"] + b["total_tokens"],
    }

def _estimate_cost(usage: Dict[str, int]) -> Dict[str, Any]:
    input_cost = (usage["input_tokens"] / 1_000_000) * OPENAI_INPUT_COST_PER_MILLION
    output_cost = (usage["output_tokens"] / 1_000_000) * OPENAI_OUTPUT_COST_PER_MILLION
    total_cost = input_cost + output_cost
    return {
        "moneda": "USD",
        "input_cost": round(input_cost, 6),
        "output_cost": round(output_cost, 6),
        "total_cost": round(total_cost, 6),
        "input_rate_per_million": OPENAI_INPUT_COST_PER_MILLION,
        "output_rate_per_million": OPENAI_OUTPUT_COST_PER_MILLION,
    }

def _compute_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _cache_get(key: str) -> Optional[Any]:
    if not CACHE_ENABLED:
        return None
    if key in _simple_cache:
        value, timestamp = _simple_cache[key]
        if time.time() - timestamp < CACHE_TTL:
            return value
        else:
            del _simple_cache[key]
    return None

def _cache_set(key: str, value: Any):
    if not CACHE_ENABLED:
        return
    _simple_cache[key] = (value, time.time())

# ============================================================================
# VALIDACIÓN TEMPRANA DE IMAGEN (ANTES DEL OCR)
# ============================================================================

def _detect_screen_capture_fast(gray: np.ndarray, w: int, h: int) -> bool:
    """
    Detecta captura de pantalla con 2 heurísticas rápidas:
    1. Uniformidad de iluminación muy alta
    2. Patrones periódicos (moiré) via FFT
    """
    indicators = 0

    # 1. Uniformidad de iluminación
    blocks_means = []
    bh, bw = max(h // 3, 1), max(w // 3, 1)
    for i in range(3):
        for j in range(3):
            block = gray[i*bh:(i+1)*bh, j*bw:(j+1)*bw]
            if block.size > 0:
                blocks_means.append(float(np.mean(block)))
    if blocks_means and np.std(blocks_means) < 18:
        indicators += 1

    # 2. Análisis FFT para moiré
    try:
        center_roi = gray[h//4:3*h//4, w//4:3*w//4]
        if center_roi.size > 0:
            fft = np.fft.fftshift(np.fft.fft2(center_roi))
            mag = np.abs(fft)
            mid = mag[mag.shape[0]//4:3*mag.shape[0]//4,
                      mag.shape[1]//4:3*mag.shape[1]//4]
            if np.mean(mid) > 0:
                peak_ratio = np.percentile(mid, 99) / (np.mean(mid) + 1e-6)
                if peak_ratio > 15:
                    indicators += 1
    except Exception:
        pass

    return indicators >= 2


def validate_image_before_ocr(img_bytes: bytes) -> ImageValidationResult:
    """
    Valida la imagen ANTES de enviarla a OpenAI.
    Rechaza imágenes problemáticas sin gastar tokens.

    Checks (del más barato al más caro):
    1. Formato de imagen válido
    2. Resolución mínima
    3. Aspect ratio de comprobante
    4. Blur score (ajustado si es screen capture)
    5. Iluminación extrema
    """
    # ── Cargar imagen ─────────────────────────────────────────────────────────
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception as e:
        return ImageValidationResult(
            accepted=False,
            rejection_code="INVALID_IMAGE",
            rejection_message="No se pudo leer el archivo enviado.",
            user_instructions=[
                "Envía una imagen en formato JPG, PNG o PDF.",
                "Asegúrate de que el archivo no esté corrupto.",
            ],
            technical_reason=str(e),
        )

    w, h = img.size
    cv_img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)

    scores = {
        "resolution_px": w * h,
        "width": w,
        "height": h,
        "aspect_ratio": round(w / h, 2) if h > 0 else 0,
    }

    # ── 1. Resolución mínima ──────────────────────────────────────────────────
    if w * h < MIN_RESOLUTION_PX:
        return ImageValidationResult(
            accepted=False,
            rejection_code="LOW_RESOLUTION",
            rejection_message=(
                f"La imagen es muy pequeña ({w}×{h} px). "
                "El sistema necesita más detalle para leer el comprobante."
            ),
            user_instructions=[
                "📸 Toma la foto más cerca del comprobante.",
                "📱 Usa la cámara principal (no la frontal).",
                "🖨️ Si escaneas, usa al menos 200 DPI.",
            ],
            quality_scores=scores,
        )

    # ── 2. Aspect ratio ───────────────────────────────────────────────────────
    aspect = w / h if h > 0 else 0
    scores["aspect_ratio"] = round(aspect, 2)
    if not (VOUCHER_ASPECT_MIN <= aspect <= VOUCHER_ASPECT_MAX):
        return ImageValidationResult(
            accepted=False,
            rejection_code="INVALID_ASPECT_RATIO",
            rejection_message=(
                "La imagen no parece ser un comprobante completo "
                "(proporción de imagen inusual)."
            ),
            user_instructions=[
                "📄 Asegúrate de capturar el comprobante completo.",
                "📱 Mantén el teléfono en posición vertical.",
                "✂️ No recortes el comprobante antes de enviarlo.",
            ],
            quality_scores=scores,
        )

    # ── 3. Blur + Screen capture ──────────────────────────────────────────────
    blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    is_screen = _detect_screen_capture_fast(gray, w, h)

    scores["blur_score"] = round(blur_score, 1)
    scores["is_screen_capture"] = is_screen

    blur_threshold = BLUR_REJECT_SCREEN_CAPTURE if is_screen else BLUR_REJECT_PHOTO
    scores["blur_threshold_applied"] = blur_threshold

    if blur_score < blur_threshold:
        if is_screen:
            return ImageValidationResult(
                accepted=False,
                rejection_code="SCREEN_CAPTURE_BLURRY",
                rejection_message=(
                    "Se detectó una foto de pantalla con poca nitidez. "
                    "Este tipo de imagen genera errores en los números del comprobante "
                    "y no puede procesarse de forma confiable."
                ),
                user_instructions=[
                    "✅ MEJOR OPCIÓN: Usa 'Compartir' o 'Guardar PDF' en tu app bancaria "
                    "y envía el archivo directamente.",
                    "📱 Si debes fotografiar la pantalla: ajusta el brillo al máximo, "
                    "mantén el teléfono completamente quieto y paralelo a la pantalla.",
                    "🔍 Verifica que el número de comprobante se vea nítido antes de enviar.",
                    "💡 Evita ángulos: la cámara debe estar centrada frente a la pantalla.",
                ],
                technical_reason=(
                    f"is_screen_capture=True, blur={blur_score:.1f} < threshold={blur_threshold}"
                ),
                quality_scores=scores,
            )
        else:
            return ImageValidationResult(
                accepted=False,
                rejection_code="IMAGE_BLURRY",
                rejection_message=(
                    "La imagen está borrosa o con movimiento. "
                    "No se puede leer el comprobante con precisión."
                ),
                user_instructions=[
                    "📸 Apoya los codos o usa ambas manos al tomar la foto.",
                    "⏸️ Espera 1 segundo después de presionar el botón antes de mover el teléfono.",
                    "🔍 El texto del comprobante debe verse nítido en pantalla antes de disparar.",
                    "💡 Busca buena iluminación, sin sombras ni reflejos.",
                    "📄 Coloca el comprobante sobre una superficie plana.",
                ],
                technical_reason=f"blur={blur_score:.1f} < threshold={blur_threshold}",
                quality_scores=scores,
            )

    # ── 4. Iluminación extrema ────────────────────────────────────────────────
    mean_brightness = float(np.mean(gray))
    scores["mean_brightness"] = round(mean_brightness, 1)

    if mean_brightness < 30:
        return ImageValidationResult(
            accepted=False,
            rejection_code="TOO_DARK",
            rejection_message="La imagen está muy oscura. No se puede leer el comprobante.",
            user_instructions=[
                "💡 Toma la foto en un lugar bien iluminado.",
                "🔦 Activa la linterna de tu teléfono si es necesario.",
                "🚫 Evita sombras sobre el comprobante.",
            ],
            quality_scores=scores,
        )

    if mean_brightness > 230:
        return ImageValidationResult(
            accepted=False,
            rejection_code="TOO_BRIGHT",
            rejection_message=(
                "La imagen está sobreexpuesta (muy brillante). "
                "El texto puede estar ilegible por el reflejo."
            ),
            user_instructions=[
                "🚫 Desactiva el flash al tomar la foto.",
                "💡 Aleja la fuente de luz o usa luz indirecta.",
                "📐 Cambia el ángulo de la cámara para evitar reflejos.",
            ],
            quality_scores=scores,
        )

    # ── Advertencia: foto de pantalla aceptable pero con riesgo ──────────────
    if is_screen:
        logger.warning(
            f"Screen capture accepted (blur={blur_score:.1f} >= threshold={blur_threshold}). "
            "Digit errors possible."
        )

    logger.info(
        f"Image pre-validation passed: blur={blur_score:.1f}, "
        f"screen={is_screen}, brightness={mean_brightness:.1f}, "
        f"resolution={w}x{h}"
    )

    return ImageValidationResult(
        accepted=True,
        rejection_code=None,
        rejection_message="",
        user_instructions=[],
        quality_scores=scores,
    )


def build_image_rejection_response(validation: ImageValidationResult) -> dict:
    """Construye respuesta JSON de rechazo de imagen compatible con el cliente."""
    return {
        "accepted": False,
        "rejection_code": validation.rejection_code,
        "rejection_message": validation.rejection_message,
        "user_instructions": validation.user_instructions,
        "quality": validation.quality_scores or {},
        "texto_extraido": "",
        "campos": {},
        "needs_retry": True,
        "retry_reasons": [validation.rejection_code],
        "detected_bank": None,
        "is_valid_voucher": False,
        "voucher_confidence": 0.0,
    }


# ============================================================================
# VALIDACIÓN DE COMPROBANTE (POST-OCR)
# ============================================================================

def _parse_amount(amount_str: str) -> Optional[float]:
    """
    Convierte string de monto a float.
    Soporta: "27.49", "27,49", "$27.49", "1.234,56", "1,234.56"
    """
    if not amount_str:
        return None

    s = amount_str.strip().replace("$", "").replace(" ", "")

    if "," in s and "." in s:
        last_dot = s.rfind(".")
        last_comma = s.rfind(",")
        if last_dot > last_comma:
            s = s.replace(",", "")          # 1,234.56 → 1234.56
        else:
            s = s.replace(".", "").replace(",", ".")  # 1.234,56 → 1234.56
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) == 2:
            s = s.replace(",", ".")         # 27,49 → 27.49
        else:
            s = s.replace(",", "")          # 1,234 → 1234

    try:
        return float(s)
    except ValueError:
        return None


def _validate_date_coherence(fecha_str: str) -> Tuple[bool, str]:
    """
    Valida que la fecha sea coherente (formato reconocible, no futura, no >1 año).
    Returns: (is_valid, mensaje_de_error_o_advertencia)
    """
    if not fecha_str:
        return False, "fecha_no_encontrada"

    month_map = {
        "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AGO": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12,
        "JAN": 1, "AUG": 8, "DEC": 12,
    }

    parsed_date = None

    # Formato "2026/FEB/02"
    m = re.match(r"(\d{4})/([A-Za-z]{3})\.?/(\d{2})", fecha_str)
    if m:
        try:
            year = int(m.group(1))
            mon_str = m.group(2).upper()
            day = int(m.group(3))
            month = month_map.get(mon_str)
            if month:
                parsed_date = datetime(year, month, day)
        except Exception:
            pass

    # Formatos estándar
    if not parsed_date:
        for fmt in ["%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%Y-%m-%d"]:
            try:
                parsed_date = datetime.strptime(fecha_str.strip(), fmt)
                break
            except ValueError:
                continue

    if not parsed_date:
        return False, f"formato_fecha_no_reconocido: '{fecha_str}'"

    now = datetime.now()
    max_past = now - timedelta(days=DATE_TOLERANCE_DAYS_PAST)
    max_future = now + timedelta(days=DATE_TOLERANCE_DAYS_FUTURE)

    if parsed_date < max_past:
        days_ago = (now - parsed_date).days
        return False, f"fecha_demasiado_antigua: {fecha_str} (hace {days_ago} días)"

    if parsed_date > max_future:
        return False, f"fecha_en_el_futuro: {fecha_str}"

    return True, ""


def validate_voucher_fields(campos: dict, raw_text: str = "") -> VoucherValidationResult:
    """
    Valida que los campos extraídos correspondan a un comprobante válido.
    Se ejecuta DESPUÉS del OCR, antes de devolver la respuesta.

    Checks (peso en la confidence):
    - entidad_bancaria       → crítico
    - numero_documento       → crítico
    - fecha coherente        → crítico
    - total > 0              → crítico
    - nombre_depositante     → informativo (peso parcial)
    - keywords de comprobante en texto → crítico
    """
    failing = []
    warnings = []
    validated = {}
    checks_passed = 0.0
    total_checks = 0.0

    # ── Entidad bancaria ──────────────────────────────────────────────────────
    total_checks += 1
    entidad = (campos.get("entidad_bancaria") or "").strip()
    if entidad and len(entidad) >= 3:
        checks_passed += 1
        validated["entidad_bancaria"] = entidad
    else:
        failing.append("entidad_bancaria_no_identificada")

    # ── Número de documento ───────────────────────────────────────────────────
    total_checks += 1
    num_doc = (campos.get("numero_documento") or "").strip()
    if num_doc and len(num_doc) >= 7 and re.search(r"\d", num_doc):
        checks_passed += 1
        validated["numero_documento"] = num_doc
    else:
        failing.append("numero_documento_invalido_o_ausente")

    # ── Fecha coherente ───────────────────────────────────────────────────────
    total_checks += 1
    fecha = (campos.get("fecha") or "").strip()
    fecha_valid, fecha_msg = _validate_date_coherence(fecha)
    if fecha_valid:
        checks_passed += 1
        validated["fecha"] = fecha
    else:
        failing.append(f"fecha_invalida: {fecha_msg}")
    if fecha_msg and fecha_valid:
        warnings.append(fecha_msg)

    # ── Monto / Total ─────────────────────────────────────────────────────────
    total_checks += 1
    total_str = (campos.get("total") or "").strip()
    monto_val = _parse_amount(total_str)
    if monto_val is not None and monto_val > 0:
        checks_passed += 1
        validated["total"] = total_str
        validated["total_numeric"] = monto_val
        if monto_val > 50_000:
            warnings.append(f"monto_inusualmente_alto: {monto_val}")
    else:
        failing.append("monto_invalido_o_cero")

    # ── Nombre depositante (peso parcial, no crítico) ─────────────────────────
    total_checks += 0.5
    nombre = (campos.get("nombre_depositante") or "").strip()
    if nombre and len(nombre) >= 4 and nombre.upper() not in {"CLIENTE", "NOMBRE", "CNB"}:
        checks_passed += 0.5
        validated["nombre_depositante"] = nombre
    else:
        warnings.append("nombre_depositante_no_encontrado_o_generico")

    # ── Keywords de comprobante en el texto ───────────────────────────────────
    total_checks += 1
    voucher_keywords = [
        "COMPROBANTE", "DEPOSITO", "DEPÓSITO", "TRANSFERENCIA",
        "PAGO", "RECIBO", "TRANSACCION", "TRANSACCIÓN", "VOUCHER",
    ]
    text_upper = raw_text.upper()
    has_keyword = any(k in text_upper for k in voucher_keywords)
    if has_keyword:
        checks_passed += 1
        validated["has_voucher_keyword"] = True
    else:
        failing.append("texto_no_contiene_palabras_clave_de_comprobante")

    # ── Confidence y veredicto ────────────────────────────────────────────────
    confidence = checks_passed / total_checks if total_checks > 0 else 0.0
    confidence = max(0.0, min(1.0, confidence))

    # Requiere mínimo de confidence Y número de documento válido
    is_valid = (
        confidence >= VOUCHER_CONFIDENCE_THRESHOLD
        and "numero_documento_invalido_o_ausente" not in failing
    )

    logger.info(
        f"Voucher validation: valid={is_valid}, confidence={confidence:.2f}, "
        f"failing={failing}, warnings={warnings}"
    )

    return VoucherValidationResult(
        is_valid_voucher=is_valid,
        confidence=round(confidence, 2),
        failing_checks=failing,
        warnings=warnings,
        validated_fields=validated,
    )


# ============================================================================
# ANÁLISIS DE CALIDAD DE IMAGEN (pipeline interno)
# ============================================================================

def _blur_score_laplacian(img_bytes: bytes) -> float:
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return 0.0
    cv_img = _pil_to_cv(img)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())

def _calculate_edge_density(img_bytes: bytes) -> float:
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return 0.0
    cv_img = _pil_to_cv(img)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    sobelx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    sobely = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    sobel = np.sqrt(sobelx**2 + sobely**2)
    threshold = sobel.mean() + sobel.std()
    edge_pixels = np.sum(sobel > threshold)
    total_pixels = gray.shape[0] * gray.shape[1]
    return edge_pixels / total_pixels if total_pixels > 0 else 0.0

def _detect_screen_capture(img_bytes: bytes) -> bool:
    """Versión completa (para pipeline interno, post-validación)"""
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return False
    cv_img = _pil_to_cv(img)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    indicators = 0

    common_resolutions = [
        (1920, 1080), (1366, 768), (1280, 720), (2560, 1440),
        (3840, 2160), (1440, 900), (1600, 900)
    ]
    for res_w, res_h in common_resolutions:
        if abs(w - res_w) < 50 and abs(h - res_h) < 50:
            indicators += 2
            break

    center_h, center_w = h // 4, w // 4
    roi = gray[center_h:3*center_h, center_w:3*center_w]
    if roi.size > 0:
        f_transform = np.fft.fft2(roi)
        f_shift = np.fft.fftshift(f_transform)
        magnitude = np.abs(f_shift)
        mid_freq_region = magnitude[
            magnitude.shape[0]//4:3*magnitude.shape[0]//4,
            magnitude.shape[1]//4:3*magnitude.shape[1]//4
        ]
        peaks = np.percentile(mid_freq_region, 99)
        mean_val = np.mean(mid_freq_region)
        if peaks / (mean_val + 1e-6) > 20:
            indicators += 1

    blocks = []
    block_h, block_w = h // 3, w // 3
    for i in range(3):
        for j in range(3):
            block = gray[i*block_h:(i+1)*block_h, j*block_w:(j+1)*block_w]
            if block.size > 0:
                blocks.append(np.mean(block))
    if len(blocks) > 0 and np.std(blocks) < 15:
        indicators += 1

    return indicators >= 2

def _quality_assessment(img_bytes: bytes, include_edge_density: bool = False) -> QualityMetrics:
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return QualityMetrics(
            blur_score=0.0, blur_threshold=BLUR_THRESHOLD,
            is_blurry=True, resolution=(0, 0), is_screen_capture=False
        )
    blur = _blur_score_laplacian(img_bytes)
    w, h = img.size
    is_screen = _detect_screen_capture(img_bytes)
    metrics = QualityMetrics(
        blur_score=blur, blur_threshold=BLUR_THRESHOLD,
        is_blurry=blur < BLUR_THRESHOLD, resolution=(w, h),
        is_screen_capture=is_screen
    )
    if include_edge_density:
        metrics.edge_density = _calculate_edge_density(img_bytes)
    return metrics

_tesseract_score_cache = {}

def _quick_score_tesseract(img_bytes: bytes) -> int:
    img_hash = _compute_hash(img_bytes)
    if img_hash in _tesseract_score_cache:
        return _tesseract_score_cache[img_hash]
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return 0
    cv_img = _pil_to_cv(img)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    text = pytesseract.image_to_string(gray, lang="spa+eng", config="--psm 6")
    score = _score_ocr_text(text)
    _tesseract_score_cache[img_hash] = score
    return score

# ============================================================================
# PREPROCESAMIENTO DE IMÁGENES
# ============================================================================

def _pil_to_cv(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

def _cv_to_jpeg_bytes(cv_img: np.ndarray, quality: int = 92) -> Optional[bytes]:
    ok, buf = cv2.imencode(".jpg", cv_img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return None
    return buf.tobytes()

def _prepare_image_for_openai(img_bytes: bytes) -> Optional[bytes]:
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return None
    w, h = img.size
    img = img.resize((int(w * 1.5), int(h * 1.5)), Image.BICUBIC)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

def _remove_moire_pattern(img_bytes: bytes) -> Optional[bytes]:
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return None
    cv_img = _pil_to_cv(img)
    cv_img = cv2.resize(cv_img, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
    cv_img = cv2.bilateralFilter(cv_img, 9, 75, 75)
    cv_img = cv2.GaussianBlur(cv_img, (3, 3), 0.5)
    kernel = np.array([[-0.5, -0.5, -0.5],
                       [-0.5,  5.0, -0.5],
                       [-0.5, -0.5, -0.5]], dtype=np.float32)
    cv_img = cv2.filter2D(cv_img, -1, kernel)
    return _cv_to_jpeg_bytes(cv_img, quality=94)

def _enhance_variant(img_bytes: bytes) -> Optional[bytes]:
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return None
    cv_img = _pil_to_cv(img)
    cv_img = cv2.resize(cv_img, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
    lab = cv2.cvtColor(cv_img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    lab = cv2.merge((l, a, b))
    cv_img = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    cv_img = cv2.bilateralFilter(cv_img, 7, 50, 50)
    kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
    cv_img = cv2.filter2D(cv_img, -1, kernel)
    return _cv_to_jpeg_bytes(cv_img, quality=94)

def _binarized_variant(img_bytes: bytes) -> Optional[bytes]:
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return None
    cv_img = _pil_to_cv(img)
    cv_img = cv2.resize(cv_img, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    gray = cv2.bilateralFilter(gray, 9, 75, 75)
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
    )
    cv_img = cv2.cvtColor(thresh, cv2.COLOR_GRAY2BGR)
    return _cv_to_jpeg_bytes(cv_img, quality=94)

def _generate_variants(img_bytes: bytes, apply_demoire: bool = False) -> List[Tuple[str, bytes]]:
    variants = []
    if apply_demoire:
        demoire = _remove_moire_pattern(img_bytes)
        if demoire:
            variants.append(("demoire", demoire))
    base = _prepare_image_for_openai(img_bytes)
    if base:
        variants.append(("base", base))
    enhanced = _enhance_variant(img_bytes)
    if enhanced:
        variants.append(("enhanced", enhanced))
    binarized = _binarized_variant(img_bytes)
    if binarized:
        variants.append(("binarized", binarized))
    return variants

# ============================================================================
# OCR CON OPENAI
# ============================================================================

def _ocr_with_openai(image_bytes: bytes, mime_type: str) -> Tuple[str, Dict[str, int]]:
    prompt = (
        "Eres un sistema de OCR. Extrae TODO el texto visible del comprobante. "
        "Respeta saltos de línea cuando sea posible. No inventes datos y no "
        "agregues explicaciones. Devuelve solo el texto. "
        "Si no se puede leer con certeza, devuelve una cadena vacía."
    )
    data_url = _image_to_data_url(image_bytes, mime_type)
    response = client.responses.create(
        model=OPENAI_OCR_MODEL,
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": data_url, "detail": OPENAI_OCR_DETAIL},
            ],
        }],
    )
    usage = _get_usage_dict(getattr(response, "usage", None))
    return response.output_text.strip(), usage

def _parse_model_json(text: str) -> Optional[Dict]:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None

def _extract_structured_from_text(raw_text: str) -> Tuple[Optional[Dict], Dict[str, int]]:
    prompt = (
        "Extrae campos del comprobante y devuelve SOLO JSON válido (sin markdown). "
        "Usa EXACTAMENTE esta estructura:\n"
        f"{CORE_FIELDS_SCHEMA}\n"
        "Reglas estrictas:\n"
        "- SOLO copia valores que aparezcan literalmente en raw_text.\n"
        "- No normalices formatos; conserva exactamente lo visto.\n"
        "- Si un campo no existe, usa null.\n"
        "- No inventes datos.\n"
        "- Si hay varias fechas/montos, elige el que represente el total/valor final.\n"
    )
    response = client.responses.create(
        model=OPENAI_OCR_MODEL,
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_text", "text": raw_text},
            ],
        }],
    )
    usage = _get_usage_dict(getattr(response, "usage", None))
    return _parse_model_json(response.output_text.strip()), usage

def _ocr_structured_with_openai(image_bytes: bytes, mime_type: str) -> Tuple[Optional[Dict], Dict[str, int]]:
    prompt = (
        "Eres un sistema de OCR. Extrae TODO el texto visible y además "
        "devuelve campos estructurados. Devuelve SOLO JSON válido (sin markdown). "
        "Usa EXACTAMENTE esta estructura:\n"
        f"{CORE_FIELDS_SCHEMA}\n"
        "Reglas estrictas:\n"
        "- SOLO copia valores que aparezcan literalmente en el comprobante.\n"
        "- No normalices formatos; conserva exactamente lo visto.\n"
        "- Si un campo no existe, usa null.\n"
        "- No inventes datos."
    )
    data_url = _image_to_data_url(image_bytes, mime_type)
    response = client.responses.create(
        model=OPENAI_OCR_MODEL,
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": data_url, "detail": OPENAI_OCR_DETAIL},
            ],
        }],
    )
    usage = _get_usage_dict(getattr(response, "usage", None))
    return _parse_model_json(response.output_text.strip()), usage

# ============================================================================
# SCORING Y SELECCIÓN DE MEJOR RESULTADO
# ============================================================================

def _score_ocr_text(text: str) -> int:
    if not text:
        return 0
    t = text.upper()
    score = 0
    keywords = [
        "COMPROBANTE", "DEPOSITO", "DEPÓSITO", "TRANSFERENCIA",
        "BANCO", "COOPERATIVA", "CUENTA", "DOCUMENTO", "CONTROL"
    ]
    for k in keywords:
        if k in t:
            score += 20
    if re.search(r"\b\d{2}/\d{2}/\d{4}\b", text):
        score += 60
    if re.search(r"\b\d{2}:\d{2}(:\d{2})?\b", text):
        score += 40
    if re.search(r"\b\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})\b", text):
        score += 35
    if "USD" in t or "DOLAR" in t or "DÓLAR" in t:
        score += 15
    if re.search(r"\b(RUC|C\.?I\.?|CI)\b", t):
        score += 30
    if re.search(r"\b(DOCUMENTO|COMPROBANTE|CONTROL|NO\.?|NRO\.?)\b", t):
        score += 30
    if len(text.strip()) < 40:
        score -= 40
    return score

def _score_structured(parsed: dict) -> int:
    raw = (parsed.get("raw_text") or "")
    fields = parsed.get("fields") or {}
    s = _score_ocr_text(raw)
    critical = ["numero_documento", "entidad_bancaria", "fecha", "total"]
    s += 50 * sum(1 for k in critical if fields.get(k))
    miss = _missing_or_suspicious_fields(fields)
    s -= 40 * sum(1 for k in miss["missing"] if k in {"numero_documento", "entidad_bancaria", "fecha", "total"})
    s -= 30 * sum(1 for k in miss["suspicious"] if k in {"numero_documento", "entidad_bancaria", "fecha", "total"})
    return s

def _ocr_with_openai_best(img_bytes: bytes, mime_type: str, apply_demoire: bool = False) -> Tuple[str, Dict[str, int], Optional[str]]:
    best_text, best_usage, best_score, best_variant = "", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, -1, None
    for variant_name, variant_bytes in _generate_variants(img_bytes, apply_demoire):
        text, usage = _ocr_with_openai(variant_bytes, mime_type)
        score = _score_ocr_text(text)
        if score > best_score:
            best_score, best_text, best_usage, best_variant = score, text, usage, variant_name
    return best_text, best_usage, best_variant

def _ocr_structured_with_openai_best(img_bytes: bytes, mime_type: str = "image/jpeg", apply_demoire: bool = False) -> Tuple[Optional[Dict], Dict[str, int], Optional[str]]:
    best, total_usage, best_score, best_variant = None, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, -10**9, None
    for variant_name, variant_bytes in _generate_variants(img_bytes, apply_demoire):
        parsed, usage = _ocr_structured_with_openai(variant_bytes, mime_type)
        total_usage = _add_usage(total_usage, usage)
        if not isinstance(parsed, dict):
            continue
        if not isinstance(parsed.get("fields"), dict):
            continue
        s = _score_structured(parsed)
        if s > best_score:
            best_score, best, best_variant = s, parsed, variant_name
    return best, total_usage, best_variant

# ============================================================================
# DETECCIÓN Y EXTRACCIÓN ESPECÍFICA POR BANCO
# ============================================================================

def _detect_bank(texto: str, campos: Dict[str, Any]) -> Optional[str]:
    sources = []
    if campos.get("entidad_bancaria"):
        sources.append(campos["entidad_bancaria"].upper())
    if texto:
        lines = [ln.strip() for ln in texto.splitlines() if ln.strip()]
        sources.extend([ln.upper() for ln in lines[:8]])
        sources.append(texto.upper())
    combined = " ".join(sources)
    for bank_code, bank_info in BANK_PATTERNS.items():
        for keyword in bank_info["keywords"]:
            if keyword in combined:
                logger.info(f"Bank detected: {bank_code} (keyword: {keyword})")
                return bank_code
    return None

def _normalize_jep_prefix(doc: str) -> str:
    if not doc:
        return doc
    d = doc.strip().upper().replace(" ", "")
    if re.fullmatch(r"J[ VY]\d{4}[A-Z]{3}\d{11}", d):
        if d.startswith("JY"):
            d = "JV" + d[2:]
    return d

def _extract_docnum_by_bank_from_text(texto: str, bank: Optional[str] = None) -> Optional[str]:
    if not texto or not bank or bank not in BANK_PATTERNS:
        return None
    bank_info = BANK_PATTERNS[bank]
    pattern = bank_info["doc_pattern"]
    requires_label = bank_info.get("requires_label", True)
    t = texto.upper().replace(" ", "")
    if not requires_label:
        for m in re.finditer(pattern, t):
            return _normalize_jep_prefix(m.group(0))
        return None
    doc_labels = bank_info.get("doc_labels", list(DOC_LABELS))
    lines = [ln.strip() for ln in texto.splitlines() if ln.strip()]
    for line in lines:
        line_upper = line.upper()
        if not any(label in line_upper for label in doc_labels):
            continue
        for m in re.finditer(pattern, line.replace(" ", "").upper()):
            candidate = m.group(0)
            if re.search(r"\d{2}/\d{2}/\d{4}", candidate):
                continue
            if re.search(r"\d{2}:\d{2}", candidate):
                continue
            return _normalize_jep_prefix(candidate)
    return None

def _docnum_from_crop_generic(img_bytes: bytes, bank: Optional[str] = None) -> Optional[str]:
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return None
    cv_img = _pil_to_cv(img)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.bilateralFilter(gray, 9, 75, 75)

    def norm(w: str) -> str:
        w = (w or "").strip().upper()
        w = w.replace(":", "").replace(".", "").replace("N°", "Nº").replace("°", "º")
        return w

    def iter_label_candidates(d):
        for i, word in enumerate(d.get("text", [])):
            if norm(word) in DOC_LABELS:
                yield i, norm(word)

    data = pytesseract.image_to_data(gray, lang="spa+eng", output_type=pytesseract.Output.DICT, config="--psm 6")
    label_hits = list(iter_label_candidates(data))
    if not label_hits:
        data = pytesseract.image_to_data(gray, lang="spa+eng", output_type=pytesseract.Output.DICT, config="--psm 11")
        label_hits = list(iter_label_candidates(data))

    bank_pattern = BANK_PATTERNS[bank]["doc_pattern"] if bank and bank in BANK_PATTERNS else None

    for i, _ in label_hits:
        near_text = " ".join(data.get("text", [])[max(0, i-3):min(len(data.get("text", [])), i+6)]).upper()
        if not re.search(r"\d", near_text):
            continue
        x, y = data["left"][i], data["top"][i]
        bw, bh = data["width"][i], data["height"][i]
        pad_y = int(bh * 1.5)
        x1 = max(x - int(bw * 0.5), 0)
        y1 = max(y - pad_y, 0)
        x2 = min(x + bw + int(bw * 25), gray.shape[1])
        y2 = min(y + bh + pad_y, gray.shape[0])
        crop = gray[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        variants = [
            crop,
            cv2.adaptiveThreshold(crop, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 31, 11),
            cv2.adaptiveThreshold(crop, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11),
            cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
        ]
        cfgs = [
            "--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-/#",
            "--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-/#",
        ]
        all_cands: List[str] = []
        for v in variants:
            for cfg in cfgs:
                txt = pytesseract.image_to_string(v, lang="spa+eng", config=cfg).strip().upper()
                pat = bank_pattern or r'\b[A-Z0-9][A-Z0-9\-/#]{7,}\b'
                for match in re.finditer(pat, txt):
                    all_cands.append(match.group(0))
        filtered = [
            c for c in all_cands
            if not re.search(r"\d{2}/\d{2}/\d{4}", c)
            and not re.search(r"\d{2}:\d{2}", c)
            and not re.search(r"\d+[.,]\d{2}", c)
            and re.search(r"\d", c)
        ]
        if not filtered:
            continue
        filtered = list(dict.fromkeys(filtered))
        filtered.sort(key=lambda s: (len(s), bool(re.search(r"[A-Z]", s)), bool(re.search(r"\d", s))), reverse=True)
        best = filtered[0]
        same_len = [c for c in filtered if len(c) == len(best)]
        close = [c for c in same_len if difflib.SequenceMatcher(None, best, c).ratio() >= 0.92]
        if len(close) >= 2:
            out = []
            for pos in range(len(best)):
                freq = {}
                for c in close:
                    if pos < len(c):
                        ch = c[pos]
                        freq[ch] = freq.get(ch, 0) + 1
                if freq:
                    top = sorted(freq.items(), key=lambda x: (x[1], 1 if x[0] == best[pos] else 0), reverse=True)[0][0]
                    out.append(top)
            voted = "".join(out)
            if _is_valid_docnum(voted) and difflib.SequenceMatcher(None, best, voted).ratio() >= 0.95:
                return voted
        return best
    return None

def _refine_last_digits_docnum_aggressive(prepared_img_bytes: bytes, current_doc: str) -> Optional[str]:
    if not prepared_img_bytes or not current_doc:
        return None
    cur = current_doc.strip().upper()
    if not re.fullmatch(r"JV\d{4}[A-Z]{3}\d{11}", cur):
        return None
    tail_len = 6
    cur_tail = cur[-tail_len:]
    try:
        img = Image.open(io.BytesIO(prepared_img_bytes)).convert("RGB")
    except Exception:
        return None
    cv_img = _pil_to_cv(img)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    scales = [2.0, 2.5, 3.0]
    all_tail_readings = []

    def norm(w: str) -> str:
        w = (w or "").strip().upper()
        return w.replace(":", "").replace(".", "").replace("N°", "Nº").replace("°", "º")

    for scale in scales:
        scaled_gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        scaled_gray = cv2.bilateralFilter(scaled_gray, 9, 75, 75)
        data = pytesseract.image_to_data(scaled_gray, lang="spa+eng", output_type=pytesseract.Output.DICT, config="--psm 6")
        label_idx = next((i for i, word in enumerate(data.get("text", [])) if norm(word) in DOC_LABELS), None)
        if label_idx is None:
            continue
        x, y = data["left"][label_idx], data["top"][label_idx]
        bw, bh = data["width"][label_idx], data["height"][label_idx]
        pad_y = int(bh * 1.5)
        x1, y1 = max(x - int(bw * 0.5), 0), max(y - pad_y, 0)
        x2 = min(x + bw + int(bw * 30), scaled_gray.shape[1])
        y2 = min(y + bh + pad_y, scaled_gray.shape[0])
        crop = scaled_gray[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        h_c, w_c = crop.shape[:2]
        right = crop[:, int(w_c * 0.55):]
        if right.size == 0:
            continue
        variants = [
            right,
            cv2.adaptiveThreshold(right, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11),
            cv2.adaptiveThreshold(right, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 31, 11),
            cv2.threshold(right, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
        ]
        for v in variants:
            for cfg in ["--psm 7 -c tessedit_char_whitelist=0123456789", "--psm 8 -c tessedit_char_whitelist=0123456789"]:
                txt = pytesseract.image_to_string(v, lang="eng", config=cfg).strip()
                m = re.search(rf"(\d{{{tail_len}}})\b", txt)
                if m:
                    all_tail_readings.append(m.group(1))

    if not all_tail_readings:
        return None

    voted_tail = []
    for pos in range(tail_len):
        digit_votes = Counter(r[pos] for r in all_tail_readings if len(r) == tail_len)
        if digit_votes:
            voted_tail.append(digit_votes.most_common(1)[0][0])

    if len(voted_tail) != tail_len:
        return None

    voted_tail_str = "".join(voted_tail)
    if voted_tail_str != cur_tail:
        diffs = [(i, a, b) for i, (a, b) in enumerate(zip(cur_tail, voted_tail_str)) if a != b]
        if 1 <= len(diffs) <= 2 and any({a, b} <= {"3", "9"} for _, a, b in diffs):
            corrected = cur[:-tail_len] + voted_tail_str
            logger.info(f"[REFINE_AGGRESSIVE] {cur} -> {corrected}")
            return corrected
    return None

def _refine_last_digits_generic(prepared_img_bytes: bytes, current_doc: str, tail_len: int = 4) -> Optional[str]:
    if not prepared_img_bytes or not current_doc:
        return None
    cur = current_doc.strip().upper()
    if len(cur) < tail_len + 3 or not cur[-tail_len:].isdigit():
        return None
    cur_tail = cur[-tail_len:]
    try:
        img = Image.open(io.BytesIO(prepared_img_bytes)).convert("RGB")
    except Exception:
        return None
    cv_img = _pil_to_cv(img)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2.2, fy=2.2, interpolation=cv2.INTER_CUBIC)
    gray = cv2.bilateralFilter(gray, 9, 75, 75)

    def norm(w: str) -> str:
        return (w or "").strip().upper().replace(":", "").replace(".", "").replace("N°", "Nº").replace("°", "º")

    data = pytesseract.image_to_data(gray, lang="spa+eng", output_type=pytesseract.Output.DICT, config="--psm 6")
    label_idx = next((i for i, word in enumerate(data.get("text", [])) if norm(word) in DOC_LABELS), None)
    if label_idx is None:
        return None
    x, y = data["left"][label_idx], data["top"][label_idx]
    bw, bh = data["width"][label_idx], data["height"][label_idx]
    pad_y = int(bh * 1.5)
    x1, y1 = max(x - int(bw * 0.5), 0), max(y - pad_y, 0)
    x2 = min(x + bw + int(bw * 30), gray.shape[1])
    y2 = min(y + bh + pad_y, gray.shape[0])
    crop = gray[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    h_c, w_c = crop.shape[:2]
    right = crop[:, int(w_c * 0.60):]
    if right.size == 0:
        return None
    variants = [
        right,
        cv2.adaptiveThreshold(right, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11),
        cv2.threshold(right, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
    ]
    tails: List[str] = []
    for v in variants:
        for cfg in ["--psm 7 -c tessedit_char_whitelist=0123456789", "--psm 8 -c tessedit_char_whitelist=0123456789"]:
            txt = pytesseract.image_to_string(v, lang="eng", config=cfg).strip()
            m = re.search(rf"(\d{{{tail_len}}})\b", txt)
            if m:
                tails.append(m.group(1))
    if not tails:
        return None
    voted_tail, _ = Counter(tails).most_common(1)[0]
    if voted_tail != cur_tail:
        diffs = [(a, b) for a, b in zip(cur_tail, voted_tail) if a != b]
        common_confusions = {frozenset({"3", "9"}), frozenset({"8", "0"}), frozenset({"1", "7"}), frozenset({"5", "6"})}
        if len(diffs) == 1 and frozenset(diffs[0]) in common_confusions:
            return cur[:-tail_len] + voted_tail
    return None

# ============================================================================
# EXTRACCIÓN REGEX Y POST-PROCESAMIENTO
# ============================================================================

def _clean_spaces(s: str) -> str:
    return re.sub(r"[ \t]+", " ", (s or "").strip())

def _is_valid_total(s: str) -> bool:
    return bool(s and re.search(r"\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})", s))

def _is_valid_date(s: str) -> bool:
    return bool(s and (
        re.search(r"\b\d{2}/\d{2}/\d{4}\b", s) or
        re.search(r"\b\d{4}/[A-Za-zñÑ]{3,}\.?/\d{2}\b", s)
    ))

def _is_valid_ci_ruc(s: str) -> bool:
    if not s:
        return False
    s = re.sub(r"\D", "", s)
    return 6 <= len(s) <= 13

def _is_valid_docnum(s: str) -> bool:
    if not s:
        return False
    s = s.strip()
    return (
        len(s) >= 7 and
        len(re.findall(r"\d", s)) >= 3 and
        bool(re.fullmatch(r"[A-Z0-9\-/#]+", s, flags=re.I))
    )

def _missing_or_suspicious_fields(fields: dict) -> dict:
    core = {
        "numero_documento": fields.get("numero_documento"),
        "nombre_depositante": fields.get("nombre_depositante"),
        "ci_ruc": fields.get("ci_ruc"),
        "entidad_bancaria": fields.get("entidad_bancaria"),
        "fecha": fields.get("fecha"),
        "total": fields.get("total"),
    }
    missing = [k for k, v in core.items() if not (v and str(v).strip())]
    suspicious = []
    if core["total"] and not _is_valid_total(core["total"]):
        suspicious.append("total")
    if core["fecha"] and not _is_valid_date(core["fecha"]):
        suspicious.append("fecha")
    if core["ci_ruc"] and not _is_valid_ci_ruc(core["ci_ruc"]):
        suspicious.append("ci_ruc")
    if core["numero_documento"] and not _is_valid_docnum(core["numero_documento"]):
        suspicious.append("numero_documento")
    n = (core["nombre_depositante"] or "").strip().upper()
    if n and (len(n) < 3 or n in {"CLIENTE", "NOMBRE", "CNB"}):
        suspicious.append("nombre_depositante")
    return {"core": core, "missing": missing, "suspicious": suspicious}

def _regex_extract_core(raw_text: str) -> Dict[str, str]:
    if not raw_text:
        return {}
    t = raw_text.replace("\u00a0", " ")
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    upper = t.upper()
    out = {}

    for line in lines[:10]:
        u = line.upper()
        if any(k in u for k in ["BANCO", "COOPERATIVA", "JEP", "PICHINCHA", "PRODUBANCO", "GUAYAQUIL", "CAC"]):
            if len(re.findall(r"\d", line)) <= 3 and len(line) <= 55:
                out["entidad_bancaria"] = _clean_spaces(line)
                break

    m = re.search(r"\b(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}(?::\d{2})?)\b", t)
    if m:
        out["fecha"], out["hora"] = m.group(1), m.group(2)
    else:
        m = re.search(r"\b(\d{4}/[A-Za-zñÑ]{3,}\.?/\d{2})\s+(\d{2}:\d{2})\b", t)
        if m:
            out["fecha"], out["hora"] = _clean_spaces(m.group(1)), m.group(2)
        else:
            m = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", t)
            if m:
                out["fecha"] = m.group(1)

    money_pat = r"(\$?\s*)(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2}))"
    for lab in ["TOTAL", "VALOR DEPOSITADO", "VALOR", "MONTO", "IMPORTE", "EFECTIVO"]:
        mm = re.search(rf"\b{lab}\b\s*[:.]?\s*{money_pat}", t, flags=re.I)
        if mm:
            out["total"] = _clean_spaces((mm.group(1) or "") + (mm.group(2) or ""))
            break
    if not out.get("total"):
        candidates = re.findall(money_pat, t)
        if candidates:
            out["total"] = _clean_spaces((candidates[-1][0] or "") + (candidates[-1][1] or ""))

    mm = re.search(r"\b(DOCUMENTO|COMPROBANTE|NRO\.?|NO\.?|Nº|CONTROL)\s*[:.]?\s*([A-Z0-9\-/#]{5,})\b", upper)
    if mm:
        out["numero_documento"] = _normalize_jep_prefix(mm.group(2).strip())
    else:
        mm = re.search(r"\bNO\.\s*([A-Z0-9]{8,})\b", upper) or re.search(r"\bNO\.([A-Z0-9]{8,})\b", upper.replace(" ", ""))
        if mm:
            out["numero_documento"] = _normalize_jep_prefix(mm.group(1).strip())

    mm = re.search(r"\bNOMBRE\s*(CNB)?\s*[:.]?\s*([A-ZÁÉÍÓÚÑ0-9 \-]{3,})\b", upper)
    if mm:
        val = mm.group(2)
        val = re.split(r"\b(CI|C\.I\.|RUC|CUENTA|VALOR|TOTAL|FECHA|HORA)\b", val, maxsplit=1)[0]
        out["nombre_depositante"] = _clean_spaces(val)

    mm = re.search(r"\b(RUC|C\.?I\.?|CI)\s*(CNB)?\s*[:.]?\s*([0-9]{6,13})\b", upper)
    if mm:
        out["ci_ruc"] = mm.group(3).strip()

    return out

def _find_first_labeled_value(lines: List[str], labels: List[str]) -> Optional[str]:
    for line in lines:
        for label in labels:
            if label.upper() in line.upper():
                m = re.search(rf"{re.escape(label)}\s*[:.]?\s*(.+)$", line, flags=re.I)
                if m:
                    v = m.group(1).strip()
                    if v:
                        return v
    return None

def _find_doc_number(lines: List[str]) -> Optional[str]:
    for ln in lines:
        u = ln.upper()
        m = re.search(r"\b(NO\.?|NRO\.?|Nº|REF\.?|REFERENCIA)\s*[:.]?\s*([A-Z0-9\-/#]{7,})\b", u)
        if m:
            return _normalize_jep_prefix(m.group(2).strip())
        m2 = re.search(r"\bNO\.\s*([A-Z0-9]{8,})\b", u)
        if m2:
            return _normalize_jep_prefix(m2.group(1).strip())
    return None

def _post_process_fields(raw_text: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(fields, dict):
        fields = {}
    if not raw_text:
        return fields
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]

    def set_if_missing(key, value):
        if not fields.get(key) and value:
            fields[key] = value

    for ln in lines[:8]:
        if any(k in ln.upper() for k in ["BANCO", "COOPERATIVA", "JEP", "CAC"]):
            set_if_missing("entidad_bancaria", ln)
            break

    m = re.search(r"\b(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2})\b", raw_text)
    if m:
        set_if_missing("fecha", m.group(1))
        set_if_missing("hora", m.group(2))
    else:
        m = re.search(r"\b(\d{4}/[A-Za-zñÑ]{3,}\.?/\d{2})\s+(\d{2}:\d{2})\b", raw_text)
        if m:
            set_if_missing("fecha", m.group(1))
            set_if_missing("hora", m.group(2))

    set_if_missing("numero_documento", _find_doc_number(lines))

    money_pat = r"(\$?\s*\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2}))"
    for lab in ["TOTAL", "EFECTIVO", "VALOR", "MONTO", "IMPORTE"]:
        mm = re.search(rf"{lab}\s*[:.]?\s*{money_pat}", raw_text, flags=re.I)
        if mm:
            set_if_missing("total", mm.group(1).strip())
            break

    set_if_missing("nombre_depositante", _find_first_labeled_value(lines, ["DEPOSITANTE", "REALIZADO POR", "NOMBRE"]))

    ci_ruc = _find_first_labeled_value(lines, ["C.I", "CI", "RUC"])
    if ci_ruc:
        ci_ruc_clean = re.sub(r"\s+", "", ci_ruc)
        if len(ci_ruc_clean) < 6 or not any(ch.isdigit() for ch in ci_ruc_clean):
            ci_ruc = None
    set_if_missing("ci_ruc", ci_ruc)

    return fields

# ============================================================================
# PROCESAMIENTO DE PDF
# ============================================================================

def _ocr_pdf(pdf_bytes: bytes) -> Tuple[str, Dict[str, int]]:
    try:
        pages = convert_from_bytes(pdf_bytes)
    except Exception:
        return "", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    pages = pages[:PDF_MAX_PAGES]
    textos = []
    usage_total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for page in pages:
        buf = io.BytesIO()
        page.save(buf, format="JPEG", quality=92)
        prepared = _prepare_image_for_openai(buf.getvalue())
        image_bytes = prepared if prepared else buf.getvalue()
        text, usage, _ = _ocr_with_openai_best(image_bytes, "image/jpeg")
        textos.append(text)
        usage_total = _add_usage(usage_total, usage)
    return "\n\n".join(textos).strip(), usage_total

# ============================================================================
# LÓGICA DE RETRY
# ============================================================================

def _should_retry(quality: QualityMetrics, missing_info: dict) -> Tuple[bool, List[str]]:
    reasons = []
    if quality.is_blurry:
        reasons.append("imagen_borrosa_o_movida")
    critical = {"numero_documento", "entidad_bancaria", "fecha", "total"}
    if any(f in critical for f in missing_info["missing"]):
        reasons.append("faltan_campos_criticos")
    if any(f in critical for f in missing_info["suspicious"]):
        reasons.append("campos_criticos_sospechosos")
    if len([f for f in missing_info["missing"] if f in critical]) >= 2:
        reasons.append("demasiados_campos_criticos_faltantes")
    return len(reasons) > 0, reasons

def _retry_instructions() -> List[str]:
    return [
        "Toma la foto lo más cerca posible (que el texto ocupe casi toda la imagen).",
        "Evita movimiento: apoya los codos o usa ambas manos y espera 1 segundo antes de disparar.",
        "Buena luz, sin sombras fuertes ni reflejos (evita flash directo).",
        "Comprobante plano (sin arrugas) y cámara paralela al papel (no en diagonal).",
        "Si es ticket, intenta sobre fondo oscuro para mejorar contraste.",
    ]

# ============================================================================
# PIPELINE PRINCIPAL
# ============================================================================

class OCRPipeline:
    def __init__(self):
        self.metrics: List[PhaseMetrics] = []

    def _record_phase(self, phase: str, duration_ms: float, success: bool,
                     score: Optional[float] = None, changes: Optional[Dict] = None, error: Optional[str] = None):
        self.metrics.append(PhaseMetrics(phase=phase, duration_ms=duration_ms, success=success,
                                         score=score, changes=changes, error=error))

    def _phase(self, name: str):
        class PhaseContext:
            def __init__(self, pipeline, phase_name):
                self.pipeline, self.phase_name, self.start_time = pipeline, phase_name, None
            def __enter__(self):
                self.start_time = time.time()
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                duration = (time.time() - self.start_time) * 1000
                if exc_type is not None:
                    self.pipeline._record_phase(self.phase_name, duration, False, error=str(exc_val))
                return False
        return PhaseContext(self, name)

    def process(self, img_bytes: bytes, is_pdf: bool = False) -> ProcessingResult:
        usage_total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        texto, campos, quality, prepared, detected_bank = "", {}, None, None, None

        # FASE 1: INGEST
        with self._phase("ingest"):
            if is_pdf:
                quality = QualityMetrics(
                    blur_score=0.0, blur_threshold=BLUR_THRESHOLD,
                    is_blurry=False, resolution=(0, 0), is_screen_capture=False
                )
            else:
                prepared = _prepare_image_for_openai(img_bytes)
                if prepared is None:
                    raise ValueError("Invalid image")

        # FASE 2: QUALITY ASSESSMENT
        if not is_pdf:
            with self._phase("quality_assessment"):
                quality = _quality_assessment(img_bytes, include_edge_density=True)
                # Hard fail ajustado por screen capture
                blur_limit = max(BLUR_HARD_FAIL, BLUR_REJECT_SCREEN_CAPTURE) if quality.is_screen_capture else BLUR_HARD_FAIL
                if quality.blur_score < blur_limit:
                    logger.warning(f"Hard fail: blur={quality.blur_score:.1f} < {blur_limit}, screen={quality.is_screen_capture}")
                    return ProcessingResult(
                        texto_extraido="", campos={},
                        uso=Usage(**usage_total), costo_estimado=_estimate_cost(usage_total),
                        quality=quality, validation={"core": {}, "missing": [], "suspicious": []},
                        needs_retry=True, retry_reasons=["imagen_demasiado_borrosa"],
                        retry_instructions=_retry_instructions(),
                        processing_metrics=[asdict(m) for m in self.metrics]
                    )

        # FASE 3: SCREEN DETECTION & DEMOIRE
        apply_demoire = False
        if not is_pdf and quality and quality.is_screen_capture:
            with self._phase("demoire_decision"):
                score_base = _quick_score_tesseract(img_bytes)
                if score_base >= 140:
                    quality.demoire_applied = False
                    quality.demoire_improved = False
                else:
                    demoire_img = _remove_moire_pattern(img_bytes)
                    if demoire_img:
                        score_demoire = _quick_score_tesseract(demoire_img)
                        if score_demoire >= score_base + 30:
                            apply_demoire = True
                            quality.demoire_applied = True
                            quality.demoire_improved = True
                        else:
                            quality.demoire_applied = False
                            quality.demoire_improved = False

        # FASE 4: OCR PRIMARY
        if is_pdf:
            with self._phase("ocr_pdf"):
                texto, usage_ocr = _ocr_pdf(img_bytes)
                usage_total = _add_usage(usage_total, usage_ocr)
        else:
            with self._phase("ocr_structured"):
                structured_img, usage_img, variant = _ocr_structured_with_openai_best(
                    prepared, "image/jpeg", apply_demoire=apply_demoire
                )
                usage_total = _add_usage(usage_total, usage_img)
                logger.info(f"Best structured variant: {variant}")
                if structured_img and isinstance(structured_img, dict):
                    texto = (structured_img.get("raw_text") or "").strip()
                    campos = structured_img.get("fields") or {}
                    campos["numero_documento"] = _normalize_jep_prefix(campos.get("numero_documento"))

        # FASE 5: FALLBACK OCR
        if not texto:
            with self._phase("ocr_fallback"):
                if not is_pdf:
                    texto, usage_ocr, variant = _ocr_with_openai_best(
                        prepared, "image/jpeg", apply_demoire=apply_demoire
                    )
                    usage_total = _add_usage(usage_total, usage_ocr)
                if texto and not campos:
                    structured, usage_struct = _extract_structured_from_text(texto)
                    usage_total = _add_usage(usage_total, usage_struct)
                    if structured and isinstance(structured, dict):
                        campos = structured.get("fields") or {}
                        campos["numero_documento"] = _normalize_jep_prefix(campos.get("numero_documento"))

        # FASE 6: BANK DETECTION
        with self._phase("bank_detection"):
            if not isinstance(campos, dict):
                campos = {}
            detected_bank = _detect_bank(texto, campos)

        # FASE 7: REGEX EXTRACTION
        with self._phase("regex_extraction"):
            for k, v in _regex_extract_core(texto).items():
                if v and not campos.get(k):
                    campos[k] = _normalize_jep_prefix(v) if k == "numero_documento" else v

        # FASE 8: POST-PROCESSING
        with self._phase("post_processing"):
            campos = _post_process_fields(texto, campos)
            if detected_bank:
                doc_bank = _extract_docnum_by_bank_from_text(texto, detected_bank)
                if doc_bank and _is_valid_docnum(doc_bank):
                    campos["numero_documento"] = doc_bank

        # FASE 9: REFINEMENT (solo imágenes)
        if not is_pdf and prepared is not None:
            with self._phase("refinement_crop"):
                doc_current = (campos.get("numero_documento") or "").strip()
                need_refine = not doc_current or not _is_valid_docnum(doc_current)
                has_no_label = bool(re.search(r"\b(NO|NRO|Nº|REF)\.?\s*[:#-]?\s*[A-Z0-9\-/#]*\d", texto or "", flags=re.I))
                doc_refined = _docnum_from_crop_generic(prepared, detected_bank)
                if need_refine or (not doc_current and has_no_label):
                    if doc_refined and _is_valid_docnum(doc_refined):
                        campos["numero_documento"] = doc_refined
                elif doc_refined and _is_valid_docnum(doc_refined):
                    if difflib.SequenceMatcher(None, doc_current, doc_refined).ratio() >= 0.90 and doc_current != doc_refined:
                        campos["numero_documento"] = doc_refined

                doc_now = (campos.get("numero_documento") or "").strip()
                if doc_now and len(doc_now) >= 10:
                    if detected_bank == "JEP":
                        doc_tail = _refine_last_digits_docnum_aggressive(prepared, doc_now)
                    else:
                        doc_tail = _refine_last_digits_generic(prepared, doc_now, tail_len=4)
                    if doc_tail and _is_valid_docnum(doc_tail):
                        campos["numero_documento"] = doc_tail

        # FASE 10: VALIDATION
        with self._phase("validation"):
            missing_info = _missing_or_suspicious_fields(campos)
            needs_retry, retry_reasons = _should_retry(quality, missing_info)

        result = ProcessingResult(
            texto_extraido=texto, campos=campos,
            uso=Usage(**usage_total), costo_estimado=_estimate_cost(usage_total),
            quality=quality, validation=missing_info,
            needs_retry=needs_retry, retry_reasons=retry_reasons,
            detected_bank=detected_bank,
            processing_metrics=[asdict(m) for m in self.metrics]
        )
        if needs_retry:
            result.retry_instructions = _retry_instructions()
        return result

# ============================================================================
# ENDPOINTS
# ============================================================================

@app.route("/ocr", methods=["POST"])
def ocr():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    img_bytes = file.read()

    if not os.getenv("OPENAI_API_KEY"):
        return jsonify({"error": "Missing OPENAI_API_KEY env var"}), 500

    filename = (file.filename or "").lower()
    content_type = (file.mimetype or "").lower()
    is_pdf = filename.endswith(".pdf") or content_type == "application/pdf"

    # ── VALIDACIÓN TEMPRANA DE IMAGEN (antes de gastar tokens) ───────────────
    if not is_pdf:
        pre_validation = validate_image_before_ocr(img_bytes)
        if not pre_validation.accepted:
            logger.info(
                f"Image rejected pre-OCR: {pre_validation.rejection_code} | "
                f"scores={pre_validation.quality_scores}"
            )
            return jsonify(build_image_rejection_response(pre_validation)), 422

    # ── CACHE ─────────────────────────────────────────────────────────────────
    cache_key = None
    if CACHE_ENABLED:
        cache_key = _compute_hash(img_bytes)
        cached = _cache_get(cache_key)
        if cached:
            cached["cache_hit"] = True
            return jsonify(cached), 200

    try:
        pipeline = OCRPipeline()
        result = pipeline.process(img_bytes, is_pdf=is_pdf)

        # ── VALIDACIÓN POST-OCR DE COMPROBANTE ────────────────────────────────
        voucher_validation = validate_voucher_fields(
            campos=result.campos,
            raw_text=result.texto_extraido,
        )

        response_data = {
            "texto_extraido": result.texto_extraido,
            "campos": result.campos,
            "uso": asdict(result.uso),
            "costo_estimado": result.costo_estimado,
            "quality": asdict(result.quality),
            "validation": result.validation,
            "needs_retry": result.needs_retry,
            "retry_reasons": result.retry_reasons,
            "detected_bank": result.detected_bank,
            "processing_metrics": result.processing_metrics,
            "is_valid_voucher": voucher_validation.is_valid_voucher,
            "voucher_confidence": voucher_validation.confidence,
            "voucher_warnings": voucher_validation.warnings,
        }

        if result.retry_instructions:
            response_data["retry_instructions"] = result.retry_instructions

        if not voucher_validation.is_valid_voucher:
            response_data["voucher_failing_checks"] = voucher_validation.failing_checks
            response_data["needs_retry"] = True
            if not response_data.get("retry_instructions"):
                response_data["retry_instructions"] = [
                    "No se pudo identificar un comprobante bancario válido.",
                    "Fotografía el comprobante completo con número, fecha y monto visibles.",
                ]
            logger.warning(
                f"Invalid voucher: confidence={voucher_validation.confidence:.2f}, "
                f"failing={voucher_validation.failing_checks}"
            )
            if CACHE_ENABLED and cache_key:
                _cache_set(cache_key, response_data)
            return jsonify(response_data), 422

        logger.info(
            f"OCR OK: bank={result.detected_bank}, "
            f"voucher_valid=True, confidence={voucher_validation.confidence:.2f}, "
            f"cost=${result.costo_estimado['total_cost']:.4f}, "
            f"tokens={result.uso.total_tokens}, "
            f"blur={result.quality.blur_score:.1f}"
        )

        if CACHE_ENABLED and cache_key:
            _cache_set(cache_key, response_data)

        return jsonify(response_data), 200

    except Exception as exc:
        logger.exception("OCR processing failed")
        return jsonify({"error": f"OCR processing failed: {exc}"}), 500


@app.route("/ocr-tesseract", methods=["POST"])
def ocr_tesseract():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    file = request.files["file"]
    img_bytes = file.read()
    img_array = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if img is None:
        return jsonify({"error": "Invalid image"}), 400
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.bilateralFilter(gray, 11, 17, 17)
    thresh = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, 10)
    texto = pytesseract.image_to_string(thresh, lang="spa")
    return jsonify({"texto_extraido": texto.strip()}), 200


@app.route("/health", methods=["GET"])
def health():
    openai_healthy = True
    try:
        client.models.list()
    except Exception as e:
        openai_healthy = False
        logger.error(f"OpenAI health check failed: {e}")
    return jsonify({
        "status": "healthy" if openai_healthy else "degraded",
        "cache_enabled": CACHE_ENABLED,
        "cache_size": len(_simple_cache),
        "model": OPENAI_OCR_MODEL,
        "openai_api": "healthy" if openai_healthy else "unhealthy",
        "tesseract_available": _check_tesseract(),
        "validation": {
            "blur_reject_screen": BLUR_REJECT_SCREEN_CAPTURE,
            "blur_reject_photo": BLUR_REJECT_PHOTO,
            "voucher_confidence_threshold": VOUCHER_CONFIDENCE_THRESHOLD,
        }
    }), 200 if openai_healthy else 503

def _check_tesseract():
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


@app.route("/debug-ocr", methods=["POST"])
def debug_ocr():
    if not os.getenv("DEBUG_ENABLED", "false").lower() == "true":
        return jsonify({"error": "Debug endpoint disabled"}), 403
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    file = request.files["file"]
    img_bytes = file.read()
    prepared = _prepare_image_for_openai(img_bytes)
    if prepared is None:
        return jsonify({"error": "Invalid image"}), 400

    # Incluir resultado de validación temprana
    pre_validation = validate_image_before_ocr(img_bytes)
    results = []
    for variant_name, variant_bytes in _generate_variants(img_bytes, apply_demoire=True):
        tesseract_score = _quick_score_tesseract(variant_bytes)
        text, usage = _ocr_with_openai(variant_bytes, "image/jpeg")
        openai_score = _score_ocr_text(text)
        results.append({
            "variant": variant_name,
            "tesseract_score": tesseract_score,
            "openai_score": openai_score,
            "text_preview": text[:200] if text else "",
            "tokens_used": usage["total_tokens"]
        })

    return jsonify({
        "pre_validation": {
            "accepted": pre_validation.accepted,
            "rejection_code": pre_validation.rejection_code,
            "quality_scores": pre_validation.quality_scores,
        },
        "variants": results,
        "total_cost_usd": sum(r["tokens_used"] for r in results) / 1_000_000 * OPENAI_INPUT_COST_PER_MILLION
    }), 200


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    port = int(os.getenv("PORT", "5000"))
    logger.info(f"Starting OCR service on port {port}")
    logger.info(f"Model: {OPENAI_OCR_MODEL}, Debug: {debug_mode}, Cache: {CACHE_ENABLED}")
    logger.info(f"Blur thresholds — screen: {BLUR_REJECT_SCREEN_CAPTURE}, photo: {BLUR_REJECT_PHOTO}")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)