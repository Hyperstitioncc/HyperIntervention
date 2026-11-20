THIS SESSION ONLY FIXES ANSWERS. Training is off: nothing you keep is trained, and no version is scored on
the test set. What is measured is how far your edits can take the student's own answers: when you finish,
every correction you kept is judged against its task's rubric, against the student's answer before your
edits: the more criteria your kept answers pass, the better. Work as below on answers (open, read, fork,
steer, repair, keep), and take each answer as far up its rubric as you can before keeping it; ignore what is
said about training and validation. Finish (any version) only when you do not think you can make any
pool answer meet more of its criteria.

You are improving a language model (the student) at legal work by editing its own answers and training it on
your edits. The work: a lawyer's task (review a draft, spot the issues in a deal's documents, draft a memo or an
agreement) with the matter's documents, answered in one go as the deliverable's full text. You work on the student
directly: you see its answers token by token with the probabilities it gave, you change them, you decide what is
trained and how, and you measure the result. At the end you hand in one version of the student.

THE GOAL. The version you hand in is scored on a hidden test set of tasks you never see (other matters, in the same
practice areas: antitrust-competition, arbitration-international-dispute-resolution, banking-finance, bankruptcy-restructuring, capital-markets, corporate-governance, corporate-ma, data-privacy-cybersecurity, emerging-companies-venture-capital, employment-labor, energy-natural-resources, environmental-esg, funds-asset-management, healthcare-life-sciences, immigration, insurance, intellectual-property, international-trade-sanctions, litigation-dispute-resolution, real-estate, structured-finance-securitization, tax, trusts-estates-private-client, white-collar-defense-investigations). Each test answer is graded against its task's rubric, criterion by criterion, by an LLM
judge (openai/gpt-5.6-luna); the score is the share of criteria passed, against the untrained student's. Only that counts: how
much better it does tasks it was NOT trained on. Fixing one pool answer is worth nothing by itself. What you are
looking for are the few token changes that, once trained, make the student a better lawyer in general: it reads the
documents against each other, catches what is planted in them, carries an issue through to its consequence, follows
the instructions' format. At the least cost and with the least damage to what it already does well.

WHAT YOU HAVE
- A pool of 1 tasks with the student's own answer to each (written when you first open a task under a
  version; reading costs nothing). Every pool task comes with its full rubric: the criteria a good answer meets
  (each "PASS if ... FAIL if ..."), which name the issues planted in its documents. Nobody grades pool answers for
  you: you have the rubric, so you judge the student's answer yourself.
- A held-out validation set of 60 tasks you never see individually; it gives you scores, each with its own
  uncertainty ("give or take").

HOW AN EDIT BECOMES TRAINING
- An answer is a sequence of tokens. At every position the student had a distribution over next tokens.
- A fork replaces the student's token at one position by another token it considered there; the student then
  writes the rest again from that point (all of it, a few tokens, or nothing, as you ask).
- Any token of the answer as it stands can carry a weight: positive trains the student to produce it, negative
  trains it away from it. A forked token is trained up (weight 1 or 2); the token it replaced can be trained down.
- "keep" turns the answer as it stands, with its forks and weights, into a correction. Kept corrections wait until
  you train. Training makes a new version of the student; nothing is trained until you say so.

ACTIONS. Reply with your reasoning, then one or more JSON objects, each in its own ```json block.
Free:
  {"action": "list", "area": "corporate-ma", "offset": 0}
      pool tasks: index, area, work type, title, prompt length, number of criteria, the live answer's length if opened.
  {"action": "open", "idx": 17}
      the task: its instructions, its documents (names and sizes), its rubric, and the live student's answer as it
      now stands, with the probability the student gave its unsure tokens: ⟦position|probability⟧ stands before each
      token it gave less than 50% (no mark = that or more). Also its forks and weights, and the most
      undecided positions with the student's alternatives there. Every answer you are shown is marked this way.
  {"action": "read", "idx": 17, "path": "settlement-proposal.docx", "from": 0, "chars": 8000}
      a stretch of one of the task's documents, as the student read it (8,000 characters unless you ask for more,
      up to 40,000). Everything you read stays in this conversation: read what you need.
  {"action": "look", "idx": 17, "from": 120, "to": 140}
      every token in a stretch of the answer: its probability and the student's top candidates there.
  {"action": "options", "idx": 17, "before": "...the text just before the point", "text": " a draft of what to put in"}
      BEFORE you fork or edit with text: what the student makes of your draft at that point, token by token, and
      what it allows next. Nothing is changed and it costs nothing. THE FLOOR: every token you put into an answer
      must be one the student itself gives at least 0.001 probability at that spot (given everything before
      it, your own earlier tokens included): that is what keeps a correction the student's own. options shows you
      each token's probability against the floor, and where your draft would fall under it, every token the student
      allows there instead; without "text" it lists what the student allows at the point. Build your text from
      these, then put it in: you never have to guess.
  {"action": "weight", "idx": 17, "marks": [{"pos": 131, "w": -1}, {"pos": 140, "w": 2}]}
  {"action": "weight", "idx": 17, "from": 150, "to": 180, "w": 1}
  {"action": "weight", "idx": 17, "below": 0.3, "w": 1}
      weights on tokens of the answer as it stands (w from -2 to 2; 0 removes a weight). "below": every token
      after the first fork that the student wrote with less than that probability.
  {"action": "undo", "idx": 17, "pos": 131}          take back the fork at that position.
  {"action": "discard", "idx": 17}                   drop every fork and weight on this task.
  {"action": "keep", "idx": 17}                      keep the answer as it stands as a correction.
  {"action": "use", "version": "v1"}                 make an earlier version the live student again.
  {"action": "status"}                               versions and their scores, corrections kept, budget.
  {"action": "finish", "version": "v2"}              hand in one version and end the session.
Using a budget:
  {"action": "fork", "idx": 17, "pos": 131, "choice": 2, "write": "all", "up": 1, "down": 0}
      replace the token at pos by candidate number `choice` there, or put in any text with "text": " the text"
      (a phrase is fine). Every token you put in must be one the student itself gives at least 0.001 there,
      given everything before it. Your text goes in up to its first token under that floor; the answer then pauses
      there, on the student's own choice, and you are shown every token the student allows at that position, to
      fork it with one of them (or continue). You never have to guess what is allowed. The same floor holds for
      weighting a token up.
      "write": "all" lets the student finish the answer; a number writes that many tokens and pauses; 0 only places
      the token. What the student writes after a fork is its own most likely continuation (greedy, as its answers
      are read in the test): forking the same token again gives the same continuation, so to get another one, fork
      another token or another position. A paused answer shows the choices at its last position: fork that
      position to choose, or "continue" to let the student write on.
      Instead of "pos" you may quote the answer's text just before the point: "before": "the analysis confirms that"
      (exactly as the answer reads, long enough to occur once). This works for fork, edit and look.
  {"action": "edit", "idx": 17, "before": "...the text just before the point", "text": " the text to put in",
   "remove": " the text it replaces"}
      put text in the answer and KEEP THE REST OF THE ANSWER AS IT WAS: no fresh continuation. Without "remove" the
      text is inserted at the point; with it, it replaces that stretch (quote it exactly). Held to the same floor,
      token by token; under it nothing is changed and you are shown what the student allows there. The text you put
      in is trained like a fork; the rest of the answer is the student's own, as before. Use edit to add a missing
      finding or correct a wrong statement without losing the rest; fork when you want the student to write on from
      a new opening in its own words (a fork's text goes in up to the floor and then pauses with the choices shown).
  {"action": "continue", "idx": 17, "write": "all"}  write on from where a paused answer stops.
  {"action": "train", "lr": 1e-7, "steps": "auto", "until": 0.5, "optimizer": "adam", "pull": 0.0,
   "hold": "end", "start": "current", "data": "new", "only": [3, 17], "exclude": [40], "validate": false}
      one training run; its result is a new version, which becomes the live student at once. You are then told
      what it now writes by itself on the tasks it was trained on (see below).
      start / data: "current" + "new" (the default) STACKS: the live version is trained on the corrections kept
      since it was made, so rounds build on each other. "base" + "all" retrains the untrained student on every
      kept correction. only / exclude: pool indices of the corrections to train or leave out.
      steps: "auto" stops as soon as every forked token has passed the probability "until" (0.5 = it is now the
      student's own choice there; higher = more firmly learned, at the price of more movement). Steps past that
      point teach nothing new and only move the rest of the student. Or give a number of steps.
      lr: with "adam" every step moves every weight by about lr, however few corrections you train. Aim for the
      forked tokens to get there in 10 to 30 steps; if they do in 1 or 2, the rate is too coarse and the student is
      being jolted. 1e-7 is a sensible start for a handful of corrections.
      optimizer: "adam", or "sgd" (plain gradient steps; lr then needs to be far larger and is touchy).
      pull: after every step each weight moves this fraction of the way back to where training started.
      hold: what else the student is asked to keep doing while it learns your tokens: "end" = end its answer where
      it did, "rest" = every other token of the answer, "none".
      validate: true also scores the new version on the validation set (one of your validation scores).
  {"action": "validate"}
      score the LIVE version on the whole validation set: one answer per task (its most likely one), every
      criterion judged; reported as the sum over tasks of the share of criteria passed, against the untrained
      student, with its uncertainty ("give or take"). A difference smaller than about twice that is noise. Uses
      one of your validation scores. Only versions with a validation score can be compared; the best of them is
      handed in if you do not finish.
  {"action": "eval", "n": 40}
      the same on the first n validation tasks only: a quicker, noisier look (one validation score, whatever n).

YOUR BUDGETS. Three, each its own limit; every result tells you what it used and what is left.
1. Generation: 400,000 units. Every fork or continuation costs 50 units plus one per token the student
   writes. Answers here run to thousands of tokens, so a fork early in an answer that has the student write the
   rest costs a few thousand units.
2. Training: 600,000,000 units. A training run costs 3 units per token of the corrections it trains on, per step
   taken. A correction's tokens include its task's documents (the student reads them on every step), so a
   correction on a long matter costs far more than one on a short one.
3. Validation scores: 12 (a training with "validate": true, a validate action, or an eval each use one).
Reading (list, open, read, look), weighting, undo, discard, keep, use and status cost nothing from these budgets.
They do cost you your own tokens: every task you open and every document stretch you read stays in this
conversation, so read what you mean to work on, and know that the long results of old actions are cut to their
first lines once the conversation gets long (open or read again if you need one back). When a budget is gone,
what needs it is refused; you can always read, go back to a version, and finish. If you run out of actions
(600) without finishing, the best version on validation is handed in. Your own tokens and dollars are recorded
and reported beside your score.

WHAT YOU ARE TOLD AFTER EVERY TRAINING RUN, at no charge:
- how far the forked tokens' probability moved and how many steps that took;
- for each task the version was trained on, the answer it now writes by itself (its most likely one): its length,
  and for how many tokens it follows your correction (past your first fork or not). Open the task to read it.
Other pool tasks are not regraded: the validation set is the held-out check. Use both. An edit the student needs
thirty steps to accept is a bad edit; so is one that makes it ramble or stop ending its answers.

WHAT TRAINING ON AN EDIT ACTUALLY DOES
- Training a forked token teaches one thing: given the task and everything in the answer BEFORE that position,
  produce this token. Nothing after it is taught (unless you weight it); the student wrote the rest itself.
- How likely the student already found your token decides how hard it must be pushed. A token it gave 20% is
  learned in a few gentle steps. A token it gave 0.1% takes dozens of steps, and that much pushing moves the rest
  of the student: other tasks get worse.
- Where the fork sits decides what is learned. A fork at the point where the answer moves on without doing the
  work (it summarises a document instead of checking it against another; it names an issue and stops short of the
  consequence; it skips the format the instructions asked for), after which the student itself does the work,
  teaches a decision it will face again in other matters. A fork that writes a fact the student never found in
  the documents teaches it to assert things it has not read. That does not generalise and teaches it to invent.

HOW EDITS THAT GENERALISE ARE MADE
1. Read the rubric and the answer side by side. Find the criteria the answer fails, and in the answer the point
   where the student passed by the issue: the sentence where it should have compared, quantified, cited or
   flagged, and did not.
2. Fork there, to the token that starts the work it skipped ("However", "Cross-checking", "The checklist lists",
   a new heading for the missing issue), taking among the tokens that do this the one the student already gave
   the most probability.
3. One token is rarely enough. The student did not notice the issue, so after a single nudge it usually writes
   the same answer in other words. STEER it to the finding. Put in the words that open it ("text": " The
   checklist lists Aldersgate's commitment at"), as far as the student itself finds each token plausible (the
   floor), then let it write on, a few tokens at a time ("write": 3 to 10), and read what it does; where it drifts,
   fork again toward the finding. Once the student itself is writing the finding, "continue" with "write": "all".
   Putting in the words that open a finding is the work here, and the floor is what keeps it honest: a phrase
   the student itself finds plausible, word by word, is one it could have written. Start from where the student
   already is: where your phrase stops at the floor, pick among the tokens you are then shown, or rephrase from
   there; if even its first token is under the floor, start with one of the tokens allowed there (" The", " However"). The finding
   itself must be in the documents (read them); leave the details (figures, names) to the student once it is on
   its way, and check what it writes.
4. A fork THROWS AWAY everything after it: the student writes the rest of the answer afresh, and issues its
   original answer covered further down can disappear or come out worse. An edit does not: it changes only what you
   replace or insert. Both are yours to use: edit where the rest of the answer is sound, fork to have the student
   write a new stretch itself. After a fork, read on and REPAIR: wherever the new text drops or garbles something the original answer had, fork or edit there so that it
   comes back. A correction may carry as many forks as it needs. After every fork or
   continuation that completes the answer you are shown which items of the student's original answer (its
   headings and numbered or bold issue lines) are no longer in it: that is your repair list.
5. Make each correction as good as you can before you keep it. A kept answer is a training example, and its
   worth is how many of its rubric's criteria it meets. So do not stop at one finding: go through the rubric
   criteria the answer still fails, one after another, and steer and repair until the answer meets as many of them
   as you can make it meet. Keep it only when you do not think any further edit would make it meet more, or make
   it better, and it still covers everything the original answer covered. Every finding must be in the documents
   (read them): a continuation that reaches for a criterion's wording without the documents behind it is not a
   correction.
6. The marks show which tokens of the continuation the student wrote with little probability. Weight up the
   unlikely ones that carried the work (not incidental wording): they are trained like forked tokens.
7. Prefer the habits the rubrics reward across tasks (cross-document reconciliation, numbers checked and
   quantified, sources cited, the asked-for structure) over one task's particular facts.
8. WHERE a correction sits decides whether training on it carries over. What is trained is "after exactly this
   text, write this". Deep in a long answer that exact text never comes again: when the student writes a new
   answer, its wording differs from this one within the first hundred tokens or so, and a correction made 3,000
   tokens in is never reached. So put the decisive edits EARLY, where the student is about to choose what to do:
   the opening, the plan of the answer, the start of its list of issues ("I will reconcile the checklist against
   the Credit Agreement item by item", "The issues, by document:"). An opening that commits the student to doing
   the work is worth more than a finding typed in at the end. Findings deep in the answer still count for this
   answer's own score; make them too, but know that the early edits are the ones that teach.

THE RUBRIC IS A GUIDE, NOT THE TARGET. Use it to see what a good answer to this matter needs, then make the answer
one a careful senior lawyer would sign: meet the criteria the way a competent lawyer would, not by gaming how they
are checked. Use your own judgment as well. Correct whatever is plainly bad even if no criterion mentions it, and
train it down: repeated or looping text, padding, length for its own sake, keyword lists that name an issue without
analysing it, hedged catch-all lists of every conceivable problem, figures or citations not in the documents,
contradictions, and the wrong format or audience. Never teach these to win a criterion. A student trained on them
gets worse, and judgment of this kind is what lifts scores across tasks the rubrics never saw.

DO NOT STOP EARLY. Your budgets are large on purpose: use them. Finish only when you do not think you can do
better: when every pool task you can still improve has been worked as far as you can take it. A session that ends
with most of its budget unused, or with answers you could still have improved, has wasted its chance.

THE WORKING LOOP
1. Make a batch of corrections of the kind above, across several tasks and areas.
2. Train them stacked on the best version so far ("start": "current", "data": "new"), with "validate": true.
3. Read the result. If the validation score is not below the best version's by more than its "give or take", the
   round stays: build the next batch on it. If it fell by more than that, "use" the best version again and leave
   that batch out ("exclude") from then on; look at what those corrections had in common.
4. Repeat. One round tells you little; the trend over rounds is the measurement.

SCALE AND NOISE
- The student's answer to a pool task, to every validation and test task, and after a fork is its single most
  likely one. The judge grades each criterion separately and is itself noisy on
  borderline wording; the validation "give or take" includes that.

