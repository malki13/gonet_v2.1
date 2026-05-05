"""Cliente para obtener promociones y catalogo comercial."""

import httpx

from packages.shared.config import get_settings


class PromotionsAPI:
    """Cliente para consultar promociones y planes.."""
    def __init__(self) -> None:
        """Inicializa el promotionsapi con la configuracion necesaria."""
        self.settings = get_settings()

    @staticmethod
    def _fmt_price(value) -> str:
        """Devuelve el precio fmt."""
        if value is None or value == "":
            return "-"
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return str(value)

    async def fetch_catalog(self) -> dict:
        """Devuelve el catalog fetch."""
        if not self.settings.promotions_url:
            if self.settings.mock_mode:
                return {
                    "data": {
                        "GONECTADOS": [
                            {"name": "Go Light", "mbps": "80", "price": "19.99", "details": [{"name": "Internet fibra"}]},
                            {"name": "Go Plus", "mbps": "120", "price": "24.99", "details": [{"name": "Internet fibra"}]},
                            {"name": "Go Max", "mbps": "200", "price": "29.99", "details": [{"name": "Internet fibra"}, {"name": "Instalación sujeta a cobertura"}]},
                            {"name": "Go Ultra", "mbps": "300", "price": "34.99", "details": [{"name": "Internet fibra"}, {"name": "Atención prioritaria"}]},
                        ],
                        "PYMES": [
                            {"name": "Pyme 150", "mbps": "150", "price": "39.99", "details": [{"name": "Soporte empresarial"}]},
                            {"name": "Pyme 300", "mbps": "300", "price": "54.99", "details": [{"name": "IP pública opcional"}]},
                        ],
                    },
                    "source": "mock",
                }
            return {}
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(self.settings.promotions_url)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _detail_names(details: list[dict], limit: int) -> list[str]:
        """Devuelve los names detalle."""
        names = [(item or {}).get("name", "") for item in (details or [])]
        names = [name for name in names if name]
        return names[:limit]

    @classmethod
    def _format_plan_block(cls, item: dict, *, details_limit: int) -> str:
        """Da formato a plan block para presentarlo de forma clara."""
        name = str(item.get("name", "-") or "-").strip()
        mbps = str(item.get("mbps", "-") or "-").strip()
        price = cls._fmt_price(item.get("price", item.get("final_price", "-")))
        detail_names = cls._detail_names(item.get("details") or [], details_limit)
        lines = [
            f"**{name}**",
            f"- **Velocidad:** **{mbps} Mbps**",
            f"- **Precio + IMP:** **${price}**",
        ]
        if detail_names:
            lines.append(f"- **Incluye:** {', '.join(detail_names)}")
        return "\n".join(lines)

    def format_gonectados_combos(self, data: dict) -> str:
        """Da formato a gonectados combos para presentarlo de forma clara."""
        items = (data.get("data") or {}).get("GONECTADOS") or []
        out = ["**Planes hogar disponibles:**"]
        order = [2, 3, 1, 0]
        details_limit = {2: 4, 3: 4, 1: 4, 0: 6}
        for idx in order:
            if idx >= len(items):
                continue
            item = items[idx]
            out.append(self._format_plan_block(item, details_limit=details_limit.get(idx, 6)))
        return "\n\n".join(out).strip()

    def format_pymes(self, data: dict) -> str:
        """Da formato a pymes para presentarlo de forma clara."""
        items = (data.get("data") or {}).get("PYMES") or []
        out = ["**Planes pyme disponibles:**"]
        for item in items[:4]:
            out.append(self._format_plan_block(item, details_limit=5))
        return "\n\n".join(out).strip()
