# Architecture Decision Records

## ADR-001: ライブ transport に FBX を使わない

- 状態: Accepted
- 決定: ライブ姿勢は TCP control + UDP pose。FBX は import/export だけ。
- 理由: FBX 再出力と Unity reimport は latency、AssetDatabase、GC、ファイル競合を発生させる。

## ADR-002: Blender 補正レイヤーは NLA COMBINE

- 状態: Accepted。Canonical Action上の複数layer core、初期Source→Canonical bake、22項目Manual mapping、transactional Source Take import、保存recipeからのBase regenerateはregression test合格
- 決定: Base=`REPLACE`、Correction=`COMBINE`。Action 内部 layer をユーザー向け補正レイヤーにしない。
- 理由: native evaluator を使え、quaternion を成分加算せず非破壊編集できる。
- 原本保護: Source Rig の import 原本は非評価 Archive Action + checksum/provenance として保存し、Base は保存済み mapping から Canonical Rig へ決定的 retarget/bake した派生成果物とする。NLA lock だけを不変性の根拠にしない。
- 現実装: `.fbx` / `.gltf` / `.glb`のSource Take importは、新規Armature丁度1体と1つ以上の利用可能Actionをtransactionalに確定し、失敗時は新規datablockをrollbackする。原Actionは変更せず、schema v2のresolved path、file hash/format、Action/range、bone hierarchy/bind pose、instance UUID、明示Slotを記録したfake-user `SOURCE_ARCHIVE` copyのFCurve/groupを保護する。schema v1 takeは従来Bakeへfallbackする。import済みtakeの初回bakeはこのArchiveだけを一時評価し、確定mapping/frame range/Source・Canonical identity/checksumをrecipeへ保存する。`Regenerate Base from Source Archive`はrecipeから候補を作り、初回Canonical checksumと同一の場合だけBase/Canonical Archiveを原子的に交換するためCorrection identity/状態を暗黙rebaseしない。Source/target rename、元file消失、`.blend`再読込、candidate/checksum/sample/commit失敗rollbackをbackground testで検証する。Source constraint/driver等の外部評価依存変更は同一結果保証外で、checksum不一致として安全に拒否する。Exact/Mixamo/Auto/Manual mapping、身長比Hips、object trajectory、stable ID付き複数Correction=`COMBINE`のCRUD、Before/After、安全条件付きlossless Merge Downも実装済み。CRUDとMergeはdense Base/Archiveを走査せず、Source/Canonical checksumは明示bake/regenerate/export境界だけで検証する。mapping/algorithm変更を伴うCorrection rebaseと重複property flatten mergeはまだない。

## ADR-003: Blender hot path は native animation evaluation

- 状態: Accepted
- 決定: Python は evaluated pose を読むだけ。全 bone を Python から毎 frame 駆動しない。
- 理由: clip 長や Python callback 負荷を定常 playback に持ち込まない。
- Fallback: benchmark 不合格時に take 分割、Keys to Samples、外部/native evaluator を順に検証する。
- 現実装: Action point数を`<500k` / `500k〜2M` / `>2M`へ分類してUI表示し、約190万pointの決定的benchmark generatorを用意した。Blender 5.0.1 / Apple arm64の65 bones・120秒・60 fps baselineは1,872,260 keys、生成0.395秒、frame-set p95 0.208 ms。Source retarget writerはtyped arrayへ蓄積後、curve単位の`keyframe_points.add + foreach_set`でcommitする。7201 frames / 98 curves / 705,698 keysの同一出力比較は旧insert 14.150秒、bulk 0.199秒だった。正式live off/on 5反復benchmarkと4.5 LTS gateは未完。

## ADR-004: Unity リターゲット境界は HumanPose

- 状態: Provisional。unbound converterと全muscle writer core、実行時生成する異体格3 targetのcomponent testは実装済みだが、永続manifestとend-to-end p95 gateは未完
- 決定: Canonical joint local pose を unbound HumanPoseHandler で HumanPose にし、Target へ muscle pose を適用する。
- 理由: Blender が Unity muscle 数学を再実装せず、Target の骨長・Avatar 定義を利用できる。
- 現実装: packetのglobal rest-relative deltaをCanonical rest global rotationへ合成し、parentのglobal rotationからlocal rotationを再構成する。Hipsだけrest local positionへpacket deltaを加え、23 pathを渡したunbound `HumanPoseHandler`で`HumanPose`へ変換する。synthetic root deltaは符号同値のidentityだけを受理し、trajectory rootはHumanPose bodyと分離する。
- 体格test: leg/arm/torso比が大きく異なる3 Avatarへ同じ非ゼロ左右対称poseを適用し、finite値、非ゼロmuscle、左右対称性、trajectory一回適用、停止時restore/scene dirty維持を確認する。これはfoot contactや全motion envelopeの品質保証ではない。
- Muscle mapping: `HumanPose.muscles[i]` は `HumanTrait` 順、`AnimationHumanStream.SetMuscle` は `MuscleHandle` を要求するため、`MuscleHandle.GetMuscleHandles` の列挙indexを直接使わない。通常muscleは `HumanTrait.MuscleName[i]` とhandle名をexact matchする。fingerだけUnity 6000.2の命名差（例: `Left Thumb 1 Stretched` と `LeftHand.Thumb.1 Stretched`）を `HumanTrait.BoneFromMuscle(i)` でfingerと確認してから正規化し、全要素のbijectionを開始時に検証する。
- Writer順序: stance reset → 全muscle → `bodyPosition * humanScale + trajectoryPosition` → `trajectoryRotation * bodyRotation`。target-bound `HumanPoseHandler.SetHumanPose` は製品writerに使わない。
- 確定条件: Canonical bind/basis と unbound HumanPoseHandler の Phase 0 golden test 合格。

## ADR-005: Unity final writer は Playable/Rig graph 一つ

- 状態: Provisional。Core PoseWriterは実装済み、optional Rig integrationは未実装
- 決定: PoseWriter job を唯一の基底 writer とする。Phase 0/1 は Core-only。Phase 2 で明示的な Rig Binding がある場合だけ、同じ所有 graph に contact pass を置く。
- 理由: `SetHumanPose`、Animator、RigBuilder の同時 Transform 書込み競合を防ぐ。
- 現実装: manual `PlayableGraph` の単一 `AnimationScriptPlayable` が `AnimationHumanStream` へstance/muscle/body/trajectoryを書き、二つ目のMotion Bridge writerと既存AnimationMode ownerを拒否する。
- 確定条件: Core lifecycle spike と optional Rig integration spike の合格。

## ADR-006: Edit Mode は AnimationMode.SamplePlayableGraph

- 状態: Accepted。stop/sample failure/target破棄/二重dispose/scene dirtyに加え、clean peer close後の全session再生成とsequence resetのcore testは合格。reload/play遷移を含む全終了matrixは未完
- 決定: 配置済み Target を AnimationMode で sample し、全終端で復元する。
- 理由: live pose を Scene/Prefab の永続変更にしない。

## ADR-007: protocol pose は joint rotation、rest translation は handshake

- 状態: Provisional
- 決定: 毎 frame は trajectory root + Hips local position delta + global rest-relative joint quaternion + contacts。その他の rest local translation は TCP descriptor。
- 理由: HPH が必要とする 7 floats/joint を毎 frame 送ると安全な UDP MTU を超え得る。Humanoid では非 root translation を保たない方が体格リターゲットに適する。
- 確定条件: Phase 0 axis / HumanPose test 合格。

## ADR-008: loopback-only を既定にする

- 状態: Accepted
- 決定: TCP/UDP とも `127.0.0.1`。LAN は明示 opt-in。
- 理由: 認証なし低 latency protocol の攻撃面を限定する。

## ADR-009: 独立 repository / UPM package

- 状態: Accepted
- 決定: 既存 Unity Assets 直下へ実装せず、独立 Blender Extension + UPM package にする。
- 理由: 複数プロジェクトで再利用でき、dirty な production project への干渉を避けられる。

## ADR-010: trajectory root と HumanPose body を分離

- 状態: Provisional
- 決定: packet root は locomotion trajectory 専用。HumanPoseHandler joint 0 は descriptor の FBX root rest transform `R=RotX(-90°)` を使い pose delta は identity とする。HumanPose body は Hips/COG 専用。WorldRoot は session 開始時 target anchor へ trajectory delta を一度だけ適用する。
- 理由: root motion の二重適用と配置済み character の world position 喪失を防ぐ。
- 確定条件: root +1 m/yaw 90° golden test と start/stop anchor restore test 合格。

## ADR-011: 接触 packet は stateless goal

- 状態: Provisional
- 決定: 毎 pose に contact weight、epoch、anchor position/rotation、ground plane を含める。
- 理由: packet loss、scrub、逆再生、frame jump 後も一 packet から pin goal を再構成するため。
- 確定条件: 10% loss / reorder / random seek test 合格。

## ADR-012: latest-only transport は reader-owned triple buffer

- 状態: Provisional。loopback receiverと基本race/lifecycle testは実装済み、本格producer/consumer stressとprofiler gateは未完
- 決定: 三 slot の ownership state と atomic published index を使い、Reading slot を producer が上書きしない。
- 理由: double buffer では producer が二 packet 先行すると consumer 読取中 slot を再利用できる race がある。
- 現実装: background socket threadがdecode/session/joint/required-mask/sequence gate後に最新slotだけをpublishし、main threadはcaller-owned `PoseFrame` へsnapshotする。stop/disposeはsocket closeでreceiveを解除し、port rebind可能。
- 確定条件: producer/consumer stress test と zero-allocation profiler test 合格。

## ADR-013: Canonical は Blender 正本から決定的生成

- 状態: Provisional
- 決定: `samples/canonical/mb_canonical_humanoid_v1.blend` を唯一の正本とし、descriptor と Unity Source Avatar FBX は generator の出力とする。
- 理由: joint list、rest basis、Avatar mapping、hash の手編集 drift を防ぐ。
- 確定条件: clean 二回生成の descriptor bytes、FBX semantic skeleton/clip hash、jointPaths、bind matrices、mapping 一致。生成時刻を含む raw FBX hash は gate にしない。

## ADR-014: Character Profile と scene Binding を分離

- 状態: Provisional
- 決定: Profile は数値と asset identity だけ、Animator/effector/socket/prop は scene Component の `MotionBridgeRigBinding` が所有する。
- 理由: ScriptableObject から scene instance を永続参照せず、preview が Scene/Prefab 構造を動的変更しないため。
- 確定条件: Phase 2 integration test と scene dirty なし test 合格。

## ADR-015: 接触 goal は固定 SessionAnchor space

- 状態: Provisional
- 決定: trajectory、contact anchors、ground plane は Streaming 開始時に固定した SessionAnchor space で送る。現在 trajectory root の local spaceにはしない。
- 理由: 同じ contact epoch 中に root が進んでも world contact goal を動かさず、foot lock を維持するため。
- 確定条件: root 前進中の一足接地で slide p95 2 cm 以下。

## ADR-016: monotonic clock は NTP four-timestamp で同期

- 状態: Provisional。capability交渉、four-timestamp exchange、直近8 samplesの最小RTT選択、offset/reset、500 ms pose counterとone-way age telemetryは実装済み。人工driftと長時間gateは未完
- 決定: TCP clock sync から Unity−Blender offset を推定し、Unity が pose one-way age と drop を `pose_stats` で返す。
- 理由: 異なる process の monotonic timestamp は epoch が異なり、直接差分では latency を測れない。
- 現実装: Blenderが`clock-sync-v1` / `pose-stats-v1` / `one-way-pose-age-v1`を提示し、Unityは依存capabilityを含む共通集合だけをacceptする。Blenderは接続時と5秒ごとにsyncし、RTTとoffsetを検証し、有効offsetまたはresetをUnityへ送る。Unityはaccepted packetのageをallocationなしの256-sample ringで集計し、同期/sampleが有効な間だけ`oneWayAgeUsecP50/P95`をcounterに加えて返す。負のageは破棄し、5 ms超のoffset jumpはreset後の再同期を要求する。
- 確定条件: 人工 offset/drift test と localhost latency measurement の再現。

## ADR-017: Skeleton Descriptor hash は RFC 8785/JCS

- 状態: Provisional
- 決定: descriptor object を RFC 8785 JCS の UTF-8 bytes へ canonicalize し SHA-256 を取る。
- 理由: Python/C#間で `-0`、指数表記、float/key serialization の独自差を残さないため。
- 確定条件: 両言語が同じ JCS conformance vectors と descriptor hash を生成する。
