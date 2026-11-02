# Motion Bridge はじめてガイド

Motion Bridgeで、手持ちのHumanoidモーションをBlenderで修正し、同じPCのUnityで確認します。以下は配布ZIPを展開したフォルダーを基準にした手順です。Gitや開発用Unityプロジェクトは不要です。

## 1. 配布ファイルと作業環境を準備する

1. 外側の `MotionBridge-0.1.13-GitHub.zip` を、後から移動しない作業用フォルダーへ展開します。`PREVIEW.zip` は利用条件が未確定の場合の確認用です。
2. Blender 5.0.1とUnity 6000.2.15f1を用意します。今回の配布版の動作確認環境はmacOS（Apple Silicon）です。Blender 4.5 LTSは互換対象ですが、今回の配布版では再検証していません。Windows/Linuxの動作確認も未完了です。
3. 利用できる権利を持つHumanoidモーション（FBX / glTF / GLB）と、Unity Humanoidとして設定されたキャラクターを用意します。キャラクターモデルや既成のモーション素材は同梱していません。
4. 既存のBlenderファイルとUnityプロジェクトを使う場合は、作業用コピーで始めます。

`Blender` フォルダー内のZIPと `Unity` フォルダー内のTGZは、展開せずにインストールします。

## 2. Blenderへインストールする（初回だけ）

1. Blenderの `Edit > Preferences`（編集 > プリファレンス）を開きます。
2. `Get Extensions` または `Add-ons` の右上メニューから `Install from Disk...`（ディスクからインストール）を選びます。
3. `Blender/motion_bridge-0.1.13.zip` を指定してインストールします。外側の販売用ZIPは指定しません。
4. `Add-ons` で `Motion Bridge` が有効で、バージョンが `0.1.13` であることを確認します。
5. `Samples/mb_canonical_humanoid_v1.blend` を開き、すぐに `File > Save As`（名前を付けて保存）で作業用の別名を付けます。

インストール方法は[Blender公式マニュアル](https://docs.blender.org/manual/en/4.5/editors/preferences/extensions.html)も参照できます。ローカルZIPからのインストールに、拡張機能サイトへのログインは不要です。

## 3. Unityへインストールする（初回だけ）

1. Unity 6000.2.15f1で作業用の3Dプロジェクトを開きます。
2. `Window > Package Management > Package Manager` を開きます。
3. 左上の `+` から `Install package from tarball...` を選び、`Unity/com.kuko.motionbridge-0.1.1.tgz` を指定します。導入方法は[Unity 6.2公式マニュアル](https://docs.unity3d.com/6000.2/Documentation/Manual/upm-ui-tarball.html)に従います。
4. パッケージ一覧で `Motion Bridge` を選び、`Samples` の `Canonical Source Avatar` を `Import` します。これがBlenderから届いた姿勢を変換するための基準Avatarです。
5. 自分のHumanoidキャラクターをProjectからHierarchyへドラッグします。Project内のPrefab/FBXそのものではなく、シーンに配置したキャラクターを使用します。
6. FBXのキャラクターでは、Inspectorの `Rig > Animation Type` が `Humanoid` で、有効なAvatarが作成されていることを確認します。変更した場合は `Apply` を押します。

キャラクターと親オブジェクトのScaleは、各オブジェクトのX・Y・Zが同じ正の値にしてください（例：`1, 1, 1`）。プレビュー中はMotion BridgeがAnimatorの姿勢を制御します。

## 4. Unityでプレビュー待受を開始する

1. `Window > Motion Bridge` を開き、`かんたんガイド` をONにします。
2. Hierarchyでキャラクターを選択し、`Hierarchyで選択中のHumanoidを使う` を押します。
3. `import済みCanonical Avatarを自動設定` を押します。
4. キャラクターとCanonical Avatarの準備ができたら、`プレビュー待受を開始` を押します。
5. `Unityの準備完了。次にBlenderの「Unityへ接続」を押します。` と表示されたら、そのままUnityを開いておきます。

`Unityを再生してもプレビューを続ける` はONのままで構いません。通常はTCPポートとSession IDを変更しません。

## 5. Blenderでモーションを準備する

1. 配布のCanonicalファイルから作った作業用 `.blend` を開きます。
2. 3D Viewにマウスを置いて `N` キーを押し、右側の `Motion Bridge > はじめてガイド` を開きます。
3. `編集用リグを選択` が表示されていれば押します。
4. `モーションFBXを読み込む` から、自分のモーションファイルを選びます。glTF / GLBも選べます。複数のモーション候補が表示された場合は使う候補を1つ選びます。
5. `モーション全体（開始 - 終了）を使用` が表示されたら押します。初期のシーン範囲のままだと、長いモーションが途中で切れます。
6. ボーン名の認識を `自動` のまま、`編集用リグへ変換` を押します。
7. `修正レイヤーを作成` を押します。元の動きはBaseに保存され、修正は別レイヤーに記録されます。

認識できないボーンがある場合は `認識結果を確認して不足分を選ぶ` を押し、表示された項目へ元リグの対応するボーンを指定してから、再度 `編集用リグへ変換` を押します。複数Armatureを含むファイルやアニメーションキーのないファイルは読み込めません。モデルのボーン名を変更する必要はありません。

## 6. BlenderからUnityへ接続する

Blenderの `Unityで確認` にある `Unityへ接続` を押します。Blenderに `接続済み：修正がUnityへ反映されています`、Unityに `リアルタイム反映中です` と表示されれば接続完了です。Blenderのタイムラインを動かして確認します。

同じPC内の通信だけに対応します。UnityのPlay開始・停止時には一度接続が切り替わり、自動で再接続します。UDPポートは自動設定されます。

## 7. モーションを修正する

1. Blenderで `モーション修正を開始` を押します。
2. 色付きの細いボーンを選び、Timelineで修正するフレームへ移動します。大きく薄い元リグは比較用です。
3. `R` キーでボーンを回して確定し、`4. このフレームに修正を記録` を押します。
4. 必要なフレームを修正したら `修正作業を終了` を押します。

位置移動はHipsを1本だけ選んだ場合に限り、`Hipsの位置移動も記録` をONにして `G` キーで調整します。腕や脚は回転で修正します。

複数フレームへなじませる場合は、修正モードでボーンと対象フレームを選んでポーズを作り、次の順に進めます。

1. `フレーム範囲を数値で指定` をONにして開始・終了を入力し、現在フレームをその範囲内へ置きます。選択中の修正キーが2フレーム以上ある場合は、その選択範囲が優先されるため、数値範囲を使うときはGraph Editor / Dope Sheetのキー選択を解除します。
2. `Smooth Pulse` や `Plateau` を選び、`範囲へ自然になじませる` を押します。
3. カーブを動かして確認し、`この結果で確定` または `開始前へ戻す` を押します。縦軸の `0` は元の動き、`1` は修正を完全に適用した動きです。
4. 終わったら `修正作業を終了` を押します。確定後は同じボーンを選び、`確定したカーブを再編集` から調整できます。

元のポーズを別フレームへ移す場合は、修正作業を終了してから、コピー元で `元モーションをコピー`、移動先で `現在フレームへ貼り付け` を押します。コピーした全身のBaseポーズを、選択中の修正レイヤーへ1フレームだけ記録します。

## 8. 品質確認とワンクリック補正（任意）

修正作業を終了して `モーションを確認` を押すと、足滑りや急な動きの候補が表示されます。問題区間の `表示` で移動して見た目を確認してください。判定は修正箇所を探すための目安です。

ワンクリック補正は必要な操作だけ、次の順番で使います。

1. `1. 揺れをなめらかに`
2. `2. 関節の曲がりすぎを直す`
3. `3. 足滑りを固定`

各操作は新しい修正レイヤーを作ります。1回の対象は最大600フレームです。長い場合はTimeline上で `P` キーを使ってPreview Rangeを600フレーム以内に設定します。Foot Lockは接地している足の位置を補正しますが、Root/Hips移動、つま先のロール、手のIKは補正しません。処理後は必ず再生して意図した動きか確認します。

## 9. FBXを書き出して使う

1. 開いているカーブを確定または取り消し、`修正作業を終了` を押します。
2. `修正済みFBXを書き出す` を押し、保存先を選びます。修正後表示のレイヤー設定（ミュート・ソロ・重み）を反映し、Baseモーションの全範囲をFBXへ出力します。Preview Rangeだけに範囲を絞る操作ではありません。キャラクターのメッシュやマテリアルは含みません。
3. UnityのProjectへFBXを入れ、Inspectorの `Rig > Animation Type` を `Humanoid`、`Avatar Definition` を `Copy From Other Avatar` にします。
4. `Source` に、手順3でImportした `MB_Canonical_Humanoid_v1` のAvatarを指定して `Apply` を押します。キャラクター側のAvatarではなく、Canonical Source Avatarを使います。
5. インポートされたAnimationClipを、HumanoidキャラクターのAnimator Controller等で使用します。

作業を続ける場合は `.blend` も保存してください。`修正をBaseへベイクして統合` は、確定した修正をBaseへまとめる操作です。個別の修正レイヤーを残したい間は使わずに保存します。

## 更新・取り外し

更新前に作業を保存し、Blender・Unity両側のプレビューを停止します。Blenderは新しい拡張ZIPを `Install from Disk...` でインストールして再起動し、Preferencesのバージョンを確認します。Unityは新しいTGZをPackage Managerからインストールします。既存の作業ファイルは別名コピーで読み込み確認してください。

取り外すときは、BlenderのPreferencesでMotion Bridgeを無効化して削除し、Unityではプレビューを停止して、シーンに追加された `MotionBridgeRuntimePreview` があれば取り外したうえで、Package ManagerのMotion Bridgeを `Remove` します。作業用 `.blend`、書き出したFBX、Import済みサンプルは残ります。

## 困ったとき

| 症状・表示 | 対処 |
|---|---|
| ZIPをインストールできない | 販売用の外側ZIPを展開し、その中の `Blender/motion_bridge-0.1.13.zip` を指定します。 |
| Motion Bridgeタブがない | Preferencesで拡張が有効か確認し、3D View上で `N` を押します。更新後ならBlenderを再起動します。 |
| 編集用リグが見つからない | `Samples/mb_canonical_humanoid_v1.blend` を開いて作業用に別名保存します。 |
| Canonical Avatarが見つからない | UnityのPackage ManagerでMotion Bridgeの `Samples > Canonical Source Avatar > Import` を実行します。 |
| ProjectのPrefabではなくHierarchy側を指定 | キャラクターをHierarchyへ配置し、シーン側を選択します。 |
| Unityのプレビュー待受がまだ開始されていません | Unityで `Window > Motion Bridge > プレビュー待受を開始` を押します。 |
| 接続できない・ポートが使用中 | 他のUnityプレビューを停止します。両側のTCPポートを同じ空き番号に変更することもできます。初期値は `39871` です。 |
| Session IDが一致しない | 両側の接続設定を同じ値へ戻します。初期値は `305419896` です。接続先は `127.0.0.1` のままにします。 |
| Unityの再生切替を待っています | 数秒待ちます。戻らなければ両側のプレビューを停止し、Unity→Blenderの順に開始します。 |
| ボーンの認識に失敗する | `認識結果を確認して不足分を選ぶ` で対応ボーンを設定します。自動認識はすべてのリグを対象にできるわけではありません。 |
| モーションが途中で終わる | 読み込み後の `モーション全体（開始 - 終了）を使用` を押してから編集用リグへ変換します。 |
| ボーンが重なって見える | `比較用の元リグを表示` をOFFにします。細い色付きリグが修正対象です。 |
| 補正ボタンを押せない | カーブを確定/取消し、修正作業を終了し、修正後表示へ戻します。ワンクリック補正はPreview Rangeを600フレーム以内にします。 |
| 古い版でFCurve作成エラーになる | 配布内のBlender拡張へ更新し、Blenderを再起動します。 |

問い合わせ時は、Motion Bridgeのバージョン、Blender/Unityのバージョン、OS、再現手順、エラー全文を添えて[GitHub Issues](https://github.com/95inari/MotionBridge/issues)へ報告してください。モデルやモーション本体は、再配布できる権利を確認したものだけを共有してください。
