import json as _json
from NorenRestApiPy.NorenApi import NorenApi  # type: ignore


class FlatTradeApiPy(NorenApi):
    def __init__(self):
        super().__init__(
            host='https://piconnect.flattrade.in/PiConnectAPI/', 
            websocket='wss://piconnect.flattrade.in/PiConnectWSAPI/'
        )

    # Patch WebSocket open: send auth with accesstoken (FlatTrade V2 requires this in payload)
    def _NorenApi__on_open_callback(self, ws=None):
        self._NorenApi__websocket_connected = True
        values = {"t": "a"}
        values["uid"] = getattr(self, '_NorenApi__username', '')        
        values["actid"] = getattr(self, '_NorenApi__username', '')
        
        # Read from access_token attribute set on this instance
        ws_token = getattr(self, '_NorenApi__access_token', '')
        values["accesstoken"] = ws_token
        values["source"] = 'API'   
        payload = _json.dumps(values)
        print(f"[WS] Sending FlatTrade WS Auth (token length={len(ws_token)})", flush=True)
        self._NorenApi__ws_send(payload)

    # Patch data callback: handle FlatTrade V2 'ak' auth ack (original SDK only handles 'ck')
    def _NorenApi__on_data_callback(self, ws=None, message=None, data_type=None, continue_flag=None):
        try:
            res = _json.loads(message)
        except Exception:
            return

        subscribe_cb = getattr(self, '_NorenApi__subscribe_callback', None)
        if subscribe_cb is not None:
            if res.get('t') in ('tk', 'tf', 'dk', 'df'):
                subscribe_cb(res)
                return

        order_cb = getattr(self, '_NorenApi__order_update_callback', None)
        if order_cb is not None:
            if res.get('t') == 'om':
                order_cb(res)
                return

        open_cb = getattr(self, '_NorenApi__on_open', None)
        if open_cb is not None:
            # 'ak' = FlatTrade V2 auth ack, 'ck' = old Shoonya format
            if res.get('t') in ('ck', 'ak') and res.get('s') == 'OK':
                print("[WS] FlatTrade WS Auth acknowledged OK — connection live!", flush=True)
                open_cb()
                return

        error_cb = getattr(self, '_NorenApi__on_error', None)
        if error_cb is not None:
            if res.get('t') in ('ck', 'ak') and res.get('s') != 'OK':
                print(f"[WS] FlatTrade WS Auth REJECTED: {res}", flush=True)
                error_cb(res)
                return
