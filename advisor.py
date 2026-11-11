"""The Advisor on a downstream: pure request rewrites (no I/O, no state)."""
import copy

from routing import is_advisor

MAX_CALLS = 3
PROMPT = ("You are an advisor to the assistant in this conversation, a stronger model it consulted mid-task. "
          "You can't run tools. Read the conversation and the assistant's work so far, then advise it on what to do "
          "next: what it may have missed or got wrong, the risks in its current approach, and a concrete next step. "
          "Be brief and specific. Don't redo its work, and don't address the user.")
TOOL = {"name": "advisor",
        "description": "Ask a stronger model for advice on the task: what you may be missing and what to do next. "
                       "Takes no input; it reads the whole conversation.",
        "input_schema": {"type": "object", "properties": {}}}


def definition(req: dict):
    """The Advisor tool definition in the request, or None."""
    for t in req.get("tools") or []:
        if isinstance(t, dict) and is_advisor(t):
            return t
    return None


def strip_advisor(req: dict) -> bool:
    """Every downstream: drop the Advisor tool definition and its calls and results from the history
    (server_tool_use named advisor, advisor_tool_result): a downstream can't run it and rejects the types.
    A message the drop leaves empty goes too. True if anything changed."""
    tools = req.get("tools")
    changed = False
    if isinstance(tools, list) and any(isinstance(t, dict) and is_advisor(t) for t in tools):
        req["tools"] = [t for t in tools if not (isinstance(t, dict) and is_advisor(t))]
        changed = True
    adv = lambda b: isinstance(b, dict) and (b.get("type") == "advisor_tool_result"
                                             or (b.get("type") == "server_tool_use" and b.get("name") == "advisor"))
    msgs = req.get("messages")
    for m in msgs if isinstance(msgs, list) else []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list) and any(adv(b) for b in c):
            m["content"], changed = [b for b in c if not adv(b)], True
    if changed and isinstance(msgs, list):
        # ponytail: an emptied message is dropped, which can leave two same-role messages side by side
        req["messages"] = [m for m in msgs if not (isinstance(m, dict) and m.get("content") == [])]
    return changed


def _is_call(b) -> bool:
    return isinstance(b, dict) and b.get("type") == "server_tool_use" and b.get("name") == "advisor"


def _is_result(b) -> bool:
    return isinstance(b, dict) and b.get("type") == "advisor_tool_result"


def _result_block(b: dict) -> dict:
    c = b.get("content")
    kind = c.get("type") if isinstance(c, dict) else None
    if kind == "advisor_result":
        text, err = c.get("text") or "", False
    elif kind == "advisor_redacted_result":
        text, err = "(advice redacted)", False
    else:
        text, err = "advisor unavailable", True
    out = {"type": "tool_result", "tool_use_id": b.get("tool_use_id"), "content": text}
    if err:
        out["is_error"] = True
    return out


def to_client_form(req: dict) -> bool:
    """Swap the Advisor definition for TOOL and split each call/result pair into a client round trip:
    assistant [..., tool_use], user [tool_result], assistant [rest]. Orphans and empty messages are dropped."""
    changed = False
    tools = req.get("tools")
    if isinstance(tools, list) and any(isinstance(t, dict) and is_advisor(t) for t in tools):
        req["tools"] = [TOOL if (isinstance(t, dict) and is_advisor(t)) else t for t in tools]
        changed = True
    msgs = req.get("messages")
    if not isinstance(msgs, list):
        return changed
    out = []
    for m in msgs:
        c = m.get("content") if isinstance(m, dict) else None
        if not (isinstance(c, list) and any(_is_call(b) or _is_result(b) for b in c)):
            out.append(m)
            continue
        changed = True
        results = {b.get("tool_use_id"): b for b in c if _is_result(b)}
        cur = []
        for b in c:
            if _is_result(b):
                continue
            if _is_call(b):
                r = results.get(b.get("id"))
                if r is None:
                    continue
                cur.append({"type": "tool_use", "id": b["id"], "name": "advisor", "input": {}})
                out.append({**m, "content": cur})
                out.append({"role": "user", "content": [_result_block(r)]})
                cur = []
            else:
                cur.append(b)
        out.append({**m, "content": cur})
    req["messages"] = [m for m in out if not (isinstance(m, dict) and m.get("content") == [])]
    return changed


def advice_request(req: dict, partial: list, model: str) -> dict:
    """The body for the advice call: the conversation, the work so far, then PROMPT. No thinking."""
    body = copy.deepcopy(req)
    msgs = list(body.get("messages") or [])
    if partial:
        msgs.append({"role": "assistant", "content": copy.deepcopy(partial)})
    msgs.append({"role": "user", "content": [{"type": "text", "text": PROMPT}]})
    body.update(messages=msgs, model=model, max_tokens=4096, stream=False, tool_choice={"type": "none"})
    body.pop("thinking", None)
    return body


def continue_request(req: dict, partial: list, call_id: str, advice: str, error: bool, offer: bool) -> dict:
    """The request that carries on after an advisor call: the call and its result appended; TOOL dropped unless offered."""
    body = copy.deepcopy(req)
    msgs = list(body.get("messages") or [])
    msgs.append({"role": "assistant", "content": copy.deepcopy(partial) + [
        {"type": "tool_use", "id": call_id, "name": "advisor", "input": {}}]})
    res = {"type": "tool_result", "tool_use_id": call_id, "content": advice}
    if error:
        res["is_error"] = True
    msgs.append({"role": "user", "content": [res]})
    body["messages"] = msgs
    if not offer and isinstance(body.get("tools"), list):
        body["tools"] = [t for t in body["tools"] if not (isinstance(t, dict) and t.get("name") == "advisor")]
    return body


def advice_text(body: dict) -> str:
    """The text blocks of an advice answer joined, or a placeholder."""
    t = "\n".join(b.get("text", "") for b in body.get("content") or []
                  if isinstance(b, dict) and b.get("type") == "text" and b.get("text"))
    return t.strip() or "(no advice)"
