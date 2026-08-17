# Dockerfile
# 1. ベースイメージ: 軽量かつセキュリティアップデートが適用されたPython公式イメージ
FROM python:3.11-slim-bookworm

# 2. 環境変数の設定
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 3. 作業ディレクトリの設定
WORKDIR /app

# 4. 【重要修正】システム依存パッケージのインストール
# ChromaDBなどのC++拡張を含むパッケージをpip installするために build-essential (gcc等) を追加します。
# セキュリティと軽量化のため、インストール後にaptのキャッシュ(lists)を削除するベストプラクティスを適用。
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 5. 依存関係のインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 6. アプリケーションコードのコピー
COPY . .

# 7. セキュリティ対策: 非rootユーザーでの実行
RUN useradd -m appuser && chown -R appuser /app
USER appuser

# 8. Cloud Run起動コマンド
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]