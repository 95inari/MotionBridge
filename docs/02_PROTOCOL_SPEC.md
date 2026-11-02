# Motion Bridge Protocol v1

- Protocol major: 1
- Transport: TCP control + UDP pose
- Byte order: little endian
- Character encoding: control JSON は UTF-8
- Default bind: `127.0.0.1`
- Default control port: `39871`
- UDP port: Unity が session ごとに割り当て、handshake で通知
- Stability: `0.1.0` pre-release。現行 208-byte fixed payload が最初の v1 baseline で、外部公開済みの旧 v1 wire format は存在しない。

実装状況（2026-09-02）: TCP length-prefix codec、`hello` / `accept` / `skeleton_descriptor` / `ready` handshake、negotiated UDP endpoint、`resume` / `pause`、`ping` / `pong`、`clock_sync_ping` / `clock_sync_pong`、`clock_sync_offset` / `clock_sync_reset`、500 ms周期の`pose_stats`、`goodbye` / `error`、5秒heartbeat timeoutをBlender clientとUnity serverへ実装済み。Blenderはdirty poseを30/60 Hz上限、静止時を5 Hzで送る。named capabilityは`hello`で提示し、`accept`で両者の共通集合を確定する。実processのBlender 5.0.1 ↔ Unity 6000.2.15f1最小E2EもPause→Resumeとone-way age telemetryを含めて実装済み。最終runはRTT `268 usec`、3 packets、sequence gap/reject/overrun 0、one-way age p50 `172 usec` / p95 `174 usec`だったが、単一runの観測値である。全受入matrixのE2Eは未完。

## 1. 原則

- pose packet は full-frame independent。前 packet がなくても pose と接触 goal を decode できる。
- UDP 欠落は許容し、再送しない。sequence が最新の packet だけを使う。
- control と skeleton metadata は TCP で確実に渡す。
- pose packet は 1200 bytes 以下に収め、IP fragmentation を避ける。
- v1 は Canonical Humanoid だけを扱う。descriptor は root を含め最大 64 joints、1200-byte UDP budget 内の PosePacket は最大 60 joints。
- 非 root translation は毎 frame 送らない。rest local translation を handshake で一度送る。

## 2. 接続方向

1. Unity が TCP listener を `127.0.0.1:39871` で開始し、UDP socket を確保する。
2. Blender が Unity の TCP listener へ接続する。
3. Blender が `hello` を送る。
4. Unity が validation 後 `accept` と UDP endpoint を返す。
5. Blender が `skeleton_descriptor` を送る。
6. Unity が Source Avatar / descriptor hash を検証して `ready` を返す。
7. Blender が UDP pose を送る。
8. TCP ping/pong が 5 秒途切れたら session を終了する。

状態:

```text
Disconnected → Connecting → Handshaking → Ready → Streaming
                                          ↘ Error
Streaming → Paused → Streaming
Streaming/Paused/Error → Disconnected
```

## 3. TCP framing

```text
uint32 payloadLength
byte[payloadLength] utf8Json
```

- `payloadLength` は JSON byte 長。4 byte prefix 自体を含めない。
- 最大 65536 bytes。
- 未知の message `type` は minor-version capability なら無視し、required message なら `error` を返す。

全 control message 共通 field:

```json
{
  "type": "hello",
  "protocolMajor": 1,
  "protocolMinor": 0,
  "sessionId": 305419896,
  "requestId": 1
}
```

## 4. Handshake messages

### 4.1 `hello` Blender → Unity

```json
{
  "type": "hello",
  "protocolMajor": 1,
  "protocolMinor": 0,
  "sessionId": 305419896,
  "requestId": 1,
  "addonVersion": "0.1.9",
  "blenderVersion": "5.0.1",
  "skeletonId": "mb-canonical-humanoid-v1",
  "skeletonHash": "sha256-hex",
  "frameRate": { "numerator": 60, "denominator": 1 },
  "rootMode": "WorldRoot",
  "requestedPoseHz": 60,
  "capabilities": [
    "global-rest-delta-v1",
    "clock-sync-v1",
    "pose-stats-v1",
    "one-way-pose-age-v1"
  ]
}
```

### 4.2 `accept` Unity → Blender

```json
{
  "type": "accept",
  "protocolMajor": 1,
  "protocolMinor": 0,
  "sessionId": 305419896,
  "requestId": 1,
  "unityVersion": "6000.2.15f1",
  "packageVersion": "0.1.0",
  "udpHost": "127.0.0.1",
  "udpPort": 50321,
  "acceptedPoseHz": 60,
  "humanTrait": {
    "muscleCount": 95,
    "muscleNamesHash": "sha256-hex"
  },
  "capabilities": [
    "global-rest-delta-v1",
    "clock-sync-v1",
    "pose-stats-v1",
    "one-way-pose-age-v1"
  ]
}
```

`muscleCount` の例示値を実装で固定しない。Unity の `HumanTrait` から生成する。

UnityはBlenderが提示したnamed capabilityのうち、自身が実装するものだけを`accept.capabilities`へ返す。Blenderは提示していないcapabilityが返ればprotocol errorにする。`clock_sync_ping/pong`は`clock-sync-v1`、`pose_stats`は`pose-stats-v1`が双方で確定したsessionだけで送る。`one-way-pose-age-v1`はその2 capabilityの両方に依存し、交渉されないsessionで`clock_sync_offset/reset`またはone-way percentile fieldを送るとprotocol errorにする。

`contacts-v1`はpacket fieldの予約だけでは交渉しない。Blenderでのcontact goal生成からUnity rig適用までが実装・検証された時点で、producer/consumer双方のcapabilityへ追加する。

### 4.3 `skeleton_descriptor` Blender → Unity

```json
{
  "type": "skeleton_descriptor",
  "protocolMajor": 1,
  "protocolMinor": 0,
  "sessionId": 305419896,
  "requestId": 2,
  "skeletonId": "mb-canonical-humanoid-v1",
  "skeletonHash": "sha256-hex",
  "coordinateSpace": "unity-left-handed-y-up-z-forward",
  "rootIndex": 0,
  "joints": [
    {
      "index": 0,
      "name": "Root",
      "humanBone": null,
      "parentIndex": -1,
      "jointPath": "",
      "required": true,
      "restLocalPosition": [0, 0, 0],
      "restLocalRotation": [-0.70710678, 0, 0, 0.70710678],
      "restGlobalRotation": [-0.70710678, 0, 0, 0.70710678]
    },
    {
      "index": 1,
      "name": "Hips",
      "humanBone": "Hips",
      "parentIndex": 0,
      "jointPath": "Hips",
      "required": true,
      "restLocalPosition": [0, 0, 1],
      "restLocalRotation": [0.70710678, 0, 0, 0.70710678],
      "restGlobalRotation": [0, 0, 0, 1]
    }
  ]
}
```

Quaternion JSON 表現は `[x,y,z,w]`。`coordinateSpace` は global/session 値の空間を示し、`restLocal*` は各 FBX parent transform の local 軸で表す。skeleton hash は RFC 8785 JSON Canonicalization Scheme (JCS) の UTF-8 bytes へ SHA-256 を取る。Python/C# は JCS conformance vectors を共有し、`-0`、指数表記、Unicode escape、object key order を含む byte equality test を通す。独自の「sort keys + float stringify」は使わない。

Unity は descriptor の順序で `jointPaths` と `NativeArray<float>(jointCount * 7)` を作る。Source Avatar asset の descriptor hash と一致しない場合は `ready` を返さない。

### 4.4 `ready` Unity → Blender

```json
{
  "type": "ready",
  "protocolMajor": 1,
  "protocolMinor": 0,
  "sessionId": 305419896,
  "requestId": 2,
  "target": {
    "name": "Character_A",
    "avatarValid": true,
    "avatarHuman": true,
    "humanScale": 1.03
  }
}
```

### 4.5 その他

- `pause` / `resume`
- `set_root_mode`
- `set_pose_hz`
- `ping` / `pong`
- `clock_sync_ping` / `clock_sync_pong`
- `clock_sync_offset` / `clock_sync_reset`
- `pose_stats`
- `error`
- `goodbye`

`error`:

```json
{
  "type": "error",
  "protocolMajor": 1,
  "protocolMinor": 0,
  "sessionId": 305419896,
  "requestId": 2,
  "code": "SKELETON_HASH_MISMATCH",
  "message": "Canonical skeleton does not match the Unity Source Avatar.",
  "recoverable": false
}
```

### 4.6 Clock sync / telemetry

Blender と Unity の monotonic clock は epoch が異なるため、packet timestamp を直接減算しない。接続時と 5 秒ごとに NTP 形式の四 timestamp sync を行う。全値は各 process の monotonic microseconds。

```json
{
  "type": "clock_sync_ping",
  "sessionId": 305419896,
  "requestId": 10,
  "t0BlenderSendUsec": 1000000
}
```

Unity は受信直後の `t1UnityReceiveUsec` と送信直前の `t2UnitySendUsec` を返し、Blender は受信時に `t3BlenderReceiveUsec` を採る。

```text
unityMinusBlenderOffset = ((t1 - t0) + (t2 - t3)) / 2
roundTripTime           = (t3 - t0) - (t2 - t1)
poseOneWayAgeAtUnity    = unityReceive - (poseTimestamp + offset)
```

直近 8 samples のうち RTT が最小の sample を採用し、sync 未成立中は one-way latency を表示しない。負の age や offset jump 5 ms 超は sample を無効化し再同期する。

`one-way-pose-age-v1`合意中は、Blenderが選択したoffsetを次の署名付き64-bit integer fieldでUnityへ送る。同期を無効化したときは`clock_sync_reset`を送り、Unityのoffsetとage windowをクリアする。

```json
{
  "type": "clock_sync_offset",
  "sessionId": 305419896,
  "requestId": 11,
  "unityMinusBlenderOffsetUsec": 9250
}
```

`unityMinusBlenderOffsetUsec`はJSON numberの整数tokenが丁度1つ必要で、float、string、duplicate field、int64範囲外は拒否する。Unityは同期中に5 msを超えてjumpするoffsetを受け付けず、`clock_sync_reset`後の再同期を要求する。

Unity は 500 ms ごとに `pose_stats` を TCP で Blender へ返す。

```json
{
  "type": "pose_stats",
  "sessionId": 305419896,
  "lastSequence": 1200,
  "received": 1195,
  "droppedBySequence": 5,
  "rejected": 0,
  "receiverOverrun": 0,
  "oneWayAgeUsecP50": 6200,
  "oneWayAgeUsecP95": 9400
}
```

`received`はaccepted packet数、`rejected`はdecode/session validation拒否数の合計とする。drop は accepted sequence の半範囲差から wrap-safe に数え、Free slot 不足で捨てた packet は `receiverOverrun` として別計上する。counterはsession内の累積値で減少してはならない。

現実装は`lastSequence`、`received`、`droppedBySequence`、`rejected`、`receiverOverrun`を常に送信する。one-way capabilityが交渉され、Unity receiverがclock同期中で有1つ以上の有効sampleを持つ場合だけ`oneWayAgeUsecP50/P95`を二つ同時に付ける。accepted packetの受信時刻とtimestamp+offsetの差をallocationなしの固定長ring 256 samplesへ入れ、負のageはpercentileを汚染せず破棄する。Blender UIはclock sync RTT、counter、optional p50/p95を表示する。

## 5. UDP PosePacket v1

### 5.1 Header: 32 bytes

| Offset | Size | Type | Field | Value |
|---:|---:|---|---|---|
| 0 | 4 | char[4] | magic | `MBP1` |
| 4 | 1 | uint8 | major | `1` |
| 5 | 1 | uint8 | minor | `0` |
| 6 | 1 | uint8 | messageType | `1 = pose` |
| 7 | 1 | uint8 | flags | 下記 |
| 8 | 4 | uint32 | sessionId | handshake と一致 |
| 12 | 4 | uint32 | sequence | wrap 可、半範囲比較 |
| 16 | 8 | uint64 | timestampUsec | Blender monotonic time |
| 24 | 4 | int32 | frameIndex | Blender scene frame |
| 28 | 2 | uint16 | payloadBytes | header を除く |
| 30 | 2 | uint16 | jointCount | descriptor と一致 |

flags:

- bit 0: `WorldRoot`。0 なら InPlace。
- bit 1: contacts 有効。
- bit 2: Blender playback 中。
- bit 3: pose dirty event は scrub/edit 由来。
- bit 4〜7: reserved、0。

### 5.2 Payload

| 順序 | Type | 内容 |
|---:|---|---|
| 1 | float32[3] | trajectory root position、SessionAnchor からの meter delta、Unity coordinate space |
| 2 | float32[4] | trajectory root rotation `[x,y,z,w]` |
| 3 | float32[3] | Hips local position delta、Canonical Hips parent space、meter |
| 4 | uint64 | present joint mask |
| 5 | float32[jointCount][4] | 各 joint の global rest-relative rotation delta |
| 6 | float32[4] | leftFoot, rightFoot, leftHand, rightHand contact weight |
| 7 | uint32[4] | 各 effector の contact epoch |
| 8 | float32[4][3] | 各 effector の contact anchor position |
| 9 | float32[4][4] | 各 effector の contact anchor rotation `[x,y,z,w]` |
| 10 | float32[4] | ground plane `(normal.x, normal.y, normal.z, distance)` |

固定 payload は `208 bytes`、packet 長は `32 + 208 + 16 * jointCount`。jointCount 56 では `1136 bytes`、最大 60 joints で `1200 bytes`。descriptor は 64 joints まで表現できるが、61 以上を pose transport に使う session は handshake で拒否する。

packet 先頭からの offset は次で固定する。`n = jointCount`。

| Field | Offset | Bytes |
|---|---:|---:|
| trajectoryRootPosition | 32 | 12 |
| trajectoryRootRotation | 44 | 16 |
| hipsLocalPositionDelta | 60 | 12 |
| presentMask | 72 | 8 |
| jointRotations | 80 | `16*n` |
| contactWeights | `80+16*n` | 16 |
| contactEpochs | `96+16*n` | 16 |
| contactAnchorPositions | `112+16*n` | 48 |
| contactAnchorRotations | `160+16*n` | 64 |
| groundPlane | `224+16*n` | 16 |

`present joint mask` は bit `i` が 1 のとき joint `i` が packet に有効。ただし v1 の配列自体は固定長で、欠落 joint の quaternion は identity を格納する。required joint の bit が 0 なら packet を捨てる。

Blender は Streaming 開始時の Canonical world transform を固定 `SessionAnchor` とする。trajectory root、contact anchor、ground plane は全てこの不変 SessionAnchor space で表す。Unity は開始時 target world transform を `TargetSessionAnchor` として保存し、`worldGoal = TargetSessionAnchor * sessionSpaceGoal` で変換する。現在 trajectory root から contact anchor を変換してはならないため、同一 epoch 中に root が移動しても foot goal は world に固定される。

接触 field は full-frame stateless に解釈する。anchor position は meter。epoch は接触が新しく開始するたび effector ごとに increment し、同じ epoch の間は anchor を維持する。packet loss、scrub、逆再生、frame jump 後も受信した一 packet だけで goal を再構成できる。contacts flag が 0 のとき weight は 0、epoch/anchor は無視する。ground plane は SessionAnchor space の `dot(normal, point) + distance = 0`、normal は unit vector。

### 5.3 Rotation semantics

Blender evaluated pose から各 joint の armature-space rotation を得る。

```text
F            = diag(-1, 1, 1)                 # FBX right → Unity left handedness
R            = RotX(-90 degrees)              # imported FBX root axis rotation
B            = R * F                          # Blender world → Unity world/session
deltaBlender = poseGlobalBlender * inverse(restGlobalBlender)
deltaUnity   = B * deltaBlender * inverse(B)
trajectoryUnity = B * trajectoryBlender
```

物理 bone の Source Avatar rest local は `F * localBlender * F`、rest global は `R * F * globalBlender * F`、joint 0 の rest local/global は `R` とする。Hips local position delta は parent である FBX root space のため `F` で変換する。Euler の成分入替や符号反転で実装せず、行列変換から quaternion 化する。

Unity reconstruction:

```text
poseGlobalUnity[i] = deltaUnity[i] * restGlobalUnity[i]

if parentIndex[i] >= 0:
    poseLocalUnity[i] = inverse(poseGlobalUnity[parent]) * poseGlobalUnity[i]
else:
    poseLocalUnity[i] = poseGlobalUnity[i]
```

`avatarPose[7*i + 0..2]` は Hips だけ `restLocalPosition + hipsLocalPositionDelta`、その他は descriptor の rest local positionを使う。joint 0 は descriptor の `R` を維持し、packet joint delta は identity とする。`avatarPose[7*i + 3..6]` は reconstructed local quaternion。trajectory root は HumanPoseHandler へ入れず、session 開始時の target anchor へ WorldRoot mode で一度だけ適用する。HumanPose `bodyPosition/bodyRotation` は Hips/COG channel であり trajectory とは別 channel。

`F`、`R`、`B` は Canonical FBX export/import の結果に依存するため、Phase 0 の bind-pose / axis golden testで descriptor と code constant に固定する。誤差が 0.1 degree / 0.1 mm を超える状態で protocol v1 を確定しない。

### 5.4 Quaternion rules

- norm が `0.99〜1.01` 外なら packet を捨てる。送信側は必ず normalize。
- 前 packet の同 joint と dot が負なら `q = -q` とし、符号連続性を維持する。
- NaN / Infinity / all-zero は拒否。
- Unity 側で normalize するが、不正値の修復目的には使わない。

## 6. Session validation / Latest-only buffer

decode は unpublished scratch slot にだけ書く。成功後、publish 前に `sessionId == accepted session`、`jointCount == descriptor`、required mask、packet 長、finite 値、uint half-range sequence freshness を検証する。失敗した scratch slot は破棄し、現在公開中の pose を変更しない。

production receiver は `PosePacketGate.TryDecodeAndAccept` だけを publication boundary として使い、raw decoder の成功だけで publish してはならない。

受信 thread は Unity API を呼ばず、三つの preallocated slot を使う。各 slot は `Free / Writing / Published / Reading` の reader ownership を持ち、producer は `Free` のみへ decode、全 validation 後に published index を atomic 交換する。consumer は published slot を `Reading` に claim してからコピーし、完了後 `Free` へ戻す。producer が二周して consumer 読取中 slot を上書きすることを禁止する。Free slot がなければ incoming packet を捨てる。

main thread は frame ごとに一回だけ最新 sequence を読む。中間 frame は捨てる。可変長 Queue、LINQ、per-packet object allocation、JSON pose decode は禁止する。

Blender 側も send buffer が詰まったら古い pose を retry せず捨てる。

## 7. Timeout

- UDP pose age 100 ms 超: UI に yellow latency warning。
- Blender は pose dirty 時に即送信し、静止中も TCP Streaming/Paused session に 5 Hz で同じ full pose を新しい sequence として送る。
- 500 ms 超: heartbeat missing として pose stale、適用停止。ただし最後の受信 pose を「静止だから戻す」ことはしない。
- TCP ping/pong 5 s 超: session disconnect、Unity preview cleanup。
- Blender pause は最後の pose を保持したまま heartbeat を継続する。disconnect は preview cleanup と Target 復元を意味する。

## 8. Versioning

- major 不一致: 接続拒否。
- minor: 低い側に capability negotiation する。
- reserved bit / field は 0 を要求。minor capability が明示された場合だけ解釈する。
- packet layout 変更は major を上げる。
- 上記は最初の repository baseline 以降に適用する。pre-release scaffold 内で置き換えた旧 draft は互換対象ではない。
- control message の optional field 追加は minor でよい。

## 9. Phase 0 completion golden tests

`protocol/golden` に両言語で共有する test vector を置く。

現 baseline は Python encoder → C# decoder の positive full-state fixture 一件まで実装済み。以下の残りと C# encoder → Python decoder は Phase 0 S0.1 の成果物で、Phase 0 exit 前に必須とする。

必須:

1. T-pose identity delta。
2. Hips yaw +90 degree。
3. 左 shoulder local raise +90 degree。
4. root `(+1m forward, +90 degree yaw)`。
5. quaternion sign flip が同一 pose になる。
6. optional joint absent。
7. truncated packet rejection。
8. wrong endian / magic / version rejection。
9. NaN / zero quaternion rejection。
10. out-of-order sequence rejection。
11. session/jointCount/required mask mismatch rejection。
12. contact epoch/anchor/ground plane が単独 packet から復元できる。
13. uint32 sequence wrap acceptance。
14. non-zero Hips local translation の axis/unit/humanScale round trip。

Python encoder → C# decoder、C# encoder → Python decoder の両方向で byte-level fixture を一致させる。
