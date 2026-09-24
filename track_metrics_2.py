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

        # 2) 的中していてまだ的中報告を出していない投稿は必ず投稿する。
        #    結果が既に出ていても、前回投稿に失敗した分をここで再送する
        #    (これが無いと『的中してるのに報告が無い』取りこぼしが起きる)。
        if e.get("result") and e.get("hit") and not e.get("announced"):
            if user_id is None:
                user_id = tm.get_user_id(token)
            if post_hit_new(e, e.get("payout"), token, user_id, log):
                e["announced"] = True
                changed = True
            else:
                print(f"  ⚠ 的中報告に失敗。次回の実行で再送します: "
                      f"{e['stadium']}{e['race_number']}R")

    if changed:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=1)
    write_csv(log)
    print("完了。")


if __name__ == "__main__":
    main()
