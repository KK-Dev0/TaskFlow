"""Provider-independent AI layer with deterministic fallbacks.

* Works with NO API key: rule-based decomposition, planning and reviews.
* With ANTHROPIC_API_KEY or OPENAI_API_KEY the same functions ask an LLM for
  structured JSON. Output is ALWAYS validated here before it can become a
  proposal, and proposals only change data after the user approves them.
"""
import json
import logging
import os
import re
import urllib.request

log = logging.getLogger("taskflow.ai")

MAX_SUBTASKS = 8


def provider():
    forced = os.environ.get("AI_PROVIDER", "").strip().lower()
    if forced in ("anthropic", "openai"):
        key = os.environ.get("ANTHROPIC_API_KEY" if forced == "anthropic" else "OPENAI_API_KEY")
        return forced if key else None
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


def enabled():
    return provider() is not None


def _extract_json(text):
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


def llm_json(system, user, max_tokens=1200):
    """Return parsed JSON from the configured provider, or None on any failure."""
    prov = provider()
    if not prov:
        return None
    try:
        if prov == "anthropic":
            body = {"model": os.environ.get("AI_MODEL", "claude-sonnet-5-5"),
                    "max_tokens": max_tokens, "system": system,
                    "messages": [{"role": "user", "content": user}]}
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                headers={"content-type": "application/json",
                         "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                         "anthropic-version": "2023-06-01"})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read().decode())
            text = "".join(b.get("text", "") for b in data.get("content", []))
        else:
            base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
            body = {"model": os.environ.get("AI_MODEL", "gpt-4o-mini"),
                    "response_format": {"type": "json_object"}, "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}]}
            req = urllib.request.Request(
                base + "/chat/completions", data=json.dumps(body).encode(),
                headers={"content-type": "application/json",
                         "authorization": "Bearer " + os.environ["OPENAI_API_KEY"]})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read().decode())
            text = data["choices"][0]["message"]["content"]
        return _extract_json(text)
    except Exception as exc:  # network, quota, bad JSON ... never crash the request
        log.warning("AI call failed: %s", exc)
        return None


# ---------- validation helpers (AI output is untrusted) ----------

def clean_subtasks(items, parent_estimate=None):
    out = []
    if not isinstance(items, list):
        return out
    for it in items[:MAX_SUBTASKS]:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title", "")).strip()[:120]
        if not title:
            continue
        try:
            est = int(it.get("estimate_min") or 0)
        except (TypeError, ValueError):
            est = 0
        est = max(5, min(est, 240)) if est else None
        try:
            pr = int(it.get("priority") or 2)
        except (TypeError, ValueError):
            pr = 2
        out.append({"title": title, "estimate_min": est, "priority": max(1, min(pr, 4))})
    return out


TEMPLATES = [
    (r"\b(write|essay|report|assignment|paper|thesis|article|blog)\b",
     [("Outline the structure", 1), ("Research and collect sources", 2), ("Write the first draft", 3),
      ("Revise and edit", 2), ("Proofread and submit", 1)]),
    (r"\b(study|exam|revise|revision|test|quiz|learn)\b",
     [("List topics and syllabus", 1), ("Review notes and lessons", 3), ("Solve practice questions", 3),
      ("Take a timed mock test", 2), ("Revise weak areas", 2)]),
    (r"\b(build|code|develop|app|website|project|implement|deploy|api|feature)\b",
     [("Define scope and requirements", 1), ("Design the approach", 2), ("Implement the core", 4),
      ("Test and fix bugs", 2), ("Document and deploy", 1)]),
    (r"\b(plan|organi[sz]e|event|trip|party|meeting)\b",
     [("Define goal and constraints", 1), ("List and assign steps", 2), ("Book or prepare resources", 3),
      ("Confirm details", 1), ("Run it and follow up", 2)]),
]
DEFAULT_TEMPLATE = [("Clarify the goal and finish line", 1), ("Plan the steps", 1),
                    ("Do the main work", 4), ("Review the result", 1), ("Wrap up and follow through", 1)]


def rule_decompose(title, estimate_min=None):
    text = (title or "").lower()
    steps = DEFAULT_TEMPLATE
    for pat, tpl in TEMPLATES:
        if re.search(pat, text):
            steps = tpl
            break
    total = sum(w for _t, w in steps)
    est_total = estimate_min or 150
    out = []
    for label, w in steps:
        est = max(10, int(round(est_total * w / total / 5.0)) * 5)
        out.append({"title": label, "estimate_min": est, "priority": 2})
    return out


def decompose(title, notes="", estimate_min=None):
    """-> (subtasks, source)"""
    data = llm_json(
        "You break tasks into 3-8 concrete, ordered subtasks. Reply ONLY with JSON: "
        '{"subtasks":[{"title":str,"estimate_min":int,"priority":1-4}]}. '
        "Titles are short imperative phrases. Do not invent deadlines.",
        f"Task: {title}\nNotes: {notes}\nTotal estimate (minutes): {estimate_min or 'unknown'}")
    if data:
        subs = clean_subtasks(data.get("subtasks"))
        if len(subs) >= 2:
            return subs, "llm"
    return clean_subtasks(rule_decompose(title, estimate_min)), "rules"


def plan_goal(goal, parsed):
    """Turn a goal into a parent task + subtasks. parsed = nlp.parse output.
    -> (payload dict, source)"""
    data = llm_json(
        "You turn a goal into one parent task plus 3-8 ordered subtasks. Reply ONLY with JSON: "
        '{"title":str,"subtasks":[{"title":str,"estimate_min":int,"priority":1-4}]}. '
        "Do not invent dates; the app handles dates.",
        f"Goal: {goal}")
    title = parsed["title"]
    subs, source = [], "rules"
    if data:
        subs = clean_subtasks(data.get("subtasks"))
        if len(subs) >= 2:
            source = "llm"
            title = str(data.get("title") or title).strip()[:200] or title
    if source == "rules":
        subs = clean_subtasks(rule_decompose(goal, parsed.get("estimate_min")))
    est = parsed.get("estimate_min") or sum(s["estimate_min"] or 0 for s in subs) or None
    return {"parent": {"title": title, "priority": parsed["priority"],
                       "due_at": parsed["due_at"].strftime("%Y-%m-%d %H:%M:%S") if parsed.get("due_at") else None,
                       "estimate_min": est, "tags": parsed.get("tags", [])},
            "subtasks": subs}, source


def narrative(kind, stats):
    """Short coaching text for daily/weekly reviews. -> (summary, tips, source)"""
    data = llm_json(
        "You are a concise productivity coach. Reply ONLY with JSON: "
        '{"summary":str,"tips":[str,str,str]}. Be specific to the numbers; no guilt, no fluff.',
        f"Kind: {kind}\nStats: {json.dumps(stats)}", max_tokens=500)
    if data and isinstance(data.get("summary"), str):
        tips = [str(t)[:200] for t in (data.get("tips") or []) if t][:4]
        return data["summary"][:600], tips, "llm"
    return None, None, "rules"
