# 環境を有効化
source .venv/bin/activate

# 念のため pip ツール自体を最新にアップデート
pip install --upgrade pip

# もう一度インストールに挑戦！
pip install -r requirements.txt

# サーバー起動コマンド→VSCのターミナルから打ち込み
uvicorn main:app --reload

→コマンドのhttpから始まるIPアドレスをクリックしてブラウザ起動

GCP URL
https://executive-ip-dashboard-1025027372191.asia-northeast1.run.app
