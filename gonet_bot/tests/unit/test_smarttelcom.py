from packages.integrations.smarttelcom import SmartTelcomClient


def test_smarttelcom_extract_monitor_snapshot_reads_plan_speed_and_model():
    client = SmartTelcomClient()

    snapshot = client.extract_monitor_snapshot(
        {
            "ok": True,
            "data": {
                "accion": "monitoreo",
                "info": {
                    "status": "200 OK",
                    "message": "Dispositivo encontrado",
                    "data": {
                        "iden": 9199,
                        "serverId": "00E0FC-Huawei-QDU7S23C07000898",
                        "conexion": "2026-04-09T22:08:48.274Z",
                        "numeroRedes": "2",
                        "modelo": {
                            "nombre": "AX3",
                            "marca": {"nombre": "HUAWEI"},
                        },
                        "planDevice": {
                            "nombre": "PLAN GOPLUS",
                            "cantidad": 350,
                        },
                    },
                },
            },
        }
    )

    assert snapshot == {
        "device_id": "9199",
        "network_count": 2,
        "plan_name": "PLAN GOPLUS",
        "plan_speed_mbps": 350,
        "device_model": "HUAWEI AX3",
        "server_id": "00E0FC-Huawei-QDU7S23C07000898",
        "last_connection_at": "2026-04-09T22:08:48.274Z",
    }


def test_smarttelcom_summarize_connected_devices_payload_breaks_down_networks():
    client = SmartTelcomClient()

    summary = client.summarize_connected_devices_payload(
        {
            "ok": True,
            "data": {
                "count": 6,
                "devices": [
                    {"Activo": True, "bandR": {"NombreRed": "LAN-1"}},
                    {"Activo": True, "bandR": {"NombreRed": "LAN-1"}},
                    {"Activo": True, "bandR": {"NombreRed": "Mesh"}},
                    {"Activo": True, "bandR": {"NombreRed": "Casa 5G"}},
                    {"Activo": False, "bandR": {"NombreRed": "Casa 2.4G"}},
                    {"Activo": True, "bandR": {}},
                ],
            },
        }
    )

    assert summary == {
        "count": 6,
        "active_count": 5,
        "lan_devices": 2,
        "mesh_devices": 1,
        "wifi_devices": 2,
        "wifi_24g_devices": 1,
        "wifi_5g_devices": 1,
        "unknown_network_devices": 1,
        "network_counts": {
            "LAN-1": 2,
            "Mesh": 1,
            "Casa 5G": 1,
            "Casa 2.4G": 1,
        },
    }
