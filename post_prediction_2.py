#!/usr/bin/env python3
"""競艇予想を @shantianzhi37 (2号アカウント) へ自動投稿するスクリプト.

1号bot(post_prediction.py)のデータ取得/スコアリング/投稿処理を流用しつつ、
- 堅いレースは @r_no_yosou 寄りの「◎1着ながし」中心(9〜11点)で構成
- 1号艇A1中心に厳選。無ければ厳選せず通常のレースを選んで投稿
- 1日1レースだけ「中穴狙い」を自動割当(一般戦・2〜4号艇に上手い/得意選手)
- 本線 / 絞り / 抑え の3ブロックで投稿
- 人間味なしのシンプルなフォーマットで投稿
- @r_no_yosou(posts_log.json)と同じレースは避ける
- 記録は posts_log_2.json

環境変数:
  THREADS_ACCESS_TOKEN_2  @shantianzhi37 の長期アクセストークン
  DRY_RUN                 "1"なら投稿せず内容を表示するだけ
"""

import json
import os
import sys
from datetime import datetime

import post_prediction as eng  # 1号botのエンジンを流用

JST = eng.JST
STADIUMS = eng.STADIUMS
score_boat = eng.score_boat

LOG_FILE = "posts_log_2.json"
OTHER_LOG = "posts_log.json"  # @r_no_yosou 側(重複回避用)

# 6投稿の予定時刻(JST)。中穴いの自動割当に使う。
SCHEDULE = ["10:14", "11:36", "12:53", "15:05", "16:29", "18:47"]

# この時間帯は必ず「堅いレース」にする(中穴を割り当てない)
FORCE_SOLID = {"11:36", "15:05"}

# 投稿の時間帯ガード(クーロン遅延対策)
WINDOW_START = "10:00"
WINDOW_END = "19:30"


# ===== 買い目ビルダー ===================================================
def mm(*xs) -> str:
    """[2,4,5] -> '245' (昇順・重複除去)."""
    return "".join(str(x) for x in sorted(set(int(x) for x in xs)))


def _expand(head, seconds, thirds):
    out = []
    for b in seconds:
        for c in thirds:
            if len({head, b, c}) == 3:
                out.append(f"{head}-{b}-{c}")
    return eng._sort_combos(list(dict.fromkeys(out)))


def _expand2(head, second, thirds):
    out = [f"{head}-{second}-{c}" for c in thirds if len({head, second, c}) == 3]
    return eng._sort_combos(list(dict.fromkeys(out)))


def build_solid(h, t, s, f4, f5, tight: bool):
    """堅いレース用の 本線/絞り/抑え を組む(@r_no_yosou 寄り = ◎1着ながし中心).

    本線 : ◎-{○▲△}-{○▲△⑤}    ◎1着ながし(9点)。的中の主力。
    絞り : ◎-{○▲}-{○▲△}      本線の中で一番硬い核(4点・表示用サブセット)
    抑え : ○-◎-{▲△}           本命が2着に沈んだ時の薄い保険(2点)
    合計 : 通常 11点 / 超本命(tight)は保険を省いて ◎1着ながし 9点に集中。
    ※どのケースも 9〜11点に収まる。
    """
    honsen_disp = f"{h}-{mm(t, s, f4)}-{mm(t, s, f4, f5)}"
    honsen = _expand(h, [t, s, f4], [t, s, f4, f5])          # 9点(◎1着ながし)
    shibori_disp = f"{h}-{mm(t, s)}-{mm(t, s, f4)}"
    shibori = _expand(h, [t, s], [t, s, f4])                 # 4点(核・サブセット)

    if tight:
        # 超本命レース: ◎1着ながし9点に一点集中(保険なし)
        osae_lines = []
        osae = []
    else:
        # 通常: ○頭に◎を2着で置く薄い保険を2点だけ
        osae_lines = [f"{t}-{h}-{mm(s, f4)}"]
        osae = _expand2(t, h, [s, f4])                       # 2点(t-h-s, t-h-f4)
    return {
        "honsen_disp": honsen_disp, "honsen": honsen,
        "shibori_disp": shibori_disp, "shibori": shibori,
        "osae_lines": osae_lines, "osae": osae,
    }


def build_medium(h, t, s, f4, f5):
    """中穴い用の 本線/絞り/抑え を組む(継続).

    本線 : ○頭で狙う中穴ゾーン  ○-{◎▲}-{◎▲△}
    絞り : ○-{◎▲}-{◎▲}
    抑え : 硬めの予想(◎頭の本命形)を保険で ◎-{○▲}-{○▲△}
    合計 4+4=8点。
    """
    honsen_disp = f"{t}-{mm(h, s)}-{mm(h, s, f4)}"
    honsen = _expand(t, [h, s], [h, s, f4])
    shibori_disp = f"{t}-{mm(h, s)}-{mm(h, s)}"
    shibori = _expand(t, [h, s], [h, s])
    osae_lines = [f"{h}-{mm(t, s)}-{mm(t, s, f4)}"]
    osae = _expand(h, [t, s], [t, s, f4])
    return {
        "honsen_disp": honsen_disp, "honsen": honsen,
        "shibori_disp": shibori_disp, "shibori": shibori,
        "osae_lines": osae_lines, "osae": osae,
    }


# ===== レース選定 =======================================================
def _lanes(race):
    boats = sorted(race["boats"], key=score_boat, reverse=True)
    return [int(b["racer_boat_number"]) for b in boats[:5]], boats


def _clarity(race):
    scores = sorted((score_boat(b) for b in race["boats"]), reverse=True)
    return scores[0] - scores[1]


def solidity(race):
    """堅さスコア: 1着が明確なほど / イン・A1ほど高い."""
    top = max(race["boats"], key=score_boat)
    val = _clarity(race)
    if int(top.get("racer_boat_number") or 0) == 1:
        val += 30
    if int(top.get("racer_class_number") or 4) == 1:
        val += 15
    st = float(top.get("racer_average_start_timing") or 0.25)
    val += max(0.0, (0.20 - st)) * 30
    return val


def _lane1_a1_solid(race) -> bool:
    """本命(スコア1位)が『1号艇のA1』かどうか。
    バックテストで的中率が大きく上がる、@r_no_yosou と同じ厳選条件。
    """
    top = max(race["boats"], key=score_boat)
    return (int(top.get("racer_boat_number") or 0) == 1
            and int(top.get("racer_class_number") or 4) == 1)


def pick_solid(cands):
    """堅いレースを選ぶ。1号艇A1中心に厳選し、無ければ厳選せず通常から選ぶ."""
    preferred = [r for r in cands if _lane1_a1_solid(r)]
    pool = preferred if preferred else cands
    return max(pool, key=solidity)


def medium_score(race):
    """中穴度: 本命が絶対的でなく、相手(○)が強いほど高い."""
    boats = sorted(race["boats"], key=score_boat, reverse=True)
    if len(boats) < 2:
        return -999
    second = boats[1]
    gap = _clarity(race)
    val = score_boat(second)
    if int(second.get("racer_boat_number") or 0) in (2, 3, 4):
        val += 5
    # 抜けた本命(大差)は中穴向きでないので減点、混戦すぎも減点
    val -= max(0.0, gap - 22) * 1.5
    val -= max(0.0, 6 - gap) * 1.0
    return val


# レースのグレード番号: 1=SG, 2=G1, 3=G2, 4=G3, 5=一般 (boatraceopenapi v2)
GENERAL_GRADE = 5


def _is_general(race) -> bool:
    """一般戦(グレード5)かどうか。グレード不明(公式サイト取得時)はFalse扱い。"""
    return int(race.get("race_grade_number") or 0) == GENERAL_GRADE


def _strong_challenger(race) -> bool:
    """2・3・4号艇に『上手い(A1)』or『得意(全国/当地2連率が高い)』選手がいるか."""
    for b in race.get("boats") or []:
        if int(b.get("racer_boat_number") or 0) not in (2, 3, 4):
            continue
        cls = int(b.get("racer_class_number") or 4)
        nat2 = float(b.get("racer_national_top_2_percent") or 0)
        loc2 = float(b.get("racer_local_top_2_percent") or 0)
        # A1(上手い) / 全国2連率45%以上(上手い) / 当地2連率45%以上(得意)
        if cls == 1 or nat2 >= 45 or loc2 >= 45:
            return True
    return False


def medium_eligible(race) -> bool:
    """中穴狙いの対象レース条件:
      ・一般戦のみ(SG/G1/G2/G3は除外)
      ・2・3・4号艇に上手い(A1)or得意な選手がいる
    """
    return _is_general(race) and _strong_challenger(race)


def _race_key(race):
    return (str(race.get("race_date") or ""),
            int(race["race_stadium_number"]), int(race["race_number"]))


def _posted_keys(now):
    """本日すでにどちらかのbotが投稿したレースの集合(重複回避)."""
    today = now.strftime("%Y-%m-%d")
    keys = set()
    for path in (OTHER_LOG, LOG_FILE):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for e in json.load(f):
                    if (e.get("race_date") or "")[:10] == today:
                        keys.add((today, int(e["stadium_number"]), int(e["race_number"])))
        except Exception:  # noqa: BLE001
            continue
    return keys


def _medium_posted_today(now) -> bool:
    today = now.strftime("%Y-%m-%d")
    if not os.path.exists(LOG_FILE):
        return False
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            for e in json.load(f):
                if (e.get("race_date") or "")[:10] == today and e.get("mode") == "中穴":
                    return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _remaining_slots(now) -> int:
    """今以降の『中穴を出せる』スロット数(堅い固定スロットは除く)."""
    hm = now.strftime("%H:%M")
    return sum(1 for s in SCHEDULE if s >= hm and s not in FORCE_SOLID)


def _current_slot(now):
    """今の実行がどのスロットか(直近45分以内に過ぎた予定時刻)を返す."""
    hm = now.strftime("%H:%M")
    past = [s for s in SCHEDULE if s <= hm]
    if not past:
        return None
    slot = past[-1]
    st = datetime.strptime(slot, "%H:%M").time()
    slot_dt = now.replace(hour=st.hour, minute=st.minute, second=0, microsecond=0)
    if 0 <= (now - slot_dt).total_seconds() <= 45 * 60:
        return slot
    return None


def candidate_races(programs, now):
    """締切20〜120分後・6艇のレースを候補に返す.

    本日すでに投稿済みのレースは避けるが、それで候補が全滅する時間帯は
    臨機応変に重複を許容して投稿する(投稿ゼロを避ける)。
    """
    posted = _posted_keys(now)
    window = []
    for race in programs:
        closed_at = race.get("race_closed_at")
        if not closed_at or len(race.get("boats") or []) != 6:
            continue
        try:
            t = datetime.strptime(closed_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
        except ValueError:
            continue
        delta = (t - now).total_seconds() / 60
        if not (20 <= delta <= 120):
            continue
        window.append(race)
    deduped = [r for r in window if _race_key(r) not in posted]
    return deduped if deduped else window


def select(programs, now):
    """このスロットで投稿する (race, mode) を決める. mode='堅い'|'中穴'."""
    cands = candidate_races(programs, now)
    if not cands:
        return None, None

    # 11:36 / 15:05 の枠は必ず堅いレース(中穴を割り当てない)
    if _current_slot(now) in FORCE_SOLID:
        return pick_solid(cands), "堅い"

    medium_done = _medium_posted_today(now)
    remaining = _remaining_slots(now)

    # 中穴は「一般戦で2・3・4号艇に上手い/得意な選手がいる」レースのみ対象
    elig = [r for r in cands if medium_eligible(r)]
    best_medium = max(elig, key=medium_score) if elig else None
    # 中穴を出すか判定 (1日1本まで・自動)
    want_medium = False
    if not medium_done and best_medium is not None:
        ms = medium_score(best_medium)
        # 残りスロットが少ない(=最後の方)なら対象があれば中穴、
        # それより前でも中穴度が高ければ前倒しで出す
        if remaining <= 1 and ms > -900:
            want_medium = True  # 最終スロット付近: 対象があれば中穴を出す
        elif ms >= 70:
            want_medium = True  # 前倒し: 中穴度が特に高いレースだけ

    if want_medium:
        return best_medium, "中穴"

    # 堅い: 1号艇A1中心に厳選(無ければ通常から選定)
    return pick_solid(cands), "堅い"


# ===== 投稿本文 =========================================================
def build_post(race, blocks, mode) -> str:
    stadium = STADIUMS.get(int(race["race_stadium_number"]), "不明")
    rno = int(race["race_number"])
    closed = race["race_closed_at"][11:16]

    lines = [f"{stadium}{rno}R  {closed}〆", ""]
    if mode == "中穴":
        lines.append("中穴狙い")
        lines.append("")
    lines.append("本線")
    lines.append(blocks["honsen_disp"])
    lines.append("")
    lines.append("絞り")
    lines.append(blocks["shibori_disp"])
    if blocks.get("osae_lines"):
        lines.append("")
        lines.append("抑え")
        for o in blocks["osae_lines"]:
            lines.append(o)
    return "\n".join(lines)[:500]


def append_log(post_id, race, now, blocks, mode):
    log = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            log = json.load(f)
    combos = eng._sort_combos(list(dict.fromkeys(
        blocks["honsen"] + blocks["osae"]
    )))
    log.append({
        "post_id": post_id,
        "posted_at": now.isoformat(timespec="seconds"),
        "race_date": race.get("race_date") or now.strftime("%Y-%m-%d"),
        "stadium_number": int(race["race_stadium_number"]),
        "stadium": STADIUMS.get(int(race["race_stadium_number"]), "不明"),
        "race_number": int(race["race_number"]),
        "race_closed_at": race["race_closed_at"],
        "mode": mode,
        "honsen": blocks["honsen"],
        "shibori": blocks["shibori"],
        "osae": blocks["osae"],
        "combos": combos,
        "announced": False,
    })
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    print("投稿ログ(posts_log_2.json)を記録しました。")


def recently_posted(now, minutes: int = 45) -> bool:
    if not os.path.exists(LOG_FILE):
        return False
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            log = json.load(f)
        if not log:
            return False
        last = datetime.fromisoformat(log[-1]["posted_at"])
        return (now - last).total_seconds() < minutes * 60
    except Exception:  # noqa: BLE001
        return False


def blocks_for(race, mode):
    lanes, _ = _lanes(race)
    h, t, s, f4, f5 = (lanes + [0, 0, 0, 0, 0])[:5]
    if mode == "中穴":
        return build_medium(h, t, s, f4, f5)
    tight = _clarity(race) >= 25
    return build_solid(h, t, s, f4, f5, tight)


def main():
    dry_run = os.environ.get("DRY_RUN", "0") == "1"
    now = datetime.now(JST)
    print(f"実行時刻(JST): {now:%Y-%m-%d %H:%M}")

    hm = now.strftime("%H:%M")
    if not dry_run and not (WINDOW_START <= hm <= WINDOW_END):
        print(f"投稿時間帯({WINDOW_START}〜{WINDOW_END})外のためスキップします。")
        return
    if not dry_run and recently_posted(now):
        print("直近に投稿済みのためスキップします。")
        return

    programs = eng.load_programs(now)
    print(f"取得レース数: {len(programs)}")

    race, mode = select(programs, now)
    if race is None:
        print("この時間帯に対象レースがないためスキップします。")
        return
    print(f"選定: {STADIUMS.get(int(race['race_stadium_number']))}"
          f"{int(race['race_number'])}R  mode={mode}")

    blocks = blocks_for(race, mode)
    text = build_post(race, blocks, mode)
    print("---- 投稿内容 ----")
    print(text)
    print("------------------")

    if dry_run:
        print("DRY_RUN=1 のため投稿はしません。")
        return

    token = os.environ.get("THREADS_ACCESS_TOKEN_2", "").strip()
    if not token:
        print("THREADS_ACCESS_TOKEN_2 が未設定です。", file=sys.stderr)
        sys.exit(1)

    user_id = eng.get_user_id(token)
    post_id = eng.post_to_threads(text, user_id, token)
    append_log(post_id, race, now, blocks, mode)


if __name__ == "__main__":
    main()
