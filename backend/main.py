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
    get_available_services, get_shipping_fee, get_station,
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
    # GỬI/NHẬN TẠI ĐIỂM (bưu cục GHN) — lấy id từ /api/ghn/stations
    pick_station_id: Optional[int] = None      # shop mang hàng ĐẾN điểm gửi
    deliver_station_id: Optional[int] = None   # khách NHẬN tại điểm

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
    # Gửi/nhận tại ĐIỂM (bưu cục). >0 mới gửi; 0/None = giao/lấy tận nơi như thường.
    if req.pick_station_id:
        ghn_payload["pick_station_id"] = req.pick_station_id
    if req.deliver_station_id:
        ghn_payload["deliver_station_id"] = req.deliver_station_id
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


@app.get("/api/ghn/stations")
async def api_stations(seller_id: str, district_id: int, ward_code: str = "",
                       offset: int = 0, limit: int = 100):
    """Danh sách ĐIỂM gửi/nhận GHN theo quận (kèm phường nếu có) — cho 'gửi tại
    điểm'. Trả gọn: [{station_id, name, address, phone}]."""
    token, shop_id = _get_seller_creds(seller_id)
    r = await get_station(token, shop_id, district_id, ward_code,
                          offset, limit, seller_id=seller_id)
    stations = []
    if r.get("ok"):
        for s in (r["data"].get("data") or []):
            stations.append({
                "station_id": s.get("stationId") or s.get("station_id") or s.get("id"),
                "name": s.get("name") or s.get("stationName") or "",
                "address": s.get("address") or s.get("fullAddress") or "",
                "phone": s.get("phone") or s.get("hotline") or "",
            })
    return {"ok": r.get("ok", False), "stations": stations,
            "message": (r.get("data") or {}).get("message") if isinstance(r.get("data"), dict) else None}


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


_QUERY_PHRASES = [
    'phường mới là gì', 'phường cũ là gì', 'xã mới là gì', 'xã cũ là gì',
    'địa chỉ mới là gì', 'địa chỉ cũ là gì', 'mới là gì', 'cũ là gì', 'là gì',
    'ra mới', 'ra cũ', 'về mới', 'về cũ', 'sau sáp nhập', 'trước sáp nhập',
    'check mới', 'check cũ', 'kiểm tra', 'thuộc phường nào', 'phường nào',
    'địa chỉ mới', 'địa chỉ cũ', 'phường mới', 'phường cũ', 'xã mới', 'xã cũ',
    'giờ là', 'bây giờ là', 'hiện tại là', 'đổi thành', 'chuyển thành',
]

def _fix_admin(s):
    """Sửa vài lỗi hiển thị tên đơn vị hành chính trong data:
    'Thịtrấn'→'Thị trấn' (thiếu space), 'Thủ Đô Hà Nội'→'Thành phố Hà Nội'."""
    if not s:
        return s
    import re as _r
    s = _r.sub(r'Th[iị]tr[aấ]n', 'Thị trấn', s)
    s = _r.sub(r'Th[aà]nhph[oố]', 'Thành phố', s)
    s = _r.sub(r'Th[iị]x[aã]', 'Thị xã', s)
    s = s.replace('Thủ Đô Hà Nội', 'Thành phố Hà Nội')
    return s


_WARD_DESC_RE = None
_CAP_CLASS = ('[A-ZĐÀÁẢÃẠĂẮẰẲẴẶÂẤẦẨẪẬÈÉẺẼẸÊẾỀỂỄỆÌÍỈĨỊÒÓỎÕỌÔỐỒỔỖỘƠỚỜỞỠỢ'
              'ÙÚỦŨỤƯỨỪỬỮỰỲÝỶỸỴ]')

def _clean_ward_list(items):
    """Làm sạch danh sách 'phường/xã CŨ' — data đôi khi lọt cả câu nghị quyết
    ('một phần diện tích TN, quy mô dân số của các phường Cửa Nam, Điện Biên...').
    Trích các tên Phường/Xã/Thị trấn thực, bỏ mảnh mô tả."""
    global _WARD_DESC_RE
    import re as _r
    if _WARD_DESC_RE is None:
        _WARD_DESC_RE = _r.compile(r'diện tích|dân số|phần còn lại|sau khi|sắp xếp|sáp nhập', _r.I)
    C = _CAP_CLASS
    pat = (r'(ph\S*ờng|xã|thị trấn)\s+((?:%s[\wÀ-ỹ]*(?:\s+%s?[\wÀ-ỹ0-9]*)*)'
           r'(?:\s*,\s*%s[\wÀ-ỹ]*(?:\s+%s?[\wÀ-ỹ0-9]*)*)*)') % (C, C, C, C)
    out = []
    def _add(nm, pre='Phường'):
        nm = _fix_admin(_r.sub(r'\s+', ' ', nm).strip(' ,.'))
        nm = _r.sub(r'\s*\(.*$', '', nm).strip()
        if not nm or _WARD_DESC_RE.search(nm):
            return
        if not _r.match(r'(Phường|Xã|Thị trấn)', nm):
            nm = pre + ' ' + nm
        if nm not in out:
            out.append(nm)
    for it in items or []:
        it = (it or '').strip()
        if not it:
            continue
        if not _WARD_DESC_RE.search(it) and len(it) <= 30:
            _add(it)
            continue
        for mm in _r.finditer(pat, it, _r.I):
            g1 = mm.group(1).lower()
            pre = 'Xã' if g1 == 'xã' else ('Thị trấn' if 'trấn' in g1 else 'Phường')
            for nm in mm.group(2).split(','):
                _add(nm.strip(), pre)
    return out


_OLD_BOUNDS_DIST_IDX: dict = {}

def _old_ward_dist(pc, ward_name):
    """District CŨ của 1 phường/xã cũ (tra GADM old_bounds theo tên)."""
    import re as _r
    if pc not in _OLD_BOUNDS_DIST_IDX:
        idx = {}
        for e in _load_old_bounds(pc):
            k2 = e.get('k2')
            if k2:
                idx.setdefault(k2, e.get('dist', ''))
        _OLD_BOUNDS_DIST_IDX[pc] = idx
    core = _r.sub(r'^\s*(phường|phuong|xã|xa|thị trấn|thi tran|thị xã|thi xa)\s+',
                  '', _n(ward_name))
    key = ''.join(core.split())
    return _OLD_BOUNDS_DIST_IDX[pc].get(key, '')


_DISTRICT_PROV = None

def _load_district_prov():
    """Bảng district(cũ) → tỉnh(cũ), dựng từ dữ liệu hành chính pre-2025 (63 tỉnh)."""
    global _DISTRICT_PROV
    if _DISTRICT_PROV is None:
        try:
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'district_old_province.json')
            with open(p, encoding='utf-8') as f:
                _DISTRICT_PROV = json.load(f)
        except Exception:
            _DISTRICT_PROV = {'district_prov': {}, 'prov_disp': {}}
    return _DISTRICT_PROV


def _old_province_of_district(pc, dist_name):
    """Tỉnh CŨ của 1 quận/huyện cũ. Khử trùng tên (Châu Thành ở 10 tỉnh) bằng
    province_aliases: chỉ giữ tỉnh cũ đã sáp nhập vào tỉnh mới hiện tại (pc)."""
    dp = _load_district_prov()
    import re as _r
    key = _r.sub(r'^\s*(quan|huyen|thi xa|thanh pho|tp|tx|thi tran|tt)\s+', '',
                 _n(dist_name)).strip()
    cands = dp.get('district_prov', {}).get(key, [])
    if not cands:
        return ''
    alias = _load_resolver().get('province_aliases', {})
    valid = {pc} | {k for k, v in alias.items() if v == pc}
    hit = [c for c in cands if c in valid] or (cands if len(cands) == 1 else [])
    if len(hit) == 1:
        return dp.get('prov_disp', {}).get(hit[0], '')
    return ''


def _enrich_old_wards(pc, names):
    """Gắn quận + tỉnh CŨ vào mỗi phường/xã cũ → 'Phường An Sinh, Kinh Môn, Hải Dương'.
    Ưu tiên tra thẳng ward trong dữ liệu pre-2025 (đủ cả huyện+tỉnh, khử trùng tên
    bằng province_aliases); thiếu thì fallback district từ GADM old_bounds."""
    import re as _r
    dp = _load_district_prov()
    ward_dp = dp.get('ward_dp', {})
    prov_disp = dp.get('prov_disp', {})
    alias = _load_resolver().get('province_aliases', {})
    valid = {pc} | {k for k, v in alias.items() if v == pc}
    out = []
    for nm in names or []:
        wk = _r.sub(r'^\s*(phuong|phuong|xa|thi tran|tt)\s+', '', _n(nm)).strip()
        cands = ward_dp.get(wk, [])
        hit = [c for c in cands if c[1] in valid] or (cands if len(cands) == 1 else [])
        if len(hit) == 1:
            ddisp, pk = hit[0]
            ddisp = ('Quận ' + ddisp) if ddisp.isdigit() else _fix_admin(ddisp)
            tinh = prov_disp.get(pk, '')
            out.append(', '.join([nm, ddisp] + ([tinh] if tinh else [])))
            continue
        dist = _fix_admin(_old_ward_dist(pc, nm))
        if dist:
            tinh = _old_province_of_district(pc, dist)
            out.append(', '.join([nm, dist] + ([tinh] if tinh else [])))
        else:
            out.append(nm)
    return out


# Viết tắt tên ĐƯỜNG phổ biến (chỉ những cái rõ ràng, không mập mờ) — bung ra để
# VietMap geocode được (nó không hiểu 'XVNT'). Khớp cả hoa/thường, nguyên token.
_STREET_ABBR = {
    'XVNT': 'Xô Viết Nghệ Tĩnh',
    'CMT8': 'Cách Mạng Tháng 8',
    'CMTT': 'Cách Mạng Tháng Tám',
    'NKKN': 'Nam Kỳ Khởi Nghĩa',
    'NTMK': 'Nguyễn Thị Minh Khai',
    'DBP': 'Điện Biên Phủ',
    'PVD': 'Phạm Văn Đồng',
    'NVL': 'Nguyễn Văn Linh',
    'HTP': 'Huỳnh Tấn Phát',
    'LVS': 'Lê Văn Sỹ',
}


def _expand_abbr(s):
    """Mở rộng viết tắt hành chính để parse được phường/quận cũ:
      P./P  → Phường   Q./Q → Quận   H./H → Huyện   X./X → Xã   TT.→Thị trấn  TX.→Thị xã
    Cả dạng số (P.8, Q12) lẫn dạng tên (P.Trung Mỹ Tây, Q.Gò Vấp, H.Bình Chánh).
    Chạy TRƯỚC khi đổi '.'→',' (khỏi vỡ 'P.Tây Thạnh'). NB: TP. để yên (là tỉnh/TP)."""
    import re as _r
    NL = r'(?<![a-zA-ZÀ-ỹ0-9])'      # ranh trái: không dính chữ/số (tránh TP., initials)
    # 0) Viết tắt TÊN ĐƯỜNG phổ biến (VietMap không hiểu 'XVNT' → phải bung ra để geo)
    for _ab, _full in _STREET_ABBR.items():
        s = _r.sub(NL + _r.escape(_ab) + r'(?![a-zA-ZÀ-ỹ0-9])', _full, s, flags=_r.IGNORECASE)
    # 1) Dạng CÓ DẤU CHẤM + tên hoặc số (vd 'P.Trung Mỹ Tây', 'Q.Gò Vấp', 'H.Bình Chánh')
    s = _r.sub(NL + r'[Tt][Tt]\.\s*', 'Thị trấn ', s)
    s = _r.sub(NL + r'[Tt][Xx]\.\s*', 'Thị xã ', s)
    s = _r.sub(NL + r'[Pp]\.\s*(?=[0-9A-Za-zÀ-ỹ])', 'Phường ', s)
    s = _r.sub(NL + r'[Qq]\.\s*(?=[0-9A-Za-zÀ-ỹ])', 'Quận ', s)
    s = _r.sub(NL + r'[Hh]\.\s*(?=[0-9A-Za-zÀ-ỹ])', 'Huyện ', s)
    s = _r.sub(NL + r'[Xx]\.\s*(?=[A-Za-zÀ-ỹ])', 'Xã ', s)
    # 2) Dạng KHÔNG DẤU CHẤM + SỐ (vd 'q8', 'p 4', 'Q12') — số mới đủ rõ để chắc chắn
    s = _r.sub(NL + r'[Qq]\s*0*(\d{1,2})(?![0-9])', lambda m: 'Quận ' + m.group(1), s)
    s = _r.sub(NL + r'[Pp]\s*0*(\d{1,2})(?![0-9])', lambda m: 'Phường ' + m.group(1), s)
    return s


def _clean_query(q):
    """Bỏ cụm hỏi ý định ('phường mới là gì'...) + coi . ; : như dấu phẩy
    (ngăn cách phần địa chỉ) để không lẫn vào tên phường / hỏng detect tỉnh."""
    import re as _r
    import unicodedata as _u
    # Chuẩn hóa NFC: input NFD (một số client/OS gửi "phường" dạng tổ hợp) sẽ KHÔNG
    # khớp literal 'phường' trong regex → mất phường cũ. NFC hóa để mọi client đều đúng.
    s = _u.normalize('NFC', q or '')
    s = _expand_abbr(s)
    s = _r.sub(r'[.;:]+', ',', s)
    for ph in _QUERY_PHRASES:
        s = _r.sub(_r.escape(ph), ' ', s, flags=_r.IGNORECASE)
    s = _r.sub(r'\s+', ' ', s).strip(' ,')
    return s


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
    không nằm trong ngoặc '(... cũ)'. Chỉ nhận tên ≥2 chữ, ≥6 ký tự, khớp nguyên cụm.
    BỎ QUA tên trùng quận/huyện/TP/tỉnh (Hạ Long, Ngô Quyền, Đồng Nai...) trừ khi
    text ghi rõ prefix xã/phường trước tên đó."""
    data = _load_resolver()
    bucket = data.get('resolver', {}).get(pc, {})
    dmap = _district_prov_map()
    prov_names = set(data.get('provinces', {}).keys()) | set(data.get('province_aliases', {}).keys())
    # danh sách quận/huyện cũ đầy đủ (GADM) của tỉnh này
    global _old_districts
    try:
        _old_districts
    except NameError:
        _old_districts = None
    if _old_districts is None:
        base = os.path.dirname(os.path.abspath(__file__))
        try:
            with open(os.path.join(base, 'old_districts.json'), encoding='utf-8') as f:
                _old_districts = json.load(f)
        except Exception:
            _old_districts = {}
    dist_set = set(_old_districts.get(pc, []))
    found = []
    for wc in bucket.keys():
        if len(wc) < 6 or ' ' not in wc:
            continue
        if not _re.search(r'(?:^|\s)' + _re.escape(wc) + r'(?:$|\s|,)', text_norm):
            continue
        # tên trùng quận/huyện hoặc tỉnh → chỉ nhận khi có prefix hành chính cấp xã
        if wc in dmap or wc in prov_names or wc in dist_set:
            if not _re.search(r'(?:xa|phuong|thi tran)\s+' + _re.escape(wc), text_norm):
                continue
        # cụm đứng ngay sau SỐ NHÀ = tên ĐƯỜNG ('15 Trần Phú') → không phải xã cũ
        if _re.search(r'\d[\w/-]*\s+' + _re.escape(wc) + r'(?:$|\s|,)', text_norm) \
           and not _re.search(r'(?:xa|phuong|thi tran)\s+' + _re.escape(wc), text_norm):
            continue
        found.append(wc)
    # ưu tiên cụm dài nhất, bỏ cụm con
    found.sort(key=len, reverse=True)
    result = []
    for wc in found:
        if not any(wc != other and wc in other for other in result):
            result.append(wc)
    return result


def _resolve_offline(text, province_hint=None):
    text = _clean_query(text)
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
        # Lọc theo DẤU: bucket gộp phường ĐỒNG ÂM khác dấu (Tân Quy Q7 ↔ Tân Quý
        # Tân Phú, cùng ward-core 'tan quy', dist rỗng nên quận không lọc được).
        # Nếu địa chỉ ghi ĐÚNG DẤU khớp 'old' của một nhóm → giữ nhóm đó.
        if len(cands) > 1:
            import unicodedata as _u2
            _txt_tone = ' '.join(_u2.normalize('NFC', text.lower()).split())
            def _old_tone(c):
                o = _u2.normalize('NFC', (c.get('old_disp') or '').lower())
                o = _re.sub(r'^\s*(phường|phuong|xã|xa|thị trấn|thị xã)\s+', '', o)
                return ' '.join(o.split())
            tone_hit = [c for c in cands if _old_tone(c) and _old_tone(c) in _txt_tone]
            if tone_hit and len(tone_hit) < len(cands):
                cands = tone_hit
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
VIETMAP_API_KEY = os.environ.get("VIETMAP_API_KEY", "")

# Độ chính xác của lần geocode gần nhất: True = tới SỐ NHÀ (đủ tin để phủ quyết
# phường user ghi), False = chỉ tới tuyến đường (chỉ được gợi ý cảnh báo).
_last_geocode_precise = False
# Ward mà geocoder trả kèm (VietMap có sẵn phường) — dùng làm gợi ý phụ.
_last_geocode_ward = None
# TẤT CẢ phường VietMap gán cho địa chỉ (nhiều feature cùng số nhà ở phường khác
# nhau) — nếu phường user ghi nằm trong đây thì user ĐÚNG, không được báo sai.
_last_geocode_wards = []

_prov_center_cache: dict = {}

def _province_center(prov_core):
    """Tâm (lon,lat) của tỉnh MỚI, tính từ bbox old_bounds — cache. Dùng làm
    focus point cho VietMap để nó xếp hạng feature đúng tỉnh lên đầu bất kể
    IP server (VietMap rank theo IP → Vercel nhận feature khác máy local)."""
    if prov_core in _prov_center_cache:
        return _prov_center_cache[prov_core]
    center = None
    lons, lats = [], []
    for e in _load_old_bounds(prov_core):
        g = e.get('g', {})
        polys = [g.get('coordinates', [])] if g.get('type') == 'Polygon' else g.get('coordinates', [])
        for poly in polys:
            for ring in poly[:1]:
                for pt in ring[::20]:  # thưa cho nhanh
                    lons.append(pt[0]); lats.append(pt[1])
    if lons:
        center = ((min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2)
    _prov_center_cache[prov_core] = center
    return center


async def _geocode_vn(q, viewbox=None, prov_core=None):
    """Geocode: Goong.io (data VN, số nhà hẻm chính xác) → Nominatim → Photon.
    viewbox=(lonmin,latmin,lonmax,latmax): giới hạn vùng tìm (tránh trùng tên
    đường ở thành phố khác trong cùng tỉnh mới, VD Kon Tum vs Quảng Ngãi)."""
    import httpx
    global _last_geocode_precise, _last_geocode_ward, _last_geocode_wards
    _last_geocode_precise = False
    _last_geocode_ward = None
    _last_geocode_wards = []
    # 0) VietMap — geocoder VN, ĐỊNH VỊ ĐÚNG SỐ NHÀ (kể cả hẻm 266/10)
    if VIETMAP_API_KEY:
        try:
            _params = {'api-version': '1.1', 'apikey': VIETMAP_API_KEY, 'text': q}
            # focus point: tâm viewbox nếu có, không thì tâm tỉnh → ép VietMap
            # ưu tiên feature đúng vùng (khắc phục rank theo IP trên Vercel).
            _focus = None
            if viewbox:
                _focus = ((viewbox[0] + viewbox[2]) / 2, (viewbox[1] + viewbox[3]) / 2)
            elif prov_core:
                _focus = _province_center(prov_core)
            if _focus:
                _params['focus.point.lon'] = _focus[0]
                _params['focus.point.lat'] = _focus[1]
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get('https://maps.vietmap.vn/api/search',
                                     params=_params)
            feats = (r.json().get('data') or {}).get('features') or []
            # số nhà đầu chuỗi truy vấn (vd "30", "266/10", "405/15", "K154")
            m_lead = _re.match(r'\s*([0-9]+(?:[/\-][0-9a-zA-Z]+)*)', q)
            lead = (m_lead.group(1).lower() if m_lead else '')
            pal = _load_resolver().get('province_aliases', {})

            def _ok_prov(f):
                # lọc theo TỈNH: tránh VietMap khớp mờ số nhà+đường ở tỉnh khác
                # (vd "87 Nguyễn Sinh Sắc, Gia Lai" nó trả TP Vinh, Nghệ An).
                # VietMap dùng tên tỉnh CŨ → chuẩn hóa qua province_aliases
                # (Bình Định→Gia Lai) rồi mới so với tỉnh mới đã nhập.
                if not prov_core:
                    return True
                rg = _n(f.get('properties', {}).get('region', ''))
                rg = rg.replace('tinh ', '').replace('thanh pho ', '').strip()
                rg_new = pal.get(rg, rg)
                return (not rg) or (prov_core in rg_new or rg_new in prov_core
                                    or prov_core in rg or rg in prov_core)

            def _ok_vb(lon, lat):
                return (not viewbox or (viewbox[0] <= lon <= viewbox[2]
                                        and viewbox[1] <= lat <= viewbox[3]))

            def _is_house(f):
                prop = f.get('properties', {})
                hn = (prop.get('housenumber') or '').lower().strip()
                nm = (prop.get('name') or '').lower()
                return bool(lead) and (hn == lead or nm.startswith(lead + ' ') or nm == lead)

            # Ưu tiên feature ĐÚNG SỐ NHÀ (vd nhà 87 ở Bình Định) hơn feature
            # khớp mờ tuyến đường cùng tỉnh (VietMap xếp hạng đôi khi lộn) —
            # rồi mới tới feature bất kỳ hợp tỉnh+viewbox.
            # Gom MỌI phường VietMap gán cho địa chỉ này (feature khớp số nhà, đúng
            # tỉnh) — để biết phường user ghi có khớp 1 phương án nào của VietMap ko.
            _last_geocode_wards = [
                f.get('properties', {}).get('locality')
                for f in feats
                if _ok_prov(f) and (not lead or _is_house(f))
                and f.get('properties', {}).get('locality')
            ]
            pick = None
            for want_house in (True, False):
                for f in feats:
                    coords = f.get('geometry', {}).get('coordinates') or []
                    if len(coords) < 2:
                        continue
                    lon, lat = float(coords[0]), float(coords[1])
                    if not _ok_prov(f) or not _ok_vb(lon, lat):
                        continue
                    if want_house and not _is_house(f):
                        continue
                    pick = (f, lon, lat)
                    break
                if pick:
                    break
            if pick:
                f, lon, lat = pick
                _last_geocode_precise = _is_house(f)
                _last_geocode_ward = f.get('properties', {}).get('locality') or None
                return lon, lat
        except Exception as e:
            print(f"[geocode-vietmap] {e}", flush=True)
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
                        _last_geocode_precise = True
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
            _last_geocode_precise = js[0].get('addresstype') in ('house', 'building')
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
        # Quận ghi KHÔNG prefix (vd 'Đống Đa') → lấy đoạn ngay TRƯỚC tỉnh làm quận
        # (giúp VietMap khử trùng tên đường: '229 Tây Sơn, Đống Đa, HN' ≠ Tây Sơn nơi khác)
        if not dist_seg and len(segs) >= 3:
            dist_seg = segs[-2]
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


_new_bounds_cache: dict = {}
_new_disp_cache: dict = {}

def _load_new_bounds(pc):
    """Ranh giới phường MỚI (sau 7/2025, tải từ sapnhap.bando.com.vn) theo tỉnh —
    để geocode điểm → phường MỚI trực tiếp (xử lý phường bị xé theo diện tích)."""
    if pc in _new_bounds_cache:
        return _new_bounds_cache[pc]
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, 'new_bounds', pc.replace(' ', '_') + '.json')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    _new_bounds_cache[pc] = data
    return data


def _new_ward_disp(pc, core):
    """Tên hiển thị (có dấu) của phường mới từ core chuẩn hóa."""
    if pc not in _new_disp_cache:
        m = {}
        for p in _load_new_wards_v3():
            if _strip_prov(p.get('tentinhmoi', '')) == pc:
                for w in p.get('phuongxa', []):
                    m[_strip_ward(w.get('tenphuongxa', ''))] = w.get('tenphuongxa', '')
                break
        _new_disp_cache[pc] = m
    import re as _r
    ck = _r.sub(r'^\s*(phuong|xa|thi tran|dac khu|tt)\s+', '', core).strip()
    return _new_disp_cache[pc].get(ck) or _fix_admin(core.title())


def _new_ward_at_point(pc, lon, lat):
    """Điểm (lon,lat) → tên phường MỚI chứa nó (PIP ranh giới mới local). None nếu
    không có dữ liệu / không trúng. Đây là cách CHÍNH XÁC nhất cho phường bị xé."""
    for core, polys in _load_new_bounds(pc).items():
        if _point_in_polys(lon, lat, polys):
            return _new_ward_disp(pc, core)
    return None


def _reverse_lookup(text, province_hint=None):
    """Tra NGƯỢC: địa chỉ/tên phường MỚI → các phường/xã CŨ đã gộp thành nó."""
    text = _clean_query(text)
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
    cands = bucket.get(wc, [])
    # Khi biết quận (từ geo) → ưu tiên khớp quận THẬT trước; entry quận rỗng chỉ
    # là fallback (tránh "Phường 6" rỗng của Cao Lãnh nuốt "Phường 6" Mỹ Tho).
    if dist_norm:
        for c in cands:
            cd = _n(c.get('dist', ''))
            if cd and (dist_norm in cd or cd in dist_norm):
                return c
    for c in cands:
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


# ══════════════════════════════════════════════════════════════════
# CHUYỂN ĐỔI ĐỊA CHỈ cho BOT: hàng loạt / theo tọa độ
# ══════════════════════════════════════════════════════════════════
class ConvertBatchReq(BaseModel):
    addresses: List[str]
    mode: Optional[str] = "new"   # "new": cũ→mới (mặc định) | "old": mới→cũ


@app.post("/api/convert-batch")
async def api_convert_batch(req: ConvertBatchReq):
    """Chuyển hàng loạt: mỗi dòng 1 địa chỉ cũ dạng free-text → địa chỉ mới."""
    out = []
    _mode = (req.mode or "new").lower()
    for raw in req.addresses[:200]:
        raw = (raw or '').strip()
        if not raw:
            continue
        # ── Chiều NGƯỢC (mới→cũ) cho cả lô ──
        if _mode == "old":
            try:
                rev = await api_address_reverse(raw, None, True)
            except Exception:
                rev = {}
            ro = rev.get("resolved_old")
            m0 = (rev.get("matches") or [None])[0]
            if ro:
                ans = f"{ro['name']}" + (f", {ro['dist']}" if ro.get('dist') else "")
                out.append({'input': raw, 'status': 'ok', 'new_ward': ans,
                            'province': rev.get('province', ''), 'old': '', 'reverse': True})
            elif m0 and m0.get("old_list"):
                out.append({'input': raw, 'status': 'ambiguous',
                            'candidates': m0["old_list"][:6], 'province': rev.get('province', '')})
            else:
                out.append({'input': raw, 'status': 'not_found', 'province': rev.get('province', '')})
            continue
        # Dùng CHUNG pipeline với tra lẻ (có geo-verify/fallback) để batch thông
        # minh y hệt: parse viết tắt, sửa phường sai theo đường, v.v. Geo chỉ chạy
        # khi cần (dòng có đường mà offline chưa chắc) nên chi phí có kiểm soát.
        try:
            res = await api_address_resolve(raw, None, True)
        except Exception:
            res = _resolve_offline(raw)
        best = None
        for it in res.get('results', []):
            if it.get('confident') and it.get('candidates'):
                best = it
                break
        if not best:
            for it in res.get('results', []):
                if it.get('candidates'):
                    best = it
                    break
        if best and best.get('confident'):
            c = best['candidates'][0]
            out.append({'input': raw, 'status': 'ok',
                        'new_ward': c['new'], 'province': res.get('province', ''),
                        'old': c.get('old_disp') or best.get('old', '')})
        elif best:
            out.append({'input': raw, 'status': 'ambiguous',
                        'candidates': [c['new'] for c in best['candidates'][:5]],
                        'province': res.get('province', '')})
        else:
            out.append({'input': raw, 'status': 'not_found',
                        'province': res.get('province', '')})
    for o in out:
        if o.get('new_ward'):
            o['new_ward'] = _fix_admin(o['new_ward'])
        if o.get('province'):
            o['province'] = _fix_admin(o['province'])
        if o.get('candidates'):
            o['candidates'] = [_fix_admin(x) for x in o['candidates']]
    return {'results': out}


_NEW_WARDS_V3_CACHE = None

def _load_new_wards_v3():
    """Danh mục phường/xã MỚI 2025 (nguồn Chính phủ) có mã ward_id_v2 GHN dùng cho
    v3. GHN public API chưa mở endpoint phường mới (trả null) → đây là nguồn chuẩn."""
    global _NEW_WARDS_V3_CACHE
    if _NEW_WARDS_V3_CACHE is None:
        try:
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'new_wards_2025.json')
            with open(p, encoding='utf-8') as f:
                _NEW_WARDS_V3_CACHE = json.load(f)
        except Exception:
            _NEW_WARDS_V3_CACHE = []
    return _NEW_WARDS_V3_CACHE


def _strip_prov(s):
    s = _n(s)
    for pre in ('thanh pho ', 'tinh ', 'tp '):
        if s.startswith(pre):
            s = s[len(pre):]
    return s.strip()


def _strip_ward(s):
    s = _n(s)
    import re as _r
    return _r.sub(r'^(phuong|xa|thi tran|tt)\s+', '', s).strip()


@app.get("/api/ward-v3-id")
async def api_ward_v3_id(province: str = "", ward: str = ""):
    """Tra mã ward_id_v2 (GHN v3) của phường MỚI theo tỉnh — để tạo đơn
    is_new_to_address=true. Verify phường có thật trong danh mục Chính phủ."""
    prov_n = _strip_prov(province)
    ward_n = _strip_ward(ward)
    if not ward_n:
        return {"found": False, "reason": "thiếu tên phường"}
    for p in _load_new_wards_v3():
        pn = _strip_prov(p.get('tentinhmoi', ''))
        if not (pn == prov_n or (prov_n and (prov_n in pn or pn in prov_n))):
            continue
        for w in p.get('phuongxa', []):
            if _strip_ward(w.get('tenphuongxa', '')) == ward_n:
                return {"found": True, "ward_id_v2": w.get('maphuongxa'),
                        "ward_name": w.get('tenphuongxa'),
                        "province": p.get('tentinhmoi')}
        return {"found": False, "reason": "phường không có trong tỉnh này",
                "province": p.get('tentinhmoi')}
    return {"found": False, "reason": "không thấy tỉnh"}


@app.get("/api/convert-coords")
async def api_convert_coords(lat: float, lng: float):
    """Chuyển tọa độ → địa chỉ mới. PIP vào ranh giới phường CŨ (GADM, local)
    rồi suy phường MỚI."""
    base = os.path.dirname(os.path.abspath(__file__))
    bdir = os.path.join(base, 'old_bounds')
    try:
        files = [f[:-5] for f in os.listdir(bdir) if f.endswith('.json')]
    except Exception:
        files = []
    for pcfile in files:
        pc = pcfile.replace('_', ' ')
        for e in _load_old_bounds(pc):
            if _pip_geom(lng, lat, e['g']):
                c = _derive_new_from_old(pc, _ward_core(e['name']), _n(e.get('dist', '')))
                provs = _load_resolver().get('provinces', {})
                return {
                    'ok': True,
                    'lat': lat, 'lng': lng,
                    'old': {'ward': e['name'], 'district': e.get('dist', '')},
                    'new': {'ward': c['new'] if c else None,
                            'province': provs.get(pc, '')},
                }
    return {'ok': False, 'error': 'Tọa độ không thuộc phường/xã nào (ngoài VN?)'}


@app.get("/api/address-reverse")
async def api_address_reverse(q: str, province: Optional[str] = None, live: bool = True):
    """
    Tra ngược địa chỉ MỚI (sau 7/2025) → phường/xã CŨ.
    Khi có tên đường → geocode + point-in-polygon với ranh giới CŨ (GADM)
    để chỉ ra CHÍNH XÁC phường cũ, không chỉ liệt kê thành phần.
    """
    q = _clean_query(q)
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
                    pt = await _geocode_vn(q_geo, viewbox=viewbox, prov_core=res.get('province_core'))
                if not pt:
                    pt = await _geocode_vn(q_geo, prov_core=res.get('province_core'))
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
                    if _last_geocode_precise:
                        res['stated_wrong_new'] = stated
                        res['correct_new'] = derived
                    else:
                        # geocode chỉ tới tuyến đường → gợi ý, không khẳng định SAI
                        res['geo_hint_new'] = derived
    if res.get('province'):
        res['province'] = _fix_admin(res['province'])
    _ro = res.get('resolved_old')
    if _ro and _ro.get('name'):
        _ro['name'] = _fix_admin(_ro['name'])
        _ro['dist'] = _fix_admin(_ro.get('dist', ''))
    for _m in res.get('matches', []):
        _m['new'] = _fix_admin(_m.get('new', ''))
        if _m.get('prov'):
            _m['prov'] = _fix_admin(_m['prov'])
        if _m.get('old_list'):
            _pc_old = res.get('province_core') or _detect_province(q, province)
            _m['old_list'] = _enrich_old_wards(_pc_old, _clean_ward_list(_m['old_list']))
    return res


@app.get("/api/poi")
async def api_poi(q: str, province: str = ""):
    """Tra ĐỊA DANH/POI (chợ, bệnh viện, trường, mall...) → danh sách nơi ứng viên
    kèm phường/quận/tỉnh (từ VietMap). Tính năng riêng, KHÔNG đụng resolve/reverse."""
    import httpx
    if not VIETMAP_API_KEY:
        return {"pois": []}
    pc = (_prov_core(province) if province else None) or _detect_province(_clean_query(q))
    params = {'api-version': '1.1', 'apikey': VIETMAP_API_KEY, 'text': q}
    if pc:
        c = _province_center(pc)
        if c:
            params['focus.point.lon'] = c[0]
            params['focus.point.lat'] = c[1]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get('https://maps.vietmap.vn/api/search', params=params)
        feats = (r.json().get('data') or {}).get('features') or []
    except Exception as e:
        print(f"[poi] {e}", flush=True)
        feats = []
    pal = _load_resolver().get('province_aliases', {})
    def _rgcore(rg):
        rg = _n(rg).replace('tinh ', '').replace('thanh pho ', '').strip()
        return pal.get(rg, rg)
    out, seen = [], set()
    for f in feats:
        p = f.get('properties', {})
        co = f.get('geometry', {}).get('coordinates') or []
        if len(co) < 2:
            continue
        rgc = _rgcore(p.get('region', ''))
        # lọc theo tỉnh (nếu xác định được) — tránh trùng tên địa danh tỉnh khác
        if pc and rgc and not (pc in rgc or rgc in pc):
            continue
        key = (p.get('name'), p.get('locality'), p.get('county'))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": _fix_admin(p.get('name') or ''),
            "ward": _fix_admin(p.get('locality') or ''),
            "district": _fix_admin(p.get('county') or ''),
            "province": _fix_admin(p.get('region') or ''),
            "lat": co[1], "lon": co[0],
        })
        if len(out) >= 6:
            break
    return {"pois": out}


_NEW_WARD_SET_CACHE: dict = {}

def _new_ward_set(pc):
    """Tập ward-core MỚI (sau 7/2025) của tỉnh — để biết 1 phường là MỚI hay chưa."""
    if pc in _NEW_WARD_SET_CACHE:
        return _NEW_WARD_SET_CACHE[pc]
    s = set()
    for p in _load_new_wards_v3():
        if _strip_prov(p.get('tentinhmoi', '')) == pc:
            for w in p.get('phuongxa', []):
                core = _strip_ward(w.get('tenphuongxa', ''))
                if core:
                    s.add(core)
            break
    _NEW_WARD_SET_CACHE[pc] = s
    return s


def _ward_in_province(pc, wards):
    """Rule 1 — anchor cấp cao nhất (tỉnh): phường input có THỰC SỰ thuộc tỉnh
    đã ghi không? Check top-down qua tập phường MỚI + phường CŨ (pre-2025,
    khử tỉnh qua province_aliases). None = không đủ dữ liệu để kết luận."""
    if not pc or not wards:
        return None
    dp = _load_district_prov()
    ward_dp = dp.get('ward_dp', {})
    alias = _load_resolver().get('province_aliases', {})
    valid = {pc} | {k for k, v in alias.items() if v == pc}
    newset = _new_ward_set(pc)
    for o in wards:
        wk = _strip_ward(o)
        if wk in newset:
            return True
        if any(ent[1] in valid for ent in ward_dp.get(wk, [])):
            return True
    return False


@app.get("/api/classify")
async def api_classify(q: str):
    """Phân loại địa chỉ đầu vào: phường ghi là CŨ hay MỚI (offline, không geo)
    + phường có thuộc tỉnh đã ghi không (Rule 1). Dùng để tự chọn chiều tra."""
    qc = _clean_query(q)
    pc = _detect_province(qc)
    olds = _extract_old_wards(qc)
    resolver = _load_resolver().get('resolver', {})
    bucket = resolver.get(pc, {}) if pc else {}
    is_old = any(_ward_core(o) in bucket for o in olds)
    newset = _new_ward_set(pc) if pc else set()
    is_new = any(_strip_ward(o) in newset for o in olds)
    # Did-you-mean: nếu phường ghi KHÔNG khớp cũ lẫn mới → gợi ý gần đúng
    sug = _suggest_wards(pc, olds) if (pc and olds and not is_old and not is_new) else []
    return {"province_core": pc or "", "wards": olds,
            "is_old": is_old, "is_new": is_new,
            "ward_in_prov": _ward_in_province(pc, olds), "suggestions": sug}


@app.get("/api/address-resolve")
async def api_address_resolve(q: str, province: Optional[str] = None, live: bool = True):
    """
    Tra phường MỚI chính xác từ địa chỉ có ghi phường CŨ.
    - Offline trước; nếu không chắc và live=true → xác minh qua sapnhap.bando.com.vn.
    """
    q = _clean_query(q)
    res = _resolve_offline(q, province)

    # Fallback khi offline KHÔNG có ứng viên nào.
    if live and res.get('province_core'):
        pcL = res['province_core']
        for item in res['results']:
            if item['candidates']:
                continue
            # (1) Nếu tên là 1 THỊ XÃ/nhóm phường cùng gốc (An Nhơn → An Nhơn
            #     Bắc/Đông/Nam/Tây...) → liệt kê nhóm đó cho người chọn.
            grp = _confusable_group(pcL, item['old'])
            if len(grp) >= 2:
                for g in grp:
                    item['candidates'].append({'new': g, 'dist': '', 'prov': pcL, 'source': 'group'})
                item['confident'] = False
                continue
            # (2) còn lại: live khớp lỏng theo tên (KHÔNG tự tin)
            live_cands = await _resolve_live(pcL, _ward_core(item['old']))
            for lc in live_cands:
                item['candidates'].append({'new': lc, 'dist': '', 'prov': pcL, 'source': 'live'})
            item['confident'] = False

    # Địa chỉ có ĐƯỜNG/số nhà không? Không có thì geocode vô nghĩa (chỉ ra
    # điểm giữa quận) → BỎ QUA geo để khỏi sinh gợi ý rác.
    _seg0 = _n((q or '').split(',')[0])
    _admin0 = _seg0.startswith(('phuong ', 'xa ', 'quan ', 'huyen ', 'thi tran ',
                                'thi xa ', 'tinh ', 'tp ', 'thanh pho '))
    q_has_street = (not _admin0) and bool(_re.search(r'\d|duong|hem|kiet|so ', _seg0))

    # GEO disambiguation: khi còn 2-6 ứng viên → geocode địa chỉ (OSM) rồi
    # tra tọa độ vào ranh giới phường (polygon từ sapnhap.bando.com.vn).
    if live and q_has_street and res.get('province_core'):
        need_geo = [it for it in res['results']
                    if not it['confident'] and 2 <= len(it['candidates']) <= 6]
        if need_geo:
            # Neo vùng tìm vào bbox các phường CŨ cùng thị xã/quận đã ghi
            # (vd 'An Nhơn' → gom mọi ward old dist='An Nhơn') → focus VietMap
            # đúng khu, tránh nó trả tuyến đường trùng tên tỉnh khác.
            vb_need = None
            _oldn = _n(need_geo[0]['old'])
            _lo, _la = [], []
            for e in _load_old_bounds(res['province_core']):
                ed, en = _n(e.get('dist', '')), _n(e.get('name', ''))
                # khớp CHẶT: đúng quận/thị xã cũ (ed==_oldn) hoặc đúng tên phường cũ
                # (en==... / ward-core trùng) — tránh gom ward khác trùng chữ.
                if _oldn and (ed == _oldn or en == _oldn
                              or _ward_core(e.get('name', '')) == _oldn):
                    g = e.get('g', {})
                    polys = ([g.get('coordinates', [])] if g.get('type') == 'Polygon'
                             else g.get('coordinates', []))
                    for poly in polys:
                        for ring in poly[:1]:
                            for p in ring[::10]:
                                _lo.append(p[0]); _la.append(p[1])
            if _lo:
                vb_need = (min(_lo) - 0.05, min(_la) - 0.05, max(_lo) + 0.05, max(_la) + 0.05)
            pt = None
            for q_geo in _build_geo_queries(q, res.get('province', '')):
                pt = await _geocode_vn(q_geo, viewbox=vb_need,
                                       prov_core=res.get('province_core'))
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
                    else:
                        # hits=0 (điểm ngoài mọi ứng viên — 'phường cũ' trích được
                        # có thể là tên QUẬN, vd 'Tân Phú') HOẶC hits>=2 (thị xã tách
                        # nhiều, biên chồng — vd An Nhơn). Cả hai: tra thẳng ranh giới
                        # phường CŨ (local) tại điểm → suy phường mới đúng.
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
                                # chỉ khẳng định khi geocode tới số nhà;
                                # OSM (tuyến đường) → để 'chưa chắc' cho thành thật
                                item['confident'] = bool(_last_geocode_precise)
                                item['correct_ward'] = derived['new'] if _last_geocode_precise else None
                                item['geo'] = True

    # GEO VERIFY chiều xuôi: kể cả khi ĐÃ chắc theo phường cũ user ghi,
    # kiểm chứng vị trí đường có thật sự nằm trong phường cũ đó không
    # (VD ghi P27 Bình Thạnh nhưng đường XVNT nằm P25 → phải sửa).
    if live and q_has_street and res.get('province_core'):
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
            # Neo viewbox theo CẢ QUẬN (mọi phường cũ cùng dist), KHÔNG chỉ phường
            # user ghi — vì phường đó có thể SAI (đang đi verify). Neo hẹp vào 1
            # phường sai sẽ loại mất điểm đúng ở phường khác trong quận.
            # (VD "374 XVNT, Phường 13" → điểm đúng ở Phường 25 bị loại nếu neo P13)
            _distk = _ns2(stated_entry.get('dist', ''))
            _lo, _la = [], []
            if _distk:
                for e in bounds:
                    if _ns2(e.get('dist', '')) == _distk:
                        g = e['g']
                        polys = ([g['coordinates']] if g['type'] == 'Polygon'
                                 else g['coordinates'])
                        for poly in polys:
                            for ring in poly[:1]:
                                for p in ring[::10]:
                                    _lo.append(p[0]); _la.append(p[1])
            if _lo:
                vb = (min(_lo) - 0.05, min(_la) - 0.05, max(_lo) + 0.05, max(_la) + 0.05)
            else:
                vb = _polys_bbox([stated_entry['g']['coordinates']]
                                 if stated_entry['g']['type'] == 'Polygon'
                                 else stated_entry['g']['coordinates'], pad=0.12)
            pt = None
            for q_geo in _build_geo_queries(q, res.get('province', '')):
                pt = await _geocode_vn(q_geo, viewbox=vb, prov_core=res.get('province_core'))
                if pt:
                    break
            if not pt:
                continue
            lon, lat = pt
            # (0) Nếu VietMap có BẤT KỲ feature cùng địa chỉ khớp phường user ghi
            #     → user ĐÚNG (địa chỉ này thực sự thuộc phường đó theo VietMap),
            #     tuyệt đối KHÔNG báo sai. (VD "94 Lê Văn Việt, Hiệp Phú": VietMap
            #     có feature Hiệp Phú lẫn Long Thạnh Mỹ → phải tin Hiệp Phú user ghi)
            if any(_ward_core(w) == _ward_core(old_disp) for w in (_last_geocode_wards or [])):
                continue
            # (1) VietMap locality = phường CŨ mà CHÍNH VietMap gán cho điểm này
            #     → đáng tin nhất. Nếu == phường user ghi → user ĐÚNG, KHÔNG báo sai.
            #     (ranh giới GADM hay lệch ở mép đường nên KHÔNG dựa vào PIP để phủ nhận)
            vm_ward = _last_geocode_ward if _last_geocode_precise else None
            if vm_ward and _ward_core(vm_ward) == _ward_core(old_disp):
                continue  # VietMap xác nhận đúng phường user ghi
            if _pip_geom(lon, lat, stated_entry['g']):
                continue  # điểm nằm trong ranh giới phường user ghi → OK
            # (2) Xác định phường CŨ THỰC: ƯU TIÊN VietMap locality; GADM PIP chỉ phụ.
            apip = next((e for e in bounds if _pip_geom(lon, lat, e['g'])), None)
            act_name = act_dist = None
            via_vietmap = False
            if vm_ward:
                act_name = vm_ward
                act_dist = apip.get('dist', '') if apip else stated_entry.get('dist', '')
                via_vietmap = True
            elif apip:
                act_name, act_dist = apip['name'], apip.get('dist', '')
            if not act_name:
                continue
            if _ward_core(act_name) == _ward_core(old_disp):
                continue  # phường thực == phường user ghi → user đúng
            derived = _derive_new_from_old(
                pc2, _ward_core(act_name), _n(act_dist))
            if derived and _n(derived['new']) != _n(item['candidates'][0]['new']):
                # CHỈ phủ quyết (báo SAI) khi VietMap locality xác nhận phường KHÁC
                # (đáng tin). Nếu chỉ có GADM PIP → GỢI Ý, không dám báo sai vì
                # ranh giới GADM hay lệch → tránh báo sai địa chỉ ĐÚNG.
                if via_vietmap:
                    item['stated_wrong'] = old_disp
                    item['candidates'] = [{'new': derived['new'], 'dist': derived.get('dist', ''),
                                           'prov': pc2, 'old_disp': act_name}]
                    item['correct_ward'] = derived['new']
                    item['geo'] = True
                    item['geo_actual_old'] = {'name': act_name, 'dist': act_dist}
                else:
                    item['geo_hint'] = {'new': derived['new'],
                                        'old': act_name, 'dist': act_dist}
            break  # chỉ verify 1 item chính, tránh spam geocode

    # FORWARD GEO FALLBACK: nhập phường MỚI/sai + có TÊN ĐƯỜNG mà tra tên không
    # ra kết quả tin cậy → geocode đường, PIP ranh giới CŨ → suy phường mới đúng.
    # (VD: "298 Nguyễn Văn Linh, Xã Đông Sơn (mới), Quảng Ngãi" → Trương Quang Trọng)
    _has_good = any(it.get('confident') and it.get('candidates') and it.get('correct_ward')
                    for it in res['results'])
    if live and q_has_street and res.get('province_core') and not _has_good:
        pc3 = res['province_core']
        bounds3 = _load_old_bounds(pc3)
        if bounds3:
            # Neo vùng tìm vào ranh giới phường MỚI mày ghi (tránh trùng tên
            # đường ở thành phố khác cùng tỉnh — VD Quảng Ngãi gộp Kon Tum)
            vb3 = None
            wm3 = _load_resolver().get('ward_malk', {}).get(pc3, {})
            _stated = [_ward_core(o) for o in res.get('old_wards', [])]
            for k, malk in wm3.items():
                kc = k[k.find(' ') + 1:] if ' ' in k else k   # bỏ 'phuong/xa'
                if any(sc and (kc == sc or k == sc) for sc in _stated):
                    _polys = await _ward_polygon(malk)
                    if _polys:
                        vb3 = _polys_bbox(_polys, pad=0.12)
                        break
            pt = None
            for q_geo in _build_geo_queries(q, res.get('province', '')):
                if vb3:
                    pt = await _geocode_vn(q_geo, viewbox=vb3, prov_core=res.get('province_core'))
                # vb3 chỉ neo 1 phường-con của thị xã tách nhiều (vd An Nhơn) →
                # số nhà có thể nằm phường-con khác, ngoài vb3. prov_core đã chặn
                # nhầm tỉnh nên thử lại KHÔNG viewbox, PIP old_bounds sẽ chọn đúng.
                if not pt:
                    pt = await _geocode_vn(q_geo, prov_core=res.get('province_core'))
                if pt:
                    break
            if pt:
                lon, lat = pt
                # ƯU TIÊN: PIP ranh giới phường MỚI → ra THẲNG phường mới (chính xác
                # nhất, xử lý được cả phường CŨ bị xé theo diện tích như Ngã Tư Sở).
                nw_pt = _new_ward_at_point(pc3, lon, lat)
                if nw_pt:
                    res['results'] = [{
                        'old': '', 'candidates': [{'new': nw_pt, 'dist': '', 'prov': pc3,
                                                   'old_disp': ''}],
                        'confident': True, 'correct_ward': nw_pt, 'geo': True,
                        'from_street': True,
                    }]
                else:
                    # Không có ranh giới mới → suy từ phường CŨ: PIP old_bounds, hụt
                    # thì dùng VietMap locality; vẫn không map được → báo geo_old_only.
                    apip3 = next((e for e in bounds3 if _pip_geom(lon, lat, e['g'])), None)
                    if apip3:
                        a_name3, a_dist3 = apip3['name'], apip3.get('dist', '')
                    elif _last_geocode_ward:
                        a_name3, a_dist3 = _last_geocode_ward, ''
                    else:
                        a_name3 = a_dist3 = None
                    if a_name3:
                        d3 = _derive_new_from_old(pc3, _ward_core(a_name3), _n(a_dist3))
                        if d3:
                            res['results'] = [{
                                'old': a_name3,
                                'candidates': [{'new': d3['new'], 'dist': d3.get('dist', ''),
                                                'prov': pc3, 'old_disp': a_name3}],
                                'confident': True, 'correct_ward': d3['new'], 'geo': True,
                                'geo_actual_old': {'name': a_name3, 'dist': a_dist3},
                                'from_street': True,
                            }]
                        else:
                            res['geo_old_only'] = {'name': _fix_admin(a_name3),
                                                   'dist': _fix_admin(a_dist3)}

    # Tổng hợp mức độ chắc chắn
    confident = [it for it in res['results'] if it['confident']]
    res['status'] = (
        'confident' if len(confident) == len(res['results']) and res['results']
        else 'ambiguous' if res['results']
        else 'no_old_ward'
    )
    res['map_link'] = 'https://sapnhap.bando.com.vn/'
    if res.get('province'):
        res['province'] = _fix_admin(res['province'])
    for _it in res.get('results', []):
        for _c in _it.get('candidates', []):
            if _c.get('new'):
                _c['new'] = _fix_admin(_c['new'])
    return res


_PROV_NAMES_CACHE: dict = {}

def _prov_ward_names(pc):
    """Tên phường (mới + cũ) của tỉnh — để gợi ý 'did-you-mean' khi user gõ sai."""
    if pc in _PROV_NAMES_CACHE:
        return _PROV_NAMES_CACHE[pc]
    names = {}   # core chuẩn hóa -> tên hiển thị
    for p in _load_new_wards_v3():
        if _strip_prov(p.get('tentinhmoi', '')) == pc:
            for w in p.get('phuongxa', []):
                nm = w.get('tenphuongxa', '')
                if nm:
                    names[_strip_ward(nm)] = nm
            break
    for wc, cands in _load_resolver().get('resolver', {}).get(pc, {}).items():
        for c in cands:
            od = c.get('old') or ''
            if od:
                names.setdefault(_strip_ward(od), od)
    _PROV_NAMES_CACHE[pc] = names
    return names


def _suggest_wards(pc, wards, n=3):
    """Gợi ý phường gần đúng (fuzzy) cho các ward user ghi sai."""
    import difflib
    if not pc or not wards:
        return []
    names = _prov_ward_names(pc)
    keys = list(names.keys())
    out = []
    for w in wards:
        wk = _strip_ward(w)
        if wk in names:   # đúng rồi thì không gợi ý
            continue
        for m in difflib.get_close_matches(wk, keys, n=n, cutoff=0.62):
            disp = names[m]
            if disp not in out:
                out.append(disp)
    return out[:5]


@app.get("/api/normalize")
async def api_normalize(q: str):
    """CHUẨN HÓA 1 PHÁT cho GHN: địa chỉ bất kỳ (lộn xộn/sai phường/thiếu ward)
    → 1 object sạch sẵn sàng tạo đơn: số nhà+đường, phường MỚI, tỉnh,
    ward_id_v2, độ tin cậy, cảnh báo."""
    res = await api_address_resolve(q, None, True)
    prov = _fix_admin(res.get('province', ''))
    results = res.get('results', [])
    best = (next((it for it in results if it.get('confident') and it.get('candidates')), None)
            or next((it for it in results if it.get('candidates')), None))
    # số nhà + đường (đoạn đầu nếu có street)
    _seg0 = _clean_query(q).split(',')[0].strip()
    _s0n = _n(_seg0)
    _admin0 = _s0n.startswith(('phuong ', 'xa ', 'quan ', 'huyen ', 'thi tran ',
                               'thi xa ', 'tinh ', 'tp ', 'thanh pho '))
    detail = _seg0 if ((not _admin0) and _re.search(r'\d|duong|hem|kiet|so ', _s0n)) else ''
    warnings = []
    if not best or not best.get('candidates'):
        cls = await api_classify(q)
        if res.get('geo_old_only'):
            warnings.append("Phường cũ tìm được nhưng CHƯA có ánh xạ mới — tra tay")
        if cls.get('ward_in_prov') is False:
            warnings.append("Phường/xã có thể KHÔNG thuộc tỉnh đã ghi")
        if not warnings:
            warnings.append("Chưa xác định được phường — thêm số nhà + tên đường")
        _sug = _suggest_wards(res.get('province_core', ''), cls.get('wards') or [])
        return {"ok": False, "input": q, "detail": detail, "new_ward": None,
                "province": prov, "ward_id_v2": None, "full_address": None,
                "confidence": "none", "warnings": warnings,
                "suggestions": _sug, "geo_old": res.get('geo_old_only')}
    c = best['candidates'][0]
    new_ward = c['new']
    _wid = await api_ward_v3_id(province=res.get('province', ''), ward=new_ward)
    ward_id_v2 = _wid.get('ward_id_v2') if _wid.get('found') else None
    if best.get('geo') or best.get('from_street'):
        conf = 'geo'
    elif best.get('confident'):
        conf = 'high'
    else:
        conf = 'ambiguous'
    if best.get('stated_wrong'):
        warnings.append(f"Phường ghi \"{best['stated_wrong']}\" SAI → đúng là {new_ward}")
    full = ', '.join(x for x in (detail, new_ward, prov) if x)
    return {"ok": True, "input": q, "detail": detail, "new_ward": new_ward,
            "province": prov, "ward_id_v2": ward_id_v2, "full_address": full,
            "confidence": conf, "warnings": warnings,
            "candidates": ([x['new'] for x in best['candidates'][:5]]
                           if conf == 'ambiguous' else None)}


class NormalizeBatchReq(BaseModel):
    addresses: List[str]


@app.post("/api/normalize-batch")
async def api_normalize_batch(req: NormalizeBatchReq):
    """Chuẩn hóa HÀNG LOẠT cho GHN: nhận list địa chỉ → mỗi cái ra object đầy đủ
    (phường mới + ward_id_v2 + confidence). Dùng cho POS xử lý file/lô lớn."""
    out = []
    for a in req.addresses[:300]:
        a = (a or '').strip()
        if not a:
            continue
        try:
            out.append(await api_normalize(a))
        except Exception as e:
            out.append({"ok": False, "input": a, "confidence": "error",
                        "warnings": [str(e)]})
    ok = sum(1 for r in out if r.get('ok'))
    return {"total": len(out), "ok": ok, "failed": len(out) - ok, "results": out}


@app.post("/api/normalize-csv")
async def api_normalize_csv(payload: dict):
    """Chuẩn hóa từ CSV thô: {'csv': '...', 'col': 0}. Trả CSV kết quả
    (input, phường mới, tỉnh, ward_id_v2, confidence, cảnh báo)."""
    import csv as _csv
    import io as _io
    text = payload.get('csv', '') or ''
    col = int(payload.get('col', 0))
    rows = list(_csv.reader(_io.StringIO(text)))
    addrs = [(r[col].strip() if len(r) > col else '') for r in rows]
    res = await api_normalize_batch(NormalizeBatchReq(addresses=[a for a in addrs if a]))
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(['input', 'phuong_moi', 'tinh', 'ward_id_v2', 'confidence', 'canh_bao'])
    for r in res['results']:
        w.writerow([r.get('input', ''), r.get('new_ward') or '', r.get('province') or '',
                    r.get('ward_id_v2') or '', r.get('confidence', ''),
                    ' | '.join(r.get('warnings') or [])])
    return {"total": res['total'], "ok": res['ok'], "failed": res['failed'],
            "csv": buf.getvalue()}


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
