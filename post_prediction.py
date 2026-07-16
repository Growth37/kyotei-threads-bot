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

# 枠番ごとのコース補正 (1コースのイン優位を反映)
COURSE_BONUS = {1: 20.0, 2: 8.0, 3: 6.0, 4: 4.0, 5: 2.0, 6: 0.0}


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
        national2 * 0.9
        + local2 * 0.6
        + motor2 * 0.5
        + boat2 * 0.15
        + COURSE_BONUS.get(lane, 0.0)
        + {1: 12.0, 2: 6.0, 3: 2.0, 4: 0.0}.get(cls, 0.0)
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
    candidates.sort(key=lambda x: x[0])

    # スコア1位と2位の差が大きい=「本命がはっきりしている」レースを優先
    def clarity(race):
        scores = sorted((score_boat(b) for b in race["boats"]), reverse=True)
        return scores[0] - scores[1]

    best = max(candidates[:12], key=lambda x: clarity(x[1]))
    return best[1]


def build_post(race: dict) -> str:
    stadium = STADIUMS.get(int(race["race_stadium_number"]), "不明")
    rno = int(race["race_number"])
    closed = race["race_closed_at"][11:16]  # HH:MM

    boats = sorted(race["boats"], key=score_boat, reverse=True)
    honmei, taikou, sanban = boats[0], boats[1], boats[2]

    def lane(b):
        return int(b["racer_boat_number"])

    def name(b):
        return str(b["racer_name"]).replace("　", "")

    def cls(b):
        return CLASSES.get(int(b.get("racer_class_number") or 4), "?")

    h, t, s = lane(honmei), lane(taikou), lane(sanban)
    trifecta = f"{h}-{t}-{s} / {h}-{s}-{t}"

    title = race.get("race_title") or ""
    if len(title) > 20:
        title = title[:20] + "…"

    lines = [
        f"🚤 {stadium}{rno}R 予想 (締切 {closed})",
        f"『{title}』" if title else "",
        "",
        f"◎ {h}号艇 {name(honmei)} ({cls(honmei)})",
        f"○ {t}号艇 {name(taikou)} ({cls(taikou)})",
        f"▲ {s}号艇 {name(sanban)} ({cls(sanban)})",
        "",
        f"3連単: {trifecta}",
        "",
        "※出走表データからの機械的な予想です。舟券は自己責任で🙏",
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


def main():
    dry_run = os.environ.get("DRY_RUN", "0") == "1"
    now = datetime.now(JST)
    print(f"実行時刻(JST): {now:%Y-%m-%d %H:%M}")

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
    post_to_threads(text, user_id, token)


if __name__ == "__main__":
    main()
