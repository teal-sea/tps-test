> **MOCK** — synthetic data for pipeline testing, not measurements.

# Fingerprint: same weights or just the same name?

Greedy decode (temp 0). `self` = the provider's agreement with itself across reps — its noise floor. `vs` agreement below the floor is not evidence of different weights.

## Tokenizer & chat-template signature

| provider | prompt_tokens (same input) | completion_tokens | logprobs |
|---|---|---|---|
| open-inference (fp4) | 2 | 150 | tokens |
| decart (fp4) | 2 | 150 | tokens |
| deepinfra flex (fp8) | 2 | 150 | tokens |
| deepinfra (fp8) | 2 | 150 | tokens |
| morph (fp8) | 2 | 150 | tokens |
| nebius (fp8) | 2 | 150 | tokens |
| fireworks | 2 | 150 | tokens |
| together | 2 | 150 | tokens |
| google-vertex | 2 | 150 | tokens |
| baseten priority | 2 | 150 | tokens |
| openrouter auto | 2 | 150 | tokens |

All providers tokenized the probe identically — consistent with one tokenizer/template, which is necessary but not sufficient.

## Self-agreement (noise floor)

| provider | token agreement rep0 vs rep1 | first divergence |
|---|---|---|
| open-inference (fp4) | 100.0% | — |
| decart (fp4) | 100.0% | — |
| deepinfra flex (fp8) | 100.0% | — |
| deepinfra (fp8) | 100.0% | — |
| morph (fp8) | 100.0% | — |
| nebius (fp8) | 100.0% | — |
| fireworks | 100.0% | — |
| together | 100.0% | — |
| google-vertex | 100.0% | — |
| baseten priority | 100.0% | — |
| openrouter auto | 100.0% | — |

Every provider reproduced itself near-exactly at temp 0.

## Pairwise agreement — `prose-256`

| | `open-inference` | `decart (fp4)` | `deepinfra flex` | `deepinfra (fp8` | `morph (fp8)` | `nebius (fp8)` | `fireworks` | `together` | `google-vertex` | `baseten priori` | `openrouter aut` |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **open-inference (fp4)** | **100** | 0 | 100 | 100 | 0 | 0 | 100 | 100 | 100 | 0 | 0 |
| **decart (fp4)** | 0 | **100** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 100 | 0 |
| **deepinfra flex (fp8)** | 100 | 0 | **100** | 100 | 0 | 0 | 100 | 100 | 100 | 0 | 0 |
| **deepinfra (fp8)** | 100 | 0 | 100 | **100** | 0 | 0 | 100 | 100 | 100 | 0 | 0 |
| **morph (fp8)** | 0 | 0 | 0 | 0 | **100** | 100 | 0 | 0 | 0 | 0 | 100 |
| **nebius (fp8)** | 0 | 0 | 0 | 0 | 100 | **100** | 0 | 0 | 0 | 0 | 100 |
| **fireworks** | 100 | 0 | 100 | 100 | 0 | 0 | **100** | 100 | 100 | 0 | 0 |
| **together** | 100 | 0 | 100 | 100 | 0 | 0 | 100 | **100** | 100 | 0 | 0 |
| **google-vertex** | 100 | 0 | 100 | 100 | 0 | 0 | 100 | 100 | **100** | 0 | 0 |
| **baseten priority** | 0 | 100 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | **100** | 0 |
| **openrouter auto** | 0 | 0 | 0 | 0 | 100 | 100 | 0 | 0 | 0 | 0 | **100** |

Diagonal = self-agreement. Values are % of aligned tokens identical.

## Clusters

- **group 1** (6): open-inference (fp4), deepinfra flex (fp8), deepinfra (fp8), fireworks, together, google-vertex
- **group 2** (3): morph (fp8), nebius (fp8), openrouter auto
- **group 3** (2): decart (fp4), baseten priority

3 groups. Members of different groups are *not* the same artifact: expect different quantization, revision, or model. Confirm with a task eval before assuming the cheaper one is equivalent.

> Limits: a provider without logprobs support is compared on whole-text equality only (coarse). Two runs never prove identical weights — they only fail to disprove it.
