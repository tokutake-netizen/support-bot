"""入室者へのDM本文の組み立て。

BOT（cogs/welcome.py）とダッシュボードのプレビューが同じ関数を使うための
モジュール。別々に実装すると、画面で見えている文面と実際に届く文面が
ずれていく。discord ライブラリには依存させない（ダッシュボード側は
discord.py を読み込まないため）。
"""
from __future__ import annotations


class _Safe(dict):
    """未知のプレースホルダを、そのままの文字列として残す。

    運用中に文面へ知らない名前を書いても BOT が止まらないようにする。
    """

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(
    template: str,
    *,
    user_mention: str = "",
    user_name: str = "",
    user_display: str = "",
    guild_name: str = "",
    member_count: int = 0,
    invite_url: str = "",
) -> str:
    values = _Safe(
        user_mention=user_mention,
        user_name=user_name,
        user_display=user_display,
        guild_name=guild_name,
        member_count=member_count,
        invite_url=invite_url,
    )
    # .env は1行で保存するため改行が "\n" のまま入っている。戻してから流す。
    text = (template or "").replace("\\n", "\n")
    try:
        return text.format_map(values)
    except (IndexError, ValueError):
        # "{" が単体で書かれている等、書式として壊れている場合は原文を返す
        return text
