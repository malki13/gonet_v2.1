from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass
class CostEstimate:
    input_cost: float
    output_cost: float
    total_cost: float
    input_rate_per_million: float
    output_rate_per_million: float
    moneda: str = "USD"


@dataclass
class QualityMetrics:
    blur_score: float
    blur_threshold: float
    is_blurry: bool
    brightness_mean: float
    edge_density: float
    resolution: tuple[int, int]
    is_screen_capture: bool


@dataclass
class OCRCandidate:
    variant: str
    raw_text: str
    fields: Dict[str, Any]
    score: int
    usage: Usage


@dataclass(frozen=True)
class UploadedDocument:
    filename: str
    mimetype: str
    content: bytes
    is_pdf: bool


@dataclass
class OCRApiResponse:
    estado: str
    texto_extraido: str
    debe_reintentar: bool
    motivos_reintento: List[str]
    instrucciones_reintento: List[str]
    uso: Usage
    costo_estimado: CostEstimate

    def to_dict(self) -> Dict[str, Any]:
        return {
            "estado": self.estado,
            "texto_extraido": self.texto_extraido,
            "debe_reintentar": self.debe_reintentar,
            "motivos_reintento": self.motivos_reintento,
            "instrucciones_reintento": self.instrucciones_reintento,
            "uso": asdict(self.uso),
            "costo_estimado": asdict(self.costo_estimado),
        }


class APIError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 400,
        code: str = "bad_request",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.details = details or {}
