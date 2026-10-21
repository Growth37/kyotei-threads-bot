#!/usr/bin/env python3
"""X投稿の動作確認用。post_to_x と同じ署名で1回投稿し、HTTPステータスと本文を
xtest_result.txt に書き出す（エラー原因の特定用）。"""
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

URL = "https://api.twitter.com/2/tweets"


def pe(v):
    return urllib.parse.quote(str(v), safe="-._~")


def main():
    ck = os.environ.get("X_API_KEY", "").strip()
    cs = os.environ.get("X_API_SECRET", "").strip()
    tok = os.environ.get("X_ACCESS_TOKEN", "").strip()
    toks = os.environ.get("X_ACCESS_SECRET", "").strip()
    present = {
        "X_API_KEY": bool(ck),
        "X_API_SECRET": bool(cs),
        "X_ACCESS_TOKEN": bool(tok),
        "X_ACCESS_SECRET": bool(toks),
    }
    if not all(present.values()):
        open("xtest_result.txt", "w", encoding="utf-8").write(
            json.dumps({"error": "missing secrets", "present": present}, ensure_ascii=False, indent=2)
        )
        print("missing secrets", present)
        return

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

    text = "接続テスト(自動投稿の動作確認) " + time.strftime("%H:%M:%S")
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(URL, data=body, method="POST")
    req.add_header("Authorization", auth)
    req.add_header("Content-Type", "application/json")

    result = {}
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = {"ok": True, "status": resp.status, "body": resp.read().decode("utf-8", "ignore")}
    except urllib.error.HTTPError as e:
        result = {"ok": False, "status": e.code, "body": e.read().decode("utf-8", "ignore")}
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": repr(e)}

    open("xtest_result.txt", "w", encoding="utf-8").write(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
