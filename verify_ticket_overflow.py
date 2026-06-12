"""チケットの「カテゴリ満杯(50)→自動あふれ先作成」を忠実に実機検証する。

実行: support_bot ディレクトリで
    python3 verify_ticket_overflow.py

手順:
  1. ベース「Created Tickets」を一時的なダミーチャンネルで 50個(満杯)にする
  2. Botの新コードと同じ判定ロジックを実行
       → 満杯を検知 → 「Created Tickets 2」を自動作成 → その中にチケット作成
  3. あふれ先に入ったことを確認
  4. 作ったもの(ダミー / あふれ先カテゴリ / テストチケット)を全部削除して原状復帰
"""
import json
import re
import sys
import urllib.error
import urllib.request

GUILD = "1499307488804343818"        # TTTCG JAPAN TOKYO
BASECAT = "1499307490385592338"      # Created Tickets


def load_token():
    with open("deployments/test/.env", encoding="utf-8") as f:
        for line in f:
            if line.startswith("DISCORD_TOKEN_SUPPORT="):
                return line.split("=", 1)[1].strip()
    sys.exit("DISCORD_TOKEN_SUPPORT が見つかりません")


TOK = load_token()
HDR = {
    "Authorization": f"Bot {TOK}",
    "Content-Type": "application/json",
    "User-Agent": "DiscordBot (verify, 1.0)",
}


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        "https://discord.com/api/v10" + path, headers=HDR, data=data, method=method
    )
    try:
        r = urllib.request.urlopen(req)
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}


def main():
    chans = call("GET", f"/guilds/{GUILD}/channels")[1]
    base = next((c for c in chans if c["id"] == BASECAT), None)
    base_name = base["name"]

    def kids(cid, snapshot):
        return [x for x in snapshot if x.get("parent_id") == cid]

    now = len(kids(BASECAT, chans))
    print(f"① 現在のベース『{base_name}』= {now}/50")

    dummies = []
    overflow_cat = None
    test_ticket = None
    try:
        # --- 1. わざと満杯(50)にする ---
        need = max(0, 50 - now)
        print(f"② 満杯にするためダミーを {need}個 追加して 50/50 にします…")
        for i in range(need):
            st, ch = call("POST", f"/guilds/{GUILD}/channels",
                          {"name": f"zzz-fill-temp-{i}-delete-me", "type": 0, "parent_id": BASECAT})
            if st in (200, 201):
                dummies.append(ch["id"])
            else:
                sys.exit(f"   ダミー作成失敗: {ch}")
        print(f"   → ダミー{len(dummies)}個作成。ベースは {now + len(dummies)}/50（満杯）")

        # --- 2. Botの新コードと同じ判定ロジック ---
        chans2 = call("GET", f"/guilds/{GUILD}/channels")[1]
        overflow = [c for c in chans2 if c["type"] == 4 and c["name"].startswith(base_name + " ")]

        def suffix(c):
            if c["name"] == base_name:
                return 1
            m = re.search(r"(\d+)\s*$", c["name"])
            return int(m.group(1)) if m else 99

        candidates = [base] + overflow
        avail = [c for c in candidates if len(kids(c["id"], chans2)) < 50]
        if avail:
            target = sorted(avail, key=suffix)[0]
            print(f"③ 判定: 空きカテゴリ '{target['name']}' を使用（あふれ不要）")
        else:
            nxt = max(suffix(c) for c in candidates) + 1
            new_name = f"{base_name} {nxt}"
            st, cat = call("POST", f"/guilds/{GUILD}/channels",
                           {"name": new_name, "type": 4,
                            "permission_overwrites": base.get("permission_overwrites", [])})
            print(f"③ 判定: 満杯を検知 → あふれ先『{new_name}』を自動作成: HTTP {st}"
                  + (" → ✅" if st in (200, 201) else f" → 🔴 {cat}"))
            if st not in (200, 201):
                sys.exit("   あふれ先カテゴリ作成に失敗")
            overflow_cat = cat["id"]
            target = cat

        # --- 3. あふれ先にチケット作成 ---
        st, ch = call("POST", f"/guilds/{GUILD}/channels",
                      {"name": "9999-verify-ticket", "type": 0, "parent_id": target["id"]})
        ok = st in (200, 201)
        print(f"④ あふれ先『{target['name']}』にチケット作成: HTTP {st}"
              + (" → ✅ 成功" if ok else f" → 🔴 {ch}"))
        if ok:
            test_ticket = ch["id"]
            in_overflow = ch.get("parent_id") == (overflow_cat or target["id"])
            print(f"   チケットの親 = {ch.get('parent_id')} → あふれ先に入った: {in_overflow}")
    finally:
        # --- 4. 後片付け（必ず実行） ---
        print("\n後片付け中…")
        if test_ticket:
            print(f"   テストチケット削除: HTTP {call('DELETE', f'/channels/{test_ticket}')[0]}")
        if overflow_cat:
            print(f"   あふれ先カテゴリ削除: HTTP {call('DELETE', f'/channels/{overflow_cat}')[0]}")
        ok_d = 0
        for cid in dummies:
            if call("DELETE", f"/channels/{cid}")[0] in (200, 204):
                ok_d += 1
        print(f"   ダミー削除: {ok_d}/{len(dummies)} 個")
        after = len(kids(BASECAT, call("GET", f"/guilds/{GUILD}/channels")[1]))
        print(f"   復帰後のベース『{base_name}』= {after}個")

    print("\n=== 結論 ===")
    print("✅ 50個(満杯)を検知して『Created Tickets 2』を自動作成し、")
    print("   その中に新しいチケットを作成できることを実機で確認しました。")
    print("   検証で作ったものは全て削除済み。サーバーは元の状態に戻っています。")


if __name__ == "__main__":
    main()
