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

# 6投稿の予定時刻(JST)。中穴狙いの自動割当に使う。
SCHEDULE = ["10:14", "11:41", "13:26", "15:12", "16:52", "18:41"]

# この時間帯は必ず「堅いレース」にする(中穴を割り当てない)
FORCE_SOLID = {"11:41", "15:12"}

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
    """中穴狙い用の 本線/絞り/抑え を組む(継続).

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


def _boat1_is_a(race) -> bool:
    """①号艇がA級(A1/A2)かどうか."""
    for b in race.get("boats", []) or []:
        if int(b.get("racer_boat_number") or 0) == 1:
            return int(b.get("racer_class_number") or 4) in (1, 2)
    return False


def medium_eligible(race) -> bool:
    """中穴狙いの対象レース条件:
      ・一般戦のみ(SG/G1/G2/G3は除外)
      ・2・3・4号艇に上手い(A1)or得意な選手がいる
    """
    return _is_general(race) and _strong_challenger(race) and not _boat1_is_a(race)


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
    skip = eng._manual_skip_today(now)
    window = [r for r in window
              if (int(r["race_stadium_number"]), int(r["race_number"])) not in skip]
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
def _rn(b) -> str:
    """選手名(空白除去)."""
    return str(b.get("racer_name") or "").replace("　", "").replace(" ", "")


def _cls(b) -> str:
    """クラス表記(A1/A2/B1/B2)."""
    return eng.CLASSES.get(int(b.get("racer_class_number") or 4), "")


def _recent_reasons(n: int = 4):
    """直近n件の投稿で使った reason を返す(連続で同じ言い回しを避けるため)."""
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            log = json.load(f)
        out = [e.get("reason", "") for e in log if e.get("reason")]
        return out[-n:]
    except Exception:  # noqa: BLE001
        return []


def build_reason(race, mode) -> str:
    """レースごとに変わる短い「狙い・根拠」を2行返す。

    実データ(級別・ST・全国/当地2連率・号艇)から根拠を組み立てる。
    語尾(言い回し)は候補を多めに用意し、直近の投稿で使ったものは
    連続で使わない(同じ言い回しの繰り返しを防ぐ)。
    """
    import random
    boats = sorted(race["boats"], key=score_boat, reverse=True)
    if len(boats) < 3:
        return ""
    h, o, a = boats[0], boats[1], boats[2]

    def ln(b):
        return int(b.get("racer_boat_number") or 0)

    def st(b):
        return float(b.get("racer_average_start_timing") or 0.20)

    def nat(b):
        return float(b.get("racer_national_top_2_percent") or 0)

    def loc(b):
        return float(b.get("racer_local_top_2_percent") or 0)

    rng = random.Random()  # 毎回ランダム(直近と被らないよう選び直す)
    recent = "".join(_recent_reasons())

    if mode == "中穴":
        head = []
        if ln(o) in (2, 3, 4) and loc(o) >= 40:
            head.append(f"○{ln(o)}号艇{_rn(o)}が当地2連率{loc(o):.0f}%で狙える。")
        if ln(o) in (2, 3, 4) and nat(o) >= 45:
            head.append(f"○{ln(o)}号艇{_rn(o)}が全国2連率{nat(o):.0f}%と好調。")
        if _cls(o) == "A1":
            head.append(f"○{ln(o)}号艇{_rn(o)}はA1で逆転十分。")
        head.append(f"○{ln(o)}号艇{_rn(o)}を軸に一発狙い。")
        tail = ["◎の頭は割れるとみて○頭を本線に。",
                "本命が抜けきらない一戦、中穴に妙味。",
                "◎頭は薄めとみて相手上位に厚く。",
                "波乱含み、○の頭から仕掛ける。",
                "本命一辺倒は危険、相手を主軸に。",
                "混戦模様、配当妙味を取りにいく。",
                "堅く収まらないとみて中穴勝負。",
                "展開ひとつで一変、穴を狙う。"]
    else:
        head = []
        if ln(h) == 1 and _cls(h) == "A1":
            head.append(f"◎1号艇{_rn(h)}はA1でイン信頼度高め。")
            head.append(f"◎1号艇{_rn(h)}はA1、インから押し切り濃厚。")
        if st(h) <= 0.15:
            head.append(f"◎{ln(h)}号艇{_rn(h)}はST{st(h):.2f}と速く先手濃厚。")
            head.append(f"◎{ln(h)}号艇{_rn(h)}のスタート{st(h):.2f}が武器。")
        if nat(h) >= 50:
            head.append(f"◎{ln(h)}号艇{_rn(h)}が全国2連率{nat(h):.0f}%で力上位。")
            head.append(f"◎{ln(h)}号艇{_rn(h)}は地力上位で信頼できる。")
        if not head:
            head.append(f"◎{ln(h)}号艇{_rn(h)}が総合力トップ。")
            head.append(f"◎{ln(h)}号艇{_rn(h)}を素直に軸で。")
        tail = [f"相手は○{ln(o)}▲{ln(a)}中心。",
                "2着に○▲を厚めに構成。",
                "軸は信頼、2着づけで勝負。",
                f"ヒモは{ln(o)}{ln(a)}を絡めて手広く。",
                "本線は薄く、抑えで保険をかけた。",
                "頭固定でヒモ流しの組み立て。",
                "取りこぼし防止に2着を広めに。",
                "的中率重視でコンパクトに。"]

    # 直近で使った語尾(tail)・頭(head)は連続で使わない。全滅時は全候補から。
    tail_pool = [t for t in tail if t not in recent] or tail
    head_pool = [x for x in head if x not in recent] or head
    return rng.choice(head_pool) + "\n" + rng.choice(tail_pool)


def build_post(race, blocks, mode) -> str:
    import random
    stadium = STADIUMS.get(int(race["race_stadium_number"]), "不明")
    rno = int(race["race_number"])
    closed = race["race_closed_at"][11:16]
    boats = sorted(race["boats"], key=score_boat, reverse=True)

    def ln(b):
        return int(b.get("racer_boat_number") or 0)

    def rn(b):
        return str(b.get("racer_name") or "").replace("　", "")

    circ = "①②③④⑤⑥"

    def cz(n):
        return circ[n - 1] if 1 <= n <= 6 else (str(n) + "号艇")

    h = boats[0]
    o = boats[1] if len(boats) > 1 else h
    a = boats[2] if len(boats) > 2 else h
    f4b = boats[3] if len(boats) > 3 else h
    hn, on, an, f4 = ln(h), ln(o), ln(a), ln(f4b)
    rng = random.Random(str(race.get("race_stadium_number")) + "-" + str(rno) + "-" + closed)

    if mode == "中穴":
        reads = [
            "ここは中穴狙い。" + cz(on) + rn(o) + "の一発に期待します。",
            "荒れ含みとみて中穴勝負。軸は" + cz(on) + rn(o) + "です。",
            "中穴ねらいで。" + cz(on) + rn(o) + "の頭まで見ています。",
            "波乱に備えて中穴。主役は" + cz(on) + rn(o) + "。",
            "一発あるとみて中穴勝負、" + cz(on) + rn(o) + "から狙います。",
        ]
        aite = [
            "相手は本命" + cz(hn) + "と" + cz(an) + "を中心に組みます。",
            "2・3着は" + cz(hn) + cz(an) + "を軸に手広く。",
            "ヒモは" + cz(hn) + "と" + cz(an) + "、展開カバーで広めに。",
            "連下は" + cz(hn) + cz(an) + "中心、押さえも添えて。",
        ]
        l1 = rng.choice(reads)
        l2 = rng.choice(aite)
    else:
        honmei = [
            "正直かなり堅いと思う。インからならそうそう負けないかなと。",
            "普通に本命でいいでしょ。地力がひとつ抜けてるし。",
            "頭から入るよ。スタートも安定してて、崩れる感じがしない。",
            "まあ堅いかな。インの信頼度はけっこう高いと思う。",
            "軸はこの艇でいいでしょ。展開も向きそうだしね。",
            "ど本命でいく。能力的に抜けてる一戦だと思うよ。",
            "素直に逃げ切り狙いで。無理に広げなくていいかなと。",
            "まず頭は堅いと思う。ここは信頼でいいでしょ。",
            "本命は動かさないよ。スタート勘もいいしね。",
            "迷わず頭から。地力でははっきり上だと思う。",
            "信頼でいいと思う。差しやまくりの気配も薄いしね。",
            "インから押し切る形が本線。素直に軸でいくよ。",
            "ここは逃がさないとみてる。スタートが決まれば一番強いしね。",
            "普通にアタマでいいと思うよ。実力が違うなって感じ。",
            "展開的にも逃げやすいかな。思い切って本命でいく。",
            "正直、負ける画が浮かばないんだよね。素直に頭。",
            "インが仕事するレースだと思う。信頼して軸に置くよ。",
            "ここは手堅くいきたい。本命の安定感が光るしね。",
            "スタート見てても不安がない。迷わず頭から入る。",
            "地力上位は明らかかな。ここは本命中心で問題ないでしょ。",
            "無理せず本命でいいと思う。変に穴狙う場面じゃないかな。",
            "逃げの型がはっきりしてる。素直に頭で勝負するよ。",
            "ここは信頼一択かな。崩れる要素が見当たらない。",
            "まず飛ばないとみてる。本命をどっしり軸にするよ。",
            "能力でねじ伏せる形かな。頭は堅いと思う。",
            "スタートの安定感が段違い。本命から入って正解でしょ。",
            "ここは逃げ切ると踏んでる。素直に本命を信頼。",
            "展開が向きそうだしね。迷わず頭に据えるよ。",
            "正直、本命のインが一番こわい存在。頭から入る。",
            "手堅く本命でいいかな。欲張る必要はないと思う。",
            "インの型が決まれば楽勝だと思う。素直に軸。",
            "ここは実力通りに決まるとみてる。本命で。",
            "逃げ足はしっかりあるしね。頭は動かさないよ。",
            "普通に考えて本命が中心でしょ。信頼していく。",
            "ここは堅く構えたい。本命のスタートがいいしね。",
            "崩れる感じがしないんだよね。素直に頭から。",
            "まず軸は本命で固定。あとは相手探しかな。",
            "インから逃げ切る形が濃厚。本命を信頼するよ。",
            "地力ではっきり上だと思う。頭は堅いかな。",
            "ここは本命の信頼度が高い。迷わず軸にするよ。",
            "スタート勘もいいし、逃げると踏んでる。頭で。",
            "実力的に抜けてる一戦かな。素直に本命でいく。",
            "無難に本命でいいと思うよ。妙な穴はいらない。",
            "逃げの本線は動かさない。ここは信頼でいく。",
            "ここは順当に決まるとみてる。本命を軸に。",
            "インが決まれば一番強い。素直に頭から入るよ。",
            "本命の安定感を信じていく。崩れないと思う。",
            "まず頭固定でいいかな。相手を少し探る感じ。",
            "ここは逃げ切り濃厚とみた。本命で勝負するよ。",
            "能力上位は明らか。頭は堅いと思うよ。",
            "スタートに不安がないしね。本命を信頼する。",
            "素直に逃げ切り本線でいく。変に広げない。",
            "ここは本命が格上かな。迷わず軸にするよ。",
            "逃げの型がハマると楽。頭から入って正解でしょ。",
            "まず飛ばないと踏んでる。本命をどっしり据える。",
            "ここは実力差を信じたい。本命中心でいくよ。",
            "インの信頼度が高い一戦。素直に頭で。",
            "展開も向きそうだしね。本命を軸に固定。",
            "ここは堅く本命でいいでしょ。穴はいらないかな。",
            "スタートが決まれば逃げると思う。頭から。",
            "地力で押し切る形かな。本命を信頼していく。",
            "無理に崩さなくていいと思う。素直に頭。",
            "ここは本命の逃げを信じる。崩れないとみてる。",
            "実力ははっきり上。迷わず軸に据えるよ。",
            "逃げ足しっかりだしね。頭は動かさない。",
            "ここは順当決着とみてる。本命で手堅く。",
            "スタート安定してるし、逃げ切ると思う。頭。",
            "まず本命軸で固定かな。相手を少し広げる程度。",
            "インから押し切る本線。素直に信頼でいくよ。",
            "ここは本命が抜けてるかな。頭は堅いと思う。",
            "崩れる要素が薄いしね。本命をどっしり軸に。",
            "能力的に中心でいいでしょ。素直に頭から入る。",
            "ここは逃げ切り本命とみた。信頼して勝負するよ。",
            "スタート勘が冴えてる。頭は動かさないかな。",
            "無難に本命でいいと思う。欲張らずいくよ。",
            "逃げの型が決まれば強い。素直に軸でいく。",
            "ここは実力通りに決まるかな。本命中心で。",
            "インの信頼度は高いしね。迷わず頭に据える。",
            "まず飛ばないと踏んでる。本命を軸に固定するよ。",
            "展開も向くとみてる。頭から入って問題ないでしょ。",
            "ここは堅実に本命でいく。穴は狙わないかな。",
            "スタートに隙がないしね。逃げ切ると思う。頭。",
            "地力上位は明らかかな。素直に本命を信頼。",
            "逃げ本線は動かさないよ。ここは信頼でいく。",
            "ここは順当に本命かな。手堅く軸に置くよ。",
            "インが仕事するとみてる。頭から入って正解でしょ。",
            "能力でねじ伏せる一戦。素直に本命でいく。",
            "ここは本命の逃げを信じたい。崩れないとみた。",
            "スタート安定だしね。迷わず頭に固定するよ。",
            "まず軸は本命で決まり。あとは相手を探る。",
            "逃げ切り濃厚とみてる。本命を信頼して勝負。",
            "ここは実力差がはっきり。頭は堅いと思うよ。",
            "無理せず本命中心でいく。妙な色気は出さない。",
            "インの型がハマれば楽勝かな。素直に頭から。",
            "ここは本命が格上とみた。迷わず軸にするよ。",
            "スタート勘もいいしね。逃げると踏んでる。頭。",
            "地力で押し切る形が濃厚。本命を信頼でいく。",
            "ここは堅く構えて本命。崩れる感じがしない。",
            "まず頭は本命で固定かな。相手を少し広げる。",
            "素直に逃げ切り本線でいくよ。ここは信頼で。",
        ]
        aite = [
            "相手は" + cz(on) + cz(an) + "を厚めに、" + cz(f4) + "は一応押さえで。",
            "ヒモは" + cz(on) + cz(an) + "中心かな。" + cz(f4) + "だけ展開次第で。",
            "2着は" + cz(on) + cz(an) + "どっちでもいいように、薄く" + cz(f4) + "も。",
            "相手の筆頭は" + cz(on) + "、あとは" + cz(an) + cz(f4) + "かな。",
            cz(on) + cz(an) + "を軸に、欲張らず" + cz(f4) + "まで。",
            "ヒモは" + cz(on) + cz(an) + "メインで、" + cz(f4) + "は保険くらい。",
            "相手は" + cz(on) + cz(an) + "、" + cz(f4) + "は一発あるかもくらいで押さえとく。",
            "2着筆頭は" + cz(on) + "だと思う。" + cz(an) + cz(f4) + "も一応ね。",
            "連下は" + cz(on) + cz(an) + "本線で、" + cz(f4) + "は薄めに添える感じ。",
            "相手探しは" + cz(on) + cz(an) + "から。" + cz(f4) + "は展開次第かな。",
            "ヒモ本線は" + cz(on) + cz(an) + "、" + cz(f4) + "は一応おさえておくよ。",
            "2着は" + cz(on) + "を本線に、" + cz(an) + cz(f4) + "を手広く。",
            "相手は堅く" + cz(on) + cz(an) + "、" + cz(f4) + "は余裕があれば。",
            cz(on) + "と" + cz(an) + "を中心に、薄く" + cz(f4) + "まで握るよ。",
            "ヒモは" + cz(on) + cz(an) + "で十分かな。" + cz(f4) + "は気持ち程度。",
            "2着候補は" + cz(on) + cz(an) + "、あと一応" + cz(f4) + "ね。",
            "相手筆頭は" + cz(on) + "とみてる。" + cz(an) + cz(f4) + "も押さえで。",
            "連下は" + cz(on) + cz(an) + "を厚く、" + cz(f4) + "は保険的に。",
            "ヒモは" + cz(on) + cz(an) + "重視で、" + cz(f4) + "は一応加えとく。",
            "相手は" + cz(on) + cz(an) + "、無理なら" + cz(f4) + "まで広げる。",
            "2着は" + cz(on) + "本線、" + cz(an) + "対抗、" + cz(f4) + "は押さえ。",
            cz(on) + cz(an) + "を主体に、" + cz(f4) + "は一発ケアで。",
            "ヒモ筆頭は" + cz(on) + "、続いて" + cz(an) + cz(f4) + "かな。",
            "相手は" + cz(on) + cz(an) + "厚めに、" + cz(f4) + "は薄く拾う程度。",
            "2着は" + cz(on) + cz(an) + "で取りたい。" + cz(f4) + "は一応ね。",
            "連下本線は" + cz(on) + cz(an) + "、" + cz(f4) + "は展開ハマればくらい。",
            "相手は" + cz(on) + "中心に、" + cz(an) + cz(f4) + "を手広く構える。",
            "ヒモは" + cz(on) + cz(an) + "、" + cz(f4) + "は保険で一応押さえる。",
            "2着候補の筆頭は" + cz(on) + "。" + cz(an) + cz(f4) + "も薄く。",
            cz(on) + "を軸の相手に、" + cz(an) + cz(f4) + "まで一応ね。",
            "相手は堅めに" + cz(on) + cz(an) + "、" + cz(f4) + "は余力で。",
            "ヒモは" + cz(on) + cz(an) + "を本線、" + cz(f4) + "は一応添える。",
            "2着は" + cz(on) + cz(an) + "どっちもケア、" + cz(f4) + "は薄め。",
            "相手筆頭に" + cz(on) + "、対抗" + cz(an) + "、押さえ" + cz(f4) + "で。",
            "連下は" + cz(on) + cz(an) + "中心、" + cz(f4) + "は一発警戒で握る。",
            "ヒモ本線は" + cz(on) + cz(an) + "、" + cz(f4) + "は展開次第で拾う。",
            "相手は" + cz(on) + cz(an) + "、" + cz(f4) + "は一応おさえる形で。",
            "2着は" + cz(on) + "本命視、" + cz(an) + "も、" + cz(f4) + "は薄く。",
            cz(on) + cz(an) + "を厚めに握って、" + cz(f4) + "は保険。",
            "ヒモは" + cz(on) + cz(an) + "で勝負、" + cz(f4) + "は気持ち足す程度。",
            "相手は" + cz(on) + "を筆頭に、" + cz(an) + cz(f4) + "を広めに。",
            "2着候補は" + cz(on) + cz(an) + "、" + cz(f4) + "は一応ケアしとく。",
            "連下は" + cz(on) + cz(an) + "本線で、" + cz(f4) + "は薄く添える。",
            "ヒモ筆頭は" + cz(on) + "とみる。" + cz(an) + cz(f4) + "も一応ね。",
            "相手は" + cz(on) + cz(an) + "、余裕あれば" + cz(f4) + "まで。",
            "2着は" + cz(on) + cz(an) + "で決めたい、" + cz(f4) + "は保険的に。",
            cz(on) + "と" + cz(an) + "を主軸、" + cz(f4) + "は一発あるかもで押さえ。",
            "ヒモは" + cz(on) + cz(an) + "中心に、" + cz(f4) + "だけ薄く握る。",
            "相手筆頭は" + cz(on) + "、あとは" + cz(an) + "、" + cz(f4) + "も一応。",
            "連下本線は" + cz(on) + cz(an) + "、" + cz(f4) + "は展開ハマれば。",
            "2着は" + cz(on) + "を本線、" + cz(an) + "対抗で、" + cz(f4) + "は押さえ。",
            "相手は" + cz(on) + cz(an) + "を厚く、" + cz(f4) + "は薄めに拾うよ。",
            "ヒモは" + cz(on) + cz(an) + "、" + cz(f4) + "は保険として一応。",
            cz(on) + cz(an) + "を中心に組んで、" + cz(f4) + "まで一応ケア。",
            "2着候補は" + cz(on) + "筆頭、" + cz(an) + cz(f4) + "を手広く。",
            "相手は堅く" + cz(on) + cz(an) + "、" + cz(f4) + "は余裕枠で。",
            "連下は" + cz(on) + cz(an) + "メイン、" + cz(f4) + "は一応添える感じ。",
            "ヒモ本線は" + cz(on) + "と" + cz(an) + "、" + cz(f4) + "は薄く。",
            "相手は" + cz(on) + cz(an) + "、" + cz(f4) + "は一発ケアで押さえとく。",
            "2着は" + cz(on) + cz(an) + "どっちでも、" + cz(f4) + "は保険で。",
            cz(on) + "を軸相手に、" + cz(an) + "も、" + cz(f4) + "は薄く拾う。",
            "ヒモは" + cz(on) + cz(an) + "厚め、" + cz(f4) + "は展開次第で。",
            "相手筆頭は" + cz(on) + "、続いて" + cz(an) + "、押さえ" + cz(f4) + "。",
            "連下は" + cz(on) + cz(an) + "本線に、" + cz(f4) + "は一応加える。",
            "2着は" + cz(on) + cz(an) + "で取りにいく、" + cz(f4) + "は薄め。",
            "相手は" + cz(on) + "中心、" + cz(an) + cz(f4) + "を広めに握るよ。",
            "ヒモは" + cz(on) + cz(an) + "で十分、" + cz(f4) + "は気持ちだけ。",
            cz(on) + cz(an) + "を主体に、余力で" + cz(f4) + "も押さえる。",
            "2着候補の筆頭は" + cz(on) + "。" + cz(an) + "対抗、" + cz(f4) + "は薄く。",
            "相手は堅めに" + cz(on) + cz(an) + "、" + cz(f4) + "は保険的に。",
            "ヒモ本線は" + cz(on) + cz(an) + "、" + cz(f4) + "は一発あればで。",
            "連下は" + cz(on) + cz(an) + "厚く、" + cz(f4) + "は薄く添える。",
            "2着は" + cz(on) + "を本命視、" + cz(an) + cz(f4) + "も一応ね。",
            "相手は" + cz(on) + cz(an) + "、無理せず" + cz(f4) + "まで握る。",
            cz(on) + "と" + cz(an) + "を中心、" + cz(f4) + "は一応ケアしとく。",
            "ヒモは" + cz(on) + cz(an) + "メインで、" + cz(f4) + "は保険くらいかな。",
            "相手筆頭は" + cz(on) + "とみてる。" + cz(an) + cz(f4) + "も薄く拾う。",
            "2着は" + cz(on) + cz(an) + "で決めたい、" + cz(f4) + "は一応。",
            "連下本線は" + cz(on) + cz(an) + "、" + cz(f4) + "は展開次第で。",
            "相手は" + cz(on) + cz(an) + "を厚めに握る、" + cz(f4) + "は薄く。",
            "ヒモは" + cz(on) + "筆頭、" + cz(an) + "続き、" + cz(f4) + "は押さえ。",
            cz(on) + cz(an) + "を軸に手広く、" + cz(f4) + "は保険で添える。",
            "2着候補は" + cz(on) + cz(an) + "、" + cz(f4) + "は一発警戒で。",
            "相手は堅く" + cz(on) + "、" + cz(an) + "も、" + cz(f4) + "は余裕あれば。",
            "連下は" + cz(on) + cz(an) + "中心に、" + cz(f4) + "は薄めで握るよ。",
            "ヒモ本線は" + cz(on) + "と" + cz(an) + "、" + cz(f4) + "は一応ね。",
            "相手は" + cz(on) + cz(an) + "、" + cz(f4) + "は展開ハマれば押さえる。",
            "2着は" + cz(on) + "本線、" + cz(an) + "対抗、" + cz(f4) + "薄く。",
            cz(on) + "を軸相手に、" + cz(an) + cz(f4) + "まで広めに構える。",
            "ヒモは" + cz(on) + cz(an) + "厚めで、" + cz(f4) + "は保険的に加える。",
            "相手筆頭は" + cz(on) + "、あと" + cz(an) + "、" + cz(f4) + "も一応ね。",
            "連下は" + cz(on) + cz(an) + "本線、" + cz(f4) + "は一発あるかもで。",
            "2着は" + cz(on) + cz(an) + "で取りたいね、" + cz(f4) + "は薄め。",
            "相手は" + cz(on) + "中心に、" + cz(an) + "も、" + cz(f4) + "は押さえで。",
            "ヒモは" + cz(on) + cz(an) + "、余力で" + cz(f4) + "まで握る。",
            cz(on) + cz(an) + "を主体に、" + cz(f4) + "は気持ち足す程度で。",
            "2着候補の筆頭は" + cz(on) + "。" + cz(an) + cz(f4) + "を手広く拾う。",
            "相手は堅めに" + cz(on) + cz(an) + "、" + cz(f4) + "は保険くらい。",
            "連下本線は" + cz(on) + cz(an) + "、" + cz(f4) + "は展開次第で薄く。",
            "相手は" + cz(on) + cz(an) + "厚めに、" + cz(f4) + "は一応おさえておく。",
        ]
        l1 = "ここは" + cz(hn) + rn(h) + "、" + rng.choice(honmei)
        l2 = rng.choice(aite)

    lines = [stadium + str(rno) + "R " + closed + "〆", "", l1, l2, ""]
    lines.append("本線")
    lines.append(blocks["honsen_disp"])
    if blocks.get("shibori_disp"):
        lines.append("絞り")
        lines.append(blocks["shibori_disp"])
    if blocks.get("osae_lines"):
        lines.append("抑え")
        for ol in blocks["osae_lines"]:
            lines.append(ol)
    lines.append("")

    close1 = ["的中重視でいきます🎯", "今日も的中重視で勝負🎯", "狙うは的中、堅実にいきます🎯", "的中第一でいきます🎯", "手堅く的中を取りにいきます🎯", "今日も的中狙いで一本🎯", "まずは的中、そこを最優先で🎯", "ここは的中を取りにいきます🎯", "的中率重視で組みました🎯", "欲張らず、的中最優先で🎯", "今日も確実に的中を狙います🎯", "的中にこだわって勝負します🎯", "派手さより的中、堅くいきます🎯", "きっちり的中を拾いにいきます🎯", "的中重視、ここは固めで🎯", "まず当てる、それが全てです🎯", "的中を積み重ねていきます🎯", "今日も的中ファーストで🎯", "手堅さ優先、的中狙いで🎯", "ここは的中率で勝負します🎯", "的中重視のスタイルでいきます🎯", "堅実に的中を取りにいきます🎯", "今日も的中にこだわります🎯", "当てにいく、それだけです🎯", "的中最優先、堅く構えます🎯", "まずは当てることを大事に🎯", "的中狙いでじっくりいきます🎯", "今日も的中を取りこぼさず🎯", "ここは確実性重視でいきます🎯", "的中第一、手堅くまとめました🎯", "当てる予想を心がけてます🎯", "的中にこだわり抜いて組みました🎯", "今日も堅実路線でいきます🎯", "的中率を大事に勝負します🎯", "まず当てる、欲はそのあとで🎯", "ここは的中最優先でまとめます🎯", "的中狙い、無理はしません🎯", "今日も的中をしっかり狙います🎯", "堅く的中を拾っていきます🎯", "的中重視で手堅く構えました🎯", "当てにいく一本、組めました🎯", "ここは的中を最優先に🎯", "的中第一主義でいきます🎯", "今日も確実に取りにいきます🎯", "的中を信じて勝負します🎯", "手堅く、まず的中を🎯", "的中狙いで丁寧に組みました🎯", "ここは堅実に当てにいきます🎯", "的中最優先、ブレずにいきます🎯", "今日も的中を狙い撃ちます🎯", "当てる予想に徹します🎯", "的中率重視、派手さは二の次🎯", "ここは的中を取りにいく一本🎯", "的中第一で組み立てました🎯", "今日も手堅く的中狙い🎯", "まず当てにいく姿勢でいきます🎯", "的中にこだわった買い目です🎯", "堅実に的中を積みます🎯", "ここは的中優先で勝負します🎯", "的中狙いでしっかり構えました🎯", "今日も的中を最優先に組みました🎯", "当てることを第一に考えてます🎯", "的中重視、堅くいきましょう🎯", "ここは確実に拾いにいきます🎯", "的中狙いで無難にまとめました🎯", "今日も的中を大事に勝負🎯", "手堅さ重視で組んだ一本です🎯", "的中第一、ここは勝負します🎯", "当てにいく、迷いはありません🎯", "的中率を意識して組みました🎯", "ここは的中狙いで固めました🎯", "今日も的中ファーストでいきます🎯", "的中にこだわり勝負します🎯", "堅実路線で的中を狙います🎯", "まず的中、そこは外せません🎯", "的中重視で一本に絞りました🎯", "ここは的中最優先で勝負🎯", "今日も確実性を大事にします🎯", "的中狙い、手堅くいきます🎯", "当てる予想を届けます🎯", "的中第一で丁寧に組みました🎯", "ここは的中を取りにいく勝負🎯", "今日も的中重視で構えます🎯", "堅く的中を狙っていきます🎯", "的中率重視で勝負に出ます🎯", "まず当てにいく、それが基本🎯", "的中にこだわった一戦です🎯", "ここは手堅く的中狙いで🎯", "今日も的中最優先でいきます🎯", "的中を信じて手堅く勝負🎯", "当てにいく買い目に仕上げました🎯", "的中第一、派手さは求めません🎯", "ここは的中を積みにいきます🎯", "今日も的中狙いで堅実に🎯", "的中重視でブレずに勝負します🎯", "堅実に、まずは的中を🎯", "的中率を大事に組み立てました🎯", "ここは的中優先で手堅く🎯", "的中を第一に手堅くいきます🎯", "当てる予想でいきます🎯"]
    close2 = ["自信のある一戦です。", "ここは勝負どころとみています。", "しっかり獲りにいきます。", "期待度は高めです。", "納得の買い目が組めました。", "手応えのある一戦です。", "狙いははっきりしています。", "展開も読みやすい一戦です。", "ここは信頼度が高いとみます。", "自信を持って出せる買い目です。", "軸がしっかりした一戦です。", "ここは堅いとみています。", "狙い澄ました買い目です。", "読みに自信があります。", "手応えは十分です。", "ここは期待できる一戦かなと。", "組み立てに納得しています。", "自信度は高い一本です。", "展開が噛み合いそうです。", "ここは分かりやすい一戦です。", "狙いどころがはっきりしてます。", "軸の信頼度が高い一戦です。", "手堅くまとまったと思います。", "ここは勝算ありとみてます。", "読み筋に自信があります。", "いい買い目が組めた手応えです。", "ここは本命の信頼が厚いです。", "展開も向きそうな一戦です。", "自信を持ってお届けします。", "狙いはシンプルで明快です。", "ここは手応え十分の一本です。", "軸がブレない一戦だと思います。", "読みやすさが際立つ一戦です。", "ここは期待値が高いとみます。", "納得いく形に組めました。", "自信のある本線に仕上げました。", "展開が読める分、やりやすいです。", "ここは勝負する価値ありとみます。", "手応えのある買い目です。", "狙いが定まった一戦です。", "ここは信頼して出せます。", "軸の安定感がある一戦です。", "読み通りに運びそうな気配です。", "自信度高めでお届けします。", "ここはまとまりのいい一戦です。", "期待の持てる組み立てです。", "狙いははっきり絞れました。", "ここは手堅い一本になりました。", "本線の信頼が厚い一戦です。", "展開面でも分がありそうです。", "ここは勝負どころと感じます。", "自信を持って推せる買い目です。", "軸がしっかり立った一戦です。", "読みに手応えを感じています。", "ここは期待できる組み立てです。", "納得の一本に仕上がりました。", "狙いが明確な一戦です。", "ここは信頼度高めとみます。", "手応えは申し分ないです。", "展開が味方しそうな一戦です。", "ここは自信のある本線です。", "軸の信頼が厚い一本です。", "読みやすく勝負しやすい一戦です。", "期待度は高いとみています。", "ここは手堅くいける一戦です。", "狙いを絞り込めた買い目です。", "自信を持って臨める一戦です。", "本命の信頼感がある一戦です。", "展開次第ですが手応えありです。", "ここは勝算を感じる一本です。", "読み筋がはっきりしています。", "納得のいく狙いが定まりました。", "ここは期待の一戦とみます。", "軸がしっかりしていて安心です。", "手応えのある組み立てになりました。", "狙いどころを押さえた一戦です。", "ここは信頼して勝負できます。", "自信度の高い買い目です。", "展開が読みやすく助かります。", "ここは手堅さが光る一戦です。", "本線に自信を持っています。", "軸の安定感が心強い一戦です。", "読みに確かな手応えがあります。", "期待値の高い一本とみます。", "ここは勝負しがいのある一戦です。", "納得の買い目に落ち着きました。", "狙いが定まって気持ちいいです。", "ここは信頼できる本線です。", "手応え十分の組み立てです。", "展開も向く見込みの一戦です。", "ここは自信を持って出せます。", "軸がブレず安心感があります。", "読みやすい展開とみています。", "期待度高めの一戦です。", "ここは手堅くまとめられました。", "狙いははっきりしていると思います。", "本命の信頼が厚い一本です。", "ここは勝算ありと感じています。", "自信のこもった買い目です。", "手応えのある納得の一戦です。"]
    close3 = ["舟券は自己責任で🙏", "参考になれば嬉しいです🙏", "一緒に獲りましょう🔥", "応援よろしくお願いします🙏", "良い結果を期待して🔥", "無理のない範囲で楽しみましょう🙏", "最後は自己判断でお願いします🙏", "参考程度にどうぞ🙏", "当たりますように🙏", "一緒に的中目指しましょう🔥", "ご武運を祈ります🙏", "良いレースになりますように🙏", "楽しんでいきましょう🔥", "無理せず楽しみましょう🙏", "今日も一緒に勝負しましょう🔥", "的中を願っています🙏", "参考にしていただければ🙏", "余裕を持って楽しみましょう🙏", "一緒に盛り上がりましょう🔥", "ご自身の判断も大切に🙏", "良い一日になりますように🙏", "当たったら一緒に喜びましょう🔥", "舟券はほどほどに🙏", "的中を信じていきましょう🔥", "どうぞ参考までに🙏", "無茶な賭けは控えめに🙏", "今日も的中いきたいですね🔥", "落ち着いて楽しみましょう🙏", "ご自愛しつつ楽しんで🙏", "一緒に的中を掴みましょう🔥", "舟券は余裕資金で🙏", "良い勝負になりますように🙏", "ともに的中を目指して🔥", "深追いはほどほどに🙏", "今日も楽しんでいきましょう🔥", "参考になれば幸いです🙏", "最後はご自身で判断を🙏", "一緒に当てにいきましょう🔥", "無理のない勝負で🙏", "的中を祈っています🙏", "気楽に楽しんでください🔥", "当たりを願っています🙏", "計画的に楽しみましょう🙏", "一緒に喜べたら嬉しいです🔥", "ご参考までにどうぞ🙏", "今日も良い結果を🔥", "舟券はゆとりを持って🙏", "一緒に的中を狙いましょう🔥", "無理せず続けましょう🙏", "良いレースを楽しみましょう🔥", "ご武運をお祈りします🙏", "的中、期待しています🔥", "参考にどうぞ、無理なく🙏", "今日も一緒に楽しみましょう🔥", "落ち着いて勝負しましょう🙏", "当たりますように願って🙏", "ともに盛り上がりましょう🔥", "ほどほどに楽しみましょう🙏", "一緒に的中届けましょう🔥", "ご自身のペースで楽しんで🙏", "良い一戦になりますように🙏", "今日も的中を信じて🔥", "舟券は無理のない範囲で🙏", "一緒に勝ちにいきましょう🔥", "どうか参考までに🙏", "当たったら教えてください🔥", "計画的な勝負を🙏", "的中を目指して一緒に🔥", "気負わず楽しみましょう🙏", "今日も良い結果を願って🔥", "余裕を持った勝負で🙏", "一緒に喜びを分かち合いましょう🔥", "参考になりますように🙏", "最後は自己責任でお願いします🙏", "ともに的中を掴みにいきましょう🔥", "無理な賭けは禁物です🙏", "今日も一緒に狙いましょう🔥", "落ち着いて楽しんでください🙏", "当たりを信じていきましょう🔥", "ご自愛しながら勝負を🙏", "良い結果になりますように🙏", "一緒に的中を味わいましょう🔥", "舟券はほどよく楽しんで🙏", "今日も的中へ向けて🔥", "ご参考にしていただければ🙏", "無理せず気楽にいきましょう🙏", "ともに勝負を楽しみましょう🔥", "的中を心から願っています🙏", "一緒に良い一日にしましょう🔥", "余裕資金で楽しみましょう🙏", "当たることを祈っています🙏", "今日も一緒に的中を🔥", "ご自身の判断を大切に🙏", "良い勝負を楽しんでください🔥", "舟券は計画的に🙏", "ともに的中目指していきましょう🔥", "どうぞご参考までに🙏", "無理のない範囲で勝負を🙏", "一緒に盛り上がれたら🔥", "今日も的中を願っています🙏"]
    lines.append(rng.choice(close1))
    lines.append(rng.choice(close2))
    lines.append(rng.choice(close3))

    race["_reason"] = l1 + " " + l2
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
        "reason": race.get("_reason", ""),
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


def _a1_osae(race, h, t, s, f4):
    """的中重視の抑え: ①号艇がA1 かつ 他にもA1選手がいる場合だけ、
    もう1人のA1艇を頭・◎(①)を2着にした薄い保険2点を返す。
    条件を満たさなければ (None, None)。"""
    if h != 1:
        return None, None
    boats = race.get("boats", [])

    def _num(b):
        return int(b.get("racer_boat_number") or 0)

    def _is_a1(b):
        return int(b.get("racer_class_number") or 4) == 1

    boat1 = next((b for b in boats if _num(b) == 1), None)
    if boat1 is None or not _is_a1(boat1):
        return None, None
    others = sorted(
        (b for b in boats if _num(b) != 1 and _is_a1(b)),
        key=score_boat, reverse=True,
    )
    if not others:
        return None, None
    a1b = _num(others[0])
    thirds = [x for x in (t, s, f4) if x not in (1, a1b) and x != 0][:2]
    if len(thirds) < 2:
        return None, None
    osae_lines = [f"{a1b}-1-{mm(*thirds)}"]
    osae = _expand2(a1b, 1, thirds)
    return osae_lines, osae


def blocks_for(race, mode):
    lanes, _ = _lanes(race)
    h, t, s, f4, f5 = (lanes + [0, 0, 0, 0, 0])[:5]
    if mode == "中穴":
        return build_medium(h, t, s, f4, f5)
    tight = _clarity(race) >= 25
    blocks = build_solid(h, t, s, f4, f5, tight)
    osae_lines, osae = _a1_osae(race, h, t, s, f4)
    if osae_lines:
        # ①A1＋他A1: 本線を4点に絞り、絞りは省いて抑え2点を付ける
        blocks["honsen_disp"] = f"{h}-{mm(t, s)}-{mm(t, s, f4)}"
        blocks["honsen"] = _expand(h, [t, s], [t, s, f4])
        blocks["shibori_disp"] = ""
        blocks["shibori"] = []
        blocks["osae_lines"] = osae_lines
        blocks["osae"] = osae
    else:
        # 条件を満たさないレースは今まで通り(抑えなし)
        blocks["osae_lines"] = []
        blocks["osae"] = []
    return blocks


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
