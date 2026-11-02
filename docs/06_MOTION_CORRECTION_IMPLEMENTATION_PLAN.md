# モーション補正機能 調査・実装計画

調査日: 2026-09-03

対象: Blender 4.5 LTS / 5.0系、Unity 6000.2、Humanoid motion

## 1. 結論

現行実装は作り直す段階ではない。Source原本をArchiveとして保護し、Canonical BaseをNLA `REPLACE`、複数Correctionを`COMBINE`で重ね、補正結果だけをvisual bake/exportする基盤は、MotionBuilder系の非破壊編集を載せる土台として妥当である。

製品目標は「ボーンを一つずつ回す補正」から「問題を確認し、ワンクリックで直し、必要なら手足のeffectorを置いて仕上げる補正」へ進めることとする。MotionBuilder完全互換を目指すのではなく、次の順序で価値と安全性を積み上げる。2026-09-03時点で1〜4の中核と関節可動域補正まで実装し、5以降とkey reductionを次段階とする。

1. 現行契約・データ所有・Undo・Source Take選択の不整合を解消する。
2. 共通sample、Correction Writer、Motion Validatorで補正の書込みと品質判定を一元化する。
3. Quaternion-safe filterを追加し、続いて誤差基準key reductionを追加する。
4. 現在の接地区間検出を、全区間のFoot Lockと関節可動域補正へ発展させる。
5. Foot/Hand IK、pole、T/R pinを持つ軽量Control Rigを追加する。
6. 体格差補償と同時に明示rebaseを提供し、Unity側optional post-retarget contactへ進む。
7. versioned time-mapを導入した後にPose Asset連携とNLA clip matching/timewarpを追加する。

最初からFull Body HumanIK互換solver、独自Story editor、独自Pose Libraryを作らない。Blender native NLA、Pose Asset、constraints、Graph Editorを使い、Motion Bridge固有実装はHumanoid semantics、world-space誤差、非破壊Correctionへの確定処理に集中する。

製品コンセプトは「初心者が迷わず、元モーションを壊さず、すぐ結果を確認できる」を最優先とする。内部が高度になっても、通常画面にsolver、quaternion、NLA、Profile、time-map等の実装用語を露出しない。Beginner UIを標準とし、Advanced UIは同じcore APIの詳細設定を段階表示する。

## 2. 調査時点の実装

### 2.1 実装済み

| 領域 | 現状 | 根拠 |
|---|---|---|
| Source取込 | FBX/glTF/GLBをtransactional importし、複数Actionをstable take IDで明示選択して原Actionを`SOURCE_ARCHIVE`として保存 | `blender_addon/motion_bridge/io/source_import.py` |
| Retarget | Exact/Mixamo/Auto/22項目Manual mapping、Canonicalへのdense one-time bake | `blender_addon/motion_bridge/retargeting/presets.py:32-90`、`blender_addon/motion_bridge/retargeting/bake.py:25-228` |
| 再生成 | 保存recipeから候補Baseを作り、checksum一致時だけ原子的に交換 | `blender_addon/motion_bridge/retargeting/regenerate.py:711-959` |
| 非破壊Layer | Base=`REPLACE`、stable UUID付き複数Correction=`COMBINE`、CRUD/mute/solo/weight | `blender_addon/motion_bridge/editing/correction_layers.py:541-749,833-1365` |
| Correction Edit | 専用NLA Tweak Mode、scene/action time変換、選択boneの完全WXYZ、HipsだけのXYZ、neutral bracket | `blender_addon/motion_bridge/editing/correction_edit.py` |
| Envelope | Pose offset/reweight、CurveMapping preview、quaternion log/exp、Apply/Cancel rollback、最大512点の適応sample | `blender_addon/motion_bridge/editing/correction_envelope.py:41-49,518-678,848-1024,1111-1438` |
| 表示 | 役割別色、Bone Collections、Source wire reference | `blender_addon/motion_bridge/editing/rig_visualization.py` |
| 接地 / Foot Lock | Left/Right Footのworld tailを高さ・速度で分類し、Preview Range設定または全接地区間のworld anchor固定を行う。解析2-bone IK、左右同時solve、到達不能clamp、前後neutral、実effector再検証を実装 | `blender_addon/motion_bridge/editing/foot_contact.py`、`blender_addon/motion_bridge/foot_lock.py`、`blender_addon/motion_bridge/editing/auto_correction.py` |
| 共通sample | immutableな`MotionSequence` / `MotionFrame`に22本のbone local/global WXYZ、world head/tail、Armature Root TRS、左右Foot effector、key densityをevaluated poseから取得 | `blender_addon/motion_bridge/editing/motion_sample.py`、`blender_addon/motion_bridge/motion_quality.py` |
| Motion Validator | 10種のquality metric、最大4,096のissue区間、3段階判定、canonical JSON / SHA-256。Beginnerは「モーションを確認」と結果・区間だけを表示し、数値とJSONはAdvancedに分離。Preview Range優先と長尺の1クリック短縮を`RangeSpec`で統一。sampling/serializationはmain threadのmodal chunk、pure analysisは隔離Python subprocessで実行し、入力transport SHA-256、32 MiB output、再試行可能なcancel/reapで境界を固定 | `blender_addon/motion_bridge/range_spec.py`、`blender_addon/motion_bridge/motion_quality.py`、`blender_addon/motion_bridge/motion_quality_process.py`、`blender_addon/motion_bridge/addon.py` |
| Correction Writer | Blender main threadだけで実行し、対象channelのconstraint/driverと有効な上位layerを持つ中間active layerを拒否する。最上位の正規化したactive Correctionにbone WXYZ / Hips XYZ / Object Root XYZ・WXYZをNLA Tweakの逆評価経由で書き、Action・Object・boneを一括rollbackしてevaluated local/world transformを検証 | `blender_addon/motion_bridge/editing/correction_writer.py` |
| 揺れ除去 | 不等間隔sample対応の局所一次Gaussian filterをQuaternion Log/Exp空間で適用。弱め／標準／強め、Root/Hips移動保護、範囲halo、変更boneだけの書込みを実装 | `blender_addon/motion_bridge/motion_filter.py`、`blender_addon/motion_bridge/editing/auto_correction.py` |
| 関節可動域 | versioned `SolveProfile`、signed X/Z swing、twist、soft zone、肘・膝の非対称hinge/横曲げ制限を実装 | `blender_addon/motion_bridge/joint_limits.py` |
| ワンクリック補正 | Beginner UIに3つの大ボタンを安全順で表示。各操作は新規Correctionへ保存し、no-opではlayerを作らず、失敗時はlayer/Action/active/revisionをrollback | `blender_addon/motion_bridge/editing/auto_correction.py`、`blender_addon/motion_bridge/addon.py` |
| 比較・統合 | Before/After、非重複propertyに限るlossless Merge Down | `blender_addon/motion_bridge/editing/comparison.py:151-217`、`blender_addon/motion_bridge/editing/layer_merge.py:468-641` |
| 確定 | Base+Correction visual bake、quaternion continuity、armature-only FBX export | `blender_addon/motion_bridge/editing/bake.py:125-321`、`blender_addon/motion_bridge/io/fbx_export.py:146-170` |
| Live preview | Blender evaluated poseをTCP/UDPでUnity HumanPose/単一Playable writerへ反映 | `blender_addon/motion_bridge/live/pose_session.py`、Unity `Runtime/Retargeting` / `Runtime/Transport` |

### 2.2 現在の到達点

Foot ContactのPreview Range検出に加え、全接地区間を対象にするCanonical Foot Lockを実装した。現在のFoot Lockは足位置をworld anchorへ固定する解析2-bone IKで、足のglobal回転を保持する。Root/Hips移動、toe roll、Hand effector、pole、IK/FK切替、T/R pin、contact情報のLive Link送信、pose mirror、timewarpは未実装である。手動修正は引き続きCanonical deform boneを直接回転し、必要な範囲へEnvelopeで配分できる。

Canonical skeletonはsynthetic Rootと22 body bonesで、finger、face、twist chainを含まない。現Auto mappingは既知名の正規化一致が中心で、体格差、ankle/sole offset、preferred bend plane、twist distributionをProfileとして扱わない。

### 2.3 調査時の検証結果

- Pure Python: 98 tests実行、97 pass、loopback bind 1件skip。
- Protocol constants: 12 checks pass。
- Blender 5.0.1 background: Correction Edit、Envelope core/UI/acceptance、Foot Contact Range、Layer、Merge、Before/After、FBX、Base regenerateを含む関連11 scripts pass。
- `git diff --check`とPython compile checkはpass。
- Blender 4.5 LTS、Unityの全EditMode/PlayMode、長時間cross-process品質E2Eは今回未実行。

調査開始時点で7ファイルに既存の未commit変更があった。主内容は日本語UI、保存後に残ったNLA Tweak Modeのrecovery、FBX周辺調整である。この計画書はそれらを現行作業として尊重し、変更していない。

### 2.4 Phase 0実装状況（2026-09-03）

本計画のPhase 0を実装した。未完成の`contacts-v1`交渉停止、EnvelopeのScene単位ownership、Hips-only Location、`EASE_OUT`表示、Source別Manual profile、複数Action pickerとstable take ID、所有外NLA/Actionの非破壊block、Full Copyで共有された初回Base/Correctionの変更前block、Beginner/Advanced共通state、compatibility/corpus manifest、統一test runnerを追加した。

Beginner UIは既定のAuto mappingで「Source選択 → Bake → Correction作成 → 編集・記録 → Before/After」まで進み、Auto認識できない場合だけ不足boneの選択欄を表示する。想定外のactive Actionは削除せず切り離して復旧できる。

統一runnerは26/26 checks（Pure Python 114/114、protocol constants、compile、ruff、Blender 5.0.1の22 scripts）をsandbox外でpassした。sandbox内ではloopback bind 1件だけskipし、Pure Pythonは113/114 passする。Unity 6000.2 EditModeは隔離copyで82/82 passした。Source取込、Source→Canonical Bake、corrected visual bakeはframe callbackやcommit callbackで対象RNAが無効になった失敗経路でも、transaction所有のAction/Archive/imported Armature dataとSession/Rig Viewをrollbackし、callbackが作成した所有外IDは保持する。Correction作成・Manual Populate/Clear・Bakeを各10往復するsemantic checkpoint Undo/Redoもpassした。起動中`--python`ではoperatorの自動Undo stepが生成されないため、この自動試験はoperatorの`UNDO`宣言と前後checkpoint間の状態復元を分けて検証している。実UIイベントが自動で1 Undo stepを作ることは手動受入項目として残す。

M0出荷判定として残るのはBlender 4.5 LTSでの同一回帰と、実UIの自動Undo境界smokeである。その後、Phase 1の第一縦切りとしてMotion Validator、Beginner UI、RangeSpec、bone/Hips/Root用Correction Writerを追加し、さらにQuaternion-safe Filter、Foot Lock、signed joint-limit `SolveProfile`を同じWriterへ接続した。Control Rigは未実装のままである。

### 2.5 Phase 1第一縦切りの実装状況（2026-09-03）

evaluated poseをimmutableな共通sampleへ変換するアダプターを追加した。各frameはCanonical 22 bonesのlocal/global WXYZ、world-space head/tail、Armature objectのRoot location/rotation/scale、左右Foot effector、Action/NLAのkey countを持つ。入力が同じならcanonical JSONとSHA-256も同じになる。

Motion Validatorはquaternion unit/sign、bone角速度/角加速度、Root移動/回転不連続、foot slide、ground penetration、limb reach、key densityの10 metricを判定する。Beginner UIには「モーションを確認」の1操作と「問題なし／確認推奨／修正推奨」、該当区間を表示し、metric数値とJSON copyはAdvancedだけに置いた。表示は先頭3区間に絞るが総件数を保持し、内部worker/pathは見せず、日本語の原因と復旧操作だけを返す。品質確認は`RangeSpec`でPreview Rangeをtake全体より優先し、20,000 frameを超えるtakeは現在位置付近へ1クリックで短縮する。問題区間を表示して範囲だけ変えた場合は前回一覧を保持し、未確認範囲であることを明示する。長尺確認はevaluated pose取得と解析入力serializationをBlender main thread上の8 ms目安のmodal chunkに分け、pure Python解析だけを隔離Python subprocessで実行する。`bpy`へはmain threadからだけアクセスし、中止、kill/reapと一時directory削除の再試行、save/undo/load前の状態復元、確認中の入力変更によるstale結果の破棄を実装した。入力streamは親子間でtransport SHA-256を照合し、outputは32 MiB、metricは64、issue区間は4,096を上限とする。

Correction WriterはBlender main threadからの呼出しだけを許可し、対象channelにconstraintまたはdriverがある書込みと、有効な上位layerを持つ中間active layerへの曖昧な書込みを明示的に拒否する。他layerを含む実評価からNLA `COMBINE`の逆評価を利用し、最上位の有効なactive Correctionへbone Quaternion WXYZ、Hips Location XYZ、Armature Object Rootのworld-space Location XYZ / Rotation WXYZを完全keyまたは要求propertyだけの部分keyとして書く。Rootはparent、全Object constraint/transform driver、delta transform、非unit scale、Euler回転をv1安全条件として拒否し、作成後もstack validatorが同条件、quaternion hemisphere、curve/group Mute、modifier/sample化を検出する。BaseはNLA stripの評価Action Slotだけを調べ、constant unit-scale keyを許可する。成功時はmotion revisionを進め、以前の品質確認結果をstaleにする。key中だけでなくCorrection Edit exit後の検証で失敗しても対象curve、Action Group、Object/bone transform、Blender作業状態とrevisionをrollbackし、書込み後のevaluated local/world transform一致と、Base、Archive、下位layer、非対象bone/rangeが変わらないことを検証した。

最新の統一runnerはsandbox外で31/31 checksをpassした。内訳はPure Python 212/212、protocol constants、compile、ruff、Blender 5.0.1の27 scriptsである。Motion Quality固有はcore 22/22、process safety 22/22、RangeSpec 11/11で、sampling中と解析中の実`.blend`再読込によるcancel/ownership解放も含む。sandbox内だけはloopback bind 1件がenvironment skipとなり211件がpassする。Unity 6000.2 EditMode 82/82はPhase 0時点の結果で、今回のBlender自動補正追加後には再実行していない。Blender 4.5 LTSと、実UI event loop上のactual timer/Undo step smokeは未完である。

`tools/benchmark_motion_quality_process.py`による7,201 frames × 22 bonesの別プロセス経路では、最新runで25.795 MiBの入力serializationが0.286 s、解析が7.66 sで完了し、processと一時directoryは残らなかった。chunk median/p95/maxは8.02/8.04/8.04 ms、10 ms超と50 ms超はいずれも0、解析poll最大は2.41 msだった。8 msはUI responsiveness用のsoft budgetでありhard deadlineとは扱わない。

```sh
/Applications/Blender.app/Contents/Resources/5.0/python/bin/python3.11 \
  tools/benchmark_motion_quality_process.py
```

### 2.6 ワンクリック補正の実装状況（2026-09-03）

Beginner UIに「揺れをなめらかに」「関節の曲がりすぎを直す」「足滑りを固定」の3操作を追加した。推奨順をそのまま上から並べ、通常はボタンだけ、強さ・左右・接地条件は折りたたんだ詳細設定に置く。Preview Rangeがあればその範囲、なければtake全体を対象にし、同期処理は最大600 frameとする。Foot Lock後に脚回転を再度変えないよう、Foot Lockは最後に実行する。

3操作は共通のimmutable sampleと`CorrectionWriter`を使い、毎回新しい最上位Correctionへ変更boneだけを書き込む。Base、Source Archive、下位Correction、Root/Hips移動、指定範囲外は変更しない。範囲境界は外側sampleをhaloとして解き、隣接frameにneutral keyを置く。変更や接地区間がなければlayerを作らず、書込みまたは事後検証が失敗した場合は新規layer、Action、active layer、motion revisionを一括rollbackする。

揺れ除去は不等間隔sampleに対応する局所一次Gaussian filterをQuaternion Log/Exp空間で適用し、弱め／標準／強めの3 presetだけを標準UIへ出す。関節補正はversioned `SolveProfile`のsigned X/Z swing、twist、soft zoneを使い、肘・膝の逆曲げと横曲げを非対称範囲へ戻す。Foot Lockは接地を高さ・速度で分類して区間anchorを作り、左右の解析2-bone IKを同時solveし、足のglobal回転を保持する。到達不能targetは脚長内へclampし、実際のevaluated foot位置を2 mm以内で再検証する。

synthetic受入ではFoot slideを2 cmから0.071 mmへ低減し、179°の膝曲げをprofile上限10°へ補正した。Preview Range外不変、Base/Archive保護、no-op、事後検証失敗時の完全rollback、UI登録と安全な表示順も確認した。600 frameの揺れ除去は変更1 boneで6.1282秒、22 bones書込み相当で29.5564秒だった。長尺の非同期化、selected-key範囲、toe/sole profile、pelvis補償、marker編集、Control Rigは次段階に残る。

## 3. 調査で特定し、Phase 0で解消したリスク

### P0-A. `contacts-v1`が実装完了前に交渉される

Blender clientは`contacts-v1`を常時offerし、Unity serverも常時acceptする一方、`LivePoseSession.tick()`はcontact flag/weight/epoch/anchorを渡していない。

- 根拠: `live/control_client.py:35-41`
- 根拠: `live/pose_session.py:102-120`
- 根拠: Unity `Runtime/Transport/ControlServer.cs:738-746`

capabilityの意味を「wire fieldをdecodeできる」ではなく「producerからconsumerまで機能する」と定義し直す。実装完了まではoffer/acceptを止めるか、`contact-goals-v1`と`contact-rig-v1`へ分割する。既存packetはcontact blockを固定長で持つため、現在のsemanticsを守る限りpacket major変更は不要である。

### P0-B. Envelope用NodeTreeの所有権が弱い

`addon.py`のEnvelope curve dataは固定名をglobal検索し、同名NodeTree/nodeを再利用・削除する経路がある。ユーザーが偶然同名dataを持つ場合の衝突と、Scene間共有の危険がある。

対策はper-scene UUID、`motion_bridge_owned`、schema/version、owner scene IDを全て検証し、所有不明dataは絶対に変更しないこと。削除は参照数とownershipを確認した自分のdataだけに限定する。

### P0-C. Location補正の仕様がUIとcoreで一致しない

UIは「Hipsのみ」と表示し、初心者UIもHips単独へ制限するが、Advanced経路と`insert/reset/remove_correction_pose_keys()`は選択した全boneへXYZを書ける。EnvelopeだけがHips allowlistを持つ。

core APIの既定をHips-onlyに統一し、将来のControl Rig内部利用だけ明示allowlistを渡す。UIだけの制限を安全境界にしない。

### P0-D. 表示と処理の不一致

`EASE_OUT`は現在0→1へ増える関数だが、日本語表示は「徐々に弱く」となっている。presetの意味を「補正weight」として固定し、label、tooltip、thumbnail、unit testを同じtruth tableから生成する。

### P0-E. NLA共存制約とUndo境界

v1 stackは作成時に所有外NLA trackがあると拒否し、validatorも所有外trackを破損扱いにする。これは安全だが、一般的なBlender NLA/Story相当workflowとは共存できない。短期は所有外trackを「破損」ではなく「unsupported coexistence」として非破壊にhard blockし、Canonical専用workfileの制約をUIで明示する。長期はowned track IDとversioned time-mapでMotion Bridge trackだけを管理する。

Correction layer作成、Source→Canonical bake、Manual mapping populate/clearは内部rollbackを持つが、Blender operatorの`UNDO`境界が統一されていない。Action/NLA datablockを含むUndo/Redo integration testを先に作り、安全なoperatorだけ`UNDO`を有効化する。

### P0-F. Manual mappingがSourceに紐付かない

22項目mappingはScene-wide propertyで、Source identityやProfile assetに紐付かない。Source差替え時に古いmappingを誤利用できる。Phase 0でsource hierarchy/bind fingerprint付き`SourceMappingProfile`へ移し、Sceneは選択中Profile参照だけを持つ。Correctionが存在するstackのmapping変更は、明示rebaseを同時提供するPhase 5までhard blockする。

### P0-G. 複数ActionからPrimary Takeを暗黙選択する

importは新規Armatureを1体に限定する一方、usable Actionが複数ある場合は先頭候補をPrimary Takeにする。Archiveとprovenanceの対象を誤るため、Phase 0で候補が一意なら自動選択し、複数ならtake pickerを表示、headless/APIでは候補一覧付きhard errorにする。

- 根拠: `io/source_import.py:430-458`

### P0-H. Scene Full CopyでActionが共有される

BlenderのScene Full CopyはObjectやNLA bindingを複製しても、Base/Correction Actionを共有する場合がある。片側からActionのkey、名前、mute/solo metadata、保護状態を変更すると、もう片側も変わり、削除や再生成では既存stackを破損し得る。

Phase 0では、初回Correction作成時にactive Base Actionの外部consumerを検出し、作成済みstackでは全Correction Actionのconsumerを確認する。共有中はAction、metadata、NLA、Envelopeを変更する前にhard blockし、自動でsingle-user化しない。Beginner UIは実装用語を出さず、不要な複製Sceneを削除してから続ける復旧方法を示す。複製側を独立編集する明示的なcopy-on-write操作は、stack全体のIDとrecipeを原子的に複製できる段階で追加する。

## 4. MotionBuilderとの比較

| MotionBuilder系能力 | 現行Motion Bridge | 判断 |
|---|---|---|
| Characterization / Retarget | 名前mappingとCanonical bakeあり | 基盤あり。体格差、spine/twist、reach補償が不足 |
| Animation Layers | 複数additive Correction、weight/mute/soloあり | 強い。Override/hierarchy/general flattenは後回し |
| Control Rig / Effectors | なし | 最重要gap |
| T/R Pin、IK/FK、pole | なし | 最重要gap |
| Floor Contact / Stabilization | Preview Range検出と左右一括Foot Lockを実装 | ankle/sole profile、toe roll、T/R pinは今後 |
| Filter / Smart Plot | Quaternion-safe揺れ除去を実装 | 誤差基準key reductionは今後 |
| Pose / Mirror / Body Part keying | なし | Blender Pose Asset/Keying Setを活用 |
| Story / TimeWarp / Clip Match | なし。Correction strip retimeは拒否 | rebase後の後段機能 |
| Ghost / Trajectory / Error表示 | Source wire referenceのみ | Motion Pathsと独自error overlayを追加 |

MotionBuilderのControl Rigはbody-part pin、automatic floor contact、keying modesを一体化しており、pinはtranslationとrotationを独立して扱う。Floor Contactも単純なfloor clampではなく、手足の接触方法とIK/FK同期を扱う。従ってMotion Bridgeでも「Control Rigの見た目」より、effector/pin/contactの編集結果を安全にCorrectionへ確定できることを優先する。

## 5. 推奨アーキテクチャ

### 5.1 既存の非破壊境界を維持する

```text
Source file
  -> SOURCE_ARCHIVE（不変）
  -> Canonical Base / REPLACE（派生、保護）
  -> Correction 1..N / COMBINE（編集対象）
  -> evaluated Canonical pose
       -> Unity live preview
       -> visual bake / FBX
```

Control Rig、filter、Foot Lock、pose paste、clip matchの姿勢変更は、全て共通`CorrectionWriter`を通して新規またはactive Correctionへ書く。Base/Archiveを直接変更しない。retimeだけはpose deltaではなくversioned time-mapへ書く。previewは同じimmutable snapshotから毎回再生成し、Apply/Cancel/exception/Undo/save/reloadの全経路で復元できることを共通契約にする。

### 5.2 補正を二つのpassに分ける

1. Blender Canonical pass: source motionそのものを修正し、FBXへ持ち出せる。Filter、Foot Lock、Control Rig bake、pose、clip seamはこちら。
2. Unity target pass: target体格で再発するfoot slide/reachだけをpost-retarget IKで直す。`RetargetProfile`とscene `MotionBridgeRigBinding`がある場合だけ有効。

Blenderで見えるpre-IK結果とUnity target固有post-IK結果を混同しない。export品質比較もpre-IK同士、post-IK同士で分ける。

### 5.3 Control RigはCanonical skeletonから分離する

Control用bone/objectをCanonical export hierarchyへ恒久追加しない。別Collectionのversioned control objectまたはdual-chain driver rigを生成し、stable owner IDでCanonicalへbindingする。これによりdescriptor hash、Unity Source Avatar、FBX joint pathを変えない。

Blender 4.5でpole target使用時のIK influenceだけに依存したIK/FK blendは避ける。v1はdual chain + Copy Transforms、または操作結果をCorrectionへvisual bakeしてconstraintを外す方式をspike比較し、pose誤差とlifecycleが小さい方を採用する。

Control Rig lifecycleは`RIG_ACTIVE`、`COMMITTING`、`COMMITTED`、`BYPASSED`を明示する。`COMMITTING`ではrig込みのevaluated poseをsnapshotし、rig constraintをmuteした基準poseから`CorrectionWriter`でresidualを生成してから`COMMITTED`へ原子的に切替える。commit後とFBX export時はrig constraintを必ずmute/removeし、未commitの`RIG_ACTIVE`状態ではexportを拒否する。これによりrig結果とCorrectionの二重評価を防ぐ。

### 5.4 共通sample、Correction Writer、誤差評価を先に作る

次の機能は同じ`MotionSample`とworld-space error evaluatorを使用する。

- Motion Validator
- Filter preview
- Key reduction / Smart Plot
- Foot Lock solve
- Control Rig commit
- Correction rebase / flatten merge
- Blender live pose vs Unity imported clip比較

最低限のsampleはframe、bone local quaternion、bone head/tail world position、root transform、contact stateを持つ。`CorrectionWriter`はscene/action時間変換、NLA `COMBINE`の逆評価、他Correctionのweight/mute/solo、bone rotation、Hips location、Armature objectのRoot trajectory location/quaternion、完全WXYZ/XYZ key、rollbackを一か所で扱う。Root trajectoryは専用channel IDを持つCorrection dataとしてBaseと同じArmature objectへ合成する。書込み先はinfluence 1、no-retimeの正規化Correctionとし、それ以外は新規layerへ書くか明示拒否する。destinationをneutralにした実評価を基準にresidualを解き、書込み後のevaluated poseが入力sampleと一致することを検証する。

Quaternionはhemisphereを揃えた上で、連続frame間のrelative quaternion増分をsegment単位でunwrapしてfilterし、exp積分で再構成する。1 sampleの角度差がπ以上ならsub-sampleするかambiguousとして拒否する。絶対quaternionのWXYZ component別filterと、branchを考慮しない絶対log-vector filterを禁止する。長回転、multi-turn、±π通過を独立fixtureで検証する。

### 5.5 Versioned data model

追加する永続dataは次を独立versionにする。

- `motion-bridge.source-mapping-profile/1`: source fingerprint、bone mapping、take provenance。
- `motion-bridge.solve-profile/1`: Canonical rest fingerprint、limb length、sole/ankle offset、preferred bend plane、joint limit、detector既定値。
- `motion-bridge.retarget-profile/1`: target fingerprint、length ratio、feet spacing、mass-center/reach補償、twist/spine distribution。
- `motion-bridge.contact-track/1`: effector、interval、blend、epoch、anchor position/rotation、ground plane/object、detector設定、source checksum。
- `motion-bridge.control-rig/1`: owner stack ID、rig version、control/binding stable IDs。
- `motion-bridge.filter-recipe/1`: filter種別、範囲、bones、parameter、input checksum、output tolerance。
- `motion-bridge.time-map/2`: stable strip ID、source/action/scene時間域、cycle ID、monotonic segment ID、trim/scale/reverse/repeat/animated-time、contact marker変換、schema migration情報。

未知versionを黙って書換えない。旧dataはcopy-on-migrateし、旧stackを残したまま候補を検証してから切替える。

### 5.6 Beginner-first UI契約

標準操作は次の3段階に揃える。

1. モーションを選ぶ。
2. 「足滑り」「手の位置」「揺れ・ノイズ」等、直したい問題を選ぶ。
3. Previewを確認し、「適用」または「戻す」を選ぶ。

UI原則:

- 各状態のprimary actionは一つにし、次に押すbuttonを明確にする。
- 初回結果までManual mapping、Profile、threshold、layer、IK設定を要求しない。推定できない場合だけ短い日本語で選択を求める。
- 「足滑りを直す」「動きを滑らかにする」「手足を置いて直す」のtask presetを入口にし、技術名はtooltipとAdvancedへ置く。
- Preview Range、接地区間、対象boneは自動検出し、必要な場合だけViewport/Timelineから修正できる。
- 20,000 frameを超える長尺ではエラーだけで止めず、「現在位置付近を確認」の1操作でPreview Rangeへ短縮する。
- 全操作は新規Correctionへ適用し、Before/After、Undo、Cancelを常に同じ位置に置く。
- errorは原因と復旧操作を一文で示す。ID、checksum、stack等の診断情報はdetailsへ畳む。
- Beginner/Advancedで別実装を持たず、同じvalidationとtransactionを通す。
- 詳細設定を保存しても、新しいユーザーや新しいfileの既定値を複雑化しない。

## 6. 実装ロードマップ

期間はBlender/Unity双方に習熟した1名が専任し、synthetic fixtureが利用できる場合の目安である。

### Phase 0 — 契約と編集安全性の固定（5〜8開発日）

実装:

- `contacts-v1`の意味を確定し、未完成capabilityのoffer/acceptを停止。
- Envelope NodeTreeのper-scene ownershipと安全なcleanup。
- Location Hips-onlyをcoreで強制。
- `EASE_OUT`表示修正とpreset truth-table test。
- Correction作成/Bake/Manual mappingのUndo/Redo spike。
- Manual mappingをsource/take fingerprint付き`SourceMappingProfile`へ移し、既存Correctionがあるstackのmapping変更を拒否。
- 複数usable Actionのtake picker。headless/APIでは曖昧時hard error。
- 所有外NLAは変更せず`unsupported coexistence`としてblockし、専用workfile制約を表示。
- Scene Full Copyで共有されたBase/Correction Actionを変更前にblockし、双方のstackを不変に保つ。
- Beginner/Advancedの入口を共通state modelへ統一し、標準画面のprimary actionを一つに整理。
- extension/package/protocol/schema compatibility manifest。
- `PYTHONPATH`を含む統一test entrypointと機械可読summary。
- procedural fixtureのseed、生成version、利用条件を記録するcorpus manifest。

合格:

- 未実装capabilityがhandshake結果に出ない。
- 同名のuser NodeTree/nodeは作成・更新・save/load・unregister後も完全不変。
- Advanced UI/APIから非Hips locationを要求しても明示拒否。
- Undo/Redo 10往復でAction、Slot、NLA binding、selection、current frameが一致。
- 複数Actionを暗黙選択せず、選択take IDがArchive/recipe/reportへ一貫して残る。
- Source差替えやmapping変更で古いProfileを黙って再利用しない。
- 共有Base/Correctionを片側から作成・編集・削除しても、双方を変更せず明示停止する。
- 新規ユーザーが既定値のまま「取込→Correction作成→補正→Before/After→出力」を完了できる。
- Blender 4.5 LTSと5.0で既存Correction回帰がpass。

### Phase 1 — Motion Validator、Correction Writer、共通transaction（8〜12開発日）

実装済み（第一縦切り）:

- `editing/motion_sample.py`: evaluated poseをframeごとにchunk sampleし、22 bones、Root、左右Foot、key densityをimmutable sequenceへ取得。
- `motion_quality.py`: quaternion unit/sign、bone角速度/角加速度、Root移動/回転不連続、slide、penetration、reach、key densityの10 metricとcanonical JSON / SHA-256。
- `editing/correction_writer.py`: Blender main-thread gate、対象channelのconstraint/driver gate、最上位の有効layer gateを通し、NLA Tweakの逆評価でbone WXYZ / Hips XYZ / Object Root XYZ・WXYZをactive Correctionへ書込み、evaluated local/world transform検証と一括rollbackを行うAPI。
- Beginner UIの「モーションを確認」、3段階判定、issue区間、Advancedのmetric数値/JSON。
- Blender main thread上の8 ms目安のmodal sampling/serialization、隔離Python subprocessでのpure analysis、progress/cancel、save/undo/load前復元、変更済み入力へのstale result破棄。`bpy`はmain threadだけで使用。
- process入力のtransport SHA-256照合、32 MiB output、64 metrics、4,096 issue区間のhard limit、terminate/kill/reapと一時directory削除の再試行。
- Beginner結果は3区間だけ表示しつつ総件数を正しく示し、process失敗時は内部用語やpathを隠して確認範囲を短くする復旧操作を表示。
- `RangeSpec` coreのselected keys > Preview Range > whole take優先順位、整数sample境界、品質確認のPreview/whole take接続、長尺takeの1クリック短縮。
- Root-onlyとbone/Root混在target、Object Root完全XYZ/WXYZ、Quaternion符号連続、world-space書込み後検証、Writer用Action/Object/bone transaction。

未実装:

- Envelope、Source import、retarget/visual bake、exportに共用する`editing/transaction.py`。
- Motion Validatorへ表示するjoint-limit metric。versioned `SolveProfile`とsigned joint-limit補正coreは実装済み。
- selected-key `RangeSpec`をFilter / Foot Lock等の各編集toolへ接続するBlender adapter。
- Timeline marker、hand/foot/hips Motion Paths、error色overlay。JSON report自体は実装済み。

合格:

- 既知値golden fixtureでmetric値、max/p95、root translation、finite判定が期待値と一致し、同じ入力から同じJSON hashを生成。
- Writer入力sampleと書込み後evaluated poseが、rotation max 0.25°、主要effector position max 2 mm、Root trajectory translation max 2 mm以内で一致。
- Base/Archiveと非選択bone/rangeはchecksum不変。
- cancel、save、undo、addon unregister、例外で一時dataが残らない。
- 一つのUI sliceは10 ms目標、50 ms超stallを定常発生させない。

現状は第一縦切りと3つのワンクリック補正の回帰が合格した段階で、Phase 1全体の合格宣言ではない。Root trajectoryはsynthetic fixtureで位置10 µm・回転0.001°以内を検証したが、全workflow共通transaction、Validator用joint-limit metric、selected-key adapter、実UI event loopのstall/Undoは未検証である。

### Phase 2 — Quaternion-safe cleanup / Smart Plot（7〜10開発日）

実装済み:

- Rotation: hemisphere整列したQuaternionをLog/Exp空間へ写し、不等間隔sample対応の局所一次Gaussian filterを適用してnormalize。
- 新規Correctionへ変更boneだけを書き、Before/Afterで比較可能にする。
- 「揺れをなめらかに」を弱め／標準／強めの3 presetで提供し、個別parameterはAdvancedへ置く。
- Preview Rangeの外側halo、neutral境界、take endpoint保持、Root/Hips移動保護、Apply/rollback。
- SciPyをruntime依存にせず有限kernelをpure Pythonで実装。

残り:

- Translationのpeak removalと必要性を確認した場合のzero-phase Butterworth。
- Key Syncと、bone角度・hand/foot world位置誤差を基準にしたKey Reducer。
- selected-key / selected-bone adapterと、長尺処理のmodal chunk化。

合格:

- 全quaternionがfiniteかつunit、sign discontinuityなし。
- multi-turnと±π通過で回転方向、累積角、endpointを維持。
- 指定許容値内を全sampleで保証。初期presetはrotation 0.25°、主要effector 2 mmを目標値とする。
- oversampled golden fixtureではkey数50%以上削減、irreducible fixtureでは品質優先で保持し、両方のruntime上限を測定。
- range境界外、Base、Archive、他Correctionを変更しない。
- synthetic noiseでslide/角速度spikeが低減し、意図したpeakの過度な縮小をValidatorが検出。

### Phase 3 — Batch Foot Lock v1（10〜15開発日）

Control Rig完成を待たず、選択takeへ一括適用できるsolverを先に作る。

実装済み（MVP）:

- Left/Right Footの全contact interval scan。
- ground height/speed/minimum duration/join gapによる接地分類とworld anchor。
- 現在のbend方向を保持する解析two-bone IK、左右同時solve、到達不能clamp。
- 区間前後のneutral、足global回転保持、新規Correctionへの変更bone限定書込み。
- Beginner UIの「足滑りを固定」1操作と、左右・接地条件だけを折りたたむ詳細設定。

残り:

- enter/exit hysteresis、foot angle、lock radius、heel/toe/sole offset、任意ground object。
- 区間markerの手動add/split/merge/deleteとanchor再設定。
- `SolveProfile`によるstable knee plane、reach超過時の上限付きpelvis補償。
- loosen/recovery相当のcontact weightと、Phase 2 reducerによる疎化。

合格:

- manifest付き`SolveProfile` 3種 × motion 10本で、未補正比70%以上のslide低減。
- contact中foot slide p95 2 cm以下、penetration p95 1 cm以下。
- reach 98%以下、膝反転0、NaN/inf 0。
- start/endの速度jumpを計測し、preset上限以下。
- cancel/失敗時にmarker、Correction、timeline、selectionを完全復元。

### Phase 4 — 軽量Control Rig v1（15〜20開発日）

実装:

- root/COG/pelvis、chest/head/look。
- left/right hand/foot IK targets。
- elbow/knee pole、heel/toe pivot。
- Translation pin、Rotation pinを独立weight化。
- Full Body / Body Part / Selection keying preset。
- Beginner UIは手・足・腰の大きなcontrolとT/R pinだけを表示し、pole、chain、constraint設定はAdvancedへ置く。
- FK/IK matchとspace switch。切替frameにmatch keyを置きpopを防止。
- 生成/再生成/repair/removeをidempotentかつtransactionalにする。
- `RIG_ACTIVE`→`COMMITTING`→`COMMITTED` / `BYPASSED`の状態遷移を実装。
- commit時にrig込みevaluated resultをsnapshotし、constraintをmuteした基準との差を`CorrectionWriter`へ渡す。
- `editing/bake.py`と`io/fbx_export.py`を更新し、control constraintをexportへ残さず、未commit rigのexportを拒否。

非目標:

- v1でMotionBuilderの全身Pull/Stiffness solverを再実装しない。
- finger/face/quadrupedを含めない。

合格:

- FK↔IK switchの主要effector位置差2 mm以下、回転差0.25°以下。
- pin中にhipsを動かしてもpinned effector driftが2 mm以下。
- left/right mirror fixtureで対称性を維持。
- rig生成前後でCanonical descriptor/hash、Base/Archive checksum、FBX joint pathが不変。
- commit直前のrig evaluated poseとcommit後のrig-disabled poseが全frameで最大許容値内。二重適用0。
- commit cancel/例外でrig state、constraint、Correction、selectionを完全rollback。
- save/reload、Undo/Redo、duplicate Scene、addon unregisterでorphan/constraint残留なし。

### Phase 5 — Retarget Profile、補償、明示Rebase（12〜18開発日）

実装:

- Phase 0の`SourceMappingProfile`とPhase 1の`SolveProfile`を入力に、target fingerprint付き`RetargetProfile`を追加。
- leg/arm/spine ratio、feet spacing、target joint limits、reach/mass-center補償。
- twist distributionとspine count差の分配。
- mapping/algorithm/Profile変更と同じUI transaction内で明示Correction rebaseを提供。
- old final poseとnew Baseを同frameで評価し、`new_delta = inverse(new_base) * old_final`を`CorrectionWriter`で再構成。
- 旧stackを残し、候補stackをBefore/After比較してから切替。

合格:

- humanScale 0.5〜2.0、脚/腕/胴比0.65〜1.55の3体で品質gateを通過。
- rebase前後の主要joint rotation p95 0.25° / max 0.75°以下、effector位置p95 2 mm / max 6 mm以下。
- root translation max 2 mm、finite failure 0、失敗・cancel時は旧stackがactiveのまま。
- mapping/Profile変更はrebaseを完了またはCorrection破棄するまで確定できない。

### Phase 6 — General flattenとUnity post-retarget contact（10〜15開発日）

実装:

- overlap property、modifier、sampled curveを含むresampled Merge Down。
- world-space error based adaptive bake。
- Unityは数値`RetargetProfile`とscene reference `MotionBridgeRigBinding`を分離。
- Core packageはAnimation Rigging非依存を維持し、Integration asmdefまたはoptional packageに分離。
- stateless contact goalをSessionAnchor spaceで送信し、packet loss後も一packetで復元。
- `contact-goals-v1` / `contact-rig-v1`を実装完了時だけ交渉。

合格:

- merge後のrotation/position/root translationのmax/p95、finite、key数、削減率、runtimeをreport。
- 10% packet loss/reorder、random seek、reverse scrub、root前進中も同epoch anchorがworld固定。
- Rig integrationなしのCore previewが従来どおり動作。
- stop/reload/play transition後にTarget TRS、Animator、Scene/Prefab dirty状態を完全復元。
- warm-up後0 B/frame、Unity pose+retarget p95 2 ms以下、IKは別計測。

### Phase 7 — Pose / Clip workflowとtime-map v2（12〜18開発日）

実装:

- Blender Pose Assetのselected-bone apply、flipped apply、blend/extrapolate結果をCorrectionへ確定。
- Full Body/Left Arm/Right Arm/Left Leg/Right Leg等をBone Collection + Keying Set preset化。
- root trajectoryのPreserve / In-place / Start-relative / 指定Anchor modeとtrajectory filter。
- stable strip ID、cycle ID、monotonic segment ID、source/action/scene時間域を持つ`time-map/2`、旧stack migration、contact marker/rangeの変換を先に実装。
- animated-timeは連続かつ区分的に狭義単調なsegmentへ制限する。scene→sourceは`TimeAddress(time, cycle, segment)`を返し、source→sceneは一致する全addressを返す。
- time-map v2へ移行済みstackだけ、NLA trim、scale、reverse、repeat、animated influence/strip timeを安全なUIで露出。
- Hipsまたは選択footによるclip root match、cross-fade、loop seam closure。
- custom auxiliary pin、hand/prop contact、two-hand gripはFoot/Hand solverを再利用して段階追加。
- MotionBuilder Story全体は複製せず、Blender NLAに不足するHumanoid matchだけ実装。

合格:

- pose mirrorの左右world-space誤差がtolerance内。
- clip境界でroot/selected effectorのposition/rotation jumpが設定上限以下。
- forward/reverse/repeat/animated-timeのgolden fixtureでscene→`TimeAddress`が一意、cycle/segment指定round-tripが一致し、source→sceneの複数解を欠落しない。
- migration失敗時は旧stackを変更せず、retime後もCorrection range、contact marker、Source provenanceが一致。

## 7. ファイル単位の実装案

| 追加・変更先 | 責務 |
|---|---|
| `motion_quality.py` | **実装済み:** immutable sequence/frame、10 metric、issue区間、canonical JSON/hash |
| `editing/motion_sample.py` | **実装済み:** 22 bones/Root/feet/key countのchunk samplingとworld/local pose record。永続cacheは未実装 |
| `editing/correction_writer.py` | **実装済み（Phase 1 writer scope）:** main-thread/constraint/driver/最上位有効layer gateを通し、bone/Hips/Rootのevaluated poseをNLA inverse evaluation経由でactive Correctionへ書込み、evaluated local/world transformと一括rollbackを検証 |
| `range_spec.py` | **実装済み（core）:** selected keys > Preview Range > whole takeの優先順位、整数sample境界、fractional take、現在位置中心のbounded Preview。品質確認はPreview/whole takeへ接続済み |
| `motion_quality_process.py` | **実装済み:** main thread上の時分割serialization、隔離Python subprocessの起動・poll・cancel・一時file cleanup、入力transport SHA-256照合、bounded output、kill/reap/cleanup再試行。subprocessはpure analysisだけを実行し`bpy`へアクセスしない |
| `addon.py` | **実装済み:** Beginner 3段階結果、正確な総件数付き3区間preview、日本語error recovery、Advanced数値/JSON、modal sampling/serialization、subprocess解析、progress/cancel/lifecycle/stale-result管理 |
| `tools/benchmark_motion_quality_process.py` | **実装済み:** 7,201 frames × 22 bonesのserialization chunk、隔離解析、process/temp cleanupを再現可能なJSONで計測 |
| `motion_filter.py` | **実装済み:** Quaternion Log/Exp、局所一次Gaussian、弱め／標準／強めpreset、不等間隔sample、endpoint保持 |
| `joint_limits.py` | **実装済み:** versioned `SolveProfile`、signed X/Z swing、twist、soft zone、Canonical 22-bone既定値 |
| `foot_lock.py` | **実装済み（MVP）:** 左右接地scan、world anchor、解析two-bone IK、到達不能clamp、neutral境界、足global回転保持 |
| `editing/auto_correction.py` | **実装済み:** 3 solverのsample/range/write接続、新規Correction作成、変更bone限定、事後検証、no-opと完全rollback |
| `editing/transaction.py` | **未実装:** datablock/key/selection/timeline共通snapshotとrollback |
| `editing/filters.py` | **残り:** translation peak removal、key sync、誤差基準key reducer。Quaternion-safe filter coreは`motion_filter.py`に実装済み |
| `editing/contact_track.py` | versioned interval/anchor/ground metadata |
| `editing/contact_lock.py` | **残り:** pelvis compensation、編集可能contact weight/track。two-bone solve MVPは`foot_lock.py`に実装済み |
| `editing/control_rig.py` | generated rig、binding、FK/IK/pin、commit lifecycle |
| `editing/pose_tools.py` | Pose Asset bridge、mirror、body-part keying |
| `editing/time_map.py` | stable strip ID、source/action/scene時間変換、schema migration |
| `editing/clip_tools.py` | match、loop seam、time-map操作 |
| `retargeting/source_profile.py` | source/take fingerprint付きmapping provenance |
| `retargeting/solve_profile.py` | **残り:** rest/limb/ankle/sole情報を含む包括profile。joint-limit schemaは`joint_limits.py`に実装済み |
| `retargeting/profile.py` | target別Retarget Profileと体格差補償 |
| `retargeting/rebase.py` | copy-on-write stack rebase |
| `performance/take_analysis.py` | validator/reducer metricsとJSON result |
| `live/pose_session.py` | contact goal sample/encode、capability gate |
| `editing/bake.py` / `io/fbx_export.py` | rig state gate、二重評価防止、control除外 |
| Unity `Runtime/Contact` | optional post-retarget contact consumer |
| Unity `Editor/Profile` | Profile/Binding validation UI |
| `addon.py` | 当面thin operator接続のみ。機能追加ごとにProperty/Operator/Panelを`ui/`へ段階分割 |

`addon.py`は調査時点で3,000行を超える。大規模な先行rewriteはせず、各新機能を別moduleへ実装し、触るoperator/panelから順に薄くする。

## 8. Test matrix

### L0 — Pure Python

- Motion Qualityの入力stream hash、built-in-only decode、output/metric/issue上限、short write、terminate→kill→reap、cleanup再試行。
- Quaternion relative-log/exp、segment unwrap、hemisphere、slerp/squad、mirror、multi-turn、±π通過。
- two-bone IK、reach clamp、bend plane、joint limit。
- hysteresis/contact epoch/anchor state machine。
- filter response、peak preservation、key reduction error bound。
- time-map forward/reverse/repeat、cycle/segment address、複数逆像、marker mapping、rebase式。
- finite/boundary/property-based cases。
- golden metric値、reducer削減率、max/p95/root translation、runtime上限。

### L1 — Blender background 4.5 / 5.0

- Slotted Action、NLA COMBINE、Tweak Mode、foreign NLAの安全なhard block。
- CorrectionWriterのscene/action時間、layer weight/mute/solo、bone rotation/Hips location/Root trajectory一致。
- preview/apply/cancel/exception/Undo/Redo。
- save/reload、stale session、duplicate Scene、addon register/unregister。
- Base/Archive/非選択rangeのchecksum不変。
- Control Rig生成/repair/remove、state遷移、commit一致、二重評価防止、FBX除外。
- long takeのchunk/cancel。

### L2 — Blender UI smoke

- narrow N-panel、Beginner/Advancedの同一core制約。
- 初見ユーザー向けgolden path: importからFoot Lock previewまで3つの主要判断以内、手動数値入力0。
- Beginner画面にsolver/quaternion/NLA/Profile/time-map等の実装用語が表示されない。
- curve/filter/marker/control操作。
- progress/cancel、error recovery、hotkey、selection。
- Japanese label/tooltip snapshot。

### L3 — Unity EditMode / PlayMode

- Profile/Binding validation。
- Core-only / Integrationありの両variant。
- single-writer ownership、0 B/frame、scene dirtyなし。
- stop/exception/reload/play transition/window close/target delete。

### L4 — Cross-process

- Correction編集→3 Target、60 Hz、30秒。
- scrub/pause/reconnect/sequence wrap。
- contact goal、10% loss/reorder、clock offset/drift。
- Blender live poseとUnity targetのpre/post IK metric。

### L5 — Export / Corpus

- seed、generator version、利用条件を固定したprocedural synthetic 3体 × walk/run/crouch/punch/one-foot contact/turn/long dense。
- 追加する実素材は権利とprovenanceをcorpus manifestへ記録し、存在しない素材を出荷gateに使わない。
- pre-IK Blender live vs Unity imported FBX: effector p95 1 cm以下、rotation p95 1°以下。
- post-IKは同じProfile/Bindingを適用した結果同士を比較。
- raw FBX hashではなくhierarchy/rest/clip semantic hashをgate。

## 9. Milestoneと出荷判断

| Milestone | 含むPhase | ユーザー価値 | 出荷条件 |
|---|---:|---|---|
| M0 Safe Editing | 0 | 迷わず現行機能を安全に利用 | P0-A〜D/F〜H解消、P0-E安全block、Beginner golden path、Undo、4.5/5.0回帰pass |
| M1 Motion Cleanup | 1-2 | 問題箇所の可視化、noise除去、key削減 | error toleranceとrollback gate |
| M2 Foot Lock MVP | 3 | 足滑りを一括補正 | 3 SolveProfile×10 motion品質gate |
| M3 Interactive Rig | 4 | effector/pinで直感的に補正 | switch/pin/lifecycle gate |
| M4 Retarget Quality | 5-6 | 体格差補償、target固有contact、安全な再base | loss/reorder/rebase/3体gate |
| M5 Clip Workflow | 7 | pose再利用、clip接続、loop修正 | mirror/clip seam/retime gate |

M0〜M3をMotion Correction MVPとし、1名専任で約9〜13週を目安にする。M4〜M5を含む拡張版は追加7〜11週、全体16〜24週を見込む。実素材の品質調整、UI iteration、Blender 4.5固有差分が出た場合は別途bufferを取る。

## 10. やらない方がよい実装

- Quaternion WXYZへcomponent別Butterworth/decimateを直接適用する。
- Control bone/constraintをCanonical export skeletonへ恒久追加する。
- 未完成featureをcapabilityとしてadvertiseする。
- contact全clip scanを一回のblocking `frame_set` loopでUI threadへ載せる。
- Animation RiggingをCore UPM packageの必須依存にする。
- mapping更新時に既存Correctionを黙って新Baseへ載せ替える。
- Blender NLA、Pose Asset、Graph Editorを独自data model/UIで複製する。
- Full Body HumanIK互換をFoot Lock/limb IKより先に実装する。
- 内部data modelやsolver optionをそのまま標準UIへ並べる。

## 11. 公式資料

MotionBuilder:

- [Control rigs — MotionBuilder 2026](https://help.autodesk.com/cloudhelp/2026/ENU/MotionBuilder-Character-Animation/files/Character-Animation/GUID-0CC7458E-1F10-4714-BFD3-4465D4565A68.html)
- [Pinning — MotionBuilder 2026](https://help.autodesk.com/cloudhelp/2026/ENU/MotionBuilder-Character-Animation/files/Character-Animation/Control-rigs/GUID-07E2CBFB-52CC-4709-A28F-1E16FD9546D4.html)
- [Floor Contact properties — MotionBuilder 2026](https://help.autodesk.com/cloudhelp/2026/ENU/MotionBuilder-Character-Animation/files/Character-Animation/Character-setup/Floor-Contact/GUID-BA4129D7-3A54-45A6-869A-30E161DA6FDE.html)
- [Retargeting Actor properties / Feet Stabilization — MotionBuilder 2026](https://help.autodesk.com/cloudhelp/2026/ENU/MotionBuilder-Reference/files/Character-Settings-Reference/GUID-8421D4B9-E5BE-41B7-B4C8-F563DB74DD6B.html)
- [Retarget Properties — MotionBuilder 2026](https://help.autodesk.com/cloudhelp/2026/ENU/MotionBuilder-Reference/files/Character-Settings-Reference/GUID-877F937B-21C2-472F-AA43-0099DBF08B75.html)
- [Animation filters — MotionBuilder](https://help.autodesk.com/cloudhelp/2022/ENU/MotionBuilder/files/GUID-3BE7AA24-887B-488D-A65F-2D7B0FE55F12.htm)
- [TimeWarp curves — MotionBuilder 2026](https://help.autodesk.com/cloudhelp/2026/ENU/MotionBuilder-Animation/files/Work-with-FCurves/GUID-6B4802CB-E22C-464D-A746-215458DD02AB.html)
- [Clip Match options — MotionBuilder](https://help.autodesk.com/cloudhelp/2023/ENU/MotionBuilder/files/GUID-3E6F330F-5ECE-4E0B-989A-ACA642997EC8.htm)

Blender:

- [NLA Strip properties — Blender 4.5 LTS](https://docs.blender.org/manual/en/4.5/editors/nla/sidebar.html)
- [NLA Strip editing and visual bake — Blender 4.5 LTS](https://docs.blender.org/manual/en/4.5/editors/nla/editing/strip.html)
- [Graph Editor filters and decimate — Blender 4.5 LTS](https://docs.blender.org/manual/en/4.5/editors/graph_editor/fcurves/editing.html)
- [Inverse Kinematics — Blender 4.5 LTS](https://docs.blender.org/manual/en/4.5/animation/armatures/posing/bone_constraints/inverse_kinematics/introduction.html)
- [Inverse Kinematics Constraint — Blender 4.5 LTS](https://docs.blender.org/manual/en/4.5/animation/constraints/tracking/ik_solver.html)
- [Motion Paths — Blender 4.5 LTS](https://docs.blender.org/manual/en/4.5/animation/motion_paths.html)
- [Pose Library — Blender 4.5 LTS](https://docs.blender.org/manual/en/4.5/animation/armatures/posing/editing/pose_library.html)

Unity:

- [Animation Rigging package — Unity 6](https://docs.unity3d.com/ja/6000.0/Manual/com.unity.animation.rigging.html)
- [RigBuilder and PlayableGraph — Animation Rigging](https://docs.unity3d.com/ja/Packages/com.unity.animation.rigging%401.2/api/UnityEngine.Animations.Rigging.RigBuilder.html)
- [Animation Rigging constraint components](https://docs.unity3d.com/ja/Packages/com.unity.animation.rigging%401.2/manual/ConstraintComponents.html)

## 12. 次の着手単位

Phase 0、Phase 1第一縦切り、Quaternion-safe揺れ除去、signed関節可動域補正、Canonical Foot Lock MVP、初心者向け3ボタンUIまでは実装済みである。次はBlender 4.5 LTSと実UI Undo smokeを通し、実モーションcorpusでpresetを調整する。その後、selected-key `RangeSpec`、長尺自動補正のmodal chunk化、誤差基準key reduction、toe/soleを含むFoot profileとcontact markerへ進む。Control Rig、Hand IK、pole/T/R pin、Motion Paths/error overlay、Unity contact packetは、その土台を再利用して段階追加する。全workflow共通transactionへ統合するまで、各補正の現在の局所transactionとrollback契約を維持する。
