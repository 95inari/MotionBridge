# Blender Unity Motion Bridge

BlenderでHumanoidモーションを非破壊で修正し、同じPC上のUnity Humanoidへリアルタイムにプレビューするツールです。

## できること

- FBX / glTF / GLBのモーション取込みとCanonicalリグへの変換。
- 元モーションを保持した修正レイヤー、修正前後の比較、範囲へなじませる補正カーブ。
- 揺れの平滑化、関節の曲がりすぎ補正、接地中の足滑り補正。
- UnityのEdit / Play Modeへのライブプレビューと修正済みFBXの出力。

## 動作環境と制限

動作確認記録はmacOS Apple Silicon、Blender 5.0.1、Unity 6000.2.15f1です。Blender 4.5 LTSは互換対象ですが、この版での再検証は未完了です。Windows/Linuxも未検証です。

Humanoidを対象とし、利用権のあるモデル・モーションを別途用意します。第三者キャラクターや既成モーションは同梱していません。ワンクリック補正は1回最大600フレームです。Foot LockはCanonicalの左右脚を対象とし、Root/Hips移動やつま先のロール、手指のIKには対応しません。ライブ接続は同じPC内に限ります。

## 導入

Python 3.11以降で、リポジトリ直下から実行します。

```sh
python3 tools/build_distribution.py
```

`dist/MotionBridge-0.1.13-GitHub.zip` にBlender拡張、Unityパッケージ、Canonicalリグ、導入ガイドと利用条件が入ります。展開後は [はじめてガイド](QUICKSTART.md) の手順で導入できます。

検証プロジェクトを使う場合は `verification/unity-6000.2/` をUnityで開きます。Motion Bridgeはembedded packageとして含まれます。Package ManagerのSample ImportでCanonical Source Avatarを取り込み、利用権のあるHumanoidキャラクターを自分で配置してください。

## 開発・検証

```sh
python3 tools/run_tests.py
```

Blender回帰は `--include-blender`、Unityとのプロセス間テストは `--include-cross-runtime` を追加します。Blenderが標準配置と異なる場合は `--blender-executable` を指定します。UnityのEditor/Runtimeテストは検証プロジェクトのTest Runnerから実行できます。

| パス | 内容 |
|---|---|
| `blender_addon/` | Blender拡張、テスト、Canonical生成ツール |
| `verification/unity-6000.2/Packages/com.kuko.motionbridge/` | Unity UPMパッケージとテスト |
| `protocol/` | 共通schema、golden data、通信テスト |
| `samples/canonical/` | 自作Canonicalリグの正本 |
| `tools/` | 検証runnerと配布ビルダー |
| `docs/` | 設計・プロトコル・検証記録 |

[設計仕様](docs/01_DESIGN_SPEC.md) / [通信仕様](docs/02_PROTOCOL_SPEC.md) / [実装計画](docs/03_IMPLEMENTATION_PLAN.md) / [設計判断](docs/04_ADR.md) / [過去の検証記録](docs/05_VALIDATION_REPORT.md) / [モーション補正の計画](docs/06_MOTION_CORRECTION_IMPLEMENTATION_PLAN.md) / [更新履歴](CHANGELOG.md)

## 利用条件

[ソース公開版利用条件](LICENSE.md)を確認してください。利用・改変、成果物の配布、出典付きの引用・抜粋を認め、ツール・リグ等の再配布を制限します。再配布制限があるため、リポジトリ全体をOSSとして案内しません。

Blender側の既存MIT/GPLによる権利は維持し、GitHub規約が認める同サービス内での閲覧・fork等を妨げません。[適用範囲](licenses/README_JA.md)も参照してください。

不具合は [GitHub Issues](https://github.com/95inari/MotionBridge/issues) へ、OS、Blender/Unityのバージョン、再現手順、エラーを添えて報告してください。
