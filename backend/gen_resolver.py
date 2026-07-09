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

    paren_re = re.compile(r'\(([^)]+)\)')
    resolver = {}
    for x in cap_xa:
        new = x['ten']
        prov_disp = prov_by_ma.get(x.get('magoc', ''), '')
        pc = prov_core(prov_disp)
        if not pc:
            continue
        truoc = x.get('truocsapnhap', '')
        # quận/huyện gợi ý trong ngoặc
        dist_hints = [norm(m) for m in paren_re.findall(truoc)]
        dist_str = ' | '.join(dist_hints)
        core = paren_re.sub('', truoc)
        for oldw in core.split(','):
            oldw = oldw.strip()
            if not oldw or len(oldw) < 2:
                continue
            wc = ward_core(oldw)
            if not wc:
                continue
            resolver.setdefault(pc, {}).setdefault(wc, [])
            entry = {'new': new, 'dist': dist_str}
            if entry not in resolver[pc][wc]:
                resolver[pc][wc].append(entry)

    out = {'provinces': provinces, 'resolver': resolver}
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))

    n_pairs = sum(len(w) for w in resolver.values())
    print(f"Provinces: {len(provinces)} | province buckets: {len(resolver)} | old-ward keys: {n_pairs}")
    print(f"Saved: {OUT}")


if __name__ == '__main__':
    main()
