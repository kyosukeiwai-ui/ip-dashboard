このコードをレビューしました。全体の設計（LangGraphでAcademic/Patent/Marketの3エージェントを並べ、構造化出力で経営層向けダッシュボードを合成する構成)は良くできていますが、**セキュリティ上見過ごせない問題がいくつかあります**。優先度順に整理します。

## 🔴 重大なセキュリティ問題

**1. セッションシークレットのデフォルト値がハードコードされている**
```python
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "super-secret-session-key"))
```
`SESSION_SECRET` が未設定だと誰でも知っている固定文字列がキーになり、セッションCookieの署名を偽造できてしまいます。認証バイパスに直結する問題です。デフォルト値を持たせず、未設定なら起動時に例外を投げるべきです。
```python
session_secret = os.environ["SESSION_SECRET"]  # 未設定ならKeyErrorで起動失敗させる
```
また `SessionMiddleware` に `https_only=True`（本番運用時）や `same_site="lax"` を明示していない点も気になります。

**2. `ALLOWED_DOMAINS` 未設定時にドメインチェックが無効化される**
```python
ALLOWED_DOMAINS = os.getenv("ALLOWED_DOMAINS", "").split(",")
```
環境変数が未設定だと `ALLOWED_DOMAINS = [""]` になります。一方、`user_email` に `@` が含まれない異常系では `domain = ""` になり、`"" in [""]` が真になってしまうため、**設定ミス一つで誰でもログインできてしまう**危険な組み合わせです。少なくとも次のように防御すべきです。
```python
ALLOWED_DOMAINS = [d.strip() for d in os.getenv("ALLOWED_DOMAINS", "").split(",") if d.strip()]
...
if not domain or domain not in ALLOWED_DOMAINS:
    raise HTTPException(status_code=403, detail="許可されていないドメインです")
```

**3. `/analyze` の例外処理がエラー内容をそのままHTMLに埋め込んでいる**
```python
except Exception as e:
    return HTMLResponse(f"<h1>分析中にエラーが発生しました</h1><p>{str(e)}</p>", status_code=500)
```
- スタックトレースやAPIレスポンス文字列（EPOの認証エラーメッセージなど）がそのままクライアントに漏れる可能性があります。
- `str(e)` がエスケープなしでHTMLに挿入されているため、エラーメッセージに `<script>` 等が混入するケース（外部APIレスポンスやLLM出力起因）で**反射型XSS**になり得ます。

サーバー側でログに出し、ユーザーには汎用メッセージのみ返すのが安全です。
```python
except Exception as e:
    logger.exception("analyze failed")
    return HTMLResponse("<h1>分析中にエラーが発生しました。時間をおいて再度お試しください。</h1>", status_code=500)
```

**4. デバッグログでメールアドレス等のPIIを平文出力**
```python
print(f"[AUTH_DEBUG] ログイン試行: {user_email}")
```
本番コードに残ったままだと個人情報がログに残り続けます。少なくとも `logging` モジュール＋適切なログレベル（DEBUG）に切り替え、本番では出力しない設定にすべきです。

**5. `json_data` / `csv_data` をテンプレートへ渡す箇所（テンプレート未確認だが要注意）**
特許・論文タイトルは外部API（EPO/Arxiv）由来の自由入力に近いデータです。これを `<script>` タグ内にJSON文字列として埋め込む場合、`</script>` や `<` を含むタイトルがあると**スクリプトインジェクション**の危険があります。`json.dumps` の結果をそのままテンプレートに `|safe` で埋め込んでいないか、`<` を `\u003c` にエスケープしているか確認してください。

## 🟠 バグ・ロジックの問題

**6. `retrieve_k` パラメータが実質未使用**
フォームで `retrieve_k` を受け取り `AgentState` にも保持していますが、実際のベクトル検索では常に `fetch_max` が使われています。
```python
retrieved_docs = vectorstore.as_retriever(search_kwargs={"k": state["fetch_max"]}).invoke(state["theme"])
```
ユーザーが `retrieve_k` を変えても結果に反映されません。`state["retrieve_k"]` に修正が必要です（academic・patent両ノード）。

**7. `UI_DICT` が `"ja"` しか定義されていない**
`output_language` は `"ja"`/`"en"` を切り替えられる想定なのに、
```python
ui_text = UI_DICT.get(lang_code, UI_DICT["ja"])
```
`en` を渡してもUI文言は日本語のままになり、LLM生成コンテンツ（英語）と画面ラベル（日本語）が混在します。英語版辞書を追加するか、未対応なら選択肢自体をUIから外すべきです。

**8. Chromaのコレクションが後始末されずリークする**
```python
vectorstore = Chroma.from_documents(documents=docs, embedding=embeddings, collection_name=f"arxiv_{uuid.uuid4().hex[:8]}")
```
毎リクエストごとにユニークなコレクションを作成していますが、使用後に削除していません。永続化ストレージを使っている場合、リクエストのたびにディスク/メモリが増え続けます。
```python
retrieved_docs = vectorstore.as_retriever(...).invoke(...)
vectorstore.delete_collection()
```
のような後片付け、あるいはインメモリの一時クライアントを明示的に使う方が安全です。

**9. `fetch_max` / `retrieve_k` に上限バリデーションがない**
```python
fetch_max: int = Form(30)
```
ユーザーが極端に大きい値を送ると、Arxiv/EPOへの大量リクエスト、埋め込み生成、LLM呼び出しコストが際限なく増えます。DoS・コスト爆発のリスクなので、`Form(30, ge=1, le=50)` のように範囲を制約すべきです。

**10. OAuthコールバックで `userinfo` が `None` の場合が未考慮**
```python
user = token.get('userinfo')
...
user_email = user.get("email", "")
```
IdPの応答形式によっては `userinfo` が取得できないケースがあり、その場合 `user` が `None` で `AttributeError` になります（500エラーで落ちるだけなので致命的ではないですが、明示的にハンドリングした方が安全です）。

## 🟡 その他の改善提案

- **`print` デバッグ文の全廃**：本番運用を意識するなら `logging` に統一し、ログレベルで制御する。
- **モデル名の確認**：`model="gemini-3.1-flash-lite"` が実在するモデルIDか、現行のGoogle Generative AI SDKで有効かを一度確認しておくと安心です。
- **ファイル分割**：ルーティング／LangGraphエージェント定義／ヘルパー関数が1ファイルに同居しており、テスト・保守がしづらい構成です。`agents.py`, `routers/auth.py`, `routers/dashboard.py` などへの分割を推奨します。
- **マジックナンバー**：`THRESHOLD = 50` に根拠のコメントがあると、後任者が調整しやすくなります。
- **同期I/Oの多用**：`analyze_theme` はLLM呼び出しやHTTPリクエストを多数同期実行しています。FastAPIのスレッドプールで動きますが、同時アクセスが増えるとスレッド枯渇のボトルネックになり得るため、将来的には非同期クライアント（`httpx.AsyncClient`など）やバックグラウンドジョブ化を検討する価値があります。

---

まず着手すべきは **1〜5のセキュリティ項目**（特にセッションシークレットとドメイン許可リストの空文字列バイパス）です。これらは設定ミス一つで認証をすり抜けられる実害の大きい問題なので、最優先での修正をおすすめします。