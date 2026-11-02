# 実装計画・受入条件

## 1. 進め方

最初に UI や自動 foot lock を作らず、座標変換、HumanPose 変換、NLA COMBINE、Edit Mode 復元の四つを小さな spike で確定する。これらが失敗すると上位機能を作っても作り直しになる。

既存の `非公開の外部素材プロジェクト` は PoC 素材としてのみ使い、実装は `このリポジトリ` の独立 package に置く。local UPM 参照を追加する段階までは既存 repo を変更しない。

2026-09-02現在、TCP descriptor handshakeとnegotiated UDPのvertical slice、capability交渉付きclock sync/pose counter/one-way age telemetry、Pause/Resume、transactional FBX/glTF/GLB Source Take import、Source→CanonicalのExact/Mixamo/Auto/Manual初期bake、複数Correction→FBX、dirty/heartbeat scheduler、bulk FCurve writer、take budget/full-size dense baseline、実行時生成する異体格3体のcomponent test、最小cross-process Blender↔Unity transport E2Eまで進んでいる。Phase 0 exitに必要な4.5 LTS、live off/on 5反復performance gate、永続manifest付き3体golden/p95、全motion/lifecycle E2E matrixは未完である。

## 2. Phase 0 — Risk spikes

期間目安: 3〜5 開発日。

### S0.1 Coordinate / Canonical Avatar

成果物:

- Canonical の単一正本 `samples/canonical/mb_canonical_humanoid_v1.blend`。
- 決定的 generator `blender_addon/tools/build_canonical.py`。
- 生成物 `protocol/generated/canonical_humanoid_v1.json` と `verification/unity-6000.2/Packages/com.kuko.motionbridge/Samples~/Canonical/MB_Canonical_Humanoid_v1.fbx`。生成物は手編集禁止。
- Unity ModelImporter preset と Humanoid mapping validator。
- T-pose、hips yaw、shoulder raise、root motion の golden data。
- Canonical generator から派生させた権利上クリーンな synthetic Humanoid target 3 体（short-leg / canonical / long-leg）と体格比 manifest。

合格:

- Blender evaluated pose → Unity joint local pose の回転誤差 p95 0.1 degree 以下。
- bind joint position 誤差 0.1 mm 以下。
- root 1 m / yaw 90 degree の方向と単位が一致。
- skeleton hash mismatch を検出できる。
- clean checkout から二回生成し、descriptor byte hash、FBX metadata を除く semantic skeleton/clip hash、jointPaths、parentIndex、bind matrices、Humanoid mapping が一致する。raw FBX hash は CreationTimeStamp 等で変わり得るため gate にしない。
- synthetic 3 target の humanScale、脚/腕/胴長比が manifest と一致し、少なくとも各軸で 15% 以上の差がある。

停止条件:

- FBX importer が canonical joint paths を安定して再現しない場合、FBX Source Avatar の生成設定を修正する。Euler component swap で場当たり修正しない。

### S0.2 HumanPose retarget

成果物:

- unbound `HumanPoseHandler(avatar, jointPaths)` spike。
- `SetInternalAvatarPose` → `GetInternalHumanPose` → Core-only PoseWriter job。Phase 0 は Animation Rigging / RigBuilder 非依存。
- 体格の異なる target 3 体での比較 scene。

合格:

- source local pose から HumanPose が生成できる。
- T-pose、Hips yaw +90°、左肩 +90°、root trajectory +1 m/+90° の各 golden pose で、required joint 回転誤差 p95 0.1°、位置誤差 p95 1 mm 以下。
- Hips local delta `(0.10 m left, -0.25 m down, 0.15 m forward)` を Blender → packet → HPH → PoseWriter まで通し、Canonical target で各 axis 誤差 1 mm 以下、target humanScale 0.75/1.0/1.5 で normalized bodyPosition と最終 local displacement の関係が式どおり。
- target body position が humanScale に応じる。
- `HumanTrait.MuscleName[i]` と `MuscleHandle.name` の bijection が全 muscle で成立し、往復 pose 誤差 p95 0.1° 以下。
- warm-up 後 0 B/frame。

停止条件:

- target-bound `SetHumanPose` を製品実装へ流用しない。Playable writer が成立しない場合はこの phase で設計を見直す。

### S0.3 Edit Mode lifecycle

成果物:

- 専用 `MotionBridgeAnimationModeDriver` と Core manual PlayableGraph の `AnimationMode.SamplePlayableGraph` preview。
- stop、exception、assembly reload、play transition cleanup test。clean disconnectは全preview sessionを再生成し、UDP sequence resetを受理するtestまで実装済み。

合格:

- preview stop 後に Target Transform と Animator 設定が完全復元する。
- Scene/Prefab が dirty にならない。
- reload 後に socket、graph、NativeArray が残らない。
- 他 AnimationMode driver active 時は開始を拒否する。
- `BeginSampling` 後の sample 例外でも `finally EndSampling` が必ず走り、部分初期化失敗と二重 Stop が安全。
- stop/disconnect/sample 例外/reload/play 遷移/target 削除/window close の全経路で、全 bone local TRS、Animator controller/enabled/culling/applyRootMotion、Scene/Prefab dirty flag が開始前と一致する。

### S0.4 Blender NLA / performance

成果物:

- Slotted Action adapter 4.5 / 5.0。
- Base `REPLACE` + Correction `COMBINE` quaternion test。
- 65 bones、120 s、60 fps、約 190 万 key point benchmark generator。
- `benchmark_dense_take.py --seed 20260901 --bones 65 --seconds 120 --fps 60` で全 channel/key を決定的生成する。viewport は factory startup、Solid、overlay off、1920x1080、active armature 一体に固定する。
- 10 秒 warm-up 後 30 秒採取を 5 回。live off、loopback UDP sink への live 60 Hz on を同一 process/session で交互実行し、frame interval と sampler cost の p50/p95、dropped pose、allocation を JSON へ保存する。

実装状況: 決定的dense Action generator、point数tier判定、frame-set計測とJSON出力は実装済み。Blender 5.0.1 / Apple arm64で65 bones、260 F-curves、1,872,260 keysを生成し、生成0.395秒、frame-set mean 0.158 ms / p95 0.208 ms / max 0.821 ms、tier `HEAVY`を記録した。同じfull-size Actionのv1互換streaming SHA-256は14.815秒で完了し、全key payloadを一括保持しない。Source retarget writerはcurveごとのbulk `foreach_set`へ移行し、同環境の7201 frames / 98 curves / 705,698 keysの一回比較で旧per-key insert 14.150秒、bulk 0.199秒（71.18倍）、値と補間の一致を確認した。上記のwarm-up/30秒×5、live off/on差分、reference machine保存形式は未完。

合格:

- identity correction は base と一致。
- 90 degree correction に quaternion component-add artifact がない。
- live-link off/on の追加負荷 p95 2 ms 以下。
- playback fps 低下 10% 以下。
- `benchmark_results/<blender-version>/<machine-id>.json` に seed、Blender build hash、OS/CPU/GPU、設定、全反復 raw summary を記録する。4.5 LTS 実機/CIが未用意なら Phase 0 exit 不可。

Phase 0 exit: 四 spike 全合格、`docs/04_ADR.md` の provisional 項目を Accepted に更新。

## 3. Phase 1 — End-to-end vertical slice

期間目安: 1〜2 週間。

### Blender

- Extension register/unregister。
- Canonical rig 選択と validation。
- current evaluated pose sampler。
- TCP hello / descriptor、UDP pose sender。
- Source Archive Action + checksum/provenance、保存済み mapping からの Canonical Base regenerate/lock、複数Correction Layerの作成とCRUD/weight/mute/solo。
- root mode、send Hz、connect/pause UI。

実装状況: TCP hello/descriptor、negotiated UDP、dirty 30/60 Hz + static 5 Hz heartbeat、root mode/send Hz/start/pause/resume/stop UI、clock sync RTT、Unity pose counter、同期中one-way pose age p50/p95表示は実装済み。

### Unity

- UPM package と asmdef。
- EditorWindow listener、Target selection、validation。
- TCP control / UDP receiver / reader-owned triple buffer。
- protocol decoder と golden tests。
- HumanPose converter と PoseWriter graph。
- Edit Mode Preview lifecycle。
- diagnostics: sequence、age、drop、frame cost。
- NTP four-timestamp clock sync と TCP `pose_stats` telemetry。

実装状況: `clock-sync-v1` / `pose-stats-v1` / `one-way-pose-age-v1`のnamed capability交渉、clock sync応答、offset/reset、fixed 256-sample rolling p50/p95、500 ms周期の受信/sequence gap/reject/receiver overrun/age telemetryは実装済み。未同期時のage省略、負のage破棄、offset jump再同期も扱う。frame cost telemetryは未完。

### Vertical slice acceptance

1. Blender で Hips と left arm の Correction key を変更する。
2. Unity の target 3 体へ 60 Hz で反映する。
3. Blender scrub 中も古い pose backlog が生じない。
4. disconnect で Unity target が元 pose に戻る。
5. Blender/Unity どちらを先に停止しても freeze / unhandled exception がない。
6. live 中に FBX reimport、AssetDatabase Refresh、scene dirty が発生しない。

component testではTCP handshake/lifecycle、異体格3体への非ゼロpose、trajectory一回適用、restore/dirty維持を確認済み。さらにBlender 5.0.1 background clientとUnity 6000.2.15f1 batchmode serverを同時起動する自動E2Eで、descriptor handshake、capability交渉、clock sync/offset、Pause→Resume、`pose_stats`、3 UDP pose、正常切断を確認した（最終runはRTT `268 usec`、sequence gap/reject/overrun 0、one-way age p50 `172 usec` / p95 `174 usec`）。これは単一の短いtransport runであり、上記6項目を3体・Correction編集・60 Hz継続条件で全て通した受入E2Eではない。

## 4. Phase 2 — Motion editing MVP

期間目安: 2〜4 週間。

- FBX / glTF import adapter。
- Source mapping presetとmanual mapper（Exact/Mixamo/Auto/Manualの初期版は実装済み）。
- Source → Canonical one-time bake。
- Correction Layer CRUD、solo、weight、merge。
- active Correction専用NLA Tweak Mode、選択boneのcomplete Quaternion/Hips-only Location key、補間・neutral blend、current reset/delete、選択key/Preview Rangeを使うtone-curve式Bone Correction EnvelopeとReweight Existing（実装済み）。
- Canonical 22 boneの役割別色/Bone Collections、細い編集表示、Source wire referenceの表示・透過・非表示切替（実装済み）。
- Canonical LeftFoot/RightFootのevaluated world tailから現在frameの接地区間を検出し、Correction用Preview Rangeへ設定（実装済み）。全接地区間の解析2-bone Foot Lockも実装済み。
- lightweight Control Rig。
- hand/interactive foot IK、pole、world pin。
- Preview Range offset/ease（初期版実装済み）、NLA trim/retime。
- Before/After。
- Motion validator（実装済み）とoverlay。
- Character Profile 作成 UI。
- Animation Rigging contact pass。
- 数値 `CharacterProfile` と scene reference `MotionBridgeRigBinding` を分離し、既設 binding がある場合だけ optional Animation Rigging contact pass を有効化する。

実装状況: transactionalなFBX/glTF/GLB import adapter、原Actionを変更しない保護`SOURCE_ARCHIVE`/schema v2 provenance、Exact/Mixamo/Auto/Manual preset、Source Archive単独評価によるSource→Canonical one-time bake、確定mapping/frame/source/canonical identityを保存するrecipe、checksum同一結果だけを採用するtransactional Base regenerate、複数Correctionのstable-ID CRUD/solo/weight、専用Correction Edit、NLA COMBINE逆値変換、補間選択付きcurrent key/reset/deleteとPreview Range offset/ease、FCurve途中失敗rollback、Motion Validator、現在frameを含むFoot Contact Range検出、Quaternion-safe揺れ除去、signed関節可動域補正、全接地区間の解析2-bone Foot Lock、Beginner 3ボタンUI、Before/After、安全条件付きlossless Merge Downまで先行実装済み。自動補正はPreview Rangeまたは全takeを最大600 frameまで処理し、新規Correctionへ変更boneだけを書き、no-opではlayerを作らず、失敗時にAction/layer/active/revisionをrollbackする。Foot Contact Rangeは明示ボタン時だけ現在frame中心の走査窓を接地境界まで適応拡張し、最大20,000 samplesを高さ/速度/Min Frames/Join Gapで分類してframe/subframeとBase/全Correctionを保持する。schema v1 takeは旧Bakeへfallbackし、v2のrecipe/UUID/Slotは`.blend`保存・再読込後も元Source fileなしで解決する。Correction CRUDとedit hot pathはdense Base/Archiveのchecksum/curve走査を避ける。Mergeは全stackを層数比例で構造検証し、curve内容は対象2 Actionだけを検査する。checksumはv1 digest互換のcurve単位streaming hashで全key一括メモリ化を避け、Source Archive checksumも初回明示bake/regen境界だけで走査する。Before表示中のbake/exportは保存済みAfter構成を評価して表示を復元する。現importは新規Armature丁度1体と1つ以上の利用可能Actionを必須とする。mapping/algorithm変更時のCorrection rebase、twist配分、重複propertyのflatten merge、interactive Control Rig/Hand IK、contact packet生成、NLA retime、包括Profile/Bindingは未実装。

合格:

- 既存 FBX motion 10 本を import し、8 本以上が preset + 5 分以内の手動補正で Canonical 化できる。
- humanScale 比 0.5〜2.0、脚/腕/胴長比 0.65〜1.55 の envelope 内にある 3 体格 × 10 motion で、NaN、膝反転、required bone 欠落が無検出のまま通過しない。
- 足接触区間の target foot slide が未補正比 70% 以上低減し、かつ slide p95 2 cm 以下、ground penetration p95 1 cm 以下、reach 98% 以下、膝/肘反転 0 件。
- target が scene offscreen でも AlwaysAnimate 中は preview が更新される。

## 5. Phase 3 — Bake/export and polish

期間目安: 1〜2 週間。

- visual bake。
- quaternion continuity。
- rotation / effector error based decimation。
- animation-only FBX preset。
- Unity import comparison test。
- foot contact interval marker/一括検出、foot lock（接地検出とCanonical Foot Lock MVPは実装済み、marker編集は未実装）。
- smooth / clean / Euler discontinuity filter UI（Quaternion-safe揺れ除去の3 preset UIは実装済み）。
- error report export。

実装状況: Base+全Correctionのvisual bake、quaternion continuity、active/corrected Actionのanimation-only FBX export、現在frameのFoot Contact Range検出、Quaternion-safe揺れ除去、関節可動域補正、全接地区間のCanonical Foot Lock MVPは先行実装済み。error-based decimation、Unity imported clip比較、interactive IK/Control Rig、contact packet生成、contact marker編集は未実装。

合格:

- pre-IK Blender live pose と Unity imported FBX clip の pre-IK 主要 effector 位置差 p95 1 cm 以下、rotation 差 p95 1 degree 以下。
- post-IK 比較は imported clip に同一 Character Profile / Rig Binding を適用した結果同士で行う。standalone post-IK asset は target-specific Unity bake の別成果物とする。
- export に意図しない Action / mesh / camera が含まれない。
- key reduction 後も tolerance を超える frame がない。

## 6. 初回実装タスク順

1. `protocol/schema` に descriptor JSON schema と PosePacket layout constant を作る。
2. Python encoder/decoder と C# encoder/decoder の共有 golden packet test、schema/constant drift check を作る。
3. Canonical 単一正本、generator、Source Avatar、descriptor を作り、二回生成 hash と axis golden test を固定する。
4. Unity unbound HumanPoseHandler spike を作る。
5. Unity PoseWriter job の Edit Mode preview を作る。
6. Blender evaluated pose sampler を作る。
7. TCP handshake と UDP latest-only transport を繋ぐ。
8. 一枚の Correction NLA COMBINE を繋ぐ。
9. cleanup / reload / disconnect tests を通す。
10. importer、mapper、Foot Lockまで追加済み。次にControl Rigを追加する。

## 7. Test matrix

### Blender unit / background tests

- Slotted Action create/assign/remove。
- quaternion COMBINE identity / 90 degree。
- skeleton mapping required/optional/duplicate。
- rest-relative delta calculation。
- coordinate conversion golden vectors。
- packet encoding validation。
- register/unregister 二回で handler 重複なし。
- load_post で runtime cache 再構築。

### Unity EditMode tests

- TCP framing partial read / multi message。
- UDP packet valid/invalid/truncated/NaN/out-of-order。
- reader-owned triple buffer concurrency（producer 二周中も Reading slot 非上書き）。
- Skeleton Descriptor hash / hierarchy validation。
- RFC 8785/JCS conformance（`-0`、指数、Unicode、key order）と semantic FBX skeleton hash。
- `HumanTrait` count/name capability mismatch。
- HumanPose scale conversion。
- Core graph create/evaluate/dispose。Phase 0 は RigBuilder 非依存。
- disconnect、assembly reload、play transition cleanup。
- Scene dirty なし、Target property restore。

### Unity PlayMode tests

- PoseWriter humanoid stream validity。
- stance reset → muscles → body order。
- target 3 体の retarget。
- Animator culling restore。
- root mode InPlace / WorldRoot。
- optional Rig constraints ordering。

### End-to-end

- T-pose。
- 片腕 90 degree。
- walk loop と root motion。
- fast punch。
- crouch / deep knee bend。
- one-foot contact。
- long dense clip 120 s。
- rapid scrub、pause/resume、packet loss 10%。
- static pose 10 秒（5 Hz heartbeat で保持）、clock offset/drift、sequence wrap、receiver overrun telemetry。
- Unity domain reload、Blender addon reload、片側 crash。

## 8. Definition of Done

機能は次を満たして Done とする。

- happy path だけでなく cleanup / invalid input test がある。
- hot path に per-frame allocation、blocking I/O、AssetDatabase call がない。
- UI 操作をせず background test で中核を検証できる。
- versioned protocol と schema へ反映される。
- p95 frame cost、allocation、packet size を記録する。
- error がユーザー操作で回復可能か明示される。
- Blender 4.5 LTS と 5.0.1 の対象 test が通る。
- Unity 6000.2.15f1 の EditMode/PlayMode test が通る。

## 9. PoC 素材

Phase 0 の必須 retarget test は Canonical generator が生成する配布可能な synthetic target 3 体を使う。固定入力は humanScale と脚/腕/胴長比を manifest に記録し、差が小さければ test target として不合格にする。

実プロジェクトでの任意 PoC 入力は 公開対象外のローカルPoC manifest を正本とし、非公開素材の固定リビジョン 上の 3 character / 3 motion と各 `.meta` SHA-256 を記録する。hash 不一致、または `licenseStatus != verified-for-local-poc` の間は使用しない。

第三者素材は配布 repository へ commit せず、権利確認済みの最小 synthetic rig/motion だけを `samples` に含める。

## 10. 主要リスク

| リスク | 兆候 | 対策 |
|---|---|---|
| Blender→Unity axis mismatch | T-pose は合うが回転で反転 | global rest-relative delta + matrix conjugation + golden test |
| HPH jointPaths mismatch | SetInternalAvatarPose 後の pose が不正 | Canonical Avatar と descriptor hash を固定 |
| Animator writer 競合 | 一 frame ごとに pose が戻る | single writer、Preview Override、graph ownership |
| Edit preview が scene を汚す | stop 後も値/dirty が残る | AnimationMode sampling と全終端 cleanup test |
| Dense Action UI が重い | Graph Editor / scrub stall | Base curve 非表示、Canonical only、take 分割、native evaluation |
| Blender socket が UI を止める | Unity 停止時 freeze | non-blocking、latest-only、timeout、main timer budget |
| target 体格で foot slide | 長い脚/短い脚で接触ずれ | HumanPose + profile + post-retarget IK |
| Unity package dependency conflict | Animation Rigging version 不一致 | Core と Integration assembly 分離、version define |
