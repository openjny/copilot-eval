# 機能仕様の索引

2026-09-12 に既存ユースケース・要件を移行。すべて Draft であり、分割は実装順序・設計境界・受入れ完了を確定しない。現在の Spec Kit の作業対象は 001 のまま。

| Feature | 対象UC | 段階 | 関連先 |
| --- | --- | --- | --- |
| [001 ローカル比較](001-local-comparison/spec.md) | UC-001〜007、UC-009 | 第一段階 | 002と共同で初期化・記録・評価・後片付けを設計 |
| [002 実行・環境管理](002-execution-management/spec.md) | UC-008、UC-001のサービス・モック | 第一段階 | 001の記録を使い、003・004から制御される |
| [003 リモート実行](003-remote-execution/spec.md) | UC-010 | 後続 | 001・002を実行先へ接続 |
| [004 ダッシュボード・CI](004-dashboard-and-ci/spec.md) | UC-011・012 | 後続／CIは候補 | 001の比較と002の制御を利用 |

共通の意味は [ドメインモデル](../docs/domain-model.md)、横断制約は [非機能要件](../docs/nonfunctional-requirements.md)、実現方式は [全体設計](../docs/architecture.md)を読む。標準成果物は各工程で必要になったときに作成し、空の plan・tasks を先に増やさない。

## 既存要件IDの移行先

IDを改番せず、001のFRとの対応または所有specへの参照を残す。この表は索引であり、要求本文の別台帳ではない。REQ-001・002・006は方針から具体化した草案、REQ-003・007は最新の再実行方針を反映、REQ-005等の確認済み表示は要求の確認であって実装検証を意味しない。

| 旧ID | 正本と対応する要求 |
| --- | --- |
| REQ-001 | [001](001-local-comparison/spec.md): FR-001 |
| REQ-002 | [001](001-local-comparison/spec.md): FR-007・012〜014 |
| REQ-003 | [001](001-local-comparison/spec.md): FR-009、Scope の評価やり直し |
| REQ-004 | [001](001-local-comparison/spec.md): FR-011、Story 3、評価履歴は共通モデル |
| REQ-005 | [001](001-local-comparison/spec.md): FR-004、Story 1 |
| REQ-006 | [001](001-local-comparison/spec.md): FR-005 |
| REQ-007 | [001](001-local-comparison/spec.md): FR-009、Scope の評価やり直し |
| REQ-008 | [002](002-execution-management/spec.md): 同ID |
| REQ-009 | [001](001-local-comparison/spec.md): FR-011、Story 3 |
| REQ-010 | [001](001-local-comparison/spec.md): FR-006 |
| REQ-011 | [001](001-local-comparison/spec.md): FR-008・014。取得範囲は要検証 |
| REQ-012 | [001](001-local-comparison/spec.md): FR-010と標準基準の補足 |
| REQ-013 | [001](001-local-comparison/spec.md): FR-005 |
| REQ-014 | [001](001-local-comparison/spec.md): FR-004、Story 1 |
| REQ-015 | [001](001-local-comparison/spec.md): FR-016 |
| REQ-016 | [001](001-local-comparison/spec.md): FR-003・007・008 |
| REQ-017 | [002](002-execution-management/spec.md): 同ID |
| REQ-018 | [001](001-local-comparison/spec.md): FR-015 |
| REQ-019 | [002](002-execution-management/spec.md): 同ID |
| REQ-020 | [002](002-execution-management/spec.md): 同ID |
| REQ-021 | [002](002-execution-management/spec.md): 同ID |

## 本文未定義だったID

旧要件の UC 表には REQ-022〜033 の参照があったが、要求本文は存在しなかった。移行によって承認済みの要件を作らず、以下を TBD として残す。具体化時には既存FRとの重複を調べる。

| ID | 旧参照元 | 確認先 |
| --- | --- | --- |
| REQ-022・029・033 | UC-005 | 001。定義と既存FRへの対応は TBD |
| REQ-023・024 | UC-006・011・012 | 001と004。定義は TBD |
| REQ-025 | UC-007・011 | 001と004。定義は TBD |
| REQ-026・027 | UC-009・010 | NFR-001・006、001と003。定義は TBD |
| REQ-028 | UC-008・010・011 | 002〜004。定義は TBD |
| REQ-030 | UC-010 | 003。定義は TBD |
| REQ-031 | UC-011 | 004。定義は TBD |
| REQ-032 | UC-012 | 004。定義は TBD |

## 作業状態

- [001の品質チェックリスト](001-local-comparison/checklists/requirements.md)は移行後に再確認する。要求と設計の承認は TBD。
- 002〜004は既存要求の保存先であり、Spec Kit の全工程を実行済みとはしない。チェックリスト・plan・tasks は今後の対象工程で作成する。
