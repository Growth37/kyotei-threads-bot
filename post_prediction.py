#!/usr/bin/env python3
"""競艇予想をThreadsに自動投稿するスクリプト.

出走表データ: Boatrace Open API (非公式) https://boatraceopenapi.github.io/
投稿先: Meta Threads API (graph.threads.net)

環境変数:
  THREADS_ACCESS_TOKEN Threads長期アクセストークン
  DRY_RUN              "1"なら投稿せず内容を表示するだけ
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import urllib.error

JST = timezone(timedelta(hours=9))

PROGRAMS_URL = "https://boatraceopenapi.github.io/programs/v2/today.json"
THREADS_API = "https://graph.threads.net/v1.0"

# 公式サイト(boatrace.jp)— 無料APIが本日分を未反映のときのフォールバック取得先
OFFICIAL_INDEX = "https://www.boatrace.jp/owpc/pc/race/index?hd={ymd}"
OFFICIAL_RACEINDEX = "https://www.boatrace.jp/owpc/pc/race/raceindex?jcd={jcd:02d}&hd={ymd}"
OFFICIAL_RACELIST = "https://www.boatrace.jp/owpc/pc/race/racelist?rno={rno}&jcd={jcd:02d}&hd={ymd}"
_CLASS_STR = {"A1": 1, "A2": 2, "B1": 3, "B2": 4}

STADIUMS = {
    1: "桐生", 2: "戸田", 3: "江戸川", 4: "平和島", 5: "多摩川", 6: "浜名湖",
    7: "蒲郡", 8: "常滑", 9: "津", 10: "三国", 11: "びわこ", 12: "住之江",
    13: "尼崎", 14: "鳴門", 15: "丸亀", 16: "児島", 17: "宮島", 18: "徳山",
    19: "下関", 20: "若松", 21: "芦屋", 22: "福岡", 23: "唐津", 24: "大村",
}

CLASSES = {1: "A1", 2: "A2", 3: "B1", 4: "B2"}

# 枠番ごとのコース補正 (1コースのイン優位を反映 / 60日バックテストで最適化済み)
COURSE_BONUS = {1: 50.0, 2: 20.0, 3: 15.0, 4: 10.0, 5: 5.0, 6: 0.0}


def http_get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "kyotei-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))


def http_post(url: str, params: dict):
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))


# ===== 公式サイト(boatrace.jp)フォールバック取得 ===================
# 無料API(boatraceopenapi)が本日分を反映していない日だけ、公式サイトの
# 出走表HTMLから同じ形のデータを組み立てる。普段はAPIを使う二段構え。

def http_get_html(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; kyotei-bot/1.0)"}
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read().decode("utf-8", errors="replace")


def _num(s):
    """'3.70'→3.7 / '-'や空→None."""
    s = (s or "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def official_active_stadiums(ymd: str) -> list:
    """本日開催している場コード(1〜24)の一覧を index ページから取得."""
    html = http_get_html(OFFICIAL_INDEX.format(ymd=ymd))
    found = set()
    for x in re.findall(r"jcd=(\d{1,2})", html):
        n = int(x)
        if 1 <= n <= 24:
            found.add(n)
    return sorted(found)


def official_closing_times(jcd: int, ymd: str) -> dict:
    """1場の各レースの締切予定時刻 {レース番号: 'HH:MM'} を raceindex から取得."""
    html = http_get_html(OFFICIAL_RACEINDEX.format(jcd=jcd, ymd=ymd))
    times = {}
    for m in re.finditer(r">(\d{1,2})R</[a-z]+>[\s\S]{0,300}?(\d{1,2}:\d{2})<", html):
        rno = int(m.group(1))
        if rno not in times:
            times[rno] = m.group(2)
    return times


def official_parse_racelist(html: str, stadium: int, rno: int,
                            date_str: str, closed_hm: str) -> dict:
    """出走表HTMLを解析し、APIと同じ形のレース辞書を返す(1〜6号艇の順)."""
    starts = [
        m.start() for m in re.finditer(
            r'<div class="is-fs11">\s*(\d{4})\s*/\s*<span[^>]*>\s*[AB][12]\s*</span>',
            html,
        )
    ][:6]
    boats = []
    for i, s in enumerate(starts):
        block = html[s: (starts[i + 1] if i + 1 < len(starts) else s + 4000)]
        cm = re.search(r"<span[^>]*>\s*([AB][12])\s*</span>", block)
        cls = _CLASS_STR.get(cm.group(1), 4) if cm else 4
        nm = re.search(r'is-fs18 is-fBold"><a[^>]*>([^<]+)</a>', block)
        name = re.sub(r"\s+", " ", nm.group(1)).strip() if nm else ""
        fst = re.search(r"F(\d+)\s*<br[^>]*>\s*L\d+\s*<br[^>]*>\s*([\d.]+|-)", block)
        flying = int(fst.group(1)) if fst else 0
        st = _num(fst.group(2)) if fst else None
        # 続く is-lineH2 セルの並び: [全国, 当地, モーター, ボート]。各セルの2番目が2連率
        cells = re.findall(
            r"is-lineH2[^>]*>\s*([\d.]+)\s*<br[^>]*>\s*([\d.]+)\s*<br[^>]*>\s*([\d.]+)\s*<",
            block,
        )

        def rate2(idx):
            return _num(cells[idx][1]) if idx < len(cells) else None

        boats.append({
            "racer_boat_number": i + 1,
            "racer_name": name,
            "racer_class_number": cls,
            "racer_national_top_2_percent": rate2(0) or 0.0,
            "racer_local_top_2_percent": rate2(1) or 0.0,
            "racer_assigned_motor_top_2_percent": rate2(2) or 0.0,
            "racer_assigned_boat_top_2_percent": rate2(3) or 0.0,
            "racer_average_start_timing": st if st is not None else 0.20,
            "racer_flying_count": flying,
        })
    return {
        "race_stadium_number": stadium,
        "race_number": rno,
        "race_date": date_str,
        "race_closed_at": f"{date_str} {closed_hm}:00",
        "boats": boats,
    }


def load_programs_official(now: datetime, ymd: str, today: str) -> list:
    """公式サイトから、対象時間帯(締切15〜130分後)のレースだけ出走表を集める."""
    try:
        stadiums = official_active_stadiums(ymd)
    except Exception as e:  # noqa: BLE001
        print(f"公式サイトの開催場取得に失敗: {e}")
        return []
    print(f"本日開催: {len(stadiums)}場 {stadiums}")

    candidates = []  # (jcd, rno, 'HH:MM')
    for jcd in stadiums:
        try:
            times = official_closing_times(jcd, ymd)
        except Exception as e:  # noqa: BLE001
            print(f"  {jcd}場のレース一覧取得に失敗: {e}")
            continue
        for rno, hm in times.items():
            try:
                t = datetime.strptime(f"{today} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)
            except ValueError:
                continue
            delta = (t - now).total_seconds() / 60
            if 15 <= delta <= 130:
                candidates.append((jcd, rno, hm))
        time.sleep(0.2)
    candidates.sort(key=lambda c: c[2])
    print(f"対象時間帯の候補レース: {len(candidates)}件")

    programs = []
    for jcd, rno, hm in candidates[:40]:
        try:
            html = http_get_html(OFFICIAL_RACELIST.format(rno=rno, jcd=jcd, ymd=ymd))
            race = official_parse_racelist(html, jcd, rno, today, hm)
            if len(race["boats"]) == 6:
                programs.append(race)
        except Exception as e:  # noqa: BLE001
            print(f"  {jcd}場{rno}Rの出走表取得に失敗: {e}")
        time.sleep(0.3)
    print(f"公式サイトから {len(programs)}レース分の出走表を取得しました。")
    return programs


def load_programs(now: datetime) -> list:
    """出走表データを取得。無料APIが本日分を反映していなければ公式サイトへ切替."""
    ymd = now.strftime("%Y%m%d")
    today = now.strftime("%Y-%m-%d")
    api_programs = []
    try:
        data = http_get_json(PROGRAMS_URL)
        api_programs = data.get("programs") or []
    except Exception as e:  # noqa: BLE001
        print(f"無料API取得に失敗: {e}")
    fresh = any((p.get("race_date") or "") >= today for p in api_programs)
    if api_programs and fresh:
        print(f"データ源: 無料API (本日 {len(api_programs)}レース)")
        return api_programs
    print("無料APIが本日分を未反映。公式サイト(boatrace.jp)から取得します。")
    return load_programs_official(now, ymd, today)


def score_boat(boat: dict) -> float:
    """出走表の公開データから簡易スコアを計算する."""
    national2 = float(boat.get("racer_national_top_2_percent") or 0)
    local2 = float(boat.get("racer_local_top_2_percent") or 0)
    motor2 = float(boat.get("racer_assigned_motor_top_2_percent") or 0)
    boat2 = float(boat.get("racer_assigned_boat_top_2_percent") or 0)
    st = float(boat.get("racer_average_start_timing") or 0.25)
    cls = int(boat.get("racer_class_number") or 4)
    lane = int(boat.get("racer_boat_number") or 6)

    score = (
        national2 * 0.6
        + local2 * 0.2
        + motor2 * 0.3
        + boat2 * 0.15
        + COURSE_BONUS.get(lane, 0.0)
        + {1: 6.0, 2: 3.0, 3: 1.0, 4: 0.0}.get(cls, 0.0)
    )
    # STが早い(数値が小さい)ほど加点。0.10で+7.5点、0.20で+2.5点程度
    if st > 0:
        score += max(0.0, (0.25 - st)) * 50
    # フライング持ちは減点
    score -= int(boat.get("racer_flying_count") or 0) * 5
    return score


def _posted_keys_today(now) -> set:
    """本日すでに投稿済みのレース(場番号, R)集合。1号/2号の両ログを見る."""
    today = now.strftime("%Y-%m-%d")
    keys = set()
    for path in ("posts_log.json", "posts_log_2.json"):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for e in json.load(f):
                    if (e.get("race_date") or "")[:10] == today:
                        keys.add((int(e["stadium_number"]), int(e["race_number"])))
        except Exception:  # noqa: BLE001
            continue
    return keys


def _manual_skip_today(now) -> set:
    """manual_skip.json の本日分(場番号, R)を自動投稿の除外対象として返す."""
    today = now.strftime("%Y-%m-%d")
    try:
        with open("manual_skip.json", encoding="utf-8") as f:
            data = json.load(f)
        return {(int(a), int(b)) for a, b in (data.get(today) or [])}
    except Exception:  # noqa: BLE001
        return set()


def pick_race(programs: list, now: datetime):
    """締切がこれから来るレースのうち、直近1〜2時間のものを選ぶ."""
    candidates = []
    for race in programs:
        closed_at = race.get("race_closed_at")
        if not closed_at:
            continue
        try:
            t = datetime.strptime(closed_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
        except ValueError:
            continue
        delta = (t - now).total_seconds() / 60
        # 20分後〜120分後に締め切られるレースを対象にする
        if 20 <= delta <= 120 and len(race.get("boats") or []) == 6:
            candidates.append((t, race))
    if not candidates:
        return None
    # 本日すでにどちらかのbotが投稿したレースは避ける(重複回避)。
    # ただし、それで候補が全滅する時間帯は臨機応変に重複を許容して投稿する。
    skip = _manual_skip_today(now)
    candidates = [
        c for c in candidates
        if (int(c[1]["race_stadium_number"]), int(c[1]["race_number"])) not in skip
    ]
    if not candidates:
        return None
    posted = _posted_keys_today(now)
    deduped = [
        c for c in candidates
        if (int(c[1]["race_stadium_number"]), int(c[1]["race_number"])) not in posted
    ]
    candidates = deduped if deduped else candidates

    # スコア1位と2位の差が大きい=「本命がはっきりしている」レースを優先
    def clarity(race):
        scores = sorted((score_boat(b) for b in race["boats"]), reverse=True)
        return scores[0] - scores[1]

    def top_is_lane1_a1(race):
        top = max(race["boats"], key=score_boat)
        return int(top.get("racer_boat_number") or 0) == 1 and int(top.get("racer_class_number") or 4) == 1

    # バックテストの結果: 「本命が1号艇のA1」のレースに絞ると的中率が大きく上がる
    preferred = [c for c in candidates if top_is_lane1_a1(c[1])]
    pool = preferred if preferred else candidates
    best = max(pool, key=lambda x: clarity(x[1]))
    return best[1]


def _sort_combos(combos: list) -> list:
    return sorted(combos, key=lambda c: tuple(int(x) for x in c.split("-")))


def build_honmei(h: int, t: int, s: int, f4: int, f5: int) -> list:
    """本命9点: ◎1着ながし。2着=○▲△、3着=○▲△⑤。

    66日分のバックテストで、狙い(4点)と同じ回収率(約78%)を保ったまま
    的中率が約38%→約59%に上がった形。◎が1着に来たときの2〜3着の
    取りこぼしを拾えるのが効いている。
    """
    seconds = [t, s, f4]
    thirds = [t, s, f4, f5]
    return _sort_combos([
        f"{h}-{b}-{c}" for b in seconds for c in thirds if c != b
    ])


def build_nerai(h: int, t: int, s: int, f4: int) -> list:
    """狙い4点: ○▲を頭に◎を2着に置く中穴ゾーン (据え置き)."""
    return _sort_combos([
        f"{t}-{h}-{s}", f"{t}-{h}-{f4}", f"{s}-{h}-{t}", f"{s}-{h}-{f4}",
    ])


def build_shibori(h: int, t: int, s: int, f4: int) -> list:
    """絞り4点: ◎1着固定・2着=○▲・3着=○▲△。本命9点の中で一番硬い核."""
    return _sort_combos([
        f"{h}-{t}-{s}", f"{h}-{t}-{f4}", f"{h}-{s}-{t}", f"{h}-{s}-{f4}",
    ])


def build_comment(race: dict, ranked: list) -> str:
    """レースの実データから関西弁の実況風コメントをつくる (毎回ちがう褒め方)."""
    import random
    seed = int(race["race_stadium_number"]) * 1000 + int(race["race_number"]) * 7 \
        + sum(int(b["racer_boat_number"]) * int(float(b.get("racer_average_start_timing") or 0.2) * 100) for b in race["boats"])
    rng = random.Random(seed)

    def nm(b):
        return str(b["racer_name"]).replace("　", "").replace(" ", "")

    def ln(b):
        return int(b["racer_boat_number"])

    top = ranked[0]

    def honmei_line(b):
        """本命艇の一番ええところを、その日ごとに違う切り口で褒める."""
        name, lane = nm(b), ln(b)
        nat = float(b.get("racer_national_top_2_percent") or 0)
        loc = float(b.get("racer_local_top_2_percent") or 0)
        mot = float(b.get("racer_assigned_motor_top_2_percent") or 0)
        st = float(b.get("racer_average_start_timing") or 0.25)
        cls = int(b.get("racer_class_number") or 4)

        cands = []
        if nat >= 45:
            cands.append([
                f"{lane}号艇{name}、全国2連率{nat}%は数字が違うわ。ここは信頼していこ",
                f"何といっても{name}の全国2連率{nat}%やろ。安定感が段違いやねん",
                f"{name}は全国2連率{nat}%、そうそう崩れへんタイプやし本線でええ",
            ])
        if loc >= 40:
            cands.append([
                f"{name}はこの水面が得意で当地2連率{loc}%、庭みたいなもんやろ",
                f"当地{loc}%の{name}、ここ走り慣れてるのはデカいで",
                f"{lane}号艇{name}、当地2連率{loc}%でコース知り尽くしとる強みがある",
            ])
        if 0 < st <= 0.14:
            cands.append([
                f"{name}はST{st}と踏み込み鋭いし、まず好スタート決めてくるやろ",
                f"{lane}号艇{name}のスタート勘がええ(ST{st})、先手取れば展開作れるで",
                f"何より{name}のST{st}、この一歩目の速さは武器やねん",
            ])
        if mot >= 40:
            cands.append([
                f"{name}はモーター2連率{mot}%と気配上々、足が来てるのはホンマ強い",
                f"{lane}号艇{name}、モーター{mot}%でよう回っとるし伸びも押さえも効くやろ",
                f"{name}の機力({mot}%)がええから、多少展開ずれても粘れるタイプや",
            ])
        if cls == 1:
            cands.append([
                f"{lane}号艇{name}はA1の格上、勝負どころの一手が違うし信頼度高いわ",
                f"{name}はA1やし地力が一枚上、ここは落ち着いて中心でええやろ",
                f"格でいえば{name}(A1)が抜けてる、本線でどっしり獲りにいくで",
            ])
        if lane == 1:
            cands.append([
                f"1コースに{name}が入ったし、イン信頼で組み立てるのがセオリーやろ",
                f"{name}が1号艇なら、まずインの逃げ本線から入るのが素直やねん",
                f"1コース{name}、枠なりならこのイン先マイが一番堅いと見てるで",
            ])
        cands.append([
            f"{lane}号艇{name}、ここは軸として信頼してええと見とる",
            f"総合的に見て{name}が一枚上や、素直に中心でいこ",
            f"{lane}号艇{name}を主役に組むのが自然な一戦やな",
            f"今日の{name}は信頼度高め、本線でどっしり構えるで",
            f"{name}の地力を買うて、ここは頭から狙いたい",
            f"{lane}号艇{name}、展開面でも恵まれそうやし軸でええ",
            f"ここは{name}の実力が一枚抜けとる、素直に中心や",
            f"{name}を信じて組む一戦、軸はブレへんで",
            f"{lane}号艇{name}、能力的にここは任せられると見とる",
            f"地力上位の{name}を頭に、手堅く攻めていこ",
            f"{name}の安定感を評価して、ここは本線で勝負や",
            f"{lane}号艇{name}が中心で間違いないとみとる",
            f"総合力なら{name}、ここは軸固定で問題なしや",
            f"{name}を信頼の本命に据えて組み立てるで",
            f"{lane}号艇{name}、ここ一番は実力を買うで",
            f"展開を読んでも{name}中心が濃厚、素直にいこ",
            f"{name}の総合力が光る一戦、頭から狙うで",
            f"ここは{lane}号艇{name}の信頼度が高い、軸でええ",
            f"地力で{name}が抜けとる、中心視で間違いなしや",
            f"{name}をどっしり軸に、落ち着いて組むで",
            f"{lane}号艇{name}、ここは堅く本命視でいこ",
            f"総合評価で{name}が上位、素直に頭で勝負や",
            f"{name}の力を信じて、ここは本線中心でいく",
            f"{lane}号艇{name}が主軸、展開も向きそうやな",
            f"ここは{name}の地力が頼りになる、軸でいこか",
            f"{name}を中心に据えるのが妥当な一戦やな",
            f"{lane}号艇{name}、能力上位で信頼できると見とる",
            f"地力上位の{name}、ここは頭固定でいくで",
            f"{name}の総合力を買うて本線で勝負するわ",
            f"ここは{lane}号艇{name}を信じて中心視でいこ",
        ])
        if not cands:
            cands.append([
                f"{lane}号艇{name}を中心に、ここは本線でいこか",
                f"総合力で{name}が抜けてるし、素直に中心視でええやろ",
            ])
        return rng.choice(rng.choice(cands))

    # 2文目: 本命以外の「注意艇/展開」ネタから1つ
    outer = [b for b in race["boats"] if ln(b) >= 3 and ln(b) != ln(top)]
    others = [b for b in race["boats"] if ln(b) != ln(top)]
    lane1 = next((b for b in race["boats"] if ln(b) == 1), None)
    best_st_out = min(outer, key=lambda b: float(b.get("racer_average_start_timing") or 0.25)) if outer else None
    best_st_oth = min(others, key=lambda b: float(b.get("racer_average_start_timing") or 0.25)) if others else None
    best_mot_oth = max(others, key=lambda b: float(b.get("racer_assigned_motor_top_2_percent") or 0)) if others else None

    seconds = []
    if lane1 is not None and ln(top) == 1 and float(lane1.get("racer_average_start_timing") or 0.2) >= 0.17 and best_st_out is not None:
        seconds.append(rng.choice([
            f"ただ1のSTがちょい遅めやから、スタート決まれば{ln(best_st_out)}の{nm(best_st_out)}が直まくりで一発あるかも",
            f"1のスタート甘なったら{ln(best_st_out)}の{nm(best_st_out)}がカドからズドンいくで、そこは警戒やな",
        ]))
    if best_st_oth is not None and float(best_st_oth.get("racer_average_start_timing") or 0.25) <= 0.14:
        seconds.append(rng.choice([
            f"相手は{ln(best_st_oth)}号艇{nm(best_st_oth)}、ST{best_st_oth.get('racer_average_start_timing')}の踏み込みが速いから展開ついたら怖い",
            f"{ln(best_st_oth)}の{nm(best_st_oth)}はスタート速いし、握って回られたら一気やで",
        ]))
    if best_mot_oth is not None and float(best_mot_oth.get("racer_assigned_motor_top_2_percent") or 0) >= 42:
        seconds.append(rng.choice([
            f"{ln(best_mot_oth)}号艇はモーター{best_mot_oth.get('racer_assigned_motor_top_2_percent')}%とよう伸びるし、相手筆頭はここやと見てる",
            f"足でいうと{ln(best_mot_oth)}の{nm(best_mot_oth)}が一番エエの積んどる、ヒモには絶対入れときたい",
        ]))
    # 汎用の締め
    seconds.append(rng.choice([
        f"あとは相手を{ln(ranked[1])}・{ln(ranked[2])}あたりでどう抑えるか、そこの勝負やな",
        f"2着争いは{ln(ranked[1])}と{ln(ranked[2])}のガチンコになると見てるで",
        f"ヒモは{ln(ranked[1])}・{ln(ranked[2])}を軸に手広く、荒れ気配あれば{ln(ranked[3])}まで",
    ]))

    seconds.extend([
        "相手は手広く構えて、取りこぼしだけは防ぎたいな",
        "展開ひとつで2着は入れ替わる、ヒモは厚めにいくで",
        "2着争いは混戦とみて、相手を広めに取るで",
        "ヒモは人気どころ中心に、薄く穴も一枚添えとこ",
        "内の艇が踏ん張れるか、そこが2着のカギやな",
        "差し・まくり両にらみで、相手は幅を持たせるで",
        "本命頭は動かさず、2着3着で遊びを入れるわ",
        "相手筆頭は実績上位、そこから手堅く流すで",
        "展開が決まれば順当、ヒモも素直でええやろ",
        "波乱の目は薄いとみて、相手は人気サイドで",
        "2着は迷うとこやけど、総合力で絞っていくで",
        "インが残る前提で、ヒモは枠なり中心やな",
        "相手の一角に伸び型がおる、そこは押さえときたい",
        "堅い決着を想定して、相手もシンプルにまとめるで",
        "2着に穴っぽいのが一枚、薄めに拾っとくわ",
        "ヒモはスタート勘のええ艇を上位に見るで",
        "相手は3艇に絞って、点数を抑えめにいくで",
        "まくり差しが決まると2着が荒れる、一応ケアや",
        "本線は本命軸で固定、相手で変化をつけるで",
        "2着候補は横一線、ここは手広く構えるわ",
        "モーター気配のええ艇を相手上位に取るで",
        "内枠有利の水面やし、ヒモも内中心でええやろ",
        "相手は堅実に、人気どころから外さんようにな",
        "展開次第で3着は大きく動く、広めにカバーや",
        "2着は実力通りとみて、素直に流していくで",
        "伸び型の一撃だけ警戒して、相手に一枚添えるで",
        "本命が決めれば相手はついてくる、順当狙いや",
        "ヒモは絞りすぎず、ほどよく幅を持たせるで",
        "相手の軸は実績上位、そこを信頼して流すわ",
        "2着3着はこの数艇で収まるとみて組むで",
    ])
    a = honmei_line(top)
    b = rng.choice(seconds[:-1]) if len(seconds) > 1 and rng.random() < 0.75 else seconds[-1]

    def finish(p):
        return p if p.endswith(("？", "！")) else p + "！"

    return f"{finish(a)}\n{finish(b)}"


def build_post(race: dict) -> str:
    stadium = STADIUMS.get(int(race["race_stadium_number"]), "不明")
    rno = int(race["race_number"])
    closed = race["race_closed_at"][11:16]  # HH:MM

    boats = sorted(race["boats"], key=score_boat, reverse=True)
    honmei, taikou, sanban, yonban, goban = boats[0], boats[1], boats[2], boats[3], boats[4]

    def lane(b):
        return int(b["racer_boat_number"])

    def name(b):
        return str(b["racer_name"]).replace("　", "")

    def cls(b):
        return CLASSES.get(int(b.get("racer_class_number") or 4), "?")

    h, t, s = lane(honmei), lane(taikou), lane(sanban)
    f4, f5 = lane(yonban), lane(goban)
    honmei_c = build_honmei(h, t, s, f4, f5)
    nerai_c = build_nerai(h, t, s, f4)
    comment = build_comment(race, boats)
    # 本命: ◎ - {○▲△} - {○▲△⑤}  /  狙い: {○▲} - ◎ - {○▲△}
    hon2 = "".join(str(x) for x in sorted([t, s, f4]))
    hon3 = "".join(str(x) for x in sorted([t, s, f4, f5]))
    ner1 = "".join(str(x) for x in sorted([t, s]))
    ner3 = "".join(str(x) for x in sorted([t, s, f4]))

    import random
    rng = random.Random(f"{race.get('race_stadium_number')}-{rno}-{closed}")
    _phrases = ["予想いくで〜", "いっとこか〜", "さあ本命勝負や", "これは獲りにいくで", "狙い絞ったで", "今日の勝負レースや", "手堅くいこか", "本命軸でいくで", "ちょい荒れ狙うで", "よっしゃ気合入れてこ", "ここらで一発当てたる", "自信の一戦や", "攻めの買い目でいくで", "鉄板狙いやで", "荒れる予感してるわ", "本命どっしりいこ", "ここは固いとみた", "勝負どころや", "腕が鳴るレースや", "今日イチの自信レースや", "そろそろ当てたいわ", "ここで流れ変えたる", "じっくり読んだで", "展開ばっちり読めたわ", "この一点が熱いで", "穴もちょい狙うで", "本線で獲りきったる", "いざ勝負や", "気合十分でいくで", "丁寧に読んでみたで", "ここは逃がさんで", "攻めるで今日は", "固め打ちでいくで", "一撃狙っていくで", "ここ一番や", "読み切ったで今回は", "熱い一戦やな", "本命から手広くいくで", "ここはインが堅いわ", "展開次第で化けるで", "絞って勝負やで", "自信あるで今日は", "ここで取り返したる", "面白いレースやで", "狙いどころ見つけたわ", "しっかり当てにいくで", "ここは本命中心でいこ", "ちょっと荒れるかもな", "勝負師の血が騒ぐわ", "今日は獲れる気するで", "この買い目で勝負や", "インからズバッといくで", "慎重に組み立てたで", "ここは手堅く本命や", "波乱あるかもしれんで", "気持ち入れていくで", "今回は自信ありやで", "じっくりいこか", "ここは絞って一点勝負", "流れ来てる気がするわ", "攻守バランスよういくで", "本命堅いとみとるで", "ここで決めたるで", "当てにいくレースや", "ちょい妙味あるで", "今日はこれで勝負や", "読みはバッチリや", "ここは外さんつもりや", "勝負の一戦いくで", "落ち着いて読んだで", "ここは本線勝負やな", "一点に懸けるで", "熱くなってきたわ", "ここで流れ掴むで", "本命から幅広く構えたで", "荒れ覚悟で攻めるで", "固いとこ押さえたで", "ここは勝ちにいくで", "今日の自信レースいくで", "しぶとく当てにいくで", "ここは堅実にいこ", "妙味たっぷりやで", "気合い入れ直していくで", "この一戦しっかり読んだ", "ここは信じて本命や", "攻めて獲りにいくで", "今日は手応えあるで", "じっくり構えていくで", "ここで一発決めたる", "本命軸は譲らんで", "読みに自信ありや", "ここは思い切っていくで", "面白い配当狙えるで", "本命でガッチリいこ", "ここが勝負の分かれ目や", "いっちょ当てたるで", "今日は冴えてるで", "ここは鉄板とみた", "勝負の時間やで", "さあ獲りにいこか"]
    header = f"🚤 {stadium}{rno}R " + rng.choice(_phrases) + f" (締切 {closed})"
    jiko = rng.choice(["※舟券は自己責任でな🙏", "※最後は自分の判断で頼むで🙏", "※舟券は無理のない範囲でな🙏", "※あくまで参考程度に頼むわ🙏", "※余剰資金で楽しむんやで🙏", "※舟券はほどほどにな🙏", "※当たったらラッキーくらいでな😊", "※最終判断はご自身で頼むで🙏", "※一緒に的中狙お🔥", "※勝負は計画的にな🙏", "※深追いはせんといてな🙏", "※参考にしてもらえたら嬉しいわ🙏", "※あたりますように🙏", "※今日もええレースになりますように🙏", "※舟券は遊べる範囲でな🙏", "※熱くなりすぎんようにな🙏", "※無理せんと楽しもな😊", "※当たるも八卦、楽しんでな🙏", "※最後は自己責任で頼むわ🙏", "※一緒に盛り上がろな🔥", "※ご武運を祈っとるで🙏", "※外れても怒らんといてな😅", "※舟券は余裕資金で頼むで🙏", "※今日も楽しんでいこな😊", "※当たったら一緒に喜ぼ🔥", "※無理のない勝負でな🙏", "※気楽に見てってな😊", "※的中を願っとるで🙏", "※参考になれば幸いや🙏", "※舟券は自己責任でお願いします🙏", "※ほどほどに楽しむんやで🙏", "※最後の判断は任せるで🙏", "※今日もよろしゅう頼むわ🙏", "※いい一日になりますように🙏", "※一緒に獲りにいこな🔥", "※勝ちも負けも楽しんでな😊", "※無茶な賭けはあかんで🙏", "※あくまで予想や、参考にな🙏", "※当たったら教えてな😊", "※舟券はゆとりを持ってな🙏", "※今日も的中いきたいな🔥", "※落ち着いて勝負しよな🙏", "※楽しんだもん勝ちやで😊", "※ご自身の判断も大事にな🙏", "※無理せんと続けていこな🙏", "※今日もしっかり狙お🔥", "※舟券は計画的に楽しんでな🙏", "※最後はあなた次第やで🙏", "※のんびり楽しんでいこな😊", "※ともに的中掴も🔥", "※欲張らんと堅実にな🙏", "※勝っても負けても笑顔でな😊", "※舟券は身の丈に合わせてな🙏", "※今日もワクワクいこな🔥", "※ムキにならんと楽しもな😊", "※大事なお金やで慎重にな🙏", "※当たりを信じていこな🔥", "※ゆるっと見てってな😊", "※投資は自己判断で頼むで🙏", "※外れても引きずらんといてな😅", "※今日もええ波来るといいな🔥", "※無理のない額で遊ぼな🙏", "※一緒にワイワイいこな😊", "※的中目指して頑張ろな🔥", "※あくまで一意見やで🙏", "※熱くなりすぎず冷静にな🙏", "※楽しむのが一番やで😊", "※勝負は余裕を持ってな🙏", "※今日も当ててみせるで🔥", "※ご自愛しながら楽しんでな🙏", "※賭けすぎは禁物やで🙏", "※一緒に夢見よな🔥", "※気負わず楽しんでな😊", "※最後は自分を信じてな🙏", "※無理したらあかんで🙏", "※今日も良いこと願っとるで🔥", "※ほどよく楽しむんやで😊", "※勝ち逃げもアリやで🙏", "※予想は予想、参考までにな🙏", "※外れは次への糧やで😅", "※今日も一緒に戦おな🔥", "※肩の力抜いて楽しもな😊", "※大きく張らんと堅実にな🙏", "※今日も的中引き寄せよ🔥", "※楽しく勝負していこな😊", "※無理ない範囲が一番やで🙏", "※一緒にゴール目指そな🔥", "※余裕資金でお願いするで🙏", "※焦らずいこな😊", "※今日もええ結果祈っとるで🔥", "※ご自身のペースでな🙏", "※深追い禁物、冷静にな🙏", "※勝っても調子乗らんとな😊", "※舟券は楽しむ道具やで🔥", "※堅実第一でいこな🙏", "※今日も気楽にいこな😊", "※一発を信じていこな🔥", "※無理せんのが長続きのコツや🙏", "※当たったら祝杯あげよな🔥", "※ともに良い一日にしよな😊"])
    lines = [
        header,
        "",
        comment,
        "",
        f"◎ {h}号艇 {name(honmei)} ({cls(honmei)})",
        f"○ {t}号艇 {name(taikou)} ({cls(taikou)})",
        f"▲ {s}号艇 {name(sanban)} ({cls(sanban)})",
        f"△ {f4}号艇 {name(yonban)} ({cls(yonban)})",
        "",
        "🎯買い目",
        f"【本命】{h}-{hon2}-{hon3}",
        f"【絞り】{h}-{ner1}-{ner3}",
        f"【狙い】{ner1}-{h}-{ner3}",
        "",
        jiko,
        "#競艇 #ボートレース #競艇予想",
    ]
    text = "\n".join(l for l in lines if l is not None)
    return text[:500]


def get_user_id(token: str) -> str:
    """アクセストークンからThreadsユーザーIDを自動取得する."""
    qs = urllib.parse.urlencode({"fields": "id,username", "access_token": token})
    me = http_get_json(f"{THREADS_API}/me?{qs}")
    if not me.get("id"):
        raise RuntimeError(f"ユーザーID取得に失敗: {me}")
    print(f"投稿先アカウント: @{me.get('username')} (id={me['id']})")
    return me["id"]


def post_to_threads(text: str, user_id: str, token: str):
    container = http_post(
        f"{THREADS_API}/{user_id}/threads",
        {"media_type": "TEXT", "text": text, "access_token": token},
    )
    creation_id = container.get("id")
    if not creation_id:
        raise RuntimeError(f"コンテナ作成に失敗: {container}")
    print(f"コンテナ作成OK: {creation_id} — 35秒待機します")
    time.sleep(35)
    result = http_post(
        f"{THREADS_API}/{user_id}/threads_publish",
        {"creation_id": creation_id, "access_token": token},
    )
    if not result.get("id"):
        raise RuntimeError(f"公開に失敗: {result}")
    print(f"投稿完了! post id = {result['id']}")
    return result["id"]


def append_log(post_id: str, race: dict, now):
    """posts_log.json に投稿記録を追記する(トラッキング用)."""
    log_file = "posts_log.json"
    log = []
    if os.path.exists(log_file):
        with open(log_file, encoding="utf-8") as f:
            log = json.load(f)
    boats = sorted(race["boats"], key=score_boat, reverse=True)
    lanes = [int(b["racer_boat_number"]) for b in boats[:5]]
    honmei_c = build_honmei(*lanes[:5])
    nerai_c = build_nerai(*lanes[:4])
    shibori_c = build_shibori(*lanes[:4])
    log.append({
        "post_id": post_id,
        "posted_at": now.isoformat(timespec="seconds"),
        "race_date": race.get("race_date") or now.strftime("%Y-%m-%d"),
        "stadium_number": int(race["race_stadium_number"]),
        "stadium": STADIUMS.get(int(race["race_stadium_number"]), "不明"),
        "race_number": int(race["race_number"]),
        "race_closed_at": race["race_closed_at"],
        "combos": honmei_c + nerai_c,
        "honmei": honmei_c,
        "nerai": nerai_c,
        "shibori": shibori_c,
        "views": {},
    })
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    print("投稿ログを記録しました。")


def recently_posted(now, minutes: int = 45) -> bool:
    """直近に投稿済みなら True (スケジュール遅延による二重投稿を防ぐ)."""
    if not os.path.exists("posts_log.json"):
        return False
    try:
        with open("posts_log.json", encoding="utf-8") as f:
            log = json.load(f)
        if not log:
            return False
        last = datetime.fromisoformat(log[-1]["posted_at"])
        return (now - last).total_seconds() < minutes * 60
    except Exception:  # noqa: BLE001
        return False


# ---- X(Twitter)へのコンパクト投稿 (OAuth1.0a / 標準ライブラリのみ) ----
X_API_URL = "https://api.twitter.com/2/tweets"


def _x_pe(v):
    return urllib.parse.quote(str(v), safe="-._~")


def _x_weighted_len(text):
    n = 0
    for ch in text:
        cp = ord(ch)
        if cp <= 0x10FF or 0x2000 <= cp <= 0x200D or 0x2010 <= cp <= 0x201F or 0x2032 <= cp <= 0x2037:
            n += 1
        else:
            n += 2
    return n


def build_x_post(race):
    """Xの文字数(280/日本語実質140)に収まるコンパクト版を作る."""
    stadium = STADIUMS.get(int(race["race_stadium_number"]), "不明")
    rno = int(race["race_number"])
    closed = race["race_closed_at"][11:16]
    boats = sorted(race["boats"], key=score_boat, reverse=True)
    honmei, taikou, sanban, yonban, goban = boats[0], boats[1], boats[2], boats[3], boats[4]

    def lane(b):
        return int(b["racer_boat_number"])

    def name(b):
        return str(b["racer_name"]).replace("\u3000", "")

    def cls(b):
        return CLASSES.get(int(b.get("racer_class_number") or 4), "?")

    h, t, s, f4, f5 = lane(honmei), lane(taikou), lane(sanban), lane(yonban), lane(goban)

    def j(xs):
        return "".join(str(x) for x in sorted(xs))

    hon = f"{h}-{j([t, s, f4])}-{j([t, s, f4, f5])}"
    shi = f"{h}-{j([t, s])}-{j([t, s, f4])}"
    ner = f"{j([t, s])}-{h}-{j([t, s, f4])}"
    header = f"\U0001f6a4 {stadium}{rno}R (締切 {closed})"
    picks = [
        f"\u25ce {h}号艇 {name(honmei)} ({cls(honmei)})",
        f"\u25cb {t}号艇 {name(taikou)} ({cls(taikou)})",
        f"\u25b2 {s}号艇 {name(sanban)} ({cls(sanban)})",
        f"\u25b3 {f4}号艇 {name(yonban)} ({cls(yonban)})",
    ]
    buy = ["\U0001f3af買い目", f"【本命】{hon}", f"【絞り】{shi}", f"【狙い】{ner}"]
    text = "\n".join([header, ""] + picks + [""] + buy)
    if _x_weighted_len(text) > 280:
        text = "\n".join([header, ""] + buy)
    return text


def post_to_x(text, env_suffix=""):
    ck = os.environ.get("X_API_KEY" + env_suffix, "").strip()
    cs = os.environ.get("X_API_SECRET" + env_suffix, "").strip()
    tok = os.environ.get("X_ACCESS_TOKEN" + env_suffix, "").strip()
    toks = os.environ.get("X_ACCESS_SECRET" + env_suffix, "").strip()
    if not all([ck, cs, tok, toks]):
        print("Xのキーが未設定のため、Xへの投稿はスキップします。")
        return None
    oauth = {
        "oauth_consumer_key": ck,
        "oauth_nonce": base64.b64encode(os.urandom(16)).decode().strip("="),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": tok,
        "oauth_version": "1.0",
    }
    param_str = "&".join("{}={}".format(_x_pe(k), _x_pe(oauth[k])) for k in sorted(oauth))
    base = "&".join(["POST", _x_pe(X_API_URL), _x_pe(param_str)])
    key = "{}&{}".format(_x_pe(cs), _x_pe(toks))
    oauth["oauth_signature"] = base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    ).decode()
    auth = "OAuth " + ", ".join(
        '{}="{}"'.format(_x_pe(k), _x_pe(oauth[k])) for k in sorted(oauth)
    )
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(X_API_URL, data=body, method="POST")
    req.add_header("Authorization", auth)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tid = (data.get("data") or {}).get("id")
        print(f"X投稿OK: {tid}")
        return tid
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        print(f"X投稿エラー: HTTP {e.code} {detail}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"X投稿エラー: {e}", file=sys.stderr)
    return None


def main():
    dry_run = os.environ.get("DRY_RUN", "0") == "1"
    now = datetime.now(JST)
    print(f"実行時刻(JST): {now:%Y-%m-%d %H:%M}")

    # ガード1: 想定時間帯(11:00〜19:30 JST)以外はスキップ (クーロン遅延対策)
    hm = now.strftime("%H:%M")
    if not dry_run and not ("11:00" <= hm <= "19:30"):
        print("想定の投稿時間帯(11:00〜19:30)外のためスキップします。")
        return

    # ガード2: 直近100分以内に投稿済みならスキップ (二重投稿防止)
    if not dry_run and recently_posted(now):
        print("直近に投稿済みのためスキップします。")
        return

    programs = load_programs(now)
    print(f"取得レース数: {len(programs)}")

    race = pick_race(programs, now)
    if race is None:
        print("この時間帯に対象レースがないため、今回は投稿をスキップします。")
        return

    text = build_post(race)
    print("---- 投稿内容 ----")
    print(text)
    print("------------------")
    try:
        x_text = build_x_post(race)
        print("---- X投稿内容(コンパクト) ----")
        print(x_text)
        print("------------------")
    except Exception as _e:  # noqa: BLE001
        x_text = None
        print(f"X投稿文の生成に失敗(本体は継続): {_e}", file=sys.stderr)

    if dry_run:
        print("DRY_RUN=1 のため投稿はしません。")
        return

    token = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        print("THREADS_ACCESS_TOKEN が未設定です。", file=sys.stderr)
        sys.exit(1)

    user_id = get_user_id(token)
    post_id = post_to_threads(text, user_id, token)
    if x_text:
        post_to_x(x_text)
    append_log(post_id, race, now)


if __name__ == "__main__":
    main()
