import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

"""
main.py – FastAPI Multi-Seller POS + GHN Real API Backend
Chạy: python -m uvicorn main:app --reload --port 8000
"""
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from typing import Optional, List
import json, os, secrets

from database import init_db, seed_initial_knowledge, seed_demo_sellers, get_conn
from ghn_client import (
    learn_endpoints, call_ghn_api,
    fetch_provinces, fetch_districts, fetch_wards,
    fetch_provinces_v3, fetch_districts_v3, fetch_wards_v3, fetch_wards_v3_by_province,
    get_available_services, get_shipping_fee,
    create_order, get_order_detail, cancel_orders,
    get_print_token, get_tracking_logs, get_shop_info,
    send_otp_employee, add_employee_by_otp,
)
from analyzer import analyze_error, chat_response
import traceback

# ── GHN Master Token của M (dùng để nhận affiliate) ────────────────────
GHN_MASTER_TOKEN = os.environ.get("GHN_MASTER_TOKEN", "")


# ── App ────────────────────────────────────────────────────────────
app = FastAPI(title="POS của tao – GHN Multi-Seller API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_PATH = os.path.join(os.path.dirname(__file__), "..", "AgencyOrders.html")

# ── Lazy init cho Vercel Serverless (mỗi cold start đều chạy) ────
_db_initialized = False

@app.middleware("http")
async def ensure_db_initialized(request, call_next):
    global _db_initialized
    if not _db_initialized:
        try:
            init_db()
            seed_initial_knowledge()
            seed_demo_sellers()
            await learn_endpoints()
            _db_initialized = True
            print("✅ DB initialized")
        except Exception as e:
            print(f"⚠️ DB init warning: {e}")
    return await call_next(request)

@app.on_event("startup")
async def startup():
    print("✅ POS Backend started")


# ── Serve Frontend ─────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def serve_frontend():
    if os.path.exists(FRONTEND_PATH):
        return FileResponse(FRONTEND_PATH)
    return {"message": "Open AgencyOrders.html directly."}


# ══════════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ══════════════════════════════════════════════════════════════════

class SellerCreate(BaseModel):
    id: str
    name: str
    owner_name: str
    phone: str
    email: Optional[str] = ""
    ghn_token: Optional[str] = ""
    ghn_shop_id: Optional[int] = 0
    login_key: Optional[str] = None   # auto-generate if None

class SellerUpdate(BaseModel):
    name: Optional[str] = None
    owner_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    ghn_token: Optional[str] = None
    ghn_shop_id: Optional[int] = None
    status: Optional[str] = None

class OrderCreateRequest(BaseModel):
    seller_id: str
    # Sender (Optional overrides)
    from_name: Optional[str] = None
    from_phone: Optional[str] = None
    from_address: Optional[str] = None
    # Receiver
    to_name: str
    to_phone: str
    to_address: str
    to_ward_code: Optional[str] = None      # bắt buộc cho địa chỉ CŨ; địa chỉ mới không cần
    to_district_id: Optional[int] = None    # bắt buộc cho địa chỉ CŨ; địa chỉ mới không cần
    to_province_id: Optional[int] = None
    # Địa chỉ hành chính mới (GHN v3 – áp dụng từ 01/07/2025)
    is_new_to_address: bool = False
    to_ward_id_v2: Optional[int] = None     # integer ward ID mới (VD: 70119087) — dùng cho API tính phí
    to_address_v2: Optional[str] = None     # địa chỉ chi tiết khi dùng đơn vị mới (fee)
    to_ward_name: Optional[str] = None      # tên phường/xã mới — API tạo đơn dùng cái này
    to_province_name: Optional[str] = None  # tên tỉnh/thành mới — API tạo đơn dùng cái này
    # Package
    weight: int = 200
    length: int = 10
    width: int = 10
    height: int = 10
    # Shipping
    service_id: int
    service_type_id: int = 2
    payment_type_id: int = 2        # 1=shop trả, 2=khách trả
    cod_amount: int = 0
    insurance_value: int = 0
    required_note: str = "KHONGCHOXEMHANG"  # CHOTHUHANG / CHOXEMHANGKHONGTHU
    note: Optional[str] = ""
    # Items
    items: Optional[List[dict]] = None
    client_order_code: Optional[str] = None

class CancelOrderRequest(BaseModel):
    seller_id: str
    order_codes: List[str]

class PrintLabelRequest(BaseModel):
    seller_id: str
    order_codes: List[str]

class FeeRequest(BaseModel):
    seller_id: str
    service_id: Optional[int] = 0          # 0 = để GHN tự chọn service
    service_type_id: Optional[int] = 2     # 2=hàng nhẹ (xe máy), 5=hàng nặng (xe tải)
    # Địa chỉ cũ
    from_district_id: Optional[int] = 0
    to_district_id: Optional[int] = 0
    to_ward_code: Optional[str] = ""
    from_ward_code: Optional[str] = ""
    # Địa chỉ mới (v3)
    is_new_to_address: bool = False
    to_ward_id_v2: Optional[int] = None
    to_address_v2: Optional[str] = None
    is_new_from_address: bool = False
    from_ward_id_v2: Optional[int] = None
    from_address_v2: Optional[str] = None
    # Package
    weight: int = 200
    length: int = 10
    width: int = 10
    height: int = 10
    insurance_value: int = 0
    cod_failed_amount: int = 0

class ServicesRequest(BaseModel):
    seller_id: str
    from_district: int
    to_district: int

class TrackingRequest(BaseModel):
    seller_id: str
    order_code: str

class AnalyzeRequest(BaseModel):
    error_text: str

class ChatRequest(BaseModel):
    message: str

class GHNCallRequest(BaseModel):
    token: str
    shop_id: int
    endpoint: str
    method: str = "POST"
    body: Optional[dict] = None

class KnowledgeAddRequest(BaseModel):
    error_msg: str
    endpoint: Optional[str] = None
    root_cause: str
    solution: str
    code_wrong: Optional[str] = None
    code_right: Optional[str] = None
    source: str = "manual"

class KnowledgeUpdateRequest(BaseModel):
    root_cause: Optional[str] = None
    solution: Optional[str] = None
    code_wrong: Optional[str] = None
    code_right: Optional[str] = None

class WebhookPayload(BaseModel):
    OrderCode: str
    ClientOrderCode: Optional[str] = None
    Status: Optional[str] = None
    Description: Optional[str] = None
    Reason: Optional[str] = None
    ReasonCode: Optional[str] = None
    TotalFee: Optional[int] = None
    CODAmount: Optional[int] = None
    Time: Optional[str] = None

class SendOTPRequest(BaseModel):
    seller_id: str
    ghn_phone: str       # SĐT đăng nhập GHN của seller A
    ghn_shop_id: int     # ShopID GHN của seller A

class VerifyOTPRequest(BaseModel):
    seller_id: str
    ghn_phone: str
    otp: str
    ghn_shop_id: Optional[int] = None  # fallback nếu DB chưa có

class GhnConnectionStatusRequest(BaseModel):
    seller_id: str


# ══════════════════════════════════════════════════════════════════
# HELPER: load seller's GHN credentials from DB
# ══════════════════════════════════════════════════════════════════

def _get_seller_creds(seller_id: str):
    """Lấy token + shop_id của chính seller (dùng cho validate, tracking, v.v.)"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT ghn_token, ghn_shop_id, status FROM sellers WHERE id=?",
            (seller_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, f"Nhà bán hàng '{seller_id}' không tồn tại")
    if row["status"] != "active":
        raise HTTPException(403, f"Nhà bán hàng '{seller_id}' đang bị khoá")
    token = row["ghn_token"]
    shop_id = row["ghn_shop_id"]
    if not token:
        raise HTTPException(422, f"Nhà bán hàng '{seller_id}' chưa cấu hình GHN Token. Cập nhật trong mục Quản lý Nhà bán hàng.")
    if not shop_id:
        raise HTTPException(422, f"Nhà bán hàng '{seller_id}' chưa cấu hình GHN Shop ID.")
    return token, shop_id


def _get_order_creds(seller_id: str):
    """
    Lấy token của M + ShopId của seller A để tạo đơn nhận affiliate.
    - Token: luôn dùng GHN_MASTER_TOKEN (tài khoản M)
    - ShopId: lấy từ ghn_shop_id của seller trong DB
    Yêu cầu seller đã kết nối affiliate (ghn_connected=1).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT ghn_shop_id, ghn_connected, status FROM sellers WHERE id=?",
            (seller_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, f"Nhà bán hàng '{seller_id}' không tồn tại")
    if row["status"] != "active":
        raise HTTPException(403, f"Nhà bán hàng '{seller_id}' đang bị khoá")
    shop_id = row["ghn_shop_id"]
    if not shop_id:
        raise HTTPException(422, f"Nhà bán hàng '{seller_id}' chưa nhập GHN Shop ID. Vào mục Kết nối GHN để cài đặt.")
    if not row["ghn_connected"]:
        raise HTTPException(422, f"Nhà bán hàng '{seller_id}' chưa kết nối GHN Affiliate. Vào mục Kết nối GHN để hoàn tất.")
    return GHN_MASTER_TOKEN, shop_id


# ══════════════════════════════════════════════════════════════════
# SELLER MANAGEMENT (Super Admin)
# ══════════════════════════════════════════════════════════════════

@app.get("/api/sellers")
async def list_sellers():
    """Lấy danh sách tất cả sellers."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, owner_name, phone, email, login_key, ghn_shop_id, status, created_at FROM sellers ORDER BY created_at DESC"
        ).fetchall()
    return {"sellers": [dict(r) for r in rows]}


@app.get("/api/sellers/init-son")
async def init_seller_son():
    """Tạo seller son nếu chưa có – gọi 1 lần để setup."""
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM sellers WHERE phone=?", ("0356755871",)).fetchone()
        if existing:
            return {"message": "Seller son đã tồn tại", "id": dict(existing)["id"]}
        conn.execute("""
            INSERT INTO sellers (id, name, owner_name, phone, email, login_key, ghn_token, ghn_shop_id, ghn_phone, ghn_connected, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active')
        """, ("SEL_SON", "Shop của Son", "Son", "0356755871", "", "son123", 
              GHN_MASTER_TOKEN, 5494011, "0986355512"))
    return {"success": True, "id": "SEL_SON", "login_key": "son123",
            "message": "Tạo xong! Login bằng SĐT 0356755871 / mật khẩu 123456"}


@app.get("/api/sellers/update-son")
async def update_seller_son():
    """Update ghn_token + shop_id + ghn_phone cho seller son."""
    with get_conn() as conn:
        conn.execute("""
            UPDATE sellers SET ghn_token=?, ghn_shop_id=?, ghn_phone=?, ghn_connected=1
            WHERE phone=? OR id=?
        """, (GHN_MASTER_TOKEN, 5494011, "0986355512", "0356755871", "SEL_SON"))
    return {"success": True, "message": "Đã update seller son với GHN token + shop_id 5494011"}


@app.post("/api/sellers", status_code=201)
async def create_seller(req: SellerCreate):
    """Tạo nhà bán hàng mới."""
    login_key = req.login_key or secrets.token_urlsafe(8)
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO sellers (id, name, owner_name, phone, email, login_key, ghn_token, ghn_shop_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """, (req.id, req.name, req.owner_name, req.phone, req.email or "",
                  login_key, req.ghn_token or "", req.ghn_shop_id or 0))
        return {"success": True, "login_key": login_key, "id": req.id}
    except Exception as e:
        raise HTTPException(400, f"Lỗi tạo seller: {str(e)}")


@app.put("/api/sellers/{seller_id}")
async def update_seller(seller_id: str, req: SellerUpdate):
    """Cập nhật thông tin / GHN credentials của seller."""
    fields = {k: v for k, v in req.dict().items() if v is not None}
    if not fields:
        raise HTTPException(400, "Không có trường nào để cập nhật")
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM sellers WHERE id=?", (seller_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Seller không tồn tại")
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE sellers SET {sets} WHERE id=?", (*fields.values(), seller_id))
    return {"success": True}


@app.delete("/api/sellers/{seller_id}")
async def delete_seller(seller_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE sellers SET status='inactive' WHERE id=?", (seller_id,))
    return {"success": True}


@app.get("/api/sellers/{seller_id}/validate")
async def validate_seller_ghn(seller_id: str):
    """Kiểm tra GHN token của seller có hợp lệ không."""
    token, shop_id = _get_seller_creds(seller_id)
    result = await get_shop_info(token)
    return {
        "valid": result.get("code") == 200,
        "shop_id": shop_id,
        "ghn_response": result,
    }


# ══════════════════════════════════════════════════════════════════
# GHN AFFILIATE – Kết nối seller vào hệ thống affiliate của M
# ══════════════════════════════════════════════════════════════════

@app.get("/api/ghn/connection-status")
async def api_ghn_connection_status(seller_id: str):
    """Trả về trạng thái kết nối GHN Affiliate của seller."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT ghn_shop_id, ghn_phone, ghn_connected FROM sellers WHERE id=?",
            (seller_id,)
        ).fetchone()
    if not row:
        # Seller chưa có trong DB (Vercel DB trống) → trả về default thay vì 404
        return {"ghn_shop_id": 0, "ghn_phone": "", "ghn_connected": False}
    return {
        "ghn_shop_id": row["ghn_shop_id"] or 0,
        "ghn_phone": row["ghn_phone"] or "",
        "ghn_connected": bool(row["ghn_connected"]),
    }


@app.post("/api/ghn/send-otp")
async def api_send_otp(req: SendOTPRequest):
    """
    Bước 1: Dùng token của M gửi OTP về số điện thoại GHN của seller A.
    API GHN id=87
    """
    # Lưu shop_id + phone trước để trạng thái không bị mất
    # Nếu seller chưa có trong DB (Vercel DB mới) → tự tạo
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM sellers WHERE id=?", (req.seller_id,)).fetchone()
        if not existing:
            import secrets as _s
            conn.execute("""
                INSERT INTO sellers (id, name, owner_name, phone, login_key, ghn_shop_id, ghn_phone, ghn_connected, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'active')
            """, (req.seller_id, req.seller_id, req.seller_id, req.ghn_phone,
                  _s.token_urlsafe(8), req.ghn_shop_id, req.ghn_phone))
        else:
            conn.execute(
                "UPDATE sellers SET ghn_shop_id=?, ghn_phone=?, ghn_connected=0 WHERE id=?",
                (req.ghn_shop_id, req.ghn_phone, req.seller_id)
            )
    result = await send_otp_employee(GHN_MASTER_TOKEN, req.ghn_phone)
    if result["ok"]:
        return {"success": True, "message": "OTP đã gửi về số " + req.ghn_phone}
    msg = result["data"].get("message") or str(result["data"])
    raise HTTPException(400, f"GHN: {msg}")


@app.post("/api/ghn/verify-otp")
async def api_verify_otp(req: VerifyOTPRequest):
    """
    Bước 2: Xác nhận OTP → M trở thành nhân viên của shop A.
    Sau đó mọi đơn hàng sẽ dùng token M + ShopId A để hưởng affiliate.
    API GHN id=89: /v2/shop/affiliateCreateWithShop
    Body cần: phone, otp, shop_id (lấy từ DB)
    """
    # Lấy shop_id từ DB (đã lưu ở bước send-otp)
    with get_conn() as conn:
        row = conn.execute("SELECT ghn_shop_id FROM sellers WHERE id=?", (req.seller_id,)).fetchone()

    if row and row["ghn_shop_id"]:
        shop_id = row["ghn_shop_id"]
    elif hasattr(req, "ghn_shop_id") and req.ghn_shop_id:
        shop_id = req.ghn_shop_id
    else:
        raise HTTPException(422, "Không tìm thấy GHN Shop ID. Vui lòng quay lại bước 1.")
    result = await add_employee_by_otp(GHN_MASTER_TOKEN, req.ghn_phone, req.otp, shop_id=shop_id)
    if result["ok"]:
        with get_conn() as conn:
            conn.execute(
                "UPDATE sellers SET ghn_connected=1 WHERE id=?",
                (req.seller_id,)
            )
        return {"success": True, "message": "Kết nối GHN Affiliate thành công!"}
    msg = result["data"].get("message") or str(result["data"])
    raise HTTPException(400, f"OTP không hợp lệ: {msg}")

# ══════════════════════════════════════════════════════════════════

def _get_location_token(seller_id: str):
    try:
        token, _ = _get_seller_creds(seller_id)
        if token: return token
    except Exception:
        pass
    with get_conn() as conn:
        row = conn.execute("SELECT ghn_token FROM sellers WHERE ghn_token IS NOT NULL AND ghn_token != '' LIMIT 1").fetchone()
        if row: return row["ghn_token"]
    # Fallback về GHN_MASTER_TOKEN để load địa chỉ
    return GHN_MASTER_TOKEN

@app.get("/api/ghn/provinces")
async def api_provinces(seller_id: str):
    token = _get_location_token(seller_id)
    return await fetch_provinces(token)


@app.get("/api/ghn/districts")
async def api_districts(seller_id: str, province_id: int):
    token = _get_location_token(seller_id)
    return await fetch_districts(token, province_id)


@app.get("/api/ghn/wards")
async def api_wards(seller_id: str, district_id: int):
    token = _get_location_token(seller_id)
    return await fetch_wards(token, district_id)


# ── Địa chỉ hành chính mới v3 (áp dụng từ 01/07/2025) ──────────────
@app.get("/api/ghn/provinces/v3")
async def api_provinces_v3(seller_id: str):
    token = _get_location_token(seller_id)
    return await fetch_provinces_v3(token)


@app.get("/api/ghn/districts/v3")
async def api_districts_v3(seller_id: str, province_id: int):
    token = _get_location_token(seller_id)
    return await fetch_districts_v3(token, province_id)


@app.get("/api/ghn/wards/v3")
async def api_wards_v3(seller_id: str, district_id: int):
    token = _get_location_token(seller_id)
    return await fetch_wards_v3(token, district_id)


@app.get("/api/ghn/wards/v3/by-province")
async def api_wards_v3_by_province(seller_id: str, province_id: int):
    """Lấy Phường/Xã mới theo Tỉnh (không qua Quận) — dành cho địa chỉ hành chính mới."""
    token = _get_location_token(seller_id)
    return await fetch_wards_v3_by_province(token, province_id)


# ══════════════════════════════════════════════════════════════════
# SHIPPING FEE & SERVICES
# ══════════════════════════════════════════════════════════════════

@app.post("/api/ghn/available-services")
async def api_available_services(req: ServicesRequest):
    """Lấy dịch vụ GHN khả dụng theo tuyến."""
    token, shop_id = _get_seller_creds(req.seller_id)
    result = await get_available_services(
        token, shop_id,
        from_district=req.from_district,
        to_district=req.to_district,
        seller_id=req.seller_id,
    )
    return result


@app.post("/api/ghn/fee")
async def api_shipping_fee(req: FeeRequest):
    """Tính phí vận chuyển thực từ GHN (hàng nhẹ service_type_id=2, hàng nặng=5)."""
    token, shop_id = _get_seller_creds(req.seller_id)

    payload: dict = {
        "weight": req.weight,
        "length": req.length,
        "width": req.width,
        "height": req.height,
        "insurance_value": req.insurance_value,
        "cod_failed_amount": req.cod_failed_amount,
    }
    # Service: dùng service_id hoặc service_type_id
    if req.service_id:
        payload["service_id"] = req.service_id
    if req.service_type_id:
        payload["service_type_id"] = req.service_type_id

    # Địa chỉ nhận
    if req.is_new_to_address:
        payload["is_new_to_address"] = True
        if req.to_ward_id_v2: payload["to_ward_id_v2"] = req.to_ward_id_v2
        if req.to_address_v2: payload["to_address_v2"] = req.to_address_v2
    else:
        payload["to_district_id"] = req.to_district_id
        payload["to_ward_code"]   = req.to_ward_code

    # Địa chỉ gửi
    if req.is_new_from_address:
        payload["is_new_from_address"] = True
        if req.from_ward_id_v2: payload["from_ward_id_v2"] = req.from_ward_id_v2
        if req.from_address_v2: payload["from_address_v2"] = req.from_address_v2
    elif req.from_district_id:
        payload["from_district_id"] = req.from_district_id
        if req.from_ward_code: payload["from_ward_code"] = req.from_ward_code

    result = await get_shipping_fee(
        token, shop_id,
        payload=payload,
        seller_id=req.seller_id,
    )
    return result


# ══════════════════════════════════════════════════════════════════
# ORDER MANAGEMENT
# ══════════════════════════════════════════════════════════════════

@app.post("/api/orders")
async def api_create_order(req: OrderCreateRequest):
    """
    Tạo đơn hàng thực trên GHN và lưu vào DB.
    Dùng token của M + ShopId của seller A để nhận affiliate commission.
    """
    token, shop_id = _get_order_creds(req.seller_id)

    import time as _t
    client_code = req.client_order_code or f"POS{int(_t.time())}"

    # Lưu đơn vào DB trước (trạng thái pending)
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO orders (
                seller_id, client_code, status,
                receiver_name, receiver_phone, receiver_address,
                to_district_id, to_ward_code, to_province_id,
                weight, length, width, height,
                cod_amount, insurance_value, service_id, payment_type, note
            ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            req.seller_id, client_code,
            req.to_name, req.to_phone, req.to_address,
            req.to_district_id, req.to_ward_code, req.to_province_id,
            req.weight, req.length, req.width, req.height,
            req.cod_amount, req.insurance_value, req.service_id,
            req.payment_type_id, req.note,
        ))

    # Build GHN payload
    ghn_payload = {
        "payment_type_id": req.payment_type_id,
        "note": req.note or "",
        "required_note": req.required_note,
        "client_order_code": client_code,
        "to_name": req.to_name,
        "to_phone": req.to_phone,
        "to_address": req.to_address,
        "weight": req.weight,
        "length": req.length,
        "width": req.width,
        "height": req.height,
        "service_type_id": req.service_type_id,
        "cod_amount": req.cod_amount,
        "insurance_value": req.insurance_value,
        "items": req.items or [{"name": "Hàng hoá", "quantity": 1, "weight": req.weight}],
    }

    if req.service_id:
        ghn_payload["service_id"] = req.service_id
    if req.from_name: ghn_payload["from_name"] = req.from_name
    if req.from_phone: ghn_payload["from_phone"] = req.from_phone
    if req.from_address: ghn_payload["from_address"] = req.from_address

    # Địa chỉ hành chính mới v3 (01/07/2025).
    # LƯU Ý: API tạo đơn dùng to_ward_name + to_province_name (TÊN),
    # KHÁC API tính phí dùng to_ward_id_v2 (ID). Không được gửi to_ward_id_v2
    # vào endpoint create → gây "To address conflict".
    if req.is_new_to_address:
        ghn_payload["is_new_to_address"] = True
        if req.to_ward_name:
            ghn_payload["to_ward_name"] = req.to_ward_name
        if req.to_province_name:
            ghn_payload["to_province_name"] = req.to_province_name
    else:
        # Địa chỉ cũ — cần ward_code + district_id
        ghn_payload["to_ward_code"] = req.to_ward_code
        ghn_payload["to_district_id"] = req.to_district_id

    result = await create_order(token, shop_id, ghn_payload, seller_id=req.seller_id)

    if result["ok"]:
        ghn_data = result["data"].get("data", {})
        return {
            "success": True,
            "order_code": ghn_data.get("order_code"),
            "expected_delivery": ghn_data.get("expected_delivery_time"),
            "total_fee": ghn_data.get("total_fee"),
            "client_code": client_code,
            "ghn_data": ghn_data,
        }
    else:
        # Trích message GHN — có thể là string, list, hoặc dict (validation errors)
        raw = result["data"] if isinstance(result.get("data"), dict) else {}
        ghn_msg = raw.get("message") or raw.get("code_message_value") or result.get("error")

        def _flatten(m):
            if m is None:
                return ""
            if isinstance(m, str):
                return m
            if isinstance(m, list):
                return "; ".join(_flatten(x) for x in m if x)
            if isinstance(m, dict):
                # ưu tiên field message/msg, nếu không có thì dump toàn bộ
                inner = m.get("message") or m.get("msg") or m.get("error")
                if inner:
                    return _flatten(inner)
                return json.dumps(m, ensure_ascii=False)
            return str(m)

        msg_text = _flatten(ghn_msg) or "Lỗi không xác định"
        # Kèm luôn field errors nếu GHN trả trong data
        errors = raw.get("data")
        if errors and not isinstance(errors, (str, int)):
            msg_text = f"{msg_text} | chi tiết: {json.dumps(errors, ensure_ascii=False)}"

        with get_conn() as conn:
            conn.execute(
                "UPDATE orders SET note=? WHERE client_code=? AND seller_id=?",
                (f"GHN_ERROR: {msg_text}"[:1000], client_code, req.seller_id)
            )
        raise HTTPException(400, f"GHN từ chối đơn hàng: {msg_text}")


@app.get("/api/orders")
async def api_list_orders(seller_id: Optional[str] = None, status: Optional[str] = None, limit: int = 100):
    """Lấy danh sách đơn hàng từ DB (lọc theo seller)."""
    with get_conn() as conn:
        query = "SELECT * FROM orders WHERE 1=1"
        params = []
        if seller_id:
            query += " AND seller_id=?"
            params.append(seller_id)
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    return {"orders": [dict(r) for r in rows]}


@app.get("/api/orders/{order_code}")
async def api_get_order(order_code: str, seller_id: str):
    """Tra cứu chi tiết đơn hàng từ GHN (real-time)."""
    token, shop_id = _get_seller_creds(seller_id)
    result = await get_order_detail(token, shop_id, order_code, seller_id=seller_id)

    if result["ok"]:
        ghn_data = result["data"].get("data", {})
        # Cập nhật trạng thái trong DB
        ghn_status = ghn_data.get("status", "")
        _status_map = {
            "ready_to_pick": "pickup", "picking": "pickup",
            "picked": "pickup", "storing": "pickup",
            "delivering": "in_transit", "delivery_fail": "in_transit",
            "waiting_to_return": "returning", "return": "returning",
            "return_transporting": "returning", "returned": "returning",
            "delivered": "delivered", "cancel": "cancelled",
        }
        db_status = _status_map.get(ghn_status, "")
        if db_status:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE orders SET status=?, updated_at=CURRENT_TIMESTAMP WHERE order_code=?",
                    (db_status, order_code)
                )
        return {"success": True, "data": ghn_data}
    else:
        raise HTTPException(400, result["data"].get("message") or "Không thể tra cứu đơn")


@app.delete("/api/orders/pending/{client_code}")
async def api_delete_pending_order(client_code: str, seller_id: str):
    """Xoá đơn pending (chưa có GHN code) khỏi DB."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, order_code FROM orders WHERE client_code=? AND seller_id=?",
            (client_code, seller_id)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Đơn hàng không tồn tại")
        if row["order_code"]:
            raise HTTPException(400, "Chỉ xoá được đơn chưa có mã GHN (pending)")
        conn.execute("DELETE FROM orders WHERE client_code=? AND seller_id=?", (client_code, seller_id))
    return {"success": True, "deleted": client_code}


@app.post("/api/orders/cancel")
async def api_cancel_orders(req: CancelOrderRequest):
    """Huỷ đơn hàng trên GHN."""
    token, shop_id = _get_seller_creds(req.seller_id)
    result = await cancel_orders(token, shop_id, req.order_codes, seller_id=req.seller_id)
    if result["ok"]:
        with get_conn() as conn:
            for code in req.order_codes:
                conn.execute(
                    "UPDATE orders SET status='cancelled', updated_at=CURRENT_TIMESTAMP WHERE order_code=?",
                    (code,)
                )
        return {"success": True, "cancelled": req.order_codes}
    else:
        ghn_msg = result["data"].get("message") or "Lỗi huỷ đơn"
        raise HTTPException(400, f"GHN lỗi: {ghn_msg}")


@app.post("/api/orders/print-label")
async def api_print_label(req: PrintLabelRequest):
    """Lấy link in tem vận đơn GHN."""
    token, shop_id = _get_seller_creds(req.seller_id)
    result = await get_print_token(token, shop_id, req.order_codes, seller_id=req.seller_id)
    if result["ok"]:
        return {
            "success": True,
            "print_url": result.get("print_url"),
            "token": result["data"].get("data", {}).get("token"),
        }
    else:
        raise HTTPException(400, result["data"].get("message") or "Lỗi tạo link in tem")


@app.post("/api/orders/tracking")
async def api_tracking(req: TrackingRequest):
    """Lấy lịch sử tracking đơn hàng (GHN API id=47)."""
    token, shop_id = _get_seller_creds(req.seller_id)
    result = await get_tracking_logs(token, shop_id, req.order_code, seller_id=req.seller_id)

    if not result["ok"]:
        raise HTTPException(400, result["data"].get("message") or "Không thể lấy tracking")

    logs = result["data"].get("data", []) or []

    _status_map = {
        "ready_to_pick": "pickup", "picking": "pickup",
        "picked": "pickup", "storing": "pickup",
        "delivering": "in_transit", "delivery_fail": "in_transit",
        "waiting_to_return": "returning", "return": "returning",
        "return_transporting": "returning", "returned": "returning",
        "delivered": "delivered", "cancel": "cancelled",
    }

    with get_conn() as conn:
        for log in logs:
            conn.execute("""
                INSERT OR IGNORE INTO tracking_logs (order_code, status, description, created_at)
                VALUES (?, ?, ?, ?)
            """, (
                req.order_code,
                log.get("Status") or log.get("status", ""),
                log.get("Description") or log.get("description", ""),
                log.get("UpdatedDate") or log.get("updated_date", ""),
            ))

        # Cập nhật status mới nhất trong bảng orders
        if logs:
            latest_status = logs[-1].get("Status") or logs[-1].get("status", "")
            db_status = _status_map.get(latest_status, "")
            if db_status:
                conn.execute(
                    "UPDATE orders SET status=?, updated_at=CURRENT_TIMESTAMP WHERE order_code=?",
                    (db_status, req.order_code)
                )

    return {"success": True, "order_code": req.order_code, "logs": logs}


# ══════════════════════════════════════════════════════════════════
# WEBHOOK từ GHN (nhận callback cập nhật trạng thái)
# ══════════════════════════════════════════════════════════════════

@app.post("/api/webhook/ghn")
async def ghn_webhook(payload: WebhookPayload):
    """
    GHN gọi endpoint này mỗi khi đơn hàng thay đổi trạng thái.
    Cấu hình URL này trong GHN Seller Portal.
    """
    _status_map = {
        "ready_to_pick": "pickup", "picking": "pickup",
        "picked": "pickup", "storing": "pickup",
        "delivering": "in_transit", "delivery_fail": "in_transit",
        "waiting_to_return": "returning", "return": "returning",
        "returned": "returning",
        "delivered": "delivered",
        "cancel": "cancelled",
    }
    db_status = _status_map.get(payload.Status or "", "")

    if db_status:
        with get_conn() as conn:
            # Nếu GHN báo thay đổi cước, cập nhật luôn (nếu có TotalFee)
            if payload.TotalFee is not None:
                conn.execute(
                    "UPDATE orders SET status=?, shipping_fee=?, updated_at=CURRENT_TIMESTAMP WHERE order_code=?",
                    (db_status, payload.TotalFee, payload.OrderCode)
                )
            else:
                conn.execute(
                    "UPDATE orders SET status=?, updated_at=CURRENT_TIMESTAMP WHERE order_code=?",
                    (db_status, payload.OrderCode)
                )
            
            # Lưu log chi tiết
            full_desc = payload.Description or ""
            if payload.Reason:
                full_desc = f"{full_desc} - {payload.Reason}".strip(" -")
                
            conn.execute(
                "INSERT INTO tracking_logs (order_code, status, description) VALUES (?, ?, ?)",
                (payload.OrderCode, payload.Status, full_desc)
            )

    return {"message": "ok"}


# ══════════════════════════════════════════════════════════════════
# AI ASSISTANT ROUTES (giữ nguyên)
# ══════════════════════════════════════════════════════════════════

@app.post("/api/ghn/learn")
async def api_learn():
    return {"success": True, "result": await learn_endpoints()}


@app.post("/api/analyze")
async def api_analyze(req: AnalyzeRequest):
    result = analyze_error(req.error_text)
    with get_conn() as conn:
        conn.execute("INSERT INTO chat_history (role, content) VALUES ('user', ?)", (req.error_text,))
        conn.execute("INSERT INTO chat_history (role, content) VALUES ('assistant', ?)", (str(result.get("root_cause","?")),))
    return result


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    is_error = any(k in req.message.lower() for k in
        ["error","failed","invalid","unauthorized","not found","exception","lỗi","thiếu","required","validation","500","401","403"])
    if is_error and len(req.message) > 10:
        result = analyze_error(req.message)
        if result["found"]:
            resp = _format_analysis_as_chat(result)
            _save_chat(req.message, resp)
            return {"type": "analysis", "message": resp, "data": result}
    if req.message.strip().startswith("/"):
        cmd_resp = _handle_command(req.message.strip())
        _save_chat(req.message, cmd_resp)
        return {"type": "command", "message": cmd_resp}
    resp = chat_response(req.message)
    _save_chat(req.message, resp)
    return {"type": "chat", "message": resp}


@app.post("/api/ghn/call")
async def api_ghn_call(req: GHNCallRequest):
    if not req.token:
        raise HTTPException(400, "Token không được trống")
    return await call_ghn_api(req.token, req.shop_id, req.endpoint, req.method, req.body)


@app.get("/api/ghn/provinces-public")
async def api_provinces_public(token: str):
    return await fetch_provinces(token)

@app.get("/api/ghn/districts-public")
async def api_districts_public(token: str, province_id: int):
    return await fetch_districts(token, province_id)

@app.get("/api/ghn/wards-public")
async def api_wards_public(token: str, district_id: int):
    return await fetch_wards(token, district_id)


# Knowledge Base
@app.get("/api/knowledge")
async def api_get_knowledge(page: int = 1, limit: int = 20, source: str = "all"):
    offset = (page - 1) * limit
    def _c(row):
        try: return row["c"]
        except Exception: return row[0]

    with get_conn() as conn:
        if source != "all":
            rows  = conn.execute("SELECT * FROM error_knowledge WHERE source=? ORDER BY hit_count DESC LIMIT ? OFFSET ?", (source, limit, offset)).fetchall()
            total = _c(conn.execute("SELECT COUNT(*) as c FROM error_knowledge WHERE source=?", (source,)).fetchone())
        else:
            rows  = conn.execute("SELECT * FROM error_knowledge ORDER BY hit_count DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            total = _c(conn.execute("SELECT COUNT(*) as c FROM error_knowledge").fetchone())
    return {"total": total, "page": page, "limit": limit, "items": [dict(r) for r in rows]}


@app.post("/api/knowledge")
async def api_add_knowledge(req: KnowledgeAddRequest):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO error_knowledge (error_msg, endpoint, root_cause, solution, code_wrong, code_right, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (req.error_msg, req.endpoint, req.root_cause, req.solution, req.code_wrong, req.code_right, req.source))
        new_id = conn.execute("SELECT lastval()").fetchone()[0]
    return {"success": True, "id": new_id}


@app.put("/api/knowledge/{kb_id}")
async def api_update_knowledge(kb_id: int, req: KnowledgeUpdateRequest):
    fields = {k: v for k, v in req.dict().items() if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update")
    with get_conn() as conn:
        if not conn.execute("SELECT id FROM error_knowledge WHERE id=?", (kb_id,)).fetchone():
            raise HTTPException(404, "Not found")
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE error_knowledge SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?", (*fields.values(), kb_id))
    return {"success": True}


@app.delete("/api/knowledge/{kb_id}")
async def api_delete_knowledge(kb_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM error_knowledge WHERE id=?", (kb_id,))
    return {"success": True}


@app.get("/api/endpoints")
async def api_endpoints():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM ghn_endpoints ORDER BY name").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/logs")
async def api_logs(limit: int = 50, seller_id: Optional[str] = None):
    with get_conn() as conn:
        if seller_id:
            rows = conn.execute("SELECT * FROM api_logs WHERE seller_id=? ORDER BY logged_at DESC LIMIT ?", (seller_id, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM api_logs ORDER BY logged_at DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/chat/history")
async def api_chat_history(limit: int = 50):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM chat_history ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return {"items": list(reversed([dict(r) for r in rows]))}


@app.get("/api/stats")
async def api_stats():
    def _count(conn, query, params=None):
        row = conn.execute(query, params or []).fetchone()
        if row is None:
            return 0
        # psycopg2 DictCursor → truy cập bằng tên cột
        try:
            return row["c"]
        except Exception:
            return row[0]

    with get_conn() as conn:
        stats = {
            "sellers_total":     _count(conn, "SELECT COUNT(*) as c FROM sellers"),
            "sellers_active":    _count(conn, "SELECT COUNT(*) as c FROM sellers WHERE status='active'"),
            "orders_total":      _count(conn, "SELECT COUNT(*) as c FROM orders"),
            "orders_pending":    _count(conn, "SELECT COUNT(*) as c FROM orders WHERE status='pending'"),
            "orders_delivered":  _count(conn, "SELECT COUNT(*) as c FROM orders WHERE status='delivered'"),
            "endpoints_learned": _count(conn, "SELECT COUNT(*) as c FROM ghn_endpoints"),
            "errors_known":      _count(conn, "SELECT COUNT(*) as c FROM error_knowledge WHERE root_cause != 'Chưa phân tích'"),
            "total_api_calls":   _count(conn, "SELECT COUNT(*) as c FROM api_logs"),
            "failed_api_calls":  _count(conn, "SELECT COUNT(*) as c FROM api_logs WHERE status_code != 200"),
        }
    return stats


# ── Internal helpers ───────────────────────────────────────────────

def _format_analysis_as_chat(r: dict) -> str:
    conf_emoji = "🟢" if r["confidence"] >= 80 else "🟡" if r["confidence"] >= 50 else "🔴"
    lines = [
        f"{conf_emoji} **Độ tin cậy: {r['confidence']}%**",
        f"\n📍 **Endpoint:** `{r['endpoint'] or 'Không xác định'}`",
        f"\n❓ **Nguyên nhân:**\n{r['root_cause']}",
        f"\n✅ **Cách sửa:**\n{r['solution']}",
    ]
    if r.get("code_wrong"): lines.append(f"\n❌ **Code sai:**\n```\n{r['code_wrong']}\n```")
    if r.get("code_right"): lines.append(f"\n✅ **Code đúng:**\n```\n{r['code_right']}\n```")
    return "\n".join(lines)


def _handle_command(cmd: str) -> str:
    with get_conn() as conn:
        if cmd == "/endpoints":
            rows = conn.execute("SELECT name, method, url FROM ghn_endpoints ORDER BY name").fetchall()
            return f"📡 **{len(rows)} Endpoints GHN:**\n\n" + "\n".join(
                f"• **{r['name']}** – `{r['method']} ...{r['url'].split('ghn.vn')[-1]}`" for r in rows)
        if cmd == "/kb":
            rows = conn.execute("SELECT error_msg, endpoint, hit_count FROM error_knowledge ORDER BY hit_count DESC LIMIT 10").fetchall()
            return f"📚 **Top 10 lỗi:**\n\n" + "\n".join(
                f"• [{r['hit_count']}x] `{r['error_msg'][:50]}` → `{r['endpoint'] or '?'}`" for r in rows)
        if cmd == "/logs":
            rows = conn.execute("SELECT endpoint, status_code, duration_ms, logged_at FROM api_logs ORDER BY logged_at DESC LIMIT 5").fetchall()
            return (f"📋 **5 API call gần nhất:**\n\n" + "\n".join(
                f"• `{r['endpoint']}` → {r['status_code']} ({r['duration_ms']}ms)" for r in rows)) if rows else "Chưa có log."
        if cmd == "/sellers":
            rows = conn.execute("SELECT id, name, status FROM sellers").fetchall()
            return "🏪 **Sellers:**\n\n" + "\n".join(
                f"• **{r['name']}** (`{r['id']}`) – {r['status']}" for r in rows)
        if cmd == "/help":
            return "**Lệnh:**\n• `/endpoints` `/kb` `/logs` `/sellers` `/help`"
    return f"Lệnh `{cmd}` không hợp lệ. Gõ `/help`."


def _save_chat(user_msg: str, assistant_msg: str):
    with get_conn() as conn:
        conn.execute("INSERT INTO chat_history (role, content) VALUES ('user', ?)", (user_msg[:2000],))
        conn.execute("INSERT INTO chat_history (role, content) VALUES ('assistant', ?)", (assistant_msg[:4000],))


# ══════════════════════════════════════════════════════════════════
# ADDRESS CHECK – kiểm tra địa chỉ cũ/mới (sau 7/2025)
# ══════════════════════════════════════════════════════════════════

_ward_lookup: dict | None = None
_new_ward_names: dict | None = None
_district_split: dict | None = None
_district_core: dict | None = None   # core name (bỏ prefix) -> {display, tinh, new_wards}

_DIST_PREFIXES = ('quận ', 'huyện ', 'thị xã ', 'thành phố ', 'tp ', 'tp.')

def _strip_dist_prefix(name: str) -> str:
    n = name.lower().strip()
    for p in _DIST_PREFIXES:
        if n.startswith(p):
            return n[len(p):].strip()
    return n

def _load_ward_data():
    global _ward_lookup, _new_ward_names, _district_split, _district_core
    if _ward_lookup is None:
        _base = os.path.dirname(os.path.abspath(__file__))
        try:
            with open(os.path.join(_base, 'ward_lookup.json'), encoding='utf-8') as f:
                _ward_lookup = json.load(f)
        except Exception:
            _ward_lookup = {}
        try:
            with open(os.path.join(_base, 'new_ward_names.json'), encoding='utf-8') as f:
                _new_ward_names = json.load(f)
        except Exception:
            _new_ward_names = {}
        try:
            with open(os.path.join(_base, 'old_district_split.json'), encoding='utf-8') as f:
                _district_split = json.load(f)
        except Exception:
            _district_split = {}
        # Build core-name map (bỏ prefix quận/huyện) — chỉ giữ quận tách >1 phường
        _district_core = {}
        for k, v in _district_split.items():
            if len(v.get('new_wards', [])) > 1:
                core = _strip_dist_prefix(v.get('display', k))
                # bỏ core thuần số ("1".."12") để tránh over-match
                if core and not core.isdigit() and len(core) >= 3:
                    _district_core[core] = v


_ADMIN_PREFIXES = ('phường ', 'xã ', 'thị trấn ', 'thị xã ', 'phuong ', 'xa ', 'thi tran ')

def _check_address(text: str) -> dict:
    """
    Tìm tên phường/xã cũ (trước 7/2025) trong đoạn text địa chỉ.
    - Chỉ match tên cũ có prefix hành chính (Phường/Xã/Thị trấn) để tránh nhầm tên đường.
    - Nếu text chứa tên QUẬN/HUYỆN cũ đã tách nhiều phường → cảnh báo mơ hồ,
      KHÔNG khẳng định được phường mới (vì cần biết phường số cũ hoặc để GHN tự resolve).
    """
    _load_ward_data()
    text_lower = text.lower()
    matches = []
    seen_new = set()
    import re as _re2
    def _bounded(key):
        # key phải đứng sau ranh giới (đầu/phẩy/ngoặc/space) và TRƯỚC dấu phẩy/ngoặc/hết
        # → tránh 'xã thanh' khớp lỏng trong 'xã thanh oai'
        pat = r'(?:^|[,(;]|\s)' + _re2.escape(key) + r'\s*(?:$|[,)\;])'
        return _re2.search(pat, text_lower) is not None

    for old_key, info in sorted(_ward_lookup.items(), key=lambda x: -len(x[0])):
        if not any(old_key.startswith(p) for p in _ADMIN_PREFIXES):
            continue
        # Tên vừa là phường CŨ (nơi khác) vừa là phường MỚI hợp lệ (VD: Phường Bình Thạnh)
        # → không được coi là cũ; để nhánh new_found xử lý
        if old_key in _new_ward_names:
            continue
        if _bounded(old_key):
            key = info['new'].lower()
            if key not in seen_new:
                seen_new.add(key)
                matches.append({'old': info['old'], 'new': info['new'], 'tinh': info['tinh']})

    # Check new names
    new_found = []
    for new_key, info in _new_ward_names.items():
        if new_key in text_lower and new_key not in seen_new:
            new_found.append({'name': info['name'], 'tinh': info['tinh']})

    # Phát hiện QUẬN/HUYỆN cũ đã tách nhiều phường (mơ hồ)
    ambiguous = None
    if not matches:  # chỉ cảnh báo khi chưa map được phường cũ cụ thể
        for core, v in _district_core.items():
            if core in text_lower:
                ambiguous = {
                    'district': v.get('display', ''),
                    'tinh': v.get('tinh', ''),
                    'new_wards': v.get('new_wards', []),
                }
                break

    # Phát hiện NHÓM phường cùng gốc tên (An Nhơn, An Nhơn Bắc/Đông/Nam/Tây...) → dễ nhầm
    confusable = None
    if not matches and new_found:
        pc = _detect_province(text)
        if pc:
            for m in new_found:
                grp = _confusable_group(pc, m['name'])
                if len(grp) >= 2:
                    confusable = {'stated': m['name'], 'group': grp,
                                  'province': _load_resolver().get('provinces', {}).get(pc, '')}
                    break

    return {
        'old_matches': matches,
        'new_found': new_found,
        'ambiguous': ambiguous,
        'confusable': confusable,
        'is_old': len(matches) > 0,
        'is_new': len(matches) == 0 and not ambiguous and len(new_found) > 0,
        'is_ambiguous': ambiguous is not None,
    }


class AddressCheckBatchRequest(BaseModel):
    addresses: List[str]


@app.get("/api/address-check")
async def api_address_check(q: str):
    """Kiểm tra địa chỉ có phải địa chỉ cũ (trước 7/2025) không."""
    result = _check_address(q)
    return result


@app.post("/api/address-check/batch")
async def api_address_check_batch(req: AddressCheckBatchRequest):
    """Kiểm tra hàng loạt địa chỉ (cho tạo đơn song song)."""
    return {'results': [_check_address(addr) for addr in req.addresses]}


# ══════════════════════════════════════════════════════════════════
# ADDRESS RESOLVER – tra phường mới chính xác từ phường CŨ + tỉnh + quận
# (offline resolver + fallback live sapnhap.bando.com.vn)
# ══════════════════════════════════════════════════════════════════
import unicodedata as _ud
import re as _re

_resolver = None          # {"provinces": {...}, "resolver": {...}}
_live_cache = None        # cache dữ liệu p.co_dvhc live (parsed)

_PROV_ALIASES = {
    'hcm': 'ho chi minh', 'tphcm': 'ho chi minh', 'tp hcm': 'ho chi minh',
    'sai gon': 'ho chi minh', 'sg': 'ho chi minh',
    'hn': 'ha noi', 'tp ha noi': 'ha noi',
}
_WARD_PREFIXES2 = ('phuong ', 'xa ', 'thi tran ', 'thi xa ', 'dac khu ')


def _n(s):
    s = _ud.normalize('NFD', (s or '').lower())
    s = ''.join(c for c in s if _ud.category(c) != 'Mn')
    s = s.replace('đ', 'd')   # NFD không tách đ → phải thay tay
    return ' '.join(s.split())


def _prov_core(s):
    n = _n(s)
    for p in ('thu do ', 'thanh pho ', 'tinh '):
        if n.startswith(p):
            n = n[len(p):]
    return n.strip()


def _ward_core(w):
    n = _n(w)
    for p in _WARD_PREFIXES2:
        if n.startswith(p):
            return n[len(p):].strip()
    return n


_phase1 = None

def _load_phase1():
    """Bảng sáp nhập đợt 1 (NQ 1278, 01/01/2025) — data sapnhap chỉ có đợt 2."""
    global _phase1
    if _phase1 is None:
        base = os.path.dirname(os.path.abspath(__file__))
        try:
            with open(os.path.join(base, 'phase1_merges.json'), encoding='utf-8') as f:
                _phase1 = {k: v for k, v in json.load(f).items() if not k.startswith('_')}
        except Exception:
            _phase1 = {}
    return _phase1


def _phase1_chain(pc, wc, tn):
    """Nếu phường số biến mất từ đợt 1 (vd P24 Bình Thạnh) → trả list key phường
    còn tồn tại để tra tiếp đợt 2. Chỉ áp khi text có nhắc đúng quận."""
    p1 = _load_phase1().get(pc, {})
    out = []
    for dkey, mapping in p1.items():
        core = dkey
        for dp in ('quan ', 'huyen ', 'thi xa ', 'thanh pho '):
            if core.startswith(dp):
                core = core[len(dp):].strip()
                break
        if core and core in tn and wc in mapping:
            out.extend(mapping[wc])
    return out


def _confusable_group(pc, name):
    """Tìm nhóm phường mới cùng gốc tên trong tỉnh (An Nhơn, An Nhơn Bắc/Đông...).
    Trả list ≥2 nếu 'name' là 1 phần của nhóm dễ nhầm."""
    data = _load_resolver()
    bucket = data.get('resolver', {}).get(pc, {})
    base = _ward_core(name)
    if len(base) < 3:
        return []
    fam = set()
    for wc, lst in bucket.items():
        for c in lst:
            nc = _ward_core(c['new'])
            if nc == base or nc.startswith(base + ' '):
                fam.add(c['new'])
    return sorted(fam)


def _load_resolver():
    global _resolver
    if _resolver is None:
        base = os.path.dirname(os.path.abspath(__file__))
        try:
            with open(os.path.join(base, 'ward_resolver.json'), encoding='utf-8') as f:
                _resolver = json.load(f)
        except Exception:
            _resolver = {'provinces': {}, 'resolver': {}}
    return _resolver


_dist_prov_map = None

def _district_prov_map():
    """Map lõi tên quận/huyện cũ -> tỉnh mới (chỉ giữ tên DUY NHẤT 1 tỉnh).
    Dùng suy ra tỉnh khi địa chỉ không ghi tỉnh (VD 'Phường 24, Bình Thạnh')."""
    global _dist_prov_map
    if _dist_prov_map is not None:
        return _dist_prov_map
    data = _load_resolver()
    m = {}
    for pc, wards in data.get('resolver', {}).items():
        for lst in wards.values():
            for c in lst:
                for part in (c.get('dist') or '').split('|'):
                    part = part.strip()
                    if not part:
                        continue
                    core = part
                    for dp in ('quan ', 'huyen ', 'thi xa ', 'thanh pho ', 'tp '):
                        if core.startswith(dp):
                            core = core[len(dp):].strip()
                            break
                    if core and not core.isdigit() and len(core) >= 4:
                        m.setdefault(core, set()).add(pc)
    # thêm từ bảng phase1 (quận đã biến mất khỏi data đợt 2)
    for pc, dists in _load_phase1().items():
        for dk in dists.keys():
            core = dk
            for dp in ('quan ', 'huyen ', 'thi xa ', 'thanh pho '):
                if core.startswith(dp):
                    core = core[len(dp):].strip()
                    break
            if core and not core.isdigit() and len(core) >= 4:
                m.setdefault(core, set()).add(pc)
    _dist_prov_map = {k: list(v)[0] for k, v in m.items() if len(v) == 1}
    return _dist_prov_map


def _detect_province(text, hint=None):
    """Trả province_core từ hint hoặc dò trong text."""
    data = _load_resolver()
    provs = data.get('provinces', {})
    if hint:
        pc = _prov_core(hint)
        if pc in provs:
            return pc
        for a, full in _PROV_ALIASES.items():
            if a in _n(hint) and full in provs:
                return full
    old_aliases = data.get('province_aliases', {})
    # Tỉnh nằm ở CUỐI địa chỉ → quét từng đoạn (tách dấu phẩy) từ cuối lên,
    # tránh tên đường trùng tỉnh ('12 Nguyễn Huệ' ≠ tỉnh Huế).
    segments = [_n(s) for s in (text or '').split(',')]
    for seg in reversed(segments):
        def _in_seg(term):
            return _re.search(r'(?:^|[\s(])' + _re.escape(term) + r'(?:$|[\s)])', seg) is not None
        for a, full in _PROV_ALIASES.items():
            if _in_seg(a) and full in provs:
                return full
        for pc in sorted(provs.keys(), key=lambda k: -len(k)):
            if _in_seg(pc):
                return pc
        for oc in sorted(old_aliases.keys(), key=lambda k: -len(k)):
            if _in_seg(oc):
                return old_aliases[oc]
    # Cuối cùng: suy tỉnh từ tên QUẬN/HUYỆN duy nhất (VD 'Bình Thạnh' → HCM)
    dp = _district_prov_map()
    for seg in reversed(segments):
        for core in sorted(dp.keys(), key=lambda k: -len(k)):
            if _re.search(r'(?:^|[\s(])' + _re.escape(core) + r'(?:$|[\s).,])', seg):
                return dp[core]
    return None


_OLD_MARK = ('cu', 'củ', 'cũ')

def _extract_old_wards(text):
    """
    Trích tên phường/xã CŨ từ địa chỉ. Ưu tiên phần trong ngoặc '(... cũ)',
    và pattern '<ward> (cũ)'. Chuẩn hóa P5 -> phường 5, phường 06 -> phường 6.
    """
    olds = []
    paren_re = _re.compile(r'\(([^)]*)\)')
    for m in paren_re.finditer(text or ''):
        content = m.group(1).strip()
        cn = _n(content)
        # bỏ chữ 'cu'/'cũ'
        cn_clean = _re.sub(r'\bcu\b', '', cn).strip(' .,-')
        if cn_clean:
            olds.append(cn_clean)
        else:
            # '(cũ)' rỗng → lấy cụm phường/xã ngay trước dấu '('
            before = text[:m.start()]
            mb = _re.search(r'((?:xã|phường|thị trấn|thị xã)\s+[^,()]+)$', before.strip(), _re.IGNORECASE)
            if mb:
                olds.append(_n(mb.group(1)))

    # Quét cả các cụm 'phường/xã/thị trấn <tên>' ghi THẲNG trong địa chỉ (kể cả số),
    # bỏ phần trong ngoặc để không trùng.
    text_nopar = _re.sub(r'\([^)]*\)', ' ', text or '')
    for m in _re.finditer(r'(?:phường|phuong|xã|xa|thị trấn|thi tran|thị xã|thi xa)\s+([^,()]+)',
                          text_nopar, _re.IGNORECASE):
        olds.append(_n(m.group(1)))
    # Viết tắt 'p4', 'P.4', 'p 04' → phường 4 (đứng riêng, không phải phần của từ khác)
    for m in _re.finditer(r'(?:^|[\s,])[pP]\.?\s*0*(\d{1,2})(?=$|[\s,])', text_nopar):
        olds.append('phuong ' + m.group(1))

    # chuẩn hóa số phường
    out = []
    for o in olds:
        o = _re.sub(r'\bp\s*0*(\d+)\b', r'phuong \1', o)
        o = _re.sub(r'phuong\s*0+(\d+)', r'phuong \1', o)
        o = o.strip()
        if o and o not in out:
            out.append(o)
    return out


def _scan_province_oldwards(pc, text_norm):
    """Quét tên xã/phường CŨ (theo bucket tỉnh) xuất hiện trong text — kể cả
    không nằm trong ngoặc '(... cũ)'. Chỉ nhận tên ≥2 chữ, ≥6 ký tự, khớp nguyên cụm."""
    data = _load_resolver()
    bucket = data.get('resolver', {}).get(pc, {})
    found = []
    for wc in bucket.keys():
        if len(wc) < 6 or ' ' not in wc:
            continue
        if _re.search(r'(?:^|\s)' + _re.escape(wc) + r'(?:$|\s|,)', text_norm):
            found.append(wc)
    # ưu tiên cụm dài nhất, bỏ cụm con
    found.sort(key=len, reverse=True)
    result = []
    for wc in found:
        if not any(wc != other and wc in other for other in result):
            result.append(wc)
    return result


def _resolve_offline(text, province_hint=None):
    data = _load_resolver()
    resolver = data.get('resolver', {})
    provs = data.get('provinces', {})
    pc = _detect_province(text, province_hint)
    olds = _extract_old_wards(text)
    tn = _n(text)

    # Gộp thêm tên xã cũ quét theo tỉnh (bắt tên nằm ngoài ngoặc, vd 'Hồng Dương')
    if pc:
        for wc in _scan_province_oldwards(pc, tn):
            if wc not in olds:
                olds.append(wc)

    results = []
    for o in olds:
        wc = _ward_core(o)
        # danh sách province để tra: ưu tiên province xác định, else all
        prov_keys = [pc] if pc else list(resolver.keys())
        def _dist_in_text(dist_str):
            for part in dist_str.split('|'):
                part = part.strip()
                if not part:
                    continue
                # khớp nguyên cụm quận (vd 'quan 10' cho Quận số)
                if len(part) >= 5 and part in tn:
                    return True
                core = part
                for dp in ('quan ', 'huyen ', 'thi xa ', 'thanh pho ', 'tp '):
                    if core.startswith(dp):
                        core = core[len(dp):].strip()
                        break
                if core and len(core) >= 3 and core in tn:
                    return True
            return False

        cands = []
        for pk in prov_keys:
            for c in resolver.get(pk, {}).get(wc, []):
                cands.append({'new': c['new'], 'dist': c['dist'], 'prov': pk,
                              'old_disp': c.get('old', '')})
        dist_matched = [c for c in cands if c['dist'] and _dist_in_text(c['dist'])]

        # Phường biến mất từ các ĐỢT TRƯỚC (2020-2021, 1/1/2025) — vd P24 Bình Thạnh,
        # P7 Quận 3, P2 Quận 8 — bucket đợt-2 không có entry khớp quận
        # → tra bảng đợt trước, chuyển sang phường còn tồn tại rồi tra tiếp.
        if pc and not dist_matched:
            p1cands = []
            _p1_disp = ('Phường ' + wc) if wc.isdigit() else wc
            for surv in _phase1_chain(pc, wc, tn):
                for c in resolver.get(pc, {}).get(surv, []):
                    p1cands.append({'new': c['new'], 'dist': c['dist'], 'prov': pc,
                                    'via_phase1': wc, 'old_disp': _p1_disp})
            if p1cands:
                cands = p1cands
                dist_matched = [c for c in cands if c['dist'] and _dist_in_text(c['dist'])]

        # giữ nhóm khớp quận (dù còn >1) để loại các quận khác
        if dist_matched:
            cands = dist_matched
        # nếu địa chỉ đã ghi sẵn tên phường MỚI → chọn đúng cái đó
        if len(cands) > 1:
            named = [c for c in cands if _n(c['new']) in tn]
            if len(named) == 1:
                cands = named
        # dedup theo new
        seen = set(); uniq = []
        for c in cands:
            if c['new'].lower() not in seen:
                seen.add(c['new'].lower()); uniq.append(c)

        # so phường mày GHI trong địa chỉ (bỏ phần trong ngoặc để không nhầm phường cũ)
        # với phường ĐÚNG theo data nhà nước.
        stated_wrong = None      # phường mới mày ghi nhưng SAI
        stated_correct = False   # mày ghi ĐÚNG phường mới
        tn_nopar = _n(_re.sub(r'\([^)]*\)', ' ', text))
        if len(uniq) == 1 and pc:
            correct = _n(uniq[0]['new'])
            for wc2, lst in resolver.get(pc, {}).items():
                for c2 in lst:
                    nn = _n(c2['new'])
                    if len(nn) < 5:
                        continue
                    if _re.search(r'(?:^|\s)' + _re.escape(nn) + r'(?:$|\s|,)', tn_nopar):
                        if nn == correct:
                            stated_correct = True
                        elif not stated_wrong:
                            stated_wrong = c2['new']

        results.append({
            'old': o,
            'candidates': uniq,
            'confident': len(uniq) == 1,
            'correct_ward': uniq[0]['new'] if len(uniq) == 1 else None,
            'stated_wrong': stated_wrong,
            'stated_correct': stated_correct,
        })
    return {
        'province': provs.get(pc, '') if pc else '',
        'province_core': pc or '',
        'old_wards': olds,
        'results': results,
    }


GOONG_API_KEY = os.environ.get("GOONG_API_KEY", "")

async def _geocode_vn(q, viewbox=None):
    """Geocode: Goong.io (data VN, số nhà hẻm chính xác) → Nominatim → Photon.
    viewbox=(lonmin,latmin,lonmax,latmax): giới hạn vùng tìm (tránh trùng tên
    đường ở thành phố khác trong cùng tỉnh mới, VD Kon Tum vs Quảng Ngãi)."""
    import httpx
    # 1) Goong.io — geocoder Việt Nam, định vị được số nhà kiểu 405/15
    if GOONG_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get('https://rsapi.goong.io/geocode',
                                     params={'address': q, 'api_key': GOONG_API_KEY})
            js = r.json()
            results = js.get('results') or []
            if results:
                loc = results[0].get('geometry', {}).get('location', {})
                lon, lat = float(loc.get('lng', 0)), float(loc.get('lat', 0))
                if lon and lat:
                    # Goong không có tham số bbox → tự kiểm tra sau
                    if not viewbox or (viewbox[0] <= lon <= viewbox[2]
                                       and viewbox[1] <= lat <= viewbox[3]):
                        return lon, lat
        except Exception as e:
            print(f"[geocode-goong] {e}", flush=True)
    params = {'q': q, 'format': 'json', 'limit': 1, 'countrycodes': 'vn'}
    if viewbox:
        params['viewbox'] = f"{viewbox[0]},{viewbox[1]},{viewbox[2]},{viewbox[3]}"
        params['bounded'] = 1
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                'https://nominatim.openstreetmap.org/search',
                params=params,
                headers={'User-Agent': 'my-pos-tool/1.0 (GHN address checker)'})
        js = r.json()
        if js:
            return float(js[0]['lon']), float(js[0]['lat'])
    except Exception as e:
        print(f"[geocode] {e}", flush=True)
    # Fallback: Photon (fuzzy hơn, chịu được số nhà kiểu 266/10)
    try:
        pparams = {'q': q, 'limit': 1}
        if viewbox:
            pparams['bbox'] = f"{viewbox[0]},{viewbox[1]},{viewbox[2]},{viewbox[3]}"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get('https://photon.komoot.io/api/', params=pparams)
        fs = r.json().get('features', [])
        if fs:
            lon, lat = fs[0]['geometry']['coordinates'][:2]
            return float(lon), float(lat)
    except Exception as e:
        print(f"[geocode-photon] {e}", flush=True)
    return None


def _polys_bbox(polys, pad=0.15):
    """BBox (lonmin,latmin,lonmax,latmax) của polygon list, nới thêm pad độ (~16km)."""
    lons, lats = [], []
    for poly in polys:
        for ring in poly[:1]:
            for pt in ring:
                lons.append(pt[0]); lats.append(pt[1])
    if not lons:
        return None
    return (min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad)


def _build_geo_queries(text, province_disp):
    """Dựng các query geocode. Nominatim fail khi query chứa 'quận/phường/thành phố'
    hoặc số nhà kiểu 266/10 → phải bỏ hết prefix hành chính + cụm phường + số nhà."""
    t = _re.sub(r'\([^)]*\)', ' ', text or '')

    def _clean_admin(s):
        # bỏ prefix hành chính, giữ tên riêng: 'quận tân phú' -> 'tân phú'
        return _re.sub(r'(?i)\b(?:quận|huyện|thị xã|thị trấn|thành phố|tỉnh|q\.|tp\.?|h\.)\s*',
                       '', s).strip(' ,')

    prov_clean = _clean_admin(province_disp or '')
    segs = [s.strip() for s in t.split(',') if s.strip()]
    queries = []
    if segs:
        street = _re.sub(r'^[\s\d/\-]+', '', segs[0]).strip()
        # số nhà dạng chữ+số ('K154 H02/4 Vũ Lăng') → bỏ các token chứa số ở đầu
        toks = street.split()
        while toks and _re.search(r'\d', toks[0]):
            toks.pop(0)
        street2 = ' '.join(toks)
        if street2 and street2 != street:
            street = street2
        dist_seg = next((s for s in segs[1:] if _re.search(r'(?i)quận|huyện|q\.|thị xã|tp', s)), '')
        dist = _clean_admin(dist_seg)
        # 1) KÈM SỐ NHÀ trước — Goong định vị được số nhà hẻm (405/15...)
        if segs[0] != street:
            parts2 = [p for p in (segs[0], dist, prov_clean) if p]
            q2 = ', '.join(parts2)
            if q2:
                queries.append(q2)
        # 2) đường + quận + tỉnh (dạng sạch — Nominatim thích nhất)
        parts = [p for p in (street, dist, prov_clean) if p]
        if parts:
            q1 = ', '.join(parts)
            if q1 not in queries:
                queries.append(q1)
    # 3) toàn văn bỏ cụm phường + prefix hành chính
    t3 = _re.sub(r'(?i)(?:phường|phuong|xã|thị trấn|p\.)\s*[^,]+,?', ' ', t)
    t3 = _clean_admin(' '.join(t3.split()))
    if t3 and t3 not in queries:
        queries.append(t3)
    return queries


_poly_cache: dict = {}

async def _ward_polygon(malk):
    """Tải ranh giới phường (GeoJSON) từ sapnhap.bando.com.vn, cache theo malk."""
    if malk in _poly_cache:
        return _poly_cache[malk]
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post('https://sapnhap.bando.com.vn/pread_json',
                                  data={'id': malk},
                                  headers={'Content-Type': 'application/x-www-form-urlencoded'})
        gj = json.loads(r.text.strip())
        polys = []
        for f in gj.get('features', []):
            g = f.get('geometry', {})
            cs = g.get('coordinates', [])
            if g.get('type') == 'Polygon':
                polys.append(cs)
            elif g.get('type') == 'MultiPolygon':
                polys.extend(cs)
        _poly_cache[malk] = polys
        return polys
    except Exception as e:
        print(f"[polygon] {e}", flush=True)
        return []


def _point_in_polys(lon, lat, polys):
    """Ray-casting point-in-polygon trên vòng ngoài mỗi polygon."""
    def _in_ring(rg):
        inside = False
        n = len(rg)
        for i in range(n):
            x1, y1 = rg[i][0], rg[i][1]
            x2, y2 = rg[(i + 1) % n][0], rg[(i + 1) % n][1]
            if (y1 > lat) != (y2 > lat):
                xin = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
                if lon < xin:
                    inside = not inside
        return inside
    for poly in polys:
        if poly and _in_ring(poly[0]):
            return True
    return False


async def _resolve_live(province_core, ward_core):
    """Fallback: fetch p.co_dvhc live, tìm phường mới cho old ward trong tỉnh."""
    global _live_cache
    try:
        if _live_cache is None:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post('https://sapnhap.bando.com.vn/p.co_dvhc',
                                      headers={'Content-Type': 'application/x-www-form-urlencoded'})
            raw = r.content.decode('utf-8-sig')
            alldata = json.loads(raw)
            cap_tinh = [x for x in alldata if 'captinh' in x.get('malk', '')]
            prov_by_ma = {x['ma']: x['ten'] for x in cap_tinh}
            _live_cache = {'xa': [x for x in alldata if 'capxa' in x.get('malk', '')],
                           'prov_by_ma': prov_by_ma}
        out = []
        for x in _live_cache['xa']:
            pdisp = _live_cache['prov_by_ma'].get(x.get('magoc', ''), '')
            if province_core and _prov_core(pdisp) != province_core:
                continue
            truoc = _n(x.get('truocsapnhap', ''))
            if ward_core and ward_core in truoc:
                out.append(x['ten'])
        return list(dict.fromkeys(out))
    except Exception as e:
        print(f"[resolve_live] {e}", flush=True)
        return []


def _reverse_lookup(text, province_hint=None):
    """Tra NGƯỢC: địa chỉ/tên phường MỚI → các phường/xã CŨ đã gộp thành nó."""
    data = _load_resolver()
    provs = data.get('provinces', {})
    nw_all = data.get('new_wards', {})
    pc = _detect_province(text, province_hint)
    tn = _n(text)

    prov_keys = [pc] if pc else list(nw_all.keys())
    matches = []
    seen = set()
    for pk in prov_keys:
        bucket = nw_all.get(pk, {})
        # match tên dài trước để tránh trùng cụm con (An Khánh vs An Khánh Đông)
        for key in sorted(bucket.keys(), key=lambda k: -len(k)):
            if len(key) < 5:
                continue
            if _re.search(r'(?:^|[\s,(])' + _re.escape(key) + r'(?:$|[\s,)])', tn):
                info = bucket[key]
                if info['name'].lower() in seen:
                    continue
                seen.add(info['name'].lower())
                raw = info.get('old', '')
                # tách danh sách cũ, giữ chú thích trong ngoặc
                parts, buf, depth = [], '', 0
                for ch in raw:
                    if ch == '(':
                        depth += 1
                    elif ch == ')':
                        depth -= 1
                    if ch == ',' and depth == 0:
                        parts.append(buf.strip()); buf = ''
                    else:
                        buf += ch
                if buf.strip():
                    parts.append(buf.strip())
                matches.append({
                    'new': info['name'],
                    'prov': provs.get(pk, ''),
                    'old_raw': raw,
                    'old_list': parts,
                    'kept': 'giữ nguyên' in raw.lower(),
                })
    return {
        'province': provs.get(pc, '') if pc else '',
        'matches': matches,
        'found': len(matches) > 0,
    }


def _derive_new_from_old(pc, wc, dist_norm):
    """Suy phường MỚI từ (ward-core cũ + quận). Tra bucket đợt-2 trước;
    không có → nối chuỗi phase-1 (phường biến mất đợt trước, vd P10 Q8→Hưng Phú)."""
    resolver = _load_resolver().get('resolver', {})
    bucket = resolver.get(pc, {})
    for c in bucket.get(wc, []):
        cd = _n(c.get('dist', ''))
        if not dist_norm or not cd or dist_norm in cd or cd in dist_norm:
            return c
    # phase-1 chain theo quận
    for dk, mp in _load_phase1().get(pc, {}).items():
        dkn = _n(dk)
        if dist_norm and not (dist_norm in dkn or dkn in dist_norm):
            continue
        if wc in mp:
            for surv in mp[wc]:
                for c in bucket.get(surv, []):
                    cd = _n(c.get('dist', ''))
                    if not cd or dkn in cd or cd in dkn:
                        return c
                if bucket.get(surv):
                    return bucket[surv][0]
    return None


_old_bounds_cache: dict = {}

def _load_old_bounds(pc):
    """Ranh giới phường/xã CŨ (GADM 4.1) theo tỉnh mới — lazy + cache."""
    if pc in _old_bounds_cache:
        return _old_bounds_cache[pc]
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, 'old_bounds', pc.replace(' ', '_') + '.json')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = []
    _old_bounds_cache[pc] = data
    return data


def _pip_geom(lon, lat, g):
    cs = g.get('coordinates', [])
    polys = [cs] if g.get('type') == 'Polygon' else cs
    return _point_in_polys(lon, lat, polys)


@app.get("/api/address-reverse")
async def api_address_reverse(q: str, province: Optional[str] = None, live: bool = True):
    """
    Tra ngược địa chỉ MỚI (sau 7/2025) → phường/xã CŨ.
    Khi có tên đường → geocode + point-in-polygon với ranh giới CŨ (GADM)
    để chỉ ra CHÍNH XÁC phường cũ, không chỉ liệt kê thành phần.
    """
    res = _reverse_lookup(q, province)
    pc = _detect_province(q, province)

    if live and pc:
        bounds = _load_old_bounds(pc)
        if bounds:
            # Neo vùng tìm quanh phường MỚI user ghi (tránh trùng tên đường
            # ở thành phố khác cùng tỉnh mới — VD Kon Tum vs Quảng Ngãi)
            viewbox = None
            if res.get('matches'):
                wm = _load_resolver().get('ward_malk', {}).get(pc, {})
                malk = wm.get(_n(res['matches'][0]['new']))
                if malk:
                    stated_polys = await _ward_polygon(malk)
                    if stated_polys:
                        viewbox = _polys_bbox(stated_polys, pad=0.15)
            pt = None
            for q_geo in _build_geo_queries(q, res.get('province', '')):
                if viewbox:
                    pt = await _geocode_vn(q_geo, viewbox=viewbox)
                if not pt:
                    pt = await _geocode_vn(q_geo)
                    # điểm phải nằm gần vùng phường đã ghi, không thì bỏ (geocode lạc)
                    if pt and viewbox:
                        big = (viewbox[0] - 0.35, viewbox[1] - 0.35,
                               viewbox[2] + 0.35, viewbox[3] + 0.35)
                        if not (big[0] <= pt[0] <= big[2] and big[1] <= pt[1] <= big[3]):
                            pt = None
                if pt:
                    break
            if pt:
                lon, lat = pt
                for e in bounds:
                    if _pip_geom(lon, lat, e['g']):
                        res['resolved_old'] = {'name': e['name'], 'dist': e['dist']}
                        res['geo'] = True
                        break

        # Từ phường CŨ (theo vị trí) suy ra phường MỚI đúng → đối chiếu với
        # phường mới user GHI. Khác nhau = ghi SAI (VD: ghi Cẩm Lệ, đúng An Khê).
        ro = res.get('resolved_old')
        if ro and res.get('matches'):
            dcand = _derive_new_from_old(pc, _ward_core(ro['name']), _n(ro.get('dist', '')))
            derived = dcand['new'] if dcand else None
            if derived:
                res['derived_new'] = derived
                stated = res['matches'][0]['new']
                if _n(derived) != _n(stated):
                    res['stated_wrong_new'] = stated
                    res['correct_new'] = derived
    return res


@app.get("/api/address-resolve")
async def api_address_resolve(q: str, province: Optional[str] = None, live: bool = True):
    """
    Tra phường MỚI chính xác từ địa chỉ có ghi phường CŨ.
    - Offline trước; nếu không chắc và live=true → xác minh qua sapnhap.bando.com.vn.
    """
    res = _resolve_offline(q, province)

    # Fallback live CHỈ khi offline KHÔNG có ứng viên nào (tránh phá bộ đã lọc theo quận)
    if live:
        for item in res['results']:
            if not item['candidates']:
                live_cands = await _resolve_live(res['province_core'], _ward_core(item['old']))
                for lc in live_cands:
                    item['candidates'].append({'new': lc, 'dist': '', 'prov': res['province_core'], 'source': 'live'})
                item['confident'] = len(item['candidates']) == 1

    # GEO disambiguation: khi còn 2-6 ứng viên → geocode địa chỉ (OSM) rồi
    # tra tọa độ vào ranh giới phường (polygon từ sapnhap.bando.com.vn).
    if live and res.get('province_core'):
        need_geo = [it for it in res['results']
                    if not it['confident'] and 2 <= len(it['candidates']) <= 6]
        if need_geo:
            pt = None
            for q_geo in _build_geo_queries(q, res.get('province', '')):
                pt = await _geocode_vn(q_geo)
                if pt:
                    break
            if pt:
                lon, lat = pt
                wm = _load_resolver().get('ward_malk', {}).get(res['province_core'], {})
                for item in need_geo:
                    hits = []
                    for c in item['candidates']:
                        malk = wm.get(_n(c['new']))
                        if not malk:
                            continue
                        if _point_in_polys(lon, lat, await _ward_polygon(malk)):
                            hits.append(c)
                    if len(hits) == 1:
                        item['candidates'] = hits
                        item['confident'] = True
                        item['correct_ward'] = hits[0]['new']
                        item['geo'] = True
                        # Nếu user ghi phường đó như phường HIỆN TẠI (không kèm 'cũ')
                        # mà thực tế đường nằm ở phường khác → báo SAI rõ ràng
                        correct_n = _n(hits[0]['new'])
                        old_disp = hits[0].get('old_disp') or item['old']
                        tn_nopar = _n(_re.sub(r'\([^)]*\)', ' ', q))
                        old_n = _n(old_disp)
                        wrote_old_as_current = (
                            old_n in tn_nopar
                            and not _re.search(_re.escape(old_n) + r'\s*cu\b', tn_nopar)
                        )
                        if wrote_old_as_current and correct_n not in tn_nopar:
                            item['stated_wrong'] = old_disp
                    elif not hits:
                        # Điểm không thuộc ứng viên nào — 'phường cũ' trích được
                        # có thể thật ra là tên QUẬN (vd 'Tân Phú'). Tra thẳng
                        # ranh giới phường CŨ (local) → suy phường mới đúng.
                        pc_g = res['province_core']
                        actual = next((e for e in _load_old_bounds(pc_g)
                                       if _pip_geom(lon, lat, e['g'])), None)
                        if actual:
                            derived = _derive_new_from_old(
                                pc_g, _ward_core(actual['name']), _n(actual.get('dist', '')))
                            if derived:
                                item['candidates'] = [{'new': derived['new'],
                                                       'dist': derived.get('dist', ''),
                                                       'prov': pc_g,
                                                       'old_disp': actual['name']}]
                                item['confident'] = True
                                item['correct_ward'] = derived['new']
                                item['geo'] = True

    # GEO VERIFY chiều xuôi: kể cả khi ĐÃ chắc theo phường cũ user ghi,
    # kiểm chứng vị trí đường có thật sự nằm trong phường cũ đó không
    # (VD ghi P27 Bình Thạnh nhưng đường XVNT nằm P25 → phải sửa).
    if live and res.get('province_core'):
        pc2 = res['province_core']
        bounds = _load_old_bounds(pc2)
        for item in res['results']:
            if not item['confident'] or item.get('geo') or not item['candidates']:
                continue
            old_disp = item['candidates'][0].get('old_disp') or item['old']
            def _ns2(s):
                return ''.join(_n(s).split())
            key = _ns2(old_disp)
            # tìm ranh giới phường CŨ user ghi
            stated_entry = None
            for e in bounds:
                if e['k'] == key or e['k2'] == key:
                    dist_hint = _ns2(item['candidates'][0].get('dist', ''))
                    if not dist_hint or e['d'] in dist_hint or _ns2(e['dist']) in dist_hint:
                        stated_entry = e
                        break
            if not stated_entry:
                continue
            vb = _polys_bbox([stated_entry['g']['coordinates']]
                             if stated_entry['g']['type'] == 'Polygon'
                             else stated_entry['g']['coordinates'], pad=0.12)
            pt = None
            for q_geo in _build_geo_queries(q, res.get('province', '')):
                pt = await _geocode_vn(q_geo, viewbox=vb)
                if pt:
                    break
            if not pt:
                continue
            lon, lat = pt
            if _pip_geom(lon, lat, stated_entry['g']):
                continue  # đường đúng là nằm trong phường cũ đã ghi → OK
            # đường nằm phường cũ KHÁC → tìm phường cũ thực + suy phường mới đúng
            actual = next((e for e in bounds if _pip_geom(lon, lat, e['g'])), None)
            if not actual:
                continue
            derived = _derive_new_from_old(
                pc2, _ward_core(actual['name']), _n(actual.get('dist', '')))
            if derived and _n(derived['new']) != _n(item['candidates'][0]['new']):
                item['stated_wrong'] = old_disp
                item['candidates'] = [{'new': derived['new'], 'dist': derived.get('dist', ''),
                                       'prov': pc2, 'old_disp': actual['name']}]
                item['correct_ward'] = derived['new']
                item['geo'] = True
                item['geo_actual_old'] = {'name': actual['name'], 'dist': actual['dist']}
            break  # chỉ verify 1 item chính, tránh spam geocode

    # Tổng hợp mức độ chắc chắn
    confident = [it for it in res['results'] if it['confident']]
    res['status'] = (
        'confident' if len(confident) == len(res['results']) and res['results']
        else 'ambiguous' if res['results']
        else 'no_old_ward'
    )
    res['map_link'] = 'https://sapnhap.bando.com.vn/'
    return res


# ══════════════════════════════════════════════════════════════════
# TELEGRAM BRIDGE
# ══════════════════════════════════════════════════════════════════

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")
TG_API       = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
_tg_reply_queue: list = []

class TgSendRequest(BaseModel):
    message: str

class TgReplyRequest(BaseModel):
    text: str
    source: Optional[str] = "hermes"

@app.post("/api/telegram/send")
async def tg_send(req: TgSendRequest):
    import httpx
    msg = f"[TOOL] {req.message}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{TG_API}/sendMessage", json={"chat_id": TG_CHAT_ID, "text": msg})
    with get_conn() as conn:
        conn.execute("INSERT INTO chat_history (role, content) VALUES ('user', ?)", (req.message[:2000],))
    return {"ok": r.status_code == 200}

@app.post("/api/telegram/reply")
async def tg_reply(req: TgReplyRequest):
    _tg_reply_queue.append({"text": req.text})
    if len(_tg_reply_queue) > 20:
        _tg_reply_queue.pop(0)
    with get_conn() as conn:
        conn.execute("INSERT INTO chat_history (role, content) VALUES ('assistant', ?)", (req.text[:4000],))
    return {"ok": True}

@app.get("/api/telegram/poll")
async def tg_poll():
    replies = list(_tg_reply_queue)
    _tg_reply_queue.clear()
    return {"messages": replies}


@app.get("/api/debug")
async def api_debug():
    import traceback as tb
    result = {}
    try:
        from database import DB_URL
        result["db_url_set"] = bool(DB_URL)
        result["db_url_prefix"] = (DB_URL[:40] + "...") if DB_URL else "MISSING"
    except Exception:
        result["db_import_error"] = tb.format_exc()
        return result
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) as c FROM sellers").fetchone()
            result["sellers_count"] = row[0] if row else "no row"
    except Exception:
        result["db_query_error"] = tb.format_exc()
        return result
    result["status"] = "OK"
    return result
