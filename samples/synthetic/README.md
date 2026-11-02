# Synthetic Phase 0 targets

Phase 0 の Canonical generator は、外部素材に依存しない Humanoid skeleton/Avatar を三体生成する。

| ID | 目的 | 初期比率目標 |
|---|---|---|
| `short-leg` | 短脚・長胴 | 脚 0.75、腕 0.90、胴 1.15 |
| `canonical` | 基準 | 全て 1.00 |
| `long-leg` | 長脚・短胴 | 脚 1.30、腕 1.15、胴 0.85 |

再現可能なgenerator、seed、利用条件と未生成targetの扱いは`corpus_manifest.json`へ記録する。上表の3体は現時点では仕様だけで、生成物とsemantic hashが記録されるまでrelease gateには使わない。生成後、各比較軸に15%以上の差がないtargetは不合格にする。
