#!/usr/bin/env python3
"""診断用: 実際のレースで build_x_post のコンパクト予想文を作り、X投稿を試す。
build生成エラー / X APIのHTTPステータス・本文を xtest_result.txt に書き出す。"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import post_prediction as eng

URL = "https://api.twitter.com/2/tweets"


def pe(v):
    return urllib.parse.quote(str(v), safe="-._~")


def main():
    result = {"step": "start"}

    # 1) 実レースを1つ拾って build_x_post を試す
    race = None
    build_err = None
    text = None
    try:
        now = datetime.now(eng.JST)
        programs = eng.load_programs(now)
        result["programs"] = len(programs)
        for p in programs:
            if p.get("boats") and len(p.get("boats") or []) >= 5:
                race = p
                break
        if race is not None:
            result["race"] = f'{race.get("race_stadium_number")}-{race.get("race_number")}'
            text = eng.build_x_post(race)
        else:
            text = "接続テスト2(実レースなし) " + time.strftime("%H:%M:%S")
    except Exception as e:  # noqa: BLE001
        build_err = repr(e)
        text = "接続テスト2(build失敗) " + time.strftime("%H:%M:%S")
    result["build_err"] = build_err
    result["text"] = text
    result["weighted"] = eng._x_weighted_len(text) if text else None

    # 2) そのテキストをXに投稿して結果を記録
    ck = os.environ.get("X_API_KEY", "").strip()
    cs = os.environ.get("X_API_SECRET", "").strip()
    tok = os.environ.get("X_ACCESS_TOKEN", "").strip()
    toks = os.environ.get("X_ACCESS_SECRET", "").strip()
    if not all([ck, cs, tok, toks]):
        result["post"] = {"error": "missing secrets"}
    else:
        oauth = {
            "oauth_consumer_key": ck,
            "oauth_nonce": base64.b64encode(os.urandom(16)).decode().strip("="),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": tok,
            "oauth_version": "1.0",
        }
        param_str = "&".join("{}={}".format(pe(k), pe(oauth[k])) for k in sorted(oauth))
        base = "&".join(["POST", pe(URL), pe(param_str)])
        key = "{}&{}".format(pe(cs), pe(toks))
        oauth["oauth_signature"] = base64.b64encode(
            hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
        ).decode()
        auth = "OAuth " + ", ".join('{}="{}"'.format(pe(k), pe(oauth[k])) for k in sorted(oauth))
        body = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(URL, data=body, method="POST")
        req.add_header("Authorization", auth)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result["post"] = {"ok": True, "status": resp.status, "body": resp.read().decode("utf-8", "ignore")}
        except urllib.error.HTTPError as e:
            result["post"] = {"ok": False, "status": e.code, "body": e.read().decode("utf-8", "ignore")}
        except Exception as e:  # noqa: BLE001
            result["post"] = {"ok": False, "error": repr(e)}

    open("xtest_result.txt", "w", encoding="utf-8").write(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
