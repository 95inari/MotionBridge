# Shared protocol fixtures

`pose_v1_full_state_1_joint.hex` は Python encoder の bytes を hex 化した最初の共有 fixture で、Python exact-byte test と C# decoder test の両方が読む。`.json` は意味上の期待値。Phase 0 で generator と binary `.mbp`、C# encoder → Python decoder の逆方向 fixture を追加する。

最低限 identity、axis rotations、trajectory root、optional joint、contact epoch/anchor/ground plane、uint32 sequence wrap、NaN/truncation/version/session mismatch を含める。手書き packet を両言語で別々に管理しない。
