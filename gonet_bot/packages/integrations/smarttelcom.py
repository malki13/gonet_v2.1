"""Cliente de soporte tecnico para SmartTelcom."""

import asyncio
import logging
import re
from typing import Any

import httpx

from packages.integrations.runtime import get_shared_http_client
from packages.shared.config import get_settings

logger = logging.getLogger("smarttelcom")


def _log_http_response(tag: str, response: httpx.Response) -> None:
    """Devuelve la respuesta log http."""
    snippet = (response.text or "").strip()[:800]
    logger.info("%s status=%s url=%s body=%s", tag, response.status_code, str(response.url), snippet)


class SmartTelcomClient:
    """Cliente para monitoreo y administración en SmartTelcom."""
    def __init__(self) -> None:
        """Inicializa el smarttelcomclient con la configuracion necesaria."""
        self.settings = get_settings()
        self._token_cache: str | None = None
        self._token_lock = asyncio.Lock()

    def enabled(self) -> bool:
        """Indica si la integracion esta habilitada por configuracion."""
        if self.settings.mock_mode:
            return True
        return bool(self.settings.smart_telcom_base_url and (self.settings.smart_telcom_auth_url or self.settings.smart_telcom_token))

    def _base_url(self) -> str:
        """Devuelve el URL base."""
        return (self.settings.smart_telcom_base_url or "").rstrip("/")

    async def _login(self, client: httpx.AsyncClient) -> str:
        """Devuelve el login."""
        if self.settings.smart_telcom_token:
            return self.settings.smart_telcom_token
        payload = {"email": self.settings.smart_telcom_email, "password": self.settings.smart_telcom_password}
        headers = {}
        if self.settings.smart_telcom_login_token:
            headers["Authorization"] = f"Bearer {self.settings.smart_telcom_login_token}"
        response = await client.post(
            self.settings.smart_telcom_auth_url,
            json=payload,
            timeout=self.settings.smart_telcom_timeout_seconds,
        )
        _log_http_response("smart_login", response)
        response.raise_for_status()
        body = response.json()
        for key in ("token", "access_token", "accessToken", "jwt", "id_token"):
            value = body.get(key) or (body.get("data") or {}).get(key)
            if value:
                return str(value)
        raise ValueError("No se pudo extraer token del login SmartTelcom")

    async def _auth_headers(self, client: httpx.AsyncClient, *, force_refresh: bool = False) -> dict[str, str]:
        """Devuelve el headers auth."""
        if force_refresh or not self._token_cache:
            async with self._token_lock:
                if force_refresh or not self._token_cache:
                    self._token_cache = await self._login(client)
        return {"Authorization": f"Bearer {self._token_cache}"} if self._token_cache else {}

    async def _request_with_auth(self, method: str, url: str, *, params=None, json=None, tag: str):
        """Devuelve el auth request with."""
        client = await get_shared_http_client()
        headers = await self._auth_headers(client)
        timeout = self.settings.smart_telcom_timeout_seconds
        response = await client.request(method, url, headers=headers, params=params, json=json, timeout=timeout)
        _log_http_response(tag, response)
        if response.status_code == 401:
            headers = await self._auth_headers(client, force_refresh=True)
            response = await client.request(method, url, headers=headers, params=params, json=json, timeout=timeout)
            _log_http_response(f"{tag}_retry", response)
        response.raise_for_status()
        return response

    async def get_by_contrato(self, contrato: str) -> dict:
        """Devuelve by contrato."""
        response = await self._request_with_auth("GET", f"{self._base_url()}/getByContrato/{contrato}", tag="smart_get_by_contrato")
        return response.json()

    async def monitor_contract(self, contrato: str) -> dict:
        """Devuelve el contract monitor."""
        if self.settings.mock_mode and not self.settings.smart_telcom_base_url:
            return {
                "ok": True,
                "data": {
                    "accion": "monitoreo",
                    "info": {
                        "contrato": contrato,
                        "numeroRedes": 2,
                        "onu_status": "up",
                        "alarmas": [],
                    },
                },
                "source": "mock",
            }
        info = await self.get_by_contrato(contrato)
        return {"ok": True, "data": {"accion": "monitoreo", "info": info}}

    async def get_all_networks_device(self, dispositivo_id: str) -> dict:
        """Devuelve all networks device."""
        response = await self._request_with_auth(
            "GET",
            f"{self._base_url()}/getAllNetworksDevice",
            params={"dispositivoId": dispositivo_id},
            tag="smart_get_all_networks_device",
        )
        return response.json()

    async def reboot_device(self, dispositivo_id: str) -> dict:
        """Devuelve el dispositivo reboot."""
        if self.settings.mock_mode and not self.settings.smart_telcom_base_url:
            return {"ok": True, "data": {"dispositivoId": dispositivo_id, "status": "accepted"}, "source": "mock"}
        response = await self._request_with_auth(
            "GET",
            f"{self._base_url()}/rebootDevice",
            params={"dispositivoId": dispositivo_id},
            tag="smart_reboot_device",
        )
        return response.json()

    async def get_connected_devices(self, dispositivo_id: str) -> dict:
        """Devuelve connected devices."""
        if self.settings.mock_mode and not self.settings.smart_telcom_base_url:
            return {
                "ok": True,
                "data": [
                    {"mac": "AA:BB:CC:DD:EE:01"},
                    {"mac": "AA:BB:CC:DD:EE:02"},
                ],
                "source": "mock",
            }
        response = await self._request_with_auth(
            "GET",
            f"{self._base_url()}/deviceConnect/{dispositivo_id}",
            tag="smart_get_connected_devices",
        )
        return response.json()

    async def change_red(self, dispositivo_id: str, red_id: str, nombre_red: str, contrasena_red: str, estado_red: bool) -> dict:
        """Devuelve el red change."""
        response = await self._request_with_auth(
            "PUT",
            f"{self._base_url()}/changeRed",
            params={"dispositivoId": dispositivo_id, "redId": red_id},
            json={
                "nombreRed": nombre_red,
                "contrasenaRed": contrasena_red,
                "estadoRed": "true" if estado_red else "false",
            },
            tag="smart_change_red",
        )
        return response.json()

    @staticmethod
    def get_dispositivo_id(byc: dict[str, Any]) -> str:
        """Devuelve ID de dispositivo."""
        return str(byc.get("dispositivoId") or byc.get("data", {}).get("dispositivoId") or byc.get("data", {}).get("iden") or "")

    @staticmethod
    def get_numero_redes(byc: dict[str, Any]) -> int | None:
        """Devuelve numero redes."""
        raw = byc.get("numeroRedes") or byc.get("data", {}).get("numeroRedes")
        try:
            return int(raw)
        except Exception:
            return None

    @staticmethod
    def _pick_text(*values: Any) -> str:
        """Devuelve el texto pick."""
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _parse_int(value: Any) -> int | None:
        """Devuelve el int parse."""
        if value in (None, ""):
            return None
        try:
            return int(float(str(value).strip()))
        except Exception:
            return None

    @classmethod
    def _device_model_label(cls, model: dict[str, Any] | None) -> str:
        """Devuelve la etiqueta dispositivo model."""
        model = model if isinstance(model, dict) else {}
        brand = model.get("marca") if isinstance(model.get("marca"), dict) else {}
        brand_name = cls._pick_text(brand.get("nombre"))
        model_name = cls._pick_text(model.get("nombre"))
        if brand_name and model_name:
            return f"{brand_name} {model_name}"
        return brand_name or model_name

    @classmethod
    def extract_monitor_snapshot(cls, monitor_out: dict[str, Any] | None) -> dict[str, Any]:
        """Extrae monitor snapshot."""
        payload = monitor_out or {}
        container = payload.get("data") if isinstance(payload, dict) else None
        info = container.get("info") if isinstance(container, dict) else None
        info_dict = info if isinstance(info, dict) else {}
        device = info_dict.get("data") if isinstance(info_dict.get("data"), dict) else info_dict
        plan_device = device.get("planDevice") if isinstance(device.get("planDevice"), dict) else {}
        tarifa = device.get("tarifa") if isinstance(device.get("tarifa"), dict) else {}
        model = device.get("modelo") if isinstance(device.get("modelo"), dict) else {}

        snapshot = {
            "device_id": cls.get_dispositivo_id(info_dict or device),
            "network_count": cls.get_numero_redes(info_dict or device),
            "plan_name": cls._pick_text(plan_device.get("nombre"), tarifa.get("nombre")),
            "plan_speed_mbps": cls._parse_int(
                plan_device.get("cantidad")
                or tarifa.get("cantidad")
                or tarifa.get("velocidad")
            ),
            "device_model": cls._device_model_label(model),
            "server_id": cls._pick_text(device.get("serverId")),
            "last_connection_at": cls._pick_text(device.get("conexion")),
        }
        return {
            key: value
            for key, value in snapshot.items()
            if value not in (None, "")
        }

    @staticmethod
    def _connected_network_name(item: dict[str, Any]) -> str:
        """Devuelve el nombre connected network."""
        band = item.get("bandR") if isinstance(item.get("bandR"), dict) else {}
        return str(
            band.get("NombreRed")
            or band.get("nombreRed")
            or band.get("nombre")
            or ""
        ).strip()

    @classmethod
    def summarize_connected_devices_payload(cls, connected_out: dict[str, Any] | None) -> dict[str, Any]:
        """Devuelve el payload summarize connected dispositivos."""
        payload = connected_out or {}
        container = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(container, dict) and isinstance(container.get("devices"), list):
            devices = container.get("devices") or []
        elif isinstance(container, list):
            devices = container
        else:
            devices = []

        summary: dict[str, Any] = {
            "count": len(devices),
            "active_count": 0,
            "lan_devices": 0,
            "mesh_devices": 0,
            "wifi_devices": 0,
            "wifi_24g_devices": 0,
            "wifi_5g_devices": 0,
            "unknown_network_devices": 0,
            "network_counts": {},
        }

        for item in devices:
            if not isinstance(item, dict):
                continue
            if cls._is_true(item.get("Activo") or item.get("active") or item.get("activo")):
                summary["active_count"] += 1

            network_name = cls._connected_network_name(item)
            if not network_name:
                summary["unknown_network_devices"] += 1
                continue

            network_counts = summary["network_counts"]
            network_counts[network_name] = int(network_counts.get(network_name) or 0) + 1
            normalized = network_name.lower()
            if normalized.startswith("lan"):
                summary["lan_devices"] += 1
                continue
            if "mesh" in normalized:
                summary["mesh_devices"] += 1
                continue

            summary["wifi_devices"] += 1
            if any(token in normalized for token in ("5g", "5 g")):
                summary["wifi_5g_devices"] += 1
            elif any(token in normalized for token in ("2.4", "24g", "2g", "2 g")):
                summary["wifi_24g_devices"] += 1

        return summary

    @staticmethod
    def _network_red_id(item: dict[str, Any]) -> str:
        """Devuelve el id network red."""
        return str(item.get("Red") or item.get("red") or item.get("id") or "").strip()

    @staticmethod
    def _is_true(value: Any) -> bool:
        """Indica si true se cumple."""
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() == "true"

    def _configured_temp_exclude_ids(self) -> set[str]:
        """Devuelve el ids configured temp exclude."""
        return {item.strip() for item in str(self.settings.temp_net_exclude_ids or "").split(",") if item.strip()}

    def _primary_red_ids(self, numero_redes: int, networks: list[dict[str, Any]]) -> set[str]:
        """Devuelve el ids primary red."""
        red_ids = self.pick_primary_red_ids(networks, numero_redes)
        return {item for item in (red_ids or []) if item}

    def _temporary_networks(
        self,
        *,
        networks: list[dict[str, Any]],
        numero_redes: int,
        only_active: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Devuelve el networks temporary."""
        excluded = self._primary_red_ids(numero_redes, networks) | self._configured_temp_exclude_ids()
        filtered: list[dict[str, Any]] = []
        for item in networks:
            red_id = self._network_red_id(item)
            if not red_id or red_id in excluded:
                continue
            active = self._is_true(item.get("EstadoRed"))
            if only_active is not None and active != only_active:
                continue
            filtered.append(
                {
                    **item,
                    "Red": red_id,
                    "EstadoRed": active,
                }
            )
        return filtered

    def _find_network_by_red_id(self, networks: list[dict[str, Any]], red_id: str) -> dict[str, Any] | None:
        """Devuelve el id find network by red."""
        for item in networks:
            if self._network_red_id(item) == str(red_id):
                return item
        return None

    @staticmethod
    def _message_for_temp_networks(temporary_networks: list[dict[str, Any]]) -> str:
        """Devuelve el networks mensaje for temp."""
        if not temporary_networks:
            return "No encontré redes temporales disponibles para activar o desactivar en este momento."
        lines = ["Estas son las redes temporales disponibles del dispositivo:"]
        for item in temporary_networks:
            status = "activa" if item.get("EstadoRed") else "desactivada"
            lines.append(f"- Red ID {item.get('Red')}: {item.get('NombreRed') or f'SSID{item.get('Red')}'} - {status}")
        lines.append(
            "Si desea activar una red temporal, envíeme: activar red 2, nombre: MiRedInvitados, contraseña: MiClave123."
        )
        lines.append("Si desea desactivarla, envíeme: desactivar red 2.")
        return "\n".join(lines)

    @staticmethod
    def _normalize_primary_network_base_name(value: str) -> str:
        """Normaliza nombre primary network base."""
        cleaned = " ".join(str(value or "").replace("_", " ").split()).strip()
        if not cleaned:
            return ""
        cleaned = re.sub(r"(?i)(?:\s|[-_])*(?:2\.4\s*g|24g|5g)\s*$", "", cleaned).strip(" _-")
        return cleaned or "WiFi"

    @classmethod
    def _primary_network_names(cls, base_name: str) -> tuple[str, str]:
        """Devuelve los names primary network."""
        normalized = cls._normalize_primary_network_base_name(base_name)
        return (f"{normalized} 2.4G", f"{normalized} 5G")

    @staticmethod
    def pick_primary_red_ids(networks: list[dict], numero_redes: int) -> list[str] | None:
        """Devuelve el ids pick primary red."""
        if numero_redes == 2 and len(networks) > 1:
            return [str(networks[0].get("Red")), str(networks[1].get("Red"))]
        if numero_redes == 4 and len(networks) > 2:
            return [str(networks[0].get("Red")), str(networks[2].get("Red"))]
        if numero_redes == 8 and len(networks) > 4:
            return [str(networks[0].get("Red")), str(networks[4].get("Red"))]
        return None

    @staticmethod
    def _format_redes_message(networks: list[dict], numero_redes: int) -> str:
        """Da formato a mensaje de redes para presentarlo de forma clara."""
        def _find_by_red(red_id: int):
            """Devuelve el red find by."""
            for item in networks:
                try:
                    if int(item.get("Red")) == red_id:
                        return item
                except Exception:
                    continue
            return None

        def _line_for_red(red_id: int):
            """Devuelve el red line for."""
            item = _find_by_red(red_id)
            if not item or not SmartTelcomClient._is_true(item.get("EstadoRed")):
                return None
            return f"Red ID {item.get('Red')}: {item.get('NombreRed')} - {item.get('EstadoRed')}"

        def _format_section(title: str, red_ids: list[int]) -> str:
            """Da formato a section para presentarlo de forma clara."""
            lines = [line for line in (_line_for_red(red_id) for red_id in red_ids) if line]
            if not lines:
                return f"{title}\nNo hay redes activas disponibles."
            return f"{title}\n" + "\n".join(lines)

        if numero_redes == 8:
            return f"{_format_section('Las redes 2.4 son:', [1])}\n{_format_section('Las redes 5G son:', [5])}."
        if numero_redes == 4:
            return f"{_format_section('Las redes 2.4 son:', [1])}\n{_format_section('Las redes 5G son:', [3])}."
        if numero_redes == 2:
            return f"{_format_section('Las redes 2.4 son:', [1])}\n{_format_section('Las redes 5G son:', [2])}."
        return "No existen redes disponibles."

    @classmethod
    def describe_primary_networks(cls, data: dict[str, Any] | None) -> str | None:
        """Devuelve el networks describe primary."""
        payload = data or {}
        networks = payload.get("networks")
        numero_redes = payload.get("numero_redes")
        if not isinstance(networks, list) or not networks:
            return None
        try:
            parsed_numero_redes = int(numero_redes)
        except Exception:
            return None

        primary_ids = cls.pick_primary_red_ids(networks, parsed_numero_redes)
        if not primary_ids or len(primary_ids) < 2:
            return None

        def _find_name(red_id: str) -> str | None:
            """Devuelve el nombre find."""
            for item in networks:
                if cls._network_red_id(item) != str(red_id):
                    continue
                name = str(item.get("NombreRed") or item.get("nombre") or "").strip()
                if name:
                    return name
            return None

        name_24g = _find_name(primary_ids[0]) or "No identificado"
        name_5g = _find_name(primary_ids[1]) or "No identificado"
        return (
            "Así se llaman actualmente sus redes principales:\n"
            f"- Red 2.4G: {name_24g}\n"
            f"- Red 5G: {name_5g}"
        )

    async def list_networks_for_contract(self, contrato: str) -> dict:
        """Lista Cliente para monitoreo y administración en SmartTelcom."""
        if self.settings.mock_mode and not self.settings.smart_telcom_base_url:
            return {
                "ok": True,
                "data": {
                    "mensaje": "Las redes 2.4 son:\nRed ID 1: Casa - true\nLas redes 5G son:\nRed ID 2: Casa_5G - true.",
                    "dispositivo_id": "demo-device",
                    "numero_redes": 2,
                    "networks": [
                        {"Red": 1, "NombreRed": "Casa", "EstadoRed": True},
                        {"Red": 2, "NombreRed": "Casa_5G", "EstadoRed": True},
                    ],
                },
                "source": "mock",
            }
        byc = await self.get_by_contrato(contrato)
        dispositivo_id = self.get_dispositivo_id(byc)
        if not dispositivo_id:
            return {"ok": False, "error": "No se encontró dispositivoId para el contrato"}
        numero_redes = self.get_numero_redes(byc) or 0
        redes = await self.get_all_networks_device(dispositivo_id)
        networks = redes.get("data") if isinstance(redes, dict) else None
        if isinstance(networks, list) and networks:
            return {
                "ok": True,
                "data": {
                    "mensaje": self._format_redes_message(networks, numero_redes),
                    "dispositivo_id": dispositivo_id,
                    "numero_redes": numero_redes,
                    "networks": networks,
                },
            }
        return {"ok": False, "error": "No se encontraron redes del dispositivo"}

    async def list_temporary_networks_for_contract(self, contrato: str, *, only_active: bool | None = None) -> dict:
        """Lista Cliente para monitoreo y administración en SmartTelcom."""
        if self.settings.mock_mode and not self.settings.smart_telcom_base_url:
            networks = [
                {"Red": "1", "NombreRed": "Casa", "EstadoRed": True},
                {"Red": "2", "NombreRed": "Invitados_24", "EstadoRed": False},
                {"Red": "3", "NombreRed": "Invitados_5G", "EstadoRed": True},
            ]
            temporary = self._temporary_networks(networks=networks, numero_redes=4, only_active=only_active)
            return {
                "ok": True,
                "data": {
                    "mensaje": self._message_for_temp_networks(temporary),
                    "dispositivo_id": "demo-device",
                    "numero_redes": 4,
                    "networks": temporary,
                },
                "source": "mock",
            }
        byc = await self.get_by_contrato(contrato)
        dispositivo_id = self.get_dispositivo_id(byc)
        if not dispositivo_id:
            return {"ok": False, "error": "No se encontró dispositivoId para el contrato"}
        numero_redes = self.get_numero_redes(byc) or 0
        redes = await self.get_all_networks_device(dispositivo_id)
        networks = redes.get("data") if isinstance(redes, dict) else None
        if not isinstance(networks, list) or not networks:
            return {"ok": False, "error": "No se encontraron redes del dispositivo"}
        temporary = self._temporary_networks(networks=networks, numero_redes=numero_redes, only_active=only_active)
        return {
            "ok": True,
            "data": {
                "mensaje": self._message_for_temp_networks(temporary),
                "dispositivo_id": dispositivo_id,
                "numero_redes": numero_redes,
                "networks": temporary,
            },
        }

    async def change_networks_for_contract(self, contrato: str, nuevo_nombre: str, password: str) -> dict:
        """Devuelve el contract change networks for."""
        ssid_24g, ssid_5g = self._primary_network_names(nuevo_nombre)
        if self.settings.mock_mode and not self.settings.smart_telcom_base_url:
            return {
                "ok": True,
                "data": {
                    "accion": "cambio_red",
                    "resultados": [
                        {"red": 1, "nombre": ssid_24g, "estado": "updated"},
                        {"red": 2, "nombre": ssid_5g, "estado": "updated"},
                    ],
                    "mensaje": (
                        f"Listo, ya quedó actualizado. Tus redes wifi ahora se llaman {ssid_24g} y {ssid_5g}. "
                        "Usa la nueva clave para volver a conectarte."
                    ),
                },
                "source": "mock",
            }
        byc = await self.get_by_contrato(contrato)
        dispositivo_id = self.get_dispositivo_id(byc)
        if not dispositivo_id:
            return {"ok": False, "error": "No se encontró dispositivoId para el contrato"}
        numero_redes = self.get_numero_redes(byc)
        if not numero_redes:
            return {"ok": False, "error": "No se encontró numeroRedes para el contrato"}
        redes = await self.get_all_networks_device(dispositivo_id)
        networks = redes.get("data") if isinstance(redes, dict) else None
        if not isinstance(networks, list) or not networks:
            return {"ok": False, "error": "No se encontraron redes del dispositivo"}
        red_ids = self.pick_primary_red_ids(networks, numero_redes)
        if not red_ids:
            return {"ok": False, "error": "No se pudo determinar las redes primarias a modificar"}
        results = []
        for red_id in red_ids:
            results.append(
                await self.change_red(
                    dispositivo_id,
                    red_id,
                    ssid_24g if red_id == red_ids[0] else ssid_5g,
                    password,
                    True,
                )
            )
        return {
            "ok": True,
            "data": {
                "accion": "cambio_red",
                "resultados": results,
                "mensaje": (
                    f"Listo, ya quedó actualizado. Tus redes wifi ahora se llaman {ssid_24g} y {ssid_5g}. "
                    "Usa la nueva clave para volver a conectarte."
                ),
            },
        }

    async def toggle_temporary_network_for_contract(
        self,
        contrato: str,
        *,
        red_id: str,
        enabled: bool,
        nombre_red: str | None = None,
        password: str | None = None,
    ) -> dict:
        """Devuelve el contract toggle temporary network for."""
        if self.settings.mock_mode and not self.settings.smart_telcom_base_url:
            accion = "activada" if enabled else "desactivada"
            return {
                "ok": True,
                "data": {
                    "accion": "activar_red_temporal" if enabled else "desactivar_red_temporal",
                    "red_id": str(red_id),
                    "mensaje": f"La red {red_id} ha sido {accion}.",
                },
                "source": "mock",
            }
        byc = await self.get_by_contrato(contrato)
        dispositivo_id = self.get_dispositivo_id(byc)
        if not dispositivo_id:
            return {"ok": False, "error": "No se encontró dispositivoId para el contrato"}
        numero_redes = self.get_numero_redes(byc) or 0
        redes = await self.get_all_networks_device(dispositivo_id)
        networks = redes.get("data") if isinstance(redes, dict) else None
        if not isinstance(networks, list) or not networks:
            return {"ok": False, "error": "No se encontraron redes del dispositivo"}
        temporary = self._temporary_networks(networks=networks, numero_redes=numero_redes, only_active=None)
        network = self._find_network_by_red_id(temporary, str(red_id))
        if not network:
            return {"ok": False, "error": f"La red temporal {red_id} no está disponible para este contrato"}
        current_name = str(network.get("NombreRed") or f"SSID{red_id}").strip()
        if enabled and (not nombre_red or not password):
            return {"ok": False, "error": "Debes indicar nombre y contraseña para activar una red temporal"}
        result = await self.change_red(
            dispositivo_id,
            str(red_id),
            (nombre_red or current_name) if enabled else f"SSID{red_id}",
            password or "contraApagada1@",
            enabled,
        )
        accion = "activada" if enabled else "desactivada"
        return {
            "ok": True,
            "data": {
                "accion": "activar_red_temporal" if enabled else "desactivar_red_temporal",
                "red_id": str(red_id),
                "mensaje": f"La red {red_id} ha sido {accion}.",
                "resultados": [result],
            },
        }

    async def reboot_router_for_contract(self, contrato: str) -> dict:
        """Devuelve el contract reboot router for."""
        if self.settings.mock_mode and not self.settings.smart_telcom_base_url:
            return {"ok": True, "data": {"contrato": contrato, "status": "accepted"}, "source": "mock"}
        byc = await self.get_by_contrato(contrato)
        dispositivo_id = self.get_dispositivo_id(byc)
        if not dispositivo_id:
            return {"ok": False, "error": "No se encontró dispositivoId para el contrato"}
        result = await self.reboot_device(dispositivo_id)
        return {"ok": True, "data": {"contrato": contrato, "dispositivo_id": dispositivo_id, "result": result}}

    async def connected_devices_for_contract(self, contrato: str) -> dict:
        """Devuelve el contract connected dispositivos for."""
        if self.settings.mock_mode and not self.settings.smart_telcom_base_url:
            devices = [
                {"mac": "AA:BB:CC:DD:EE:01"},
                {"mac": "AA:BB:CC:DD:EE:02"},
            ]
            return {"ok": True, "data": {"contrato": contrato, "count": len(devices), "devices": devices}, "source": "mock"}
        byc = await self.get_by_contrato(contrato)
        dispositivo_id = self.get_dispositivo_id(byc)
        if not dispositivo_id:
            return {"ok": False, "error": "No se encontró dispositivoId para el contrato"}
        devices_out = await self.get_connected_devices(dispositivo_id)
        devices = devices_out.get("data") if isinstance(devices_out, dict) else None
        if not isinstance(devices, list):
            devices = []
        return {
            "ok": True,
            "data": {
                "contrato": contrato,
                "dispositivo_id": dispositivo_id,
                "count": len(devices),
                "devices": devices,
            },
        }
