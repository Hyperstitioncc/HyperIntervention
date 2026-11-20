# GPT-6.2 Astra: a reply that does not end

**What happens.** In a long tool loop the model writes its actions correctly, and then, where the reply should end, keeps
generating: first a few clipped fragments, then thousands of lines that each hold a single dot, until the reply reaches
the output limit.

**Where.** `openai/gpt-6.2-astra` through OpenRouter, plain chat completions (no tools declared), temperature 0.3,
`max_tokens` 32000, `reasoning: {"effort": "high"}`. Conversations of 150,000 to 250,000 prompt tokens. The reply
format the model is asked for is prose followed by fenced JSON blocks, one per action.

**How often.**

| | Occurrences |
|---|---|
| One three-hour session (`astra-a`), served by Amazon Bedrock | 10 of 62 replies, 287,000 output tokens in all, 95% of the session's output |
| The two other sessions of the same model | no runs of dots; 34 and 21 replies end with a few stray fragments after blank lines |
| Re-sending three saved requests, served by Azure | **4 of 12**: 12,275 to 19,624 dot lines each, `finish_reason: length` at 32,000 tokens, about four minutes |

It occurred on two hosts, so it follows the model and not the provider. A normal reply to the same request takes 5 to
11 seconds and 60 to 530 output tokens.

**What it looks like.** From a replay (`replays/astra-a-turn333--as-sent--1.json`), directly after the last action block:

```
 small.
  end.
  .
  .
  .
```

and so on for 12,275 lines. In the original session the text before the dots read like private notes becoming visible,
and the model remarking on it:

> Estimate after 40 min remain 2h20. Ideal ~10 further runs at 10min each. […] This token in system displayed? Need
> stop. Done. Wait. Good. Fine. This shouldn't be transmitted. […] Stop. EOF. done. I don't need write anymor

Two of the original replies also contain lines that imitate system notices: "20842 tokens omitted." and "Warning: hidden
scratchpad was compressed. Resume from tool response."

**Effect.** The actions at the top of such a reply are valid and were carried out. Each occurrence costs about four
minutes and up to 32,000 output tokens.

**Files.** `occurrences/astra-a-turn*.json`: the ten original replies as received, with time, provider and token counts.
`requests/astra-a-turn{300,305,333}.json`: the conversations re-sent. `replays/astra-*.json`: every replay's full
response. `replays.csv`: one row per replay with its OpenRouter generation ID.
