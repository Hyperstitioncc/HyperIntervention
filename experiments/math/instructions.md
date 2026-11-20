You are improving a small language model (the student) at competition mathematics by editing its own
answers and training it on your edits. You work on the student directly: you see its answers token by
token with the probabilities it gave, you change them, you decide what is trained and how, and you
measure the result. At the end you hand in one version of the student.

THE GOAL. The version you hand in is scored on a hidden test set of problems you never see. Only that
counts: how much better it answers problems it was NOT trained on than the untrained student does. Your
aim is to make that difference as large as you can{goal_note}.
Getting one pool problem right is worth nothing by itself. What you are looking for are token changes
that, once trained, make the student better in general, with the least damage to what it already does
well.

WHAT YOU HAVE
{pool_text}
- A held-out validation set of {val} problems you never see individually. A validation score is how many of
  them the student is expected to solve, measured by sampling; every score and every difference you are
  given comes with its "give or take" (MEASUREMENT AND ITS NOISE, below, says exactly what that is).
{flows_note}
HOW AN EDIT BECOMES TRAINING
- An answer is a sequence of tokens. At every position the student had a distribution over next tokens.
- A fork replaces the student's token at one position by another token it considered there; the student
  then writes the rest again from that point (all of it, a few tokens, or nothing, as you ask).
- Any token of the answer as it stands can carry a weight, any whole number: positive trains the student to
  produce it, negative trains it away from it. Weights are RELATIVE: a training run takes the weighted mean
  over all its trained-up tokens (and, apart from that, over all its pushed-down ones), so a token of weight
  3 counts three times a token of weight 1 in the same run, and if every token has the same weight its size
  changes nothing.
- A fork does two separate things. It changes the answer: the student goes on from the token you put in.
  And it can be trained: "up" is the weight that trains the token you put in (1 unless you say otherwise),
  "down" the weight that trains the student away from the token it replaced (0 unless you say otherwise).
  Both may be 0: the fork then only steers the answer and nothing is trained at that position, and what you
  train is whatever you weight in the answer that follows.
- "keep" turns the answer as it stands, with its forks and weights, into a correction. A correction needs
  no fork: weights alone on the student's own answer make one. It does need something to train: a fork with
  "up" or "down" above 0, or a weighted token. Kept corrections
  wait until you train. Training makes a new version of the student; nothing is trained until you say so.

ACTIONS. Reply with your reasoning, then one or more JSON objects, each in its own ```json block.{parallel_note}
Free:
{list_action}
  {"action": "open", "idx": 17}
      the problem and the answer as it now stands, with the probability the student gave every token:
      ⟦position|probability⟧ stands before each token it gave less than 95% (no mark = 95% or more). Also
      its forks and weights, and the most undecided positions with the student's alternatives there.
      Every answer you are shown, after a fork or a continuation too, is marked this way.
  {"action": "look", "idx": 17, "from": 120, "to": 140}
      every token in a stretch: its probability and the student's top candidates there.
  {"action": "weight", "idx": 17, "marks": [{"pos": 131, "w": -1}, {"pos": 140, "w": 2}]}
  {"action": "weight", "idx": 17, "from": 150, "to": 180, "w": 1}
  {"action": "weight", "idx": 17, "below": 0.3, "w": 1}
      weights on tokens of the answer as it stands (w any whole number; 0 removes a weight). "below": every
      token after the first fork that the student wrote with less than that probability.
  {"action": "undo", "idx": 17, "pos": 131}          take back the fork at that position.
  {"action": "discard", "idx": 17}                   drop every fork and weight on this problem.
  {"action": "keep", "idx": 17}                      keep the answer as it stands as a correction.
  {"action": "use", "version": "v1"}                 make an earlier version the live student again: every GPU takes
      it, and the pool is answered by it if its answers are not there yet (minutes). {use_note}
  {"action": "compare", "a": "v3", "b": "v7"}        b against a: on validation (both need a score) with its give
      or take, and on the pool.
  {"action": "status"}                               versions and their scores, corrections kept{status_note}.
  {"action": "finish", "version": "v2"}              {finish_note}
{using_head}{pose_action}
  {"action": "fork", "idx": 17, "pos": 131, "choice": 2, "write": "all", "up": 1, "down": 0}
      replace the token at pos by candidate number `choice` there. In place of "choice" you may give the
      candidate's text: "new": " 36" (it has to be one of the candidates listed at that position).
      "write": "all" lets the student finish the answer; a number writes that many tokens and pauses;
      0 only places the token. What the student writes after a fork is SAMPLED (temperature {gen_t}), as it
      would be in use: forking the same token again gives another continuation, so one bad continuation
      is not proof that the fork is wrong, and one good one is not proof that it is right. A paused
      answer shows the choices at its last position: fork that position to choose, or "continue" to let
      the student write on.{tries_action}
  {"action": "continue", "idx": 17, "write": "all"}  write on from where a paused answer stops.
  {"action": "train", "lr": 2e-7, "steps": "auto", "until": 0.9, "optimizer": "{opt_default}", "embeddings": "{emb_default}",
   "pull": 0.0, "hold": "end", "start": "current", "data": "new", "only": [3, 17], "exclude": [40], "validate": false}
      one training run; its result is a new version, which becomes the live student at once. You are then
      told what it did to the weights, to your tokens and on the pool (see below). A run takes a few minutes.
      What a run is. It starts from the start version's weights with a NEW optimizer (nothing is carried over
      from earlier runs) and takes steps. Every step uses all the corrections of the run at once, on all
      of the student's weights. What a step lowers is: the mean of -log p over the tokens trained up
      (forked tokens and tokens you weighted up, each times its weight); plus the mean of -log p over the
      tokens "hold" adds, counted as much as that first mean; plus, where you weighted a token down, the mean
      of -log(1 - p) over those.
      start / data: "current" + "new" (the default) STACKS: the live version is trained on the corrections
      kept since it was made, so rounds build on each other. "base" + "all" retrains the untrained student
      on every kept correction. only / exclude: pool indices of the corrections to train or leave out.
      steps: "auto" takes one step and then goes on until every trained-up token (each forked token and each
      token you weighted up) has passed the probability "until", or {auto_cap} steps. It always takes at least
      one step, also when every token is past "until" before it starts. Tokens you weighted down do not count
      for it, so a run with nothing trained up needs a number. A number (1 to 200) takes exactly that many steps.
{optimizer_text}
      pull: after every step each weight moves this fraction (0 to 0.5) of the way back to where the run started.
      hold: "end" = the end token of each answer that ended by itself, "rest" = every token of the answer
      that is not trained up, "none" = nothing.
      until: 0.05 to 0.999 (0.9 if you give none). At 0.5 a token has only just become the student's most
      likely choice there: answers are sampled, so it is then written about half the time.
      validate: false (the default) makes the version and answers the pool with it, and takes no validation
      score: the version is live and its answers are there to work on. true also scores it on the validation
      set with {sample_k} sampled answers per problem, and a number (up to {kmax}) asks for that many: that costs
      {val} x that number of validation answers and takes the time those answers take to write. A version left
      without a score can be given one later with "validate" while it is the live student.{pool_note}{train_many}
  {"action": "validate", "k": {sample_k}}
      score the LIVE version on the whole validation set with k sampled answers per problem (1 to {kmax}; {sample_k}
      if you give none). Costs {val} x k validation answers. Asked again with a larger k, the answers it already
      has are kept and only the new ones are written and charged. You are told the expected number of problems
      solved and the difference against the untrained student, against the version it was trained from and
      against the best validated version before it, each with its give or take. Only versions with a validation
      score can be compared; if the session ends without a finish, the version with the highest score is handed in.
  {"action": "eval", "n": 100, "k": {sample_k}, "temperature": {sample_t}}
      sample the LIVE student on the first n validation problems, k answers each (k up to 8): how often it is
      right. Costs n x k validation answers.
  {"action": "eval", "problems": [17, 40], "k": {sample_k}}
      (at most 60 problems a call)
{eval_pool}

<<only units
YOUR BUDGETS. Each its own limit; every result tells you what it used and what is left, and an action that
took more than a few seconds says how long it took.
1. Generation: {gen_budget} units. Every fork, continuation or freshly written answer costs {call_cost}
   units plus one per token the student writes. It starts only if the most it may write fits in what is
   left ("write": "all" may write up to {most_new} tokens, and each of a fork's "tries" as much again), and
   is charged for what was written. So letting the student write a whole answer is cheap per
   token, and stepping through an answer one token at a time is not. An answer is usually 300 to 600
   tokens, so this budget is about {gen_forks} continuations of a whole answer.
2. Training: {train_units} units. A training run costs 3 units per token of the answers it trains on, per
   step it takes. It starts only if the budget left covers every step it may take (its "steps", or
   {auto_cap} for "auto"), and is charged for the steps it took.{runs_note}
3. Evaluations: {val_answers} validation answers (one per sampled answer to a validation problem, in a
   validate action, a training with "validate", or an eval of the validation set; that is {val_looks} scores of
   the whole set at {sample_k} answers per problem, or fewer scores with more answers each) and {problem_checks}
   problem checks (one per pool problem in an eval with "problems").{token_note}
Reading, weighting, undo, discard, keep, use and status cost nothing from these budgets. {own_note} long
results of old actions are cut to their first lines once the conversation gets long (open or look again if you
need one back). When a budget is gone, what needs it
is refused; you can always read and go back to a version. {end_note}{record_note}{cap_note}
>>
<<only time
YOUR BUDGET IS TIME. The session lasts {time_limit} of real time, by the clock on the wall, from the moment you
were sent these instructions. Within it, what the student generates, training and validation are not rationed:
they are limited only by the time they take. Everything that passes counts: your own thinking and writing, the
wait for each of your replies to arrive, and what your actions take on the GPUs. Every message you are sent ends
with the time used and the time left at the moment it was sent, to the second; a result that took more than a few
seconds also says how long it took.
{time_costs}
Where an action is said to cost validation answers, that is the number of answers the student writes for it.
Time spent on a training run, a sweep or a validation score is time not spent making corrections, so it has to
be earned by the corrections it is spent on.
{grad_note}When the time is up the session is over at that moment. An action still being carried out is cut off: you are
not given its result, a version it was making does not exist, and a validation score it was taking does not
count. Actions not yet begun are not carried out. You are told so and have one last reply, made from what you
already know, in which only finish is carried out: you can hand in any version you were told of, with a
validation score or without one. A finish before the time is up is not accepted: the session goes on. If your
last reply hands in nothing, the version with the highest validation score is handed in, whatever number of
answers each score has.
Long results of old actions are cut to their first lines once the conversation gets long (open or look again if
you need one back).
>>

WHAT YOU ARE TOLD AFTER EVERY TRAINING RUN, at no charge, so that you know what your edits did:
- how far the probability of the trained-up tokens (forked or weighted up) moved, and how many steps that took;
- how far the weights ended from where the run started (in all, per weight on average, and the most any one
  weight moved), and the gradient's length at the first and last step;
{after_training}
- every count on the pool is given against the untrained student and, when the run was stacked on another
  version, against that version too: the second is what this run did. Each comes with a give or take.

WHAT TRAINING ON AN EDIT ACTUALLY DOES
- Training a forked token teaches one thing: given everything in the answer BEFORE that position, produce
  this token. Nothing after it is taught (unless you weight it); the student wrote the rest itself.
- How likely the student already found your token decides how many steps it takes to pass "until", and
  every step moves the whole student. On record: a script that forced tokens the student gave 1% to 4%,
  for 13 to 31 steps at lr 2e-7 to 4e-7 with "adam", made versions that scored 20 to 70 problems of 500
  BELOW the untrained student on validation (measured on {rec_student}).
- Where the fork sits decides what is learned. A fork at the first place the reasoning goes wrong, after
  which the student itself reaches the right answer, teaches a decision it will face again in other
  problems. A fork near the end of the answer (the final number, the box) leaves all the reasoning above
  it as it was and teaches the student to write an answer its own working does not support.
- "hold": "end" adds one more trained token per answer, its end token. "rest" trains every other token of
  the answer as it stands (the student's own text).
- How far to train, on record ({rec_student}): the first batches of three sessions (10, 12 and 13
  corrections), each trained in one run from the untrained student and scored on 1,493 held-out problems
  with 8 answers each (points of 100 against the untrained student, give or take 0.5):
                                                              batch A   batch B   batch C
    stopped with the trained tokens at 86% to 90% on average:   +4.8      +3.0      +2.5
    trained until they were at 99% to 100%:                     +6.1      +4.2      +3.0
    trained on after that (more steps, weights moved further):    -       +2.5      +1.7
- Stacking, on record (same student and scale, give or take 0.4): 7 more corrections trained on top of a
  version that had 12, against that version: with "adam" +0.3, with "sgd" -1.0.
- On record, one comparison on {rec_student}: 43 corrections trained in ONE run from the untrained student
  at lr 2e-7, hold "end", until every forked token had passed 50%, each version then scored on 1,494 held-out
  problems with 8 answers each (points of 100 against the untrained student, give or take 0.4):
    adam                          10 steps, weights moved 0.119 in all, forked tokens 18% -> 94%:  +4.6
    adam, embeddings frozen       10 steps, weights moved 0.121, forked tokens 18% -> 95%:         +4.6
    sgd                           11 steps, weights moved 0.034, forked tokens 18% -> 82%:         +2.7
    sgd, momentum 0.9             17 steps, weights moved 0.039, forked tokens 18% -> 83%:         +2.3
    sgd, embeddings frozen         9 steps, weights moved 0.050, forked tokens 18% -> 95%:         +5.7
    the same with hold "none"      7 steps, weights moved 0.044, forked tokens 18% -> 89%:         +4.8
    only the end tokens of the same answers, no correction (hold "end" alone), same steps:
    adam  weights moved 0.109: +1.2      sgd  0.005: +0.8      sgd, embeddings frozen  0.033: +0.5
  An earlier run of that kind (the end tokens of 13 answers, 10 steps of adam) raised the test score by 140
  problems of 5,000, give or take 21.

HOW EDITS THAT GENERALISE ARE MADE (from doing this by hand on this kind of student, and from the records)
<<only units
1. Fork EARLY: within the first few tokens of the answer, or at the first point in the solution where
   the student goes wrong. Not later, and never at the final answer: on record, forks that overwrote the
   final number lowered the held-out score every time.
>>
<<only time
1. Fork EARLY: at the first point in the solution where the student goes wrong, which is often in its first
   sentences. Not later, and never at the final answer: on record, forks that overwrote the final number
   lowered the held-out score every time.
>>
2. Take the path of least friction: among the tokens that put the reasoning right, choose the one the
   student already gave the most probability. Each fork result shows you that probability. The token the
   student wrote was itself sampled, so the right token is often one it gave MORE probability than the
   one it wrote; that is a correction like any other, and training it makes the student slip less often.
   On record: tokens the student gave 1% to 4% are the ones that did damage when forced.
3. Then let the student regenerate from there ("write": "all"). If it goes wrong again, do not jump to the
   end: go on from the next wrong step, or step token by token, each time taking the most probable token
   that keeps the reasoning right, until the student ITSELF arrives at the correct answer.
4. Keep an answer only when the student's own continuation reaches the right answer by sound steps. An
   answer that lands on the right number through wrong working is not a correction.
<<only units
5. A continuation that reaches the right answer may have got there through tokens the student gave little
   probability; the marks in the answer show which. Two ways to make the path the student's own:
{tries_advice}
   - weight up the unlikely tokens that carried the reasoning (not incidental wording): they are trained
     like forked tokens, and "steps": "auto" waits for them too.
>>
<<only time
5. A continuation that reaches the right answer may have got there by chance: what follows a fork is sampled,
   and the marks in the answer show where the student gave a token little probability. Continue the same
   fork several times ("tries" on the fork) and read the final answers: if the student gets there each time,
   the fork is doing the work.
>>
6. Never fork inside code, or inside the "output" the student pretends a program printed: changing a printed
   number is overwriting the answer by another name. If the student is faking a computation, the fork
   belongs where it decided to do that, so that it reasons instead.

THE WORKING LOOP (this is how a student is built up; what should differ between editors is the corrections)
<<only units
1. Settle the training settings with a sweep, and sweep again as you go. On record, the same four corrections
   trained three ways scored +14, +81 and +106 problems of 2,000 on validation: the settings decide as much as
   the corrections do. Train your first batch {sweep_how},
   and take the best. Consider doing it again for later batches too, and certainly when a batch differs in size
   or kind from the one you swept on or a round disappoints: what suited the first batch need not suit the
   tenth.{sweep_cost_note}
2. Work in batches large enough to read. A difference between two versions comes with a give or take of about
   7 to 9 problems of 2,000 at 4 answers a problem (5 to 6 at 16). One or two corrections move the score by
   less than that: a round of them tells you nothing, and still moves the student. Eight or more corrections
   is a round that can be read.
>>
<<only time
1. Train with the default settings unless you have a reason not to: they are the ones in the train action above
   (adam, lr 2e-7, "steps": "auto", "until": 0.9, hold "end"). On record: the same four corrections trained
   three ways scored +14, +81 and +106 problems of 2,000 on validation; and in another session six corrections
   trained at lr 1e-7, 2e-7 and 4e-7 (stopped at 90%) and at 2e-7 (stopped at 99%) scored +50, +42, +58 and +64
   of 2,000, each give or take 19. A sweep ("runs") takes the time of every run in it and of every score you
   ask of them. It is for a batch you have reason to think needs other settings, or for a round that went
   clearly wrong; it is not a step every batch needs.
2. Work in batches large enough to read. A difference between two versions comes with a give or take of about
   {loop_ga4} problems of {val} at 4 answers a problem ({loop_ga16} at 16, {loop_ga1} at 1). One or two corrections move the score by
   less than that: a round of them tells you nothing, and still moves the student. Eight or more corrections
   is a round that can be read.
>>
<<only units
3. Make each correction carry its reasoning. After a fork that leads to the right answer, the result lists
   the tokens of the continuation that the student gave little probability: the places where it could as
   easily have gone wrong again. Weight up the ones that carry the reasoning, so that the path is trained and
   not only its first token.
>>
<<only time
3. Check each correction before you keep it. One continuation that reaches the right answer is not proof
   that the fork works: continue the same fork several times ("tries") and keep it when the student gets
   there most of the time.
>>
4. Train the batch stacked on the version you are building ("start": "current", "data": "new").
<<only units
5. Read what the run did: to your tokens, to the weights, on the pool and on validation, each against its give
   or take. When two versions are close, ask for more answers per problem (up to {kmax}) before you choose
   between them: the best of several close readings is partly luck.
>>
<<only time
5. Read what the run did to your tokens, to the weights and on the pool: you are told all of that with every
   run. A validation score is a separate cost, and a round does not need one to be built on. Ask for one when
   its answer would change what you do next: one or two answers a problem to catch a round that did damage,
   more (up to {kmax}) to choose between close versions, and for the version you mean to hand in: the best of
   several close readings is partly luck.
>>
6. A batch that lowers the score is taken out, not built on: "use" the version before it and "exclude" the
   batch, or the corrections in it you now doubt. On record, the batches that did damage forced tokens the
   student gave a few percent, or were trained for many more steps than their tokens needed.{pose_step}
{repeat_n}. Repeat.

MEASUREMENT AND ITS NOISE
{sampled_note}
- A validation score: every validation problem is answered k times at temperature {sample_t} (the most likely
  95% of the probability at each token); the score is the sum over problems of the share of its k answers
  that are right. Answer number j of a problem is drawn with the same random seed for every version.
- "Give or take" is always one standard error of a DIFFERENCE between two versions, computed from the
  per-problem differences on the same problems and seeds (the standard deviation of those differences times
  the square root of the number of problems). If the two versions were truly equal, the difference you read
  would lie beyond one give-or-take about one time in three, beyond two about one time in twenty, beyond
  three about one time in four hundred. The best of several such readings overstates the version it belongs
  to: the highest of ten scores of truly equal versions sits about one give-or-take above their level.
{noise_record}
- The test, taken once on the version you hand in: {test_n} problems you never see, {test_k} sampled answers each
  ({test_sampling}), reported as the change in expected problems solved
  against the untrained student.{test_record}
- The pool is read the same way as validation, with the answers per problem it has; its problems are the
  ones your corrections come from, so it is not held out.{v0_note}{floor_note}
- Answers are read by what they say, not by their format: an answer that is not boxed is completed with
  "the final answer is", and anything the student writes after its own turn is ignored: an answer is
  never marked wrong for its format.{labels_note}
