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
        lines.append("※的中。" + ("絞りにも入っていた。" if in_shibori else "絞りには入っていない。"))
    return "\n".join(lines)


def _llm_comment(entry, race, hit):
    """Gemini無料枠で関西弁コメントを生成(@r_no_yosou用)。失敗時はNone."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    facts = _race_facts(entry, race)
    style = (
        "あなたは競艇予想アカウント『@r_no_yosou』の中の人。関西の予想家で、"
        "軽妙なぼやき・ツッコミ・自虐まじりの、こてこての関西弁で話す(語尾は『〜や』『〜やで』『〜やなぁ』『〜へん』、『ほんま』『しゃあない』『〜してもうた』等を必ず使い、標準語には絶対にしない)。絵文字あり、必ず3〜4行で書く。毎回ちがう語り出し・言い回し・絵文字にして、同じ定型やテンプレは繰り返さない。"
        "不的中は怒るより『あちゃー』『しゃあない』系の関西のぼやき・ツッコミで。"
        "的中は『よっしゃ』『ドンピシャ』系で気持ちよく喜ぶ。艇番は①②③、選手名も出してよい。"
        "結果の事実(誰が来たか/本命の着順/STや決まり手)を必ず踏まえる。"
        "『ふざけとる』『なにしとんねん』『何してるの』等の強い罵倒口調は使わない。"
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


def _fallback_comment(hit):
    if hit:
        return "よっしゃ本命キッチリ獲れたで😎\n狙い通りや、ええ流れやん🎯\nこの調子でいくで〜！"
    return "あちゃー、こら獲れんわ…🙏\nまあこういう日もあるわな💦\n気持ち切り替えて次いこ！"


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
        comment = _fallback_comment(hit)
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
                    if e["hit"] and not e.get("hit_posted"):
                        if user_id is None:
                            user_id = get_user_id(token)
                        if post_hit(e, payout, token, user_id, current_streak(log)):
                            e["hit_posted"] = True

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
