"""
ghn_client.py – Kết nối thật GHN API (api.ghn.vn) cho multi-seller POS
"""
import httpx
import json
import time
from typing import Optional
from database import get_conn

GHN_BASE    = "https://online-gateway.ghn.vn/shiip/public-api"
GHN_BASE_V2 = f"{GHN_BASE}/v2"
GHN_BASE_V3 = f"{GHN_BASE}/v3"

# ── Sandbox / Production switch ────────────────────────────────────
SANDBOX_BASE    = "https://dev-online-gateway.ghn.vn/shiip/public-api"
SANDBOX_BASE_V2 = f"{SANDBOX_BASE}/v2"


def _build_headers(token: str, shop_id: int) -> dict:
    return {
        "Content-Type": "application/json",
        "Token": token,
        "ShopId": str(shop_id),
    }


def _build_headers_no_shop(token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Token": token,
    }


async def _call(
    token: str,
    shop_id: int,
    path: str,
    method: str = "POST",
    body: Optional[dict] = None,
    seller_id: Optional[str] = None,
    use_sandbox: bool = False,
) -> dict:
    """
    Core GHN API call với logging.
    use_sandbox=True → dùng dev gateway (an toàn để test).
    """
    base = SANDBOX_BASE if use_sandbox else GHN_BASE
    url = f"{base}{path}"
    headers = _build_headers(token, shop_id)

    start = time.time()
    error_msg = None
    status_code = 0
    response_text = ""

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            if method.upper() == "POST":
                resp = await client.post(url, headers=headers, json=body or {})
            elif method.upper() == "DELETE":
                resp = await client.request("DELETE", url, headers=headers, json=body or {})
            else:
                resp = await client.get(url, headers=headers, params=body or {})

            status_code    = resp.status_code
            response_text  = resp.text

            try:
                data = resp.json()
            except Exception:
                data = {"raw": response_text}

            # Auto-learn lỗi
            if status_code != 200 or (isinstance(data, dict) and data.get("code") not in (200, None)):
                _auto_learn_error(path, body, data)

    except httpx.TimeoutException:
        error_msg = "Request timeout sau 20 giây"
        data = {"error": error_msg, "code": -1}
    except Exception as e:
        error_msg = str(e)
        data = {"error": error_msg, "code": -1}

    duration_ms = int((time.time() - start) * 1000)

    # Ghi log vào DB
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO api_logs (seller_id, endpoint, request_body, status_code, response, error_msg, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            seller_id,
            path,
            json.dumps(body, ensure_ascii=False) if body else None,
            status_code,
            response_text[:4000],
            error_msg,
            duration_ms,
        ))

    return {
        "url": url,
        "status_code": status_code,
        "duration_ms": duration_ms,
        "data": data,
        "error": error_msg,
        "ok": status_code == 200 and data.get("code") in (200, None),
    }


def _auto_learn_error(endpoint: str, request: Optional[dict], response: dict):
    msg = ""
    if isinstance(response, dict):
        msg = (response.get("message") or response.get("msg")
               or response.get("error") or "")
        if not msg and response.get("data"):
            msg = str(response["data"])
    if not msg:
        return

    with get_conn() as conn:
        exists = conn.execute(
            "SELECT id FROM error_knowledge WHERE error_msg LIKE ?",
            (f"%{msg[:50]}%",)
        ).fetchone()
        if not exists:
            conn.execute("""
                INSERT INTO error_knowledge (error_msg, endpoint, source, root_cause)
                VALUES (?, ?, 'ghn_api', 'Tự động phát hiện – chưa phân tích')
            """, (msg, endpoint))


# ══════════════════════════════════════════════════════════
# MASTER DATA (không cần shop_id)
# ══════════════════════════════════════════════════════════

async def fetch_provinces(token: str) -> dict:
    """Lấy tất cả Tỉnh/Thành – không cần ShopId."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{GHN_BASE}/master-data/province",
            headers=_build_headers_no_shop(token)
        )
    return resp.json()


async def fetch_districts(token: str, province_id: int) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{GHN_BASE}/master-data/district",
            headers=_build_headers_no_shop(token),
            params={"province_id": province_id}
        )
    return resp.json()


async def fetch_wards(token: str, district_id: int) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{GHN_BASE}/master-data/ward",
            headers=_build_headers_no_shop(token),
            params={"district_id": district_id}
        )
    return resp.json()


# ══════════════════════════════════════════════════════════
# MASTER DATA V3 (đơn vị hành chính mới – áp dụng từ 01/07/2025)
# GHN xác nhận "không phát sinh API mới" → cùng endpoint, GHN tự
# trả data mới sau ngày hiệu lực. URL v3 cần xác nhận thêm với GHN.
# Hiện tại fallback về cùng endpoint với v1 để đảm bảo hoạt động.
# Khi GHN công bố URL v3 chính xác, chỉ cần đổi GHN_BASE_V3 ở trên.
# ══════════════════════════════════════════════════════════

async def fetch_provinces_v3(token: str) -> dict:
    """Lấy danh sách Tỉnh/Thành mới (v3) — đơn vị hành chính sau sáp nhập 01/07/2025."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{GHN_BASE_V3}/master-data/province/all",
            headers=_build_headers_no_shop(token)
        )
    return resp.json()


async def fetch_districts_v3(token: str, province_id: int) -> dict:
    """Lấy Quận/Huyện theo đơn vị hành chính mới (v3)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                f"{GHN_BASE_V3}/master-data/district",
                headers=_build_headers_no_shop(token),
                params={"province_id": province_id}
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("code") == 200:
                return data
        except Exception:
            pass
        resp = await client.get(
            f"{GHN_BASE}/master-data/district",
            headers=_build_headers_no_shop(token),
            params={"province_id": province_id}
        )
    return resp.json()


def _load_new_wards_data():
    """Load danh sách phường/xã mới 2025 từ file static (nguồn: Chính phủ VN)."""
    import os
    path = os.path.join(os.path.dirname(__file__), "new_wards_2025.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

_NEW_WARDS_DATA = None

def _get_new_wards_by_province_name(province_name: str) -> list:
    """Tìm wards mới theo tên tỉnh (match không dấu, không phân biệt hoa thường)."""
    global _NEW_WARDS_DATA
    if _NEW_WARDS_DATA is None:
        _NEW_WARDS_DATA = _load_new_wards_data()

    import unicodedata
    def normalize(s):
        s = s.lower().strip()
        s = unicodedata.normalize('NFD', s)
        s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
        for prefix in ['thanh pho ', 'tinh ', 'tp ', 'tp. ']:
            if s.startswith(prefix):
                s = s[len(prefix):]
        return s

    target = normalize(province_name)
    for p in _NEW_WARDS_DATA:
        name = p.get('tentinhmoi', '')
        if normalize(name) == target or target in normalize(name) or normalize(name) in target:
            return [
                {"WardCode": str(w["maphuongxa"]), "WardName": w["tenphuongxa"],
                 "WardID": w["maphuongxa"], "ward_id_v2": w["maphuongxa"]}
                for w in p.get("phuongxa", [])
            ]
    return []


async def fetch_wards_v3_by_province(token: str, province_id: int) -> dict:
    """
    Lấy danh sách Phường/Xã mới theo Tỉnh (v3).
    Gọi trực tiếp GHN v3 API: POST /v3/master-data/ward/all-by-province-id
    province_id ở đây là ID mới của v3 (VD: 1000033), không phải ID cũ (202).
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{GHN_BASE_V3}/master-data/ward/all-by-province-id",
            headers=_build_headers_no_shop(token),
            json={"province_id": province_id, "offset": 0, "limit": 500}
        )
    return resp.json()


async def fetch_wards_v3(token: str, district_id: int) -> dict:
    """Lấy Phường/Xã theo đơn vị hành chính mới (v3)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                f"{GHN_BASE_V3}/master-data/ward",
                headers=_build_headers_no_shop(token),
                params={"district_id": district_id}
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("code") == 200:
                return data
        except Exception:
            pass
        resp = await client.get(
            f"{GHN_BASE}/master-data/ward",
            headers=_build_headers_no_shop(token),
            params={"district_id": district_id}
        )
    return resp.json()


# ══════════════════════════════════════════════════════════
# SHIPPING SERVICES & FEE
# ══════════════════════════════════════════════════════════

async def get_available_services(
    token: str,
    shop_id: int,
    from_district: int,
    to_district: int,
    seller_id: Optional[str] = None,
) -> dict:
    """Lấy dịch vụ khả dụng theo tuyến."""
    return await _call(
        token, shop_id,
        path="/v2/shipping-order/available-services",
        method="GET",
        body={"ShopId": shop_id, "FromDistrict": from_district, "ToDistrict": to_district},
        seller_id=seller_id,
    )


async def get_shipping_fee(
    token: str,
    shop_id: int,
    payload: dict,
    seller_id: Optional[str] = None,
) -> dict:
    """Tính phí vận chuyển thực tế từ GHN.
    payload chứa đầy đủ fields: service_type_id, weight, to_district_id/to_ward_id_v2..."""
    return await _call(
        token, shop_id,
        path="/v2/shipping-order/fee",
        method="POST",
        body=payload,
        seller_id=seller_id,
    )


# ══════════════════════════════════════════════════════════
# ORDER MANAGEMENT
# ══════════════════════════════════════════════════════════

async def create_order(
    token: str,
    shop_id: int,
    payload: dict,
    seller_id: Optional[str] = None,
) -> dict:
    """
    Tạo đơn hàng thực trên GHN.
    payload phải đủ các trường: ToName, ToPhone, ToAddress, ToWardCode, ToDistrictId,
    Weight, ServiceTypeId, PaymentTypeId, Items[]
    """
    result = await _call(
        token, shop_id,
        path="/v2/shipping-order/create",
        method="POST",
        body=payload,
        seller_id=seller_id,
    )

    # Nếu thành công → lưu đơn vào DB
    if result["ok"] and result["data"].get("data", {}).get("order_code"):
        ghn_data = result["data"]["data"]
        order_code = ghn_data.get("order_code")
        with get_conn() as conn:
            conn.execute("""
                UPDATE orders
                SET order_code = ?, status = 'pickup', ghn_response = ?, shipping_fee = ?
                WHERE client_code = ? AND seller_id = ?
            """, (
                order_code,
                json.dumps(ghn_data, ensure_ascii=False),
                ghn_data.get("total_fee", 0),
                payload.get("client_order_code", ""),
                seller_id,
            ))

    return result


async def get_order_detail(
    token: str,
    shop_id: int,
    order_code: str,
    seller_id: Optional[str] = None,
) -> dict:
    """Tra cứu chi tiết đơn hàng theo OrderCode."""
    return await _call(
        token, shop_id,
        path="/v2/shipping-order/detail",
        method="GET",
        body={"order_code": order_code},
        seller_id=seller_id,
    )


async def cancel_orders(
    token: str,
    shop_id: int,
    order_codes: list,
    seller_id: Optional[str] = None,
) -> dict:
    """Huỷ một hoặc nhiều đơn hàng trên GHN."""
    result = await _call(
        token, shop_id,
        path="/v2/switch-status/cancel",
        method="POST",
        body={"order_codes": order_codes},
        seller_id=seller_id,
    )

    if result["ok"]:
        # Cập nhật trạng thái trong DB
        with get_conn() as conn:
            for code in order_codes:
                conn.execute(
                    "UPDATE orders SET status='cancelled', updated_at=CURRENT_TIMESTAMP WHERE order_code=?",
                    (code,)
                )

    return result


async def get_print_token(
    token: str,
    shop_id: int,
    order_codes: list,
    seller_id: Optional[str] = None,
) -> dict:
    """Lấy token in tem GHN (A5)."""
    result = await _call(
        token, shop_id,
        path="/v2/a5/gen-token",
        method="POST",
        body={"order_codes": order_codes},
        seller_id=seller_id,
    )

    if result["ok"] and result["data"].get("data", {}).get("token"):
        print_token = result["data"]["data"]["token"]
        print_url = f"https://dev-online-gateway.ghn.vn/a5/public-api/print5x5?token={print_token}"
        result["print_url"] = print_url

    return result


async def get_tracking_logs(
    token: str,
    shop_id: int,
    order_code: str,
    seller_id: Optional[str] = None,
) -> dict:
    """Lấy lịch sử tracking đơn hàng (GHN API id=47)."""
    return await _call(
        token, shop_id,
        path="/v2/shipping-order/tracking",   # FIX: /detail → /tracking
        method="POST",
        body={"order_code": order_code},
        seller_id=seller_id,
    )


# ══════════════════════════════════════════════════════════
# SHOP INFO
# ══════════════════════════════════════════════════════════

async def get_shop_info(token: str) -> dict:
    """Lấy thông tin shop theo token (không cần shop_id)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{GHN_BASE_V2}/shop/all",
            headers=_build_headers_no_shop(token)
        )
    return resp.json()


# ══════════════════════════════════════════════════════════
# GENERIC CALL (dùng cho API Tester)
# ══════════════════════════════════════════════════════════

async def call_ghn_api(
    token: str,
    shop_id: int,
    endpoint_path: str,
    method: str,
    body: Optional[dict] = None,
    seller_id: Optional[str] = None,
) -> dict:
    return await _call(token, shop_id, endpoint_path, method, body, seller_id)


# ══════════════════════════════════════════════════════════
# ENDPOINT METADATA LEARNING
# ══════════════════════════════════════════════════════════

LEARN_ENDPOINTS = [
    {"name": "Tạo đơn hàng",         "url": f"{GHN_BASE_V2}/shipping-order/create",             "method": "POST", "description": "Tạo đơn giao hàng mới",           "required_fields": ["PaymentTypeId","RequiredNote","ToName","ToPhone","ToAddress","ToWardCode","ToDistrictId","Weight","ServiceTypeId","Items"]},
    {"name": "Tính phí ship",         "url": f"{GHN_BASE_V2}/shipping-order/fee",                "method": "GET",  "description": "Tính phí vận chuyển dự kiến",      "required_fields": ["ServiceId","FromDistrictId","ToDistrictId","ToWardCode","Weight"]},
    {"name": "Dịch vụ khả dụng",     "url": f"{GHN_BASE_V2}/shipping-order/available-services", "method": "GET",  "description": "Dịch vụ theo tuyến",               "required_fields": ["ShopId","FromDistrict","ToDistrict"]},
    {"name": "Tra cứu đơn",          "url": f"{GHN_BASE_V2}/shipping-order/detail",             "method": "GET",  "description": "Chi tiết đơn theo OrderCode",      "required_fields": ["OrderCode"]},
    {"name": "Huỷ đơn hàng",         "url": f"{GHN_BASE_V2}/switch-status/cancel",              "method": "POST", "description": "Huỷ một hoặc nhiều đơn hàng",     "required_fields": ["order_codes"]},
    {"name": "In nhãn A5",            "url": f"{GHN_BASE_V2}/a5/gen-token",                      "method": "POST", "description": "Tạo token để in nhãn GHN A5",      "required_fields": ["order_codes"]},
    {"name": "Danh sách Tỉnh/Thành", "url": f"{GHN_BASE}/master-data/province",                 "method": "GET",  "description": "Tất cả tỉnh thành Việt Nam",        "required_fields": []},
    {"name": "Danh sách Quận/Huyện", "url": f"{GHN_BASE}/master-data/district",                 "method": "GET",  "description": "Quận/huyện theo province_id",      "required_fields": ["province_id"]},
    {"name": "Danh sách Phường/Xã",  "url": f"{GHN_BASE}/master-data/ward",                     "method": "GET",  "description": "Phường/xã theo district_id",        "required_fields": ["district_id"]},
    {"name": "Thông tin shop",        "url": f"{GHN_BASE_V2}/shop/all",                          "method": "GET",  "description": "Danh sách shop thuộc token",        "required_fields": []},
    {"name": "Tracking đơn hàng",    "url": f"{GHN_BASE_V2}/shipping-order/tracking",           "method": "POST", "description": "Lịch sử tracking đơn hàng",        "required_fields": ["order_code"]},
]


async def learn_endpoints() -> dict:
    results = []
    with get_conn() as conn:
        for ep in LEARN_ENDPOINTS:
            existing = conn.execute("SELECT id FROM ghn_endpoints WHERE url=?", (ep["url"],)).fetchone()
            rf = json.dumps(ep["required_fields"], ensure_ascii=False)
            if existing:
                conn.execute("UPDATE ghn_endpoints SET name=?, description=?, required_fields=? WHERE url=?",
                             (ep["name"], ep["description"], rf, ep["url"]))
                results.append({"action": "updated", "name": ep["name"]})
            else:
                conn.execute("INSERT INTO ghn_endpoints (name, url, method, description, required_fields) VALUES (?, ?, ?, ?, ?)",
                             (ep["name"], ep["url"], ep["method"], ep["description"], rf))
                results.append({"action": "added", "name": ep["name"]})
    return {"learned": len(results), "details": results}


# ══════════════════════════════════════════════════════════════════
# GHN AFFILIATE – OTP Flow (dùng token của M, không cần ShopId)
# API id=87: GET OTP → /v2/shop/affiliateOTP
# API id=89: Add Staff → /v2/shop/affiliateCreateWithShop
# ══════════════════════════════════════════════════════════════════

async def send_otp_employee(master_token: str, shop_phone: str) -> dict:
    """
    Gửi OTP tới số điện thoại GHN của shop A để M xin làm nhân viên.
    API id=87: POST /v2/shop/affiliateOTP
    Body: {"phone": "<phone>"}  ← chữ thường, không phải "Phone"
    """
    url = f"{GHN_BASE}/v2/shop/affiliateOTP"
    headers = _build_headers_no_shop(master_token)
    body = {"phone": shop_phone}   # GHN yêu cầu lowercase

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=headers, json=body)
            data = resp.json()
        return {"ok": resp.status_code == 200 and data.get("code") == 200, "data": data}
    except Exception as e:
        return {"ok": False, "data": {"message": str(e)}}


async def add_employee_by_otp(master_token: str, shop_phone: str, otp: str, shop_id: int = 0) -> dict:
    """
    Dùng OTP để thêm M làm nhân viên của shop A.
    API id=89: POST /v2/shop/affiliateCreateWithShop
    Body: {"phone": "...", "otp": "...", "shop_id": <int>}  ← tất cả lowercase
    Sau bước này, dùng token M + ShopId A để lên đơn → nhận affiliate.
    """
    url = f"{GHN_BASE}/v2/shop/affiliateCreateWithShop"
    headers = _build_headers_no_shop(master_token)
    body = {"phone": shop_phone, "otp": otp, "shop_id": shop_id}  # GHN yêu cầu lowercase

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=headers, json=body)
            data = resp.json()
        return {"ok": resp.status_code == 200 and data.get("code") == 200, "data": data}
    except Exception as e:
        return {"ok": False, "data": {"message": str(e)}}
