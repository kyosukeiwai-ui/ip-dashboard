# 環境を有効化
source .venv/bin/activate

# 念のため pip ツール自体を最新にアップデート
pip install --upgrade pip

# もう一度インストールに挑戦！
pip install -r requirements.txt

# サーバー起動コマンド→VSCのターミナルから打ち込み
uvicorn main:app --reload

→コマンドのhttpから始まるIPアドレスをクリックしてブラウザ起動

GCP URL：どちらでもよし。推奨下
https://executive-ip-dashboard-1025027372191.asia-northeast1.run.app
https://executive-ip-dashboard-iwz6jwew2a-an.a.run.app

# 〜リストの順番について〜
論文は完全なランダムではありません。
処理は次の流れです。

Geminiがテーマから英語キーワードを生成
Arxiv APIで最大 fetch_max 件を取得
academic_agent_node
取得した論文をEmbedding化
Chromaのベクトル検索で、入力テーマとの意味的な近さを再検索
その検索結果順を top_academic_list に格納
特にこの部分です。

したがって画面上の「Top論文」は、基本的にはテーマとのEmbedding類似度が高い順です。ただし、以下の理由で厳密に毎回同じ順になるとは限りません。

Geminiが生成する検索キーワードが変わる可能性
Arxiv APIの取得結果が変わる可能性
Embedding検索の同点・近似順位
Arxiv側で sortBy を明示していない
また、full_academic_list はChromaで並べ替えず、Arxiv APIから取得した順番のままです。

特許リストの順番
特許も完全なランダムではありません。

GeminiがIPC、コア語、関連語を生成
EPO APIで検索
検索件数が少ない場合、条件を緩めて再検索
最もヒット件数の多い検索結果を採用
特許文書をEmbedding化
テーマとの意味的な近さでChroma検索
その順番を top_patent_list に格納
該当箇所はpatent_agent_nodeです。

特許の検索条件は、おおむね次の優先順位です。
IPC + コア語 + 関連語
IPC + コア語
コア語 + 関連語
コア語のみ

# 学術熱度・特許ホワイトスペース度・FTOリスクに数式や統計的根拠はあるか
現状はありません。
つまり、次の値はGeminiの判断です。
指標	現在の実態

学術熱度 20%	論文データを見たGeminiの主観的推定
特許ホワイトスペース度 40%	特許データを見たGeminiの主観的推定
FTOリスク 65%	特許データを見たGeminiの主観的推定
そのため、同じテーマでも以下の理由で値が変わる可能性があります。

Geminiが生成する検索キーワードの変化
Arxiv/EPOの検索結果の変化
検索結果の件数や内容の変化
Geminiの判断の揺れ
市場データの生成内容の変化

概念上はおそらく次の意味です。
学術熱度：関連論文が多い、または近年の研究活動が活発であるほど高い
特許ホワイトスペース度：関連特許が少なく、参入余地が大きいほど高い
FTOリスク：関連特許が多く、既存権利との抵触可能性が高いほど