# -*- coding: utf-8 -*-
"""
Bộ test hồi quy resolver địa chỉ — chạy sau MỖI lần sửa backend.

    python eval_addresses.py               # test lên production
    python eval_addresses.py http://localhost:8000   # test local

Thêm case mới khi phát hiện lỗi: bổ sung vào CASES với đáp án đúng đã verify
(từ file Excel/GHN xác nhận/nghị quyết) — fix xong case cũ không bao giờ tái vỡ.
"""
import json, sys, time, unicodedata, urllib.parse, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://my-pos-tool.vercel.app"

def n(s):
    s = unicodedata.normalize('NFD', (s or '').lower())
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return ' '.join(s.replace('đ', 'd').split())

# (địa_chỉ, phường_mới_đúng, ghi_chú)   — expected=None nghĩa là PHẢI ambiguous
CASES = [
    # — chuỗi sáp nhập nhiều đợt —
    ("405/15 Xô Viết Nghệ Tĩnh, Phường 24, Bình Thạnh, HCM", "Phường Bình Thạnh", "P24 đợt1→P14 đợt2"),
    ("4/13a chánh hưng, p4, quận 8, hcm",                    "Phường Chánh Hưng", "viết tắt p4"),
    # P12 ghi sai nhưng OSM chỉ street-level → giữ mapping P12 (Phú Định) + geo_hint
    # Chánh Hưng. Khi có GOONG_API_KEY (số nhà) → kỳ vọng đổi thành Phường Chánh Hưng.
    ("4/13a chánh hưng, phường 12, quận 8, hồ chí minh",     "Phường Phú Định",   "P12 data-correct + hint Chánh Hưng (Goong sẽ nâng cấp)"),
    ("123 Lý Chính Thắng, Phường 7, Quận 3, HCM",            "Phường Xuân Hòa",   "P7 Q3 2020→Võ Thị Sáu"),
    ("45 Dương Bá Trạc, Phường 2, Quận 8, HCM",              "Phường Chánh Hưng", "Q8→Rạch Ông"),
    # — lọc quận cho phường số —
    ("80/12/35 Dương Quảng Hàm (P5 cũ), Phường Gò Vấp, HCM", "Phường An Nhơn",    "P5+Gò Vấp"),
    # — quét tên xã ngoài ngoặc + suy tỉnh —
    ("Thôn Tảo Dương Hồng Dương, xã thanh oai, hà nội",      "Xã Dân Hòa",        "Hồng Dương ngoài ngoặc"),
    # — tỉnh cũ + miền Tây —
    ("30 Trần Hưng Đạo, Phường 3, TP Mỹ Tho, Tiền Giang",    "Phường Mỹ Tho",     "tỉnh cũ Tiền Giang"),
    ("12 Nguyễn Huệ, Phường 5, TP Bến Tre",                  "Phường An Hội",     "đường trùng tỉnh Huế"),
    ("99 Lê Lợi, Phường 2, TP Trà Vinh",                     "Phường Trà Vinh",   "Trà Vinh phase1"),
    ("10 Hùng Vương, Phường 2, TP Tân An, Long An",          "Phường Long An",    "Tân An phase1"),
    # — geo disambiguation (phường cũ tách nhiều) —
    ("266/10 Lê trọng tấn, phường sơn kỳ, quận tân phú, HCM", "Phường Tây Thạnh", "Sơn Kỳ tách 3, geo chọn"),
    ("Kiệt 333 Phạm Văn Đồng (Phường Phú Thượng cũ), Thuận An, Huế", "Phường Mỹ Thượng", "Phú Thượng Huế"),
    ("374 Hoàng Văn Thái (Hoà Khánh Nam cũ), Đà Nẵng",       "Phường Hòa Khánh",  "Đà Nẵng"),
    ("K154 H02/4 Vũ Lăng (Phường Hòa Phát Cũ), Đà Nẵng",     "Phường An Khê",     "số nhà chữ K154"),
    # P14 Q10 tách đôi (Diên Hồng/Hòa Hưng) — geo chọn giữa ứng viên hợp lệ
    ("7/28 thành thái, Phường 14, quận 10, hcm",             "Phường Diên Hồng",  "geo chọn giữa 2 ứng viên tách đôi"),
    # Ghi phường MỚI/SAI + có tên đường → geo từ đường xác định đúng
    ("298 Nguyễn Văn Linh, Xã Đông Sơn, Quảng Ngãi",         "Phường Trương Quang Trọng", "ghi ward mới sai→geo đường"),
    # Thị xã An Nhơn tách nhiều phường + OSM thiếu đường → ambiguous, KHÔNG đoán bừa
    ("87 Nguyễn Sinh Sắc, Phường An Nhơn, Gia Lai",          None,                "thị xã An Nhơn tách nhiều→ambiguous"),
]

# reverse: (địa_chỉ, resolved_old kỳ vọng chứa, ghi chú)
REV_CASES = [
    ("7/28 thành thái, phường diên hồng, hồ chí minh", "phuong 14", "mới→cũ geo"),
    ("298 Nguyễn Văn Linh, Xã Đông Sơn, Quảng Ngãi",   "truong quang trong", "neo viewbox tránh Kon Tum"),
]

def get(path, q):
    url = f"{BASE}{path}?q={urllib.parse.quote(q)}"
    req = urllib.request.Request(url, headers={"User-Agent": "eval/1.0"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    passed = failed = 0
    print(f"== FORWARD ({BASE}) ==")
    for addr, expect, note in CASES:
        try:
            d = get("/api/address-resolve", addr)
            best = next((it for it in d.get("results", [])
                         if it.get("confident") and it.get("candidates")), None)
            got = best["candidates"][0]["new"] if best else None
            if expect is None:
                ok = got is None          # phải ambiguous
                shown = got or "(ambiguous ✓)"
            else:
                ok = got is not None and n(got) == n(expect)
                shown = got or f"(none/{d.get('status')})"
        except Exception as e:
            ok, shown = False, f"ERR {e}"
        mark = "PASS" if ok else "FAIL"
        passed += ok; failed += (not ok)
        print(f"[{mark}] {shown:<28} | kỳ vọng {expect or 'ambiguous':<22} | {note}")
        time.sleep(1.2)

    print("== REVERSE ==")
    for addr, expect_old, note in REV_CASES:
        try:
            d = get("/api/address-reverse", addr)
            ro = d.get("resolved_old") or {}
            got = ro.get("name", "")
            ok = expect_old in n(got)
        except Exception as e:
            ok, got = False, f"ERR {e}"
        mark = "PASS" if ok else "FAIL"
        passed += ok; failed += (not ok)
        print(f"[{mark}] {got:<28} | kỳ vọng chứa '{expect_old}' | {note}")
        time.sleep(1.2)

    print(f"\n== TỔNG: {passed} PASS / {failed} FAIL ==")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
