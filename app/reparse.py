"""Re-run the LLM parse over existing listings (after a prompt change), then rescore.

  python -m app.reparse            # listings currently marked relevant
  python -m app.reparse --all      # everything not gone
  python -m app.reparse --flagged  # live listings with red flags (after a red-flag prompt change)
"""
import asyncio
import sys

import httpx

from . import db, parse, score


async def main(all_rows: bool, flagged: bool = False):
    con = db.connect()
    where = "status != 'gone'" + ("" if all_rows else " AND relevant = 1")
    if flagged:
        where = "status IN ('active', 'pending') AND relevant = 1 AND COALESCE(red_flags, '[]') NOT IN ('[]', '')"
    rows = con.execute(f"SELECT * FROM listings WHERE {where}").fetchall()
    changed = 0
    async with httpx.AsyncClient() as http:
        for r in rows:
            p = await parse.parse(http, dict(r))
            if p is None:
                continue
            changed += p["relevant"] != r["relevant"]
            cols = ", ".join(f"{k} = ?" for k in p)
            con.execute(f"UPDATE listings SET {cols}, parsed = 1, equipment = NULL WHERE id = ?", (*p.values(), r["id"]))
            con.commit()
    score.rescore_all(con)
    print(f"reparsed {len(rows)}, relevance flipped on {changed}")


if __name__ == "__main__":
    db.init()
    asyncio.run(main("--all" in sys.argv, "--flagged" in sys.argv))
