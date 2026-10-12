import asyncio, json, sys, websockets
from pathlib import Path

NUM   = "+19415499805" #sys.argv[1]
GROUP = "zCV6AAXf4WH28L62kT/iMZ4m6Mrck6ZIwiJhHupA5Ck="  #sys.argv[2] if len(sys.argv) > 2 else None   # internal_id; omit = all groups
LOG   = None  # open(Path(__file__).parent / "signal-sample.jsonl", "a")  # disabled: dump.py is for viewing only

async def main():
    async for ws in websockets.connect(f"ws://127.0.0.1:8080/v1/receive/{NUM}"):
        try:
            async for raw in ws:
                msg = json.loads(raw)
                env = msg.get("envelope", {})
                # own-device messages arrive as syncMessage.sentMessage
                dm  = env.get("dataMessage") \
                      or (env.get("syncMessage") or {}).get("sentMessage") \
                      or {}
                gid = (dm.get("groupInfo") or {}).get("groupId")
                if not gid or (GROUP and gid != GROUP):
                    continue
                print(json.dumps(msg, indent=2), flush=True)
                if LOG:
                    LOG.write(raw.strip() + "\n"); LOG.flush()
        except websockets.ConnectionClosed:
            continue

asyncio.run(main())