# Motion Bridge システム設計仕様

- 文書版: 0.2.0
- 状態: Phase 0 Spike In Progress（中核 ADR は spike 合格まで Provisional）
- 更新日: 2026-09-02
- 基準環境: Blender 5.0.1、Unity 6000.2.15f1

## 1. 結論

Motion Bridge は、FBX をライブ更新する仕組みではなく、次の二つを組み合わせて実現する。

1. Blender は Source Rig の import 原本を Archive Action として保存し、保存済み mapping から Canonical Base Action を決定的に retarget/bake する。疎な差分だけを NLA `COMBINE` Correction として編集する。
2. Unity には評価済み Canonical Humanoid 姿勢を送信し、Unity 内で `HumanPoseHandler` により Mecanim HumanPose へ正規化してから、単一の Playable graph で対象キャラクターへ適用する。Animation Rigging は Phase 2 の任意 integration とする。

これにより、Blender 側は密なキーを Python で毎フレーム処理せず、Unity 側はキャラクター固有の骨長を保ったまま Mecanim と IK で補正できる。FBX は入力と最終書き出しにだけ使い、ライブ中に `AssetDatabase.Refresh` や再 import は行わない。

「どの体格でも絶対に破綻しない」は保証対象にしない。Humanoid Avatar が有効で、定義した体格・可動域の範囲内にあるキャラクターに対して、事前検証、Mecanim リターゲット、接触 IK、関節制限、エラー可視化により破綻を検出・抑制する。

## 2. 目的

### 2.1 必須成果

- Blender で FBX / glTF の Humanoid モーションを読み込み、元データを壊さず修正できる。
- Blender の再生、スクラブ、ポーズ編集が Unity 上の配置済み Humanoid モデルへ 30/60 Hz で反映される。
- 異なる骨長・身長のモデルへ、Transform の生コピーではなく HumanPose を介してリターゲットする。
- 足・手の接触、床抜け、膝反転、root motion の二重適用を抑制できる。
- 長い、または全フレームにキーがあるモーションでも、クリップ長に比例して毎フレーム処理が重くならない。
- 修正済みモーションを visual bake し、animation-only FBX として Unity の通常 import パイプラインへ渡せる。

### 2.2 非目標

- 非 Humanoid、四足、翼、触手などの Generic Rig。
- 表情 BlendShape、リップシンク、クロス、髪、SpringBone、物理シミュレーションの同期。
- Blender と Unity のプロジェクト全体同期。
- インターネット越しの共同編集。v1 は localhost のみ。
- MotionBuilder の全機能再現。
- Unity 実行中の gameplay Animator Controller とライブ姿勢の任意ブレンド。v1 は排他的 Preview Override。

## 3. 対応条件

### 3.1 バージョン

| 項目 | 基準 | 方針 |
|---|---:|---|
| Blender | 5.0.1 | 開発・手元検証対象 |
| Blender | 4.5 LTS | 互換下限。Slotted Action API を使用 |
| Unity | 6000.2.15f1 | 開発・PoC 対象 |
| Animation Rigging | 1.4 系 | 接触補正フェーズで導入。Core と分離可能にする |

Blender 4.4 以前は Action API の差が大きいため v1 対象外とする。旧 `Action.fcurves` 互換 API に依存せず、Action Slot / Layer / Strip / Channelbag を `compat/action_api.py` に隔離する。

### 3.2 対象 Avatar

Unity 対象は次を全て満たすこと。

- `Animator` と `Avatar` が存在する。
- `avatar.isValid && avatar.isHuman` が true。
- `animator.isHuman` が true。
- Hips、spine chain、head、両 upper/lower arm、両 hand、両 upper/lower leg、両 foot がマップ済み。
- Transform に NaN、ゼロ scale、負 scale、非一様 scale がない。
- v1 では Optimize Game Objects が無効。対応時は必要骨を `extraExposedTransformPaths` へ明示する。

任意骨（UpperChest、Toes、Eyes、Jaw、Finger）は未マップでも接続できるが、該当補正機能を無効化して警告する。

## 4. 用語

| 用語 | 定義 |
|---|---|
| Source Rig | 読み込んだ FBX / glTF 固有の骨格 |
| Canonical Rig | Motion Bridge が固定定義する Humanoid 骨格 |
| Archive Action | import 直後の原本。UI から編集せず checksum と provenance を保持 |
| Base Action | 元モーション。読み取り専用、NLA `REPLACE` |
| Correction Layer | 疎な差分 Action。NLA `COMBINE` |
| Control Rig | root/COG、pelvis、hands、feet、poles、head を中心とする軽量制御骨 |
| Evaluated Pose | FCurve、NLA、Constraint、Driver 適用後の Depsgraph 上の姿勢 |
| Source Avatar | Canonical Rig と一致する Unity Humanoid Avatar。HumanPose 変換専用 |
| Target Avatar | Unity シーン上でプレビューするキャラクター |
| Character Profile | キャラクター固有の数値設定を持つ ScriptableObject |
| Rig Binding | scene 内の Animator、effector、socket、prop 参照を持つ Component |

## 5. 全体構成

```text
Blender
  FBX / glTF
      │ import + map + one-time retarget/bake
      ▼
  Canonical Rig
      ├─ Archive Action    : immutable source + checksum
      ├─ Base Action       : derived canonical bake / NLA REPLACE / locked
      ├─ Correction Actions: NLA COMBINE / sparse keys / stable IDs
      └─ Lightweight Control Rig + native Constraints
      │
      ▼ evaluated depsgraph
  Pose Sampler ── UDP pose ─────────────────────────────┐
  Session/Schema ─ TCP control ───────────────────────┐ │
                                                      │ │
Unity Editor                                         │ │
  Control Server ◀───────────────────────────────────┘ │
  Latest-only Pose Receiver ◀──────────────────────────┘
      │ main thread
      ▼
  Canonical joint pose
      │ HumanPoseHandler(avatar, jointPaths)
      ▼
  HumanPose
      │ PoseWriter IAnimationJob
      ▼
  optional MotionBridgeRigBinding: foot/hand/pelvis/aim corrections
      ▼
  Target Animator / placed FBX character
```

### 5.1 責務境界

- Blender: モーション編集、Canonical Rig への正規化、接触マーカー生成、最終 bake。
- Protocol: セッション、Skeleton Descriptor、フレーム姿勢、接触 goal/epoch/ground plane。キャラクター asset は送らない。
- Unity: HumanPose 変換、Target Avatar 検証、対象体格へのリターゲット、キャラクター固有 IK、Edit Mode の安全な preview。
- FBX: 入力と最終成果物。ライブ transport として使わない。

## 6. ユーザーフロー

### 6.1 初回セットアップ

1. Git checkout内の`verification/unity-6000.2`を開く。Motion Bridgeは`Packages/com.kuko.motionbridge`のembedded packageとして同梱する。
2. Unity で `Window > Motion Bridge` を開き、シーン上の Target Animator を選ぶ。
3. Target Validation を実行する。Error が一件でもあれば Live Preview は開始しない。
4. Unity 側で `Start Listener` を押す。既定 bind は `127.0.0.1`。
5. Blender Extension を有効化し、`Motion Bridge > Session` から Unity へ接続する。

### 6.2 モーション編集

1. `Import Take` から FBX / glTF を読み込む。
2. Source bone mapping preset を選ぶ。自動判定に失敗した骨だけ手動修正する。
3. `Normalize to Canonical` を実行する。Base Action はロックされる。
4. `Add Correction Layer` を作成し、Control Rig または選択骨へ差分キーを打つ。
5. 必要に応じて現在frameを含む Foot Contact Range を検出し、CorrectionのPreview Range適用に使う。Foot Lock、smooth、decimate、time warpは後続機能とする。
6. Blender を再生・スクラブし、Unity の複数 Target Profile で破綻を確認する。
7. `Validate Motion` で foot slide、ground penetration、joint limit、root discontinuity を確認する。

### 6.3 確定

1. `Visual Bake` で評価済み姿勢を新しい Action へ bake する。
2. quaternion 角度誤差と effector 位置誤差を基準にキー削減する。
3. animation-only FBX を一つの明示 Action だけで出力する。
4. Unity で通常の Humanoid AnimationClip として import し、Target Avatar へ再生する。

pre-IK Live Preview と imported FBX の pre-IK 結果差が許容値を超えた場合、export 成功扱いにしない。post-IK を比較するときは imported clip に同じ Character Profile と Rig Binding を適用する。キャラクター単体へ post-IK 結果を焼き込む要件は将来の target-specific Unity bake として分離する。

## 7. Blender 設計

### 7.1 Import Adapter

`io/source_import.py` が importer 差とtransaction境界を隠蔽し、Blender非依存の`io/source_take.py`がpath検証とprovenanceを定義する。

- Fast path: Blender の C++ FBX importer が利用可能ならオプトインする。
- Fallback: Python FBX importer。
- glTF は Slotted Action 対応を利用する。
- importer 実行後、単位、axis、rest pose、bone hierarchy、frame rate を検証する。
- Source mesh、material、camera、light は Motion 編集用 Collection から除外し、評価対象を Armature と Action に絞る。
- Source Rig から Canonical Rig へのリターゲットは初回に一度 bake し、その後 Source Rig は無効化できる。

Source Rig を毎フレーム Constraint で参照し続けない。名称変換や任意 skeleton の差は import 段階で吸収する。

現実装の`Import Source Take`は`.fbx`、`.gltf`、`.glb`を受け付ける。FBXはBlender 5のnative importerを先に試し、失敗した試行の新規datablockを除去してcompatibility importerへfallbackする。import前に主要な`bpy.data` collectionとeditor stateをsnapshotし、新規Armatureが丁度1体、かつキーを持つ利用可能な新規Actionが1つ以上のときだけcommitする。候補が1つなら自動選択し、複数ならUIでAction名とframe範囲を示して1つ選択させる。候補のtake IDはfile/hierarchy、scene-localなSlot名を除いたcurve-content checksum、stable candidate ordinalから生成するため、同じframe範囲のActionを区別しつつ、既存datablock名の衝突による`.001` suffixには影響されない。headless/APIでは構造化候補付きerrorにし、暗黙選択しない。選択ActionはSource表示へ明示bindし、同じActionだけをArchive/provenanceの対象にする。import/validation失敗時はtransaction中の新規datablockをrollbackし、成否にかかわらずselection、active object/mode、timeline/preview range/current frameを復元する。

Source Action自体にcustom propertyやlockを追加せず、fake-userの`SOURCE_ARCHIVE::<file-hash>::<action>` copyを作る。schema v2のArchiveとSource Armatureにはresolved absolute filepath、file SHA-256、format、Armature/Action名、frame範囲、bone name/parent hierarchy SHA-256、head/tail/rest matrixを含むbind-pose SHA-256、選択take ID、stable archive ID、scene instance UUID、明示Action Slot identifierを保存し、Archiveの全FCurveをlock/hide、groupをlockする。新規takeではtake IDをArchive identity、recipe source archive ID、operator reportまで一貫して用いる。Source Archive content checksumは初回の明示retarget bake時に一度計算して保存するため、import/playback hot pathへ毎frame走査を持ち込まない。schema v1 takeは旧evaluated-source bakeへfallbackし、再生成recipeを必要とするときだけ再importを要求する。複数Armature take、animation keyのないtake、Generic Rig、import option provenance、mesh/material/camera/lightの自動整理は現時点の対象外。

### 7.2 Canonical Rig

- Unity Humanoid の required bones を必須とする。
- UpperChest、Toes、Eyes、Jaw、Finger は optional。
- 永続 ID、schema version、skeleton hash を Armature custom property に保存する。
- rest pose は T-pose を基準とし、shoulder、elbow、knee の bend direction を validator が確認する。
- 単一正本は `samples/canonical/mb_canonical_humanoid_v1.blend`。`blender_addon/tools/build_canonical.py` が同一正本から `protocol/generated/canonical_humanoid_v1.json` と `verification/unity-6000.2/Packages/com.kuko.motionbridge/Samples~/Canonical/MB_Canonical_Humanoid_v1.fbx` を生成する。
- descriptor / FBX は生成物であり手編集禁止。clean 状態で二回生成し、descriptor の byte hash、joint paths、parent index、bind matrices、Unity Humanoid mapping、semantic skeleton hash が一致しなければ Phase 0 を合格にしない。FBX exporter が生成時刻 metadata を埋めるため raw FBX file hash は gate にせず、metadata を除いた skeleton/animation semantic projection を比較する。
- Canonical Armature object の基準transformはidentityに固定する。Animationの`location` / `rotation_quaternion`によるevaluated trajectoryは許可し、Live開始時の剛体transform（unit scale）をSessionAnchorとする。FBX local handedness `F = diag(-1,1,1)` と FBX root axis rotation `R = RotX(-90°)` を分離し、物理 bone の rest local は `F * L_blender * F`、rest global は `R * F * G_blender * F`、joint 0 の rest local/global は `R` とする。Unity importer は `bakeAxisConversion=false`。

Source → Canonical の one-time bake は joint ごとの source/canonical bind global 行列から補正を作る。

```text
D_source(i,t)    = P_source(i,t) * inverse(B_source(i))
A(i)             = B_canonical(i) * inverse(M(i) * B_source(i))
D_canonical(i,t) = A(i) * M(i) * D_source(i,t) * inverse(M(i)) * inverse(A(i))
P_canonical(i,t) = D_canonical(i,t) * B_canonical(i)
L_canonical(i,t) = inverse(P_canonical(parent,t)) * P_canonical(i,t)
```

`M(i)` は source bone basis から canonical basis への mapping。root trajectory の translation/rotation は limb pose と分離して単位・basis 変換し、twist は chain 長に基づく定義済み weight で分配する。bone name、roll、A/T pose 差を Euler の component swap で吸収しない。この式と Canonical joint list は Phase 0 golden test で確定する。

現実装の初期subsetは、Import Source Takeまたは既にSceneへ読み込まれたSource Armatureを対象に、Unity HumanBone exact名、Mixamo名、両方を試すAutoのconservative preset、または22項目のManual profileからmappingを作り、Sceneの整数frame範囲をevaluated poseからCanonical Actionへbakeする。Manual profileはSource instance/archive/action Slot/hierarchy/bind pose fingerprintへ紐付け、Scene内ではSourceごとのversioned registryと現在選択中のprofileを保持する。Auto Populate、Clear、source bone pickerを持ち、手動欄の変更もprofileへ即時同期する。Source差替え時は別Sourceのmappingを流用せず、同じSourceへ戻した場合だけそのprofileを復元する。既存Correctionがある作業ファイルでは明示rebaseが未実装のためSource/mapping変更をUI、operator、RNA update callbackで停止し、直接propertyを書き換えた場合も元値へ戻す。namespaceと句読点は正規化するがfuzzy substring matchは行わず、曖昧名、required bone欠落、duplicate mapping、Sourceから消えた手動bone名をhard errorにする。schema v2 takeの初回bakeはmutableなSource Action/NLAではなく`SOURCE_ARCHIVE`を明示Slotへ一時bindして単独評価し、Source binding/NLA/editor stateをfinallyで復元する。確定mapping、frame範囲、Source instance/archive/checksum/hierarchy/bind pose、Canonical skeleton/bind pose/Slot、algorithm versionをsorted JSON recipeとしてBase、Canonical Archive、Armatureへ保存する。Archiveはanimation channelの正本だが、評価時のSource object/bone constraint、driver、外部targetまでは埋め込まないため、それらを変更したrigは同一結果を保証する対象外とする。

`Regenerate Base from Source Archive`は現在のpanel mappingやScene範囲を参照せず、保存recipeから候補Baseをunboundで生成する。Source Archive checksum、Source/Canonical bind pose、mapping、Action Slot、同一frame範囲、local editable datablockをpreflightし、候補checksumが初回Canonical Archive checksumと一致する場合だけBase stripとCanonical Archiveを原子的に交換する。全Correction Action/UUID/curve/order/mute/solo/influence、Before/After、Source/editor stateを保持し、候補作成、checksum、state capture、sample、commit中の例外では候補Actionを削除して旧stackへrollbackする。保存・再読込、Source/target/Archive rename、元Source file消失もUUIDと埋め込みArchiveで解決する。constraint/driver等の評価依存変更やmapping/algorithm更新でchecksumが変わる場合は既存Correctionの意味を変えるため採用せず、後者は将来の明示rebase機能へ分離する。twist配分は未実装。

### 7.3 非破壊レイヤー

| Track | Blend | 編集 | 内容 |
|---|---|---|---|
| Archive | 非評価 | immutable | Source Rig の import 原本、checksum、source path/import options |
| Base | `REPLACE` | locked | 正規化済み元モーション |
| Body Correction | `COMBINE` | editable | root/COG、spine、limb の差分 |
| Contact | `COMBINE` | editable | feet/hands の pin と IK weight |
| Polish | `COMBINE` | editable | 最終の小さな調整 |

実装規約:

- Action と NLA Strip の Slot を必ず明示する。
- Base は Archive と保存済み mapping/import options から Canonical Rig へ retarget/bake した派生成果物とする。任意 Source Rig の Action を単純複製しない。NLA lock だけを不変性の根拠にせず、Archive checksum/provenance、決定的な `Regenerate Base From Archive`、保存時 validation を持つ。
- quaternion 補正は 4 channel 全てをキー化し、未キー既定値を `(w=1,x=0,y=0,z=0)` とする。
- location の未キー既定値は `(0,0,0)`、scale は `(1,1,1)`。
- quaternion を成分加算する `ADD` は使用しない。
- scene time と action time を同一視せず、NLA tweak の時間変換 API を通す。
- Base Action の FCurve は Graph Editor の通常表示対象から外し、Correction Action のみ表示する。

現実装は1 stackに複数のCorrection=`COMBINE`を保持し、各層をstable UUIDで識別する。active切替、add、rename、duplicate、NLA評価順の上下移動、mute、solo、strip weight、removeを提供し、removeは必ず1層以上を残す。soloはBaseを評価したまま非対象Correctionだけをmuteし、solo中にadd/duplicateした層もその状態を壊さないようmuteで追加する。Before/Afterはper-layerのmute/solo choiceを変更せず全Correction trackをbypass/復元し、active Action binding/NLA Tweak Mode中はBase-onlyを保証できないため切替を拒否する。bake/exportはBefore表示中も一時的に保存済みAfter構成を評価して表示状態を戻す。旧schemaの単層stackは読み込み時に決定的IDとactive metadataを付与し、従来の`correction_action/track/strip`参照はactive層compatibility APIとして維持する。CRUDはBase/Archiveの全FCurveやchecksumを走査しない軽量構造validationを使い、失敗時にbinding/metadataを戻してArchiveとBaseを変更しない。Merge Downはstack全体を層数比例で構造検証し、curveは対象2 Actionだけを走査する。active Action bindingなし、unit influence・configured unmuted・同一time mapping・keyed curveのみ・quaternion WXYZ同時key/正規化・data path property/group非重複・Action/NLA modifierなしの隣接層に限定し、下層UUIDを維持したlossless cloneをtransactionalに行う。重複propertyを評価sampleからflattenするmergeは未実装。stack全体の完全なchecksum/quaternion validationはvisual bake/export境界で実行し、Baseと現在評価される全Correctionの結果を対象とする。Action checksumはv1 digestとの互換性を保ちつつcurve単位でstreaming hashし、全key payloadの一括メモリ化を行わない。

Correction Editはactive stable IDのstripだけを正規のNLA Tweak Modeへ入れ、Action Slotとscene/action time変換を明示する。NLA editorが非選択Objectの選択stripも同時にtweakする仕様を避けるため、view layer全体のNLA選択とactive trackをsnapshotし、対象だけを選択して終了後に復元する。開始前のObject/Bone選択、mode、timeline、editor areaも復元する。キー操作は選択Pose Boneだけを対象に、Quaternion WXYZとHipsだけのLocation XYZを完全な組としてupsert/reset/deleteし、Quaternionを正規化して隣接キーとのhemisphereを揃える。非Hips Location、object location、delta location、scale、Euler等がCorrection Actionへ混入した場合はfinish/validatorで明示拒否する。表示中のPose値はBaseと下位Correctionを含むため、現在poseのkeyはBlender標準`PoseBone.keyframe_insert`を通してNLA COMBINEの逆値変換を行い、最終poseを差分として二重適用しない。UIではHips Location、neutralまでのBlend Frames、Smooth（auto-clamped Bezier）/Linear/Stepped補間を選択できる。

`Key Current`は現在frameと前後のneutralをキー化する。`Apply Preview Range`はcurrent poseをPreview Rangeの両端へ保持キーとして配置し、その外側へneutral easeを置く。全scene timeは個別にAction timeへ変換し、current frameがrange内であることを要求する。NLA逆変換用の一時current keyは読み取り直後に完全復元し、既定では選択propertyのease envelope内にある既存キーだけを削除してから完全channel setを作り直す。全key操作は対象FCurveのco、handle、補間、easing、type、選択状態をsnapshotし、途中失敗時に新規curveも含めてrollbackする。非選択bone、範囲外key、Base/下位Correction/Archiveは変更しない。sampled pointまたはFCurve modifierが対象propertyにある場合は破壊的置換を拒否する。Blender標準UIでTweak Modeだけが先に終了した場合はLayer CRUDをロックし、`Recover Edit Session`でstale snapshotを復元する。addon unregister時も所有sessionをcancelして開始前状態へ戻す。終了時はactive Action/Slot、完全Quaternion、完全Location、finite値、scale不在をCorrection Actionだけ走査して検証し、dense Base/Archiveをhot pathで読まない。

`Bone Correction Envelope`は上記transactionとNLA逆変換を再利用する追加編集モードである。選択Pose Boneに属する選択Correction keyが異なる2frame以上にあればAction最小・最大frameをSceneへ逆変換し、なければTimeline Preview Rangeを使う。UIは専用`ShaderNodeFloatCurve`の`CurveMapping`を時間0..1／補正weight 0..1として表示する。Pose Offsetはcurrent deltaを一度だけcaptureし、各UI更新を必ず開始時snapshotから再構築する。回転はQuaternion principal log/expによる `Exp(weight * Log(delta))`、位置は`delta * weight`とし、Locationの既定許可はHipsだけとする。Curve control pointをseedにweight誤差0.002とQuaternion normalized-linear補間の回転誤差0.1度を満たすまで適応分割し、最大512 samplesのLINEAR完全WXYZ/XYZ keysへ変換する。Applyはschema、kind、Scene/Action range、対象bone、Location bone、正規化時間/weight samples、UI curve pointsをAction metadataへ保存する。Cancel、callback/RNA失敗、unregister、stale edit recoveryはbaselineのco/handle/type/interpolation/easing/selectionと新規FCurveを復元し、SaveおよびUndo/Redo開始前にもactive previewを自動Cancelする。file load後は無効化されたmemory snapshotをclearする。Reweight Existingは選択済み完全property keyだけを同じQuaternion/Location規則で変更し、handle形状を値差分だけ平行移動する。負weightとovershootのcore mathは許容するが、通常UIは0..1 clipとする。

`Rig View`は編集対象のCanonicalと参照用Sourceを視覚的に分離する。Canonical 22 boneだけを役割別のnamespaced Bone Collectionsへ追加し、Hips/root motion、Torso、Head、Left/Right Arm、Left/Right Legの7色を`Bone.color`へ設定する。既存の非Motion Bridge collectionと同名collisionは所有せず、再適用は冪等とする。Canonicalは既定`STICK`か選択可能な`WIRE`/`OCTAHEDRAL`で常時前面、Sourceは非編集の`WIRE` referenceとして背面表示または非表示にする。Blender bone colorには個別alphaがないため、透過は操作中の3D Viewの`bone_wire_alpha`/`xray_alpha_bone`に限定し、Themeや別Windowを変更しない。初回適用時にBone Color/Collection、Canonical/Source表示、Source visibility、Viewport overlayをmemory snapshotし、Source差し替え、明示Restore、unregister時に元へ戻す。file load時は無効RNA snapshotを破棄する。Armature Dataが共有される場合は適用を拒否する。表示schemaをArmature custom propertyへ置くと`use_custom_props=True`のFBXへ混入するため、所有markerはBone Collectionだけに保持する。表示処理はObject/Bone transform、Action/FCurve、Canonical descriptorおよびFBX評価結果を変更してはならない。

`Foot Contact Range` はCorrection Editに入る前の明示ボタン操作でだけ走査する。Canonical `LeftFoot` / `RightFoot` のうち選択した片足のevaluated pose bone tailを現在frame中心に毎frame world space sampleし、接地区間が走査窓の端へ触れる間だけ左右へ適応拡張する。signed height `tail.z - Ground <= Max Height` と、同じheight候補run内部のframe間速度がMax Speed以下であることを接地候補にする。これにより隣接swingの速度を静止区間端へ持ち込まず、床下penetrationも修正対象として検出する。Join Gap以下の一時的な非接地を結合した後にMin Frames未満を除外し、現在frame/subframeを含む一区間だけをTimeline Preview Rangeにcommitする。検出の成否にかかわらず元のframe/subframeとevaluated stateを復元し、失敗時は元のPreview Rangeも保持する。Base、全Correction、Archive、Action/FCurveは読み取りのみで、検出自体はキーを追加しない。一回の走査は重複なし20,000 samplesを上限とし、必要範囲が超過した時点で拒否する。この初期版は範囲検出とPreview Range設定のみで、IK、foot lock、contact marker/packet生成、ground plane solveは実装しない。

### 7.4 Control Rig

MVP は 20〜30 control に限定する。

- root、COG、pelvis
- left/right hand IK、elbow pole
- left/right foot IK、knee pole、toe/heel roll
- chest、head、look target
- global/local space switch

全身の装飾 control、facial control、twist bone 個別 UI は v1 に含めない。Constraint は Blender native を使い、Python が毎フレーム bone 値を書かない。

### 7.5 編集機能

#### MVP

- Mapping preset: Canonical、Mixamo、Unity Humanoid naming、manual。
- Correction Layer の作成、複製、rename、mute、solo、weight、merge、削除。
- 現在 pose の offset key、選択範囲への offset/ease。
- IK/FK hand・foot、pole、world-space pin。
- NLA trim、scale、animated strip time。
- Before/After 比較。
- visual bake、clean、decimate、animation-only export。
- Live Link の connect、pause、send rate、latency/drop 表示。

#### 次段階

- foot contact 区間検出（現在frameを含む片足のPreview Range設定は実装済み）と contact interval marker。
- foot lock、toe/heel roll、pelvis height solve。
- hand/object contact、両手 prop constraint。
- Butterworth / Gaussian smoothing、Euler discontinuity filter。
- trajectory overlay、foot-slide heatmap、joint-limit warning。
- mirror、pose library、breakdown/in-between、motion trail。
- 複数 Target Profile の A/B 切替。

現実装はtransactionalなFBX/glTF/GLB Source Take import、Exact/Mixamo/Auto/Manual preset、22項目Manual mappingのScene永続化とAuto Populate/Clear、保存recipeからのchecksum同一Base regenerate、複数Correctionのactive/add/rename/duplicate/reorder/mute/solo/weight/remove、Correction Editのcomplete quaternion/location key、neutral bracket、補間選択、current reset/delete、Preview Range offset/ease、現在frameを含むFoot Contact Range検出、Before/After、安全条件付きlossless Merge Down、全層のvisual bake、active/corrected Actionのanimation-only FBX export、Live Linkのconnect/pause/resume/send rate/root mode/TCP RTT・受信counter・同期中one-way age表示まで。mapping変更時のCorrection rebase、重複propertyのflatten merge、Control Rig、IK/foot lock/contact packet生成、NLA retime/filter/validatorは次段階に残る。

### 7.6 Live Pose Sampler

イベント handler は dirty flag のみを更新する。

- `frame_change_post`: 再生・スクラブ。
- `depsgraph_update_post`: 同一 frame 内の pose、constraint、action 変更。
- `undo_post` / `redo_post`: 強制再送。
- `load_post`: runtime cache、handler、timer の再構築。
- `bpy.msgbus`: 接続設定など UI/RNA 変更だけ。

`bpy.app.timers` が 30/60 Hz 上限で dirty pose を直ちに flush する。TCP session が Ready/Streaming の間は静止ポーズを保持できるよう、dirty がなくても 5 Hz の pose heartbeat として同じ full pose を新しい sequence で再送する。`pause` は最後の pose を保持、`disconnect` は Unity preview を復元する意味とし、混同しない。

このdirty handler、30/60 Hz cap、5 Hz full-state heartbeat、UIからのpause/resume、`clock-sync-v1`、`pose-stats-v1`、`one-way-pose-age-v1`のcapability交渉は実装済み。Blenderは接続時と5秒ごとにfour-timestamp syncを行い、直近8 samplesの最小RTT sampleを採用する。one-way capability同意中は有効なUnity−Blender offsetを`clock_sync_offset`でUnityへ渡し、sync無効化時は`clock_sync_reset`を送る。Unityはaccepted packetの受信時刻からone-way ageを固定長256 sample windowで集計し、500 msごとの累積counterにoptional p50/p95を追加する。未同期またはsampleがない間はpercentile fieldを省略し、負のageは破棄、5 msを超えるoffset jumpはreset後の再同期を要求する。Blender panelはTCP RTT、counter、同期中のone-way age p50/p95を表示する。

1. `evaluated_depsgraph_get()` を一回だけ取得。
2. Canonical Rig の `evaluated_get()` を取得。
3. 対象 `PoseBone.matrix` を読む。
4. rest-relative global rotation delta、Hips local position delta、trajectory root、stateless contact goal/epoch/ground plane へ正規化。
5. quaternion を normalize し、前 frame との符号連続性を揃える。
6. non-blocking UDP へ一つの full pose packet を送る。

evaluated object を tick 間で保持しない。handler 内で socket 待機、JSON encode、`scene.frame_set()`、Asset import/export を行わない。

### 7.7 Blender thread 規約

Blender Python API は main thread 以外から触らない。

- MVP の UDP send と TCP poll は non-blocking socket を main-thread timer から実行する。
- 送信できないときは pose を queue せず破棄し、次の最新 pose を優先する。
- 複雑な WebSocket、圧縮、reconnect が必要になった場合は Blender 内 thread ではなく別 process bridge を検討する。
- `register()` / `unregister()` は handler、timer、msgbus、socket を対称に解除する。

### 7.8 Blender 性能規約

- 定常 playback は FCurve / NLA / Constraint の native 評価へ寄せる。
- Python から全 bone へ毎 frame 書き込まない。
- 一つの active take だけを評価し、未使用 Action は unlink する。
- Base の密な曲線を Graph Editor に描画しない。
- FCurve 検索結果を `(data_path, array_index)` dictionary に cache し、frame loop で `find()` しない。
- 大量 key 生成は `keyframe_points.add + foreach_set + update` を使う。
- overlay は大量の Empty ではなく GPU draw handler と cache 済み vertex buffer を使う。
- hot path で `bpy.ops` を使わない。

初期 heuristic:

- 50 万 key point 未満: そのまま native Base Action。
- 50〜200 万: Base を非表示・lock、Canonical Rig のみ、active take 一つ。
- 200 万超: 120 秒以下の take に分割する提案を表示する。

Actionのkeyframe/sample pointを集計してこのtierをpanel表示するanalyzerと、`keyframe_points.add + foreach_set`で65 bones・120秒・60 fpsを決定的生成する`benchmark_dense_take.py`は実装済み。Blender 5.0.1 / Apple arm64で1,872,260 keysのnative baselineは生成0.395秒、frame-set mean 0.158 ms / p95 0.208 ms / max 0.821 msだった。Source retarget writerもframe中はtyped arrayへ蓄積し、curveごとに`keyframe_points.add`と`foreach_set("co" / "interpolation")`で一括commitする。同環境の7201 frames / 98 curves / 705,698 keysの一回比較では旧per-key insert 14.150秒、bulk 0.199秒（71.18倍）で、座標bytesとLINEAR補間の一致を確認した。正式条件のlive off/on比較、5反復、4.5 LTS gateは未完であるため、これらの値だけで閾値は確定しない。Keys to Samplesは資産依存のため、計測で改善した場合だけ既定化する。Python sample-cacheで骨を駆動する方式は、native backendが受入性能を満たさなかった場合のfallbackでありMVPには入れない。

## 8. Unity 設計

### 8.1 パッケージ責務

`com.kuko.motionbridge` は次を提供する。

- Motion Bridge EditorWindow。
- TCP control server と UDP pose receiver。
- protocol decoder / validator / latest-only buffer。
- Canonical Source Avatar と Skeleton Descriptor。
- HumanPose converter。
- PoseWriter animation job。
- Edit Mode preview lifecycle。
- Play Mode preview override。
- Character Profile と validation report。
- optional Animation Rigging integration。

現実装ではEditorWindowのTCP serverがCanonical ID/hashとruntime descriptorを検証してUDP endpointを通知し、`ready`後だけUDP poseをpreviewへ渡す。Play開始時はEditor transportを停止してscene上のRuntime Previewが同じport/sessionを引き継ぎ、Blenderはtransient切断を指数backoffで再接続する。Runtimeは共有manual Playable PoseWriterを排他的に所有し、停止時にAnimator設定と全child TRSを復元する。Profile/Bindingとoptional Animation Riggingは未実装。

### 8.2 HumanPose 変換

Transform を Target へ直接コピーしない。Unity は次の一方向 pipeline にする。

1. Blender packet の global rest-relative rotation delta から Canonical joint の local pose を復元する。
2. `jointPaths[0] == ""` を含む joint path 配列と Source Avatar から、unbound `HumanPoseHandler(avatar, jointPaths)` を一度だけ生成する。
3. `NativeArray<float>` に各 joint の local position 3、local quaternion 4 を格納する。Hips だけ `restLocalPosition + hipsLocalPositionDelta`、その他は Canonical rest 値、joint 0 は descriptor の FBX root rest transform `R` を使い packet rotation delta は identity とする。packet の trajectory root を HumanPoseHandler 入力へ混ぜない。
4. `SetInternalAvatarPose(avatarPose)` を呼ぶ。
5. `GetInternalHumanPose(ref humanPose)` で normalized HumanPose を取得する。
6. preallocated job buffer へ `bodyPosition`、`bodyRotation`、全 muscle をコピーする。

`SetInternalAvatarPose` は root Transform 版 constructor の Handler では動作しないため、必ず `jointPaths` 版を使う。

`HumanPose.bodyPosition` は Source Avatar の human scale で正規化されている。PoseWriter では次を使う。

```text
target bodyLocalPosition = humanPose.bodyPosition * target humanStream.humanScale
target bodyLocalRotation = humanPose.bodyRotation
```

muscle 数や名称を定数化しない。`HumanTrait.MuscleName[i]` と `MuscleHandle.name` から name→handle の bijection table を session 初期化時に構築し、欠落/重複を hard error にする。`GetMuscleHandles()` の返却順を `HumanPose.muscles[i]` と同一だと仮定しない。全 muscle の往復 mapping を Phase 0 で検証し、muscle name hash を handshake capability と一致させる。

HumanPose の `bodyPosition/bodyRotation` は Hips/COG channel、packet の root position/rotation は locomotion trajectory channel であり別物とする。Hips translation は専用 `hipsLocalPositionDelta` から生成する。trajectory は session 開始時に保存した target world anchor へ一度だけ適用する。

### 8.3 単一 writer 原則

同じ Animator hierarchy を `SetHumanPose`、Animator Controller、独立 PlayableGraph、RigBuilder が同時に書かない。

- Core の最終書込みは PoseWriter `IAnimationJob` 一つ。
- Animation Rigging を使わない構成では PoseWriter を `AnimationScriptPlayable` から一つの `AnimationPlayableOutput` へ接続する。
- Phase 0/1 Core は Animation Rigging 非依存で、PoseWriter だけを評価する。
- Phase 2 の Animation Rigging integration は、事前に authoring された `MotionBridgeRigBinding` がある場合だけ同じ所有 graph に contact pass を追加する。preview 中に hierarchy、RigBuilder、Constraint を動的追加して Scene/Prefab を変更しない。
- 別 PlayableGraph と RigBuilder を同じ Animator に同時出力しない。
- target-bound `HumanPoseHandler.SetHumanPose` は診断 spike だけに限定し、製品 hot path では使わない。

PoseWriter の処理順:

1. human stream validity を確認。
2. stance pose へ reset。
3. 全 muscle を書く。
4. body local position / rotation を書く。
5. 後段の contact rig が feet/hands/pelvis を補正する。

### 8.4 Edit Mode Preview

実際にシーンへ配置された Target GameObject を専用 `MotionBridgeAnimationModeDriver` の `AnimationMode` で preview する。他の AnimationMode driver が active なら開始を拒否し、所有権を奪わない。

開始時:

- Animator、controller、enabled、cullingMode、applyRootMotion の状態を保存する。
- `cullingMode = AlwaysAnimate`、`applyRootMotion = false` に一時変更する。
- manual Core PlayableGraph を構築する。Phase 2 で有効な Rig Binding がある場合だけ、その binding が所有する optional contact graph を追加する。
- `AnimationMode.StartAnimationMode(driver)` を開始する。

`EditorApplication.update` ごとに latest frame を一つだけ取り込み、`timeSinceStartup` で最大 60 Hz に制限する。

```text
AnimationMode.BeginSampling
try:
    optionalRigBinding.Sync()
    AnimationMode.SamplePlayableGraph(target, graph, outputIndex, time)
finally:
    AnimationMode.EndSampling
EditorApplication.QueuePlayerLoopUpdate
SceneView.RepaintAll
```

終了、接続切断、assembly reload、play mode 遷移、対象削除、例外時には必ず:

- 所有 driver の `StopAnimationMode`（二重実行しても安全）
- graph destroy
- optional Rig Binding の一時 runtime state clear
- NativeArray / HumanPoseHandler dispose
- socket / cancellation 停止
- Animator の保存状態を復元

初期化は「状態保存 → resource 作成 → StartAnimationMode」の各段階を記録し、途中失敗でも作成済み resource だけを逆順 cleanup する。stop/disconnect/sample 例外/assembly reload/play 遷移/target 削除/window close の全経路を同じ冪等 `DisposeSession()` に集約する。`EndSampling` と `StopAnimationMode` による復元を利用し、preview 結果を Scene/Prefab へ保存しない。`Undo` や SerializedObject で毎 frame 書き込まない。

### 8.5 Play Mode Preview

v1 は `Preview Override` モードだけを提供する。

- socket receive / parse は background I/O。
- Unity API、HumanPoseHandler、PlayableGraph は main thread だけ。
- `Update` で latest pose を preallocated buffer へ一回コピーする。
- animation pass で PoseWriter → optional Rig constraints の順に一回評価する。
- gameplay Animator Controller との合成は行わず、preview 中は一時的に exclusive ownership を取る。
- `Animator.applyRootMotion` は false。Motion Bridge の root と二重適用しない。
- Play Mode 終了でも I/O task は自動終了しない前提で cancellation と cleanup を実行する。

### 8.6 Target Character Profile

Profile は純粋な数値/asset identity を持つ `ScriptableObject` で、Target asset を改変しない。scene object 参照は `MotionBridgeRigBinding` Component に分離する。

```text
profileId
avatarGuid / skeleton signature
groundOffset
left/right foot sole offset
toe pivot offset
pelvisHeightMin/Max
kneePoleDirection
elbowPoleDirection
jointLimitOverrides
contactWeightScale
rootMode: InPlace | WorldRoot
```

```text
MotionBridgeRigBinding (scene Component)
target Animator
optional RigBuilder / contact constraints
left/right hand socket Transform
optional prop Transform
left/right foot/hand effector Transform
```

自動生成後にユーザーが調整できる。profile がなくても Mecanim retarget は可能だが、contact correction は conservative default にする。

### 8.7 破綻防止

limit-aware solver として順序を固定し、IK 後の単純 clamp で接触を壊さない。

1. Source pose → HumanPose 正規化。
2. Target Avatar へ muscle pose を適用。
3. root mode と humanScale を適用。
4. pelvis + 両脚を reach/関節 limit 内で反復 solve（最大 4 iteration）。
5. support feet の contact goal を同じ反復内で解く。
6. hand IK / prop contact を limit-aware solve。
7. aim / twist stabilization。
8. NaN、reach、limit、contact error を validation。許容外は weight を安全に下げ、警告する。

v1 のサポート envelope は target/source humanScale 比 `0.5〜2.0`、脚長・腕長・胴長比 `0.65〜1.55`、非一様 scale なしとする。外側は接続前 warning または unsupported。接触区間の合格値は foot slide p95 `2 cm` 以下、ground penetration p95 `1 cm` 以下、reach `98%` 以下、膝/肘反転 0 件とし、相対改善率だけでは合格にしない。

Hard error:

- Avatar invalid / non-human。
- required bone 欠落。
- skeleton hash、protocol major、joint count 不一致。
- NaN / Infinity、ゼロ quaternion、packet length 不正。
- 非一様・負 scale。

Warning:

- optional bone 欠落。
- foot reach が leg length の 98% 超。
- knee/elbow bend が前 frame と反転。
- foot slide、ground penetration、joint limit 超過。
- TCP alive なのに pose heartbeat が 500 ms 以上ない。

## 9. Root Motion

モードは handshake と UI の両方で明示する。

- `InPlace`: trajectory root の水平 translation と yaw を適用しない。上下 COG は HumanPose の範囲で維持。
- `WorldRoot`: session 開始時の target world position/rotation を anchor とし、packet の trajectory delta を一度だけ target root に適用する。`Animator.applyRootMotion=false` を強制する。

Trajectory root は Blender session 開始時に固定した SessionAnchor からの meter 単位 delta として送る。Hips local position delta は Canonical Hips parent space の meter 単位で HumanPoseHandler へ入れ、生成された bodyPosition は source humanScale で正規化され、PoseWriter 側で target humanScale を掛ける。trajectory root を HumanPoseHandler へ入れない。Hips 以外の bone translation は v1 では送らず、骨長を Target Avatar に保持させる。

## 10. データ永続化

### Blender `.blend`

- Canonical Rig UUID、schema version、skeleton hash。
- Source Take provenance（resolved filepath/hash/format/Armature/Action/frame range/bone hierarchy/bind pose）、instance UUID、明示Action Slot、保護`SOURCE_ARCHIVE` Action参照/checksum。
- 確定mapping/frame range/algorithm/Source・Canonical identityを持つretarget recipe。
- Base / Correction Action参照、Correction stable ID、active/mute/solo/order/weight metadata。
- NLA layer metadata。
- contact markers、Control Rig 設定。
- Unity host/port は保存可。ただし自動接続しない。

### Unity

- Project Settings: listener port、send rate、logging、loopback-only。
- Character Profile assets。
- Scene/Prefab へ live pose を保存しない。
- Session runtime state は domain reload で破棄・再構築する。

## 11. エクスポート

- `bpy.ops.nla.bake` 相当の visual keying を Data API/Operator adapter 経由で明示実行する。
- bake 対象は Canonical output bones のみ。
- quaternion 連続性を整えた後、角度誤差と effector 位置誤差で key reduction する。
- Constraint は export せず、結果だけを bake する。
- FBX exporter は一つの baked Action だけを選択し、`All Actions` を無効にする。
- `Export Active Action FBX` operator は Canonical skeleton ID と active baked Action を必須とし、選択、mode、active Action、timeline、current frame を成功/例外の両経路で復元する。
- mesh、material、camera、light は export しない。
- Unity importer は Humanoid、Canonical Avatar から Copy Avatar、必要に応じ loop 設定。

後段の Unity-only contact correction は Blender FBX に含まれない。pre-IK の一致は Blender live と imported FBX を直接比較する。post-IK の一致は imported clip に同じ Character Profile/Rig Binding を併用して比較する。単体 asset に同じ補正を必要とする場合は、将来の target-specific Unity bake で `.anim` を生成する。

## 12. UI

### Blender N-panel: Motion Bridge

- Session: connection、latency、drop、send Hz、root mode。
- Take: import、mapping、normalize、active take、retime。
- Layers: Base regenerate、add/duplicate/mute/solo/weight/merge、active Correction Edit、選択bone key、neutral bracket。
- Controls: IK/FK、space、pin、pole、key current pose。
- Contacts: Left/Right footの現在接地range検出とPreview Range設定（実装済み）、interval marker、lock、ground plane。
- Validate: errors、warnings、jump to frame、overlay。
- Export: bake range、fps、error tolerance、FBX preset。

### Unity EditorWindow

- Listener: start/stop、address、ports、protocol status。
- Target: Animator、Avatar status、profile、root mode。
- Preview: start/pause/stop、Edit/Play ownership 状態。
- Diagnostics: sequence、age、drop、decode error、HumanPose error、frame cost。
- Validation: hard errors、warnings、bone mapping、profile mismatch。

## 13. パッケージ構造

```text
blender_addon/
  blender_manifest.toml
  motion_bridge/
    __init__.py
    compat/action_api.py
    io/source_take.py
    io/source_import.py
    rig/canonical.py
    rig/control_rig.py
    retarget/mapper.py
    editing/correction_layers.py
    edit/foot_lock.py
    edit/filters.py
    live/dirty_tracker.py
    live/pose_sampler.py
    live/control_channel.py
    live/pose_udp.py
    bake/baker.py
    export/fbx_export.py
    ui/operators.py
    ui/panels.py

verification/unity-6000.2/
  Assets/
  Packages/
    manifest.json
    packages-lock.json
    com.kuko.motionbridge/
      package.json
      Runtime/
        Protocol/
        Transport/
        Retargeting/
      Editor/
        Window/
        Preview/
      Tests/
        Runtime/
        Editor/
        PlayMode/
  ProjectSettings/

protocol/
  schema/
  golden/
  test_vectors/
```

## 14. 性能予算

基準データは 65 bones、120 秒、60 fps、全 frame quaternion key、約 190 万 key point とする。

| 指標 | 合格条件 |
|---|---:|
| Blender live-link 追加負荷 | p95 2 ms/frame 以下 |
| Blender playback 低下 | addon off 比 10% 以下 |
| Blender UI stall | 50 ms 超を定常発生させない |
| End-to-end pose latency | localhost p95 50 ms 以下 |
| Unity receiver backlog | queue なし、reader-owned 3 slot 固定 |
| Unity hot-path allocation | warm-up 後 0 B/frame |
| Unity pose+retarget cost | p95 2 ms/frame 以下、IK 別計測 |
| UDP packet | 1200 bytes 以下 |
| Disconnect recovery | UI freeze なし、1 秒以内に操作可能 |

性能値は Apple Silicon 16 GB 級を最初の reference machine とし、CPU/GPU/scene complexity を計測ログへ記録する。

## 15. セキュリティと堅牢性

- 既定は `127.0.0.1` bind。LAN bind は明示 opt-in。
- protocol magic、major/minor、payload length、joint count、session ID を検証する。
- TCP frame は 64 KiB、descriptor は 64 joints、PosePacket は 60 joints、UDP は 1200 bytes を上限とする。
- 全 float の finite、quaternion norm、index、parent hierarchy を検証する。
- 不正 packet は捨て、同じ理由を rate-limit して log する。
- UDP sequence が古い packet は適用しない。
- connection loss 時は最後の pose を保持せず、preview を停止して元状態へ戻す。
- Blender Extension 有効化だけでは network 接続しない。

## 16. 公式 API 根拠

Blender:

- [Slotted Actions migration](https://developer.blender.org/docs/release_notes/4.4/upgrading/slotted_actions/)
- [Actions](https://docs.blender.org/manual/en/4.5/animation/actions.html)
- [NLA COMBINE](https://docs.blender.org/manual/en/4.5/editors/nla/sidebar.html)
- [Handlers](https://docs.blender.org/api/4.5/bpy.app.handlers.html)
- [Timers](https://docs.blender.org/api/4.5/bpy.app.timers.html)
- [PoseBone.matrix](https://docs.blender.org/api/4.5/bpy.types.PoseBone.html)
- [Python thread constraints](https://docs.blender.org/api/4.5/info_gotchas_threading.html)
- [FCurve bulk key insertion](https://docs.blender.org/api/4.5/bpy.types.FCurveKeyframePoints.html)
- [FBX importer](https://developer.blender.org/docs/release_notes/4.5/pipeline_assets_io/)

Unity:

- [HumanPoseHandler constructors](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/HumanPoseHandler-ctor.html)
- [SetInternalAvatarPose](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/HumanPoseHandler.SetInternalAvatarPose.html)
- [GetInternalHumanPose](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/HumanPoseHandler.GetInternalHumanPose.html)
- [HumanPose](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/HumanPose.html)
- [AnimationHumanStream](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/Animations.AnimationHumanStream.html)
- [AnimationScriptPlayable](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/Animations.AnimationScriptPlayable.html)
- [AnimationMode.SamplePlayableGraph](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/AnimationMode.SamplePlayableGraph.html)
- [Animation Rigging workflow](https://docs.unity3d.com/Packages/com.unity.animation.rigging@1.4/manual/RiggingWorkflow.html)
- [EditorApplication.update](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/EditorApplication-update.html)
- [HumanTrait](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/HumanTrait.html)
