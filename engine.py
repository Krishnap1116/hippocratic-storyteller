"""Orchestration: generate -> deterministic pre-check -> dual LLM judges ->
targeted regeneration (fail-closed) -> kid-persona reaction."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

import checks
import prompts

MODEL = "gpt-3.5-turbo"
MAX_PRECHECK_ATTEMPTS = 5

MAX_JUDGE_ATTEMPTS = 3
QUALITY_SCORE_THRESHOLD = 3.0  

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client


def call_model(system_prompt: str, user_prompt: str, max_tokens: int = 800, temperature: float = 0.7) -> str:
    """One retry on transient API failure; raises on a second failure rather
    than pretending it succeeded."""
    client = _get_client()
    last_error = None
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001 -- broad on purpose, this is the API boundary
            last_error = e
            if attempt == 0:
                time.sleep(1)
    raise RuntimeError(f"OpenAI call failed after retry: {last_error}")


def _parse_json_response(raw: str, system_prompt: str, user_prompt: str) -> dict | None:
    """Try to parse; on failure, re-ask once for strict JSON. Returns None if
    it still can't be parsed , callers must treat None as fail-safe/unsafe."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    retry_prompt = (
        f"{user_prompt}\n\nYour previous response was not valid JSON. "
        f"Respond with ONLY the JSON object, nothing else."
    )
    raw2 = call_model(system_prompt, retry_prompt, temperature=0.0)
    try:
        return json.loads(raw2)
    except json.JSONDecodeError:
        return None


def generate_story(user_request: str, revision_note: str | None = None, prior_draft: str | None = None) -> str:
    prompt = prompts.build_generator_prompt(user_request, revision_note, prior_draft)
    return call_model(prompts.GENERATOR_SYSTEM_PROMPT, prompt, max_tokens=2800, temperature=0.8).strip()


def expand_story(draft: str, words_needed: int) -> str:
    prompt = prompts.build_expand_prompt(draft, words_needed)
    return call_model(prompts.EXPAND_SYSTEM_PROMPT, prompt, max_tokens=2800, temperature=0.6).strip()


def judge_story(user_request: str, story: str) -> dict | None:
    prompt = prompts.build_judge_prompt(user_request, story)
    raw = call_model(prompts.JUDGE_SYSTEM_PROMPT, prompt, max_tokens=500, temperature=0.0)
    return _parse_json_response(raw, prompts.JUDGE_SYSTEM_PROMPT, prompt)


def judge_safety_second_opinion(story: str) -> dict | None:
    prompt = prompts.build_safety_only_prompt(story)
    raw = call_model(prompts.SAFETY_ONLY_JUDGE_SYSTEM_PROMPT, prompt, max_tokens=150, temperature=0.0)
    return _parse_json_response(raw, prompts.SAFETY_ONLY_JUDGE_SYSTEM_PROMPT, prompt)


def kid_persona_reaction(story: str) -> str:
    prompt = prompts.build_kid_persona_prompt(story)
    return call_model(prompts.KID_PERSONA_SYSTEM_PROMPT, prompt, max_tokens=120, temperature=0.9).strip()


def avg_score(scores: dict) -> float:
    vals = list(scores.values())
    return sum(vals) / len(vals) if vals else 0.0


def produce_story(user_request: str, revision_note: str | None = None, starting_draft: str | None = None) -> dict:
    trace: list[str] = []
    draft = starting_draft
    revision_instruction = revision_note
    attempts_used = 0
    precheck_attempts = 0
    judge_attempts = 0
    safety_ever_failed = False
    next_action = "generate"

    while True:
        if precheck_attempts >= MAX_PRECHECK_ATTEMPTS or judge_attempts >= MAX_JUDGE_ATTEMPTS:
            break
        attempts_used += 1
        trace.append(
            f"--- attempt {attempts_used} (pre-check {precheck_attempts}/{MAX_PRECHECK_ATTEMPTS}, "
            f"judge {judge_attempts}/{MAX_JUDGE_ATTEMPTS}) ---"
        )

        if next_action == "expand":
            words_needed = checks.WORD_COUNT_MIN - checks.run_pre_check(draft)["word_count"]
            draft = expand_story(draft, words_needed)
            trace.append(f"expanded draft with one appended scene (needed ~{words_needed} more words)")
        else:
            draft = generate_story(user_request, revision_instruction, draft)
            trace.append("generated draft")
        next_action = "generate"  

        pre = checks.run_pre_check(draft)
        trace.append(f"pre-check: passed={pre['passed']} notes={pre['notes']}")

        if not pre["passed"]:
            precheck_attempts += 1
            if pre["word_count"] < checks.WORD_COUNT_MIN:
                revision_instruction = (
                    f"the story is too short ({pre['word_count']} words, need at least "
                    f"{checks.WORD_COUNT_MIN})"
                )
                if draft:  
                    trace.append("pre-check failed on length -- expanding existing draft next, not a full rewrite")
                    next_action = "expand"
                else:
                    revision_instruction += " -- write a real story arc"
                continue
            fixes = []
            if pre["word_count"] > checks.WORD_COUNT_MAX:
                fixes.append(f"the story is too long ({pre['word_count']} words) -- tighten it up")
            if pre["unsafe_hits"]:
                fixes.append(f"remove/replace these flagged words: {pre['unsafe_hits']}")
            revision_instruction = " | ".join(fixes)
            trace.append("pre-check failed -- regenerating without spending a judge call")
            continue

        # Run both judges concurrently -- they're independent calls, no reason to pay
        # their latency sequentially (matters at real conversational scale).
        with ThreadPoolExecutor(max_workers=2) as pool:
            judge1_future = pool.submit(judge_story, user_request, draft)
            judge2_future = pool.submit(judge_safety_second_opinion, draft)
            judge1 = judge1_future.result()
            judge2 = judge2_future.result()

        if judge1 is None or judge2 is None:
            judge_attempts += 1
            trace.append("judge JSON unparsable after retry -- treating as fail-safe unsafe")
            revision_instruction = "Previous attempt could not be evaluated; please rewrite it clearly and simply."
            continue

        safety_pass = bool(judge1.get("safety_pass")) and bool(judge2.get("safety_pass"))
        if not safety_pass:
            safety_ever_failed = True
        avg_score_val = avg_score(judge1.get("scores", {}))
        trace.append(
            f"judge1.safety_pass={judge1.get('safety_pass')} "
            f"judge2.safety_pass={judge2.get('safety_pass')} "
            f"combined_safety_pass={safety_pass} avg_quality={avg_score_val:.2f}"
        )

        unsafe = not safety_pass  # pre["unsafe_hits"] already gated above
        low_quality = avg_score_val < QUALITY_SCORE_THRESHOLD

        if not unsafe and not low_quality:
            trace.append("PASSED: safe and above quality threshold")
            reaction = kid_persona_reaction(draft)
            trace.append("kid-persona reaction generated")
            return {
                "story": draft,
                "passed": True,
                "judge": judge1,
                "kid_reaction": reaction,
                "trace": trace,
                "attempts_used": attempts_used,
                "safety_ever_failed": safety_ever_failed,
                "last_reason": None,
            }

        judge_attempts += 1
        reasons = []
        if not safety_pass:
            checklist_note = judge1.get("reasoning") if not judge1.get("safety_pass") else None
            holistic_note = judge2.get("reason") if not judge2.get("safety_pass") else None
            reasons.append(checklist_note or holistic_note or "a judge flagged a safety concern")
        if low_quality:
            feedback_bits = [v for v in judge1.get("feedback", {}).values() if v]
            reasons.append("quality below threshold: " + "; ".join(feedback_bits) if feedback_bits else "quality below threshold")
        revision_instruction = " | ".join(reasons)
        trace.append(f"REGENERATING because: {revision_instruction}")

    trace.append("FAIL-CLOSED: max attempts reached without a safe, passing draft -- refusing to deliver")
    return {
        "story": None,
        "passed": False,
        "judge": None,
        "kid_reaction": None,
        "trace": trace,
        "attempts_used": attempts_used,
        "safety_ever_failed": safety_ever_failed,
        "last_reason": revision_instruction,
    }
