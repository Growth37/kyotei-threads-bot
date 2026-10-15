#!/usr/bin/env python3
"""投稿の表示回数トラッキングと的中チェック.

- posts_log.json に記録された各投稿について
  1時間後 / 5時間後 / 1日後 の表示回数(views)をThreads APIから取得
- レース結果(Boatrace Open API)と照合し、6点の中に3連単の結果があれば
  的中投稿を自動でThreadsに出す
- 結果を metrics.csv / metrics.xlsx にまとめる

環境変数:
  THREADS_ACCESS_TOKEN Threads長期アクセストークン
"""

import csv
import json
import re
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
THREADS_API = "https://graph.threads.net/v1.0"
RESULTS_URL = "https://boatraceopenapi.github.io/results/v2/{y}/{ymd}.json"

LOG_FILE = "posts_log.json"
CSV_FILE = "metrics.csv"
XLSX_FILE = "metrics.xlsx"

SNAPSHOTS = [("1h", 1.0), ("5h", 5.0), ("24h", 24.0)]


def http_get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "kyotei-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))


def http_post(url: str, params: dict):
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))


def fetch_views(post_id: str, token: str):
    """投稿の表示回数(views)を取得。失敗時はNone."""
    try:
        qs = urllib.parse.urlencode({"metric": "views", "access_token": token})
        data = http_get_json(f"{THREADS_API}/{post_id}/insights?{qs}")
        for m in data.get("data") or []:
            if m.get("name") != "views":
                continue
            if isinstance(m.get("total_value"), dict) and "value" in m["total_value"]:
                return int(m["total_value"]["value"])
            values = m.get("values") or []
            if values and "value" in values[0]:
                return int(values[0]["value"])
    except Exception as e:  # noqa: BLE001
        print(f"  views取得失敗 ({post_id}): {e}")
    return None


_results_cache = {}


def fetch_results(race_date: str):
    """指定日のレース結果一覧を取得 (日付ごとにキャッシュ)."""
    if race_date in _results_cache:
        return _results_cache[race_date]
    ymd = race_date.replace("-", "")
    try:
        data = http_get_json(RESULTS_URL.format(y=ymd[:4], ymd=ymd))
        _results_cache[race_date] = data.get("results") or []
    except Exception as e:  # noqa: BLE001
        print(f"  結果取得失敗 ({race_date}): {e}")
        _results_cache[race_date] = []
    return _results_cache[race_date]


OFFICIAL_RESULT = "https://www.boatrace.jp/owpc/pc/race/raceresult?rno={rno}&jcd={jcd:02d}&hd={ymd}"


def http_get_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; kyotei-bot/1.0)"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read().decode("utf-8", errors="replace")


def _result_from_official(entry: dict):
    """無料APIに結果がまだ無い時、公式サイト(boatrace.jp)から3連単の結果を取る."""
    ymd = (entry.get("race_date") or "").replace("-", "")
    jcd = int(entry.get("stadium_number") or 0)
    rno = int(entry.get("race_number") or 0)
    if not (ymd and jcd and rno):
        return None
    try:
        html = http_get_html(OFFICIAL_RESULT.format(rno=rno, jcd=jcd, ymd=ymd))
    except Exception as e:  # noqa: BLE001
        print(f"  公式結果の取得失敗 ({jcd}-{rno}): {e}")
        return None
    i = html.find("3連単")
    if i < 0:
        return None
    seg = html[i:i + 1200]
    digits = re.findall(r"numberSet1_number[^>]*>\s*(\d)\s*<", seg)
    if len(digits) < 3:
        return None
    combo = f"{digits[0]}-{digits[1]}-{digits[2]}"
    pm = re.search(r'is-payout1"[^>]*>[^\d]*?([\d,]+)', seg)
    payout = int(pm.group(1).replace(",", "")) if pm else None
    print(f"  公式サイトから結果取得: {jcd}-{rno} {combo} ({payout}円)")
    return combo, payout


def find_race_result(entry: dict):
    """無料API→無ければ公式サイト の順で結果を取得する."""
    res = _result_from_api(entry)
    if res:
        return res
    return _result_from_official(entry)


def _result_from_api(entry: dict):
    """該当レースの結果から (3連単の組番, 配当) を返す。未確定ならNone."""
    for race in fetch_results(entry["race_date"]):
        if int(race.get("race_stadium_number") or 0) != int(entry["stadium_number"]):
            continue
        if int(race.get("race_number") or 0) != int(entry["race_number"]):
            continue
        places = {}
        for b in race.get("boats") or []:
            p = b.get("racer_place_number")
            if p in (1, 2, 3):
                places[int(p)] = int(b["racer_boat_number"])
        if len(places) < 3:
            return None  # まだ確定していない/欠場等
        combo = f"{places[1]}-{places[2]}-{places[3]}"
        payout = None
        tri = (race.get("payouts") or {}).get("trifecta") or []
        for t in tri:
            if t.get("combination") == combo:
                payout = t.get("payout")
                break
        return combo, payout
    return None


def get_user_id(token: str) -> str:
    qs = urllib.parse.urlencode({"fields": "id", "access_token": token})
    me = http_get_json(f"{THREADS_API}/me?{qs}")
    return me["id"]


def current_streak(log: list) -> int:
    """結果確定済みレースを時系列に並べ、直近から遡った連続的中数を返す."""
    resolved = [e for e in log if e.get("result")]
    resolved.sort(key=lambda x: x.get("race_closed_at") or "")
    streak = 0
    for e in reversed(resolved):
        if e.get("hit"):
            streak += 1
        else:
            break
    return streak


def fetch_permalink(post_id: str, token: str):
    """投稿のURL(permalink)を取得。失敗時はNone."""
    try:
        qs = urllib.parse.urlencode({"fields": "permalink", "access_token": token})
        data = http_get_json(f"{THREADS_API}/{post_id}?{qs}")
        return data.get("permalink")
    except Exception as e:  # noqa: BLE001
        print(f"  permalink取得失敗 ({post_id}): {e}")
        return None


def hit_comment(entry: dict, payout, streak: int) -> str:
    """的中の内容に合わせた関西弁の一言をつくる."""
    import random
    rng = random.Random(str(entry.get("post_id")))
    result = entry.get("result") or ""
    head = result.split("-")[0] if result else ""
    is_nerai = bool(entry.get("nerai")) and result in entry["nerai"]

    if is_nerai:
        base = rng.choice([
            f"{head}頭は狙い通りや！本命party には獲れへんやつやで",
            f"{head}のまくり、読み通りズバリでニヤけたわ",
            "中穴ゾーンどんぴしゃ！こういうのが獲れたら競艇は楽しいんよ",
        ])
        base = base.replace("party ", "党")
    else:
        base = rng.choice([
            "イン逃げ本線どおり、堅く獲ったで",
            "読み通りの決着や。素直が一番やね",
            "本線ドンピシャ！この積み重ねが大事なんよ",
        ])
    if payout:
        p = int(payout)
        if p >= 5000:
            base += rng.choice([f" しかも{p:,}円は美味すぎるやろ", " このオッズでこれは笑いが止まらんて"])
        elif p < 1000:
            base += rng.choice([" 配当は渋いけど当たりは正義や", " 安くても獲るもんは獲る、それが勝ちパターンや"])
    if streak >= 3:
        base += f" これで{streak}連勝、ゾーン入っとるかもしれん"
    return base


def post_hit(entry: dict, payout, token: str, user_id: str, streak: int = 1) -> bool:
    lines = []
    if streak >= 2:
        lines.append(f"🔥{streak}連勝中やで!!")
    import random
    _rng = random.Random(str(entry.get("post_id")) + "hithead")
    lines += [
        _rng.choice([
            "🎯的中や!!",
            "🎯キタ────!!",
            "🎯ズバリ的中や!!",
            "🎯獲ったで────!!",
            "🎯どんぴしゃ的中!!",
            "🎯やったで的中や!!",
            "🎯ピタリ当てたで!!",
            "🎯文句なしの的中や!!",
            "🎯きっちり獲ったで!!",
            "🎯読み通り的中や!!",
            "🎯しっかり的中!!",
            "🎯当たったで────!!",
            "🎯ドンピシャや!!",
            "🎯本命的中や!!",
            "🎯見事に的中!!",
            "🎯完璧に獲ったで!!",
            "🎯的中いただき!!",
            "🎯ばっちり的中や!!",
            "🎯狙い通り的中!!",
            "🎯やっぱ獲れたで!!",
            "🎯ここ獲ったで!!",
            "🎯気持ちええ的中や!!",
        ]),
        f"{entry['stadium']}{entry['race_number']}R 3連単 {entry['result']}",
    ]
    if payout:
        lines.append(f"配当 {int(payout):,}円")
    lines += ["", hit_comment(entry, payout, streak)]
    permalink = fetch_permalink(entry["post_id"], token)
    if permalink:
        lines += ["", "👇この予想やで", permalink]
    lines += ["", "※舟券は自己責任でな🙏", "#競艇 #ボートレース #競艇予想"]
    text = "\n".join(lines)
    try:
        c = http_post(
            f"{THREADS_API}/{user_id}/threads",
            {"media_type": "TEXT", "text": text, "access_token": token},
        )
        if not c.get("id"):
            print(f"  的中投稿コンテナ失敗: {c}")
            return False
        time.sleep(35)
        r = http_post(
            f"{THREADS_API}/{user_id}/threads_publish",
            {"creation_id": c["id"], "access_token": token},
        )
        if r.get("id"):
            print(f"  的中投稿完了! post id = {r['id']}")
            return True
        print(f"  的中投稿公開失敗: {r}")
    except Exception as e:  # noqa: BLE001
        print(f"  的中投稿エラー: {e}")
    return False


def write_csv(log: list):
    headers = [
        "投稿日", "投稿時刻", "場", "R", "締切", "予想6点",
        "結果(3連単)", "的中", "配当",
        "views 1時間後", "views 5時間後", "views 1日後",
    ]
    rows = []
    for e in sorted(log, key=lambda x: x["posted_at"]):
        views = e.get("views") or {}
        hit = ""
        if e.get("result"):
            hit = "◎的中" if e.get("hit") else "×"
        rows.append([
            e["posted_at"][:10], e["posted_at"][11:16],
            e["stadium"], e["race_number"], e["race_closed_at"][11:16],
            " / ".join(e.get("combos") or []),
            e.get("result") or "", hit,
            e.get("payout") or "",
            views.get("1h", ""), views.get("5h", ""), views.get("24h", ""),
        ])
    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        wb = Workbook()
        ws = wb.active
        ws.title = "投稿実績"
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="1F4E78")
        for r in rows:
            ws.append(r)
        widths = [11, 9, 8, 5, 8, 40, 12, 8, 10, 13, 13, 13]
        for i, wd in enumerate(widths, start=1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = wd
        wb.save(XLSX_FILE)
    except Exception as e:  # noqa: BLE001
        print(f"xlsx生成スキップ: {e}")


# ===== リプライ(結果を見た関西弁コメント / @r_no_yosou) =================
TECHNIQUE = {
    1: "逃げ", 2: "差し", 3: "まくり", 4: "まくり差し", 5: "抜き", 6: "恵まれ",
}


def _find_result_race(entry):
    """結果APIから該当レースのraw(選手名・ST・決まり手入り)を返す."""
    try:
        for race in fetch_results(entry.get("race_date") or ""):
            if int(race.get("race_stadium_number") or 0) != int(entry["stadium_number"]):
                continue
            if int(race.get("race_number") or 0) != int(entry["race_number"]):
                continue
            return race
    except Exception:  # noqa: BLE001
        return None
    return None


def _mult_str(payout):
    try:
        mult = int(payout) / 100
        return f"{mult:.1f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return ""


def _pred_head(entry):
    for combo in (entry.get("honsen") or []) + (entry.get("combos") or []):
        try:
            return int(str(combo).split("-")[0])
        except (ValueError, IndexError):
            continue
    return None


def _race_facts(entry, race):
    """LLMに渡すレース事実のテキストを組み立てる(@r_no_yosou用)."""
    boats = race.get("boats") or []
    by_place = {}
    st_map = {}
    name_map = {}
    for b in boats:
        n = int(b.get("racer_boat_number") or 0)
        nm = (b.get("racer_name") or "").replace("　", " ").strip()
        name_map[n] = nm
        p = b.get("racer_place_number")
        if p in (1, 2, 3):
            by_place[int(p)] = n
        st = b.get("racer_start_timing")
        if st is not None:
            st_map[n] = st

    def label(n):
        return (str(n) + "号艇" + name_map.get(n, "")).strip()

    order = " → ".join(
        label(by_place[p]) + "(" + str(p) + "着)" for p in (1, 2, 3) if p in by_place
    )
    head = _pred_head(entry)
    head_place = None
    if head:
        head_place = next(
            (int(b.get("racer_place_number"))
             for b in boats
             if int(b.get("racer_boat_number") or 0) == head and b.get("racer_place_number")),
            None,
        )
    tech = TECHNIQUE.get(int(race.get("race_technique_number") or 0), "")
    st_txt = "、".join(label(n) + " ST" + str(st_map[n]) for n in sorted(st_map))

    combo = entry.get("result") or ""
    mult = _mult_str(entry.get("payout"))
    hit = bool(entry.get("hit"))
    in_shibori = combo in (entry.get("shibori") or [])
    mode = entry.get("mode") or ""

    lines = [
        "会場: " + str(entry.get("stadium")) + str(entry.get("race_number")) + "R",
        "予想の狙い: " + (mode + "狙い。" if mode else "") + "本命(◎)は" + (str(head) + "号艇" if head else "不明") + "。買い目 " + str(len(entry.get("combos") or [])) + "点。",
        "決着: " + combo + "(" + mult + "倍) " + ("的中" if hit else "不的中"),
        "着順: " + order,
        "本命◎(" + (str(head) if head else "?") + "号艇)の着順: " + (str(head_place) + "着" if head_place else "不明"),
    ]
    if tech:
        lines.append("決まり手: " + tech)
    if st_txt:
        lines.append("ST: " + st_txt)
    if hit:
        if entry.get("shibori"):
            lines.append("※" + ("絞りにも入って的中。" if in_shibori else "絞りには入らず本線/抑えで的中。"))
        else:
            lines.append("※この買い目に絞りは無い(本線と抑えのみ)。『絞り』とは書かないこと。")
    return "\n".join(lines)


# --- 人間味を出すための指針(予想投稿・リプライ共通で意識する) ---------------
# (1)くだけ変換: 会話調に。AIっぽい言い回し・硬い構成は消す(意味は残す)
# (2)ベテラン筆致: 経験者が書いたような文のリズム・言葉選び。形式語は避ける
# (3)AI臭消し: AIが書いたと感じさせない。文の長さに変化を。盛らない
# (4)となりの先生: 仲のいい専門家が話すようなカジュアルな会話調。教科書調はカット
# (5)ぬくもり足し: 人間らしい感情と強弱を。単調な繰り返しをなくす
# (6)素のひとこと: 飾らない自然な言葉。核心だけストレートに
# (7)緩急つけ: 文の長さを変えてテンポ調整。先の読める言い回しはカット


def _llm_comment(entry, race, hit):
    """Gemini無料枠で関西弁コメントを生成(@r_no_yosou用)。失敗時はNone."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    facts = _race_facts(entry, race)
    style = (
        "あなたは競艇予想アカウント『@r_no_yosou』の中の人。関西の予想家で、"
        "軽妙なぼやき・ツッコミ・自虐まじりの、こてこての関西弁で話す(語尾は『〜や』『〜やで』『〜やなぁ』『〜へん』、『ほんま』『しゃあない』『〜してもうた』等を必ず使い、標準語には絶対にしない)。絵文字あり、必ず3〜4行で書く。毎回ちがう語り出し・言い回し・絵文字にして、同じ定型やテンプレは繰り返さない。"
        "不的中は怒るより『あちゃー』『しゃあない』系の関西のぼやき・ツッコミで(選手名はレース展開に絡めて出し、個人への人格攻撃にはしない)。"
        "的中は喜ぶが、トーンは状況で変える。絞り(核)で仕留めた時だけ『狙い通り』『ドンピシャ』と自信を出す。買い目が多い/絞りに入っていない広めの的中では『狙い通り』『計算通り』と自慢せず『広めに買って拾えた』と控えめにする。高配当(万舟級)の的中は『読み通り』ではなく『まさかの高配当』『嬉しい誤算』の驚き・幸運のトーンにする。艇番は①②③、選手名も出してよい。"
        "結果の事実(誰が来たか/本命の着順/STや決まり手)を必ず踏まえる。"
        "的中でも、本線が広い(買い目が多い)うえ低めの配当での的中は、ドヤらず控えめに書く。『狙い通り』『ついてきてよかった』のような自慢は、絞りでの的中か高配当のときだけ。万舟・高配当は素直に驚く。点数のわりに地味な的中を大げさに喜ばない。決め台詞の使い回しやbotっぽさを避け、毎回ちがう自然な言い回しにする。"
        "『ふざけとる』『なにしとんねん』『何してるの』等の強い罵倒口調は使わない。"
        "本命が1着でないときは『逃げ切った』『本命勝利』とは書かず、実際に1着だった艇を踏まえて書く。"
        "書き方は、経験者が会話調で崩して書いたように。AIっぽい型・硬い構成・形式的な言葉・先の読める言い回しは避け、文の長さに緩急をつけて単調な繰り返しをなくし、人間らしい温度を少し乗せる。中身は変えず盛らない。"
        "買い目の行(○-○-○や倍率)は書かない。コメント本文だけ返す。"
    )
    examples = (
        "例(不的中):\nあー①逃げ損ねたか…📉\nこら獲れんわ、しゃあない切り替えやで🙏\n\n"
        "例(不的中):\nまさかの⑤マクリとはなぁ💦\nこんなん読めるかいな🤯 また次いこ！\n\n"
        "例(的中):\nよっしゃ本命キッチリ‼️\n狙い通りやで、ええ感じやん😎\n\n"
        "例(的中):\nドンピシャやん🎯\nこういうの獲れると気持ちええわ〜🍺"
    )
    prompt = style + "\n\n" + examples + "\n\n--- 今回のレース ---\n" + facts + "\n\nコメント本文のみ:"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 1.2, "topP": 0.95, "maxOutputTokens": 320},
    }).encode("utf-8")
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.0-flash:generateContent?key=" + key)
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        txt = "".join(p.get("text", "") for p in parts).strip()
        return txt or None
    except Exception as ex:  # noqa: BLE001
        print("  コメント生成失敗: " + str(ex))
        return None


def _fallback_comment(entry, race, hit):
    """Gemini不発時のコメント。点数・配当・絞り・実際の1着艇に応じて内容を変える(@r_no_yosou)."""
    import random
    rng = random.Random(str(entry.get("post_id")) + ("h" if hit else "m") + "fb1e")
    combo = entry.get("result") or ""
    pts = len(entry.get("combos") or [])
    in_shibori = combo in (entry.get("shibori") or [])
    try:
        odds = int(entry.get("payout") or 0) / 100.0
    except (TypeError, ValueError):
        odds = 0.0
    nm = {}
    pl = {}
    winner = None
    for b in ((race.get("boats") if race else None) or []):
        n = int(b.get("racer_boat_number") or 0)
        nm[n] = (b.get("racer_name") or "").replace("　", " ").strip()
        p = b.get("racer_place_number")
        if p:
            pl[n] = int(p)
            if int(p) == 1:
                winner = n
    head = _pred_head(entry)
    won = (head is not None and winner == head)
    if hit:
        if odds >= 30:
            op = ["うお、これは獲れたか‼️🎯", "まさかの高配当キタ━━‼️", "いや〜これはデカい一本や🎯", "よもやの的中、びっくりやわ😳", "高いの引けたなぁ、ラッキー🎯", "これ獲れたんは嬉しい誤算や😳", "正直これは獲れると思てへんかった😳", "うわ、配当エグいの引いたで🎯", "こんなん獲れたら今日はええ日や😆", "まさかまさかの的中やで🎯"]
            cl = ["こういうのが獲れると痺れるわ🔥", "たまにはこんな日もええやろ😎", "次もどこかで一発狙うで🍺", "いや〜ようハマったわ✌️", "今日は祝杯やな🍺", "こういう当たりは記憶に残るわ😎"]
        elif in_shibori:
            op = ["絞りからズバッと的中✌️", "絞りが的中、気持ちええわ🎯", "絞った甲斐あったわ🎯", "狙い澄ました絞りで的中や🎯", "絞り的中、読み勝ちやな✌️", "よっしゃ、きっちり獲ったで🎯", "会心の的中やな🎯"]
            cl = ["この調子でいくで〜😎", "次もきっちり獲りにいく🔥", "ついてきてよかったやろ✌️", "ええ流れや、続けるで🍺", "自信持って出せた一本やった😎", "こういうの積み重ねるで🔥"]
        elif pts <= 6:
            op = ["よっしゃ、きっちり獲ったで🎯", "読み通りの的中や🎯", "手堅く的中や😎", "会心の的中やな🎯", "ピタッと決まったな✌️", "狙い通りに的中や🎯", "きっちり拾ったで✌️", "しっかり的中、気持ちええわ😎"]
            cl = ["この調子でいくで〜😎", "次もきっちり獲りにいく🔥", "ついてきてよかったやろ✌️", "ええ流れや、続けるで🍺", "自信持って出せた一本やった😎", "こういうの積み重ねるで🔥"]
        else:
            op = ["本線広めやったけど的中や🎯", "手堅く拾えた一本やな", "点数は多めやけど獲れたで", "まあ本線で拾えたわ🙆", "欲張らず的中、よしとこか", "広めに買うての的中、中身は地味やけどな", "薄めやけど的中は的中や", "無難に本線で収まったな", "点数かけてるぶん配当は地味やけどね", "取りこぼさんかっただけヨシや🙆"]
            cl = ["こつこつ当てていくわ", "次は中身でも勝負したいな", "地味でも的中は的中、よしや🙆", "次も手堅くいくで", "こういうのを外さんのが大事や", "派手さはないけど拾えてよかった"]
        if won and nm.get(head):
            mid = ["◎" + str(head) + "号艇" + nm[head] + "がきっちり来たで", "本命" + str(head) + "号艇" + nm[head] + "、ようやってくれた", str(head) + "号艇" + nm[head] + "、読みどおりの1着や", "展開も読み通りやった", "軸がブレずに決まったわ"]
        elif (not won) and winner and nm.get(winner):
            mid = ["本命は頭まで届かんかったけど、押さえが的中や", str(winner) + "号艇" + nm[winner] + "の1着をしっかり拾えたわ", "軸は連下やったけど、頭の" + str(winner) + "号艇" + nm[winner] + "を押さえてた", "頭は" + str(winner) + "号艇" + nm[winner] + "、押さえが効いたな", "本線やなかったけど買い目でカバーできたわ"]
        else:
            mid = ["展開は読み通りやった", "人気どころで堅う収まったな", "軸がブレずに決まったわ", "思た通りに収まったわ"]
    else:
        if odds >= 30:
            op = ["いや、こんな荒れは獲れんて😅", "まさかの高配当、これは仕方ないわ🤷", "こんな決着は狙ってへんしな💦", "荒れたなぁ、こればっかりは😮‍💨", "手の出しようがない一本やった😵", "これは誰も獲れへんやろ😅", "大荒れやな、ノーチャンスや🤷", "こんなん来たら脱帽やわ😮‍💨"]
        else:
            op = ["あちゃ〜、これは獲りたかったわ😣", "堅いとこやのに拾えんかった…反省や😩", "むむ、これ外すのは痛いわ💦", "手堅いとこやのに獲れんとは…😮‍💨", "うーん、獲れる内容やったのになぁ😣", "これは獲らなアカンやつやった😩", "取れるとこを落としたなぁ💦", "惜しい、あと一歩やったのに😣"]
        mid = ["展開が噛み合わんかったわ", "ここは読み切れんかったな", "絞り切れんかったのが悔やまれる", "一本に絞れてたらなぁ…", "読みがちょっとズレたわ"]
        if head and nm.get(head) and pl.get(head):
            mid += ["◎" + str(head) + "号艇" + nm[head] + "が" + str(pl[head]) + "着やもんな…", "本命" + str(head) + "号艇" + nm[head] + "が飛んだら厳しいわ"]
        if winner and winner != head and nm.get(winner):
            mid += [str(winner) + "号艇" + nm[winner] + "に持ってかれたわ…", str(winner) + "号艇" + nm[winner] + "が来るとはなぁ"]
        cl = ["次こそしっかり獲り返すで💪", "反省して立て直すわ🔥", "引きずらんと前向くで🙏", "次は獲るで、見ときや", "ここは修正して次や💪", "次はきっちり取りこぼさんようにするわ", "ここは組み直して出直しや", "もうちょい精度あげなアカンな", "こういうのを拾えるようになりたいわ", "次こそ結果で黙らせたる", "まあこういう日もあるわ、次次や", "気を取り直してもう一丁や🔥", "悔しいけど、前だけ向いてこ", "次のレースで取り返したるで💪", "切り替えが大事、はい次や", "こっからや、こっから🔥", "外れは忘れて次に集中や", "また一から組み立て直すわ", "こんなもんで凹んでられへん💪", "次は自信持って出せるようにするわ", "ここで腐らんと次いこ🙏", "悔しさは次の的中で晴らすで🔥", "まだまだこれから、巻き返すで", "ひとつ勉強になったわ、次に活かす", "次こそドンピシャ決めたるで🎯", "こればっかりはしゃあない、前向こ", "気持ち新たに次のレースや", "負けは負け、受け止めて次や", "次はもっと堅く攻めるわ", "ここから挽回していくで🔥", "また明日、ええ予想組むわ", "次で取り返す、約束や💪", "一回外しても続けることが大事や", "次はしっかり的中届けるで🎯", "ええ経験やと思て次いこ", "外した分、次で倍返しや🔥", "まだ勝負はこれからやで", "淡々と、また次に向かうわ", "ここで折れてられへん、続けるで", "次こそ気持ちよう当てたいわ", "反省は反省、引きずらんでいく", "もう次のこと考えてるわ、切り替え早いで😎", "こういう日を糧にするで🔥", "次はもっとええ買い目組んだる", "焦らず、次を取りにいこ", "前を向くしかないわ🙏", "ここから流れ変えていくで", "次でちゃんと帳尻合わせるで💪", "今日は今日、明日は明日や", "しっかり立て直して、また挑むわ🔥"]
    return rng.choice(op) + "\n" + rng.choice(mid) + "\n" + rng.choice(cl)


def post_reply(entry, token, user_id) -> bool:
    """予想投稿へのリプライ。的中/不的中どちらも結果行＋関西弁コメントを返信."""
    pid = entry.get("post_id")
    if not pid:
        return False
    combo = entry.get("result") or ""
    mult = _mult_str(entry.get("payout"))
    hit = bool(entry.get("hit"))
    mark = "的中🎯" if hit else "❌"
    header = (combo + "　" + mult + "倍" + mark) if mult else (combo + "　" + mark)

    race = _find_result_race(entry)
    comment = _llm_comment(entry, race, hit) if race else None
    if not comment:
        comment = _fallback_comment(entry, race, hit)
    text = (header + "\n" + comment)[:500]

    try:
        c = http_post(f"{THREADS_API}/{user_id}/threads",
                      {"media_type": "TEXT", "text": text,
                       "reply_to_id": pid, "access_token": token})
        if not c.get("id"):
            print(f"  リプライコンテナ失敗: {c}")
            return False
        time.sleep(35)
        r = http_post(
            f"{THREADS_API}/{user_id}/threads_publish",
            {"creation_id": c["id"], "access_token": token},
        )
        if r.get("id"):
            print(f"  リプライ完了! post id = {r['id']}")
            return True
        print(f"  リプライ公開失敗: {r}")
    except Exception as ex:  # noqa: BLE001
        print(f"  リプライ投稿エラー: {ex}")
    return False


def main():
    token = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        print("THREADS_ACCESS_TOKEN が未設定です。", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(LOG_FILE):
        print("posts_log.json がまだありません。投稿後に生成されます。")
        return
    with open(LOG_FILE, encoding="utf-8") as f:
        log = json.load(f)
    if not log:
        print("記録された投稿がまだありません。")
        write_csv(log)
        return

    now = datetime.now(JST)
    user_id = None
    changed = False

    for e in log:
        posted = datetime.fromisoformat(e["posted_at"])
        elapsed_h = (now - posted).total_seconds() / 3600
        e.setdefault("views", {})

        # 表示回数スナップショット (削除済み投稿はスキップ)
        for key, hours in SNAPSHOTS:
            if key not in e["views"] and elapsed_h >= hours and not e.get("deleted"):
                v = fetch_views(e["post_id"], token)
                if v is not None:
                    e["views"][key] = v
                    e.setdefault("views_at", {})[key] = round(elapsed_h, 2)
                    print(f"{e['stadium']}{e['race_number']}R: {key} views = {v}")
                    changed = True

        # 的中チェック (締切25分後以降)
        if not e.get("result"):
            closed = datetime.strptime(
                e["race_closed_at"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=JST)
            if now >= closed + timedelta(minutes=25):
                res = find_race_result(e)
                if res:
                    combo, payout = res
                    e["result"] = combo
                    e["payout"] = payout
                    e["hit"] = combo in (e.get("combos") or [])
                    changed = True
                    mark = "🎯的中!" if e["hit"] else "不的中"
                    print(f"{e['stadium']}{e['race_number']}R 結果 {combo} → {mark}")
                    # 的中報告の新規投稿は無効化(@r_no_yosou はリプライのみ運用)。

        # 結果が出ていてまだリプライしていない投稿は、的中/不的中どちらも
        # 予想投稿へ1回だけ関西弁コメント付きでリプライする。
        if e.get("result") and not e.get("reply_posted"):
            try:
                closed_r = datetime.strptime(
                    e["race_closed_at"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=JST)
                fresh_r = (now - closed_r) <= timedelta(minutes=90)
            except (ValueError, KeyError):
                fresh_r = False
            if not fresh_r:
                e["reply_posted"] = True
                changed = True
            else:
                if user_id is None:
                    user_id = get_user_id(token)
                if post_reply(e, token, user_id):
                    e["reply_posted"] = True
                    changed = True

    if changed:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=1)
    write_csv(log)
    print("完了。")


if __name__ == "__main__":
    main()
