# Canonical asset ownership

`mb_canonical_humanoid_v1.blend` は唯一の手編集可能な Canonical 正本とする。

`blender_addon/tools/build_canonical.py` が次を決定的生成する。

- `protocol/generated/canonical_humanoid_v1.json`
- `verification/unity-6000.2/Packages/com.kuko.motionbridge/Samples~/Canonical/MB_Canonical_Humanoid_v1.fbx`

生成物は手編集せず、clean checkout から二回生成した descriptor byte hash、joint paths、parent index、Unity-imported rest pose、Humanoid mapping の一致を CI で検証する。FBX raw hash は exporter 固有IDが変わるため gate にしない。
