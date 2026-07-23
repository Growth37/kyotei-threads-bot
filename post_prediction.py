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
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

PROGRAMS_URL = "https://boatraceopenapi.github.io/programs/v2/today.json"
THREADS_API = "https://graph.threads.net/v1.0"

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


def build_honmei(h: int, t: int, s: int, f4: int) -> list:
    """本命4点: ◎1着固定の硬め (バックテストで最も的中率が高かった形)."""
    return _sort_combos([
        f"{h}-{t}-{s}", f"{h}-{t}-{f4}", f"{h}-{s}-{t}", f"{h}-{s}-{f4}",
    ])


def build_nerai(h: int, t: int, s: int, f4: int) -> list:
    """狙い4点: ○▲を頭に◎を2着に置く中穴ゾーン."""
    return _sort_combos([
        f"{t}-{h}-{s}", f"{t}-{h}-{f4}", f"{s}-{h}-{t}", f"{s}-{h}-{f4}",
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
    honmei, taikou, sanban, yonban = boats[0], boats[1], boats[2], boats[3]

    def lane(b):
        return int(b["racer_boat_number"])

    def name(b):
        return str(b["racer_name"]).replace("　", "")

    def cls(b):
        return CLASSES.get(int(b.get("racer_class_number") or 4), "?")

    h, t, s = lane(honmei), lane(taikou), lane(sanban)
    f4 = lane(yonban)
    honmei_c = build_honmei(h, t, s, f4)
    nerai_c = build_nerai(h, t, s, f4)
    comment = build_comment(race, boats)
    ab2 = "".join(str(x) for x in sorted([t, s]))
    ab3 = "".join(str(x) for x in sorted([t, s, f4]))

    lines = [
        f"🚤 {stadium}{rno}R 予想いくで〜 (締切 {closed})",
        "",
        comment,
        "",
        f"◎ {h}号艇 {name(honmei)} ({cls(honmei)})",
        f"○ {t}号艇 {name(taikou)} ({cls(taikou)})",
        f"▲ {s}号艇 {name(sanban)} ({cls(sanban)})",
        f"△ {f4}号艇 {name(yonban)} ({cls(yonban)})",
        "",
        "🎯買い目",
        f"【本命】{h}-{ab2}-{ab3}",
        f"【狙い】{ab2}-{h}-{ab3}",
        "",
        "※舟券は自己責任でな🙏",
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
    lanes = [int(b["racer_boat_number"]) for b in boats[:4]]
    honmei_c = build_honmei(*lanes)
    nerai_c = build_nerai(*lanes)
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
        "views": {},
    })
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    print("投稿ログを記録しました。")


def recently_posted(now, minutes: int = 100) -> bool:
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

    data = http_get_json(PROGRAMS_URL)
    programs = data.get("programs") or []
    print(f"本日のレース数: {len(programs)}")

    race = pick_race(programs, now)
    if race is None:
        print("この時間帯に対象レースがないため、今回は投稿をスキップします。")
        return

    text = build_post(race)
    print("---- 投稿内容 ----")
    print(text)
    print("------------------")

    if dry_run:
        print("DRY_RUN=1 のため投稿はしません。")
        return

    token = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        print("THREADS_ACCESS_TOKEN が未設定です。", file=sys.stderr)
        sys.exit(1)

    user_id = get_user_id(token)
    post_id = post_to_threads(text, user_id, token)
    append_log(post_id, race, now)


if __name__ == "__main__":
    main()
