# GCP運用保守・リソース点検ガイド

本ドキュメントは、**Executive IP Dashboard** (`vsj-ipdashboard-prod` / `asia-northeast1`) におけるGCPリソースの推奨点検頻度、確認対象、および具体的なチェック手順をまとめた運用ガイドです。

---

## 1. 点検スケジュール（推奨頻度別）

| 頻度 | 点検対象リソース | 点検項目 | 主な目的 / 想定リスク |
| :--- | :--- | :--- | :--- |
| **都度 (デプロイ時)** | Cloud Build / Cloud Run | ビルド成否、最新リビジョンの稼働確認、403/500エラーの有無 | デプロイ失敗、権限リセット（allUsers外れ）の即時検知 |
| **週次 (Weekly)** | Artifact Registry | イメージ数、各イメージのサイズ、クリーンアップポリシー動作 | 3GB級の巨大イメージ滞留によるストレージ課金膨張の防止 |
| **週次 (Weekly)** | Cloud Run ログ | エラーログ（ERROR/CRITICAL）、再起動回数、コールドスタート時間 | 例外発生頻度、APIクォータ枯渇（Gemini API等）の検知 |
| **月次 (Monthly)** | Cloud Billing (請求) | 予算消化率、サービス別コスト内訳（Run, Artifact Registry, API） | 想定外課金の早期発見、予算アラート（Budget Alert）の機能確認 |
| **四半期 (Quarterly)** | Secret Manager / IAM | サービスアカウント権限、シークレット（SESSION_SECRET等）の棚卸し | 不要な権限の削除、クレデンシャル漏洩リスクの低減 |

---

## 2. リソース別チェック手順

### 2.1. Cloud Run（サービス稼働・アクセス制御）

#### 確認コマンド
```powershell
# サービスのステータス・URL・最新リビジョンの確認
gcloud run services describe executive-ip-dashboard --region=asia-northeast1 --project=vsj-ipdashboard-prod --format="yaml(status.url, status.latestReadyRevisionName, status.conditions)"

# allUsers (未認証アクセス) が正しく許可されているかの確認
gcloud run services get-iam-policy executive-ip-dashboard --region=asia-northeast1 --project=vsj-ipdashboard-prod
```
*※ `get-iam-policy` の出力に `roles/run.invoker` -> `allUsers` が含まれていることを確認します。*

#### 直近のエラーログ確認（過去1時間）
```powershell
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=executive-ip-dashboard AND severity>=ERROR" --limit=20 --project=vsj-ipdashboard-prod --format="table(timestamp, severity, textPayload)"
```

---

### 2.2. Artifact Registry（コンテナイメージ・容量）

#### 確認コマンド
```powershell
# 保存されているイメージ一覧とタグ、作成日、サイズを表示
gcloud artifacts docker images list asia-northeast1-docker.pkg.dev/vsj-ipdashboard-prod/ip-dashboard-repo/executive-ip-dashboard --project=vsj-ipdashboard-prod --include-tags --sort-by=~CREATE_TIME

# クリーンアップポリシーの確認
gcloud artifacts repositories describe ip-dashboard-repo --location=asia-northeast1 --project=vsj-ipdashboard-prod --format="yaml(cleanupPolicies)"
```

#### 注意事項・チェックポイント
1. **サイズ確認**:
   - 軽量化後のイメージ（PyTorch/Transformers 削除版）は **約300MB〜600MB** 前後に収まる想定です。
   - 以前の **3.2GB〜3.4GB** のイメージが残存している場合は、ストレージ容量を圧迫するため手動削除を検討してください。
2. **ポリシーのタイムラグ**:
   - `olderThan: 259200s`（3日間）が設定されている場合、3日経過するまでは最新2世代以外も保持されます（正常動作です）。
3. **手動削除（必要な場合）**:
   ```powershell
   # 例: 特定のダイジェストを指定して削除
   gcloud artifacts docker images delete "asia-northeast1-docker.pkg.dev/vsj-ipdashboard-prod/ip-dashboard-repo/executive-ip-dashboard@sha256:IMAGE_DIGEST" --delete-tags --quiet
   ```

---

### 2.3. Cloud Build（ビルド成否・所要時間）

#### 確認コマンド
```powershell
# 直近5回のビルド結果・所要時間・ステータス一覧
gcloud builds list --limit=5 --project=vsj-ipdashboard-prod --format="table(id, status, createTime, duration, source.repoSource.branchName)"
```
- ステータスが `SUCCESS` であること。
- ビルド所要時間が極端に増加していないか確認します（PyTorch削除後はビルド時間・ステップ時間も短縮されます）。

---

### 2.4. Secret Manager（シークレット管理）

#### 確認コマンド
```powershell
# 登録シークレットの一覧
gcloud secrets list --project=vsj-ipdashboard-prod

# SESSION_SECRET のバージョン一覧と有効性
gcloud secrets versions list SESSION_SECRET --project=vsj-ipdashboard-prod
```
- Cloud Run のサービスアカウント（デフォルトまたは専用SA）に `roles/secretmanager.secretAccessor` が付与されていることを前提とします。

---

### 2.5. Cloud Billing（コスト・予算監視）

#### Webコンソール推奨点検項目
- [GCP Billing コンソール](https://console.cloud.google.com/billing) へアクセス
1. **レポート (Reports)**:
   - プロジェクト: `vsj-ipdashboard-prod` でフィルタ。
   - グループ化: `Service`（サービス別）を選択。
   - **確認対象**:
     - `Cloud Run`: リクエスト数・CPU/メモリ時間
     - `Artifact Registry`: ストレージ使用量（GB/月）
     - `Generative Language API / Vertex AI`: Gemini 埋め込みAPIの利用コスト
2. **予算とアラート (Budgets & alerts)**:
   - 月額予算（例: 1,000円〜3,000円等、想定運用規模に応じた額）が設定されているか確認。
   - 50%, 90%, 100% 到達時のメール通知が有効になっているか。

---

## 3. 異常発生時の初動チェックリスト

| 症状 | 考えられる原因 | 初動対応 |
| :--- | :--- | :--- |
| **403 Forbidden** (ブラウザアクセス時) | Cloud Run の `allUsers` 権限が外れた、または組織ポリシー再適用 | `gcloud run services add-iam-policy-binding` で `roles/run.invoker` に `allUsers` を再付与。拒否されたら組織ポリシー (`constraints/iam.allowedPolicyMemberDomains`) を確認。 |
| **500 Internal Server Error** | `SESSION_SECRET` 未設定、Gemini APIキー無効、起動時エラー | `gcloud logging read` でコンテナの `stdout`/`stderr` を確認。環境変数・Secret Manager の参照設定を確認。 |
| **ビルドが失敗する** | `cloudbuild.yaml` の構文エラー、Dockerfile 依存関係不整合 | Cloud Build ログで失敗ステップを特定。`requirements.txt` のバージョン競合を確認。 |
| **請求額が急増している** | 巨大イメージの放置、APIの過剰リクエスト、コンテナ最小インスタンス設定（min-instances > 0） | Artifact Registry で不要イメージを削除。Cloud Run の min-instances が 0（アイドル時ゼロ停止）になっているか確認。 |
