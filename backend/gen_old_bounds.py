"""
Sinh backend/old_bounds/{province_core}.json từ GADM 4.1 level-3 (ranh giới
phường/xã CŨ trước sáp nhập). Dùng cho tra NGƯỢC: tọa độ → phường cũ.

Input:  C:/Temp/gadm3/gadm41_VNM_3.json (tải từ geodata.ucdavis.edu)
Output: mỗi tỉnh MỚI 1 file: [{'k': 'phuong14', 'd': 'quan10', 'name', 'dist', 'g': geom}]
        key đã norm bỏ dấu + BỎ HẾT KHOẢNG TRẮNG (GADM viết dính liền).
"""
import json, os, sys, unicodedata

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = sys.argv[1] if len(sys.argv) > 1 else r'C:\Temp\gadm3\gadm41_VNM_3.json'
OUTDIR = os.path.join(BASE, 'old_bounds')


def norm(s):
    s = unicodedata.normalize('NFD', (s or '').lower())
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return s.replace('đ', 'd')


def ns(s):
    """norm + bỏ mọi khoảng trắng/gạch — để khớp tên GADM viết dính liền."""
    return ''.join(norm(s).split()).replace('-', '')


def round_coords(obj, nd=5):
    if isinstance(obj, (int, float)):
        return round(obj, nd)
    return [round_coords(x, nd) for x in obj]


def main():
    with open(os.path.join(BASE, 'ward_resolver.json'), encoding='utf-8') as f:
        R = json.load(f)
    # old-province (không dấu, không space) -> new province core
    prov_map = {}
    for pc in R['resolver'].keys():
        prov_map[ns(pc)] = pc                       # tỉnh giữ nguyên tên
    for old, new in R.get('province_aliases', {}).items():
        prov_map[ns(old)] = new                     # tỉnh cũ -> mới
    prov_map['thuathienhue'] = 'hue'                # GADM dùng tên cũ 2021

    with open(SRC, encoding='utf-8') as f:
        g = json.load(f)

    buckets = {}
    miss = set()
    for feat in g['features']:
        pr = feat['properties']
        p1 = ns(pr.get('NAME_1', ''))
        pc = prov_map.get(p1)
        if not pc:
            miss.add(pr.get('NAME_1'))
            continue
        name3 = pr.get('NAME_3', '')
        type3 = pr.get('TYPE_3', '')
        # key phường: cả dạng có prefix lẫn không
        k_full = ns(type3 + name3)   # 'phuong14', 'xaantinh'
        k_bare = ns(name3)           # '14', 'antinh' (GADM NAME_3 có thể đã kèm prefix)
        # tên hiển thị: GADM viết dính ('Phường14', 'ThạnhMỹTây') → chèn space
        def spacify(s):
            out = ''
            for i, ch in enumerate(s):
                if i > 0:
                    prev = s[i - 1]
                    if (prev.islower() and (ch.isupper() or ch.isdigit())) or \
                       (prev.isdigit() and ch.isalpha()):
                        out += ' '
                out += ch
            return out
        disp3 = spacify(name3)
        if ns(name3).startswith(ns(type3)):
            disp = disp3
        else:
            disp = f"{type3} {disp3}".strip()
        disp2 = spacify(pr.get('NAME_2', ''))
        entry = {
            'k': k_full,
            'k2': k_bare,
            'd': ns(pr.get('NAME_2', '')),
            'name': disp,
            'dist': disp2,
            'g': {'type': feat['geometry']['type'],
                  'coordinates': round_coords(feat['geometry']['coordinates'])},
        }
        buckets.setdefault(pc, []).append(entry)

    os.makedirs(OUTDIR, exist_ok=True)
    total = 0
    for pc, items in buckets.items():
        path = os.path.join(OUTDIR, pc.replace(' ', '_') + '.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(items, f, ensure_ascii=False, separators=(',', ':'))
        total += len(items)
        sz = os.path.getsize(path) // 1024
        print(f"{pc}: {len(items)} wards, {sz} KB")
    if miss:
        print("UNMAPPED provinces:", miss)
    print(f"Total: {total} old wards")


if __name__ == '__main__':
    main()
