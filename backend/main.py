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
    to_ward_code: str
    to_district_id: int
    to_province_id: Optional[int] = None
    # Địa chỉ hành chính mới (GHN v3 – áp dụng từ 01/07/2025)
    is_new_to_address: bool = False
    to_ward_id_v2: Optional[str] = None     # thay to_ward_code khi dùng địa chỉ mới
    to_address_v2: Optional[str] = None     # thay to_address khi dùng địa chỉ mới
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
    service_id: int
    from_district_id: int
    to_district_id: int
    to_ward_code: str
    weight: int
    length: int = 10
    width: int = 10
    height: int = 10
    insurance_value: int = 0

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
    """Tính phí vận chuyển thực từ GHN."""
    token, shop_id = _get_seller_creds(req.seller_id)
    result = await get_shipping_fee(
        token, shop_id,
        service_id=req.service_id,
        from_district_id=req.from_district_id,
        to_district_id=req.to_district_id,
        to_ward_code=req.to_ward_code,
        weight=req.weight,
        length=req.length,
        width=req.width,
        height=req.height,
        insurance_value=req.insurance_value,
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
        "to_ward_code": req.to_ward_code,
        "to_district_id": req.to_district_id,
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

    # Địa chỉ hành chính mới (v3)
    if req.is_new_to_address:
        ghn_payload["is_new_to_address"] = True
        if req.to_ward_id_v2:
            ghn_payload["to_ward_id_v2"] = req.to_ward_id_v2
        if req.to_address_v2:
            ghn_payload["to_address_v2"] = req.to_address_v2

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
        # Lưu lỗi GHN vào DB để debug
        ghn_msg = result["data"].get("message") or result.get("error") or "Lỗi không xác định"
        with get_conn() as conn:
            conn.execute(
                "UPDATE orders SET note=? WHERE client_code=? AND seller_id=?",
                (f"GHN_ERROR: {ghn_msg}", client_code, req.seller_id)
            )
        raise HTTPException(400, f"GHN từ chối đơn hàng: {ghn_msg}")


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


# ── DEBUG ENDPOINT (xoá sau khi fix xong) ──────────────────────────
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
