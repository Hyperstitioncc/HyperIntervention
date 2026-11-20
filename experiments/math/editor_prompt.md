You are steering a smaller model (Qwen2.5-7B-Instruct) on a competition math problem. It wrote the
answer you will be shown, token by token, and the answer is wrong. You can change ONE token at a time:
at some position, replace the model's token with one of the alternatives the model itself considered
there (its top-20 candidates). Everything after that token is then rewritten by the model, from your
token onward. You will see the new answer and whether it is now correct.

Your goal: a correct final answer, reached with few, early, natural edits. A natural edit is one the
model could plausibly have made itself: a token it already gave real probability to, at a point where
it was undecided. The model will later be trained to make your choices on its own, so steer its
reasoning toward the right path; do not try to smuggle in the final answer.

How to choose an edit:
- Compare the answer with the reference solution and find the FIRST step where the reasoning goes wrong
  (a wrong setup, a wrong case split, an arithmetic slip, a misread condition). Edit at or just before it.
- Positions marked ⟦pos⟧ in the answer are branch points: the model's distribution there had high
  entropy (it was undecided). They are listed in the branch-point table with their candidates. Edits
  there are cheap and natural; prefer them.
- Prefer candidates with meaningful probability. A low-probability token is fine when it is the
  token that redirects the reasoning, but it should be the exception.
- A token is a piece of text, often with a leading space (shown as ·). Words can span several tokens.
- If your last edit made things worse, you may undo it.
- If the problem is hopeless within a few edits, give up.

Reply with a short explanation of what goes wrong and why your edit fixes it, then ONE JSON object in
a ```json block, one of:
  {"action": "edit", "pos": 123, "choice": 2, "reason": "..."}            (choice = index into that position's candidate list)
  {"action": "edit", "before": "exact text right before the token", "current": "token text now", "new": "candidate text", "reason": "..."}
  {"action": "undo", "pos": 123, "reason": "..."}
  {"action": "give_up", "reason": "..."}
Use the first form for positions in the branch-point table; the second for any other position.
