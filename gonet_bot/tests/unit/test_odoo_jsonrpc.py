import logging

from packages.integrations.odoo_jsonrpc import OdooJsonRpcClient


def test_odoo_jsonrpc_error_preview_uses_traceback_tail():
    client = OdooJsonRpcClient(
        logger=logging.getLogger("test_odoo_jsonrpc"),
        request_log_tag="request",
        response_log_tag="response",
    )

    preview = client._error_preview(
        {
            "message": "Odoo Server Error",
            "data": {
                "debug": (
                    "Traceback (most recent call last):\n"
                    '  File "/usr/lib/python3/dist-packages/odoo/http.py", line 357, in checked_call\n'
                    "    result = self.endpoint(*a, **kw)\n"
                    '  File "/mnt/extra-addons/app/models/deposit.py", line 88, in action_reconnect\n'
                    "    assert contract_id\n"
                    "AssertionError\n"
                )
            },
        }
    )

    assert "Odoo Server Error" in preview
    assert "action_reconnect" in preview
    assert "AssertionError" in preview
    assert "checked_call" not in preview
