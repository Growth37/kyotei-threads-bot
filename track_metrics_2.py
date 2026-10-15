#!/usr/bin/env python3
"""@shantianzhi37 (2号) の的中チェック＆的中投稿.

posts_log_2.json の各投稿について、締切25分後以降にレース結果を照合し、
買い目(本線∪抑え)の中に3連単の決着があれば「的中報告」を出す。
的中報告はリプライにしない。コピー投稿もしない。元の予想投稿のURLを
「この予想⬇️」と一緒に貼った的中報告を新規投稿する。

的中報告フォーマット(人間味なし):
    {会場}{R}R　⚪-⚪-⚪　⚪⚪倍🎯

    {回収・的中ペース系の一言}

    この予想⬇️
    {元の予想投稿のURL}

環境変数:
  THREADS_ACCESS_TOKEN_2  @shantianzhi37 の長期アクセストークン
"""

import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta

import track_metrics as tm  # 結果取得ロジックを流用

JST = tm.JST
THREADS_API = tm.THREADS_API

LOG_FILE = "posts_log_2.json"
CSV_FILE = "metrics_2.csv"


def _resolved(log):
    return [e for e in log if e.get("result")]


def _streak(log):
    res = sorted(_resolved(log), key=lambda x: x.get("race_closed_at") or "")
    n = 0
    for e in reversed(res):
        if e.get("hit"):
            n += 1
        else:
            break
    return n


def pace_line(log, entry) -> str:
    """回収・的中ペース系の定型文(人間味なし)を1つ返す."""
    import random
    res = _resolved(log)
    hits = sum(1 for e in res if e.get("hit"))
    total = len(res)
    rate = round(100 * hits / total) if total else 0
    # 直近5戦
    recent = sorted(res, key=lambda x: x.get("race_closed_at") or "")[-5:]
    r_hits = sum(1 for e in recent if e.get("hit"))
    r_total = len(recent)
    # 今週(直近7日)
    now = datetime.now(JST)
    wk = [e for e in res if e.get("race_date") and
          (now - datetime.strptime(e["race_date"][:10], "%Y-%m-%d").replace(tzinfo=JST)).days < 7]
    w_hits = sum(1 for e in wk if e.get("hit"))
    w_total = len(wk)
    streak = _streak(log)

    cands = []
    if w_total and w_hits / w_total >= 0.65:
        cands.append(f"今週 {w_hits}/{w_total} 的中ペース")
    # 「直近N戦M的中」は半分以上的中している時のみ(2戦以上・的中率65%超)。
    # 5戦1的中や5戦2的中のような見栄えの悪いものは出さない。
    if r_total >= 2 and r_hits / r_total > 0.65:
        cands.insert(0, f"直近{r_total}戦{r_hits}的中")
    if streak >= 2:
        cands.append(f"{streak}連勝中🎯")
    payout = entry.get("payout")
    if payout and int(payout) >= 5000:
        cands.append(f"高配当回収（{int(payout):,}円）")
    # モード・決着系 / 節目
    combo = entry.get("result") or ""
    win_lane = combo.split("-")[0] if combo else ""
    honsen = entry.get("honsen") or []
    fav = honsen[0].split("-")[0] if honsen else ""
    mode = entry.get("mode")
    if mode == "中穴":
        cands.append("中穴ズバリ🎯")
    if mode == "堅い" and win_lane and win_lane == fav:
        cands.append("本命ど真ん中🎯")
    if win_lane == "1":
        cands.append("イン逃げ的中🎯")
    if hits in (10, 25, 50, 100):
        cands.append(f"通算{hits}的中🎯")
    if not cands:
        return ""
    rng = random.Random(str(entry.get("post_id")))
    return rng.choice(cands)


def build_hit_text(entry, payout, log) -> str:
    """的中報告の本文(会場R + 結果 + 倍率 + ペース一言)を組む."""
    stadium = entry.get("stadium") or ""
    rno = entry.get("race_number")
    venue = f"{stadium}{rno}R" if rno else stadium
    combo = entry.get("result") or ""
    if payout:
        mult = int(payout) / 100
        mult_s = f"{mult:.1f}".rstrip("0").rstrip(".")
        head = f"{venue}　{combo}　{mult_s}倍🎯"
    else:
        head = f"{venue}　{combo}　的中🎯"
    pace = pace_line(log, entry)
    return f"{head}\n\n{pace}" if pace else head


def fetch_permalink(post_id, token):
    """投稿のパーマリンク(URL)をThreads APIから取得。失敗時はNone."""
    if not post_id:
        return None
    try:
        import urllib.parse
        qs = urllib.parse.urlencode({"fields": "permalink", "access_token": token})
        data = tm.http_get_json(f"{THREADS_API}/{post_id}?{qs}")
        url = (data.get("permalink") or "").strip()
        return url or None
    except Exception as e:  # noqa: BLE001
        print(f"  パーマリンク取得失敗 ({post_id}): {e}")
        return None


def _publish(user_id, token, params) -> str:
    """コンテナ作成→35秒待ち→公開。成功時は公開後のpost_id、失敗時はNone."""
    c = tm.http_post(f"{THREADS_API}/{user_id}/threads",
                     {**params, "access_token": token})
    if not c.get("id"):
        print(f"  コンテナ失敗: {c}")
        return None
    time.sleep(35)
    r = tm.http_post(f"{THREADS_API}/{user_id}/threads_publish",
                     {"creation_id": c["id"], "access_token": token})
    if r.get("id"):
        return r["id"]
    print(f"  公開失敗: {r}")
    return None


def post_hit_new(entry, payout, token, user_id, log) -> bool:
    """的中報告フロー(リプライにしない・コピー投稿もしない):
      元の予想投稿のURLを取得し、
      「会場R 結果 倍率🎯 / ペース一言 / この予想⬇️ + 元予想URL」を新規投稿する。
      URL取得に失敗した場合はURL行なしで的中報告のみ投稿する。
    """
    hit = build_hit_text(entry, payout, log)
    permalink = fetch_permalink(entry.get("post_id"), token)
    if permalink:
        report = f"{hit}\n\nこの予想⬇️\n{permalink}"
    else:
        report = hit
    report = report[:500]

    try:
        rid = _publish(user_id, token, {"media_type": "TEXT", "text": report})
        if rid:
            print(f"  的中報告投稿完了(新規投稿)! post id = {rid}")
            return True
    except Exception as e:  # noqa: BLE001
        print(f"  的中報告投稿エラー: {e}")
    return False


def write_csv(log):
    headers = ["投稿日", "時刻", "場", "R", "モード", "締切",
               "買い目", "結果(3連単)", "的中", "配当(円)", "倍率"]
    rows = []
    for e in sorted(log, key=lambda x: x["posted_at"]):
        hit = ""
        if e.get("result"):
            hit = "的中" if e.get("hit") else "×"
        payout = e.get("payout")
        mult = f"{int(payout)/100:.1f}" if (e.get("hit") and payout) else ""
        rows.append([
            e["posted_at"][:10], e["posted_at"][11:16], e["stadium"],
            e["race_number"], e.get("mode", ""), e["race_closed_at"][11:16],
            " / ".join(e.get("combos") or []),
            e.get("result") or "", hit, e.get("payout") or "", mult,
        ])
    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


# ===== リプライ(結果を見た人間味コメント) =============================
TECHNIQUE = {
    1: "逃げ", 2: "差し", 3: "まくり", 4: "まくり差し", 5: "抜き", 6: "恵まれ",
}


def _find_result_race(entry):
    """結果APIから該当レースのraw(選手名・ST・決まり手入り)を返す."""
    try:
        for race in tm.fetch_results(entry.get("race_date") or ""):
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


def _race_facts(entry, race):
    """LLMに渡すレース事実のテキストを組み立てる."""
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
    one_place = next(
        (int(b.get("racer_place_number"))
         for b in boats
         if int(b.get("racer_boat_number") or 0) == 1 and b.get("racer_place_number")),
        None,
    )
    tech = TECHNIQUE.get(int(race.get("race_technique_number") or 0), "")
    st_txt = "、".join(label(n) + " ST" + str(st_map[n]) for n in sorted(st_map))

    combo = entry.get("result") or ""
    mult = _mult_str(entry.get("payout"))
    hit = bool(entry.get("hit"))
    in_shibori = combo in (entry.get("shibori") or [])
    in_osae = combo in (entry.get("osae") or [])

    lines = [
        "会場: " + str(entry.get("stadium")) + str(entry.get("race_number")) + "R",
        "この予想は『本命=1号艇のイン逃げ(的中重視)』狙い。買い目 " + str(len(entry.get("combos") or [])) + "点。",
        "決着: " + combo + "(" + mult + "倍) " + ("的中" if hit else "不的中"),
        "着順: " + order,
        "1号艇(本命◎)の着順: " + (str(one_place) + "着" if one_place else "不明"),
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
    elif in_osae:
        lines.append("※抑えには入っていたが本線は外れ。")
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
    """Gemini無料枠で結果に応じた人間味コメントを生成。失敗時はNone."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    facts = _race_facts(entry, race)
    style = (
        "あなたは競艇の予想アカウント『的中重視・イン逃げ狙い』の中の人。"
        "レース結果を見て、ファン向けの短い本音コメントを書く。"
        "口調は標準語(ていねいすぎない、くだけた標準語)。関西弁・方言は一切使わない"
        "(『〜や』『〜やで』『〜へん』『〜やねん』『ほんま』『しゃあない』『〜しとる』などは禁止)。"
        "絵文字あり、必ず3〜4行で書く。毎回ちがう語り出し・言い回し・絵文字にして、同じ定型やテンプレは繰り返さない。"
        "不的中は悔しさを素直に出す。選手名は出してよいが、個人への人格攻撃ではなく『◯◯の差しにやられた』『◯◯が来るとは』のようにレース結果・展開への悔しさとして書く。"
        "的中ならドヤ混じりで喜ぶ。艇番は①②③、選手名も出してよい。"
        "結果の事実(誰が来たか/本命①の着順/STの遅れ/決まり手)を必ず踏まえる。"
        "的中でも、本線が広い(買い目が多い)うえ低めの配当での的中は、ドヤらず控えめに書く。『狙い通り』『ついてきてよかった』のような自慢は、絞りでの的中か高配当のときだけ。万舟・高配当は素直に驚く。点数のわりに地味な的中を大げさに喜ばない。決め台詞の使い回しやbotっぽさを避け、毎回ちがう自然な言い回しにする。"
        "本命①が1着でないときは『イン逃げ』『本命が逃げ切った』とは書かず、実際に1着だった艇を踏まえて書く。"
        "書き方は、経験者が会話調で崩して書いたように。AIっぽい型・硬い構成・形式的な言葉・先の読める言い回しは避け、文の長さに緩急をつけて単調な繰り返しをなくし、人間らしい温度を少し乗せる。中身は変えず盛らない。"
        "買い目の行(○-○-○や倍率)は書かない。コメント本文だけを返す。"
    )
    examples = (
        "例(不的中):\n①福来、来ないのかよ‼️\nこれはふざけんな💢\n本命党じゃ獲れない、ありえないだろ😡\n\n"
        "例(不的中):\n⑥甘寧が来るなんて💦\n④杉山、スタート遅れるとか何やってんだ💢\nこんなの無理、切り替えるしかない😤\n\n"
        "例(的中):\n見事に的中‼️\n絞り4点で買ってればしっかりプラス💰\n的中重視の予想、これだよこれ✌️"
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
    """Gemini不発時のコメント。点数・配当・絞り・実際の1着艇に応じて内容を変える(@shantianzhi37)."""
    import random
    rng = random.Random(str(entry.get("post_id")) + ("h" if hit else "m") + "fb2e")
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
    head = 1
    won = (winner == head)
    if hit:
        if odds >= 30:
            op = ["おお、これは獲れたか‼️🎯", "まさかの高配当、キタ‼️", "これはデカい一本だ🎯", "正直びっくりの的中😳", "高いのが引けた、ラッキーだ🎯", "嬉しい誤算の的中だ😳", "これは獲れると思ってなかった😳", "配当すごいの引いたな🎯", "こんなの獲れたら上出来だ😆", "まさかまさかの的中だ🎯"]
            cl = ["こういうのが獲れると痺れる🔥", "たまにはこんな日もいい😎", "次もどこかで一発狙う", "いや〜よくハマった✌️", "今日はいい日だ🍺", "こういう当たりは記憶に残る😎"]
        elif in_shibori:
            op = ["絞りからズバッと的中✌️", "絞りが的中、気持ちいい🎯", "絞った甲斐があった🎯", "狙い澄ました絞りで的中🎯", "絞り的中、読み勝ちだ✌️", "よし、きっちり獲った🎯", "会心の的中だ🎯"]
            cl = ["この調子でいく😎", "次もきっちり獲りにいく🔥", "ついてきてよかったでしょ✌️", "いい流れだ、続ける", "自信を持って出せた一本だった😎", "こういうのを積み重ねる🔥"]
        elif pts <= 6:
            op = ["よし、きっちり獲った🎯", "読み通りの的中だ🎯", "手堅く的中だ😎", "会心の的中だ🎯", "ピタッと決まった✌️", "狙い通りに的中🎯", "きっちり拾った✌️", "しっかり的中、気持ちいい😎"]
            cl = ["この調子でいく😎", "次もきっちり獲りにいく🔥", "ついてきてよかったでしょ✌️", "いい流れだ、続ける", "自信を持って出せた一本だった😎", "こういうのを積み重ねる🔥"]
        else:
            op = ["本線広めだったが的中🎯", "手堅く拾えた一本", "点数は多めだが獲れた", "まあ本線で拾えた🙆", "欲張らず的中、よしとこう", "広めに買っての的中、中身は地味だが", "薄めだが的中は的中", "無難に本線で収まった", "点数をかけてるぶん配当は地味だが", "取りこぼさなかっただけヨシだ🙆"]
            cl = ["こつこつ当てていく", "次は中身でも勝負したい", "地味でも的中は的中、よしとする🙆", "次も手堅くいく", "こういうのを外さないのが大事", "派手さはないが拾えてよかった"]
        if won and nm.get(head):
            mid = ["◎①" + nm[head] + "がきっちり逃げ切った", "本命①" + nm[head] + "、しっかり1着だ", "①" + nm[head] + "の逃げ、読みどおり", "展開も読み通りだった", "軸がブレずに決まった"]
        elif (not won) and winner and nm.get(winner):
            mid = ["本命①は頭まで届かなかったが、押さえが的中", str(winner) + "号艇" + nm[winner] + "の1着をしっかり拾えた", "①は連下、頭の" + str(winner) + "号艇" + nm[winner] + "を押さえていた", "本命は2着でも買い目でカバーできた", "頭は" + str(winner) + "号艇" + nm[winner] + "、押さえが仕事をした"]
        else:
            mid = ["展開は読み通りだった", "人気どころで堅く収まった", "軸がブレずに決まった", "思った通りに収まった"]
    else:
        if odds >= 30:
            op = ["いや、こんな荒れは獲れない😅", "まさかの高配当、これは仕方ない🤷", "こんな決着は狙ってないしな💦", "荒れたな、どうしようもない😮‍💨", "手の出しようがない一戦だった😵", "これは誰も獲れないだろう😅", "大荒れだ、ノーチャンスだ🤷", "これが来たら脱帽だ😮‍💨"]
        else:
            op = ["これは獲りたかった…😣", "堅いところなのに拾えなかった、反省だ😩", "むむ、これを外すのは痛い💦", "手堅いところなのに獲れないとは…😮‍💨", "獲れる内容だったのにな😣", "これは獲らないといけないやつだった😩", "取れるところを落とした💦", "惜しい、あと一歩だったのに😣"]
        mid = ["展開が噛み合わなかった", "ここは読み切れなかった", "絞り切れなかったのが悔やまれる", "一本に絞れていればな…", "読みが少しズレた"]
        if nm.get(head) and pl.get(head):
            mid += ["◎①" + nm[head] + "が" + str(pl[head]) + "着じゃな…", "本命①" + nm[head] + "が沈むと厳しい"]
        if winner and winner != head and nm.get(winner):
            mid += [str(winner) + "号艇" + nm[winner] + "に持っていかれた…", str(winner) + "号艇" + nm[winner] + "が来るとはな"]
        cl = ["次は取りこぼさないようにする", "ここは反省して組み直す", "悔しいが次でしっかり当て返す", "精度を上げて次に臨む", "こういうのを拾えるようにしたい", "次こそ結果で示す", "気持ちを切り替えて次へ向かう", "ここから巻き返していく", "引きずらず、前を向く", "次のレースで取り返す", "こういう日もある、淡々と次へ", "反省点を次に活かす", "まだまだこれから、立て直す", "ひとつ学びになった、次へ", "次こそ納得の一本を出す", "外れは外れ、切り替えていく", "もう少し丁寧に読み直す", "次は堅く攻め直す", "ここで止まらず前進する", "悔しさを次の的中に変える", "冷静に見直して出直す", "また一から組み立て直す", "次でしっかり帳尻を合わせる", "今日の反省を明日に繋げる", "気を取り直してもう一度", "ここは修正して次に向かう", "次はきっちり仕留めたい", "負けを受け止めて前へ", "また良い予想を組んで臨む", "次こそ的中を届けたい", "落ち着いて立て直していく", "悔しいけれど前だけ向く", "次はもっと精度を高める", "一度の外れで折れはしない", "ここから流れを変えていく", "次のレースに集中する", "外した分、次で取り返す", "反省を力に変えて進む", "また気持ちを入れ直す", "次こそ本命で決めたい", "こういう時こそ冷静にいく", "立て直しの一本を次で見せる", "悔しさはバネにしていく", "まだ巻き返せる、諦めない", "次はしっかり読み切りたい", "淡々と次の予想へ向かう", "取り返すチャンスはまだある", "前を向いて準備し直す", "次こそ気持ちよく当てたい", "反省は反省、引きずらない", "もう次を見据えて動く", "こういう日を糧にする", "次はもっと良い買い目を組む", "焦らず次を取りにいく", "前向きに切り替えていく", "ここから挽回していく", "次でちゃんと結果を出す", "今日は今日、次は次", "しっかり見直して出直す", "これも競艇、気持ちを切り替える", "次はこの反省を活かす", "外れたものは仕方ない、前進", "もう一度気合を入れ直す", "次こそ納得の結果を出したい", "冷静に、また次へ", "必ず取り返してみせる", "負けっぱなしでは終わらない", "次のレースに全力を注ぐ", "また組み立てて勝負する", "一喜一憂せず続けていく", "次はきっちり当てにいく", "ここからが腕の見せどころ", "悔しさを次に向ける", "外れも含めて競艇、次へ", "気持ちを切らさず次へ", "反省点は次に持ち越す", "また一本一本を大事にする", "次こそ笑って終わりたい", "ここで諦めず続けていく", "淡々と次の勝負へ向かう", "外れは次の的中への前段階", "もっと精進していく", "次はしっかり読み込む", "こういう日もある、気楽に次へ", "取り返す機会はまだ十分ある", "前を向いて次の準備をする", "悔しいが切り替えて進む", "次こそ当てて喜んでもらいたい", "また明日に向けて組み直す", "外した悔しさは忘れない", "次はもっと丁寧に読む", "ここで止まってはいられない", "反省して、また挑む", "次こそ本線で決めたい", "こういう時は無理をしない", "立て直して必ず取り返す", "負けを引きずらず前へ進む", "次のレースでリベンジする", "気を取り直して前進あるのみ", "また次、気持ちよく当てにいく"]
    return rng.choice(op) + "\n" + rng.choice(mid) + "\n" + rng.choice(cl)


def post_reply(entry, token, user_id) -> bool:
    """予想投稿へのリプライ。的中/不的中どちらも結果行＋人間味コメントを返信."""
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
        rid = _publish(user_id, token,
                       {"media_type": "TEXT", "text": text, "reply_to_id": pid})
        if rid:
            print("  リプライ完了! post id = " + str(rid))
            return True
    except Exception as ex:  # noqa: BLE001
        print("  リプライ投稿エラー: " + str(ex))
    return False


def main():
    token = os.environ.get("THREADS_ACCESS_TOKEN_2", "").strip()
    if not token:
        print("THREADS_ACCESS_TOKEN_2 が未設定です。", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(LOG_FILE):
        print("posts_log_2.json がまだありません。投稿後に生成されます。")
        return
    with open(LOG_FILE, encoding="utf-8") as f:
        log = json.load(f)
    if not log:
        print("記録された投稿がまだありません。")
        return

    now = datetime.now(JST)
    user_id = None
    changed = False

    for e in log:
        # 1) まだ結果が出ていない投稿はレース結果を照合する
        if not e.get("result"):
            try:
                closed = datetime.strptime(
                    e["race_closed_at"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=JST)
            except (ValueError, KeyError):
                continue
            if now < closed + timedelta(minutes=25):
                continue
            res = tm.find_race_result(e)
            if not res:
                continue
            combo, payout = res
            e["result"] = combo
            e["payout"] = payout
            e["hit"] = combo in (e.get("combos") or [])
            changed = True
            mark = "🎯的中!" if e["hit"] else "不的中"
            print(f"{e['stadium']}{e['race_number']}R 結果 {combo} → {mark}")

        # 2) 的中報告の新規投稿は無効化(@shantianzhi37 はリプライのみ運用)。

        # 3) 結果が出ていてまだリプライしていない投稿は、的中/不的中どちらも
        #    予想投稿へ1回だけリプライする(結果を見た人間味コメント付き)。
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
                    user_id = tm.get_user_id(token)
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
