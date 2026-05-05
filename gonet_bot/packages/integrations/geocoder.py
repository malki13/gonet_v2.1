"""Cliente de geocoding usado por el flujo comercial."""

import re
import unicodedata

import httpx

from packages.shared.config import get_settings

FORWARD_STOPWORDS = {
    "av",
    "avenida",
    "calle",
    "sector",
    "barrio",
    "urbanizacion",
    "urbanización",
    "ciudadela",
    "cdla",
    "mz",
    "manzana",
    "solar",
    "km",
    "via",
    "vía",
    "y",
    "de",
    "del",
    "la",
    "el",
    "ecuador",
}

FORWARD_PREFIXES = (
    "av",
    "av.",
    "avenida",
    "calle",
    "sector",
    "barrio",
    "urbanizacion",
    "urbanización",
    "ciudadela",
    "cdla",
    "via",
    "vía",
)


def _normalized_tokens(value: str | None) -> list[str]:
    """Devuelve el tokens normalized."""
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    tokens = re.findall(r"[a-z0-9]+", ascii_value)
    return [token for token in tokens if len(token) > 1 and token not in FORWARD_STOPWORDS]


def _forward_query_variants(query: str | None) -> list[str]:
    """Reenvía query variants al destino correspondiente."""
    cleaned = " ".join(str(query or "").split()).strip()
    if not cleaned:
        return []
    variants = [cleaned]
    stripped = re.sub(
        rf"^(?:{'|'.join(re.escape(prefix) for prefix in FORWARD_PREFIXES)})\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" ,.-")
    if stripped and stripped.lower() != cleaned.lower():
        variants.append(stripped)
    without_connectors = re.sub(r"\s+y\s+", " ", stripped or cleaned, flags=re.IGNORECASE).strip(" ,.-")
    if without_connectors and all(without_connectors.lower() != item.lower() for item in variants):
        variants.append(without_connectors)
    return variants


class GeocoderClient:
    """Cliente de Cliente geocodificador para resolver y revertir ubicaciones.."""
    def __init__(self) -> None:
        """Inicializa el geocoderclient con la configuracion necesaria."""
        self.settings = get_settings()

    def _search_url(self) -> str:
        """Devuelve el URL search."""
        reverse_url = str(self.settings.geocoder_url or "").strip()
        if reverse_url.endswith("/reverse"):
            return reverse_url[: -len("/reverse")] + "/search"
        if reverse_url.endswith("reverse"):
            return reverse_url[: -len("reverse")] + "search"
        return "https://nominatim.openstreetmap.org/search"

    @staticmethod
    def _forward_candidate(item: dict) -> dict:
        """Reenvía candidate al destino correspondiente."""
        address = item.get("address") or {}
        city = address.get("city") or address.get("town") or address.get("village")
        province = address.get("state")
        zone = address.get("suburb") or address.get("neighbourhood")
        country_code = str(address.get("country_code") or "").strip().lower()
        latitude = item.get("lat")
        longitude = item.get("lon")
        try:
            latitude = float(latitude) if latitude is not None else None
            longitude = float(longitude) if longitude is not None else None
        except (TypeError, ValueError):
            latitude = None
            longitude = None
        return {
            "latitude": latitude,
            "longitude": longitude,
            "city": city,
            "province": province,
            "zone": zone,
            "address": item.get("display_name"),
            "country_code": country_code,
            "importance": float(item.get("importance") or 0.0),
            "place_rank": int(item.get("place_rank") or 0),
        }

    @staticmethod
    def _forward_score(query: str, candidate: dict) -> tuple[float, bool]:
        """Reenvía score al destino correspondiente."""
        query_tokens = _normalized_tokens(query)
        candidate_tokens = set(_normalized_tokens(candidate.get("address")))
        if not query_tokens or not candidate_tokens:
            return 0.0, False
        overlap = sum(1 for token in query_tokens if token in candidate_tokens)
        numeric_tokens = {token for token in query_tokens if token.isdigit()}
        numeric_match = not numeric_tokens or bool(numeric_tokens & candidate_tokens)
        score = overlap / len(query_tokens)
        if numeric_tokens and not numeric_match:
            score -= 0.2
        return max(score, 0.0), numeric_match

    def _pick_forward_candidate(self, query: str, rows: list[dict]) -> dict:
        """Devuelve el candidate pick forward."""
        candidates = [self._forward_candidate(item) for item in rows if isinstance(item, dict)]
        candidates = [
            item
            for item in candidates
            if item.get("latitude") is not None and item.get("longitude") is not None and item.get("country_code") == "ec"
        ]
        scored: list[tuple[float, bool, dict]] = []
        for candidate in candidates:
            score, numeric_match = self._forward_score(query, candidate)
            scored.append((score, numeric_match, candidate))
        scored.sort(key=lambda item: (item[0], item[2].get("importance") or 0.0, item[2].get("place_rank") or 0), reverse=True)
        if not scored:
            return {}
        best_score, numeric_match, best = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        if not numeric_match:
            return {}
        if best_score < 0.72:
            return {}
        if second_score >= 0.68 and (best_score - second_score) < 0.08:
            return {}
        return {
            "latitude": best["latitude"],
            "longitude": best["longitude"],
            "city": best.get("city"),
            "province": best.get("province"),
            "zone": best.get("zone"),
            "address": best.get("address"),
        }

    async def forward(self, query: str | None, *, city: str | None = None, province: str | None = None) -> dict:
        """Reenvia la consulta al backend correspondiente."""
        cleaned = " ".join(str(query or "").split()).strip()
        if not cleaned:
            return {}
        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                headers={"User-Agent": self.settings.geocoder_user_agent},
            ) as client:
                for candidate_query in _forward_query_variants(cleaned):
                    lookup_parts: list[str] = [candidate_query]
                    lowered_query = candidate_query.lower()
                    for extra in (city, province):
                        value = " ".join(str(extra or "").split()).strip()
                        if value and value.lower() not in lowered_query:
                            lookup_parts.append(value)
                    lookup_parts.append("Ecuador")
                    response = await client.get(
                        self._search_url(),
                        params={
                            "format": "jsonv2",
                            "q": ", ".join(lookup_parts),
                            "addressdetails": 1,
                            "countrycodes": "ec",
                            "limit": 5,
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, list):
                        continue
                    picked = self._pick_forward_candidate(candidate_query, payload)
                    if picked:
                        return picked
            return {}
        except Exception:
            if self.settings.mock_mode:
                return {
                    "latitude": -2.170998,
                    "longitude": -79.922359,
                    "city": city or "Guayaquil",
                    "province": province or "Guayas",
                    "zone": "Centro",
                    "address": cleaned,
                }
            raise

    async def reverse(self, latitude: float | None, longitude: float | None) -> dict:
        """Devuelve el reverse."""
        if latitude is None or longitude is None:
            return {"latitude": latitude, "longitude": longitude}
        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                headers={"User-Agent": self.settings.geocoder_user_agent},
            ) as client:
                response = await client.get(
                    self.settings.geocoder_url,
                    params={
                        "format": "jsonv2",
                        "lat": latitude,
                        "lon": longitude,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            address = payload.get("address") or {}
            city = address.get("city") or address.get("town") or address.get("village")
            province = address.get("state")
            zone = address.get("suburb") or address.get("neighbourhood")
            return {
                "latitude": latitude,
                "longitude": longitude,
                "city": city,
                "province": province,
                "zone": zone,
                "address": payload.get("display_name"),
            }
        except Exception:
            if self.settings.mock_mode:
                return {
                    "latitude": latitude,
                    "longitude": longitude,
                    "city": "Guayaquil",
                    "province": "Guayas",
                    "zone": "Centro",
                    "address": f"Ubicación demo ({latitude}, {longitude})",
                }
            raise
