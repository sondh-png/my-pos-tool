"""
Sinh ward_resolver.json từ dữ liệu sáp nhập (p.co_dvhc của sapnhap.bando.com.vn).

Cấu trúc output:
{
  "provinces": { "<province_core>": "<Tên tỉnh hiển thị>" },
  "resolver": {
     "<province_core>": {
        "<old_ward_core>": [ {"new": "Phường X", "dist": "quận gò vấp"}, ... ]
     }
  }
}

- province_core / old_ward_core: đã bỏ dấu, lowercase, bỏ prefix hành chính.
- dist: gợi ý quận/huyện cũ (đã bỏ dấu) để lọc phường số trùng tên.

Chạy: python gen_resolver.py [đường_dẫn_dvhc.json]
Mặc định đọc dvhc.json cùng thư mục; nếu không có thì fetch live.
"""
import json, sys, os, re, unicodedata

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, 'dvhc.json')
OUT = os.path.join(BASE, 'ward_resolver.json')


def norm(s):
    s = unicodedata.normalize('NFD', (s or '').lower())
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = s.replace('đ', 'd')   # NFD không tách đ → phải thay tay
    return ' '.join(s.split())


def prov_core(s):
    n = norm(s)
    for p in ('thu do ', 'thanh pho ', 'tinh '):
        if n.startswith(p):
            n = n[len(p):]
    return n.strip()


_WARD_PREFIXES = ('phuong ', 'xa ', 'thi tran ', 'thi xa ', 'dac khu ')

def ward_core(w):
    n = norm(w)
    for p in _WARD_PREFIXES:
        if n.startswith(p):
            return n[len(p):].strip()
    return n


def load_data():
    if os.path.exists(SRC):
        with open(SRC, encoding='utf-8-sig') as f:
            return json.load(f)
    # fetch live
    import urllib.request
    req = urllib.request.Request('https://sapnhap.bando.com.vn/p.co_dvhc',
                                 method='POST',
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode('utf-8-sig')
    return json.loads(raw)


def main():
    data = load_data()
    cap_xa = [x for x in data if 'capxa' in x.get('malk', '')]
    cap_tinh = [x for x in data if 'captinh' in x.get('malk', '')]

    prov_by_ma = {x['ma']: x['ten'] for x in cap_tinh}         # magoc -> tên tỉnh hiển thị
    provinces = {prov_core(v): v for v in prov_by_ma.values()}

    # Alias tỉnh CŨ -> core tỉnh MỚI (parse từ truocsapnhap cấp tỉnh,
    # vd 'thành phố Cần Thơ, tỉnh Sóc Trăng và tỉnh Hậu Giang' -> soc trang: can tho)
    province_aliases = {}
    for x in cap_tinh:
        new_core = prov_core(x['ten'])
        truoc = x.get('truocsapnhap', '')
        if 'giu nguyen' in norm(truoc):
            continue
        parts = re.split(r',| và | va ', truoc)
        for p in parts:
            core = prov_core(p.strip())
            core = core.replace('tphcm', 'ho chi minh').strip()
            if core and core != new_core and len(core) >= 3:
                province_aliases[core] = new_core

    paren_re = re.compile(r'\(([^)]+)\)')
    resolver = {}
    for x in cap_xa:
        new = x['ten']
        prov_disp = prov_by_ma.get(x.get('magoc', ''), '')
        pc = prov_core(prov_disp)
        if not pc:
            continue
        truoc = x.get('truocsapnhap', '')
        # Gán quận/huyện THEO TỪNG PHƯỜNG: hint trong ngoặc áp cho các phường
        # đứng TRƯỚC nó (tính từ hint trước đó) — không gộp cả record,
        # tránh 'Phường 10 (Quận 6)' dính nhầm '(Quận 8)' của phường khác.
        pending = []          # [(ward_core, old_display)]
        entries = []          # [(wc, old, dist_hint)]
        for part in truoc.split(','):
            part = part.strip()
            if not part:
                continue
            hints = paren_re.findall(part)
            wtext = paren_re.sub('', part).strip()
            if wtext and len(wtext) >= 2:
                wc = ward_core(wtext)
                if wc:
                    pending.append((wc, wtext))
            if hints:
                hint = norm(hints[-1])
                for wc, wtext in pending:
                    entries.append((wc, wtext, hint))
                pending = []
        for wc, wtext in pending:      # phường cuối không có ngoặc
            entries.append((wc, wtext, ''))

        for wc, wtext, hint in entries:
            resolver.setdefault(pc, {}).setdefault(wc, [])
            entry = {'new': new, 'dist': hint, 'old': wtext}   # old = tên gốc CÓ DẤU
            if entry not in resolver[pc][wc]:
                resolver[pc][wc].append(entry)

    # Map tên phường MỚI -> malk (id lớp bản đồ, dùng tải polygon từ pread_json)
    # + map NGƯỢC: phường mới -> danh sách phường/xã cũ (nguyên văn từ nghị quyết)
    ward_malk = {}
    new_wards = {}
    for x in cap_xa:
        pc = prov_core(prov_by_ma.get(x.get('magoc', ''), ''))
        if not pc:
            continue
        key = norm(x['ten'])
        if x.get('malk'):
            ward_malk.setdefault(pc, {})[key] = x['malk']
        new_wards.setdefault(pc, {})[key] = {
            'name': x['ten'],
            'old': x.get('truocsapnhap', ''),
        }

    out = {'provinces': provinces, 'province_aliases': province_aliases,
           'ward_malk': ward_malk, 'new_wards': new_wards, 'resolver': resolver}
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))

    n_pairs = sum(len(w) for w in resolver.values())
    print(f"Provinces: {len(provinces)} | province buckets: {len(resolver)} | old-ward keys: {n_pairs}")
    print(f"Saved: {OUT}")


if __name__ == '__main__':
    main()
