"""
analyzer.py – Phân tích lỗi API GHN từ knowledge base
"""
import re
import json
from database import get_conn


def analyze_error(error_text: str) -> dict:
    """
    Nhận một chuỗi lỗi → tìm trong knowledge base → trả kết quả phân tích.
    """
    error_text_clean = error_text.strip()
    if not error_text_clean:
        return _no_match("Vui lòng nhập thông báo lỗi.")

    with get_conn() as conn:
        # ── 1. Tìm exact / partial match trong error_knowledge ────
        rows = conn.execute("""
            SELECT * FROM error_knowledge ORDER BY hit_count DESC
        """).fetchall()

    best = None
    best_score = 0

    for row in rows:
        row = dict(row)
        pattern = row["error_msg"].lower()
        target  = error_text_clean.lower()

        # Tính điểm match
        score = 0
        if pattern in target:
            score = len(pattern)  # longer match = better
        elif target in pattern:
            score = len(target) // 2
        else:
            # Token overlap
            p_tokens = set(re.split(r"[\s'\".,]", pattern))
            t_tokens = set(re.split(r"[\s'\".,]", target))
            overlap  = p_tokens & t_tokens - {"", "for", "the", "a", "is", "in", "of", "to"}
            score    = len(overlap) * 3

        if score > best_score:
            best_score = score
            best = row

    if best and best_score >= 3:
        # Tăng hit_count
        with get_conn() as conn:
            conn.execute(
                "UPDATE error_knowledge SET hit_count = hit_count + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (best["id"],)
            )

        # Lấy thông tin endpoint liên quan
        endpoint_info = None
        if best.get("endpoint"):
            with get_conn() as conn:
                ep = conn.execute(
                    "SELECT * FROM ghn_endpoints WHERE url LIKE ? OR name LIKE ?",
                    (f"%{best['endpoint'].split('/')[-1]}%", f"%{best['endpoint']}%")
                ).fetchone()
                if ep:
                    endpoint_info = dict(ep)

        return {
            "found":       True,
            "confidence":  min(100, best_score * 5),
            "error_msg":   error_text_clean,
            "matched_pattern": best["error_msg"],
            "endpoint":    best.get("endpoint", "Không xác định"),
            "root_cause":  best.get("root_cause") or "Chưa có phân tích",
            "solution":    best.get("solution") or "Chưa có giải pháp",
            "code_wrong":  best.get("code_wrong"),
            "code_right":  best.get("code_right"),
            "source":      best.get("source", "manual"),
            "endpoint_info": endpoint_info,
            "tip":         _generate_tip(best),
        }

    # ── 2. Không tìm thấy – thử heuristic ────────────────────────
    heuristic = _heuristic_analyze(error_text_clean)
    if heuristic:
        return heuristic

    # ── 3. Không match gì – lưu lại để học sau ───────────────────
    _save_unknown_error(error_text_clean)
    return _no_match(
        error_text_clean,
        hint="Lỗi này chưa có trong knowledge base. Đã lưu lại để bổ sung sau."
    )


def _heuristic_analyze(text: str) -> dict | None:
    """Phân tích heuristic dựa theo pattern phổ biến."""
    t = text.lower()

    if "401" in t or "unauthorized" in t or "unauthenticated" in t:
        return _make_result(
            error_msg=text,
            endpoint="Tất cả endpoint",
            root_cause="Token không hợp lệ, hết hạn, hoặc sai định dạng header",
            solution='Kiểm tra header: {"Token": "YOUR_TOKEN", "ShopId": YOUR_SHOP_ID}',
            code_wrong='headers = {"Authorization": "Bearer token"}  # SAI format GHN',
            code_right='headers = {"Token": "your_ghn_token", "ShopId": 123456}  # ĐÚNG',
            confidence=80,
        )

    if "404" in t or "not found" in t:
        return _make_result(
            error_msg=text,
            endpoint="Không xác định",
            root_cause="Endpoint không tồn tại hoặc OrderCode/ShopId không đúng",
            solution="Kiểm tra lại URL endpoint và các ID trong request",
            confidence=60,
        )

    if "timeout" in t or "connection" in t:
        return _make_result(
            error_msg=text,
            endpoint="Tất cả endpoint",
            root_cause="Kết nối đến GHN API bị timeout hoặc lỗi mạng",
            solution="Kiểm tra kết nối internet, thử lại sau vài giây, hoặc tăng timeout",
            confidence=70,
        )

    if "required" in t or "missing" in t or "field" in t:
        # Trích field name
        field_match = re.search(r"['\"](\w+)['\"]", text)
        field_name  = field_match.group(1) if field_match else "field bắt buộc"
        return _make_result(
            error_msg=text,
            endpoint="Không xác định",
            root_cause=f"Thiếu trường bắt buộc: `{field_name}`",
            solution=f'Thêm trường `{field_name}` vào request body với giá trị hợp lệ',
            code_right=f'request_body["{field_name}"] = <giá_trị_hợp_lệ>',
            confidence=65,
        )

    if "500" in t or "internal server" in t:
        return _make_result(
            error_msg=text,
            endpoint="Không xác định",
            root_cause="Lỗi phía server GHN (Internal Server Error)",
            solution="Thử lại sau vài phút. Nếu vẫn lỗi, liên hệ GHN support.",
            confidence=50,
        )

    return None


def _make_result(error_msg, endpoint, root_cause, solution,
                 code_wrong=None, code_right=None, confidence=70) -> dict:
    return {
        "found":           True,
        "confidence":      confidence,
        "error_msg":       error_msg,
        "matched_pattern": "Heuristic",
        "endpoint":        endpoint,
        "root_cause":      root_cause,
        "solution":        solution,
        "code_wrong":      code_wrong,
        "code_right":      code_right,
        "source":          "heuristic",
        "endpoint_info":   None,
        "tip":             None,
    }


def _generate_tip(row: dict) -> str | None:
    tips = []
    if row.get("endpoint") and "create" in row["endpoint"]:
        tips.append("💡 Luôn gọi available-services trước để lấy ServiceId hợp lệ.")
    if row.get("endpoint") and "ward" in str(row.get("error_msg", "")):
        tips.append("💡 Dùng API master-data/ward để lấy WardCode chính xác theo quận.")
    return " | ".join(tips) if tips else None


def _save_unknown_error(error_text: str):
    """Lưu lỗi chưa biết vào DB để review sau."""
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT id FROM error_knowledge WHERE error_msg = ?", (error_text[:500],)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO error_knowledge (error_msg, source, root_cause) VALUES (?, 'unknown', 'Chưa phân tích')",
                (error_text[:500],)
            )


def _no_match(error_text: str, hint: str = "") -> dict:
    return {
        "found":           False,
        "confidence":      0,
        "error_msg":       error_text,
        "matched_pattern": None,
        "endpoint":        None,
        "root_cause":      "Không tìm thấy trong knowledge base",
        "solution":        hint or "Paste lỗi đầy đủ hơn, hoặc thêm vào knowledge base thủ công.",
        "code_wrong":      None,
        "code_right":      None,
        "source":          None,
        "endpoint_info":   None,
        "tip":             "💡 Thử gõ đúng message lỗi gốc từ response JSON của GHN API.",
    }


def chat_response(message: str) -> str:
    """
    Xử lý tin nhắn chat thông thường (không phải paste error).
    """
    m = message.lower().strip()

    # Câu hỏi về endpoint
    if any(k in m for k in ["endpoint", "api nào", "gọi gì", "url"]):
        with get_conn() as conn:
            eps = conn.execute("SELECT name, url, method FROM ghn_endpoints LIMIT 5").fetchall()
        lines = [f"• **{e['name']}** – `{e['method']} {e['url'].split('ghn.vn')[-1]}`" for e in eps]
        return "Các endpoint GHN chính:\n\n" + "\n".join(lines) + \
               "\n\nGõ `/endpoints` để xem tất cả, hoặc paste lỗi để tôi phân tích."

    if any(k in m for k in ["token", "header", "xác thực", "auth"]):
        return (
            "**Cách xác thực GHN API:**\n\n"
            "Thêm vào header mỗi request:\n"
            "```\n"
            "Token: YOUR_GHN_TOKEN\n"
            "ShopId: YOUR_SHOP_ID\n"
            "Content-Type: application/json\n"
            "```\n"
            "Token lấy ở: GHN Dashboard → Cài đặt → API"
        )

    if any(k in m for k in ["tạo đơn", "create order", "tao don"]):
        return (
            "**Tạo đơn GHN – required fields:**\n\n"
            "```json\n"
            "{\n"
            '  "PaymentTypeId": 2,\n'
            '  "Note": "Ghi chú",\n'
            '  "RequiredNote": "KHONGCHOXEMHANG",\n'
            '  "ToName": "Tên người nhận",\n'
            '  "ToPhone": "0xxxxxxxxx",\n'
            '  "ToAddress": "Số nhà, tên đường",\n'
            '  "ToWardCode": "20308",\n'
            '  "ToDistrictId": 1442,\n'
            '  "Weight": 200,\n'
            '  "Length": 10, "Width": 10, "Height": 10,\n'
            '  "ServiceTypeId": 2,\n'
            '  "Items": [{"name": "Tên SP", "quantity": 1, "weight": 200}]\n'
            "}\n"
            "```"
        )

    if any(k in m for k in ["tính phí", "fee", "phi ship", "phí ship"]):
        return (
            "**Tính phí ship GHN:**\n\n"
            "Endpoint: `GET /v2/shipping-order/fee`\n\n"
            "```json\n"
            "{\n"
            '  "ServiceId": 53321,\n'
            '  "InsuranceValue": 0,\n'
            '  "FromDistrictId": 1442,\n'
            '  "ToDistrictId": 1820,\n'
            '  "ToWardCode": "020408",\n'
            '  "Weight": 200\n'
            "}\n"
            "```"
        )

    if any(k in m for k in ["knowledge", "đã học", "lỗi biết", "base"]):
        with get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) as c FROM error_knowledge WHERE root_cause != 'Chưa phân tích'").fetchone()["c"]
            ep_count = conn.execute("SELECT COUNT(*) as c FROM ghn_endpoints").fetchone()["c"]
        return (
            f"📚 **Knowledge Base hiện có:**\n\n"
            f"• **{ep_count}** endpoint GHN đã học\n"
            f"• **{count}** lỗi + giải pháp đã biết\n\n"
            f"Paste thông báo lỗi bất kỳ để tôi tra cứu ngay!"
        )

    if any(k in m for k in ["xin chào", "hello", "hi", "chào"]):
        return (
            "👋 Xin chào! Tôi là **GHN API Assistant**.\n\n"
            "Tôi có thể giúp bạn:\n"
            "• 🔍 **Phân tích lỗi** – paste error message vào\n"
            "• 📖 **Tra endpoint** – hỏi về API GHN\n"
            "• 🧪 **Test API** – tab API Test bên cạnh\n"
            "• 📚 **Xem knowledge base** – tab KB\n\n"
            "Hỏi gì đi!"
        )

    # Default
    return (
        "Tôi chưa hiểu câu hỏi này. Bạn có thể:\n\n"
        "• **Paste lỗi trực tiếp** để tôi phân tích\n"
        "• Hỏi về **endpoint cụ thể** (vd: *cách tạo đơn*, *tính phí ship*)\n"
        "• Gõ `/endpoints` để xem danh sách API\n"
        "• Gõ `/kb` để xem knowledge base"
    )
