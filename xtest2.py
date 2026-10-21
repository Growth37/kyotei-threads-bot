#!/usr/bin/env python3
"""2号(da_yama7777)のX投稿経路テスト: 実レースで2号のフル本文を作り、post_to_x(_2)で投稿する。"""
import contextlib
import io
import json
import time
from datetime import datetime

import post_prediction as eng
import post_prediction_2 as eng2


def main():
    result = {"step": "start"}
    race = None
    blocks = None
    text = None
    build_err = None
    try:
        now = datetime.now(eng.JST)
        programs = eng.load_programs(now)
        result["programs"] = len(programs)
        for p in programs:
            if p.get("boats") and len(p.get("boats") or []) >= 6:
                race = p
                break
        if race is not None:
            result["race"] = str(race.get("race_stadium_number")) + "-" + str(race.get("race_number"))
            mode = "堅い"
            blocks = eng2.blocks_for(race, mode)
            text = eng2.build_post(race, blocks, mode)
        else:
            text = "接続テスト2号(実レースなし) " + time.strftime("%H:%M:%S")
    except Exception as e:  # noqa: BLE001
        build_err = repr(e)
        text = "接続テスト2号(build失敗) " + time.strftime("%H:%M:%S")
    result["build_err"] = build_err
    result["text"] = text
    result["weighted"] = eng._x_weighted_len(text) if text else None

    x_text = text
    try:
        if text and eng._x_weighted_len(text) > 280 and blocks is not None:
            x_text = eng2.build_x_post_2(race, blocks)
    except Exception as e:  # noqa: BLE001
        result["xbuild_err"] = repr(e)
    result["x_text"] = x_text

    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        tid = eng.post_to_x(x_text, "_2")
    result["post"] = {"tid": tid, "stdout": out.getvalue(), "stderr": err.getvalue()}

    open("xtest2_result.txt", "w", encoding="utf-8").write(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
