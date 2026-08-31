"""
Before submitting the assignment, describe here in a few sentences what you would have
built next if you spent 2 more hours on this project:

The two safety judges currently share the same underlying model (gpt-3.5-turbo, kept fixed
per the assignment's constraint) with different prompts, so they can share systematic blind
spots -- they aren't truly independent signals despite running as separate calls. Within
that constraint, I'd add a lightweight trained classifier (a separate, non-LLM component,
not a change to the generation/judge model) as a genuinely independent backstop, and build
a small human-labeled eval set to measure each judge's actual precision/recall, rather than
only checking system-level pass/refuse rates against itself. I'd also expand batch_eval.py
into a proper CI regression suite that runs on every prompt change, add adversarial
robustness testing for the unsafe-word scan (misspellings, spacing tricks), add prompt
versioning (so prompt changes are tracked and rollback-able independent of code deploys),
retrieval-augmented fact-checking for stories referencing real animals/places/science,
persisted story/session history, and a simple web UI instead of a CLI.
"""
import engine

MAX_FEEDBACK_ROUNDS = 3


def _print_trace(trace: list[str]) -> None:
    print("\n--- trace ---")
    for line in trace:
        print(line)
    print("-------------\n")


def main() -> None:
    user_request = input("What kind of story do you want to hear? ")
    result = engine.produce_story(user_request)
    _print_trace(result["trace"])

    if not result["passed"]:
        print(
            "I wasn't able to generate a story I'm confident is appropriate for this "
            "request. Could you try rephrasing it?"
        )
        return

    story = result["story"]
    kid_reaction = result["kid_reaction"]

    for round_num in range(1, MAX_FEEDBACK_ROUNDS + 1):
        print(story)
        print(f"\n\U0001F9D2 Kid reaction: {kid_reaction}\n")

        prompt = "Any feedback, or should I finalize this? "
        if kid_reaction and any(w in kid_reaction.lower() for w in ["confus", "bored", "didn't like", "scary", "boring"]):
            prompt = (
                "Note: the simulated child reader's reaction above sounded mixed. "
                "Any feedback, or should I finalize this? "
            )

        user_feedback = input(prompt).strip()
        if not user_feedback or user_feedback.lower() in {"no", "done", "looks good", "finalize", "good"}:
            print("\nGreat, enjoy the story!")
            return

        print(f"\n--- revising (feedback round {round_num}/{MAX_FEEDBACK_ROUNDS}) ---")
        result = engine.produce_story(user_request, revision_note=user_feedback, starting_draft=story)
        _print_trace(result["trace"])

        if not result["passed"]:
            print(
                "I wasn't able to produce a revised story I'm confident is appropriate. "
                "Keeping the previous version instead.\n"
            )
            print(story)
            return

        story = result["story"]
        kid_reaction = result["kid_reaction"]

    print("\nReached the feedback round limit -- here's the latest version:")
    print(story)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"\nSomething went wrong talking to the story service: {e}")
        print("Please check your OPENAI_API_KEY and billing status, then try again.")
