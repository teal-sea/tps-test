> **MOCK** — synthetic data for pipeline testing, not measurements.

# Fingerprint: same weights or just the same name?

Greedy decode (temp 0). `self` = the provider's agreement with itself across reps — its noise floor. Agreement below that floor is not evidence of different weights.

## Tokenizer & chat-template signature

| provider | prompt_tokens (same input) | completion_tokens | logprobs |
|---|---|---|---|
| acme-ai (fp8) | 60 | 150 | tokens |
| globex (fp8) | 60 | 150 | tokens |
| hooli (fp8) | 60 | 150 | tokens |
| initech (fp4) | 60 | 150 | tokens |
| vandelay (fp4) | 60 | 150 | tokens |
| wonka (unlisted) | 69 | 150 | tokens |
| gateway-auto | 60 | 150 | tokens |

⚠ 2 distinct prompt-token counts for identical input — different tokenizer or chat template: 60 tokens → acme-ai (fp8), gateway-auto, globex (fp8), hooli (fp8), initech (fp4), vandelay (fp4); 69 tokens → wonka (unlisted).

## Self-agreement (noise floor)

| provider | token agreement rep0 vs rep1 | first divergence |
|---|---|---|
| acme-ai (fp8) | 100.0% | — |
| globex (fp8) | 100.0% | — |
| hooli (fp8) | 100.0% | — |
| initech (fp4) | 100.0% | — |
| vandelay (fp4) | 100.0% | — |
| wonka (unlisted) | 100.0% | — |
| gateway-auto | 46.7% | 70 |

⚠ Non-deterministic at temp 0: gateway-auto. Their cross-provider agreement can't be interpreted — the endpoint itself varies run to run — so they never decide the verdict.

## Pairwise agreement — `prose-256`

| | `acme-ai (fp8)` | `globex (fp8)` | `hooli (fp8)` | `initech (fp4)` | `vandelay (fp4)` | `wonka (unliste` | `gateway-auto` |
|---|---|---|---|---|---|---|---|
| **acme-ai (fp8)** | **100** | 100 | 100 | 64 | 64 | 9 | 100 |
| **globex (fp8)** | 100 | **100** | 100 | 64 | 64 | 9 | 100 |
| **hooli (fp8)** | 100 | 100 | **100** | 64 | 64 | 9 | 100 |
| **initech (fp4)** | 64 | 64 | 64 | **100** | 100 | 9 | 64 |
| **vandelay (fp4)** | 64 | 64 | 64 | 100 | **100** | 9 | 64 |
| **wonka (unlisted)** | 9 | 9 | 9 | 9 | 9 | **100** | 9 |
| **gateway-auto** | 100 | 100 | 100 | 64 | 64 | 9 | **47** |

Diagonal = self-agreement. Values are % of token positions identical.

## Clusters

- **group 1** (4): acme-ai (fp8), globex (fp8), hooli (fp8), gateway-auto
- **group 2** (2): initech (fp4), vandelay (fp4)
- **group 3** (1): wonka (unlisted)

**Verdict: split.** 3 distinguishable groups. Members of different groups are *not* serving the same artifact: expect different quantization, revision, or model. Confirm with a task eval before assuming the cheaper one is equivalent.

> Limits: a provider without logprobs support is compared on whole-text equality only (coarse). Two runs never prove identical weights — they only fail to disprove it.
