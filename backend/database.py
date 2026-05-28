"""
database.py – SQLite setup cho GHN POS Multi-Seller
"""
import sqlite3
import os
import secrets

DB_PATH = os.path.join(os.path.dirname(__file__), "knowledge.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        -- ══════════════════════════════════════════
        -- MULTI-SELLER TABLES
        -- ══════════════════════════════════════════

        -- Bảng nhà bán hàng (Sellers)
        CREATE TABLE IF NOT EXISTS sellers (
            id          TEXT PRIMARY KEY,           -- e.g. SEL001
            name        TEXT NOT NULL,              -- Tên shop
            owner_name  TEXT NOT NULL,              -- Tên chủ shop
            phone       TEXT NOT NULL,
            email       TEXT,
            login_key   TEXT NOT NULL UNIQUE,       -- Key đăng nhập qua link
            ghn_token   TEXT,                       -- GHN API Token (được mã hoá bởi app)
            ghn_shop_id INTEGER DEFAULT 0,          -- GHN Shop ID
            status      TEXT DEFAULT 'active',      -- active / inactive
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        -- Bảng đơn hàng (Orders) – lưu đơn thật gửi GHN
        CREATE TABLE IF NOT EXISTS orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id       TEXT NOT NULL,
            order_code      TEXT UNIQUE,            -- Mã vận đơn GHN (null nếu chưa gửi)
            client_code     TEXT,                   -- Mã đơn nội bộ
            status          TEXT DEFAULT 'pending', -- pending/pickup/in_transit/returning/delivered/cancelled
            receiver_name   TEXT,
            receiver_phone  TEXT,
            receiver_address TEXT,
            to_province_id  INTEGER,
            to_district_id  INTEGER,
            to_ward_code    TEXT,
            weight          INTEGER DEFAULT 200,    -- grams
            length          INTEGER DEFAULT 10,
            width           INTEGER DEFAULT 10,
            height          INTEGER DEFAULT 10,
            cod_amount      INTEGER DEFAULT 0,
            insurance_value INTEGER DEFAULT 0,
            shipping_fee    INTEGER DEFAULT 0,
            service_id      INTEGER,
            payment_type    INTEGER DEFAULT 2,      -- 1=shop trả, 2=khách trả
            note            TEXT,
            ghn_response    TEXT,                   -- JSON raw từ GHN
            print_token     TEXT,                   -- Token in tem GHN
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        -- Bảng tracking logs (webhook / manual sync)
        CREATE TABLE IF NOT EXISTS tracking_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code  TEXT NOT NULL,
            status      TEXT,
            description TEXT,
            location    TEXT,
            logged_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        -- ══════════════════════════════════════════
        -- AI ASSISTANT TABLES (giữ nguyên)
        -- ══════════════════════════════════════════

        CREATE TABLE IF NOT EXISTS ghn_endpoints (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            url         TEXT NOT NULL,
            method      TEXT NOT NULL DEFAULT 'POST',
            description TEXT,
            required_fields TEXT,
            sample_request  TEXT,
            sample_response TEXT,
            error_codes     TEXT,
            learned_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS error_knowledge (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            error_msg   TEXT NOT NULL,
            endpoint    TEXT,
            root_cause  TEXT,
            solution    TEXT,
            code_wrong  TEXT,
            code_right  TEXT,
            source      TEXT DEFAULT 'manual',
            hit_count   INTEGER DEFAULT 0,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS api_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id   TEXT,
            endpoint    TEXT NOT NULL,
            request_body TEXT,
            status_code INTEGER,
            response    TEXT,
            error_msg   TEXT,
            duration_ms INTEGER,
            logged_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS chat_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)
        
        # Migration: Add seller_id to api_logs if it doesn't exist
        try:
            conn.execute("ALTER TABLE api_logs ADD COLUMN seller_id TEXT")
        except sqlite3.OperationalError:
            pass # Column already exists

        # Migration: Add GHN Affiliate columns to sellers
        for col_def in [
            "ALTER TABLE sellers ADD COLUMN ghn_phone TEXT",          # SĐT đăng nhập GHN của seller
            "ALTER TABLE sellers ADD COLUMN ghn_connected INTEGER DEFAULT 0",  # 1 = đã kết nối affiliate
        ]:
            try:
                conn.execute(col_def)
            except sqlite3.OperationalError:
                pass

    print(f"[DB] Initialized at {DB_PATH}")


def seed_initial_knowledge():
    """Seed kiến thức GHN cơ bản nếu chưa có."""
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM error_knowledge").fetchone()[0]
        if count > 0:
            return

        initial_errors = [
            {
                "error_msg": "Field validation for 'ShiftID' failed",
                "endpoint":  "tenant_masterdata_get_shift_detail",
                "root_cause": "Payload thiếu trường ShiftID bắt buộc hoặc ShiftID bằng 0/null",
                "solution":  "Thêm ShiftID hợp lệ vào request body trước khi gọi API",
                "code_wrong": '{"Token": "your_token"}',
                "code_right": '{"Token": "your_token", "ShiftID": 123}',
                "source":    "ghn_api",
            },
            {
                "error_msg": "Unauthorized",
                "endpoint":  "all",
                "root_cause": "Token không hợp lệ hoặc đã hết hạn",
                "solution":  "Kiểm tra lại Token trong header. Đảm bảo dùng đúng token của shop.",
                "code_wrong": 'headers = {"Token": ""}',
                "code_right": 'headers = {"Token": "YOUR_VALID_TOKEN", "ShopId": 123}',
                "source":    "ghn_api",
            },
            {
                "error_msg": "ShopID not found",
                "endpoint":  "v2/shipping-order/create",
                "root_cause": "ShopId trong header không tồn tại hoặc không thuộc token này",
                "solution":  "Lấy ShopId đúng từ GHN dashboard, kiểm tra token có quyền với shop đó không",
                "code_wrong": 'headers = {"Token": "tkn", "ShopId": 0}',
                "code_right": 'headers = {"Token": "tkn", "ShopId": 5_000_000}',
                "source":    "ghn_api",
            },
            {
                "error_msg": "required_ward",
                "endpoint":  "v2/shipping-order/create",
                "root_cause": "Thiếu WardCode (mã phường) hoặc WardCode sai trong payload",
                "solution":  "Dùng API /master-data/ward để lấy WardCode đúng theo tỉnh/quận",
                "code_wrong": '{"ToWardCode": ""}',
                "code_right": '{"ToWardCode": "20308", "ToDistrictId": 1442}',
                "source":    "ghn_api",
            },
            {
                "error_msg": "The district does not support this service",
                "endpoint":  "v2/shipping-order/create",
                "root_cause": "Dịch vụ vận chuyển không hỗ trợ tuyến đường này",
                "solution":  "Gọi API /v2/shipping-order/available-services để lấy danh sách dịch vụ khả dụng",
                "code_wrong": '{"ServiceTypeId": 2}',
                "code_right": "# Gọi available-services trước, rồi lấy ServiceId phù hợp",
                "source":    "ghn_api",
            },
            {
                "error_msg": "cod amount exceeds",
                "endpoint":  "v2/shipping-order/create",
                "root_cause": "Số tiền COD vượt quá giới hạn cho phép (thường 10.000.000đ)",
                "solution":  "Giảm CodAmount xuống dưới 10.000.000 hoặc liên hệ GHN để nâng hạn mức",
                "code_wrong": '{"CodAmount": 50000000}',
                "code_right": '{"CodAmount": 8000000}  # max 10 triệu',
                "source":    "ghn_api",
            },
            {
                "error_msg": "weight exceed",
                "endpoint":  "v2/shipping-order/create",
                "root_cause": "Khối lượng đơn hàng (Weight) vượt quá giới hạn dịch vụ",
                "solution":  "Giảm Weight, hoặc chuyển sang dịch vụ hàng cồng kềnh (ServiceTypeId=5)",
                "code_wrong": '{"Weight": 60000}',
                "code_right": '{"Weight": 20000, "ServiceTypeId": 5}',
                "source":    "ghn_api",
            },
        ]

        for e in initial_errors:
            conn.execute("""
                INSERT INTO error_knowledge (error_msg, endpoint, root_cause, solution, code_wrong, code_right, source)
                VALUES (:error_msg, :endpoint, :root_cause, :solution, :code_wrong, :code_right, :source)
            """, e)

    print("[DB] Seeded initial GHN knowledge")


def seed_demo_sellers():
    """Seed 3 nhà bán hàng demo nếu chưa có."""
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM sellers").fetchone()[0]
        if count > 0:
            return

        demos = [
            {
                "id": "SEL001",
                "name": "Shop Thời Trang Minh Anh",
                "owner_name": "Nguyễn Minh Anh",
                "phone": "0901234567",
                "email": "minhanh@gmail.com",
                "login_key": "MA2024xT9k",
                "ghn_token": "",
                "ghn_shop_id": 0,
                "status": "active",
            },
            {
                "id": "SEL002",
                "name": "Điện Tử Thành Công",
                "owner_name": "Trần Thành Công",
                "phone": "0912345678",
                "email": "thanh.cong@gmail.com",
                "login_key": "TC2024pW3m",
                "ghn_token": "",
                "ghn_shop_id": 0,
                "status": "active",
            },
            {
                "id": "SEL003",
                "name": "Mỹ Phẩm Ngọc Hân",
                "owner_name": "Lê Ngọc Hân",
                "phone": "0923456789",
                "email": "ngochan@gmail.com",
                "login_key": "NH2024qR7v",
                "ghn_token": "",
                "ghn_shop_id": 0,
                "status": "inactive",
            },
        ]

        for s in demos:
            conn.execute("""
                INSERT OR IGNORE INTO sellers (id, name, owner_name, phone, email, login_key, ghn_token, ghn_shop_id, status)
                VALUES (:id, :name, :owner_name, :phone, :email, :login_key, :ghn_token, :ghn_shop_id, :status)
            """, s)

    print("[DB] Seeded demo sellers")
