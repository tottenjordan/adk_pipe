# Person swap: edit vs regenerate

Fidelity = layout, text, graphics and product all unchanged (vision comparison with the original). Likeness = the image check's same-person verdict.

| Mode | Renders | Errors | Fidelity | Only person changed | Likeness |
|---|---|---|---|---|---|
| regenerate | 12 | 0 | 0% | 0% | 100% |
| edit | 12 | 0 | 75% | 58% | 83% |

| Mode / photo | Renders | Errors | Fidelity | Only person changed | Likeness |
|---|---|---|---|---|---|
| regenerate / photo1 | 6 | 0 | 0% | 0% | 100% |
| regenerate / photo2 | 6 | 0 | 0% | 0% | 100% |
| edit / photo1 | 6 | 0 | 67% | 33% | 100% |
| edit / photo2 | 6 | 0 | 83% | 83% | 67% |

Rule: go when edit fidelity >= regenerate + 30% and edit likeness >= 80%.

**Decision: go**
