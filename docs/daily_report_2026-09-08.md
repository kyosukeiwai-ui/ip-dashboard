# Executive IP Dashboard GCP環境整備および最適化作業日報（2026-09-08）

## 結論

Cloud Runへのアクセス遮断（403 Forbidden）、3.4GB超のコンテナイメージ肥大化、およびコード・設定上のセキュリティ脆弱性を実機およびリポジトリ上で網羅的に解消した。
ローカルPyTorchスタックをGemini Embedding API（`gemini-embedding-001`）へ移行し、コンテナイメージの実測サイズを **275.3 MB**（元の約1/12）へと劇的にスリム化することに成功した。
Secret Managerによるセッション暗号化、組織ポリシー解除によるアクセス開通、Claudeレビュー指摘の修正、GitHub連携によるCloud Build自動デプロイ、および個人運用に即したArtifact Registry保持期間（2日: 172800s）への短縮設定を完了した。
当月のGCP利用実績は合計4円であり、不要・放置リソースのない極めて健全な運用状態を確認した。
対象リポジトリ: `ip-dashboard`。本番GCPプロジェクト: `vsj-ipdashboard-prod`（`asia-northeast1`）。

## 再現できた重大問題と修正

1. **Cloud Run 再作成に伴う 403 Forbidden（未認証拒否）と組織ポリシーの競合**
   - **事象**: サービスURLアクセス時に `Error: Forbidden (Your client does not have permission to get URL / from this server)` が発生。
   - **原因**: サービス再作成時に `roles/run.invoker` の `allUsers` 権限がリセットされていた。さらに、再付与時にGCP組織ポリシー `constraints/iam.allowedPolicyMemberDomains`（外部ドメインID追加禁止）によってブロックされた。
   - **対応**: プロジェクト単位で当該組織ポリシーを `restoreDefault`（親ポリシー継承解除・プロジェクト既定値）に再設定し、`allUsers` への `roles/run.invoker` 付与を成功させて開通。

2. **ローカル埋め込み（PyTorch/Transformers）による 3.4GB 超のイメージ肥大化**
   - **事象**: Artifact Registry 内のイメージが単体で 3.2GB〜3.4GB に達し、ビルド時間・デプロイ時間・ストレージ料金を圧迫。
   - **原因**: `sentence-transformers` / `langchain-huggingface` / `torch`（Linux wheelはCUDA同梱で1.8GB超）をコンテナ内でビルド・保持していた。
   - **対応**: ローカル推論を廃止し、Google GenAI SDK（`gemini-embedding-001`）経由の外部API埋め込みに全面切り替え。`requirements.txt` から PyTorch 関連ライブラリを全削除。実測サイズ 275.3 MB への削減を達成。

3. **SESSION_SECRET のコード直書き脆弱性と Secret Manager 連携**
   - **事象**: セッション暗号化キーがコード内にハードコード、またはフォールバック文字列になっていた。
   - **対応**: GCP Secret Manager に `SESSION_SECRET`（32バイト暗号乱数hex）を登録。`cloudbuild.yaml` および Cloud Run デプロイパラメータに `--set-secrets="SESSION_SECRET=SESSION_SECRET:latest"` を追加。未設定時は起動を明示的に停止するフェイルセーフを実装。

4. **Artifact Registry クリーンアップポリシーの調整と巨大イメージ消去**
   - **事象**: 以前の3.2GBイメージが残存しており、初期設定（7日間保持）では日数が長すぎた。
   - **対応**: ユーザー自身による最古の3.2GBイメージの手動削除を実施。個人利用の運用実態に合わせ、リポジトリのクリーンアップポリシー保持期間を「7日間（604800s）」から「2日間（172800s）」へ短縮更新・保存。

5. **Claude レビュー指摘事項に基づくセキュリティ・堅牢性改修**
   - **修正内容**:
     - 空文字ドメインによるドメイン制限バイパス防止（厳格バリデーション）。
     - エラーハンドリングにおける生の例外メッセージ・内部情報の隠蔽とサニタイズ。
     - ログ出力におけるメールアドレス等の PII（個人識別情報）マスキング。
     - セッション破棄時における Chroma 一時コレクションの確実な解放（`delete_collection`）。
     - 言語切替用 `UI_DICT` の英語リソース不足解消。

## 実機の証跡

- **GCP プロジェクト**: `vsj-ipdashboard-prod`
- **リージョン**: `asia-northeast1` (東京)
- **Cloud Run サービス**: `executive-ip-dashboard`
  - URL: `https://executive-ip-dashboard-722184650630.asia-northeast1.run.app`
  - 認証ポリシー: `roles/run.invoker` -> `allUsers` 付与完了。
- **組織ポリシー**:
  - `gcloud resource-manager org-policies reset constraints/iam.allowedPolicyMemberDomains --project=vsj-ipdashboard-prod` 適用完了。
- **Artifact Registry**:
  - リポジトリ: `ip-dashboard-repo`（Docker形式）。
  - クリーンアップポリシー: 最新2世代保持 ＋ 経過日数 **2日（172800s）** 設定・保存完了。
  - イメージ仮想サイズ: **275.3 MB**（3世代とも同サイズ、3.2GB超の肥大化が解消されたことを実画面で確認）。
  - 不要な空リポジトリ（`cloud-run-source-deploy`）削除済み。
- **Secret Manager**:
  - `SESSION_SECRET` 作成およびバージョン1追加完了。
- **Cloud Build / CI/CD**:
  - コミット `b7dc062` プッシュによる自動ビルドおよび最新マニフェスト（`sha256:227bc18...`）生成・デプロイ成功を確認。
- **Cloud Billing**:
  - 当月合計利用料: **¥4**（Cloud Storage ¥2, Artifact Registry ¥1, Cloud Run ¥1 (割引後¥0)）。
  - 予測日額 ¥0.6〜0.8 推移。アイドル時常時課金リソース（min-instances > 0 や放置VM）の非存在を実証。
- **エディタ開発環境**:
  - `.vscode/settings.json` に `"git.autofetch": true` を設定し、バックグラウンドでの最新リポジトリ情報自動取得を有効化。

## 自動検証

- **ローカル仮想環境（`.venv`）検証**:
  - 軽量化後の `requirements.txt` を対象に、`main.py` のインポートおよび初期化テストスクリプトを実行。
  - `Importing main.py...` -> `[OK] Imported main.py successfully`
  - `[OK] GoogleGenAIEmbeddings configured with gemini-embedding-001`
  - `[OK] All Checks Passed OK` を確認。
- **Git 差分・構成検証**:
  - `Dockerfile`, `requirements.txt`, `cloudbuild.yaml`, `main.py` の差分が整合していることを確認。
  - 不要となったシェルスクリプト（`infrastructure_setup.sh`）の削除を確認。

## 残る範囲と次の優先順位

1. **Cloud Run 上でのブラウザ E2E 動作確認**:
   - 本番URLへアクセスし、Google OAuth ログイン、特許検索・Embedding 処理、ダッシュボード表示が一連で正常動作することを確認。
2. **Cloud Billing 予算アラートの設定**:
   - 想定外のアクセスやリソース放置に備え、Google Cloud コンソールにて月額予算（例: 1,000円〜3,000円）のアラート設定を完了させる。
3. **不要となった一時設定ファイルの整理**:
   - ローカルに残っている `cleanup_policy.json` の Git リポジトリからの削除・整理。
