"""Drive one real text interview through a running server (scripts/dev.py) and print what each
AI role produced. Uses dev auth, so run it against local dev only.

    uv run python scripts/smoke.py [http://localhost:8000]
"""

from __future__ import annotations

import json
import sys
import time
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
CREATOR = {"x-dev-creator": "00000000-0000-0000-0000-00000000c0de"}
BRIEF = ("A twin of my mother, Sunita, so my kids can ask her about her recipes and her childhood "
         "in Jaipur. She speaks Hindi and English and mixes them. Don't bring up my late father.")
ANSWERS = [
    "Haan, I learned cooking from my aunt Kamla in Jaipur. Every Sunday we made dal baati.",
    "For the dal, first you roast jeera in ghee until it smells like toast, then hing, then the "
    "boiled moong and chana dal. Baati goes in the tandoor till it cracks.",
    "My father... I don't really want to talk about that.",
    "ok",
]


def step(name: str, r: httpx.Response) -> dict:
    t = r.elapsed.total_seconds()
    if r.status_code >= 400:
        print(f"✗ {name} [{r.status_code}] {r.text[:400]}")
        sys.exit(1)
    print(f"✓ {name} ({t:.1f}s)")
    return r.json()


def main() -> None:
    c = httpx.Client(base_url=BASE, timeout=120)
    twin = step("brief → spec", c.post("/api/twins", json={"brief": BRIEF}, headers=CREATOR))
    tid = twin["twin_id"]
    spec = c.get(f"/api/twins/{tid}/spec", headers=CREATOR).json()["latest"]["spec"]
    print("  domains:", [d["id"] for d in spec["content_domains"]],
          "| sensitivities:", spec.get("sensitivities"))
    step("approve", c.post(f"/api/twins/{tid}/spec/approve", headers=CREATOR))
    link = step("invite", c.post(f"/api/twins/{tid}/invites", json={}, headers=CREATOR))["link"]
    s = step("start text session", c.post("/api/sessions", json={
        "invite_token": link.rsplit("/", 1)[1], "modality": "text"}))
    auth = {"authorization": f"Bearer {s['session_token']}"}
    print("  opening:", s["history"][0]["text"])
    for i, text in enumerate(ANSWERS):
        print(f"\n  SUBJECT: {text}")
        t0 = time.time()
        r = step("turn", c.post("/api/turn", headers=auth, json={
            "client_turn_id": f"smoke-{uuid.uuid4().hex[:8]}", "subject": {"text": text},
            "messages": [{"text": text, "at_ms": 0}]}))
        print(f"  INTERVIEWER ({time.time() - t0:.1f}s): {r['reply']}")
        a = step("analyze", c.post(f"/api/turn/{r['turn_id']}/analyze", headers=auth))
        print(f"  → {a.get('engagement')} / {a.get('policy')}: {a.get('guidance', '')[:160]}")
        del i
    step("end", c.post(f"/api/sessions/{s['session_id']}/end", headers=auth, json={"reason": "user"}))
    cov = c.get(f"/api/twins/{tid}/coverage", headers=CREATOR).json()
    print("\n  slots:", [(x["domain_id"], x["item"], x["slot"], x["status"]) for x in cov["slots"]][:12])
    print("  never raise:", cov["do_not_raise"])
    print("  notes:", [n["text"] for n in cov["notes"]][:6])
    ans = step("ask twin", c.post(f"/api/twins/{tid}/ask", headers=CREATOR,
                                  json={"question": "How do you start the dal?"}))
    print("  TWIN:", json.dumps(ans.get("answer"), ensure_ascii=False)[:300])
    print(f"\ncreator view: {BASE}/creator#/twin/{tid}/sessions")


if __name__ == "__main__":
    main()
