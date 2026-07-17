#!/usr/bin/env python3
"""過去データでスコアリング重みとレース選択戦略をバックテストする.

- 過去DAYS日分の出走表(programs)と結果(results)を取得
- 重みの組み合わせを総当たりで評価し、3連単6点の的中率を比較
- レース選択戦略(クラリティ/イン純度など)ごとの的中率も評価
"""

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from itertools import product

JST = timezone(timedelta(hours=9))
P_URL = "https://boatraceopenapi.github.io/programs/v2/{y}/{ymd}.json"
R_URL = "https://boatraceopenapi.github.io/results/v2/{y}/{ymd}.json"

DAYS = int(os.environ.get("DAYS", "60"))

# イン(1コース)が強いことで知られる場
IN_STADIUMS = {24, 18, 21, 19, 12}  # 大村, 徳山, 芦屋, 下関, 住之江


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "kyotei-backtest/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def load_data():
    races = []
    today = datetime.now(JST).date()
    for i in range(1, DAYS + 1):
        d = today - timedelta(days=i)
        ymd = d.strftime("%Y%m%d")
        try:
            progs = get(P_URL.format(y=ymd[:4], ymd=ymd)).get("programs") or []
            ress = get(R_URL.format(y=ymd[:4], ymd=ymd)).get("results") or []
        except Exception:  # noqa: BLE001
            continue
        rmap = {}
        for r in ress:
            key = (int(r.get("race_stadium_number") or 0), int(r.get("race_number") or 0))
            places = {}
            for b in r.get("boats") or []:
                p = b.get("racer_place_number")
                if p in (1, 2, 3):
                    places[int(p)] = int(b["racer_boat_number"])
            if len(places) == 3:
                rmap[key] = f"{places[1]}-{places[2]}-{places[3]}"
        for p in progs:
            key = (int(p.get("race_stadium_number") or 0), int(p.get("race_number") or 0))
            boats = p.get("boats") or []
            if key not in rmap or len(boats) != 6:
                continue
            feats = []
            for b in boats:
                feats.append({
                    "lane": int(b.get("racer_boat_number") or 6),
                    "nat": float(b.get("racer_national_top_2_percent") or 0),
                    "loc": float(b.get("racer_local_top_2_percent") or 0),
                    "mot": float(b.get("racer_assigned_motor_top_2_percent") or 0),
                    "bt": float(b.get("racer_assigned_boat_top_2_percent") or 0),
                    "st": float(b.get("racer_average_start_timing") or 0.25),
                    "cls": int(b.get("racer_class_number") or 4),
                    "fly": int(b.get("racer_flying_count") or 0),
                })
            races.append({
                "date": d.strftime("%Y-%m-%d"),
                "stadium": key[0],
                "rno": key[1],
                "closed": p.get("race_closed_at") or "",
                "boats": feats,
                "result": rmap[key],
            })
    return races


COURSE = {1: 20.0, 2: 8.0, 3: 6.0, 4: 4.0, 5: 2.0, 6: 0.0}
CLASSP = {1: 12.0, 2: 6.0, 3: 2.0, 4: 0.0}


def score(b, w):
    s = (b["nat"] * w[0] + b["loc"] * w[1] + b["mot"] * w[2] + b["bt"] * 0.15
         + COURSE[b["lane"]] * w[3] + CLASSP[b["cls"]] * w[4])
    if b["st"] > 0:
        s += max(0.0, 0.25 - b["st"]) * w[5]
    s -= b["fly"] * 5
    return s


def combos_current(h, t, s3, f4):
    return {f"{h}-{t}-{s3}", f"{h}-{t}-{f4}", f"{h}-{f4}-{s3}",
            f"{t}-{s3}-{f4}", f"{f4}-{h}-{t}", f"{f4}-{h}-{s3}"}


def combos_anchor(h, t, s3, f4):
    # ◎1着固定: 2着3着に○▲△を流す6点
    out = set()
    for a, b2 in [(t, s3), (t, f4), (s3, t), (s3, f4), (f4, t), (f4, s3)]:
        out.add(f"{h}-{a}-{b2}")
    return out


def top4(r, w):
    ranked = sorted(r["boats"], key=lambda b: score(b, w), reverse=True)
    return [b["lane"] for b in ranked[:4]], ranked


def evaluate(races, w, shape):
    hit = 0
    for r in races:
        lanes, _ = top4(r, w)
        cs = combos_current(*lanes) if shape == "current" else combos_anchor(*lanes)
        if r["result"] in cs:
            hit += 1
    return hit / len(races) if races else 0.0


def main():
    races = load_data()
    print(f"検証レース数: {len(races)} (過去{DAYS}日)")
    if not races:
        return

    base = (0.9, 0.6, 0.5, 1.0, 1.0, 50)
    grid = list(product(
        [0.6, 0.9, 1.2],          # nat
        [0.2, 0.6],               # loc
        [0.3, 0.5, 0.8],          # motor
        [0.5, 1.0, 1.75, 2.5],    # course scale
        [0.5, 1.0, 2.0],          # class scale
        [0, 50, 100],             # st
    ))
    results = []
    for w in grid:
        for shape in ("current", "anchor"):
            results.append((evaluate(races, w, shape), w, shape))
    results.sort(reverse=True)
    print("\n== 重み探索 上位10 (的中率 | nat,loc,mot,course,class,st | 形) ==")
    for rate, w, shape in results[:10]:
        print(f"{rate:.3%} | {w} | {shape}")
    print(f"\n現行設定: {evaluate(races, base, 'current'):.3%} | {base} | current")

    best_rate, bw, bshape = results[0]

    # ---- レース選択戦略の評価 (ベスト重みで) ----
    def clarity(r):
        ranked = sorted((score(b, bw) for b in r["boats"]), reverse=True)
        return ranked[0] - ranked[1]

    def hit_of(subset):
        if not subset:
            return 0.0, 0
        h = 0
        for r in subset:
            lanes, _ = top4(r, bw)
            cs = combos_current(*lanes) if bshape == "current" else combos_anchor(*lanes)
            if r["result"] in cs:
                h += 1
        return h / len(subset), len(subset)

    print("\n== レース選択戦略 (ベスト重みで) ==")
    allr, n = hit_of(races)
    print(f"全レース: {allr:.3%} (n={n})")

    def top_boat_is(r, lane=None, cls=None):
        lanes, ranked = top4(r, bw)
        b0 = ranked[0]
        if lane is not None and b0["lane"] != lane:
            return False
        if cls is not None and b0["cls"] != cls:
            return False
        return True

    s1 = [r for r in races if top_boat_is(r, lane=1)]
    print("本命が1号艇: {:.3%} (n={})".format(*hit_of(s1)))
    s2 = [r for r in races if top_boat_is(r, lane=1, cls=1)]
    print("本命が1号艇かつA1: {:.3%} (n={})".format(*hit_of(s2)))
    s3_ = [r for r in s2 if r["stadium"] in IN_STADIUMS]
    print("↑かつイン優位場: {:.3%} (n={})".format(*hit_of(s3_)))

    cl_sorted = sorted(races, key=clarity, reverse=True)
    for pct in (10, 20, 30):
        top = cl_sorted[: len(races) * pct // 100]
        print(f"クラリティ上位{pct}%: {hit_of(top)[0]:.3%} (n={len(top)})")

    both = [r for r in cl_sorted if top_boat_is(r, lane=1, cls=1)]
    for pct in (30, 50):
        top = both[: max(1, len(both) * pct // 100)]
        print(f"1号艇A1本命×クラリティ上位{pct}%: {hit_of(top)[0]:.3%} (n={len(top)})")

    # 1日3レース選定シミュレーション (11:00〜19:30締切から選ぶ)
    from collections import defaultdict
    bydate = defaultdict(list)
    for r in races:
        t = r["closed"][11:16]
        if "11:00" <= t <= "19:30":
            bydate[r["date"]].append(r)
    picked = []
    for d, rs in bydate.items():
        cand = [r for r in rs if top_boat_is(r, lane=1, cls=1)] or rs
        cand.sort(key=clarity, reverse=True)
        picked += cand[:3]
    print("\n1日3レース選定シミュレーション(1号艇A1優先+クラリティ): {:.3%} (n={})".format(*hit_of(picked)))


if __name__ == "__main__":
    main()
