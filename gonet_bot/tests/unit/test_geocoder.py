import asyncio

from packages.integrations import geocoder as geocoder_module
from packages.integrations.geocoder import GeocoderClient


def test_forward_geocoder_retries_with_simplified_street_query(monkeypatch):
    queries: list[str] = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            query = (params or {}).get("q", "")
            queries.append(query)
            if query.startswith("Av Garcia Moreno y Republica"):
                return FakeResponse([])
            return FakeResponse(
                [
                    {
                        "lat": "-2.8999472",
                        "lon": "-78.9940210",
                        "display_name": "Gabriel García Moreno, La Republica, San Blas, Cuenca, Azuay, Ecuador",
                        "importance": 0.053393288319218704,
                        "place_rank": 26,
                        "address": {
                            "road": "Gabriel García Moreno",
                            "neighbourhood": "La Republica",
                            "city": "Cuenca",
                            "state": "Azuay",
                            "country_code": "ec",
                        },
                    }
                ]
            )

    monkeypatch.setattr(geocoder_module.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(GeocoderClient().forward("Av Garcia Moreno y Republica", city="Cuenca"))

    assert result["latitude"] == -2.8999472
    assert result["longitude"] == -78.994021
    assert result["city"] == "Cuenca"
    assert result["province"] == "Azuay"
    assert queries == [
        "Av Garcia Moreno y Republica, Cuenca, Ecuador",
        "Garcia Moreno y Republica, Cuenca, Ecuador",
    ]
