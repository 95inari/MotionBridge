# com.kuko.motionbridge

Blenderで編集したHumanoidモーションを、同じPC上のUnityのキャラクターへリアルタイム表示するUPM packageです。検証環境はUnity 6000.2.15f1です。Animation Rigging / RigBuilderの追加導入は不要です。

## インストール

1. 作業中のUnityプロジェクトをバックアップします。
2. `Window > Package Management > Package Manager`を開き、`+ > Install package from tarball…`から、配布ZIP内の`Unity/com.kuko.motionbridge-0.1.1.tgz`を選びます。tgz自体を展開する必要はありません。
3. Package Managerで`Motion Bridge`を選び、`Samples > Canonical Source Avatar > Import`を押します。
4. Projectの`Assets/Samples/Motion Bridge/0.1.1/Canonical Source Avatar`へFBXが追加されたことを確認します。

キャラクターモデルと元モーションは含みません。利用権のあるHumanoidモデルを別途用意してください。Blender拡張と編集用リグの導入は、配布ZIP直下の`QUICKSTART.md`を参照します。

開発用checkoutでは本packageは検証プロジェクトへ埋め込み済みです。既にembedded packageがあるプロジェクトにはtgzを重ねてインストールせず、新規プロジェクトで配布版を確認してください。

## 実装済み

- `Kuko.MotionBridge.Protocol`: PosePacket v1 encoder/decoder、session/sequence/joint mask gate、TCP length-prefixed JSON codec、共有 golden fixture。
- `Kuko.MotionBridge.Transport`: loopback-only TCP control server、background UDP receiver、reader-owned 3-slot latest-only buffer、受信/sequence gap/reject/overrun diagnostics、clock同期中のrolling 256-sample one-way age p50/p95、冪等 start/stop/dispose。
- `Kuko.MotionBridge.Retargeting`: Canonical Humanoid v1 の23 joint path/parent/rest定数と、unbound `HumanPoseHandler(avatar, paths)` を使う allocation-stable `CanonicalPoseConverter`。
- Editor: Canonical Source Avatar importer、`Window > Motion Bridge` listener、hello/descriptor/ready handshakeとUDP endpoint negotiation、named capability交渉、Edit Mode manual PlayableGraph preview、停止・例外時のAnimator/Transform復元、peer close・timeout後のlistener再生成。
- Runtime: scene上の`MotionBridgeRuntimePreview`、共有manual Humanoid PoseWriter、Play Mode中のTCP/UDP再待受、排他的Animator override、停止時のAnimator/全child TRS復元。

preview writer は `HumanPose.muscles` の `HumanTrait` 順序を `MuscleHandle` へ明示的に対応付ける。`MuscleHandle.GetMuscleHandles` の列挙順には依存せず、通常 muscle は名前をexact matchし、fingerだけ Unity 6000.2 の命名差（例: `Left Thumb 1 Stretched` → `LeftHand.Thumb.1 Stretched`）を `HumanTrait.BoneFromMuscle` で識別して正規化する。全 muscle の対応が一対一でない場合は preview 開始前に失敗する。

## 最小 live preview

1. Package Manager の Samples から `Canonical Source Avatar` をimportする。
2. scene に preview対象の有効な Humanoid Animatorを配置する。targetとその親は正のuniform scaleを使う。
3. `Window > Motion Bridge` を開き、scene Animatorとimport済みCanonical Source Avatarを指定する。
4. かんたんガイドではUnityの`プレビュー待受を開始`、Blenderの`Unityへ接続`の順に押す。詳細設定を使う場合だけ、両側のTCP control port/session IDを合わせる。再生中も継続する場合は`Unityを再生してもプレビューを続ける`を有効にする。UDP portはUnityがbindしてBlenderへ通知する。
5. UnityのPlay開始/終了ではEdit/Runtime listenerを同じport/session IDでhandoffし、Blenderが自動再接続する。`Stop`、timeout、sample例外ではtargetを開始前の状態へ戻す。

TCP controlは`hello`→`accept`→`skeleton_descriptor`→`ready`、`resume`/`pause`、`ping`/`pong`、`clock_sync_ping`/`clock_sync_pong`、`clock_sync_offset`/`clock_sync_reset`、500 ms周期の`pose_stats`、`goodbye`/`error`を実装済み。TCP/UDPともIPv4 loopback限定で、session、Canonical skeleton ID/hash、descriptor hierarchy、HumanTraitとnamed capabilityを検証する。受信、sequence gap、reject、receiver overrunの累積counterに加え、`clock-sync-v1`と`pose-stats-v1`に依存する`one-way-pose-age-v1`が交渉され、有効なoffsetとaccepted packet sampleがある間だけ`oneWayAgeUsecP50/P95`を送る。ageは固定長256 sampleで集計し、負値は破棄、5 msを超えるoffset jumpは同期を無効化してresetを要求する。contacts/IKとAnimation Rigging integrationは未実装。

## 更新・削除

更新前にUnity・Blenderのプレビューを停止し、作業を保存します。Package Managerから新しいtgzをインストールします。更新時の手順は配布ZIPの`QUICKSTART.md`に従ってください。

削除する場合は、プレビューを停止してからPackage Managerで`Motion Bridge > Remove`を選びます。SamplesからImportしたFBXはAssetsに残ります。使用中のシーンやAvatar参照を確認してから不要なサンプルだけを削除してください。

## 制限

- Unity EditorでのEdit Mode / Play Modeプレビュー用です。ビルド済みゲームとの接続はサポート対象外です。
- 接続は同じPCのIPv4 loopbackのみです。別PCへ配信する機能はありません。
- プレビュー対象はシーン上の有効なHumanoid Animatorで、対象および親TransformのScaleは正の均等値が必要です。
- 指の個別モーション、表情、衣装、髪、Generic rig、contacts/IK、Animation Rigging連携は対象外です。
- プレビュー中は対象AnimatorをMotion Bridgeが制御します。通常のAnimator Controllerによる再生へ戻すときはプレビューを停止します。
