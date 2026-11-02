> 利用条件は同梱の `Licenses/` を参照してください。

# Motion Bridge 0.1.13

BlenderでHumanoidモーションを修正し、同じPCのUnityでリアルタイムに確認するツールです。

最初に `QUICKSTART.md` を開いてください。Blender拡張、Unityパッケージ、編集用リグがこのZIPに揃っています。

## 同梱ファイル

| ファイル | 用途 |
|---|---|
| `Blender/motion_bridge-0.1.13.zip` | Blenderへインストールする拡張 |
| `Unity/com.kuko.motionbridge-0.1.1.tgz` | Unity Package Managerへインストールするパッケージ。Canonical Source Avatarサンプルを含む |
| `Samples/mb_canonical_humanoid_v1.blend` | 編集用Canonicalリグ。別名保存して使用 |
| `QUICKSTART.md` | 導入・編集・書き出し・トラブル対処 |
| `CHANGELOG.md` | 更新履歴 |
| `compatibility_manifest.json` | 拡張、Unityパッケージ、データ形式のバージョン情報 |
| `Licenses/` | 同梱物のライセンス文書 |
| `SHA256SUMS.txt` | ファイル破損確認用のSHA-256一覧 |

Blender拡張とUnityパッケージは別々のバージョン番号を持ちます。番号が異なっていても、この配布に同梱した組み合わせで使用できます。

## 必要な環境と素材

今回の配布版の動作確認環境は、macOS（Apple Silicon）、Blender 5.0.1、Unity 6000.2.15f1です。Blender 4.5 LTSは互換対象ですが、今回の配布版では再検証していません。Windows/Linuxの動作確認も未完了です。BlenderとUnityは同じPCで動かします。

利用権のあるHumanoidモーション（FBX / glTF / GLB）と、Unity Humanoidとして設定されたキャラクターを別途用意してください。第三者のキャラクター、既成のモーション素材、Blender本体、Unity本体は含みません。

## 主な機能

- 元モーションを残した修正レイヤー、修正前後の比較。
- ボーンの回転修正とHipsの位置修正、範囲へなじませる補正カーブ。
- 揺れの平滑化、関節可動域の補正、接地中の足滑り補正。
- Unity Humanoidへのライブプレビューと、修正済みモーションのFBX出力。

## 現時点の制限

- Humanoidを対象とします。Generic Rigや任意のコントロールリグの直接編集、手・指のIK、フェイシャル・BlendShape編集は対象外です。
- 自動ボーン認識に合わないリグは手動で対応付けが必要です。入力ファイルに複数Armatureがある場合は読み込めません。
- ワンクリック補正は1回最大600フレームです。長いモーションはPreview Rangeで分割します。
- Foot LockはCanonicalの左右の脚を対象にした位置補正です。Root/Hips移動やつま先のロールは補正しません。
- ライブ接続は同じPC内のBlenderとUnity Editor間のみです。別PC接続とビルド済みアプリへの配信は対象外です。
- 書き出すFBXはモーション用です。キャラクターメッシュやマテリアルは含みません。

## ライセンス・問い合わせ

各同梱物には `Licenses/` の条件が適用されます。使用するキャラクターやモーション素材には、それぞれの提供元の条件が適用されます。

問い合わせには、製品・Blender・Unityのバージョン、OS、再現手順、エラー全文を記載してください。[GitHub Issues](https://github.com/95inari/MotionBridge/issues)へ報告してください。
