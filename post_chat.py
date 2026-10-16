#!/usr/bin/env python3
"""@r_no_yosou に予想以外の「雑談/画像」ポストを1日1本程度、ランダムな時刻に投稿する.

予想bot一色にならないよう、ゆるい一言(関西弁)や画像を混ぜて人間味を出す。
- 1日1本まで(chat_log.json で管理)
- 毎スロット必ずは出さない(確率で間引いて不規則に)
- images/ に画像(.jpg/.png)を置くと、たまに画像付きで投稿する
"""
import json
import os
import random
import sys
import time
from datetime import datetime

import post_prediction as eng  # 1号botのエンジン(投稿関数)を流用

JST = eng.JST
LOG_FILE = "chat_log.json"
IMAGES_DIR = "images"
RAW_IMAGE_BASE = (
    "https://raw.githubusercontent.com/Growth37/kyotei-threads-bot/main/images/"
)
WINDOW_START = "10:30"
WINDOW_END = "21:00"
POST_PROBABILITY = 0.5   # 各スロットで投稿する確率(不規則化)
IMAGE_PROBABILITY = 0.5  # 画像がある時に画像付きにする確率

CHATTER = [
    "今日もぼちぼち予想していくで〜 よろしゅう頼むわ",
    "さて、今日はどの場が荒れるかな〜",
    "競艇はやっぱりインが面白いな、今日も楽しも",
    "朝からコーヒー片手に出走表とにらめっこや",
    "今日もええ勝負が見られますように",
    "負けた日も次があるから競艇はやめられんな",
    "みんな今日の狙い場どこにする？",
    "風の読みが難しい季節やなぁ",
    "今日は固いレース多めかな、どうやろ",
    "万舟は夢があるよな、たまには狙いたいわ",
    "ナイターの雰囲気、やっぱりええな",
    "スタート展示は毎回ドキドキするわ",
    "今日もケガなく無事に全レース終わりますように",
    "予想が当たった時のあの瞬間がたまらんねん",
    "きょうも一緒にワイワイやっていこな",
    "舟券は余裕を持って、無理せんのが一番やで",
    "ベテラン勢の渋い走り、見てて惚れるわ",
    "若手のまくり、決まると気持ちええよな",
    "今日の水面はどんな感じやろ、楽しみや",
    "外れても引きずらん、それがワシのモットーや",
    "のんびり観戦するのもええもんやで",
    "今日も的中目指してがんばるで〜",
    "一緒に競艇楽しめる仲間がおるの、ありがたいわ",
    "さぁ、今日も気合い入れていこか",
    "レース後の振り返りも勉強になるんよな",
    "インの逃げを見るとやっぱり安心するわ",
    "今日はちょっと冒険して穴も狙おかな",
    "展開読むのがほんま楽しい競技やで競艇は",
    "今日もよろしくな、ぼちぼちいこ",
    "勝っても負けても、楽しんだもん勝ちやで",
    "この時間帯のレース、結構おもろいの多いねん",
    "ちょっと一息、今日の予想まとめ中や",
    "みんなの推し選手おる？ワシは渋いの好きやわ",
    "今日もいい波来るとええな〜",
    "競艇場のあの独特の空気、好きやねん",
    "さぁ後半戦、ここからが本番やで",
    "今日の予想、なかなか手応えある気がするわ",
    "まったり構えていこ、焦りは禁物やで",
    "一日お疲れさん、今日もありがとな",
    "明日もええレースになりますように",
]


def _load_log():
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


def _posted_today(now) -> bool:
    today = now.strftime("%Y-%m-%d")
    for e in _load_log():
        if (e.get("posted_at") or "")[:10] == today:
            return True
    return False


def _append_log(post_id, kind, text, now):
    log = _load_log()
    log.append(
        {
            "post_id": post_id,
            "kind": kind,
            "text": text,
            "posted_at": now.isoformat(),
        }
    )
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def _list_images():
    try:
        return [
            f
            for f in sorted(os.listdir(IMAGES_DIR))
            if f.lower().endswith((".jpg", ".jpeg", ".png"))
        ]
    except Exception:  # noqa: BLE001
        return []


def post_image(image_url, caption, user_id, token):
    container = eng.http_post(
        f"{eng.THREADS_API}/{user_id}/threads",
        {
            "media_type": "IMAGE",
            "image_url": image_url,
            "text": caption,
            "access_token": token,
        },
    )
    creation_id = container.get("id")
    if not creation_id:
        raise RuntimeError(f"画像コンテナ作成に失敗: {container}")
    print(f"画像コンテナ作成OK: {creation_id} — 35秒待機します")
    time.sleep(35)
    result = eng.http_post(
        f"{eng.THREADS_API}/{user_id}/threads_publish",
        {"creation_id": creation_id, "access_token": token},
    )
    return result.get("id") or creation_id


def main():
    dry_run = os.environ.get("DRY_RUN", "0") == "1"
    now = datetime.now(JST)
    print(f"実行時刻(JST): {now:%Y-%m-%d %H:%M}")

    hm = now.strftime("%H:%M")
    if not dry_run and not (WINDOW_START <= hm <= WINDOW_END):
        print(f"時間帯({WINDOW_START}〜{WINDOW_END})外のためスキップします。")
        return
    if not dry_run and _posted_today(now):
        print("本日は雑談を投稿済みのためスキップします。")
        return
    if not dry_run and random.random() > POST_PROBABILITY:
        print("今回はランダムで見送り(不規則化)。")
        return

    rng = random.Random()
    caption = rng.choice(CHATTER)
    images = _list_images()
    use_image = bool(images) and rng.random() < IMAGE_PROBABILITY

    print("---- 投稿内容 ----")
    print(("[画像付き] " if use_image else "") + caption)
    print("------------------")

    if dry_run:
        print("DRY_RUN=1 のため投稿はしません。")
        return

    token = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        print("THREADS_ACCESS_TOKEN が未設定です。", file=sys.stderr)
        sys.exit(1)

    user_id = eng.get_user_id(token)
    if use_image:
        image_url = RAW_IMAGE_BASE + rng.choice(images)
        post_id = post_image(image_url, caption, user_id, token)
        _append_log(post_id, "image", caption, now)
    else:
        post_id = eng.post_to_threads(caption, user_id, token)
        _append_log(post_id, "text", caption, now)
    print(f"投稿完了: {post_id}")


if __name__ == "__main__":
    main()
