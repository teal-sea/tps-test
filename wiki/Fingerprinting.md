# Fingerprinting

Same weights, or just the same name?

A model slug is not an artifact. Behind one name you can find fp4, fp8, mxfp4, a
dozen endpoints that decline to say — and in principle distilled siblings and stale
revisions. Speed and price cannot tell them apart.

```bash
tps fingerprint -c targets.toml
```

## How it works

1. Each target gets the same prompt **twice**, `temperature = 0`, non-streaming,
   with `logprobs`.
2. Each target's two runs are compared with each other — its **noise floor**.
   Batching and kernel nondeterminism mean nothing is bit-exact across calls.
3. Targets are compared with each other on run 1's token sequence.
4. Greedy decoding is nearly a function of the weights: identical weights and
   kernels reproduce tokens; fp4 vs fp8 diverges within dozens of tokens; a
   distilled model diverges almost immediately.
5. Cheap independent check: `usage.prompt_tokens` for the same input string —
   same tokenizer and template ⇒ same count.

## Reading it

| Observation | Reading |
|---|---|
| `self_agreement` < 98% | endpoint is not deterministic; its cross-provider numbers mean nothing |
| pair ≥ 99% | same weights and kernels — or indistinguishable at this length |
| 90–99%, divergence deep | same base, different quantization or kernels |
| < 90%, divergence early | **different artifact** — distilled, old revision, another model |
| `prompt_tokens` differ | different tokenizer or chat template |

Clusters union targets whose agreement clears both noise floors
(`max(0.98, min_floor − 0.01)`).

## Cost

11 targets × 2 prompts × 2 reps × 160 tokens ≈ 7k tokens, about a minute, a few
cents. Run it before a pricing decision ships.

## Limits

- **Can fail to disprove identity. Cannot prove it.** A distilled model can survive
  150 tokens of prose.
- Providers without `logprobs` fall back to whole-text equality — only gross
  differences (mode `text` in `fingerprints.jsonl`).
- Same hardware, same weights can still diverge from batch-dependent numerics.
  That is why the noise floor exists.
- Reasoning models may emit hidden tokens that shift the visible sequence.
- **Quantization is not fraud.** fp4 at half price may be exactly right. The point
  is knowing which is which, then evaluating quality on your task.

## The cheap smell test

Before fingerprinting, read the `discover` table: a price far below the median for
the same model is usually fewer bits or a smaller model; an endpoint with no
quantization listed is unknown, not fp8; a 3× speed gap between "identical"
endpoints is a weights signal, not a serving optimization.
